from __future__ import annotations

import json
import logging
import hashlib
import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .data.adapter import DatasetAdapter
from .data.errors import DataError, QueryLimitError, redact_message, tool_error
from .data.source import make_store
from .renderer import render_view
from .tools import call_tool, tool_definitions
from .viewstate import decode_view, normalize_view, resolve_frame, validation_context
from .web import INDEX_HTML
from .render_service import RenderService, ServiceBusy, SessionExpired
from .video import StaleGeneration
from .held_response import HeldResponse, HeldJSONResponse, HeldStreamResponse
from .resource_lifecycle import ResourceLifecycle


logger = logging.getLogger("lerobot-visualizer")


def _adapter(settings: Settings) -> DatasetAdapter:
    settings.validate_source()
    return DatasetAdapter(make_store(settings), settings.cache_dir)


def _error(exc: Exception, status_code: int = 400) -> JSONResponse:
    if isinstance(exc, ServiceBusy): status_code = 429
    elif isinstance(exc, StaleGeneration): status_code = 409
    elif isinstance(exc, SessionExpired): status_code = 410
    return JSONResponse({"error": {"type": type(exc).__name__, "code": type(exc).__name__, "message": redact_message(str(exc))}}, status_code=status_code)


def _origin_allowed(origin: str | None, settings: Settings) -> bool:
    if not origin:
        return True
    normalized = origin.rstrip("/")
    if normalized in settings.allowed_origins:
        return True
    parsed = urlparse(normalized)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _cors_headers(origin: str | None, settings: Settings) -> dict[str, str]:
    if origin and _origin_allowed(origin, settings):
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    return {}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    adapter: DatasetAdapter | None = None
    service = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal adapter, service
        try:
            adapter = _adapter(settings)
            service = RenderService(adapter, settings); app.state.render_service = service
            logger.info("dataset ready: episodes=%s", len(adapter.episodes))
        except Exception as exc:
            logger.warning("dataset unavailable: %s", type(exc).__name__)
        yield
        if service is not None: await asyncio.to_thread(service.close)

    app = FastAPI(title="LeRobot Visualizer", version="1.0", lifespan=lifespan)
    app.mount("/assets", StaticFiles(directory=Path(__file__).with_name("assets")), name="assets")

    @app.middleware("http")
    async def resource_boundary(request, call_next):
        if request.url.path.startswith(("/api/playback", "/render")):
            origin = request.headers.get("origin")
            if not _origin_allowed(origin, settings): return _error(ValueError("OriginNotAllowed"), 403)
            if request.method == "OPTIONS":
                return Response(status_code=204, headers={**_cors_headers(origin, settings), "Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": "content-type"})
            response = await call_next(request)
            response.headers.update(_cors_headers(origin, settings)); return response
        return await call_next(request)

    async def body_json(request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > settings.max_query_bytes: raise QueryLimitError("request JSON byte limit")
            raw.extend(chunk)
        return json.loads(raw)

    def state_from(body):
        if not isinstance(body, dict) or set(body) - {"url", "view"} or (("url" in body) == ("view" in body)):
            raise ValueError("provide exactly one of url or view")
        context = validation_context(get_adapter(), settings)
        return decode_view(body["url"], max_url_chars=settings.max_view_url_chars, **context) if "url" in body else normalize_view(body["view"], **context)

    async def work(fn, owner, cancelled, leases, request=None, bounded=True):
        dispatch = service.reserve_dispatch(owner) if bounded and service is not None else None
        if request is not None:
            request.state.render_service = service; request.state.render_owner = owner
            request.state.render_cancellation = cancelled; request.state.render_leases = leases
            request.state.render_dispatch = dispatch
        def execute():
            try: return fn()
            finally:
                if dispatch is not None: dispatch.finish(compute=True)
        try:
            task = asyncio.ensure_future(asyncio.get_running_loop().run_in_executor(service.executor if dispatch else None, execute))
        except BaseException:
            if dispatch is not None: dispatch.finish(compute=True,response=True)
            raise
        async def watch_disconnect():
            if request is None: return
            while not task.done():
                if await request.is_disconnected():
                    cancelled.set()
                    if service: service.cancel(owner)
                    return
                await asyncio.sleep(.05)
        watcher = asyncio.create_task(watch_disconnect())
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            if service: service.cancel(owner)
            def finish(t):
                try: t.result()
                except BaseException: pass
                for lease in leases: lease.close()
            task.add_done_callback(finish)
            raise
        finally: watcher.cancel()

    def get_adapter() -> DatasetAdapter:
        if adapter is None:
            raise HTTPException(status_code=503, detail="dataset is not configured or unavailable")
        return adapter

    @app.get("/health")
    def health():
        return {"ok": adapter is not None, "service": "lerobot-visualizer", "mcp": "/mcp", "view": "/view"}

    @app.get("/api/manifest")
    def manifest():
        try:
            return get_adapter().manifest()
        except Exception as exc:
            return _error(exc)

    @app.post("/api/view/normalize")
    async def normalize_view_endpoint(request: Request):
        try:
            current = get_adapter()
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"url", "view"}:
                raise ValueError("request must contain only url or view")
            if ("url" in body) == ("view" in body):
                raise ValueError("provide exactly one of url or view")
            context = validation_context(current, settings)
            if "url" in body:
                state = decode_view(body["url"], max_url_chars=settings.max_view_url_chars, **context)
            else:
                state = normalize_view(body["view"], **context)
            from .tools import _view_url
            return {"view": state.to_dict(), "view_url": _view_url(state, settings), "manifest": current.manifest(), "resolved_frame": resolve_frame(state, current), "truncated": False}
        except Exception as exc:
            return _error(exc)

    @app.get("/")
    def root():
        return HTMLResponse(INDEX_HTML)

    @app.get("/view")
    def view():
        return HTMLResponse(INDEX_HTML)

    @app.get("/render")
    async def render(request: Request, state: str | None = Query(default=None)):
        leases = []; cancelled = threading.Event()
        try:
            current = get_adapter()
            context = validation_context(current, settings)
            if state:
                current_state = decode_view(state, max_url_chars=settings.max_view_url_chars, **context)
            else:
                current_state = normalize_view({}, **context)
            owner = f"http-{id(cancelled)}"
            def job():
                lease = service.render(current_state, owner=owner, valid=lambda: not cancelled.is_set()); leases.append(lease); return lease
            result = await work(job, owner, cancelled, leases, request)
            if len(result.png) > settings.max_image_bytes:
                for lease in leases: lease.close()
                return _error(QueryLimitError("PNG exceeds max_image_bytes; request a smaller render size"), 413)
            headers = {"X-Render-Manifest": json.dumps(result.manifest, separators=(",", ":")), "X-Render-Bytes": str(len(result.png)), "X-Render-SHA256": hashlib.sha256(result.png).hexdigest()}
            return HeldResponse(result.png, media_type="image/png", headers=headers, leases=leases)
        except Exception as exc:
            for lease in leases: lease.close()
            return _error(exc, 413 if isinstance(exc, QueryLimitError) else 400)

    @app.get("/render/manifest")
    async def render_manifest(request: Request, state: str | None = Query(default=None)):
        leases = []; cancelled = threading.Event()
        try:
            current = get_adapter()
            context = validation_context(current, settings)
            current_state = decode_view(state or "", max_url_chars=settings.max_view_url_chars, **context)
            owner = f"manifest-{id(cancelled)}"
            def job():
                lease = service.render(current_state, owner=owner, valid=lambda: not cancelled.is_set()); leases.append(lease); return lease
            result = await work(job, owner, cancelled, leases, request)
            if len(result.png) > settings.max_image_bytes:
                for lease in leases: lease.close()
                return _error(QueryLimitError("PNG exceeds max_image_bytes; request a smaller render size"), 413)
            return HeldJSONResponse({"manifest": result.manifest}, leases=leases)
        except Exception as exc:
            for lease in leases: lease.close()
            return _error(exc, 413 if isinstance(exc, QueryLimitError) else 400)

    @app.post("/api/playback/prepare")
    async def playback_prepare(request: Request):
        cancelled = threading.Event(); owner = f"prepare-{id(cancelled)}"; created = []
        try:
            state = state_from(await body_json(request))
            def job():
                result = service.prepare(state, valid=lambda: not cancelled.is_set(), owner=owner); created.append(result)
                if cancelled.is_set(): service.generation(result["session"], 1, release=True)
                return result
            return await work(job, owner, cancelled, [], request)
        except Exception as exc: return _error(exc, 413 if isinstance(exc, QueryLimitError) else 400)

    @app.get("/api/playback/{sid}/timeline")
    async def playback_timeline(sid: str, cursor: int = 0, limit: int = 512):
        try: get_adapter(); return service.timeline(sid, cursor, limit)
        except Exception as exc: return _error(exc, 413 if isinstance(exc, QueryLimitError) else 400)

    @app.post("/api/playback/{sid}/generation")
    async def playback_generation(sid: str, request: Request):
        try:
            get_adapter(); body = await body_json(request)
            if not isinstance(body, dict) or set(body) - {"generation", "release"} or type(body.get("release", False)) is not bool: raise ValueError("invalid generation control")
            return service.generation(sid, body["generation"], body.get("release", False))
        except Exception as exc: return _error(exc)

    @app.get("/api/playback/{sid}/frame")
    async def playback_frame(sid: str, request: Request, frame: int, generation: int):
        leases = []; cancelled = threading.Event(); owner = f"frame-{id(cancelled)}"
        try:
            get_adapter(); session = service.get_session(sid)
            def job():
                lease = service.render(session.state, sid=sid, frame=frame, generation=generation, owner=owner, valid=lambda: not cancelled.is_set()); leases.append(lease); return lease
            result = await work(job, owner, cancelled, leases, request); parts = service.envelope(result)
            async def chunks():
                for part in parts: yield part
            return HeldStreamResponse(chunks(), media_type="application/vnd.lerobot.frame+bin", leases=leases,
                                      headers={"Cache-Control": "no-store", "Content-Length": str(sum(map(len, parts)))})
        except Exception as exc:
            for lease in leases: lease.close()
            return _error(exc, 413 if isinstance(exc, QueryLimitError) else 400)

    @app.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
    async def mcp(request: Request):
        leases = []; cancelled = threading.Event(); owner = f"mcp-{id(cancelled)}"
        origin = request.headers.get("origin")
        if not _origin_allowed(origin, settings):
            return JSONResponse({"error": {"type": "OriginNotAllowed", "message": "Origin is not allowed"}}, status_code=403)
        if request.method == "OPTIONS":
            headers = {"Access-Control-Allow-Headers": "content-type,mcp-session-id,mcp-protocol-version", "Access-Control-Allow-Methods": "GET,POST,OPTIONS"}
            headers.update(_cors_headers(origin, settings))
            return Response(status_code=204, headers=headers)
        if request.method == "GET":
            return Response(status_code=405, headers={"Allow": "POST,OPTIONS", **_cors_headers(origin, settings)})
        try:
            message = await body_json(request)
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "lerobot-visualizer", "version": "1.0"}}
            elif method in {"notifications/initialized", "notifications/cancelled"}:
                return Response(status_code=202, headers=_cors_headers(origin, settings))
            elif method == "tools/list":
                result = {"tools": tool_definitions(settings)}
            elif method == "tools/call":
                params = message.get("params", {})
                name, args = params.get("name"), params.get("arguments", {})
                def tracked_renderer(adapter, state):
                    lease = service.render(state, owner=owner, valid=lambda: not cancelled.is_set()); leases.append(lease)
                    return lease
                def invoke():
                    fn = lambda: call_tool(name,args,get_adapter(),settings,tracked_renderer)
                    if name in {"compare_series","audit_timing","audit_integrity"}:
                        if service is None: return fn()
                        return service.analysis(fn,owner,lambda:not cancelled.is_set())
                    return fn()
                try:
                    value = await work(invoke, owner, cancelled, leases, request, bounded=name in {"render_view","compare_series","audit_timing","audit_integrity"})
                except Exception as exc:
                    for lease in leases: lease.close()
                    result={"content":[{"type":"text","text":json.dumps(tool_error(exc,min(512,settings.max_query_bytes)),ensure_ascii=False,separators=(",",":"))}],"isError":True}
                    return HeldJSONResponse({"jsonrpc":"2.0","id":request_id,"result":result},headers={"MCP-Protocol-Version":"2025-03-26",**_cors_headers(origin,settings)},leases=leases)
                if name == "render_view" and "image_base64" in value:
                    image = value["image_base64"]
                    structured = {k: v for k, v in value.items() if k != "image_base64"}
                    result = {"content": [{"type": "image", "data": image, "mimeType": "image/png"}, {"type": "text", "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":"))}], "structuredContent": structured, "isError": False}
                else:
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}], "structuredContent": value, "isError": False}
            else:
                raise ValueError(f"unsupported MCP method: {method}")
            headers = {"MCP-Protocol-Version": "2025-03-26"}
            headers.update(_cors_headers(origin, settings))
            return HeldJSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result}, headers=headers, leases=leases)
        except Exception as exc:
            for lease in leases: lease.close()
            logger.warning("mcp error method=%s type=%s", locals().get("method"), type(exc).__name__)
            headers = {"MCP-Protocol-Version": "2025-03-26"}
            headers.update(_cors_headers(origin, settings))
            return JSONResponse({"jsonrpc": "2.0", "id": locals().get("request_id"), "error": {"code": -32000, "message": redact_message(str(exc))}}, status_code=200, headers=headers)

    app.add_middleware(ResourceLifecycle)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)

