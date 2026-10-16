"""Actual received/decoded PNG, final URL state and projection provenance."""
import base64
import copy
import hashlib
from playwright.sync_api import expect
from tests.browser.test_evidence_navigation import data, tool, assert_render_channels


def test_single_variable_final_urls_and_projection_manifest(page,evidence_service):
    service=evidence_service
    frame=3025 if service.length==3029 else 25
    query=data(page,service,'query_series',dict(episode=0,field='action.left_hand_tracks',start_frame=frame,end_frame=frame+1,limit=1))
    state=data(page,service,'decode_view',{'url':query['view_url']})['view']
    state['display']={'locale':'en','info_bar':True}
    on=data(page,service,'encode_view',{'view':state})
    offstate=copy.deepcopy(on['view']); offstate['display']['info_bar']=False
    off=data(page,service,'encode_view',{'view':offstate})
    delivered=[on['view_url'],off['view_url']]
    assert {k for k in on['view'] if on['view'][k]!=off['view'][k]}=={'display'}
    hashes=[]
    for url in delivered:
        page.goto(url); expect(page.locator('#render')).to_be_visible()
        assert_render_channels(page,service)
        actual=page.evaluate('async()=>Array.from(new Uint8Array(await visualizer.snapshot.blob.arrayBuffer()))')
        result=tool(page,service,'render_view',{'url':url})
        assert bytes(actual)==base64.b64decode(result['content'][0]['data'])
        manifest=result['structuredContent']['manifest']; overlay=manifest['overlay']
        assert sum(overlay[k] for k in ('nonfinite_points','behind_camera_points','outside_image_points','drawn_points'))==overlay['total_points']==42
        assert overlay['source_fields']==['left_hand/tracks','right_hand/tracks'] and overlay['source_frame']==frame
        assert (manifest['range_start_frame'],manifest['range_end_frame'])==(frame,frame+1)
        hashes.append(hashlib.sha256(bytes(actual)).hexdigest())
    assert hashes[0]!=hashes[1]
