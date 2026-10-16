"""The same evidence/navigation regressions run on generated and real snapshots."""
import base64
import hashlib
import json
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from tests.browser.test_visualizer import canonical, image_manifest, normalized_url


FIELD = "action.left_hand_tracks"


def tool(page, service, name, arguments):
    response = page.request.post(service.url + "/mcp", data={"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert response.ok, response.text()
    body = response.json()
    assert "error" not in body, body
    return body["result"]


def data(page, service, name, arguments):
    return tool(page, service, name, arguments)["structuredContent"]


def assert_window(page, expected, source):
    manifest = image_manifest(page)
    assert (manifest["range_start_frame"], manifest["range_end_frame"]) == expected
    assert manifest["range_source"] == source
    assert manifest["range_expanded"] is False
    for series in manifest["series"]:
        assert (series["start_frame"], series["end_frame"]) == expected
    return manifest


def assert_render_channels(page, service):
    query = urlparse(page.url).query
    png = page.request.get(service.url + "/render?" + query)
    manifest = page.request.get(service.url + "/render/manifest?" + query)
    assert png.ok and manifest.ok
    rendered = tool(page, service, "render_view", {"url": page.url})
    assert base64.b64decode(rendered["content"][0]["data"]) == png.body()
    sha = hashlib.sha256(png.body()).hexdigest()
    assert sha == png.headers["x-render-sha256"] == page.locator("#render").get_attribute("data-sha256")
    assert manifest.json()["manifest"] == image_manifest(page) == rendered["structuredContent"]["manifest"]
    displayed_sha = page.evaluate("""async () => {
        const bytes=await window.visualizer.snapshot.blob.arrayBuffer();
        return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
    }""")
    assert displayed_sha == sha


def times(page, service):
    return data(page, service, "query_series", {"episode": 0, "field": "timestamp_ns",
                "start_frame": service.anchor, "end_frame": service.anchor + 31, "limit": 31})["values"]


def test_query_first_page_continuations_and_aggregate_exact_windows(page, evidence_service):
    service = evidence_service
    start = service.anchor
    args = {"episode": 0, "field": FIELD, "component": 0,
            "start_frame": start, "end_frame": start + 30, "limit": 10}
    for page_no in range(3):
        result = data(page, service, "query_series", args)
        page.goto(result["view_url"])
        expected = (start + page_no * 10, start + (page_no + 1) * 10)
        manifest = assert_window(page, expected, "query_page")
        assert canonical(page)["evidence"]["returned_frame_range"] == {"start": expected[0], "end": expected[1]}
        assert manifest["series"][0]["field"] == FIELD + "[0]"
        assert manifest["series"][0]["min"] == min(result["values"])
        assert manifest["series"][0]["max"] == max(result["values"])
        assert result["truncated"] is (page_no < 2)
        assert_render_channels(page, service)
        if result["next_cursor"] is not None:
            args["cursor"] = result["next_cursor"]
        else:
            assert page_no == 2
    args.pop("limit")
    args.pop("cursor")
    result = data(page, service, "aggregate_series", args)
    page.goto(result["view_url"])
    manifest = assert_window(page, (start, start + 30), "aggregate_input")
    assert result["input_count"] == 30 and result["exact"] is True
    assert manifest["series"][0]["min"] == result["min"]
    assert manifest["series"][0]["max"] == result["max"]
    assert_render_channels(page, service)
    shared = canonical(page)
    page.reload()
    assert_window(page, (start, start + 30), "aggregate_input")
    assert canonical(page) == shared


def test_timestamp_query_single_point_and_hidden_series_keep_evidence_window(page, evidence_service):
    service = evidence_service
    timestamps = times(page, service)
    args = {"episode": 0, "field": FIELD, "component": 1,
            "start_ns": timestamps[0] + 1, "end_ns": timestamps[30], "limit": 1}
    result = data(page, service, "query_series", args)
    page.goto(result["view_url"])
    expected = (service.anchor + 1, service.anchor + 2)
    manifest = assert_window(page, expected, "query_page")
    assert len(manifest["series"]) == 1
    assert manifest["series"][0]["min"] == manifest["series"][0]["max"] == result["values"][0]
    assert canonical(page)["evidence"]["requested_time_range"] == {"start": args["start_ns"], "end": args["end_ns"]}
    sha = page.locator("#render").get_attribute("data-sha256")
    page.locator("#panelSeries").uncheck()
    page.wait_for_function("!document.querySelector('#render').hidden && !JSON.parse(document.querySelector('#render').dataset.manifest).series_rendered")
    assert assert_window(page, expected, "query_page")["series"] == []
    assert page.locator("#render").get_attribute("data-sha256") != sha
    assert canonical(page)["evidence"] is not None
    assert_render_channels(page, service)


def test_normalized_frame_and_default_window_after_evidence_is_removed(page, evidence_service):
    service = evidence_service
    timestamps = times(page, service)
    response = page.request.post(service.url + "/api/view/normalize", data={"view": {
        "cursor": None, "time_range": {"start_ns": timestamps[0] + 1, "end_ns": timestamps[30]}}})
    body = response.json()
    assert response.ok
    assert body["resolved_frame"] == service.anchor + 1
    assert "resolved_frame" not in body["view"]
    page.goto(body["view_url"])
    assert_window(page, (service.anchor + 1, service.anchor + 30), "time_range")
    expect(page.locator("#frame")).to_have_value(str(service.anchor + 1))
    page.goto(normalized_url(page, service.url, {"cursor": {"frame": service.anchor}}))
    assert_window(page, (max(0, service.anchor - 240), min(service.length, service.anchor + 241)), "cursor_window")


@pytest.mark.parametrize("direction,delta", [("prev", -1), ("next", 1)])
def test_time_range_step_uses_resolved_frame_and_clears_evidence(page, evidence_service, direction, delta):
    service = evidence_service
    timestamps = times(page, service)
    query = data(page, service, "query_series", {"episode": 0, "field": FIELD, "component": 0,
                 "start_frame": service.anchor, "end_frame": service.anchor + 30, "limit": 10})
    view = data(page, service, "decode_view", {"url": query["view_url"]})["view"]
    view.update(cursor=None, time_range={"start_ns": timestamps[0], "end_ns": timestamps[30]})
    page.goto(normalized_url(page, service.url, view))
    assert_window(page, (service.anchor, service.anchor + 10), "query_page")
    expect(page.locator("#frame")).to_have_value(str(service.anchor))
    page.locator("#" + direction).click()
    target = service.anchor + delta
    page.wait_for_function("frame => !document.querySelector('#render').hidden && JSON.parse(document.querySelector('#render').dataset.manifest).frame===frame", arg=target)
    final = canonical(page)
    assert final["cursor"] == {"frame": target}
    assert final["time_range"] is None and final["evidence"] is None
    for key in ("series", "panels", "overlays"):
        assert final[key] == view[key]
    assert_window(page, (max(0, target - 240), min(service.length, target + 241)), "cursor_window")


def test_time_range_navigation_boundaries(page, evidence_service):
    service = evidence_service
    page.goto(service.url + "/view")
    expect(page.locator("#render")).to_be_visible()
    expect(page.locator("#prev")).to_be_disabled()
    expect(page.locator("#next")).to_be_enabled()
    last = data(page, service, "query_series", {"episode": 0, "field": "timestamp_ns",
                "start_frame": service.length - 1, "end_frame": service.length, "limit": 1})["values"][0]
    end = data(page, service, "describe_dataset", {})["manifest"]["episodes"][0]["data_end_ns"]
    page.goto(normalized_url(page, service.url, {"cursor": None, "time_range": {"start_ns": last, "end_ns": end}}))
    # PyAV currently decodes from the first frame; a real final frame can exceed
    # Playwright's default 5-second assertion budget on a busy test host.
    expect(page.locator("#render")).to_be_visible(timeout=20_000)
    expect(page.locator("#frame")).to_have_value(str(service.length - 1))
    expect(page.locator("#next")).to_be_disabled()
    expect(page.locator("#prev")).to_be_enabled()


def test_pending_time_resolution_disables_steps_and_failure_restores_navigation(page, evidence_service):
    service = evidence_service
    timestamps = times(page, service)
    page.goto(service.url + "/view")
    expect(page.locator("#render")).to_be_visible()
    good_url = page.url
    pending = []
    def hold_range(route):
        pending.append(route)
        page.evaluate("window.__rangePending = true")
    page.route("**/api/view/normalize", hold_range)
    page.locator("#startNs").fill(str(timestamps[0]))
    page.locator("#endNs").fill(str(timestamps[30]))
    page.locator("#applyRange").click()
    page.wait_for_function("window.__rangePending === true")
    expect(page.locator("#prev")).to_be_disabled()
    expect(page.locator("#next")).to_be_disabled()
    pending[0].fulfill(status=400, content_type="application/json", body=json.dumps({"error": {"message": "rejected range"}}))
    page.unroute("**/api/view/normalize", hold_range)
    expect(page.locator("#error")).to_contain_text("rejected range")
    expect(page.locator("#frame")).to_have_value("0")
    assert page.url == good_url
    expect(page.locator("#prev")).to_be_disabled()
    expect(page.locator("#next")).to_be_enabled()
    page.locator("#retry").click()
    expect(page.locator("#render")).to_be_visible()


def test_rapid_steps_from_time_range_keep_latest_pending_cursor(page, evidence_service):
    service = evidence_service
    timestamps = times(page, service)
    page.goto(normalized_url(page, service.url, {"cursor": None, "time_range": {
        "start_ns": timestamps[0], "end_ns": timestamps[30]}}))
    expect(page.locator("#render")).to_be_visible()
    pending = []
    def hold_first_step(route):
        response = route.fetch()
        frame = route.request.post_data_json["view"]["cursor"]["frame"]
        if frame == service.anchor + 1:
            pending.append((route, response))
            page.evaluate("window.__stepPending = true")
        else:
            route.fulfill(response=response)
    page.route("**/api/view/normalize", hold_first_step)
    page.locator("#next").click()
    page.wait_for_function("window.__stepPending === true")
    page.locator("#next").click()
    target = service.anchor + 2
    page.wait_for_function("frame => !document.querySelector('#render').hidden && JSON.parse(document.querySelector('#render').dataset.manifest).frame===frame", arg=target)
    for route, response in pending:
        route.fulfill(response=response)
    page.unroute("**/api/view/normalize", hold_first_step)
    page.wait_for_load_state("networkidle")
    expect(page.locator("#frame")).to_have_value(str(target))
    assert canonical(page)["cursor"] == {"frame": target}
    assert image_manifest(page)["frame"] == target
