"""Real HTTP service and generated LeRobot fixtures; no real snapshot is required."""
import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import av
import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import uvicorn

from app.config import Settings
from app.main import create_app


if importlib.util.find_spec("pytest_playwright") is None:
    raise pytest.UsageError("Browser tests require: python -m pip install -r requirements-browser.txt; python -m playwright install chromium")


def _write_dataset(root, frame_count=40, video_shift=0, position_offset=0.):
    (root / "meta").mkdir(parents=True)
    info = {"codebase_version": "v3.0", "fps": 10, "robot_type": "synthetic", "total_videos": 2,
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
            "features": {"base_0_camera/intrinsics": [[48, 0, 32], [0, 48, 24], [0, 0, 1]],
                         "base_0_camera/resolution": [64, 48]}}
    (root / "meta/info.json").write_text(json.dumps(info), encoding="utf-8")
    episodes = []
    for episode in range(2):
        rows = []
        for frame in range(frame_count):
            ts = frame * 100_000_000
            tracks = [(point - 10) * .01 + frame * .001 for point in range(21)]
            points = [value for x in tracks for value in (x, .1, 1.)]
            rows.append({"timestamp": ts / 1e9, "timestamp_ns": ts, "frame_index": frame,
                         "episode_index": episode, "index": episode * frame_count + frame, "task_index": 0,
                         "base_0_camera/position": [ts, position_offset + frame * .001, 0., 0.],
                         "base_0_camera/quaternion_xyzw": [ts, 0., 0., 0., 1.],
                         "left_hand/tracks": [ts, *points], "right_hand/tracks": [ts, *points],
                         "action.left_hand_tracks": points, "action.right_hand_tracks": points})
        data_path = root / f"data/chunk-000/file-{episode:03d}.parquet"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows), data_path)
        video_path = root / f"videos/base_0_camera/rgb/image/chunk-000/file-{episode:03d}.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        with av.open(str(video_path), "w") as container:
            stream = container.add_stream("libx264", rate=10)
            stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
            for frame in range(frame_count):
                pixels = np.zeros((48, 64, 3), dtype=np.uint8)
                pixels[:] = ((30 + frame * 3) % 256, (65 + episode * 70 + video_shift) % 256, 100)
                for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        episodes.append({"episode_index": episode, "length": frame_count, "tasks": ["synthetic regression"],
                         "data/chunk_index": 0, "data/file_index": episode,
                         "videos/base_0_camera/rgb/image/chunk_index": 0,
                         "videos/base_0_camera/rgb/image/file_index": episode,
                         "videos/base_0_camera/rgb/image/from_timestamp": 0.,
                         "videos/base_0_camera/rgb/image/to_timestamp": frame_count / 10})
    pq.write_table(pa.Table.from_pylist(episodes), root / "meta/episodes.parquet")
    pq.write_table(pa.table({"task_index": [0], "task": ["synthetic regression"]}), root / "meta/tasks.parquet")


class _OriginPage(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>MCP browser client fixture</title>")

    def log_message(self, *_):
        pass


@contextmanager
def _run_service(root, cache_dir, allowed_origins=()):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = Settings(data_root=root, s3_bucket=None, s3_endpoint=None, s3_region="auto",
                        public_base_url=url, cache_dir=cache_dir, allowed_origins=allowed_origins)
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                pytest.fail("FastAPI fixture did not start")
            time.sleep(.05)
        assert httpx.get(url + "/health", trust_env=False).json()["ok"]
        yield SimpleNamespace(url=url, settings=settings, app=app)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "FastAPI fixture failed to shut down"


@pytest.fixture(scope="session")
def live_service():
    with tempfile.TemporaryDirectory(prefix="lerobot-browser-") as directory:
        root = Path(directory).resolve()
        # The only recursive cleanup target is this temporary fixture directory.
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        _write_dataset(root)
        origin_server = ThreadingHTTPServer(("127.0.0.2", 0), _OriginPage)
        origin = f"http://127.0.0.2:{origin_server.server_port}"
        origin_thread = threading.Thread(target=origin_server.serve_forever, daemon=True)
        origin_thread.start()
        try:
            with _run_service(root, root / "cache", (origin, "https://review.example")) as service:
                service.origin = origin
                service.anchor = 2
                service.length = 40
                yield service
        finally:
            origin_server.shutdown()
            origin_server.server_close()
            origin_thread.join(timeout=5)


@pytest.fixture(scope="session")
def paged_service():
    with tempfile.TemporaryDirectory(prefix="lerobot-browser-paged-") as directory:
        root = Path(directory).resolve()
        _write_dataset(root, frame_count=540)
        with _run_service(root, root / "cache") as service:
            yield service


@pytest.fixture(params=["synthetic", "real"])
def timeline_service(request):
    return request.getfixturevalue("paged_service" if request.param == "synthetic" else "real_service")


@pytest.fixture(scope="session")
def real_service():
    configured = os.getenv("LEROBOT_TEST_DATA_ROOT")
    if not configured:
        pytest.skip("real browser regression requires explicit LEROBOT_TEST_DATA_ROOT")
    root = Path(configured).resolve()
    assert (root / "meta/info.json").is_file(), "LEROBOT_TEST_DATA_ROOT has no meta/info.json"
    with tempfile.TemporaryDirectory(prefix="lerobot-browser-real-cache-") as directory:
        cache_dir = Path(directory).resolve()
        assert cache_dir.parent == Path(tempfile.gettempdir()).resolve()
        with _run_service(root, cache_dir) as service:
            service.anchor = 1514
            service.length = 3029
            yield service


@pytest.fixture(params=["synthetic", "real"])
def evidence_service(request):
    return request.getfixturevalue("live_service" if request.param == "synthetic" else "real_service")


@pytest.fixture(scope="session")
def browser(browser_type, browser_type_launch_args):
    try:
        instance = browser_type.launch(**browser_type_launch_args)
    except Exception as exc:
        pytest.fail("Chromium could not launch. Install with: python -m playwright install chromium\n" + str(exc))
    yield instance
    instance.close()


@pytest.fixture
def visualizer(page, live_service):
    page.set_default_timeout(10000)
    page.goto(live_service.url + "/view")
    page.locator("#render").wait_for(state="visible")
    return page
