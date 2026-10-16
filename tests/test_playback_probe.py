"""Correctness/resource checks for the isolated probe, not a playback acceptance."""
import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import deque

import av
import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.data.errors import QueryLimitError
from scripts.probe_runtime import ByteCache, IndexedDecoder, ProbeRuntime, StaleGeneration


def test_probe_compressed_lru_counts_bytes_and_evicts():
    cache = ByteCache(budget=10)
    cache.put("a", (b"123456", {}))
    cache.put("b", (b"1234", {}))
    assert cache.get("a")[0] == b"123456"
    cache.put("c", (b"789", {}))
    assert cache.get("b") is None
    assert cache.bytes == 9 and cache.peak == 10
    cache.put("a", (b"x", {}))
    cache.put("too-large", (b"x" * 11, {}))
    assert cache.bytes == 4 and cache.get("too-large") is None


def test_probe_seek_identity_with_independent_visible_binary_frame_marker(tmp_path):
    path = tmp_path / "markers.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        stream.options = {"crf": "0", "g": "8"}
        for frame in range(40):
            image = Image.new("RGB", (64, 48), (40, 60, 100))
            draw = ImageDraw.Draw(image)
            for bit in range(6):
                draw.rectangle((bit * 8, 0, bit * 8 + 7, 15), fill="white" if frame & (1 << bit) else "black")
            draw.text((4, 25), str(frame), fill="white")
            for packet in stream.encode(av.VideoFrame.from_image(image)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    reference = []
    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            reference.append((int(frame.pts), hashlib.sha256(frame.to_ndarray(format="rgb24").tobytes()).hexdigest()))
    decoder = IndexedDecoder(path, 40)
    try:
        for index in (0, 39, 17, 8, 7, 9, 23, 24, 25, 1, 2, 3, 38, 39):
            image, pts = decoder.get(index)
            pixels = np.asarray(image)
            visible_marker = sum((1 << bit) for bit in range(6) if pixels[8, bit * 8 + 4].mean() > 128)
            assert visible_marker == index
            assert (pts, hashlib.sha256(image.tobytes()).hexdigest()) == reference[index]
        with pytest.raises(ValueError, match="out of range"):
            decoder.get(40)
    finally:
        decoder.close()


def test_probe_generation_removes_waiting_jobs_and_rejects_running_publication():
    async def scenario():
        runtime = ProbeRuntime.__new__(ProbeRuntime)
        runtime.generation, runtime.concurrency = 0, 2
        runtime.active = runtime.peak_active = runtime.peak_waiting = runtime.requests = runtime.discarded = 0
        runtime.waiting, runtime.timestamps = deque(), list(range(40))
        runtime.closing = False
        runtime.idle = asyncio.Event(); runtime.idle.set()
        runtime.executor = ThreadPoolExecutor(max_workers=2)
        release = threading.Event()
        def blocking_render(job):
            assert release.wait(timeout=5)
            return b"png", {"frame": job.frame}, {}
        runtime._render = blocking_render
        jobs = [asyncio.create_task(runtime.frame(i, 0, f"r{i}")) for i in range(6)]
        await asyncio.sleep(.02)
        try:
            assert runtime.active == 2 and len(runtime.waiting) == 4
            with pytest.raises(QueryLimitError, match="queue is full"):
                await runtime.frame(6, 0, "overflow")
            assert runtime.invalidate() == 1
            assert len(runtime.waiting) == 0
            release.set()
            results = await asyncio.gather(*jobs, return_exceptions=True)
            assert all(isinstance(result, StaleGeneration) for result in results)
            await runtime.idle.wait()
            assert runtime.active == 0 and runtime.peak_active == 2 and runtime.peak_waiting == 4
        finally:
            release.set(); runtime.executor.shutdown(wait=True)
    # Playwright's session-scoped sync browser owns an event loop on pytest's
    # main thread. Keep this independent queue test on its own loop/thread.
    with ThreadPoolExecutor(max_workers=1) as driver:
        driver.submit(lambda: asyncio.run(scenario())).result(timeout=10)
