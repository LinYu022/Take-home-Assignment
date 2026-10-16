"""Exercise the probe in a real browser; synthetic data establishes no FPS gate."""
import socket
import threading
import time
from dataclasses import replace

import uvicorn

from scripts.probe_runtime import ProbeRuntime, create_probe_app
from app.renderer import compose_view, encode_png


def test_probe_requests_real_png_hits_and_bounds_snapshots(page, live_service, tmp_path):
    settings = replace(live_service.settings, cache_dir=tmp_path / "probe-cache")
    runtime = ProbeRuntime(settings.data_root, settings, cache_series=True)
    assert all(not values.flags.writeable for values in runtime.series_cache.values())
    state = replace(runtime.base, cursor={"frame": 19})
    image, _ = runtime.decoder.get(19)
    direct, direct_manifest = compose_view(runtime.adapter, state, image, info_bar=True)
    cached, cached_manifest = compose_view(runtime.adapter, state, image, info_bar=True, series_cache=runtime.series_cache)
    assert encode_png(direct) == encode_png(cached) and direct_manifest == cached_manifest
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); sock.listen(128)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_probe_app(runtime), log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True); thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.02)
        page.goto(url)
        result = page.evaluate("""async () => window.probe.seeks({concurrency:2,prefetch:6,level:1,frames:[2,2,19,19]})""")
        assert len(result["requests"]) == 4
        assert [row["cache_hit"] for row in result["requests"]] == [False, True, False, True]
        assert [row["frame"] for row in result["presentations"]] == [2, 2, 19, 19]
        assert all(row["frame"] == row["view_frame"] for row in result["presentations"])
        assert all(row["sha256"] == row["browser_png_sha256"] for row in result["results"])
        played = page.evaluate("""async () => window.probe.play({concurrency:2,prefetch:8,level:1,rate:1,seconds:1,start:0})""")
        frames = [row["frame"] for row in played["presentations"]]
        assert len(frames) > 3 and len(frames) == len(set(frames))
        assert all(row["frame"] == row["view_frame"] for row in played["presentations"])
        peak = played["browser_peak"]
        assert peak["in_flight"] <= 2 and peak["waiting"] <= 4
        assert peak["compressed_frames"] <= 8 and peak["compressed_bytes"] <= 12 * 1024**2
        assert peak["decoded_images"] <= 2 and peak["decoded_pixels"] <= 16 * 1024**2
        assert played["server_final"]["waiting"] == 0
        runtime.settings = replace(runtime.settings, max_query_scalars=2)
        assert page.request.get(url + "/timeline?start=0&limit=1").status == 413
        runtime.settings = replace(runtime.settings, max_query_bytes=16)
        assert page.request.post(url + "/prepare", data={"concurrency": 2, "level": 1}).status == 413
    finally:
        server.should_exit = True; thread.join(timeout=10); sock.close()
        assert not thread.is_alive()
        assert runtime.decoder.closed
