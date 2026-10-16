from __future__ import annotations

import base64
import json
from typing import Any

import numpy as np

from .data.errors import DataNotFound, QueryLimitError, ToolArgumentError
from .viewstate import decode_view, encode_view, integer, normalize_view, validation_context


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _view_url(state, settings):
    return encode_view(state, settings.public_base_url, settings.max_view_url_chars)


def _episode(adapter, episode):
    result = next((e for e in adapter.episodes if e.episode_index == episode), None)
    if result is None:
        raise DataNotFound(f"Episode {episode} not found")
    return result


def _view_for(adapter, settings, episode=None, frame=0, field=None, component=None, evidence=None):
    if episode is None:
        episode = adapter.episodes[0].episode_index
    raw = {"episode": episode, "cursor": {"frame": min(frame, _episode(adapter, episode).length - 1)}}
    if field is not None:
        raw["series"] = [f"{field}[{component}]" if component is not None else field]
    if evidence is not None:
        raw["evidence"] = evidence
    return normalize_view(raw, **validation_context(adapter, settings))


def _selected_frames(adapter, episode, args):
    info = _episode(adapter, episode)
    using_time = args.get("start_ns") is not None or args.get("end_ns") is not None
    using_frames = args.get("start_frame") is not None or args.get("end_frame") is not None
    if using_time and using_frames:
        raise DataNotFound("frame and timestamp ranges are mutually exclusive")
    time_range = None
    if using_time:
        start_ns = integer(args.get("start_ns", info.data_start_ns), "start_ns")
        end_ns = integer(args.get("end_ns", info.data_end_ns), "end_ns")
        if not info.data_start_ns <= start_ns < end_ns <= info.data_end_ns:
            raise DataNotFound(f"time range must be within [{info.data_start_ns},{info.data_end_ns})")
        ts = np.asarray(adapter.table(episode)["timestamp_ns"].to_pylist(), dtype=np.int64)
        start, end = map(int, np.searchsorted(ts, [start_ns, end_ns], side="left"))
        time_range = {"start": start_ns, "end": end_ns}
    else:
        start = integer(args.get("start_frame", 0), "start_frame")
        end = integer(args.get("end_frame", info.length), "end_frame")
        if not 0 <= start < end <= info.length:
            raise DataNotFound("frame range must be within [0,episode.length]")
    if start == end: raise ToolArgumentError("range", "range contains no sampling rows")
    return start, end, time_range


def _values(adapter, episode, field, start, end, component):
    catalog = {f.name: f for f in adapter.fields()}
    if field not in catalog:
        raise DataNotFound("unknown field")
    length = getattr(catalog[field], "observed_length", None)
    if length is None:
        sample = adapter.table(episode)[field].slice(0, 1).to_pylist()
        length = len(sample[0]) if sample and isinstance(sample[0], list) else None
    if component is not None:
        integer(component, "component")
        if length is None or not 0 <= component < length:
            raise DataNotFound("component is out of range; scalars use component=null")
    values = adapter.table(episode)[field].slice(start, end - start).to_pylist()
    if component is not None:
        return [row[component] for row in values], 1
    return values, length or 1


def _evidence(adapter, episode, field, component, start, end, time_range, page_start, page_end, limit, kind):
    ts = adapter.table(episode)["timestamp_ns"].to_pylist()
    info = _episode(adapter, episode)
    returned_start = int(ts[page_start]) if page_start < info.length else info.data_end_ns
    returned_end = int(ts[page_end]) if page_end < info.length else info.data_end_ns
    return {"kind": kind, "field": field, "component": component,
            "requested_frame_range": {"start": start, "end": end},
            "requested_time_range": time_range,
            "returned_frame_range": {"start": page_start, "end": page_end},
            "returned_time_range": {"start": returned_start, "end": returned_end},
            "page_offset": page_start - start, "page_limit": limit}


def _bounded(result, settings):
    if len(json_bytes(result)) > settings.max_query_bytes:
        raise QueryLimitError(f"complete tool result exceeds {settings.max_query_bytes} bytes")
    return result


def tool_definitions(settings=None):
    rows = getattr(settings, "max_query_rows", 256)
    output = getattr(settings, "max_query_bytes", 128000)
    scalar = {"type": "integer", "minimum": 0}
    ranges = {"episode": scalar, "field": {"type": "string", "maxLength": 200},
              "component": {"type": ["integer", "null"], "minimum": 0},
              "start_frame": scalar, "end_frame": scalar, "start_ns": scalar, "end_ns": scalar}
    def definition(name, description, props, required=()):
        return {"name": name, "description": description,"annotations":{"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False},
                "inputSchema": {"type": "object", "properties": {**props, "max_output_bytes": {"type": "integer", "minimum": 512, "maximum": output}}, "required": list(required),
                                "additionalProperties": False}}
    definitions = [
        definition("describe_dataset", "Discover bounded episode/field catalogs with original description, coordinate frame, units and units_source, actual vector lengths, precise data/video time domains and missing evidence. Read per-field semantics before choosing raw components; only explicitly timestamp-prefixed vectors have ts_ns at index 0. Catalog cap: 32 fields, 20 episodes. Includes view_url, truncated and continuation instructions.", {}),
        definition("list_episodes", "Paginate episode metadata, with data_start_ns/data_end_ns and independent video_start_ns/video_end_ns. limit 1–100, cursor is an offset; every page has view_url.", {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": scalar}),
        definition("query_series", "Read ONE field over a half-open frame [start_frame,end_frame) OR nanosecond [start_ns,end_ns) range; never mix them. component is the RAW zero-based vector index (embedded timestamp remains index 0); omit/null means full vector. Default 256 rows; also bounded by scalar count and COMPLETE UTF-8 result bytes including timestamps and evidence URL. Reuse the ORIGINAL args and returned next_cursor to continue without overlap. Evidence retains requested and returned ranges and selected component. Empty or reversed ranges are rejected.", {**ranges, "limit": {"type": "integer", "minimum": 1, "maximum": 256}, "cursor": scalar}, ("episode", "field")),
        definition("aggregate_series", "Exact count/min/max/mean/p50/p95 for one scalar field or RAW vector component over a half-open frame OR time range. Input computation capped at 1,000,000 rows; output separately byte-bounded. Vector fields require component. Returns input_range, input_count, exact, view_url. No claims about success rates or absent annotations.", ranges, ("episode", "field")),
        definition("encode_view", "Validate canonical ViewState v1 and encode URL <= MAX_VIEW_URL_CHARS (default 16384). cursor and time_range are exclusive; time_range must be within the data domain. Up to 8 series and 8 overlays; series field[index] selects a raw vector component. Optional evidence preserves query ranges and pagination.", {"view": {"type": "object"}}, ("view",)),
        definition("decode_view", "Decode a bounded view_url/token and recover canonical ViewState including evidence component, requested range and returned page range.", {"url": {"type": "string", "maxLength": 16384}}, ("url",)),
        definition("render_view", "Browser-free deterministic PNG: ONE MCP image content block plus manifest/hash/view_url; no base64 in text. Use view OR url, not both. Width 320–1600, height 240–1200, PNG <= 2 MiB by default. time_range/evidence determine exact curve window; minimum display expansion is explicit in manifest.", {"view": {"type": "object"}, "url": {"type": "string", "maxLength": 16384},
                    "width": {"type": "integer", "minimum": 320, "maximum": 1600}, "height": {"type": "integer", "minimum": 240, "maximum": 1200}})
    ]
    definitions[0]["inputSchema"]["properties"].update(field_limit={"type": "integer", "minimum": 1, "maximum": 32}, field_cursor=scalar)
    definitions[0]["description"] = "Discover declared and observed field semantics, provenance, success metadata declaration, precise time domains and missing evidence. Fields: field_limit 1..32, field_cursor offset; continue with next_field_cursor. Episodes continue separately with list_episodes. sampled observed_length is NOT a full structure audit; use audit_integrity. Coordinate/units must come from metadata, never assume a timestamp prefix. Every result has view_url."
    definitions[2]["inputSchema"]["properties"]["limit"]["maximum"] = rows
    for d in definitions:
        props=d["inputSchema"]["properties"]
        for param,setting in (("width","max_render_width"),("height","max_render_height")):
            if param in props:props[param]["maximum"]=getattr(settings,setting,props[param]["maximum"])
        if "url" in props:props["url"]["maxLength"]=getattr(settings,"max_view_url_chars",16384)
    definitions[3]["description"]=definitions[3]["description"].replace("1,000,000",str(getattr(settings,"max_aggregate_rows",1000000)))
    definitions[2]["description"] = definitions[2]["description"].replace("Default 256 rows", f"Default/maximum {rows} rows; larger limit is an error") + " Top-level start/end are THIS PAGE; requested_frame_range is the original selection. cursor is an offset WITHIN that selection. requested_limit/returned_count/truncation_reasons explain smaller legal pages."
    selection = {"type": "object", "properties": {"field": ranges["field"], "component": ranges["component"]}, "required": ["field"], "additionalProperties": False}
    audit_range = {"episode": scalar, "start_frame": scalar, "end_frame": scalar, "cursor": scalar,
                   "max_examples": {"type": "integer", "minimum": 0, "maximum": 5}}
    limits = f" Max {getattr(settings,'max_audit_rows',8192)} rows, {getattr(settings,'max_audit_scalars',1000000)} scalars, 5 examples per call. Return completed/uncompleted ranges and continuation; repeat ORIGINAL arguments with returned cursor. max_output_bytes caps all text including errors; no arbitrary code."
    definitions += [
        definition("compare_series", "Pair scalar/raw-component values by frame_index: right_frame=t+right_frame_offset. Vectors require component. exact_numeric means finite binary64 numeric equality (+0=-0); unsafe integer->float conversions rejected. Nonfinite/missing/duplicate pairs are not matches; zero finite pairs yields null errors. Returns exact match counts separately from floating MAE/RMSE/max and coverage. Every result carries replay parameters and window URL; mismatch examples carry BOTH frame URLs." + limits,
                   {**audit_range, "left": selection, "right": selection, "right_frame_offset": {"type": "integer"}, "equality": {"type": "string", "enum": ["exact_numeric"]}}, ("episode", "left", "right")),
        definition("audit_timing", "Read-only timestamp_ns/PTS audit over half-open physical row offsets. Reports adjacent-within-range intervals, nulls, duplicates/backwards, frame-index paired PTS coverage/deltas. Raw integer PTS and rational time_base use integer ties-to-even ns conversion rational_ns_v1. Encoding agreement cannot establish capture precision, contact time, or content alignment; no automatic anomaly threshold." + limits, audit_range, ("episode",)),
        definition("audit_integrity", "Read-only PRE-validation diagnostic for up to 8 numeric fields: null/nonfinite, bounded length histogram, EXACT all-zero vector count (not mean==0), payload/nonzero counts and maxabs. Physical row offsets distinguish duplicate/bad frame_index; examples use safe type markers, never JSON NaN/Infinity. Invalid-source URLs are explicitly unavailable for strict rendering; unchecked is never zero anomalies." + limits,
                   {**audit_range, "fields": {"type": "array", "items": ranges["field"], "minItems": 1, "maxItems": 8, "uniqueItems": True}}, ("episode", "fields")),
    ]
    for d in definitions:
        d["description"] += " Optional max_output_bytes bounds complete content.text UTF-8 bytes (>=512); failed business calls return isError with code/parameter/constraint/recovery."
    return definitions


def _validate(value, schema, path="arguments"):
    types = schema.get("type", [])
    if isinstance(types, str): types = [types]
    valid = {"integer": isinstance(value, int) and not isinstance(value, bool), "number": isinstance(value, (int,float)) and not isinstance(value,bool),
             "string": isinstance(value, str), "object": isinstance(value, dict), "array": isinstance(value, list), "null": value is None}
    if types and not any(valid.get(t, False) for t in types): raise ToolArgumentError(path, "type must be " + "/".join(types))
    if value is None: return
    for key, op in (("minimum", lambda a,b:a>=b),("maximum",lambda a,b:a<=b)):
        if key in schema and not op(value,schema[key]): raise ToolArgumentError(path, f"{key}={schema[key]}")
    if "enum" in schema and value not in schema["enum"]: raise ToolArgumentError(path, "allowed=" + str(schema["enum"]))
    if isinstance(value, str) and len(value)>schema.get("maxLength",10**9): raise ToolArgumentError(path,"string too long")
    if isinstance(value, dict):
        props = schema.get("properties")
        if props is not None:
            if schema.get("additionalProperties") is False and set(value)-set(props): raise ToolArgumentError(path,"unknown keys")
            for k in schema.get("required", []):
                if k not in value: raise ToolArgumentError(k,"required")
            for k,v in value.items():
                if k in props: _validate(v,props[k], k)
    if isinstance(value,list):
        if not schema.get("minItems",0)<=len(value)<=schema.get("maxItems",10**9): raise ToolArgumentError(path,"invalid item count")
        if schema.get("uniqueItems") and len(set(map(str,value)))!=len(value): raise ToolArgumentError(path,"items must be unique")
        for item in value: _validate(item,schema.get("items",{}),path)


class _BudgetSettings:
    def __init__(self, original, budget): self.original,self.max_query_bytes=original,budget
    def __getattr__(self, name): return getattr(self.original,name)


def call_tool(name: str, args: dict[str, Any], adapter, settings, renderer):
    definitions = {d["name"]:d for d in tool_definitions(settings)}
    if name not in definitions: raise ToolArgumentError("name", "unknown tool")
    _validate(args, definitions[name]["inputSchema"])
    budget = args.get("max_output_bytes",settings.max_query_bytes)
    args = {k:v for k,v in args.items() if k!="max_output_bytes"}
    return _call_tool(name,args,adapter,_BudgetSettings(settings,budget),renderer)


def _call_tool(name: str, args: dict[str, Any], adapter, settings, renderer):
    definitions = {d["name"]: d for d in tool_definitions(settings)}
    if name not in definitions:
        raise DataNotFound("unknown tool")
    schema = definitions[name]["inputSchema"]
    if not isinstance(args, dict) or set(args) - set(schema["properties"]):
        raise DataNotFound("tool arguments contain unknown keys")
    if any(k not in args for k in schema["required"]):
        raise DataNotFound("missing required tool arguments")
    if name == "describe_dataset":
        manifest = adapter.manifest()
        all_fields = manifest["fields"]
        field_cursor, field_limit = args.get("field_cursor",0),args.get("field_limit",32)
        if field_cursor and field_cursor>=len(all_fields): raise ToolArgumentError("field_cursor","outside field catalog")
        manifest["episodes"] = manifest["episodes"][:20]
        manifest["fields"] = all_fields[field_cursor:field_cursor+field_limit]
        next_field = field_cursor+len(manifest["fields"])
        truncated = len(adapter.episodes) > 20 or next_field < len(all_fields)
        return _bounded({"manifest": manifest, "coordinate_notes": {
            "time_key": "timestamp_ns", "time_range": "half-open data sampling domain",
            "components": "Raw vector indices. Read each field's description/frame; only timestamp-prefixed vectors have ts_ns at index 0. MANO has no leading timestamp.",
            "missing_evidence": ["population success rate", "subtask intervals", "force/contact", "anatomical point names", "MANO betas cause"]},
            "truncated": truncated, "next_cursor": 20 if len(adapter.episodes) > 20 else None,
            "next_field_cursor": next_field if next_field<len(all_fields) else None,
            "field_cursor": field_cursor, "total_fields": len(all_fields),
            "continuation": "Fields: describe_dataset(field_cursor=next_field_cursor); episodes: list_episodes(cursor=next_cursor)." if truncated else None,
            "view_url": _view_url(_view_for(adapter, settings), settings)}, settings)
    if name == "list_episodes":
        limit = integer(args.get("limit", 20), "limit")
        cursor = integer(args.get("cursor", 0), "cursor")
        if not 1 <= limit <= 100 or cursor < 0:
            raise QueryLimitError("limit must be 1–100; cursor non-negative")
        while True:
            items = [{**ep.as_dict(), "view_url": _view_url(_view_for(adapter, settings, ep.episode_index), settings)}
                     for ep in adapter.episodes[cursor:cursor + limit]]
            more = cursor + len(items) < len(adapter.episodes)
            result = {"episodes": items, "cursor": cursor, "limit": limit, "truncated": more,
                      "next_cursor": cursor + len(items) if more else None,
                      "view_url": _view_url(_view_for(adapter, settings), settings)}
            if len(json_bytes(result)) <= settings.max_query_bytes:
                return result
            if limit == 1:
                raise QueryLimitError("one episode exceeds max_query_bytes")
            limit = max(1, limit // 2)
    if name in {"query_series", "aggregate_series"}:
        episode = integer(args["episode"], "episode")
        info = _episode(adapter, episode)
        field, component = args["field"], args.get("component")
        start, end, requested_time = _selected_frames(adapter, episode, args)
        if name == "query_series":
            cursor, requested_limit = integer(args.get("cursor", 0), "cursor"), integer(args.get("limit", settings.max_query_rows), "limit")
            if cursor < 0 or cursor >= end - start or requested_limit < 1:
                raise QueryLimitError("cursor outside selected range or limit below 1")
            page_start = start + cursor
            page_end = min(end, page_start + requested_limit)
            reasons = []
            while True:
                values, scalar_width = _values(adapter, episode, field, page_start, page_end, component)
                # Timestamp array and value array both count against scalar budget.
                evidence = _evidence(adapter, episode, field, component, start, end, requested_time, page_start, page_end, requested_limit, "series_query")
                view = _view_for(adapter, settings, episode, min(page_start, info.length - 1), field, component, evidence)
                more = page_end < end
                result = {"episode": episode, "field": field, "component": component,
                          "start_frame": page_start, "end_frame": page_end,
                          "requested_frame_range": {"start": start, "end": end}, "requested_time_range": requested_time,
                          "values": values, "timestamp_ns": adapter.table(episode)["timestamp_ns"].slice(page_start, page_end - page_start).to_pylist(),
                          "truncated": more, "next_cursor": page_end - start if more else None,
                          "requested_limit": requested_limit, "returned_count": len(values),
                          "truncation_reasons": reasons + (["row_limit"] if more and not reasons else []),
                          "view_url": _view_url(view, settings)}
                scalar_count = (scalar_width + 1) * len(values)
                if scalar_count <= settings.max_query_scalars and len(json_bytes(result)) <= settings.max_query_bytes:
                    return result
                if page_end - page_start <= 1:
                    raise QueryLimitError("one row plus timestamps/evidence exceeds result budget; select a component or increase limits")
                if scalar_count > settings.max_query_scalars and "scalar_limit" not in reasons: reasons.append("scalar_limit")
                if len(json_bytes(result)) > settings.max_query_bytes and "byte_limit" not in reasons: reasons.append("byte_limit")
                page_end = page_start + max(1, (page_end - page_start) // 2)
        if end - start > settings.max_aggregate_rows:
            raise QueryLimitError("aggregate computation row limit exceeded; narrow range")
        values, width = _values(adapter, episode, field, start, end, component)
        catalog = {f.name: f for f in adapter.fields()}
        if component is None and getattr(catalog[field], "observed_length", None) is not None:
            raise QueryLimitError("vector aggregate requires component")
        arr = np.asarray(values, dtype=np.float64)
        evidence = _evidence(adapter, episode, field, component, start, end, requested_time, start, end, end - start, "aggregate_series")
        evidence.update(aggregation="count,min,max,mean,p50,p95", input_count=len(values), exact=True)
        view = _view_for(adapter, settings, episode, min(start, info.length - 1), field, component, evidence)
        stats = {key: float(fn(arr)) if arr.size else None for key, fn in (
            ("min", np.min), ("max", np.max), ("mean", np.mean),
            ("p50", lambda a: np.percentile(a, 50)), ("p95", lambda a: np.percentile(a, 95)))}
        return _bounded({"episode": episode, "field": field, "component": component,
            "start_frame": start, "end_frame": end,
            "input_range": {"start_frame": start, "end_frame": end, "requested_time_range": requested_time},
            "input_count": len(values), "count": int(arr.size), **stats,
            "exact": True, "truncated": False, "next_cursor": None, "view_url": _view_url(view, settings)}, settings)
    if name in {"compare_series", "audit_timing", "audit_integrity"}:
        from .audits import run_audit
        return _bounded(run_audit(name, args, adapter, settings),settings)
    context = validation_context(adapter, settings)
    if name == "encode_view":
        view = normalize_view(args["view"], **context)
    elif name == "decode_view":
        view = decode_view(args["url"], max_url_chars=settings.max_view_url_chars, **context)
    elif name == "render_view":
        if args.get("view") is not None and args.get("url") is not None:
            raise QueryLimitError("provide view OR url")
        view = decode_view(args["url"], max_url_chars=settings.max_view_url_chars, **context) if args.get("url") else normalize_view(args.get("view", {}), **context)
        if "width" in args or "height" in args:
            raw = view.to_dict()
            raw["render"] = {**raw["render"], **{k: args[k] for k in ("width", "height") if k in args}}
            view = normalize_view(raw, **context)
        result = renderer(adapter, view)
        if len(result.png) > settings.max_image_bytes:
            raise QueryLimitError("PNG exceeds max_image_bytes; request a smaller render size")
        import hashlib
        value = {"mime_type": "image/png",
                "manifest": result.manifest, "bytes": len(result.png), "sha256": hashlib.sha256(result.png).hexdigest(),
                "truncated": False, "view_url": _view_url(view, settings)}
        _bounded(value,settings)
        return {**value,"image_base64":base64.b64encode(result.png).decode("ascii")}
    return _bounded({"view": view.to_dict(), "view_url": _view_url(view, settings), "truncated": False}, settings)

