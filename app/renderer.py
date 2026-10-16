from __future__ import annotations

import io
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .viewstate import ViewState, resolve_frame


@dataclass(frozen=True)
class RenderResult:
    png: bytes
    manifest: dict[str, Any]


@lru_cache(maxsize=8)
def _font(size: int):
    return ImageFont.truetype(str(Path(__file__).with_name("assets") / "NotoSansSC-VF.ttf"), size)


def _quat_matrix(q):
    x, y, z, w = [float(v) for v in q]
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype=np.float64)


def _video_frame(path, index: int) -> Image.Image:
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        if index < 0:
            raise ValueError("frame must be non-negative")
        for i, frame in enumerate(container.decode(video=0)):
            if i == index:
                return frame.to_image().convert("RGB")
        raise ValueError("video frame is out of range")


def _series_values(table, field: str, start: int, end: int) -> np.ndarray:
    component = None
    if field.endswith("]") and "[" in field:
        field, raw = field.rsplit("[", 1)
        component = int(raw[:-1])
    values = table[field].slice(start, end - start).to_pylist()
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim == 1:
        return arr
    if component is not None:
        return arr[:, component]
    # These vectors store ts_ns at index 0; a whole-field curve reduces values only.
    # Explicit component selectors retain the raw dataset indexing contract.
    if field in {"base_0_camera/position", "base_0_camera/quaternion_xyzw", "left_hand/tracks", "right_hand/tracks"}:
        arr = arr[:, 1:]
    return arr.mean(axis=1)


def _curve_range(table, state: ViewState, frame_index: int) -> tuple[int, int, str, bool]:
    """Resolve one half-open curve window shared by every selected series."""
    if state.evidence is not None:
        if state.evidence["kind"] == "series_query":
            interval = state.evidence["returned_frame_range"]
            source = "query_page"
        else:
            interval = state.evidence["requested_frame_range"]
            source = "aggregate_input"
        return interval["start"], interval["end"], source, False
    if state.time_range is not None:
        timestamps = np.asarray(table["timestamp_ns"].to_pylist(), dtype=np.int64)
        requested_start, requested_end = map(int, np.searchsorted(
            timestamps, [state.time_range["start_ns"], state.time_range["end_ns"]], side="left"))
        start = max(0, min(requested_start, table.num_rows - 1))
        end = max(start + 1, min(requested_end, table.num_rows))
        if end - start < 2 and end < table.num_rows:
            end = start + 2
        return start, end, "time_range", (start, end) != (requested_start, requested_end)
    return max(0, frame_index - 240), min(table.num_rows, frame_index + 241), "cursor_window", False


def compose_view(adapter, state: ViewState, video: Image.Image, *, info_bar=None, series_cache=None):
    from .compositor import compose
    return compose(adapter, state, video, info_bar=info_bar, series_cache=series_cache)


def encode_png(canvas: Image.Image, *, compress_level: int = 1) -> bytes:
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=False, compress_level=compress_level)
    return output.getvalue()


def render_view(adapter, state: ViewState) -> RenderResult:
    frame_index = resolve_frame(state, adapter)
    video = _video_frame(adapter.materialize(adapter.video_key(state.episode)), frame_index)
    canvas, manifest = compose_view(adapter, state, video)
    return RenderResult(encode_png(canvas), manifest)

