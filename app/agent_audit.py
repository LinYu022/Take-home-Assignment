"""Append-only, crash-resilient client evidence; never treat a server log as model input."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def text_content(response):
    result = response.get("result", response)
    return [item["text"] for item in result.get("content", []) if item.get("type") == "text"]


class RunLedger:
    """Persist received objects BEFORE parsing or user computation; reserve before sending."""
    def __init__(self, directory, *, calls=40, text_bytes=204800, resume=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=resume)
        path=self.directory/"calls.jsonl"
        previous=[json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()] if resume else []
        self.handle = path.open("a" if resume else "x", encoding="utf-8")
        self.max_calls, self.max_bytes = calls, text_bytes
        self.records=[r for r in previous if "ordinal" in r]
        self.calls=len(self.records);self.bytes=sum(r["text_utf8_bytes"] for r in self.records)

    def reserve(self, output_bytes, *, final_reserve=4096):
        if not 512 <= output_bytes or self.calls >= self.max_calls:
            raise ValueError("CallBudgetExhausted")
        if self.bytes + output_bytes + final_reserve > self.max_bytes:
            raise ValueError("TextBudgetExhausted: stop before making a call")
        return output_bytes

    def event(self, value):
        self.handle.write(packed(value).decode() + "\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def received(self, request, response, *, wire_bytes=None, delivery="client_received"):
        # Actual wire is captured by the proxy first, before even JSON decoding.
        parts = text_content(response)
        count = sum(len(s.encode("utf-8")) for s in parts)
        self.calls += 1; self.bytes += count
        saved = json.loads(packed(response))
        for item in saved.get("result", saved).get("content", []):
            if item.get("type") == "image":
                png = base64.b64decode(item.pop("data"), validate=True)
                digest = hashlib.sha256(png).hexdigest()
                filename = f"call-{self.calls:03d}-{digest[:12]}.png"
                (self.directory / filename).write_bytes(png)
                item.update(path=filename, bytes=len(png), sha256=digest)
        result = response.get("result", {})
        structured = result.get("structuredContent", {})
        record = dict(ordinal=self.calls, request=request, response=saved,
                      text_characters=sum(map(len, parts)), text_utf8_bytes=count,
                      cumulative_text_utf8_bytes=self.bytes, mcp_object_bytes=len(packed(response)),
                      wire_bytes=wire_bytes, remaining_calls=self.max_calls-self.calls,
                      remaining_text_bytes=self.max_bytes-self.bytes, delivery=delivery,
                      returned_count=structured.get("returned_count"),
                      range={k: structured.get(k) for k in ("start_frame", "end_frame")},
                      used_for_computation="not_yet_reported", view_url=structured.get("view_url"),
                      truncated=structured.get("truncated"), next_cursor=structured.get("next_cursor"))
        self.event(record); self.records.append(record)
        if self.calls > self.max_calls or self.bytes > self.max_bytes:
            raise ValueError("BudgetViolation: persisted response must not be discarded")
        return record

    def close(self):
        self.handle.close()


def validate_final_links(text, known, decode):
    """Check final delivered strings, not only intermediate encoder variables."""
    import re
    urls = re.findall(r"https?://[^\s<>\)\]\"']+", text)
    results = []
    for url in urls:
        if "/view?state=" not in url: continue
        record = dict(url=url, exact_source_call=known.get(url), valid=False)
        try:
            state = decode(url)
            if url not in known: raise ValueError("URL is not an unmodified tool result")
            record.update(valid=True, view=state.to_dict() if hasattr(state, "to_dict") else state)
        except Exception as exc:
            record["error"] = type(exc).__name__ + ": " + str(exc)
        results.append(record)
    return results
