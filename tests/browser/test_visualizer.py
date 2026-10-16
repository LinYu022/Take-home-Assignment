import base64
import json

import pytest
from playwright.sync_api import expect


DEFAULT_SERIES = ["action.left_hand_tracks", "base_0_camera/position"]


def canonical(page):
    response = page.request.post(
        page.url.split("/view", 1)[0] + "/api/view/normalize", data={"url": page.url})
    assert response.ok, response.text()
    return response.json()["view"]


def image_manifest(page):
    # Real snapshots may build a cold PTS index here; this is not a playback
    # performance assertion (those keep their separate measured thresholds).
    expect(page.locator("#render")).to_be_visible(timeout=20000)
    return json.loads(page.locator("#render").get_attribute("data-manifest"))


def edit_frame(page, frame):
    page.locator("#frame").fill(str(frame))
    page.locator("#frame").press("Tab")
    expect(page.locator("#status")).to_contain_text("frame " + str(frame))
    expect(page.locator("#render")).to_be_visible()


def normalized_url(page, url, view):
    response = page.request.post(url + "/api/view/normalize", data={"view": view})
    assert response.ok, response.text()
    return response.json()["view_url"]


def evidence_url(page, url):
    response = page.request.post(url + "/mcp", data={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "query_series", "arguments": {
                                     "episode": 0, "field": "action.left_hand_tracks", "component": 0,
                                     "start_frame": 2, "end_frame": 6, "limit": 4}}})
    assert response.ok
    return response.json()["result"]["structuredContent"]["view_url"]


def test_home_defaults_whole_vectors_and_explicit_empty_series(visualizer, live_service):
    page = visualizer
    view = canonical(page)
    assert view["series"] == DEFAULT_SERIES
    assert sorted(page.locator("#series").evaluate("el => [...el.selectedOptions].map(o=>o.value)")) == DEFAULT_SERIES
    assert page.locator("#series option[value='base_0_camera/position[0]']").count() == 1
    manifest = image_manifest(page)
    assert [s["field"] for s in manifest["series"]] == DEFAULT_SERIES
    position = next(s for s in manifest["series"] if s["field"] == "base_0_camera/position")
    assert position["max"] < .1  # meters, never nanoseconds mixed into the mean
    url = normalized_url(page, live_service.url, {"series": []})
    page.goto(url)
    expect(page.locator("#render")).to_be_visible()
    assert canonical(page)["series"] == []
    assert page.locator("#series").evaluate("el => el.selectedOptions.length") == 0
    assert image_manifest(page)["series"] == []
    fields = ["timestamp", "timestamp_ns", "frame_index", "episode_index", "index", "task_index", *DEFAULT_SERIES]
    page.locator("#series").select_option(fields)
    page.wait_for_function("!document.querySelector('#render').hidden && JSON.parse(document.querySelector('#render').dataset.manifest).series.length===8")
    assert [s["field"] for s in image_manifest(page)["series"]] == sorted(fields)


@pytest.mark.parametrize("edit", ["frame", "time_range", "series", "episode"])
def test_query_evidence_is_cleared_only_for_data_selection_edits(visualizer, live_service, edit):
    page = visualizer
    page.goto(evidence_url(page, live_service.url))
    expect(page.locator("#render")).to_be_visible()
    assert canonical(page)["evidence"]["requested_frame_range"] == {"start": 2, "end": 6}
    if edit == "frame":
        edit_frame(page, 15)
    elif edit == "time_range":
        page.locator("#startNs").fill("200000000")
        page.locator("#endNs").fill("3200000000")
        page.locator("#applyRange").click()
        expect(page.locator("#status")).to_contain_text("time 200000000–3200000000")
    elif edit == "series":
        page.locator("#series").select_option(["base_0_camera/position[1]"])
        expect(page.locator("#series")).to_have_values(["base_0_camera/position[1]"])
    else:
        page.locator("#episode").select_option("1")
        expect(page.locator("#status")).to_contain_text("episode 1")
    expect(page.locator("#render")).to_be_visible()
    assert canonical(page)["evidence"] is None
    expect(page.locator("#error")).to_be_empty()


def test_panels_overlay_preserve_evidence_and_change_actual_pixels(visualizer, live_service):
    page = visualizer
    page.goto(evidence_url(page, live_service.url))
    original = canonical(page)
    before = image_manifest(page)
    sha = page.locator("#render").get_attribute("data-sha256")
    page.locator("#panelVideo").uncheck()
    page.wait_for_function("!document.querySelector('#render').hidden && !JSON.parse(document.querySelector('#render').dataset.manifest).video_rendered")
    after = image_manifest(page)
    assert after["video_rendered"] is False and after["overlay"]["projected_points"] == 0
    assert page.locator("#render").get_attribute("data-sha256") != sha
    assert canonical(page)["evidence"] == original["evidence"]
    assert canonical(page)["time_range"] == original["time_range"]
    page.locator("#panelMetadata").uncheck()
    page.wait_for_function("!document.querySelector('#render').hidden && !JSON.parse(document.querySelector('#render').dataset.manifest).metadata_rendered")
    assert image_manifest(page)["metadata_rendered"] is False
    page.locator("#panelSeries").uncheck()
    page.wait_for_function("!document.querySelector('#render').hidden && !JSON.parse(document.querySelector('#render').dataset.manifest).series_rendered")
    assert image_manifest(page)["series"] == []
    assert canonical(page)["series"] == original["series"]
    # With every panel disabled, no border, label or curve remains on the canvas.
    rgb = page.evaluate("async () => {const i=document.querySelector('#render');return [...new Set(i.getContext('2d').getImageData(0,0,i.width,i.height).data)]}")
    assert sorted(rgb) == [20, 23, 29, 255]
    page.locator("#left").uncheck()
    expect(page.locator("#render")).to_be_visible()
    page.locator("#right").uncheck()
    page.wait_for_function("document.querySelector('#render').hidden === false && !document.querySelector('#right').checked && !document.querySelector('#left').checked")
    assert canonical(page)["overlays"] == []
    assert canonical(page)["evidence"] == original["evidence"]


def test_share_url_restores_time_range_components_panels_and_overlay(visualizer, live_service, new_context):
    page = visualizer
    page.locator("#series").select_option(["base_0_camera/position", "action.left_hand_tracks[0]"])
    expect(page.locator("#render")).to_be_visible()
    page.locator("#startNs").fill("200000000")
    page.locator("#endNs").fill("3200000000")
    page.locator("#applyRange").click()
    page.wait_for_function("!document.querySelector('#render').hidden && JSON.parse(document.querySelector('#render').dataset.manifest).time_range?.end_ns===3200000000")
    assert image_manifest(page)["range_start_frame"] == 2
    assert image_manifest(page)["range_end_frame"] == 32
    page.locator("#panelMetadata").uncheck()
    page.wait_for_function("!document.querySelector('#render').hidden && !JSON.parse(document.querySelector('#render').dataset.manifest).metadata_rendered")
    page.locator("#right").uncheck()
    page.wait_for_function("!document.querySelector('#render').hidden && JSON.parse(document.querySelector('#render').dataset.manifest).overlay.projected_points===21")
    state = canonical(page)
    second = new_context().new_page()
    second.goto(page.url)
    expect(second.locator("#render")).to_be_visible()
    assert canonical(second) == state
    assert image_manifest(second) == image_manifest(page)
    assert second.locator("#render").get_attribute("data-sha256") == page.locator("#render").get_attribute("data-sha256")
    expect(second.locator("#startNs")).to_have_value("200000000")
    expect(second.locator("#endNs")).to_have_value("3200000000")
    expect(second.locator("#panelMetadata")).not_to_be_checked()
    expect(second.locator("#right")).not_to_be_checked()


def test_invalid_url_and_invalid_edits_hide_image_and_restore_controls(visualizer, live_service):
    page = visualizer
    render_requests = []
    page.on("request", lambda request: render_requests.append(request.url) if "/render?" in request.url else None)
    page.goto(live_service.url + "/view?state=invalid")
    expect(page.locator("#error")).to_contain_text("HTTP 400")
    expect(page.locator("#render")).to_be_hidden()
    assert render_requests == []
    page.locator("#reset").click()
    expect(page.locator("#render")).to_be_visible()
    good_url = page.url
    page.locator("#frame").fill("999")
    page.locator("#frame").press("Tab")
    expect(page.locator("#error")).to_contain_text("frame is out of range")
    expect(page.locator("#render")).to_be_hidden()
    expect(page.locator("#frame")).to_have_value("0")
    assert page.url == good_url
    page.locator("#retry").click()
    expect(page.locator("#render")).to_be_visible()
    page.locator("#series").select_option(["action.left_hand_tracks[" + str(i) + "]" for i in range(9)])
    expect(page.locator("#error")).to_contain_text("limited to 8")
    assert canonical(page)["series"] == DEFAULT_SERIES
    expect(page.locator("#render")).to_be_hidden()
    page.locator("#startNs").fill("0")
    page.locator("#endNs").fill("5000000000")
    page.locator("#applyRange").click()
    expect(page.locator("#error")).to_contain_text("time_range must be within")
    expect(page.locator("#startNs")).to_have_value("")


@pytest.mark.parametrize("failure,expected", [(413, "HTTP 413"), (503, "HTTP 503"),
                                               ("network", "Failed to fetch"), ("corrupt", "could not load")])
def test_render_failures_are_visible_and_retryable(visualizer, failure, expected):
    page = visualizer
    def fail(route):
        if failure == "network":
            route.abort()
        elif failure == "corrupt":
            route.fulfill(status=200, content_type="image/png", body=b"broken PNG")
        else:
            route.fulfill(status=failure, content_type="application/json", body=json.dumps({"error": {"message": "simulated failure"}}))
    page.route("**/render?**", fail)
    page.locator("#next").click()
    expect(page.locator("#error")).to_contain_text(expected)
    expect(page.locator("#render")).to_be_hidden()
    page.unroute("**/render?**", fail)
    page.locator("#retry").click()
    expect(page.locator("#render")).to_be_visible()
    assert canonical(page)["cursor"]["frame"] == 1


@pytest.mark.parametrize("phase", ["normalize", "render"])
def test_delayed_validation_and_render_cannot_overwrite_latest_state(visualizer, phase):
    page = visualizer
    # Delay the first real normalization response. The newer response is allowed to finish first.
    pending = []
    def delay_response(route):
        response = route.fetch()
        frame = (route.request.post_data_json.get("view", {}).get("cursor", {}).get("frame")
                 if phase == "normalize" else json.loads(response.headers["x-render-manifest"])["frame"])
        if frame == 1:
            pending.append((route, response))
            page.evaluate("window.__testBarrier = true")
        else:
            route.fulfill(response=response)
    pattern = "**/api/view/normalize" if phase == "normalize" else "**/render?**"
    page.route(pattern, delay_response)
    page.locator("#frame").fill("1")
    page.locator("#frame").press("Tab")
    page.wait_for_function("window.__testBarrier === true")
    page.locator("#frame").fill("2")
    page.locator("#frame").press("Tab")
    page.wait_for_function("!document.querySelector('#render').hidden && JSON.parse(document.querySelector('#render').dataset.manifest).frame===2")
    assert pending
    for route, response in pending:
        route.fulfill(response=response)
    page.unroute(pattern, delay_response)
    page.wait_for_load_state("networkidle")
    expect(page.locator("#frame")).to_have_value("2")
    assert canonical(page)["cursor"]["frame"] == 2
    assert image_manifest(page)["frame"] == 2


def test_rapid_different_controls_preserve_pending_selection(visualizer):
    page = visualizer
    pending = []
    def delay_selection(route):
        response = route.fetch()
        frame = route.request.post_data_json["view"]["cursor"]["frame"]
        if frame == 0:
            pending.append((route, response))
            page.evaluate("window.__testBarrier = true")
        else:
            route.fulfill(response=response)
    page.route("**/api/view/normalize", delay_selection)
    page.locator("#series").select_option(["action.left_hand_tracks[1]"])
    page.wait_for_function("window.__testBarrier === true")
    edit_frame(page, 3)
    for route, response in pending:
        route.fulfill(response=response)
    page.unroute("**/api/view/normalize", delay_selection)
    page.wait_for_load_state("networkidle")
    state = canonical(page)
    assert state["cursor"] == {"frame": 3}
    assert state["series"] == ["action.left_hand_tracks[1]"]
    expect(page.locator("#series")).to_have_values(["action.left_hand_tracks[1]"])
    assert [s["field"] for s in image_manifest(page)["series"]] == state["series"]


@pytest.mark.parametrize("raw,expected", [
    ({"episode": 99}, "episode is out of range"),
    ({"series": ["unknown"]}, "unknown field"),
    ({"panels": ["video", "unknown"]}, "panels contain an unknown value"),
    ({"overlays": [{"type": "unknown"}]}, "unknown overlay"),
])
def test_invalid_shared_selections_never_request_render(page, live_service, raw, expected):
    requests = []
    page.on("request", lambda request: requests.append(request.url) if "/render?" in request.url else None)
    token = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
    page.goto(live_service.url + "/view?state=" + token)
    expect(page.locator("#error")).to_contain_text(expected)
    expect(page.locator("#render")).to_be_hidden()
    assert requests == []
    page.locator("#reset").click()
    expect(page.locator("#render")).to_be_visible()


def test_browser_mcp_cross_origin_initialize_notifications_and_error(page, live_service):
    page.goto(live_service.origin)
    results = page.evaluate("""async endpoint => {
        const results=[];
        for(const method of ['initialize','notifications/initialized','notifications/cancelled','unknown']){
            const response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id:1,method,params:{}})});
            results.push({status:response.status,body:await response.text()});
        }
        return results;
    }""", live_service.url + "/mcp")
    assert [x["status"] for x in results] == [200, 202, 202, 200]
    assert results[1]["body"] == results[2]["body"] == ""
    assert "error" in json.loads(results[3]["body"])
    for method in ("initialize", "notifications/initialized", "notifications/cancelled", "unknown"):
        response = page.request.post(live_service.url + "/mcp", headers={"Origin": "https://review.example"}, data={"method": method})
        assert response.headers["access-control-allow-origin"] == "https://review.example"
        assert response.headers["vary"] == "Origin"
