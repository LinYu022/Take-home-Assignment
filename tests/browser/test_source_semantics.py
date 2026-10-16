"""Source/cache associations and field meaning through real browser/HTTP paths."""
import base64
import hashlib
import io
from urllib.parse import urlparse

import av
from PIL import Image
from playwright.sync_api import expect

from tests.browser.conftest import _run_service, _write_dataset


def tool(page, url, name, arguments):
    response = page.request.post(url + "/mcp", data={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": arguments}})
    assert response.ok
    result = response.json()
    assert "error" not in result
    return result["result"]


def test_browser_and_mcp_follow_new_source_with_shared_video_cache(page, tmp_path, record_property):
    cache = tmp_path / "shared-cache"
    pixels = []
    for name, shift, offset in (("a", 0, 0.), ("b", 90, .5)):
        root = tmp_path / name
        _write_dataset(root, frame_count=4, video_shift=shift, position_offset=offset)
        original = root / "videos/base_0_camera/rgb/image/chunk-000/file-000.mp4"
        with av.open(str(original)) as container:
            reference = next(container.decode(video=0)).to_ndarray(format="rgb24")
        with _run_service(root, cache) as service:
            state = page.request.post(service.url + "/api/view/normalize", data={"view": {
                "panels": ["video"], "series": [], "overlays": [], "display": {"info_bar": False, "locale": "en"}}}).json()
            page.goto(state["view_url"])
            expect(page.locator("#render")).to_be_visible(timeout=20000)
            actual = page.evaluate("({view:visualizer.snapshot.view,hash:visualizer.snapshot.sha256,manifest:visualizer.snapshot.manifest})")
            png = page.request.get(service.url + "/render?" + urlparse(page.url).query).body()
            rendered = tool(page, service.url, "render_view", {"view": actual["view"]})
            assert png == base64.b64decode(rendered["content"][0]["data"])
            assert hashlib.sha256(png).hexdigest() == actual["hash"]
            x, y, w, h = actual["manifest"]["video_rect"]
            pixel = Image.open(io.BytesIO(png)).getpixel((x + w // 2, y + h // 2))
            assert pixel == tuple(map(int, reference[24, 32]))
            values = tool(page, service.url, "query_series", {"episode": 0, "field": "base_0_camera/position",
                          "component": 1, "start_frame": 0, "end_frame": 1, "limit": 1})["structuredContent"]["values"]
            assert values == [offset]
            pixels.append(pixel)
            record_property("source_" + name + "_rgb", str(pixel))
    assert pixels[0] != pixels[1]


def test_real_catalog_and_shared_png_retain_original_field_semantics(page, real_service, tmp_path, record_property):
    service = real_service
    source = service.app.state.render_service.adapter.info["features"]
    public = page.request.get(service.url + "/api/manifest").json()
    described = tool(page, service.url, "describe_dataset", {})["structuredContent"]
    assert public["fields"] == described["manifest"]["fields"]
    fields = {f["name"]: f for f in public["fields"]}
    for name, metadata in source.items():
        if name not in fields or not isinstance(metadata, dict): continue
        assert fields[name]["description"] == metadata.get("description")
        assert fields[name]["frame"] == metadata.get("frame")
    for name in ("observation.left_hand_mano", "observation.right_hand_mano"):
        assert fields[name]["frame"] == "camera" and fields[name]["observed_length"] == 61
        assert "global_orient_axis_angle(3) + hand_pose_axis_angle(45) + betas(10) + transl(3)" in fields[name]["description"]
        assert fields[name]["units"] is None
    assert fields["base_0_camera/position"]["units"] == "m"
    assert fields["base_0_camera/position"]["units_source"] == "description"
    selected = page.request.post(service.url + "/api/view/normalize", data={"view": {"cursor": {"frame": 1514}}}).json()
    page.goto(selected["view_url"])
    expect(page.locator("#render")).to_be_visible(timeout=20000)
    actual = page.evaluate("({view:visualizer.snapshot.view,hash:visualizer.snapshot.sha256,manifest:visualizer.snapshot.manifest})")
    assert actual["manifest"]["series"][1]["field"] == "base_0_camera/position"
    assert actual["manifest"]["series"][1]["unit"] == "m"
    png = page.request.get(service.url + "/render?" + urlparse(page.url).query).body()
    rendered = tool(page, service.url, "render_view", {"view": actual["view"]})
    assert png == base64.b64decode(rendered["content"][0]["data"])
    browser_hash = page.evaluate("async()=>{const data=await visualizer.snapshot.blob.arrayBuffer();return [...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(v=>v.toString(16).padStart(2,'0')).join('')}")
    assert browser_hash == hashlib.sha256(png).hexdigest() == actual["hash"]
    (tmp_path / "real-field-units.png").write_bytes(png)
    record_property("real_png_sha256", actual["hash"])
    record_property("real_png_bytes", len(png))
