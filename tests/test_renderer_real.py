import os
from pathlib import Path

import pytest

from app.config import Settings
from app.data.adapter import DatasetAdapter
from app.data.source import LocalObjectStore
from app.renderer import render_view
from app.viewstate import normalize_view


ROOT = Path(os.environ["LEROBOT_TEST_DATA_ROOT"]) if os.getenv("LEROBOT_TEST_DATA_ROOT") else None


@pytest.mark.skipif(ROOT is None, reason="real snapshot requires explicit LEROBOT_TEST_DATA_ROOT")
def test_real_renderer_honors_time_range_and_is_deterministic():
    settings = Settings(data_root=ROOT, s3_bucket=None, s3_endpoint=None, s3_region="auto",
                        public_base_url="http://127.0.0.1:8000", cache_dir=Path(os.getenv("TEMP", ".")) / "maxinsights-review-cache-test")
    adapter = DatasetAdapter(LocalObjectStore(ROOT), settings.cache_dir)
    timestamps = adapter.table(0)["timestamp_ns"].to_pylist()
    episode = adapter.episodes[0]
    state = normalize_view({"episode": 0, "cursor": None, "time_range": {"start_ns": int(timestamps[1514]), "end_ns": int(timestamps[1544])}}, **__import__("app.viewstate", fromlist=["validation_context"]).validation_context(adapter, settings))
    first = render_view(adapter, state)
    second = render_view(adapter, state)
    assert first.png == second.png
    assert first.manifest["range_start_frame"] == 1514
    assert first.manifest["range_end_frame"] == 1544
    assert first.manifest["cursor_frame"] == 1514

