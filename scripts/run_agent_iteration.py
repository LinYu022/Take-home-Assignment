"""One real Codex evaluation, with an audited stdio->HTTP MCP boundary.

No raw-data/file tools are exposed to the evaluated Agent. Unique output only.
The proxy captures actual received bytes before parsing; public ledgers replace
image base64 with the exact PNG file. CLI events provide a distinct client layer.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.agent_audit import RunLedger, packed, validate_final_links


PROMPT="""你是首次接入 LeRobot 数据服务的独立分析 Agent。仅凭当前 AGENTS.md 和 MCP discovery 完成分析；不能读取源码、原数据或使用其他工具。不启动其他 Agent。
硬预算：所有业务 tools/call（含错误、重复、丢弃）最多40次，全部响应 content.text 累计UTF-8最多204800字节；发现/协议另外记账。代理每次附client_budget，包含实际累计；最终证据复查预留4096字节。不能完成就报告剩余范围，停止，不能越预算再查。只使用工具返回的原始URL，绝不手工编造token。
请自行 discovery 并按有界方式完成：
1 数据规模、task、字段声明/存储类型、camera/hand时间前缀、MANO原始61分量布局与坐标系/单位、observed_length观测范围、success实际来源及不能推出的内容。
2 对非零起点[1513,1518)以limit2逐页查询action.left_hand_tracks component0，保存全部三页范围和next_cursor，聚合同范围对照；遇上限错误按工具提示修正。可先有意请求limit257一次验证恢复，不重复制造错误。
3 用compare_series尝试覆盖左action raw0[t]和observed left_hand/tracks raw1[t+1]的[0,3028)，明确找到/有限/缺失/匹配计数与完整性；再用小查询窗口独立核对几对数值，不把单分量扩展为全63维结论。
4 audit_timing全段及audit_integrity左手action+两个MANO字段，分别说明完整覆盖、时间编码关系、全零和均值为零区别。字段名以实际发现为准，不猜测。缺失/失败和未检查不是0异常。
5 构造frame3025、单帧series_query evidence=[3025,3026)、series=[action.left_hand_tracks]、左右overlay、1024x768的完整canonical视图；通过encode/decode得到状态，深拷贝后仅改display.info_bar再编码/解码。两张都render_view并真正看图，逐字段确认只有info_bar差异。解释outside_canvas、drawn和overlay源帧；暗带不等于无效ROI、接触或失准。最终原样给两条URL，不能只校验中间变量。
6 回答追问：该关系能否把O[t+1]作为预测A[t]的输入？最后全零能否证明静止/终止/成功？success=true能否给成功率？能否从暗带、投影计数0、小PTS差推断像素对齐/时钟漂移/接触力？分别指出证据缺口。
最终交付中文分析：结论ID、性质（事实/推断/假设）、来源（Agent通过实际工具；无人工核对不要声称人工）、精确范围/分量、调用序号或可定位工具与范围、原样URL、复核方法；另列预算、覆盖缺口、看图观察与限制，以及使用过的新工具和一条后续工具设计建议。勿将相同分布等同逐帧匹配；勿把图像接收当人工检查。
代理若说预算不足，立即完成有限结论，不再调用。一次实验，不静默重试整轮。
"""


def proxy(url,directory,resume=False):
    import httpx
    run=RunLedger(Path(directory)/'client',resume=resume)
    raw_dir=Path(directory)/'wire'; raw_dir.mkdir(exist_ok=resume)
    ordinal=len(list(raw_dir.glob('*.json')))
    with httpx.Client(trust_env=False,timeout=120) as client:
        for raw in sys.stdin.buffer:
            request=json.loads(raw)
            method=request.get('method'); business=method=='tools/call'
            if method in {'ping','notifications/initialized','notifications/cancelled'}:
                if 'id' in request: print(json.dumps(dict(jsonrpc='2.0',id=request['id'],result={})),flush=True)
                continue
            ordinal+=1; original=json.loads(packed(request)); effective=json.loads(packed(request))
            if business:
                if run.calls>=40:
                    run.event(dict(stop_reason='call_limit',blocked_attempt=original)); break
                remaining=run.max_bytes-run.bytes-4096-512
                if remaining<512:
                    response=dict(jsonrpc='2.0',id=request['id'],result=dict(content=[dict(type='text',text='Budget exhausted: STOP and report incomplete coverage.')],isError=True))
                    run.received(original,response); print(packed(response).decode(),flush=True); continue
                args=effective['params'].setdefault('arguments',{})
                desired=args.get('max_output_bytes',16384)
                bound=min(desired,16384,remaining) if type(desired) is int and desired>=512 else 512
                if 'max_output_bytes' not in args or type(desired) is int and desired>=512: args['max_output_bytes']=bound
                run.reserve(bound+512)
            try:
                response=client.post(url,json=effective)
                body=response.content
                # Write immediately: parsing/statistics may fail next.
                filename=raw_dir/f'{ordinal:03d}.json'
                with filename.open('xb') as handle: handle.write(body); handle.flush(); os.fsync(handle.fileno())
                message=json.loads(body)
            except Exception as exc:
                message=dict(jsonrpc='2.0',id=request.get('id'),result=dict(content=[dict(type='text',text='ClientTransportError: '+type(exc).__name__)],isError=True))
                body=b''
            if business:
                if 'error' in message:
                    message=dict(jsonrpc='2.0',id=request.get('id'),result=dict(isError=True,content=[dict(type='text',text=json.dumps(message['error'],ensure_ascii=False))]))
                # A separate client block makes the UTF-8/call ledger visible to the Agent.
                content=message['result']['content']; old=sum(len(x['text'].encode()) for x in content if x['type']=='text')
                budget=dict(calls=run.calls+1,text_utf8_bytes=run.bytes+old,remaining_calls=39-run.calls)
                block=dict(type='text',text='')
                for _ in range(4):
                    block['text']=packed({'client_budget':budget}).decode()
                    budget['text_utf8_bytes']=run.bytes+old+len(block['text'].encode())
                    budget['remaining_text_bytes']=run.max_bytes-budget['text_utf8_bytes']
                block['text']=packed({'client_budget':budget}).decode(); content.append(block)
                run.event(dict(effective_request=effective,original_request=original,wire_file=f'wire/{filename.name}' if body else None))
                run.received(original,message,wire_bytes=len(body),delivery='proxy_forwarded_to_Codex; model_handoff_requires_client_trace')
            else:
                run.event(dict(protocol=original,response=message,wire_bytes=len(body),main_budget=False))
            print(packed(message).decode(),flush=True)
    run.close()


def _public_events(path):
    """Keep actual CLI events, extracting image payloads without publishing duplicate base64."""
    def walk(value):
        if isinstance(value,dict):
            if value.get('type')=='image' and isinstance(value.get('data'),str):
                data=base64.b64decode(value['data']); digest=hashlib.sha256(data).hexdigest()
                target=path.parent/f'client-image-{digest[:12]}.png'
                if not target.exists(): target.write_bytes(data)
                return {**{k:walk(v) for k,v in value.items() if k!='data'},'path':target.name,'sha256':digest,'bytes':len(data)}
            return {k:walk(v) for k,v in value.items()}
        if isinstance(value,list): return [walk(v) for v in value]
        return value
    with path.open(encoding='utf-8') as source,(path.parent/'events.public.jsonl').open('x',encoding='utf-8') as dest:
        for line in source:
            try: dest.write(packed(walk(json.loads(line))).decode()+'\n')
            except json.JSONDecodeError: dest.write(packed({'unparsed_event':line}).decode()+'\n')


def main():
    p=argparse.ArgumentParser(); p.add_argument('--proxy',nargs=2,metavar=('URL','DIRECTORY'))
    p.add_argument('--data-root',type=Path); p.add_argument('--output',type=Path); p.add_argument('--codex',type=Path)
    p.add_argument('--continue-thread');p.add_argument('--resume-ledger',action='store_true')
    args=p.parse_args()
    if args.proxy: proxy(*args.proxy,resume=args.resume_ledger); return
    if not args.data_root or not args.output or not args.codex: p.error('require --data-root, --output, --codex')
    out=args.output.resolve()
    if args.continue_thread:
        initial=json.loads((out/'events.jsonl').read_text(encoding='utf-8').splitlines()[0])
        if initial.get('thread_id')!=args.continue_thread:raise ValueError('continuation must use this exact original thread')
        part=out/'continuation-01';part.mkdir(exist_ok=False)
        previous=[json.loads(s) for s in (out/'client/calls.jsonl').read_text(encoding='utf-8').splitlines()]
        business=[r for r in previous if 'ordinal'in r]
        prompt=f'这是同一实验的客户端配置修复后继续，非新实验。所有失败和首份回答保留。权威账本已有{len(business)}次业务调用、{sum(r["text_utf8_bytes"] for r in business)}个content.text UTF-8字节，沿用同一40次/204800字节总预算。已关闭apps连接器，只允许本项目只读工具。继续未完成问题，不重复已取得的数据；client_budget为权威累计。此前自述的字符/字节数若与原始账本不符，最后纠正，不改写原答。'+PROMPT
    else:
        out.mkdir(parents=True,exist_ok=False);part=out;prompt=PROMPT
    (part/'prompt.txt').write_text(prompt,encoding='utf-8')
    shutil.copyfile(ROOT/'AGENTS.md',part/'AGENTS.md')
    (part/'runner.py').write_bytes(Path(__file__).read_bytes())
    from app.config import Settings
    from app.main import create_app
    from app.tools import tool_definitions
    from app.viewstate import decode_view,validation_context
    import uvicorn
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); url=f'http://127.0.0.1:{sock.getsockname()[1]}'
    with tempfile.TemporaryDirectory(prefix='lerobot-agent-eval-') as tmp:
        tmp=Path(tmp); clean=tmp/'workspace'; clean.mkdir(); shutil.copyfile(ROOT/'AGENTS.md',clean/'AGENTS.md')
        settings=Settings(data_root=args.data_root.resolve(),s3_bucket=None,s3_endpoint=None,s3_region='auto',public_base_url=url,cache_dir=tmp/'cache')
        app=create_app(settings); server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True); thread.start()
        try:
            for _ in range(400):
                if server.started: break
                if not thread.is_alive(): raise RuntimeError('service startup failed')
                time.sleep(.05)
            else: raise RuntimeError('service startup timed out')
            disabled=['shell_tool','plugins','apps','hooks','tool_suggest','skill_search','multi_agent','goals','sleep_tool','view_image','image_generation','computer_use','in_app_browser','workspace_dependencies','memories']
            config={'web_search':'disabled','features.skip_host_skill_discovery':True,
                    'mcp_servers.lerobot.command':sys.executable,
                    'mcp_servers.lerobot.args':[str(Path(__file__).resolve()),'--proxy',url+'/mcp',str(out)]+(['--resume-ledger'] if args.continue_thread else []),
                    'mcp_servers.lerobot.required':True,'mcp_servers.lerobot.tool_timeout_sec':120,
                    'mcp_servers.lerobot.enabled_tools':[d['name'] for d in tool_definitions(settings)],
                    'mcp_servers.lerobot.default_tools_approval_mode':'approve',
                    'mcp_servers.lerobot.env':{'PYTHONPATH':os.environ.get('PYTHONPATH','')},
                    'tool_output_token_limit':24000,'project_doc_max_bytes':32768}
            for f in disabled: config['features.'+f]=False
            for d in tool_definitions(settings): config[f"mcp_servers.lerobot.tools.{d['name']}.output_token_limit"]=24000
            cmd=[str(args.codex),'-C',str(clean),'-s','read-only','exec']+(['resume'] if args.continue_thread else [])+['--ignore-user-config','--ignore-rules','--skip-git-repo-check','--json','-o',str(part/'final-answer.md')]
            for k,v in config.items():
                def toml(value):
                    if isinstance(value,dict): return '{'+','.join(json.dumps(a)+'='+toml(b) for a,b in value.items())+'}'
                    return json.dumps(value,ensure_ascii=False)
                cmd+=['-c',k+'='+toml(v)]
            cmd+=([args.continue_thread] if args.continue_thread else [])+['-']
            (part/'configuration.json').write_bytes(packed(dict(config=config,cli_flags=cmd[:7],auth='existing CLI login; never copied',model='installed CLI default',budget=dict(calls=40,text_utf8_bytes=204800,protocol_included=False))))
            (part/'tools.json').write_bytes(packed(tool_definitions(settings)))
            env={k:v for k,v in os.environ.items() if not k.upper().startswith(('AWS_','S3_')) and k not in {'DATA_ROOT','LEROBOT_TEST_DATA_ROOT'}}
            started=time.time()
            with (part/'events.jsonl').open('xb') as stdout,(part/'stderr.txt').open('xb') as stderr:
                process=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr,env=env)
                process.communicate(prompt.encode(),timeout=1800)
            _public_events(part/'events.jsonl')
            calls=[json.loads(s) for s in (out/'client/calls.jsonl').read_text(encoding='utf-8').splitlines()]
            business=[c for c in calls if 'ordinal' in c]
            known={c['view_url']:c['ordinal'] for c in business if c.get('view_url')}
            # These are deterministic OFFLINE checks, not unrecorded Agent MCP calls.
            context=validation_context(app.state.render_service.adapter,settings)
            answer=(part/'final-answer.md').read_text(encoding='utf-8') if (part/'final-answer.md').exists() else ''
            links=validate_final_links(answer,known,lambda u:decode_view(u,**context))
            summary=dict(exit_code=process.returncode,elapsed_seconds=time.time()-started,public_base_url=url,
                         calls=len(business),content_text_utf8_bytes=sum(c['text_utf8_bytes'] for c in business),
                         call_limit=40,text_limit=204800,links=links,
                         model_delivery='CLI event trace must be checked; proxy receipt alone is insufficient',
                         offline_checks='final URL strings against received tool registry + canonical decoder; not Agent knowledge or extra tool calls')
            (part/'summary.json').write_bytes(packed(summary))
            print(packed({k:v for k,v in summary.items() if k!='links'}).decode())
        finally:
            server.should_exit=True; thread.join(15); sock.close()


if __name__=='__main__': main()
