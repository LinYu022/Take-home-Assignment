import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.resource_lifecycle import ResourceLifecycle
from scripts.benchmark_player import summarize


def test_cancelled_request_before_response_start_releases_lease_and_dispatch():
    closed=[];cancelled=threading.Event()
    state=dict(render_cancellation=cancelled,render_owner="request-1",render_service=SimpleNamespace(cancel=lambda owner:closed.append(owner)),
               render_leases=[SimpleNamespace(close=lambda:closed.append("lease"))],render_dispatch=SimpleNamespace(finish=lambda **kw:closed.append(kw)))
    async def interrupted(scope,receive,send):raise asyncio.CancelledError()
    async def scenario():
        with pytest.raises(asyncio.CancelledError):await ResourceLifecycle(interrupted)({"type":"http","state":state},None,None)
    with ThreadPoolExecutor(max_workers=1) as worker:worker.submit(lambda:asyncio.run(scenario())).result()
    assert cancelled.is_set() and closed==["request-1","lease",{"response":True}]


def test_half_rate_has_source_supply_threshold_and_mandatory_windows():
    frames=[dict(frame=i,at_ms=i*1000/15,pts_ns=round(i/30*1e9)) for i in range(1,151)]
    metrics=dict(plays=[dict(rate=.5,started_at=0,ended_at=10000,start_frame=0,end_frame=150,start_pts=0,end_pts=5_000_000_000)],presentations=frames,buffers=[],requests=[],peaks={})
    result=summarize(metrics,30)
    assert result["average_fps_threshold"]==12 and result["window_fps_threshold"]==10
    assert all(result["gates"].values())
    metrics["presentations"]=[dict(frame=i,at_ms=i*5000/45,pts_ns=i*1000) for i in range(1,46)]+[dict(frame=45+i,at_ms=5000+i*5000/75,pts_ns=i*1000) for i in range(1,76)]
    result=summarize(metrics,30)
    assert result["gates"]["fps"] and not result["gates"]["windows"]
