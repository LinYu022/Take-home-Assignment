"""Independent deterministic HTTP evidence, NOT the budgeted Agent experiment."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.agent_audit import RunLedger, packed, validate_final_links


def main():
    p=argparse.ArgumentParser();p.add_argument('--data-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    import httpx
    import uvicorn
    from app.config import Settings
    from app.main import create_app
    from app.viewstate import decode_view,validation_context
    ledger=RunLedger(args.output,calls=200,text_bytes=4*1024**2)
    sock=socket.socket();sock.bind(('127.0.0.1',0)); endpoint=f'http://127.0.0.1:{sock.getsockname()[1]}'
    with tempfile.TemporaryDirectory(prefix='tool-iteration-check-') as cache:
        settings=Settings(data_root=args.data_root,s3_bucket=None,s3_endpoint=None,s3_region='auto',public_base_url='http://127.0.0.1:8000',cache_dir=Path(cache))
        app=create_app(settings);server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            for _ in range(400):
                if server.started:break
                time.sleep(.05)
            with httpx.Client(base_url=endpoint,trust_env=False,timeout=120) as client:
                def call(name,arguments):
                    request=dict(jsonrpc='2.0',id=ledger.calls+1,method='tools/call',params=dict(name=name,arguments=arguments))
                    response=client.post('/mcp',json=request); result=response.json()
                    ledger.received(request,result,wire_bytes=len(response.content),delivery='deterministic HTTP client, not Agent')
                    if result['result'].get('isError'):raise RuntimeError(result)
                    return result['result']['structuredContent']
                overview=call('describe_dataset',{})
                timing=call('audit_timing',{'episode':0})
                comparison=call('compare_series',dict(episode=0,left={'field':'action.left_hand_tracks','component':0},right={'field':'left_hand/tracks','component':1},start_frame=0,end_frame=3028,right_frame_offset=1))
                integrity=call('audit_integrity',dict(episode=0,fields=['action.left_hand_tracks','observation.left_hand_mano','observation.right_hand_mano']))
                maxima=call('aggregate_series',dict(episode=0,field='action.left_hand_tracks',component=0,start_frame=0,end_frame=3028))
                local_pairs=0;local_scalars=0
                for frame in (0,1513,3025):
                    left=call('query_series',dict(episode=0,field='action.left_hand_tracks',start_frame=frame,end_frame=frame+3,limit=3))
                    right=call('query_series',dict(episode=0,field='left_hand/tracks',start_frame=frame+1,end_frame=frame+4,limit=3))
                    assert left['values']==[v[1:] for v in right['values']]
                    local_pairs+=len(left['values']);local_scalars+=sum(map(len,left['values']))
                base=call('query_series',dict(episode=0,field='action.left_hand_tracks',start_frame=3025,end_frame=3026,limit=1))
                state=call('decode_view',{'url':base['view_url']})['view']
                alternate=copy.deepcopy(state);alternate['display']['info_bar']=False
                off=call('encode_view',{'view':alternate})
                images=[]
                for url in (base['view_url'],off['view_url']):
                    rendered=call('render_view',{'url':url});images.append(rendered)
                    normal=client.get('/render'+url.split('/view',1)[1])
                    assert hashlib.sha256(normal.content).hexdigest()==rendered['sha256']
                known={r['view_url']:r['ordinal'] for r in ledger.records if r.get('view_url')}
                links=validate_final_links('\n'.join(f'[evidence]({u})' for u in known),known,lambda u:decode_view(u,**validation_context(app.state.render_service.adapter,settings)))
                assert all(x['valid'] for x in links)
                summary=dict(source='deterministic HTTP client; independent from formal Agent budget',calls=ledger.calls,text_utf8_bytes=ledger.bytes,
                             overview=overview,timing=timing,comparison=comparison,integrity=integrity,action_component0_before_last=maxima,
                             local_client_crosscheck=dict(frame_pairs=local_pairs,scalar_pairs=local_scalars,match=True,scope='three 3-frame windows, NOT entire episode'),
                             images=images,final_links=links,service_stats=app.state.render_service.stats())
                (args.output/'summary.json').write_bytes(packed(summary))
                print(packed(dict(calls=ledger.calls,text_utf8_bytes=ledger.bytes,pts_checked=timing['details']['pts_checked'],finite_pairs=comparison['details']['finite_pairs'],matched_pairs=comparison['details']['exact_match_count'],local_scalar_pairs=local_scalars)).decode())
        finally:
            ledger.close();server.should_exit=True;thread.join(15);sock.close()


if __name__=='__main__':main()
