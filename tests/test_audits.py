import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.data.adapter import DatasetAdapter
from app.data.errors import ToolArgumentError, QueryLimitError
from app.data.source import LocalObjectStore
from app.tools import call_tool, tool_definitions, json_bytes
from app.main import create_app


def dataset(tmp_path, rows=None, features=None):
    root=tmp_path/'source'; (root/'meta').mkdir(parents=True); (root/'data').mkdir()
    if rows is None:
        rows=[dict(timestamp_ns=i*100,frame_index=i,x=float(i+1),y=float(i),v=[0.,0.] if i==7 else [1.,-1.]) for i in range(8)]
    (root/'meta/info.json').write_text(json.dumps(dict(data_path='data/{file_index}.parquet',features=features or {},total_videos=0)),encoding='utf-8')
    pq.write_table(pa.Table.from_pylist(rows),root/'data/0.parquet')
    pq.write_table(pa.Table.from_pylist([dict(episode_index=0,length=len(rows),tasks=[],**{'data/chunk_index':0,'data/file_index':0})]),root/'meta/episodes.parquet')
    pq.write_table(pa.table({'task_index':[0],'task':['fixture']}),root/'meta/tasks.parquet')
    settings=Settings(data_root=root,s3_bucket=None,s3_endpoint=None,s3_region='auto',public_base_url='http://localhost:8000',cache_dir=tmp_path/'cache')
    return DatasetAdapter(LocalObjectStore(root),settings.cache_dir),settings


def test_compare_full_pair_offset_and_boundary(tmp_path):
    adapter,settings=dataset(tmp_path)
    args=dict(episode=0,left={'field':'x'},right={'field':'y'},right_frame_offset=1,start_frame=0,end_frame=7)
    result=call_tool('compare_series',args,adapter,settings,None)
    assert result['complete'] and result['details']['exact_match_count']==7
    assert result['details']['mae']==0 and result['details']['finite_pairs']==7
    mismatch=call_tool('compare_series',{**args,'right_frame_offset':0},adapter,settings,None)
    assert mismatch['details']['mismatch_count']==7
    assert len(mismatch['details']['examples'])==5
    boundary=call_tool('compare_series',{**args,'end_frame':8},adapter,settings,None)
    assert boundary['details']['missing_pairs']==1
    with pytest.raises(ToolArgumentError):call_tool('compare_series',{**args,'left':{'field':'v','component':99}},adapter,settings,None)
    with pytest.raises(ToolArgumentError):call_tool('compare_series',{**args,'right':{'field':'missing'}},adapter,settings,None)


def test_diagnostics_precede_strict_rejection(tmp_path):
    rows=[dict(timestamp_ns=i*100,frame_index=i,v=value) for i,value in enumerate(([0.,0.],[1.,-1.],None,[float('nan'),1.],[float('inf')],[]))]
    adapter,settings=dataset(tmp_path,rows,{'v':{'shape':[2]}})
    with pytest.raises(Exception): adapter.table(0)
    result=call_tool('audit_integrity',dict(episode=0,fields=['v']),adapter,settings,None)
    d=result['details']['fields']['v']
    assert d['null_rows']==1 and d['nonfinite_components']==2
    assert d['all_zero_vectors']==1 and d['mean_zero_nonzero_vectors']==1
    assert d['length_histogram']=={'2':3,'null':1,'1':1,'0':1}
    assert len(d['examples'])>=3
    json.dumps(result,allow_nan=False)


def test_compare_duplicate_missing_nonfinite_and_unsafe_integer(tmp_path):
    rows=[dict(timestamp_ns=i*100,frame_index=f,x=x,y=y) for i,(f,x,y) in enumerate([(0,1.,1.),(1,float('nan'),0.),(1,0.,0.),(3,float('inf'),0.)])]
    adapter,settings=dataset(tmp_path,rows)
    args=dict(episode=0,left={'field':'x'},right={'field':'y'})
    d=call_tool('compare_series',args,adapter,settings,None)['details']
    assert (d['duplicate_pairs'],d['missing_pairs'],d['nonfinite_pairs'],d['finite_pairs'])==(1,1,1,1)
    root=tmp_path/'large'; root.mkdir()
    a,s=dataset(root,[dict(timestamp_ns=0,frame_index=0,x=2**60,y=2**60)])
    with pytest.raises(ToolArgumentError): call_tool('compare_series',args,a,s,None)


def test_paged_audit_continuation_and_timing_missing_pts(tmp_path):
    adapter,settings=dataset(tmp_path)
    settings=replace(settings,max_audit_rows=3)
    seen=[]; cursor=0
    while True:
        r=call_tool('audit_integrity',dict(episode=0,fields=['v'],start_frame=1,end_frame=8,cursor=cursor),adapter,settings,None)
        seen.extend(range(r['completed_frame_range']['start'],r['completed_frame_range']['end']))
        if not r['truncated']: break
        cursor=r['next_cursor']
    assert seen==list(range(1,8))
    t=call_tool('audit_timing',dict(episode=0,start_frame=2,end_frame=5),adapter,settings,None)['details']
    assert t['timestamp_count']==3 and t['intervals_ns']['count']==2
    assert t['pts_status']=='unavailable' and t['pts_missing']==3


def test_strict_limit_dynamic_schema_errors_and_byte_cap(tmp_path):
    adapter,settings=dataset(tmp_path)
    settings=replace(settings,max_query_rows=2)
    query=dict(episode=0,field='x',start_frame=2,end_frame=7,limit=3)
    schema=next(d for d in tool_definitions(settings) if d['name']=='query_series')
    assert schema['inputSchema']['properties']['limit']['maximum']==2
    with pytest.raises(ToolArgumentError): call_tool('query_series',query,adapter,settings,None)
    seen=[];cursor=0
    while True:
        r=call_tool('query_series',{**query,'limit':2,'cursor':cursor},adapter,settings,None)
        assert r['requested_limit']==2 and r['returned_count']==len(r['values'])
        seen.extend(range(r['start_frame'],r['end_frame']))
        if not r['truncated']: break
        cursor=r['next_cursor']
    assert seen==list(range(2,7))
    with pytest.raises(QueryLimitError): call_tool('query_series',{**query,'limit':2,'max_output_bytes':512},adapter,settings,None)
    for bad in (dict(start_frame=4,end_frame=4),dict(start_frame=2,start_ns=0),dict(component=50)):
        with pytest.raises(Exception): call_tool('query_series',{**query,'limit':2,**bad},adapter,settings,None)


def test_http_error_and_diagnostics_on_invalid_source(tmp_path):
    adapter,settings=dataset(tmp_path,[dict(timestamp_ns=0,frame_index=0,v=[float('nan')])])
    with TestClient(create_app(settings)) as client:
        def call(name,args): return client.post('/mcp',json=dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name=name,arguments=args))).json()['result']
        error=call('query_series',dict(episode=0,field='v',limit=257,max_output_bytes=512))
        assert error['isError'] and len(error['content'][0]['text'].encode())<=512
        assert json.loads(error['content'][0]['text'])['error']['parameter']=='limit'
        result=call('audit_integrity',dict(episode=0,fields=['v']))
        assert result['structuredContent']['details']['fields']['v']['nonfinite_components']==1


def test_catalog_field_pagination_success_and_semantics(tmp_path):
    rows=[{**dict(timestamp_ns=i*100,frame_index=i),**{f'x{j}':float(j) for j in range(40)}} for i in range(2)]
    adapter,settings=dataset(tmp_path,rows)
    (settings.data_root/'meta/custom_metadata.csv').write_text('episode_index,success\n0,True\n')
    first=call_tool('describe_dataset',{},adapter,settings,None)
    second=call_tool('describe_dataset',{'field_cursor':first['next_field_cursor']},adapter,settings,None)
    assert len(first['manifest']['fields'])+len(second['manifest']['fields'])==42
    ep=first['manifest']['episodes'][0]
    assert ep['success'] is True and ep['success_source']=='meta/custom_metadata.csv:episode_index=0:success'
    assert 'median' in ep['frame_period_method']
    assert first['manifest']['fields'][0]['observed_length_scope']['kind']=='sampled'


def test_audit_and_render_share_admission_and_cancel_holds_memory(tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from app.render_service import RenderService
    from app.video import StaleGeneration
    adapter,settings=dataset(tmp_path); service=RenderService(adapter,settings)
    entered=threading.Event();release=threading.Event();cancel=threading.Event()
    def slow(): entered.set(); assert release.wait(5); return 1
    with ThreadPoolExecutor(max_workers=3) as pool:
        task=pool.submit(service.analysis,slow,'audit',lambda:not cancel.is_set())
        assert entered.wait(2)
        ticket,_=service._acquire('render',1)
        waiting=pool.submit(service.analysis,lambda:2,'next-audit')
        deadline=time.monotonic()+2
        while service.stats()['waiting']!=1 and time.monotonic()<deadline:time.sleep(.01)
        assert service.stats()['active']==2 and service.stats()['waiting']==1
        cancel.set()
        assert service.stats()['audit_work_bytes']==32*1024**2
        assert service.stats()['active']==2
        release.set()
        with pytest.raises(StaleGeneration):task.result()
        with service.cv:service._release(ticket)
        assert waiting.result()==2
    assert service.stats()['peak_active']<=2 and service.stats()['peak_audit_work_bytes']==32*1024**2
    assert service.stats()['audit_work_bytes']==0
    service.close()


@pytest.mark.parametrize('base,pts,expected',[(Fraction(1,6),[0,1,2],[0,166666667,333333333]),(Fraction(1,2000000000),[1,3,5],[0,2,2])])
def test_timing_rational_bases_and_ties_even(tmp_path,monkeypatch,base,pts,expected):
    from types import SimpleNamespace
    import av
    rows=[dict(timestamp_ns=ts,frame_index=i,x=0.) for i,ts in enumerate(expected)]
    adapter,settings=dataset(tmp_path,rows)
    adapter.info['video_path']='videos/{video_key}/{chunk_index}-{file_index}.mp4'
    metadata=pq.read_table(settings.data_root/'meta/episodes.parquet').to_pylist()
    prefix='videos/base_0_camera/rgb/image/'
    metadata[0].update({prefix+'chunk_index':0,prefix+'file_index':0})
    pq.write_table(pa.Table.from_pylist(metadata),settings.data_root/'meta/episodes.parquet')
    adapter.materialize=lambda _: 'fake.mp4'
    class Container:
        streams=SimpleNamespace(video=[SimpleNamespace(time_base=base,width=64,height=48)])
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def decode(self,*_):return iter(SimpleNamespace(pts=p) for p in pts)
    monkeypatch.setattr(av,'open',lambda *_:Container())
    result=call_tool('audit_timing',dict(episode=0),adapter,settings,None)['details']
    assert [e['pts_ns'] for e in result['pts_examples']]==expected
    assert result['delta_ns']['max']==result['delta_ns']['min']==0
    assert result['pts_checked']==3
    last=call_tool('audit_timing',dict(episode=0,start_frame=2,end_frame=3),adapter,settings,None)['details']
    assert last['pts_checked']==1 and last['intervals_ns']['count']==0


def test_unread_work_is_not_zero_anomalies(tmp_path):
    adapter,settings=dataset(tmp_path)
    limited=replace(settings,max_audit_scalars=1)
    result=call_tool('audit_integrity',dict(episode=0,fields=['v']),adapter,limited,None)
    assert result['complete'] is False and result['input_count']==0
    assert result['completed_frame_range'] is None and result['uncompleted_frame_ranges']==[dict(start=0,end=8)]
    assert result['details']['status']=='failed' and 'fields' not in result['details']
    with pytest.raises(ValueError):replace(settings,max_audit_rows=8193)
    with pytest.raises(ValueError):replace(settings,max_audit_scalars=1000001)


@pytest.mark.parametrize('pts,start,expected_checked,expected_duplicates,scan_end',[
    ([1,1,2],1,2,0,3),([0,1],0,2,0,2),([0,None,2],0,2,0,3)])
def test_timing_actual_scan_missing_pts_and_selected_interval_scope(tmp_path,monkeypatch,pts,start,expected_checked,expected_duplicates,scan_end):
    import av
    from types import SimpleNamespace
    adapter,settings=dataset(tmp_path,[dict(timestamp_ns=i*100,frame_index=i,x=0.) for i in range(3)])
    adapter.info['video_path']='videos/{video_key}/{chunk_index}-{file_index}.mp4'
    metadata=pq.read_table(settings.data_root/'meta/episodes.parquet').to_pylist()
    prefix='videos/base_0_camera/rgb/image/'
    metadata[0].update({prefix+'chunk_index':0,prefix+'file_index':0})
    pq.write_table(pa.Table.from_pylist(metadata),settings.data_root/'meta/episodes.parquet')
    adapter.materialize=lambda _:'fake.mp4'
    class Container:
        streams=SimpleNamespace(video=[SimpleNamespace(time_base=Fraction(1,10000000),width=64,height=48)])
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def decode(self,*_):return iter(SimpleNamespace(pts=p) for p in pts)
    monkeypatch.setattr(av,'open',lambda *_:Container())
    d=call_tool('audit_timing',dict(episode=0,start_frame=start,end_frame=3),adapter,settings,None)['details']
    assert d['pts_checked']==expected_checked and d['pts_duplicate_count']==expected_duplicates
    assert d['pts_scan_scope']['end_frame']==scan_end
    assert d['pts_missing']==3-start-expected_checked


@pytest.mark.skipif(not os.getenv('LEROBOT_TEST_DATA_ROOT'),reason='explicit real snapshot required')
def test_real_full_audits():
    root=Path(os.environ['LEROBOT_TEST_DATA_ROOT'])
    adapter=DatasetAdapter(LocalObjectStore(root),Path(os.environ.get('TEMP','.'))/'audit-real-cache')
    settings=Settings(data_root=root,s3_bucket=None,s3_endpoint=None,s3_region='auto',public_base_url='http://localhost:8000',cache_dir=adapter.cache_dir)
    c=call_tool('compare_series',dict(episode=0,left={'field':'action.left_hand_tracks','component':0},right={'field':'left_hand/tracks','component':1},right_frame_offset=1,start_frame=0,end_frame=3028),adapter,settings,None)
    assert c['input_count']==3028 and c['complete']
    assert c['details']['finite_pairs']+c['details']['missing_pairs']+c['details']['duplicate_pairs']+c['details']['nonfinite_pairs']==3028
    t=call_tool('audit_timing',{'episode':0},adapter,settings,None)
    assert t['details']['pts_checked']==3029 and t['details']['intervals_ns']['count']==3028
    i=call_tool('audit_integrity',dict(episode=0,fields=['action.left_hand_tracks']),adapter,settings,None)
    assert i['details']['fields']['action.left_hand_tracks']['rows']==3029
