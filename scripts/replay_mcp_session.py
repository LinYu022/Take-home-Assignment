"""Replay the documented Agent workflow against a running local MCP endpoint.

The JSONL output is deliberately redacted: image bytes are represented by a
hash and size, and credentials are never read from or written to the record.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import urllib.request
from pathlib import Path


def post(url: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-03-26",
    })
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
        if not raw:
            return {"http_status": response.status, "empty": True}
        return json.loads(raw.decode("utf-8"))


def redact(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"image_base64", "data"} and isinstance(item, str) and len(item) > 100:
                try:
                    raw = base64.b64decode(item)
                    result[key] = {"mime_type": "image/png", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                except Exception:
                    result[key] = "[REDACTED_BINARY]"
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


def open_transcript(path: Path, append: bool):
    return path.open("a" if append else "w", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp"))
    parser.add_argument("--output", type=Path, default=Path("agent-sessions/session-001.jsonl"))
    parser.add_argument("--append", action="store_true", help="append to the transcript instead of replacing it")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    request_id = 0
    transcript = open_transcript(args.output, args.append)

    def call(question: str, method: str, params=None):
        nonlocal request_id
        request_id += 1
        request = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        response = post(args.url, request)
        record = {"question": question, "method": method, "request": request, "response": redact(response)}
        transcript.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        transcript.flush()
        return response

    init = call("Initialize the MCP session", "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "replay-mcp-session", "version": "1"}})
    call("Complete MCP initialization", "notifications/initialized")
    listed = call("Discover the available tools", "tools/list")
    names = [tool["name"] for tool in listed["result"]["tools"]]
    if names != ["describe_dataset", "list_episodes", "query_series", "aggregate_series", "encode_view", "decode_view", "render_view", "compare_series", "audit_timing", "audit_integrity"]:
        raise SystemExit(f"unexpected tool list: {names}")

    def tool(question, name, arguments):
        result = call(question, "tools/call", {"name": name, "arguments": arguments})
        if "error" in result or result.get("result",{}).get("isError"):
            raise SystemExit(result)
        return result["result"]["structuredContent"]

    described = tool("How many episodes, fields, and frames exist?", "describe_dataset", {})
    tool("List the first episode with its time domains.", "list_episodes", {"limit": 1, "cursor": 0})
    query = tool("Read a bounded hand-track component over the episode.", "query_series", {"episode": 0, "field": "action.left_hand_tracks", "component": 0, "start_frame": 0, "end_frame": int(described["manifest"]["episodes"][0]["length"]), "limit": 256})
    if not query["truncated"] or query["next_cursor"] is None:
        raise SystemExit("query was not truncated as expected")
    tool("Continue the hand-track query using the returned cursor.", "query_series", {"episode": 0, "field": "action.left_hand_tracks", "component": 0, "start_frame": 0, "end_frame": int(described["manifest"]["episodes"][0]["length"]), "limit": 256, "cursor": query["next_cursor"]})
    tool("Compute exact statistics for the same component.", "aggregate_series", {"episode": 0, "field": "action.left_hand_tracks", "component": 0, "start_frame": 0, "end_frame": int(described["manifest"]["episodes"][0]["length"])})
    view = {"episode": 0, "cursor": {"frame": 1514}, "time_range": None, "panels": ["video", "series", "metadata"], "series": ["base_0_camera/position", "action.left_hand_tracks"], "overlays": [{"type": "hand_tracks", "sides": ["left", "right"]}], "render": {"width": 1024, "height": 768}, "evidence": None}
    encoded = tool("Create a shareable view for frame 1514.", "encode_view", {"view": view})
    tool("Decode the shareable view and verify its state.", "decode_view", {"url": encoded["view_url"]})
    rendered = tool("Show frame 1514 with both hand overlays.", "render_view", {"url": encoded["view_url"]})
    if rendered.get("bytes", 0) <= 0:
        raise SystemExit("render_view did not return an image")
    tool("What is the success rate across tasks?", "describe_dataset", {})
    transcript.close()
    print(json.dumps({"output": str(args.output), "calls": request_id, "tools": names, "image_sha256": rendered.get("sha256"), "append": args.append}, ensure_ascii=False))


if __name__ == "__main__":
    main()

