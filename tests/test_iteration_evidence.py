import copy
import json
from PIL import Image
import numpy as np
import pytest

from tests.test_field_semantics import semantic_dataset
from app.data.adapter import DatasetAdapter
from app.data.source import LocalObjectStore
from app.renderer import compose_view
from app.viewstate import normalize_view, validation_context, decode_view
from app.tools import call_tool
from app.agent_audit import validate_final_links


def test_overlay_exclusive_counts_and_source_geometry(semantic_dataset):
    root,settings,_=semantic_dataset
    adapter=DatasetAdapter(LocalObjectStore(root),settings.cache_dir)
    state=normalize_view({'series':[],'overlays':[{'type':'hand_tracks','sides':['left']}]},**validation_context(adapter,settings))
    original=adapter.row(0,0); row=dict(original)
    points=[[0.,0.,1.] for _ in range(21)]
    points[0]=[float('nan'),0.,1.]; points[1]=[0.,0.,-1.]; points[2]=[100.,0.,1.]
    row['left_hand/tracks']=[0,*np.asarray(points).reshape(-1).tolist()]
    row['base_0_camera/position']=[0,0.,0.,0.]
    adapter.row=lambda *_:row  # test compositor alone, not bypass production validation
    _,manifest=compose_view(adapter,state,Image.new('RGB',(64,48)))
    overlay=manifest['overlay']
    assert [overlay[k] for k in ('total_points','nonfinite_points','behind_camera_points','outside_image_points','drawn_points')]==[21,1,1,1,18]
    assert overlay['outside_canvas']==3 and overlay['projected_points']==21
    assert overlay['source_fields']==['left_hand/tracks'] and overlay['source_frame']==0
    assert manifest['calibration_image_size']==manifest['decoded_video_size']==[64,48]
    assert manifest['overlay_transform']['crop'] is None


def test_final_url_single_variable_and_bad_delivered_links(semantic_dataset):
    root,settings,_=semantic_dataset
    adapter=DatasetAdapter(LocalObjectStore(root),settings.cache_dir)
    request=dict(episode=0,field='action.left_hand_tracks',start_frame=2,end_frame=3,limit=1)
    result=call_tool('query_series',request,adapter,settings,None)
    decoded=call_tool('decode_view',{'url':result['view_url']},adapter,settings,None)
    a=decoded['view']; b=copy.deepcopy(a); b['display']['info_bar']=False
    encoded=call_tool('encode_view',{'view':b},adapter,settings,None)
    final=f"[on]({result['view_url']}) [off]({encoded['view_url']})"
    links=validate_final_links(final,{result['view_url']:1,encoded['view_url']:2},lambda u:decode_view(u,**validation_context(adapter,settings)))
    assert all(x['valid'] for x in links)
    assert {k for k in a if a[k]!=links[1]['view'][k]}=={'display'}
    for raw in ({**a,'render':{'width':2024,'height':768}}, {k:v for k,v in a.items() if k!='series'}):
        if 'series' not in raw:
            # A valid default is still not the original query evidence.
            default=call_tool('encode_view',{'view':raw},adapter,settings,None)
            assert default['view']['series']!=a['series']
            check=validate_final_links(f"[default]({default['view_url']})",{result['view_url']:1},lambda u:decode_view(u,**validation_context(adapter,settings)))
            assert check[0]['valid'] is False
        else:
            with pytest.raises(Exception): call_tool('encode_view',{'view':raw},adapter,settings,None)
    bad=validate_final_links(final.replace(result['view_url'],result['view_url'][:-8]+'broken'),{},lambda u:decode_view(u,**validation_context(adapter,settings)))
    assert not any(x['valid'] for x in bad)
