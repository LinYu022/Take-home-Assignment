import json
import pytest
from app.agent_audit import RunLedger, validate_final_links


def test_utf8_errors_repeats_and_crash_are_kept(tmp_path):
    run = RunLedger(tmp_path / "run")
    response = {"result": {"isError": True, "content": [{"type": "text", "text": "ASCII中文🙂"}]}}
    for _ in range(2):
        run.reserve(512)
        run.received({"method": "tools/call"}, response)
    run.event({"local_error": "summary failed after receiving response"}); run.close()
    rows = [json.loads(s) for s in (tmp_path / "run/calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[1]["cumulative_text_utf8_bytes"] == 2 * len("ASCII中文🙂".encode())
    assert rows[0]["text_characters"] == len("ASCII中文🙂")
    assert rows[2]["local_error"]
    with pytest.raises(FileExistsError): RunLedger(tmp_path / "run")


def test_preflight_budget_and_final_links(tmp_path):
    run = RunLedger(tmp_path / "run", calls=1, text_bytes=1024)
    with pytest.raises(ValueError): run.reserve(513, final_reserve=512)
    run.reserve(512, final_reserve=512)
    run.received({}, {"content": [{"type": "text", "text": "x"}]})
    with pytest.raises(ValueError): run.reserve(512, final_reserve=0)
    run.close()
    result = validate_final_links("[one](http://localhost/view?state=good) [bad](http://localhost/view?state=damaged)",
                                  {"http://localhost/view?state=good": 1}, lambda u: {"state": u})
    assert [r["valid"] for r in result] == [True, False]


def test_resume_preserves_refusal_and_cumulative_budget(tmp_path):
    run=RunLedger(tmp_path/'run')
    run.received({'client_refusal':True},{'result':{'content':[{'type':'text','text':'denied中文'}],'isError':True}})
    run.close()
    continued=RunLedger(tmp_path/'run',resume=True)
    assert continued.calls==1 and continued.bytes==len('denied中文'.encode())
    continued.received({}, {'result':{'content':[{'type':'text','text':'next'}]}})
    continued.close()
    rows=[json.loads(s) for s in (tmp_path/'run/calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [r['ordinal'] for r in rows]==[1,2]


def test_proxy_keeps_wire_before_json_failure(tmp_path,monkeypatch,capsys):
    import io
    import sys
    from types import SimpleNamespace
    import httpx
    from scripts.run_agent_iteration import proxy
    class Client:
        def __init__(self,**_):pass
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def post(self,*_,**__):return SimpleNamespace(content=b'broken JSON after received HTTP response')
    monkeypatch.setattr(httpx,'Client',Client)
    request=dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='query_series',arguments=dict(episode=0,field='x')))
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO((json.dumps(request)+'\n').encode())))
    proxy('http://localhost/mcp',tmp_path)
    assert (tmp_path/'wire/001.json').read_bytes()==b'broken JSON after received HTTP response'
    response=json.loads(capsys.readouterr().out)
    assert response['result']['isError']
    assert 'JSONDecodeError' in response['result']['content'][0]['text']
    rows=[json.loads(s) for s in (tmp_path/'client/calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert rows[-1]['ordinal']==1 and rows[-1]['text_utf8_bytes']>0


def test_published_real_agent_record_is_offline_reproducible():
    from pathlib import Path
    from scripts.verify_agent_record import verify
    result=verify(Path(__file__).parents[1]/'agent-sessions/runs/20261016-iteration-01')
    assert result==dict(calls=21,content_text_utf8_bytes=48389,images=2,offline_verified=True)
