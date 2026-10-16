"""One service-wide admission queue and byte ledger for every render entrance."""
from __future__ import annotations

import hashlib
import json
import secrets
import struct
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .data.errors import DataError, QueryLimitError
from .renderer import RenderResult, _series_values, compose_view, encode_png
from .video import IndexedDecoder, StaleGeneration
from .viewstate import encode_view, resolve_frame

MiB = 1024**2
PIXELS = PNG_BYTES = 64 * MiB
INDEX_BYTES = 8 * MiB
SERIES_BYTES = 2 * MiB


class ServiceBusy(DataError):
    pass


class SessionExpired(DataError):
    pass


@dataclass(eq=False)
class Ticket:
    owner: str
    pixels: int
    session_id: str | None = None
    generation: int | None = None
    cancelled: bool = False
    released: bool = False


@dataclass
class Session:
    id: str
    state: object
    generation: int = 0
    touched: float = field(default_factory=time.monotonic)
    decoder: object = None
    decoder_bytes: int = 0
    users: int = 0
    closed: bool = False
    lock: object = field(default_factory=threading.RLock)
    timeline_index: tuple | None = None


@dataclass
class Block:
    png: bytes
    manifest: dict
    refs: int = 0
    cached: bool = True


class Lease:
    def __init__(self, service, ticket, block, state, timing):
        self.service, self.ticket, self.block, self.state, self.timing = service, ticket, block, state, timing
        self.closed = False
        with service.cv: service.leases.add(self)

    @property
    def png(self): return self.block.png

    @property
    def manifest(self): return self.block.manifest

    def close(self):
        with self.service.cv:
            if self.closed: return
            self.closed = True; self.block.refs -= 1
            self.service.leases.discard(self)
            if not self.block.cached and self.block.refs == 0:
                self.service.png_used -= len(self.block.png)
            self.service._release(self.ticket)


class RenderService:
    def __init__(self, adapter, settings):
        self.adapter, self.settings = adapter, settings
        self.cv = threading.Condition(threading.RLock())
        self.active = self.pixel_used = self.png_used = self.png_reserved = 0
        self.peak_active = self.peak_waiting = self.peak_pixels = self.peak_png = 0
        self.waiting = deque(); self.sessions = {}; self.cache = OrderedDict(); self.indices = {}
        self.series = OrderedDict(); self.series_bytes = self.index_bytes = 0
        self.closed = False
        self.leases = set()
        self.dispatches = set()
        self.peak_dispatch = 0
        self.audit_bytes = self.peak_audit_bytes = 0
        # Six admitted jobs total: at most two execute, four wait. No render
        # jobs can collect in asyncio's unrelated default executor queue.
        self.executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="bounded-render")
        # Adapter content is immutable for the lifetime of this service instance.
        self.version = secrets.token_hex(16) + ":png-player-v2:level1"
        self.stop_reaper = threading.Event()
        self.reaper = threading.Thread(target=self._reap_loop, daemon=True, name="playback-expiry")
        self.reaper.start()

    def _reap_loop(self):
        while not self.stop_reaper.wait(15):
            with self.cv: self._expire()

    def _drop_decoder(self, session):
        if session.decoder is not None and session.users == 0:
            session.decoder.close(); session.decoder = None
            self.pixel_used -= session.decoder_bytes; session.decoder_bytes = 0

    def _expire(self):
        for sid, session in list(self.sessions.items()):
            if session.closed or time.monotonic() - session.touched >= 300:
                session.closed = True; self.cancel_session(sid); self._drop_decoder(session)
                if session.users == 0: self.sessions.pop(sid, None)

    def _trim(self, need, exclude):
        for session in sorted(self.sessions.values(), key=lambda s: s.touched):
            if self.pixel_used + need <= PIXELS: break
            if session.id != exclude and not session.users: self._drop_decoder(session)

    def _acquire(self, owner, pixels, valid=lambda: True, session_id=None, generation=None):
        ticket = Ticket(owner, pixels, session_id, generation); started = time.perf_counter()
        with self.cv:
            self._expire(); self._trim(pixels, owner)
            if pixels > PIXELS: raise QueryLimitError("pixel request exceeds service budget")
            if len(self.waiting) >= 4: raise ServiceBusy("render waiting queue is full")
            self.waiting.append(ticket); self.peak_waiting = max(self.peak_waiting, len(self.waiting))
            while True:
                if self.closed or ticket.cancelled or not valid():
                    if ticket in self.waiting: self.waiting.remove(ticket)
                    self.cv.notify_all(); raise StaleGeneration("render generation is obsolete")
                self._trim(pixels, owner)
                if self.waiting[0] is ticket and self.active < 2 and self.pixel_used + pixels <= PIXELS:
                    self.waiting.popleft(); self.active += 1; self.pixel_used += pixels
                    self.peak_active = max(self.peak_active, self.active); self.peak_pixels = max(self.peak_pixels, self.pixel_used)
                    return ticket, (time.perf_counter() - started) * 1000
                self.cv.wait(.1)

    def _release(self, ticket):
        if not ticket.released:
            ticket.released = True; self.active -= 1; self.pixel_used -= ticket.pixels; self.cv.notify_all()

    def cancel(self, owner):
        with self.cv:
            for ticket in list(self.waiting):
                if ticket.owner == owner:
                    ticket.cancelled = True; self.waiting.remove(ticket)
            self.cv.notify_all()

    def cancel_session(self, sid, current_generation=None):
        with self.cv:
            for ticket in list(self.waiting):
                if ticket.session_id == sid and (current_generation is None or ticket.generation != current_generation):
                    ticket.cancelled = True; self.waiting.remove(ticket)
            self.cv.notify_all()

    def stats(self):
        with self.cv:
            return dict(active=self.active, waiting=len(self.waiting), sessions=len(self.sessions),
                        png_bytes=self.png_used, png_reserved=self.png_reserved, pixel_bytes=self.pixel_used,
                        peak_active=self.peak_active, peak_waiting=self.peak_waiting, peak_png=self.peak_png,
                        peak_pixels=self.peak_pixels, index_bytes=self.index_bytes, series_bytes=self.series_bytes,
                        dispatches=len(self.dispatches), dispatch_waiting=max(0,len(self.dispatches)-2), peak_dispatch=self.peak_dispatch,
                        audit_work_bytes=self.audit_bytes,peak_audit_work_bytes=self.peak_audit_bytes)

    def analysis(self, fn, owner, valid=lambda: True):
        """Audits share render admission, with a separate 32 MiB total work reservation."""
        ticket, _ = self._acquire(owner,0,valid)
        reserved=False
        try:
            with self.cv:
                while self.audit_bytes:
                    if self.closed or not valid(): raise StaleGeneration("audit cancelled")
                    self.cv.wait(.05)
                if self.closed or not valid(): raise StaleGeneration("audit cancelled")
                self.audit_bytes=32*MiB; reserved=True
                self.peak_audit_bytes=max(self.peak_audit_bytes,self.audit_bytes)
            value=fn()
            if not valid(): raise StaleGeneration("audit cancelled before publish")
            return value
        finally:
            with self.cv:
                if reserved: self.audit_bytes=0
                self._release(ticket)

    def reserve_dispatch(self, owner):
        with self.cv:
            if self.closed or len(self.dispatches) >= 6: raise ServiceBusy("service task admission is full (2 executing, 4 waiting)")
            reservation = Dispatch(self, owner); self.dispatches.add(reservation)
            self.peak_dispatch = max(self.peak_dispatch,len(self.dispatches))
            return reservation

    def _dimensions(self):
        dims = self.adapter.info.get("features", {}).get("base_0_camera/resolution")
        if not dims or len(dims) != 2: raise QueryLimitError("camera resolution is required for resource reservation")
        return int(dims[0]), int(dims[1])

    def _decoder(self, session, ticket):
        # Caller holds session.lock and has reserved source/codec working pixels.
        if session.decoder is None:
            episode = session.state.episode; key = self.adapter.video_key(episode)
            path = self.adapter.materialize(key)
            with self.cv:
                index = self.indices.get(episode)
                estimate = 1024 + self.adapter.episodes[episode].length * 256
                if index is None and self.index_bytes + estimate > INDEX_BYTES:
                    raise QueryLimitError("PTS/index cache exceeds 8 MiB")
                if index is None: self.index_bytes += estimate
            try:
                decoder = IndexedDecoder(path, self.adapter.episodes[episode].length, index=index, expected_dimensions=self._dimensions())
                if decoder.width != self._dimensions()[0] or decoder.height != self._dimensions()[1]:
                    decoder.close(); raise QueryLimitError("video dimensions differ from reserved metadata")
            except Exception:
                with self.cv:
                    if index is None: self.index_bytes -= estimate
                raise
            with self.cv:
                if index is None: self.indices[episode] = decoder.index
                session.decoder = decoder
                pixels = decoder.width * decoder.height * 32
                ticket.pixels -= pixels; session.decoder_bytes = pixels
        return session.decoder

    def prepare(self, state, valid=lambda: True, owner=None):
        session = Session(secrets.token_urlsafe(24), state)
        w, h = self._dimensions()
        ticket, _ = self._acquire(owner or session.id, w * h * 32, valid)
        try:
            with self.cv:
                if len(self.sessions) >= 8: raise ServiceBusy("eight playback sessions are already in use")
                self.sessions[session.id] = session; session.users += 1
            with session.lock:
                decoder = self._decoder(session, ticket)
                # PTS is read-only and Arrow columns are immutable. These are
                # references to existing data, not another full index allocation.
                timeline_index = (decoder.pts, decoder.time_base,
                                  self.adapter.table(state.episode)["timestamp_ns"])
            with self.cv:
                session.timeline_index = timeline_index
            if not valid(): raise StaleGeneration("prepare was cancelled")
            pts_first, pts_last = int(decoder.pts[0]), int(decoder.pts[-1])
            return dict(session=session.id, generation=0, view=state.to_dict(), view_url=self.url(state),
                        resolved_frame=resolve_frame(state, self.adapter), length=len(decoder.pts),
                        source_fps=(len(decoder.pts) - 1) / float((pts_last - pts_first) * decoder.time_base),
                        max_image_bytes=self.settings.max_image_bytes, max_json_bytes=self.settings.max_query_bytes,
                        stats=self.stats())
        except Exception:
            session.closed = True
            raise
        finally:
            with self.cv:
                session.users = max(0, session.users - 1)
                if session.closed: self._drop_decoder(session); self.sessions.pop(session.id, None)
                self._release(ticket)

    def get_session(self, sid):
        with self.cv:
            self._expire(); session = self.sessions.get(sid)
            if session is None or session.closed: raise SessionExpired("playback session expired; prepare the view again")
            session.touched = time.monotonic(); return session

    def generation(self, sid, generation, release=False):
        session = self.get_session(sid)
        with self.cv:
            if type(generation) is not int or not 0 <= generation <= 2**31 - 1:
                raise ValueError("generation must be a bounded non-negative integer")
            if generation < session.generation: raise StaleGeneration("older generation control")
            session.generation = generation; session.closed = release; self.cancel_session(sid, None if release else generation)
            if release:
                self._drop_decoder(session)
                if not session.users: self.sessions.pop(sid, None)
            return dict(generation=session.generation, released=release, stats=self.stats())

    def timeline(self, sid, cursor, limit):
        with self.cv:
            session = self.get_session(sid)
            pts, base, timestamps = session.timeline_index
        # No decoder/user lease is needed: these immutable references remain
        # valid even if the decoder is evicted or the session ends during slicing.
        # In particular, never wait for session.lock on the async HTTP thread.
        if not 0 <= cursor < len(pts) or not 1 <= limit <= 512: raise ValueError("timeline range is invalid")
        end = min(cursor + limit, len(pts))
        if (end - cursor) * 3 > self.settings.max_query_scalars: raise QueryLimitError("timeline scalar limit")
        times = timestamps.slice(cursor, end - cursor).to_pylist()
        result = dict(rows=[dict(frame=i, pts_ns=int(round(int(pts[i]) * base * 1_000_000_000)), timestamp_ns=int(times[i - cursor])) for i in range(cursor, end)],
                      truncated=end < len(pts), next_cursor=end if end < len(pts) else None)
        if len(json.dumps(result).encode()) > self.settings.max_query_bytes: raise QueryLimitError("timeline JSON limit")
        return result

    def url(self, state):
        return encode_view(state, self.settings.public_base_url, self.settings.max_view_url_chars)

    def _evict_png(self, need):
        for key in list(self.cache):
            if self.png_used + self.png_reserved + need <= PNG_BYTES: break
            block = self.cache.pop(key); block.cached = False
            if not block.refs: self.png_used -= len(block.png)

    def _curves(self, state):
        # Immutable scalar reductions only; raw vectors are not cached here.
        result = {}
        for selector in state.series if "series" in state.panels else ():
            key = (state.episode, selector)
            with self.cv:
                arr = self.series.get(key)
                if arr is None:
                    size = self.adapter.episodes[state.episode].length * 8 + len(selector.encode("utf-8")) + 512
                    if size + self.series_bytes > SERIES_BYTES: continue
                    # Reserve before conversion; construction is serialized.
                    self.series_bytes += size
                    try:
                        arr = _series_values(self.adapter.table(state.episode), selector, 0, self.adapter.episodes[state.episode].length)
                        arr.setflags(write=False); self.series[key] = arr
                    except Exception:
                        self.series_bytes -= size; raise
                result[selector] = arr
        return result

    def render(self, state, *, sid=None, frame=None, generation=None, owner=None, valid=lambda: True):
        session = self.get_session(sid) if sid else Session(owner or secrets.token_urlsafe(16), state)
        if sid:
            if type(frame) is not int or not 0 <= frame < self.adapter.episodes[state.episode].length: raise ValueError("frame is out of range")
            if generation != session.generation: raise StaleGeneration("frame generation is obsolete")
            state = replace(session.state, cursor={"frame": frame}, time_range=None, evidence=None)
        def current(): return valid() and not self.closed and not session.closed and (not sid or generation == session.generation)
        w, h = self._dimensions(); scratch = state.render["width"] * state.render["height"] * 8 + w * h * 8
        # Reserve even when a decoder currently exists: another admitted job
        # may evict that idle decoder while this job is waiting.
        codec = w * h * 32
        ticket, queue_ms = self._acquire(owner or session.id, scratch + codec, current, sid, generation)
        reserved = 0; published = False
        with self.cv: session.users += 1
        try:
            key = (self.version, json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")))
            with self.cv:
                if not current(): raise StaleGeneration("obsolete before cache access")
                block = self.cache.get(key)
                if block:
                    block.refs += 1; self.cache.move_to_end(key)
                    published = True
                    return Lease(self, ticket, block, state, dict(cache_hit=True, queue_ms=queue_ms, decode_ms=0, compose_ms=0, encode_ms=0))
                self._evict_png(self.settings.max_image_bytes)
                if self.png_used + self.png_reserved + self.settings.max_image_bytes > PNG_BYTES:
                    raise ServiceBusy("service PNG budget is pinned by active responses")
                reserved = self.settings.max_image_bytes; self.png_reserved += reserved
                self.peak_png = max(self.peak_png, self.png_used + self.png_reserved)
            start = time.perf_counter()
            with session.lock:
                decoder = self._decoder(session, ticket)
                image, pts = decoder.get(resolve_frame(state, self.adapter), current)
                pts_ns = int(round(pts * decoder.time_base * 1_000_000_000))
            decoded = time.perf_counter()
            if not current(): raise StaleGeneration("obsolete after video decode")
            canvas, manifest = compose_view(self.adapter, state, image, series_cache=self._curves(state))
            composed = time.perf_counter(); png = encode_png(canvas); encoded = time.perf_counter()
            if len(png) > self.settings.max_image_bytes: raise QueryLimitError("PNG exceeds max_image_bytes; request a smaller render size")
            if not current(): raise StaleGeneration("obsolete before publish")
            manifest = {**manifest, "pts": pts, "pts_ns": pts_ns}
            block = Block(png, manifest, 1)
            with self.cv:
                if not current(): raise StaleGeneration("obsolete before cache publication")
                self.png_reserved -= reserved; reserved = 0; self.png_used += len(png)
                # A concurrent equal render may already own this key. Keep its
                # block alive for its response but remove only the cache ref.
                previous = self.cache.pop(key, None)
                if previous:
                    previous.cached = False
                    if not previous.refs: self.png_used -= len(previous.png)
                self.cache[key] = block; self.peak_png = max(self.peak_png, self.png_used + self.png_reserved)
            published = True
            return Lease(self, ticket, block, state, dict(cache_hit=False, queue_ms=queue_ms,
                         decode_ms=(decoded-start)*1000, compose_ms=(composed-decoded)*1000, encode_ms=(encoded-composed)*1000))
        except Exception:
            with self.cv:
                self.png_reserved -= reserved
            raise
        finally:
            with self.cv:
                session.users -= 1
                if not sid or session.closed: self._drop_decoder(session)
                # Pixel temporaries are gone at return, but the task/PNG lease
                # remains held until the actual HTTP/MCP response has ended.
                if not ticket.released:
                    self.pixel_used -= ticket.pixels; ticket.pixels = 0; self.cv.notify_all()
                    if not published: self._release(ticket)

    def envelope(self, lease):
        meta = dict(view=lease.state.to_dict(), view_url=self.url(lease.state), manifest=lease.manifest,
                    frame=lease.manifest["frame"], pts=lease.manifest["pts"], pts_ns=lease.manifest["pts_ns"],
                    png_bytes=len(lease.png), sha256=hashlib.sha256(lease.png).hexdigest(), timing=lease.timing)
        encoded = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.settings.max_query_bytes: raise QueryLimitError("frame metadata JSON limit")
        return (b"LRF1" + struct.pack(">I", len(encoded)), encoded, lease.png)

    def close(self):
        self.stop_reaper.set(); self.reaper.join(timeout=2)
        with self.cv:
            self.closed = True
            for session in self.sessions.values(): session.closed = True
            # Lifespan shutdown runs after HTTP request teardown. Published
            # leases are no longer serving a live response; native work still
            # retains its own ticket until it actually returns.
            self.cv.notify_all()
            while self.active or self.dispatches:
                for lease in list(self.leases): lease.close()
                if self.active or self.dispatches: self.cv.wait(.1)
            for session in self.sessions.values(): self._drop_decoder(session)
            self.sessions.clear(); self.cache.clear(); self.indices.clear(); self.series.clear()
            self.png_used = self.index_bytes = self.series_bytes = 0
        self.executor.shutdown(wait=True)


class Dispatch:
    def __init__(self, service, owner):
        self.service, self.owner = service, owner
        self.computed = self.responded = False

    def finish(self, *, compute=False, response=False):
        with self.service.cv:
            self.computed |= compute; self.responded |= response
            if self.computed and self.responded:
                self.service.dispatches.discard(self); self.service.cv.notify_all()
