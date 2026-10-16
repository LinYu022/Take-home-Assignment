"""Run the gated real-data PNG experiment; never starts the production player.

From the repository root:
python scripts/playback_probe.py --data-root <snapshot> --output docs/performance/png-probe.json
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import socket
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import av
import numpy as np
import uvicorn
from playwright.sync_api import sync_playwright

from app.config import Settings
from scripts.probe_runtime import ProbeRuntime, create_probe_app


def distribution(values):
    values = list(values)
    if not values:
        return None
    return {"count": len(values), "mean": float(np.mean(values)), "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95)), "max": float(max(values))}


def windows_memory():
    """Real process working set, separate from application cache reservations."""
    if os.name != "nt":
        return {"available": False, "reason": "working-set sampler supports Windows only"}
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak_ws", "ws", "qpp", "qp", "qnp", "qn", "pagefile", "peak_pagefile")]
    counters = Counters(); counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess
    process.restype = wintypes.HANDLE
    getter = ctypes.windll.psapi.GetProcessMemoryInfo
    getter.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not getter(process(), ctypes.byref(counters), counters.cb):
        return {"available": False}
    return {"available": True, "working_set_bytes": counters.ws, "peak_working_set_bytes": counters.peak_ws,
            "scope": "Python runner + in-process probe service; excludes browser/GPU"}


def summarize(result):
    requests = result.pop("requests")
    presentations = result.pop("presentations")
    result["stages_ms"] = {key: distribution(row[key] for row in requests if key in row)
                           for key in ("queue_ms", "decode_ms", "compose_ms", "encode_ms", "ttfb_ms",
                                       "download_ms", "http_ms", "browser_decode_ms", "present_opportunity_ms")}
    result["response_bytes"] = distribution(row["bytes"] for row in requests)
    result["transferred_bytes"] = sum(row["bytes"] for row in requests)
    result["request_count"] = len(requests)
    result["cache_hits"] = sum(row["cache_hit"] for row in requests)
    result["snapshot_identity_ok"] = all(row["frame"] == row["view_frame"] for row in presentations)
    result["presented_frames"] = [row["frame"] for row in presentations]
    result["server_peak"] = {key: max((row["server_resources"][key] for row in requests), default=0)
                             for key in ("peak_active", "peak_waiting", "png_peak", "pixel_peak")}
    if presentations:
        result["first_snapshot"] = presentations[0]
        result["last_snapshot"] = presentations[-1]
    if "results" in result:
        result["seek_ms"] = distribution(row["elapsed_ms"] for row in result["results"])
        for row in result["results"]:
            row["measurement"].pop("server_resources", None)
    return result


def reference_identity(runtime):
    """Independent, straight-through decode, without calling the seek implementation."""
    chosen = {0, len(runtime.timestamps) - 1, 1513, 1514, 1515}
    for frame in runtime.decoder.keyframes:
        chosen.update(i for i in (frame - 1, frame, frame + 1) if 0 <= i < len(runtime.timestamps))
    chosen.update(random.Random(1729).sample(range(len(runtime.timestamps)), 100))
    chosen = sorted(chosen)
    reference = {}
    with av.open(str(runtime.path)) as container:
        for i, frame in enumerate(container.decode(video=0)):
            if i in chosen:
                reference[i] = (int(frame.pts), hashlib.sha256(frame.to_ndarray(format="rgb24").tobytes()).hexdigest())
    checks = []
    order = list(chosen); random.Random(2718).shuffle(order)
    for i in order:
        image, pts = runtime.decoder.get(i)
        rgb_hash = hashlib.sha256(image.tobytes()).hexdigest()
        checks.append({"frame": i, "pts": pts, "rgb_sha256": rgb_hash,
                       "matches_sequential_reference": (pts, rgb_hash) == reference[i]})
    return {"method": "separate sequential av.open/decode RGB24 + integer PTS; shuffled indexed seeks",
            "count": len(checks), "passed": all(row["matches_sequential_reference"] for row in checks), "checks": checks}


def gate(result):
    rate = result["configuration"]["rate"]
    return {"fps_at_least_24": result["effective_fps"] >= 24,
            "active_speed_error_at_most_5_percent": abs(result["active_speed"] / rate - 1) <= .05,
            "buffer_at_most_1_percent": result["buffer_ratio"] <= .01,
            "snapshot_identity": result["snapshot_identity_ok"]}


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=os.getenv("LEROBOT_TEST_DATA_ROOT"))
    parser.add_argument("--output", type=Path, default=ROOT / "docs/performance/png-probe.json")
    parser.add_argument("--screen-seconds", type=float, default=3)
    parser.add_argument("--gate-seconds", type=float, default=30)
    parser.add_argument("--smoke", action="store_true", help="one short configuration only; cannot establish acceptance")
    parser.add_argument("--cache-series", action="store_true", help="bounded reuse of already selected scalar reductions")
    args = parser.parse_args()
    if args.data_root is None or not args.data_root.is_dir():
        parser.error("--data-root or LEROBOT_TEST_DATA_ROOT must name a readable real snapshot")
    report = {"schema_version": 1, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "isolated PNG feasibility probe, not production playback acceptance",
              "instrument_version": 4, "cache_series": args.cache_series,
              "source_sha256": {str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in (ROOT / "app/renderer.py", Path(__file__), ROOT / "scripts/probe_runtime.py", ROOT / "scripts/probe_client.js")},
              "size": [1024, 768], "series": ["base_0_camera/position", "action.left_hand_tracks"],
              "info_bar": True, "metadata": True, "hand_overlays": ["left", "right"],
              "environment": {"os": platform.platform(), "python": platform.python_version(),
                              "machine": platform.machine(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                              "dependencies": {name: importlib.metadata.version(name) for name in
                                               ("av", "numpy", "Pillow", "pyarrow", "fastapi", "uvicorn", "playwright")}},
              "limits": {"server_active": 2, "server_waiting": 4, "server_sessions": 1,
                         "server_png_bytes": 64 * 1024**2, "server_reserved_pixel_bytes": 64 * 1024**2,
                         "browser_in_flight": 2, "browser_waiting": 4, "browser_png_frames": 8,
                         "browser_png_bytes": 12 * 1024**2, "browser_decoded_images": 2,
                         "browser_decoded_pixel_bytes": 16 * 1024**2, "timeline_response_rows": 512},
              "screening": [], "finalists": [], "passed": False}
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            report["environment"]["cpu_model"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    report["environment"]["presentation_method"] = "headless Chromium createImageBitmap + canvas 2D, RAF swap then next RAF opportunity"
    with tempfile.TemporaryDirectory(prefix="lerobot-png-probe-") as cache:
        started = time.perf_counter()
        settings = replace(Settings.from_env(), data_root=args.data_root, cache_dir=Path(cache))
        runtime = ProbeRuntime(args.data_root, settings, cache_series=args.cache_series)
        report["cold_application_index_ms"] = (time.perf_counter() - started) * 1000
        report["dataset"] = {"rows": len(runtime.timestamps), "video_frames": len(runtime.decoder.pts),
                             "video_sha256": hashlib.sha256(runtime.path.read_bytes()).hexdigest(),
                             "keyframe_count": len(runtime.decoder.keyframes), "video_pixels": [runtime.decoder.width, runtime.decoder.height]}
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); sock.listen(128)
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(create_probe_app(runtime), log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True); thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("probe server failed to start")
                time.sleep(.02)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                report["environment"]["browser"] = browser.version
                context = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
                # Avoid screencast screenshots during timing: PNG screenshots can
                # perturb the very image pipeline being measured. Capture visual
                # evidence in a separate short trace after all timed runs.
                context.tracing.start(screenshots=False, snapshots=False)
                page = context.new_page(); page.set_default_timeout(120_000)
                page.goto(base)
                page.wait_for_function("window.probe !== undefined")
                def call(method, options):
                    return page.evaluate("async ([method, options]) => window.probe[method](options)", [method, options])
                initial = {"concurrency": 1, "prefetch": 4, "level": 1}
                report["cold_application_first_seek"] = summarize(call("seeks", {**initial, "frames": [1514]}))
                report["decode_identity"] = reference_identity(runtime)
                configs = [{"concurrency": c, "prefetch": p, "level": level}
                           for c in (1, 2) for p in (4, 6, 8) for level in (1, 3, 6)]
                if args.smoke:
                    configs = [{"concurrency": 2, "prefetch": 6, "level": 1}]
                for cfg in configs:
                    samples = []
                    for rate in (1, 2):
                        result = summarize(call("play", {**cfg, "rate": rate, "seconds": args.screen_seconds, "start": 300}))
                        samples.append(result)
                        print(f"screen c={cfg['concurrency']} p={cfg['prefetch']} png={cfg['level']} {rate}x: "
                              f"{result['effective_fps']:.2f} fps; buffer {result['buffer_ratio']:.2%}; speed {result['active_speed']:.3f}", flush=True)
                    report["screening"].append({"configuration": cfg, "samples": samples})
                    write_report(args.output, report)
                # Rank by the weaker playback result; all candidates stay within the same bounds.
                winner = max(report["screening"], key=lambda candidate: min(row["effective_fps"] for row in candidate["samples"]))
                cfg = winner["configuration"]; report["selected_configuration"] = cfg
                for rate in (1, 2):
                    result = summarize(call("play", {**cfg, "rate": rate, "seconds": args.gate_seconds, "start": 0}))
                    result["gate"] = gate(result); report["finalists"].append(result)
                    print(f"final {rate}x: {result['effective_fps']:.2f} fps; buffer {result['buffer_ratio']:.2%}; "
                          f"active speed {result['active_speed']:.3f}; {result['gate']}", flush=True)
                    write_report(args.output, report)
                frames = random.Random(42).sample(range(len(runtime.timestamps)), 100)
                # Adjacent duplicate pairs guarantee the second request is a server PNG HIT,
                # but both are fresh browser fetch/decode/presentation operations.
                pairs = [frame for target in frames for frame in (target, target)]
                seeks = summarize(call("seeks", {**cfg, "frames": pairs}))
                report["random_seeks"] = seeks
                misses = [row["elapsed_ms"] for row in seeks["results"] if not row["measurement"]["cache_hit"]]
                hits = [row["elapsed_ms"] for row in seeks["results"] if row["measurement"]["cache_hit"]]
                report["warm_png_miss_ms"] = distribution(misses); report["png_hit_ms"] = distribution(hits)
                report["warm_seek_gate"] = bool(len(misses) == 100 and len(hits) == 100 and
                                                seeks["request_count"] == 200 and np.percentile(misses, 95) <= 250)
                # Retain a current real-data image and browser opportunity evidence in ignored runtime artifacts.
                artifact = ROOT / ".runtime/playback-probe"; artifact.mkdir(parents=True, exist_ok=True)
                context.tracing.stop(path=str(artifact / "measurement-trace.zip"))
                context.tracing.start(screenshots=True, snapshots=True)
                call("seeks", {**cfg, "frames": [1514], "hold": True})
                page.screenshot(path=str(artifact / "probe-page.png"))
                call("stop", {})
                context.tracing.stop(path=str(artifact / "trace.zip"))
                context.close(); browser.close()
                report["passed"] = (not args.smoke and args.gate_seconds >= 30 and report["decode_identity"]["passed"]
                                    and report["warm_seek_gate"] and all(all(row["gate"].values()) for row in report["finalists"]))
        except Exception as exc:
            report["execution_error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            server.should_exit = True; thread.join(timeout=20); sock.close()
            if thread.is_alive():
                report["cleanup_error"] = "probe service did not exit within 20 seconds"
                report["passed"] = False
            report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            report["python_process_memory"] = windows_memory()
            write_report(args.output, report)
    print(f"probe {'PASSED' if report['passed'] else 'NOT ACCEPTED'}; report: {args.output}")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
