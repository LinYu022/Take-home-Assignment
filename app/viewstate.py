from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urlparse

import re

class ViewStateError(ValueError):
    pass


ALLOWED_PANELS = {"video", "series", "metadata"}
ALLOWED_OVERLAYS = {"hand_tracks"}
DEFAULT_PANELS = ["video", "series", "metadata"]
DEFAULT_SERIES = ["base_0_camera/position", "action.left_hand_tracks"]
DEFAULT_OVERLAYS = [{"type": "hand_tracks", "sides": ["left", "right"]}]
MAX_SERIES = 8
MAX_OVERLAYS = 8
MAX_EVIDENCE_KEYS = 12


@dataclass(frozen=True)
class ViewState:
    schema_version: int
    episode: int
    cursor: dict[str, int] | None
    time_range: dict[str, int] | None
    panels: tuple[str, ...]
    series: tuple[str, ...]
    overlays: tuple[dict[str, Any], ...]
    render: dict[str, int]
    evidence: dict[str, Any] | None = None
    display: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "episode": self.episode,
            "cursor": self.cursor,
            "time_range": self.time_range,
            "panels": list(self.panels),
            "series": list(self.series),
            "overlays": [dict(x) for x in self.overlays],
            "render": dict(self.render),
            "evidence": dict(self.evidence) if self.evidence is not None else None,
            "display": dict(self.display or {"locale": "en", "info_bar": True}),
        }


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_token(token: str) -> dict[str, Any]:
    try:
        padded = token + "=" * (-len(token) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception as exc:
        raise ViewStateError("state is not valid base64url JSON") from exc


def _range(value: Any, name: str) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ViewStateError(f"{name} must contain start and end")
    start, end = int(value["start"]), int(value["end"])
    if start < 0 or end <= start:
        raise ViewStateError(f"{name} must be a non-empty half-open range")
    return {"start": start, "end": end}


def _evidence(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > MAX_EVIDENCE_KEYS:
        raise ViewStateError("evidence must be a small object")
    allowed = {"kind", "field", "component", "requested_frame_range", "requested_time_range", "returned_frame_range", "returned_time_range", "page_offset", "page_limit", "aggregation", "input_count", "exact"}
    unknown = set(value) - allowed
    if unknown:
        raise ViewStateError(f"evidence has unknown keys: {sorted(unknown)}")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key.endswith("_range"):
            result[key] = _range(item, key)
        elif key == "component" and item is None:
            result[key] = None
        elif key in {"component", "page_offset", "page_limit", "input_count"}:
            if type(item) is not int:
                raise ViewStateError(f"{key} must be an integer")
            result[key] = item
            if result[key] < 0:
                raise ViewStateError(f"{key} must be non-negative")
        elif key == "exact":
            result[key] = bool(item)
        else:
            result[key] = str(item)
    return result


def normalize_view(raw: dict[str, Any], *, episode_count: int, episode_lengths: dict[int, int], fields: Iterable[str], episode_time_ranges: dict[int, tuple[int, int]] | None = None, field_lengths=None, max_width: int = 1600, max_height: int = 1200) -> ViewState:
    if not isinstance(raw, dict):
        raise ViewStateError("view state must be an object")
    allowed = {"schema_version", "episode", "cursor", "time_range", "panels", "series", "overlays", "render", "evidence", "display"}
    if set(raw) - allowed:
        raise ViewStateError("view has unknown keys")
    schema = integer(raw.get("schema_version", 1), "schema_version")
    if schema != 1:
        raise ViewStateError("unsupported schema_version")
    episode = integer(raw.get("episode", 0), "episode")
    if episode not in episode_lengths:
        raise ViewStateError("episode is out of range")
    cursor = raw.get("cursor")
    time_range = raw.get("time_range")
    if cursor is not None and time_range is not None:
        raise ViewStateError("cursor and time_range are mutually exclusive")
    if cursor is None and time_range is None:
        cursor = {"frame": 0}
    if cursor is not None:
        if not isinstance(cursor, dict) or set(cursor) != {"frame"}:
            raise ViewStateError("cursor must contain only frame")
        frame = integer(cursor["frame"], "frame")
        if frame < 0 or frame >= episode_lengths[episode]:
            raise ViewStateError("frame is out of range")
        cursor = {"frame": frame}
    if time_range is not None:
        if not isinstance(time_range, dict) or set(time_range) != {"start_ns", "end_ns"}:
            raise ViewStateError("time_range must contain start_ns and end_ns")
        start, end = integer(time_range["start_ns"], "start_ns"), integer(time_range["end_ns"], "end_ns")
        if start < 0 or end <= start:
            raise ViewStateError("time_range must be a non-empty half-open interval")
        domain = (episode_time_ranges or {}).get(episode)
        if domain is None:
            raise ViewStateError("episode time domain is required for time_range")
        if domain is not None and (start < domain[0] or end > domain[1]):
            raise ViewStateError(f"time_range must be within [{domain[0]},{domain[1]})")
        time_range = {"start_ns": start, "end_ns": end}
    panels_raw = raw.get("panels", DEFAULT_PANELS)
    if not isinstance(panels_raw, list) or len(panels_raw) > 3:
        raise ViewStateError("panels are limited to three entries")
    panels = tuple(x for x in DEFAULT_PANELS if x in panels_raw)
    if any(x not in ALLOWED_PANELS for x in panels_raw):
        raise ViewStateError("panels contain an unknown value")
    known = set(fields)
    series_raw = raw.get("series", [x for x in DEFAULT_SERIES if x in known])
    if not isinstance(series_raw, list) or len(series_raw) > MAX_SERIES:
        raise ViewStateError(f"series is limited to {MAX_SERIES} fields")
    for selector in series_raw:
        series_selector(selector, known, field_lengths)
    series = tuple(sorted(set(series_raw)))
    overlays_raw = raw.get("overlays", DEFAULT_OVERLAYS)
    if not isinstance(overlays_raw, list):
        raise ViewStateError("overlays must be a list")
    if len(overlays_raw) > MAX_OVERLAYS:
        raise ViewStateError(f"overlays are limited to {MAX_OVERLAYS} entries")
    overlays: list[dict[str, Any]] = []
    for item in overlays_raw:
        if not isinstance(item, dict) or set(item) - {"type", "sides"} or item.get("type") not in ALLOWED_OVERLAYS:
            raise ViewStateError("unknown overlay")
        sides_raw = item.get("sides", ["left", "right"])
        if not isinstance(sides_raw, list) or len(sides_raw) > 2:
            raise ViewStateError("overlay sides are limited to two entries")
        sides = sorted(set(sides_raw))
        if any(x not in {"left", "right"} for x in sides):
            raise ViewStateError("overlay sides must be left or right")
        overlays.append({"type": item["type"], "sides": sides})
    render = raw.get("render", {})
    if not isinstance(render, dict) or set(render) - {"width", "height"}:
        raise ViewStateError("invalid render settings")
    width, height = integer(render.get("width", 1024), "width"), integer(render.get("height", 768), "height")
    if width < 320 or height < 240 or width > max_width or height > max_height:
        raise ViewStateError(f"render size must be between 320x240 and {max_width}x{max_height}")
    evidence = _evidence(raw.get("evidence"))
    if evidence is not None:
        if evidence.get("kind") not in {"series_query", "aggregate_series"}:
            raise ViewStateError("unknown evidence kind")
        field, component = evidence.get("field"), evidence.get("component")
        selector = f"{field}[{component}]" if component is not None else field
        series_selector(selector, known, field_lengths)
        if selector not in series:
            raise ViewStateError("evidence field/component does not match series")
        requested, returned = evidence.get("requested_frame_range"), evidence.get("returned_frame_range")
        if not requested or not returned or not 0 <= requested["start"] <= returned["start"] <= returned["end"] <= requested["end"] <= episode_lengths[episode]:
            raise ViewStateError("evidence ranges are outside episode or requested range")
        if evidence["kind"] == "aggregate_series" and requested != returned:
            raise ViewStateError("aggregate evidence requested and returned ranges must match")
        if evidence.get("page_offset", 0) != returned["start"] - requested["start"]:
            raise ViewStateError("evidence page offset does not match range")
        for key in ("requested_time_range", "returned_time_range"):
            interval = evidence.get(key)
            domain = (episode_time_ranges or {}).get(episode)
            if interval and (domain is None or interval["start"] < domain[0] or interval["end"] > domain[1]):
                raise ViewStateError("evidence time range is outside episode")
    merged = sorted({side for o in overlays for side in o["sides"]})
    overlays = [{"type": "hand_tracks", "sides": merged}] if merged else []
    display = raw.get("display", {})
    if not isinstance(display, dict) or set(display) - {"locale", "info_bar"}:
        raise ViewStateError("invalid display settings")
    locale, info_bar = display.get("locale", "en"), display.get("info_bar", True)
    if locale not in {"en", "zh"} or type(info_bar) is not bool:
        raise ViewStateError("display requires locale en/zh and boolean info_bar")
    return ViewState(schema, episode, cursor, time_range, panels, series, tuple(overlays), {"width": width, "height": height}, evidence, {"locale": locale, "info_bar": info_bar})


def encode_view(state: ViewState, base_url: str, max_url_chars: int = 16_384) -> str:
    token = base64.urlsafe_b64encode(_canonical_json(state.to_dict()).encode("utf-8")).decode("ascii").rstrip("=")
    url = f"{base_url.rstrip('/')}/view?{urlencode({'state': token})}"
    if len(url) > max_url_chars:
        raise ViewStateError(f"view URL exceeds {max_url_chars} characters")
    return url


def decode_view(url_or_token: str, max_url_chars: int = 16_384, **validation) -> ViewState:
    if not isinstance(url_or_token, str) or len(url_or_token) > max_url_chars:
        raise ViewStateError(f"view URL exceeds {max_url_chars} characters")
    token = url_or_token
    if "://" in url_or_token:
        query = parse_qs(urlparse(url_or_token).query)
        values = query.get("state")
        if not values:
            raise ViewStateError("view URL has no state query parameter")
        token = values[0]
    return normalize_view(_decode_token(token), **validation)


def resolve_frame(state: ViewState, adapter) -> int:
    if state.cursor is not None:
        return state.cursor["frame"]
    return adapter.frame_for_timestamp(state.episode, state.time_range["start_ns"], strict=True)


def integer(value, name):
    if type(value) is not int:
        raise ViewStateError(f"{name} must be an integer")
    return value


def series_selector(selector, fields, field_lengths=None):
    if not isinstance(selector, str) or len(selector) > 200:
        raise ViewStateError("series selector must be a bounded string")
    match = re.fullmatch(r"([^\[\]]+)(?:\[(\d+)\])?", selector)
    if not match or match[1] not in fields:
        raise ViewStateError("unknown field or malformed component selector")
    field, component = match[1], int(match[2]) if match[2] else None
    if field_lengths is not None and component is not None:
        length = field_lengths.get(field)
        if length is None or not 0 <= component < length:
            raise ViewStateError("component is out of range")
    return field, component


def validation_context(adapter, settings):
    fields = adapter.fields()
    lengths = {}
    for field in fields:
        observed = getattr(field, "observed_length", None)
        if observed is None:
            sample = adapter.table(adapter.episodes[0].episode_index)[field.name].slice(0, 1).to_pylist()
            observed = len(sample[0]) if sample and isinstance(sample[0], list) else None
        lengths[field.name] = observed
    return {"episode_count": len(adapter.episodes),
            "episode_lengths": {e.episode_index: e.length for e in adapter.episodes},
            "episode_time_ranges": {e.episode_index: (e.data_start_ns, e.data_end_ns) for e in adapter.episodes},
            "fields": [f.name for f in fields], "field_lengths": lengths,
            "max_width": settings.max_render_width, "max_height": settings.max_render_height}

