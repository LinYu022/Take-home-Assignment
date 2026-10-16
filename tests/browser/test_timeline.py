"""Timeline requests must neither publish stale errors nor wait for decoding."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from playwright.sync_api import expect


def _hold_second_page(page, service):
    held = []
    pattern = "**/api/playback/**/timeline?cursor=512&limit=512"
    page.route(pattern, lambda route: held.append(route))
    page.goto(service.url + "/view")
    # A cold real snapshot builds the PTS index on its first render.
    expect(page.locator("#render")).to_be_visible(timeout=20000)
    page.evaluate("() => visualizer.seek(510)")
    page.locator("#play").click()
    # Dispatch Playwright events without sleeping inside the route handler.
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(held) == 1
    return held[0], pattern


def _presented_state(page):
    return page.evaluate("""() => ({
        frame: visualizer.snapshot.frame, view: visualizer.snapshot.view,
        manifest: visualizer.snapshot.manifest, hash: visualizer.snapshot.sha256,
        pixels: document.getElementById('render').toDataURL(), url: location.href,
        frameInput: document.getElementById('frame').value,
        progress: document.getElementById('progress').value,
        status: document.getElementById('status').textContent,
        loading: document.getElementById('loading').textContent,
        error: document.getElementById('error').textContent,
        playing: visualizer.playing
    })""")


@pytest.mark.parametrize("failure", ["503", "network", "invalid_json"])
def test_obsolete_timeline_failure_preserves_new_picture(page, timeline_service, failure):
    route, pattern = _hold_second_page(page, timeline_service)
    page.evaluate("() => visualizer.seek(100)")
    expect(page.locator("#render")).to_be_visible()
    before = _presented_state(page)
    assert before["frame"] == 100 and not before["playing"] and not before["error"]
    if failure == "network":
        route.abort("failed")
    else:
        route.fulfill(status=503 if failure == "503" else 200, content_type="application/json",
                      body='{"error":{"message":"old timeline unavailable"}}' if failure == "503" else 'broken JSON')
    page.unroute(pattern)
    page.wait_for_timeout(200)
    expect(page.locator("#render")).to_be_visible()
    assert _presented_state(page) == before


def test_current_timeline_failure_is_visible_and_recoverable(page, paged_service):
    route, pattern = _hold_second_page(page, paged_service)
    route.fulfill(status=503, content_type="application/json", body='{"error":{"message":"timeline unavailable"}}')
    expect(page.locator("#error")).to_contain_text("503")
    expect(page.locator("#render")).to_be_hidden()
    assert not page.evaluate("visualizer.playing")
    page.unroute(pattern)
    page.locator("#retry").click()
    expect(page.locator("#render")).to_be_visible()
    expect(page.locator("#error")).to_be_empty()


def test_timeline_and_generation_respond_during_slow_decode(live_service, monkeypatch, record_property):
    service = live_service.app.state.render_service
    with httpx.Client(base_url=live_service.url, trust_env=False, timeout=10) as client:
        prepared = client.post("/api/playback/prepare", json={"view": {}}).json()
        sid = prepared["session"]
        endpoint = f"/api/playback/{sid}"
        baseline = client.get(endpoint + "/timeline").json()
        session = service.get_session(sid)
        entered, release, timeline_entered = threading.Event(), threading.Event(), threading.Event()
        original_get, original_timeline = session.decoder.get, service.timeline

        def slow_decode(*args, **kwargs):
            entered.set()
            assert release.wait(10), "test did not release the decoder"
            return original_get(*args, **kwargs)

        def observed_timeline(*args, **kwargs):
            timeline_entered.set()
            return original_timeline(*args, **kwargs)

        monkeypatch.setattr(session.decoder, "get", slow_decode)
        monkeypatch.setattr(service, "timeline", observed_timeline)
        with service.cv:
            service._evict_png(64 * 1024**2)

        def timed_request(path, payload=None):
            start = time.perf_counter()
            response = client.get(path) if payload is None else client.post(path, json=payload)
            return response, (time.perf_counter() - start) * 1000

        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                frame = pool.submit(client.get, endpoint + "/frame?frame=17&generation=0")
                try:
                    assert entered.wait(5)
                    timeline = pool.submit(timed_request, endpoint + "/timeline")
                    assert timeline_entered.wait(5)
                    generation = pool.submit(timed_request, endpoint + "/generation", {"generation": 1})
                    result, timeline_ms = timeline.result(timeout=.5)
                    control, generation_ms = generation.result(timeout=.5)
                    assert result.status_code == control.status_code == 200
                    assert result.json() == baseline
                    assert timeline_ms < 500 and generation_ms < 500
                    record_property("timeline_ms", round(timeline_ms, 3))
                    record_property("generation_ms", round(generation_ms, 3))
                    assert not release.is_set() and not frame.done()
                finally:
                    release.set()
                assert frame.result().status_code == 409
        finally:
            release.set()
            client.post(endpoint + "/generation", json={"generation": 2, "release": True})


@pytest.mark.parametrize("end_session", ["release", "expire"])
def test_timeline_survives_decoder_eviction_but_not_session_end(live_service, monkeypatch, end_session):
    service = live_service.app.state.render_service
    with httpx.Client(base_url=live_service.url, trust_env=False) as client:
        prepared = client.post("/api/playback/prepare", json={"view": {}}).json()
        sid = prepared["session"]
        endpoint = f"/api/playback/{sid}"
        original = client.get(endpoint + "/timeline?cursor=2&limit=4").json()
        assert original["rows"] == [dict(frame=i, pts_ns=i * 100_000_000, timestamp_ns=i * 100_000_000)
                                    for i in range(2, 6)]
        session = service.get_session(sid)
        with service.cv:
            service._drop_decoder(session)
        assert session.decoder is None

        def unexpected(*args, **kwargs):
            pytest.fail("timeline must use prepared references, not reload data or create a decoder")

        monkeypatch.setattr(service, "_decoder", unexpected)
        monkeypatch.setattr(service.adapter, "table", unexpected)
        assert client.get(endpoint + "/timeline?cursor=2&limit=4").json() == original
        for query in ("cursor=-1", "cursor=40", "limit=0", "limit=513"):
            assert client.get(endpoint + "/timeline?" + query).status_code == 400
        for setting, value in (("max_query_scalars", 2), ("max_query_bytes", 10)):
            with monkeypatch.context() as limits:
                limits.setattr(service, "settings", replace(service.settings, **{setting: value}))
                assert client.get(endpoint + "/timeline?limit=1").status_code == 413
        if end_session == "release":
            assert client.post(endpoint + "/generation", json={"generation": 1, "release": True}).status_code == 200
        else:
            with service.cv:
                session.touched -= 301
        response = client.get(endpoint + "/timeline")
        assert response.status_code == 410 and response.json()["error"]["code"] == "SessionExpired"
