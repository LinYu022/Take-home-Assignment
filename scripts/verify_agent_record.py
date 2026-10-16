"""Offline verification of public evidence: no model, server or extra tool calls."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.agent_audit import packed,text_content


def verify(root):
    rows=[json.loads(s) for s in (root/'client/calls.jsonl').read_text(encoding='utf-8').splitlines()]
    business=[r for r in rows if 'ordinal'in r];total=0
    assert [r['ordinal'] for r in business]==list(range(1,len(business)+1))
    for r in business:
        actual=sum(len(s.encode()) for s in text_content(r['response']));total+=actual
        assert r['text_utf8_bytes']==actual and r['cumulative_text_utf8_bytes']==total
        restored=copy.deepcopy(r['response'])
        for item in restored.get('result',{}).get('content',[]):
            if item.get('type')!='image':continue
            png=(root/'client'/item.pop('path')).read_bytes()
            assert hashlib.sha256(png).hexdigest()==item.pop('sha256') and len(png)==item.pop('bytes')
            item['data']=base64.b64encode(png).decode()
        assert len(packed(restored))==r['mcp_object_bytes']
    evaluation=json.loads((root/'evaluation.json').read_text(encoding='utf-8'))
    assert evaluation['business_calls']==len(business)<=40
    assert evaluation['content_text_utf8_bytes']==total<=204800
    for image in evaluation['model_delivered_images']:
        png=(root/image['path']).read_bytes()
        assert hashlib.sha256(png).hexdigest()==image['sha256']
    assert evaluation['image_delivery_verified'] and evaluation['final_link_pass']
    answer=(root/'continuation-01/final-answer.md').read_text(encoding='utf-8')
    for link in evaluation['final_links']:assert link['url'] in answer and link['valid']
    return dict(calls=len(business),content_text_utf8_bytes=total,images=len(evaluation['model_delivered_images']),offline_verified=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('run',type=Path);args=p.parse_args();print(packed(verify(args.run)).decode())
