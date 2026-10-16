"""Measure the production browser player; historical probe results stay untouched."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import random
import re
import socket
import sys
import tempfile
import threading
import subprocess
from types import SimpleNamespace
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
import numpy as np
import uvicorn
from playwright.sync_api import sync_playwright
from app.config import Settings
from app.main import create_app
from app.render_service import PNG_BYTES
from scripts.playback_probe import distribution, windows_memory
from scripts.playback_probe import reference_identity
from app.data.adapter import DatasetAdapter
from app.data.source import LocalObjectStore
from app.video import IndexedDecoder


def summarize(metrics, source_fps):
    run=metrics["plays"][-1]; end=run.get("ended_at",metrics["presentations"][-1]["at_ms"])
    wall=(end-run["started_at"])/1000
    frames=[r for r in metrics["presentations"] if r["at_ms"]>=run["started_at"]]
    distinct=len({r["frame"] for r in frames if r["frame"]!=run["start_frame"]})
    buffer=sum(max(0,min(b["end"],end)-max(b["start"],run["started_at"])) for b in metrics["buffers"])/1000
    source=((run.get("end_pts") or frames[-1]["pts_ns"])-run["start_pts"])/1e9
    theoretical=min(30,run["rate"]*source_fps)
    average_gate=.8*theoretical if run["rate"]==.5 else 24
    window_gate=theoretical*2/3 if run["rate"]==.5 else 20
    windows=[]
    for i in range(int(wall//5)):
        a=run["started_at"]+i*5000; b=a+5000
        fps=len({r["frame"] for r in frames if a<r["at_ms"]<=b})/5
        windows.append(dict(start_s=i*5,fps=fps,passed=fps>=window_gate))
    longest=max((b["end"]-b["start"] for b in metrics["buffers"]),default=0)
    result=dict(rate=run["rate"],elapsed_s=wall,source_seconds=source,effective_fps=distinct/wall,
                active_speed=source/max(.0001,wall-buffer),overall_speed=source/wall,buffer_ratio=buffer/wall,
                longest_buffer_ms=longest,distinct_frames=distinct,skipped_source_frames=run.get("end_frame",0)-run["start_frame"]-distinct,
                average_fps_threshold=average_gate,window_fps_threshold=window_gate,windows=windows,
                stages_ms={key:distribution(r[key] for r in metrics["requests"] if key in r) for key in ("ttfb_ms","http_ms","browser_decode_ms")},
                server_stages_ms={key:distribution(r["timing"][key] for r in metrics["requests"] if key in r.get("timing",{})) for key in ("queue_ms","decode_ms","compose_ms","encode_ms")},
                wire_bytes=sum(r["bytes"] for r in metrics["requests"]),browser_peak=metrics["peaks"])
    result["gates"]=dict(fps=result["effective_fps"]>=average_gate,windows=all(w["passed"] for w in windows),
                         active_speed=abs(result["active_speed"]/run["rate"]-1)<=.05,buffer=result["buffer_ratio"]<=.01,longest_buffer=longest<=500)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root",required=True,type=Path)
    parser.add_argument("--output",type=Path,default=ROOT/"docs/performance/player-20261015.json")
    parser.add_argument("--session-name",default="session-003")
    args=parser.parse_args()
    if args.output.exists(): parser.error("output exists; choose a new report name")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",args.session_name): parser.error("invalid session name")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    result=dict(schema_version=1,started_at=datetime.now(timezone.utc).isoformat(),environment=dict(os=platform.platform(),python=platform.python_version(),logical_cpus=os.cpu_count()),
                default_size=[1024,768],dense_size=[1600,1200],runs=[],passed=False)
    def save():args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="player-benchmark-") as cache:
        reference_adapter=DatasetAdapter(LocalObjectStore(args.data_root),Path(cache)/"reference")
        path=reference_adapter.materialize(reference_adapter.video_key(0))
        decoder=IndexedDecoder(path,reference_adapter.episodes[0].length)
        try:
            result["video_identity"]=reference_identity(SimpleNamespace(path=path,decoder=decoder,timestamps=reference_adapter.table(0)["timestamp_ns"].to_pylist()))
        finally: decoder.close()
        result["source_sha256"]={str(p.relative_to(ROOT)).replace("\\","/"):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/"app/assets/player.js",ROOT/"app/renderer.py",ROOT/"app/compositor.py",ROOT/"app/render_service.py",ROOT/"app/video.py",ROOT/"app/assets/NotoSansSC-VF.ttf"]}
        sock=socket.socket();sock.bind(("127.0.0.1",0));sock.listen(128);base=f"http://127.0.0.1:{sock.getsockname()[1]}"
        settings=replace(Settings.from_env(),data_root=args.data_root,cache_dir=Path(cache),public_base_url=base)
        app=create_app(settings);server=uvicorn.Server(uvicorn.Config(app,log_level="error",access_log=False,timeout_graceful_shutdown=10));thread=threading.Thread(target=server.run,kwargs={"sockets":[sock]},daemon=True)
        cold=time.perf_counter();thread.start()
        try:
            deadline=time.monotonic()+30
            while not server.started:
                if time.monotonic()>deadline:raise RuntimeError("service startup timed out")
                time.sleep(.02)
            svc=app.state.render_service
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True);result["environment"]["browser"]=browser.version
                context=browser.new_context(viewport={"width":1600,"height":1100},device_scale_factor=1)
                page=context.new_page();page.set_default_timeout(30000);page.goto(base+"/view")
                page.wait_for_function("window.visualizer?.snapshot!==null && window.visualizer?.snapshot!==undefined")
                page.evaluate("document.fonts.ready")
                result["cold_application_first_display_ms"]=(time.perf_counter()-cold)*1000
                row=page.request.post(base+"/api/playback/prepare",data={"view":{}}).json();source_fps=row["source_fps"];length=row["length"]
                page.request.post(base+f"/api/playback/{row['session']}/generation",data={"generation":1,"release":True})
                result["source_fps"]=source_fps;result["source_frames"]=length
                for rate in (.5,1,2):
                    page.evaluate("async()=>{visualizer.pause();await visualizer.seek(0);visualizer.resetMetrics()}")
                    page.select_option("#rate",str(rate if rate!=1 else 1))
                    page.evaluate("()=>visualizer.play()")
                    deadline_s=max(120,((length-1)/source_fps/rate)*4+60)
                    page.wait_for_function(f"!visualizer.playing && visualizer.snapshot.frame==={length-1}",timeout=deadline_s*1000)
                    data=page.evaluate("visualizer.metrics")
                    measured=summarize(data,source_fps);measured["completed_episode"]=True;result["runs"].append(measured)
                    print(f"production {rate}x: {measured['effective_fps']:.2f}fps, speed={measured['active_speed']:.3f}, buffer={measured['buffer_ratio']:.2%}, {measured['gates']}",flush=True);save()
                jumps=[]
                for target in random.Random(42).sample(range(length),100):
                    # Only the PNG cache is emptied. Data, font, reductions and
                    # PTS/index remain warm, and the HIT is a new HTTP request.
                    with svc.cv:svc._evict_png(PNG_BYTES)
                    for kind in ("MISS","HIT"):
                        record=page.evaluate("async f=>{const at=performance.now();await visualizer.seek(f);const s=visualizer.snapshot;return {elapsed_ms:performance.now()-at,frame:s.frame,sha256:s.sha256,request:visualizer.metrics.requests.at(-1)}}",target)
                        record["expected_cache"]=kind
                        assert record["frame"]==target and record["request"]["timing"]["cache_hit"]==(kind=="HIT")
                        jumps.append(record)
                result["jumps"]=jumps
                result["warm_png_miss_ms"]=distribution(r["elapsed_ms"] for r in jumps if r["expected_cache"]=="MISS")
                result["png_hit_ms"]=distribution(r["elapsed_ms"] for r in jumps if r["expected_cache"]=="HIT")
                result["jump_gate"]=result["warm_png_miss_ms"]["p95"]<=250
                fields=["base_0_camera/position","base_0_camera/quaternion_xyzw","left_hand/tracks","right_hand/tracks","action.left_hand_tracks","action.right_hand_tracks","timestamp","timestamp_ns"]
                page.select_option("#series",fields)
                page.wait_for_function("visualizer.snapshot.view.series.length===8 && visualizer.snapshot.view.render.width===1600")
                page.evaluate("async()=>{await visualizer.seek(0);visualizer.resetMetrics()}");page.select_option("#rate","1");page.evaluate("()=>visualizer.play()")
                page.wait_for_timeout(30000);page.evaluate("visualizer.pause()")
                result["eight_fields_30s"]=summarize(page.evaluate("visualizer.metrics"),source_fps)
                result["eight_fields_30s"]["performance_gates_applicable"]=False
                page.select_option("#locale","zh");page.wait_for_function("visualizer.snapshot.view.display.locale==='zh'")
                artifacts=ROOT/".runtime/player-acceptance";artifacts.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(artifacts/"eight-fields-zh-page.png"),full_page=True)
                raw=page.request.get(base+"/render"+page.url.split("/view",1)[1]).body()
                (artifacts/"eight-fields-zh.png").write_bytes(raw)
                assert hashlib.sha256(raw).hexdigest()==page.evaluate("visualizer.snapshot.sha256")
                page.click("#fullscreen");page.wait_for_function("document.fullscreenElement?.id==='stage'")
                page.screenshot(path=str(artifacts/"fullscreen-zh.png"))
                page.evaluate("document.exitFullscreen()")
                result["server_resources"]=svc.stats();result["python_memory"]=windows_memory()
                replay=ROOT/"agent-sessions"/(args.session_name+".jsonl")
                if replay.exists(): raise RuntimeError("session-003 exists; refusing to overwrite historical replay")
                process=subprocess.run([sys.executable,str(ROOT/"scripts/replay_mcp_session.py"),"--url",base+"/mcp","--output",str(replay)],capture_output=True,text=True,check=True)
                result["mcp_replay"]=json.loads(process.stdout.strip())
                summary={**result["mcp_replay"],"executed_at":datetime.now(timezone.utc).isoformat(),"scope":"12-call regression replay; not a complete agent analysis conversation", "transcript_sha256":hashlib.sha256(replay.read_bytes()).hexdigest()}
                (ROOT/"agent-sessions"/(args.session_name+".json")).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                result["performance_passed"]=all(all(r["gates"].values()) for r in result["runs"]) and result["jump_gate"]
                context.close();browser.close()
        except Exception as exc:
            result["execution_error"]=f"{type(exc).__name__}: {exc}";save();raise
        finally:
            server.should_exit=True;thread.join(timeout=30);sock.close()
            result["cleanup_passed"]=not thread.is_alive()
            result["resources_after_shutdown"]=app.state.render_service.stats()
            result["cleanup_passed"] &= all(result["resources_after_shutdown"][key]==0 for key in ("active","waiting","sessions","png_bytes","png_reserved","pixel_bytes","dispatches"))
            result["passed"]=bool(result.get("performance_passed")) and result["cleanup_passed"] and result["video_identity"]["passed"]
            result["finished_at"]=datetime.now(timezone.utc).isoformat();save()
            if thread.is_alive():
                server.force_exit=True
                raise RuntimeError("benchmark service did not stop")
    print(f"production acceptance {'PASSED' if result['passed'] else 'NOT ACCEPTED'}: {args.output}")
    return 0 if result["passed"] else 2


if __name__=="__main__":raise SystemExit(main())
