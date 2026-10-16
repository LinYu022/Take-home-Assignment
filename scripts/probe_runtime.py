"""Isolated, bounded PNG playback experiment. Not mounted by python -m app.

The experiment reuses production composition. Its API is deliberately local
and temporary: it measures feasibility before a production player is built.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from contextlib import asynccontextmanager
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter

import av
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.config import Settings
from app.data.adapter import DatasetAdapter
from app.data.errors import DataContractError, QueryLimitError, redact_message
from app.data.source import LocalObjectStore
from app.main import _origin_allowed
from app.renderer import _series_values, compose_view, encode_png
from app.viewstate import normalize_view, validation_context


MIB = 1024 * 1024
PNG_BUDGET = 64 * MIB
PIXEL_BUDGET = 64 * MIB
MAX_WAITING = 4


from app.video import IndexedDecoder, StaleGeneration


class ByteCache:
    def __init__(self, budget=PNG_BUDGET):
        self.budget = budget
        self.bytes = self.peak = self.hits = 0
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            value = self.items.get(key)
            if value is not None:
                self.items.move_to_end(key)
                self.hits += 1
            return value

    def put(self, key, value):
        size = len(value[0])
        if size > self.budget:
            return
        with self.lock:
            if key in self.items:
                self.bytes -= len(self.items.pop(key)[0])
            while self.items and self.bytes + size > self.budget:
                self.bytes -= len(self.items.popitem(last=False)[1][0])
            self.items[key] = value
            self.bytes += size
            self.peak = max(self.peak, self.bytes)




@dataclass
class Job:
    frame: int
    generation: int
    request_id: str
    future: asyncio.Future
    queued_at: float


class ProbeRuntime:
    def __init__(self, root: Path, settings: Settings, *, cache_series=False):
        self.settings = settings
        self.adapter = DatasetAdapter(LocalObjectStore(root), settings.cache_dir)
        self.context = validation_context(self.adapter, settings)
        self.base = normalize_view({}, **self.context)
        self.path = root / self.adapter.video_key(0)
        self.source_version = (self.path.stat().st_size, self.path.stat().st_mtime_ns)
        self.decoder = IndexedDecoder(self.path, self.adapter.episodes[0].length)
        self.timestamps = self.adapter.table(0)["timestamp_ns"].to_pylist()
        self.series_cache = None
        if cache_series:
            # Cache only the validated selected reductions, not whole vectors or
            # unselected fields. Numeric cache is independently capped at 2 MiB.
            self.series_cache = {}
            for field in self.base.series:
                values = _series_values(self.adapter.table(0), field, 0, len(self.timestamps))
                if sum(v.nbytes for v in self.series_cache.values()) + values.nbytes > 2 * MIB:
                    raise QueryLimitError("selected series cache exceeds 2 MiB")
                values.setflags(write=False)
                self.series_cache[field] = values
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="png-probe")
        self.cache = ByteCache()
        self.generation = 0
        self.level = 1
        self.concurrency = 2
        self.waiting = deque()
        self.active = self.peak_active = self.peak_waiting = 0
        self.pixel_bytes = self.pixel_peak = 0
        self.pixel_lock = threading.Lock()
        self.requests = self.discarded = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.closing = False

    def invalidate(self):
        self.generation += 1
        # Remove obsolete work immediately, rather than letting a stale queue drain.
        while self.waiting:
            job = self.waiting.popleft()
            if not job.future.done():
                job.future.set_exception(StaleGeneration("generation invalidated"))
            self.discarded += 1
        return self.generation

    async def configure(self, concurrency, level):
        if concurrency not in (1, 2) or level not in (1, 3, 6):
            raise ValueError("unsupported bounded experiment configuration")
        self.invalidate()
        await self.idle.wait()
        self.concurrency, self.level = concurrency, level
        self.cache = ByteCache()
        self.peak_active = self.peak_waiting = self.pixel_peak = 0
        self.requests = self.discarded = 0
        return self.generation

    def stats(self):
        return {"active": self.active, "waiting": len(self.waiting), "peak_active": self.peak_active,
                "peak_waiting": self.peak_waiting, "png_bytes": self.cache.bytes,
                "png_peak": self.cache.peak, "png_hits": self.cache.hits,
                "pixel_bytes": self.pixel_bytes, "pixel_peak": self.pixel_peak,
                "series_cache_bytes": sum(v.nbytes for v in (self.series_cache or {}).values()),
                "requests": self.requests, "discarded": self.discarded}

    async def frame(self, frame, generation, request_id):
        if generation != self.generation or self.closing:
            raise StaleGeneration("stale request")
        if not 0 <= frame < len(self.timestamps):
            raise ValueError("frame is out of range")
        if self.active >= self.concurrency and len(self.waiting) >= MAX_WAITING:
            raise QueryLimitError("render waiting queue is full")
        job = Job(frame, generation, request_id, asyncio.get_running_loop().create_future(), perf_counter())
        self.requests += 1
        if self.active < self.concurrency:
            self._start(job)
        else:
            self.waiting.append(job)
            self.peak_waiting = max(self.peak_waiting, len(self.waiting))
        try:
            return await job.future
        except asyncio.CancelledError:
            if job in self.waiting:
                self.waiting.remove(job)
                self.discarded += 1
            raise

    def _start(self, job):
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        self.idle.clear()
        asyncio.create_task(self._run(job))

    async def _run(self, job):
        queue_ms = (perf_counter() - job.queued_at) * 1000
        try:
            result = await asyncio.get_running_loop().run_in_executor(self.executor, self._render, job)
            if job.generation != self.generation:
                raise StaleGeneration("completed request is stale")
            if not job.future.done():
                png, manifest, timing = result
                job.future.set_result((png, manifest, {**timing, "queue_ms": queue_ms}))
        except Exception as exc:
            if not job.future.done():
                job.future.set_exception(exc)
        finally:
            self.active -= 1
            while self.waiting and self.active < self.concurrency:
                next_job = self.waiting.popleft()
                if next_job.future.done() or next_job.generation != self.generation:
                    if not next_job.future.done():
                        next_job.future.set_exception(StaleGeneration("queued request is stale"))
                    self.discarded += 1
                    continue
                self._start(next_job)
            if self.active == 0:
                self.idle.set()

    def _render(self, job):
        if job.generation != self.generation:
            raise StaleGeneration("obsolete before decode")
        state = replace(self.base, cursor={"frame": job.frame}, time_range=None, evidence=None)
        state_json = json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":"))
        key = (self.source_version, state_json, self.level, "probe-info-bar-v1")
        cached = self.cache.get(key)
        if cached:
            return cached[0], cached[1], {"decode_ms": 0., "compose_ms": 0., "encode_ms": 0., "cache_hit": True}
        # Reserve space for the source and the RGB/RGBA composition temporaries.
        # Pillow's RGB storage can use four bytes/pixel. Reserve two source
        # surfaces and six full-size RGBA surfaces before allocating any of them.
        # This is application-owned pixel accounting, not a claim about native
        # codec/GPU allocator RSS (reported separately by the runner).
        reservation = self.decoder.width * self.decoder.height * 8 + 1024 * 768 * 24
        with self.pixel_lock:
            if self.pixel_bytes + reservation > PIXEL_BUDGET:
                raise QueryLimitError("decoded pixel budget exceeded")
            self.pixel_bytes += reservation
            self.pixel_peak = max(self.pixel_peak, self.pixel_bytes)
        try:
            start = perf_counter()
            image, pts = self.decoder.get(job.frame, lambda: job.generation == self.generation)
            decode_ms = (perf_counter() - start) * 1000
            if job.generation != self.generation:
                raise StaleGeneration("obsolete after decode")
            start = perf_counter()
            canvas, manifest = compose_view(self.adapter, state, image, info_bar=True, series_cache=self.series_cache)
            compose_ms = (perf_counter() - start) * 1000
            start = perf_counter()
            png = encode_png(canvas, compress_level=self.level)
            encode_ms = (perf_counter() - start) * 1000
            if len(png) > self.settings.max_image_bytes:
                raise QueryLimitError("PNG exceeds max_image_bytes")
            if job.generation != self.generation:
                raise StaleGeneration("obsolete before publish")
            manifest = {**manifest, "view": state.to_dict(), "pts": pts,
                        "pts_ns": int(round(pts * self.decoder.time_base * 1_000_000_000)),
                        "sha256": hashlib.sha256(png).hexdigest(), "probe_info_bar": True}
            self.cache.put(key, (png, manifest))
            return png, manifest, {"decode_ms": decode_ms, "compose_ms": compose_ms, "encode_ms": encode_ms, "cache_hit": False}
        finally:
            with self.pixel_lock:
                self.pixel_bytes -= reservation

    async def close(self):
        self.closing = True
        self.invalidate()
        await self.idle.wait()
        self.decoder.close()
        self.executor.shutdown(wait=True)


def create_probe_app(runtime: ProbeRuntime):
    @asynccontextmanager
    async def lifespan(_app):
        yield
        await runtime.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if not _origin_allowed(request.headers.get("origin"), runtime.settings):
            return JSONResponse({"error": "OriginNotAllowed"}, status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"error": "invalid Content-Length"}, status_code=400)
        if content_length > runtime.settings.max_query_bytes:
            return JSONResponse({"error": "request too large"}, status_code=413)
        return await call_next(request)

    @app.get("/")
    def page():
        return HTMLResponse('<!doctype html><meta charset="utf-8"><title>PNG feasibility probe</title>'
                            '<style>canvas{position:absolute;left:0;top:30px;width:1024px;height:768px}</style>'
                            '<span id="status">Probe only; not the production player</span><canvas id="surface" width="1024" height="768"></canvas>'
                            '<script src="/client.js"></script>')

    @app.get("/client.js")
    def client():
        return Response(Path(__file__).with_name("probe_client.js").read_text(encoding="utf-8"), media_type="text/javascript")

    @app.post("/prepare")
    async def prepare(request: Request):
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > runtime.settings.max_query_bytes:
                    return JSONResponse({"error": "request too large"}, status_code=413)
                raw.extend(chunk)
            body = json.loads(raw)
            if set(body) - {"concurrency", "level"}:
                raise ValueError("unknown configuration")
            generation = await runtime.configure(body["concurrency"], body["level"])
            return {"generation": generation, "view": runtime.base.to_dict(), "length": len(runtime.timestamps),
                    "stats": runtime.stats()}
        except Exception as exc:
            return JSONResponse({"error": redact_message(str(exc))}, status_code=400)

    @app.get("/timeline")
    def timeline(start: int = 0, limit: int = 512):
        if not 0 <= start < len(runtime.timestamps) or not 1 <= limit <= 512:
            return JSONResponse({"error": "timeline limit"}, status_code=400)
        end = min(start + limit, len(runtime.timestamps))
        if (end - start) * 3 > runtime.settings.max_query_scalars:
            return JSONResponse({"error": "timeline scalar limit"}, status_code=413)
        result = {"rows": [{"frame": i, "pts_ns": int(round(int(runtime.decoder.pts[i]) * runtime.decoder.time_base * 1e9)),
                            "timestamp_ns": runtime.timestamps[i]} for i in range(start, end)],
                  "next_cursor": end if end < len(runtime.timestamps) else None}
        if len(json.dumps(result).encode()) > runtime.settings.max_query_bytes:
            return JSONResponse({"error": "timeline byte limit"}, status_code=413)
        return result

    @app.post("/generation")
    async def generation():
        return {"generation": runtime.invalidate(), "stats": runtime.stats()}

    @app.get("/frame")
    async def frame(frame: int, generation: int, request_id: str):
        try:
            if len(request_id) > 64:
                raise ValueError("request_id too long")
            png, manifest, timing = await runtime.frame(frame, generation, request_id)
            return Response(png, media_type="image/png", headers={
                "Cache-Control": "no-store", "X-Request-ID": request_id,
                "X-Render-Manifest": json.dumps(manifest, separators=(",", ":")),
                "X-Probe-Timing": json.dumps(timing, separators=(",", ":")),
                "X-Probe-Resources": json.dumps(runtime.stats(), separators=(",", ":")),
            })
        except Exception as exc:
            status = 409 if isinstance(exc, StaleGeneration) else 429 if isinstance(exc, QueryLimitError) else 400
            return JSONResponse({"error": redact_message(str(exc))}, status_code=status)

    return app
