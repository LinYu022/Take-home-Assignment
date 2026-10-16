import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app


REAL_ROOT = Path(os.environ["LEROBOT_TEST_DATA_ROOT"]) if os.getenv("LEROBOT_TEST_DATA_ROOT") else None


class _FakeEpisode:
    episode_index = 0
    length = 4
    data_start_ns = 0
    data_end_ns = 400
    video_start_ns = 0
    video_end_ns = 400
    frame_period_ns = 100
    tasks = ["fixture"]

    def as_dict(self):
        return {"episode_index": 0, "length": 4, "tasks": ["fixture"], "data_start_ns": 0, "data_end_ns": 400, "video_start_ns": 0, "video_end_ns": 400, "frame_period_ns": 100}


class _FakeField:
    def __init__(self, name, observed_length=None):
        self.name, self.observed_length = name, observed_length


class _FakeAdapter:
    episodes = [_FakeEpisode()]

    def fields(self):
        return [_FakeField("value", 1), _FakeField("vector", 2)]

    def manifest(self):
        return {"total_episodes": 1, "total_frames": 4, "fields": [{"name": "value", "kind": "scalar", "observed_length": None}, {"name": "vector", "kind": "vector", "observed_length": 2}], "episodes": [self.episodes[0].as_dict()]}


def test_mcp_notifications_and_errors_share_cors_policy(monkeypatch):
    import app.main as main_module
    from fastapi.testclient import TestClient
    settings = Settings(data_root=None, s3_bucket=None, s3_endpoint=None, s3_region="auto",
                        public_base_url="http://127.0.0.1:8000", cache_dir=Path("."),
                        allowed_origins=("https://review.example",))
    monkeypatch.setattr(main_module, "_adapter", lambda _: _FakeAdapter())
    with TestClient(create_app(settings)) as client:
        for origin in (None, "http://localhost:3000", "https://review.example"):
            headers = {"Origin": origin} if origin else {}
            for method, status in (("initialize", 200), ("notifications/initialized", 202),
                                   ("notifications/cancelled", 202), ("unknown", 200)):
                response = client.post("/mcp", json={"jsonrpc": "2.0", "method": method, "params": {}}, headers=headers)
                assert response.status_code == status
                assert response.headers.get("access-control-allow-origin") == origin
                assert response.headers.get("vary") == ("Origin" if origin else None)
                if status == 202:
                    assert response.content == b""
        for method in ("initialize", "notifications/initialized", "notifications/cancelled"):
            response = client.post("/mcp", json={"jsonrpc": "2.0", "method": method}, headers={"Origin": "https://other.example"})
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers



@pytest.mark.skipif(REAL_ROOT is None, reason="real snapshot requires explicit LEROBOT_TEST_DATA_ROOT")
def test_mcp_jsonrpc_replay_and_origin_policy():
    settings = Settings(data_root=REAL_ROOT, s3_bucket=None, s3_endpoint=None, s3_region="auto",
                        public_base_url="http://127.0.0.1:8000", cache_dir=Path(os.getenv("TEMP", ".")) / "maxinsights-review-cache-test")
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(create_app(settings)) as client:
        initialize = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}, headers={"Accept": "application/json, text/event-stream"})
        assert initialize.status_code == 200
        listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers={"Accept": "application/json, text/event-stream"}).json()
        assert [x["name"] for x in listed["result"]["tools"]] == ["describe_dataset", "list_episodes", "query_series", "aggregate_series", "encode_view", "decode_view", "render_view", "compare_series", "audit_timing", "audit_integrity"]
        described = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "describe_dataset", "arguments": {}}}, headers={"Accept": "application/json, text/event-stream"}).json()
        assert described["result"]["structuredContent"]["manifest"]["total_frames"] == 3029
        view = {"episode": 0, "cursor": {"frame": 1514}, "time_range": None, "panels": ["video", "series", "metadata"], "series": ["base_0_camera/position", "action.left_hand_tracks"], "overlays": [{"type": "hand_tracks", "sides": ["left", "right"]}], "render": {"width": 640, "height": 480}, "evidence": None}
        rendered = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "render_view", "arguments": {"view": view}}}, headers={"Accept": "application/json, text/event-stream"}).json()
        assert rendered["result"]["content"][0]["type"] == "image"
        assert rendered["result"]["structuredContent"]["mime_type"] == "image/png"
        assert client.options("/mcp", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.options("/mcp", headers={"Origin": "http://localhost:3000"}).status_code == 204
    allowed = replace(settings, allowed_origins=("https://review.example",))
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(create_app(allowed)) as client:
        assert client.options("/mcp", headers={"Origin": "https://review.example"}).headers.get("access-control-allow-origin") == "https://review.example"
        assert client.options("/mcp", headers={"Origin": "https://other.example"}).status_code == 403


@pytest.mark.skipif(REAL_ROOT is None, reason="real snapshot requires explicit LEROBOT_TEST_DATA_ROOT")
def test_view_normalize_and_browser_render_limit():
    settings = Settings(data_root=REAL_ROOT, s3_bucket=None, s3_endpoint=None, s3_region="auto",
                        public_base_url="http://127.0.0.1:8000", cache_dir=Path(os.getenv("TEMP", ".")) / "maxinsights-review-cache-test")
    from dataclasses import replace
    from app.viewstate import encode_view, normalize_view, validation_context
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(create_app(settings)) as client:
        view = {"episode": 0, "cursor": {"frame": 1514}, "time_range": None, "panels": ["video", "series", "metadata"], "series": ["action.left_hand_tracks[0]"], "overlays": [], "render": {"width": 640, "height": 480}, "evidence": None}
        normalized = client.post("/api/view/normalize", json={"view": view}).json()
        assert normalized["view"]["series"] == ["action.left_hand_tracks[0]"]
        assert client.post("/api/view/normalize", json={"view": view, "url": normalized["view_url"]}).status_code == 400
        assert client.post("/api/view/normalize", json={"url": normalized["view_url"]}).json()["view"] == normalized["view"]
        tiny = replace(settings, max_image_bytes=1)
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(create_app(tiny)) as client:
        assert client.get("/render", params={"state": normalized["view_url"].split("state=", 1)[1]}).status_code == 413


def test_view_normalize_and_render_limit_without_dataset(monkeypatch):
    import app.main as main_module
    from types import SimpleNamespace
    settings = Settings(data_root=None, s3_bucket=None, s3_endpoint=None, s3_region="auto", public_base_url="http://127.0.0.1:8000", cache_dir=Path("."), max_image_bytes=3)
    monkeypatch.setattr(main_module, "_adapter", lambda _: _FakeAdapter())
    monkeypatch.setattr(main_module.RenderService, "render", lambda *_args, **_kwargs: SimpleNamespace(png=b"012345", manifest={"frame": 0}, close=lambda: None))
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(create_app(settings)) as client:
        normalized = client.post("/api/view/normalize", json={"view": {"episode": 0, "cursor": {"frame": 0}, "series": ["vector[0]"], "overlays": []}})
        assert normalized.status_code == 200
        assert normalized.json()["resolved_frame"] == 0
        assert "resolved_frame" not in normalized.json()["view"]
        assert normalized.json()["view"]["series"] == ["vector[0]"]
        assert client.post("/api/view/normalize", json={"view": {}, "url": normalized.json()["view_url"]}).status_code == 400
        assert client.get("/render", params={"state": normalized.json()["view_url"].split("state=", 1)[1]}).status_code == 413

