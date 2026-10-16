import base64
import hashlib
import json
from dataclasses import replace
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient

from app.main import create_app


def test_real_http_png_matches_mcp_image_and_headers(live_service):
    with httpx.Client(base_url=live_service.url, trust_env=False) as client:
        state = client.post("/api/view/normalize", json={"view": {}}).json()
        query = urlparse(state["view_url"]).query
        browser = client.get("/render?" + query)
        assert browser.status_code == 200 and browser.headers["content-type"] == "image/png"
        assert len(browser.content) == int(browser.headers["x-render-bytes"])
        assert hashlib.sha256(browser.content).hexdigest() == browser.headers["x-render-sha256"]
        result = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "render_view", "arguments": {"view": state["view"]}}}).json()["result"]
        image = result["content"][0]
        assert image["type"] == "image" and image["mimeType"] == "image/png"
        assert base64.b64decode(image["data"]) == browser.content
        assert "image_base64" not in result["structuredContent"]


def test_real_png_limit_rejects_browser_manifest_and_mcp(live_service):
    tiny = replace(live_service.settings, max_image_bytes=1)
    with TestClient(create_app(tiny)) as client:
        view = client.post("/api/view/normalize", json={"view": {}}).json()
        query = urlparse(view["view_url"]).query
        for route in ("/render?", "/render/manifest?"):
            rejected = client.get(route + query)
            assert rejected.status_code == 413
            assert rejected.json()["error"]["type"] == "QueryLimitError"
            assert "max_image_bytes" in rejected.json()["error"]["message"]
            assert rejected.headers["content-type"] == "application/json"
        mcp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "render_view", "arguments": {"view": view["view"]}}}).json()
        assert mcp["result"]["isError"]
        assert "max_image_bytes" in json.loads(mcp["result"]["content"][0]["text"])["error"]["constraint"]
