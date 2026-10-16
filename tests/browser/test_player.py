import base64
import hashlib
import json
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from playwright.sync_api import expect


def snapshot(page):
    return page.evaluate("() => {const s=visualizer.snapshot;return {view:s.view,frame:s.frame,sha256:s.sha256,manifest:s.manifest}}")


def test_play_pause_seek_fullscreen_locale_and_share(visualizer, live_service, new_context):
    page = visualizer
    page.locator("#play").click()
    page.wait_for_function("visualizer.snapshot.frame >= 3")
    page.locator("#play").click()
    frame = snapshot(page)["frame"]
    page.wait_for_timeout(250)
    assert snapshot(page)["frame"] == frame and not page.evaluate("visualizer.playing")
    page.evaluate("() => visualizer.seek(19)")
    assert snapshot(page)["frame"] == 19
    page.locator("#next").click(); page.wait_for_function("visualizer.snapshot.frame===20")
    page.locator("#locale").select_option("zh")
    page.wait_for_function("visualizer.snapshot.view.display.locale==='zh'")
    expect(page.locator("#play")).to_have_text("播放")
    page.locator("#infoBar").uncheck()
    page.wait_for_function("!visualizer.snapshot.view.display.info_bar")
    page.locator("#fullscreen").click()
    page.wait_for_function("document.fullscreenElement?.id==='stage'")
    assert snapshot(page)["manifest"]["overlay"]["projected_points"] == 42
    page.evaluate("document.exitFullscreen()")
    state = snapshot(page)
    other = new_context().new_page(); other.goto(page.url)
    expect(other.locator("#render")).to_be_visible()
    assert snapshot(other) == state and not other.evaluate("visualizer.playing")
    assert page.evaluate("visualizer.resources.decoded_images") <= 2


def test_frame_envelope_actual_png_matches_browser_render_and_mcp(visualizer, live_service):
    page = visualizer
    with page.expect_response(lambda r: "/api/playback/" in r.url and "/frame?" in r.url) as received:
        page.evaluate("() => visualizer.seek(17)")
    raw = received.value.body()
    assert raw[:4] == b"LRF1"
    length = struct.unpack(">I", raw[4:8])[0]; meta = json.loads(raw[8:8+length]); png = raw[8+length:]
    assert len(png) == meta["png_bytes"]
    browser_hash = page.evaluate("async()=>{const raw=await visualizer.snapshot.blob.arrayBuffer();return [...new Uint8Array(await crypto.subtle.digest('SHA-256',raw))].map(x=>x.toString(16).padStart(2,'0')).join('')}")
    assert browser_hash == hashlib.sha256(png).hexdigest() == meta["sha256"]
    normalized = page.request.post(live_service.url+"/api/view/normalize", data={"view":meta["view"]}).json()
    regular = page.request.get(live_service.url+"/render"+normalized["view_url"].split("/view",1)[1]).body()
    mcp = page.request.post(live_service.url+"/mcp", data={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"render_view","arguments":{"view":meta["view"]}}}).json()
    assert png == regular == base64.b64decode(mcp["result"]["content"][0]["data"])


def test_drag_then_pause_or_hidden_does_not_resume(visualizer):
    page = visualizer
    for action in ("pause", "hidden"):
        page.locator("#play").click(); page.wait_for_function("visualizer.playing")
        page.locator("#progress").dispatch_event("pointerdown")
        if action == "pause": page.evaluate("visualizer.pause()")
        else:
            page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
        page.locator("#progress").evaluate("el=>{el.value=12;el.dispatchEvent(new Event('change'))}")
        page.wait_for_function("visualizer.snapshot.frame===12")
        assert not page.evaluate("visualizer.playing")
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false})")


def test_service_global_budget_mixed_entrances(live_service, monkeypatch):
    import app.render_service as module
    svc = live_service.app.state.render_service
    with svc.cv: svc._evict_png(64*1024**2)
    prepared = httpx.post(live_service.url+"/api/playback/prepare", json={"view":{}}, trust_env=False).json()
    entered=threading.Event(); release=threading.Event(); original=module.compose_view
    def block(*args,**kwargs):
        entered.set(); assert release.wait(10)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,"compose_view",block)
    def render(frame, kind):
        view={"cursor":{"frame":frame}}
        with httpx.Client(timeout=20,trust_env=False) as client:
            if kind=="mcp": return client.post(live_service.url+"/mcp",json={"jsonrpc":"2.0","id":frame,"method":"tools/call","params":{"name":"render_view","arguments":{"view":view}}})
            url=client.post(live_service.url+"/api/view/normalize",json={"view":view}).json()["view_url"]
            return client.get(live_service.url+"/render"+url.split("/view",1)[1])
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(render,31,"http"),pool.submit(render,32,"mcp")]
        assert entered.wait(5)
        deadline=time.monotonic()+5
        while svc.stats()["active"]<2 and time.monotonic()<deadline: time.sleep(.01)
        sid=prepared["session"]
        futures += [pool.submit(httpx.get,live_service.url+f"/api/playback/{sid}/frame?frame=8&generation=0",trust_env=False,timeout=20),
                    pool.submit(httpx.post,live_service.url+"/api/playback/prepare",json={"view":{}},trust_env=False,timeout=20),
                    pool.submit(render,33,"http"),pool.submit(render,34,"mcp")]
        try:
            deadline=time.monotonic()+5
            while svc.stats()["waiting"]<4 and time.monotonic()<deadline: time.sleep(.01)
            stats=svc.stats(); assert stats["active"]==2 and stats["waiting"]==4
            assert render(35,"http").status_code==429
            response=httpx.post(live_service.url+f"/api/playback/{sid}/generation",json={"generation":1},trust_env=False)
            assert response.status_code==200 and svc.stats()["waiting"]==3 and svc.stats()["active"]==2
        finally: release.set()
        results=[f.result() for f in futures]
        assert results[2].status_code==409
        for response in results:
            if response.headers.get("content-type","").startswith("application/json"):
                body=response.json()
                if "session" in body: httpx.post(live_service.url+f"/api/playback/{body['session']}/generation",json={"generation":1,"release":True},trust_env=False)
    httpx.post(live_service.url+f"/api/playback/{sid}/generation",json={"generation":2,"release":True},trust_env=False)
    stats=svc.stats()
    assert stats["peak_active"]<=2 and stats["peak_waiting"]<=4
    assert stats["peak_pixels"]<=64*1024**2 and stats["peak_png"]<=64*1024**2
    assert stats["peak_dispatch"]<=6 and stats["dispatch_waiting"]<=4


@pytest.mark.parametrize("phase",["prepare","frame"])
def test_pause_while_preparing_or_seeking_rejects_late_image(visualizer,phase):
    page=visualizer; before=snapshot(page); pending=[]
    def hold(route):
        response=route.fetch();pending.append((route,response));page.evaluate("window.__held=true")
    pattern="**/api/playback/prepare" if phase=="prepare" else "**/api/playback/**/frame?**"
    page.route(pattern,hold)
    page.evaluate("()=>{void visualizer.seek(18)}")
    page.wait_for_function("window.__held===true")
    page.evaluate("visualizer.pause()")
    for route,response in pending:
        try:route.fulfill(response=response)
        except Exception:pass  # a cancelled browser request may already be gone
    page.unroute(pattern,hold);page.wait_for_timeout(200)
    assert snapshot(page)==before and not page.evaluate("visualizer.playing")
    assert page.evaluate("visualizer.resources.decoded_images")<=2


@pytest.mark.parametrize("body",[b"broken",b"LRF1\xff\xff\xff\xff",b"LRF1\0\0\0\2{}extra"])
def test_bad_frame_envelopes_are_rejected_and_recoverable(visualizer,body):
    page=visualizer
    page.route("**/api/playback/**/frame?**",lambda route:route.fulfill(status=200,content_type="application/vnd.lerobot.frame+bin",body=body))
    page.evaluate("()=>{void visualizer.seek(17)}")
    expect(page.locator("#error")).not_to_be_empty()
    expect(page.locator("#render")).to_be_hidden()
    page.unroute("**/api/playback/**/frame?**")
    page.locator("#retry").click();expect(page.locator("#render")).to_be_visible()


def test_copy_during_pending_setting_uses_presented_snapshot(visualizer):
    page=visualizer; original=snapshot(page);held=[]
    page.evaluate("navigator.clipboard.writeText=async value=>{window.__copied=value}")
    def hold(route):
        response=route.fetch();held.append((route,response));page.evaluate("window.__settingHeld=true")
    page.route("**/api/view/normalize",hold)
    page.locator("#infoBar").uncheck();page.wait_for_function("window.__settingHeld===true")
    # The snapshot is still copyable while the next setting is being validated.
    page.locator("#copy").click()
    for route,response in held:route.fulfill(response=response)
    page.unroute("**/api/view/normalize",hold);page.wait_for_timeout(200)
    assert snapshot(page)==original
    assert page.evaluate("window.__copied")==page.url
    assert not page.evaluate("visualizer.playing")


def test_real_player_fullscreen_dense_language_and_snapshot(page,real_service,new_context,tmp_path):
    page.goto(real_service.url+"/view");expect(page.locator("#render")).to_be_visible()
    page.evaluate("()=>visualizer.seek(1514)")
    assert snapshot(page)["frame"]==1514
    page.locator("#play").click();page.wait_for_function("visualizer.snapshot.frame>=1517")
    page.evaluate("visualizer.pause()")
    held=snapshot(page);page.wait_for_timeout(100)
    assert snapshot(page)==held
    fields=["base_0_camera/position","base_0_camera/quaternion_xyzw","left_hand/tracks","right_hand/tracks","action.left_hand_tracks","action.right_hand_tracks","timestamp","timestamp_ns"]
    page.locator("#series").select_option(fields)
    page.wait_for_function("visualizer.snapshot.view.series.length===8&&visualizer.snapshot.view.render.width===1600")
    page.locator("#locale").select_option("zh")
    page.wait_for_function("visualizer.snapshot.view.display.locale==='zh'")
    before=snapshot(page)
    assert len(before["manifest"]["series"])==8
    assert all(r["rect"][2]>=700 and r["rect"][3]>=140 for r in before["manifest"]["series"])
    page.locator("#fullscreen").click();page.wait_for_function("document.fullscreenElement?.id==='stage'")
    assert snapshot(page)==before
    page.screenshot(path=str(tmp_path/"real-fullscreen-zh.png"))
    page.evaluate("document.exitFullscreen()")
    png=page.request.get(real_service.url+"/render"+page.url.split("/view",1)[1]).body()
    assert hashlib.sha256(png).hexdigest()==before["sha256"]
    (tmp_path/"real-eight-fields-zh.png").write_bytes(png)
    other=new_context().new_page();other.goto(page.url);expect(other.locator("#render")).to_be_visible()
    assert snapshot(other)==before and not other.evaluate("visualizer.playing")
    assert page.evaluate("localStorage.getItem('lerobot.locale')")=='zh'
