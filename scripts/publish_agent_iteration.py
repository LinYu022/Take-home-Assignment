"""Publish only this run's public evidence; never auth, hidden instructions or reasoning."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.agent_audit import packed


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);p.add_argument('--rollout',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    source=args.run; target=args.output;target.mkdir(parents=True,exist_ok=False)
    initial=[json.loads(s) for s in (source/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    thread=initial[0]['thread_id']
    if thread not in args.rollout.name:raise ValueError('must select the exact evaluated thread, not any other conversation')
    for sub in (Path('.'),Path('continuation-01')):
        folder=source/sub;dest=target/sub;dest.mkdir(parents=True,exist_ok=True)
        for name in ('prompt.txt','AGENTS.md','runner.py','configuration.json','tools.json','source-at-run.json','final-answer.md','events.public.jsonl','summary.json','stderr.txt'):
            path=folder/name
            if path.exists():shutil.copyfile(path,dest/name)
        for path in folder.glob('client-image-*.png'):shutil.copyfile(path,dest/path.name)
    shutil.copytree(source/'client',target/'client')
    rows=[json.loads(s) for s in args.rollout.read_text(encoding='utf-8').splitlines()]
    calls=[json.loads(s) for s in (target/'client/calls.jsonl').read_text(encoding='utf-8').splitlines()]
    business=[r for r in calls if 'ordinal'in r]
    expected_images={item['sha256'] for r in business for item in r['response'].get('result',{}).get('content',[]) if item.get('type')=='image'}
    delivered=[];tool_text_bytes=0;tool_schema_names=set();transforms=[]
    def walk(value):
        nonlocal tool_text_bytes
        if isinstance(value,dict):
            if value.get('type')=='input_image' and str(value.get('image_url','')).startswith('data:image/png;base64,'):
                png=base64.b64decode(value['image_url'].split(',',1)[1]);digest=hashlib.sha256(png).hexdigest()
                name=f'model-input-{digest[:12]}.png';(target/name).write_bytes(png)
                delivered.append(dict(sha256=digest,bytes=len(png),path=name,matches_business_png=digest in expected_images))
                return {**{k:v for k,v in value.items() if k!='image_url'},'image_path':name,'sha256':digest,'bytes':len(png)}
            if value.get('type')=='input_text':tool_text_bytes+=len(value.get('text','').encode())
            return {k:walk(v) for k,v in value.items()}
        if isinstance(value,list):return [walk(v) for v in value]
        return value
    with (target/'model-tool-context.jsonl').open('x',encoding='utf-8') as handle:
        for row in rows:
            payload=row.get('payload',{})
            if row.get('type')!='response_item' or payload.get('type') not in {'custom_tool_call','custom_tool_call_output','function_call','function_call_output'}:continue
            # Actual executed client code plus model-context tool results; no system/developer or reasoning records.
            handle.write(packed(walk(row)).decode()+'\n')
            if payload.get('type') in {'custom_tool_call','function_call'}:transforms.append(dict(call_id=payload.get('call_id'),name=payload.get('name'),code=payload.get('input',payload.get('arguments'))))
    images_complete=bool(expected_images) and expected_images=={x['sha256'] for x in delivered}
    state=json.loads((target/'continuation-01/summary.json').read_text(encoding='utf-8'))
    refusal=business[0]['text_utf8_bytes']
    summary=dict(thread_id=thread,business_calls=len(business),content_text_utf8_bytes=sum(r['text_utf8_bytes'] for r in business),
                 call_limit=40,text_limit=204800,budget_pass=len(business)<=40 and sum(r['text_utf8_bytes'] for r in business)<=204800,
                 protocol_separate=True,model_tool_context_text_utf8_bytes=tool_text_bytes,
                 text_scope_note='model tool-context metric includes discovery, execution wrappers and duplicate serialized structuredContent; distinct from main content.text budget',
                 business_mcp_object_bytes=sum(r['mcp_object_bytes'] for r in business),wire_bytes=sum(r['wire_bytes'] or 0 for r in business),
                 expected_image_hashes=sorted(expected_images),model_delivered_images=delivered,image_delivery_verified=images_complete,
                 final_links=state['links'],final_link_pass=bool(state['links']) and all(x['valid'] for x in state['links']),
                 client_refusal_text_utf8_bytes=refusal,
                 errata=[dict(location='continuation-01/prompt.txt and final-answer.md',reported=58,actual=refusal,
                              reason='operator continuation prompt miscount repeated by Agent; immutable original retained; ledger always used actual UTF-8=61'),
                         dict(location='initial final-answer.md',reported=71,actual=refusal,reason='Agent self-report was not content.text byte count')],
                 isolation='initial launch exposed unrelated app tool descriptions and refused one call; continuation disabled apps and approved only read-only lerobot; retained in same thread/budget',
                 historical_material='old B04/B05 full raw ledger/code/dialogue remain missing; no backfill',
                 redaction='raw wire/base64 duplicates, hidden platform instructions/reasoning and auth config excluded; actual tool context text, code and PNG retained')
    (target/'evaluation.json').write_bytes(packed(summary))
    (target/'README.md').write_text(f'''# 独立 Agent 实验：2026-10-16

同一线程 `{thread}`，一次改后实验，两次执行阶段（首次本地客户端配置拒绝，原会话续接）。首份回答、网络重试及拒绝保留，没有另开成功轮替换。

主预算：{len(business)}/40 次业务调用，{summary['content_text_utf8_bytes']}/204800 字节 UTF-8 content.text，含错误与 client_budget。客户端上下文文本另计 {tool_text_bytes} 字节；发现/协议、序列化重复字段、传输与 PNG 分开记录。

- 最终回答：[完整原文](continuation-01/final-answer.md)；首份失败回答：[原文](final-answer.md)。
- 全部业务请求、实际响应与计数：[账本](client/calls.jsonl)。图片 base64 改为实际 PNG 文件及哈希。
- 实际客户端执行代码及进入模型上下文的工具文本/图片：[model-tool-context.jsonl](model-tool-context.jsonl)。不复制隐藏平台指令或推理内容。图片哈希均与工具 PNG 对上：{images_complete}。
- 最终 URL 字符串、解码状态及预算：[evaluation.json](evaluation.json)。两条 URL 原文通过复核；本地临时服务已退出，换端口需服务 normalize 并保留映射。
- 所有人工编写实验提示、指南、工具 schema、代码与源码哈希分别保存在根目录和 continuation-01；未保存认证配置或原始数据。

## 勘误与验收边界

首次拒绝的实际 content.text 是 **{refusal} 字节**。Agent 首答自述71；续接提示误写58，末答沿用58。原文全部保留，权威总账始终按61计数。不能把末答的所有文字都标为无误。

数值检查、非零起点分页、单变量图像、原样 URL、预算与不可回答问题已有实际证据；看图是 Agent 行为，不是人工核对。原历史 B04/B05 缺失材料无法恢复。未在本轮重新做完整播放性能矩阵或真实 S3。完整 Agent 实验现已交付，不据此宣称整体完全通过。
''',encoding='utf-8')
    print(packed({k:v for k,v in summary.items() if k in {'business_calls','content_text_utf8_bytes','budget_pass','image_delivery_verified','final_link_pass','client_refusal_text_utf8_bytes'}}).decode())


if __name__=='__main__':main()
