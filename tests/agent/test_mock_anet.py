"""M14-AC-013：MockHub find（精确、尾 *、逗号 OR、大小写、无匹配共 10 例）；未服务能力 UNAVAILABLE；长任务第 5 个并发立即
UNAVAILABLE；回执 7 项绑定校验（篡改 → 486）；即时能力回复时刻 = t_send + L。"""

from __future__ import annotations

import asyncio

import pytest
from fakes.stub_provider import StubProvider, q

from awr.agent.anet_mock.hub import MockHub
from awr.agent.anet_mock.network import MockNetwork, make_ix
from awr.agent.runtime.clock import SimScheduler
from awr.agent.runtime.types import EffectStatus


@pytest.mark.parametrize("pattern,want", [
    ("thermal.imaging", ["b1", "b2"]), ("thermal.*", ["b1", "b2"]), ("rgb.zoom", ["a1"]), ("rgb.*,thermal.imaging", ["a1", "b1", "b2"]),
    ("Thermal.imaging", []), ("thermal.imag*", ["b1", "b2"]), ("thermal.imaging.hd", []), ("*", ["a1", "b1", "b2", "c1"]),
    ("relay.*", ["c1"]), ("lidar.*", [])])
def test_hub_find_patterns(pattern: str, want: list[str]) -> None:
    hub = MockHub()
    hub.register("b2", 3, ["thermal.imaging", "task.quote"])
    hub.register("a1", 1, ["rgb.zoom", "task.quote"])
    hub.register("b1", 2, ["thermal.imaging", "task.quote"])
    hub.register("c1", 4, ["relay.communication"])
    assert [aid for _no, aid in hub.find(pattern)] == want


def _net(**kw) -> tuple[SimScheduler, MockNetwork]:
    s = SimScheduler(0)
    return s, MockNetwork(s, world_seed=5, **kw)


async def _pump(s: SimScheduler, until, t_max_s: float = 60.0) -> None:
    while not until() and s.now_s() < t_max_s:
        await s.advance_to(s.now_ns() + 10_000_000)


def test_quote_timing_and_receipt() -> None:
    s, net = _net()
    p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(), sched=s)

    async def main():
        await net.register(p)
        ix = await net.delegate("req", p.aid, "task.quote", {}, task_id="T-0001")
        assert ix == make_ix("T-0001", "quote", 1, p.aid)
        fut = asyncio.ensure_future(net.result("req", ix, 3.0))
        await _pump(s, fut.done)
        return fut.result(), ix

    res, ix = asyncio.run(main())
    assert res.effect.status is EffectStatus.OK and res.receipt_ok
    assert res.t_sim_ns == net.relay.L_ns(f"{ix}/rt") or abs(res.t_sim_ns - net.relay.L_ns(f"{ix}/rt")) <= 10_000_000
    assert 0.9e9 <= res.t_sim_ns <= 1.11e9


def test_unserved_and_unresolvable() -> None:
    s, net = _net()
    p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(), sched=s)

    async def main():
        await net.register(p)
        ix1 = await net.delegate("req", p.aid, "lidar.mapping", {}, task_id="T-1")
        ix2 = await net.delegate("req", "bafyreinobody", "task.quote", {}, task_id="T-1")
        f1 = asyncio.ensure_future(net.result("req", ix1, 5.0))
        f2 = asyncio.ensure_future(net.result("req", ix2, 5.0))
        await _pump(s, lambda: f1.done() and f2.done())
        return f1.result(), f2.result()

    r1, r2 = asyncio.run(main())
    assert r1.effect.status is EffectStatus.UNAVAILABLE and r1.effect.message == "not served"
    assert r2.effect.status is EffectStatus.UNAVAILABLE


def test_long_task_concurrency_limit() -> None:
    s, net = _net()
    p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(), sched=s, exec_s=100.0)

    async def main():
        await net.register(p)
        ixs = [await net.delegate("req", p.aid, "thermal.imaging", {}, task_id=f"T-{i}") for i in range(5)]
        futs = [asyncio.ensure_future(net.result("req", ix, 60.0)) for ix in ixs]
        await _pump(s, lambda: any(f.done() for f in futs), 5.0)
        done = [f.result() for f in futs if f.done()]
        return done, len(net.daemons[p.aid].long_calls)

    done, n_long = asyncio.run(main())
    # 请求按各自键控延迟到达：最后到达的第 5 个立即 UNAVAILABLE（BUSY），其余 4 个在执行
    assert len(done) == 1 and done[0].effect.status is EffectStatus.UNAVAILABLE and done[0].effect.message == "BUSY"
    assert n_long == 4


def test_long_task_updates_and_tamper() -> None:
    s, _net_unused = _net()
    p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(), sched=s, exec_s=5.0)

    async def run(tamper: bool):
        n = MockNetwork(s, world_seed=5)
        if tamper:
            def t(d, deliv, rc):
                deliv["effect"]["metrics"]["confidence"] = 0.99
                return deliv, rc
            n.tamper = t
        await n.register(p)
        ix = await n.delegate("req", p.aid, "thermal.imaging", {}, task_id="T-9")
        kinds = []

        async def reader():
            kinds.extend([(u.kind, u.phase.value if u.phase else None, s.now_ns()) async for u in n.updates("req", ix)])

        rt = asyncio.ensure_future(reader())
        await _pump(s, rt.done, 30.0)
        return kinds, (await n.result("req", ix, 1.0)), n, ix

    kinds, res, n, ix = asyncio.run(run(False))
    assert [k[0] for k in kinds] == ["first", "progress", "progress", "progress", "final"]
    assert res.receipt_ok and res.effect.status is EffectStatus.OK
    L = n.relay.L_ns(f"{ix}/rt")
    assert kinds[0][2] >= L - n.relay.d_resp_ns  # 请求投递 t_send + L − d_resp，第一段再加 d_resp
    _kinds2, res2, n2, _ = asyncio.run(run(True))
    assert not res2.receipt_ok and res2.effect.status is EffectStatus.FAILED and "RECEIPT_INVALID" in res2.effect.message
    assert n2.stats["receipt_fail"] == 1


def test_cancel_long_task() -> None:
    s, net = _net()
    p = StubProvider("b1", 2, ["thermal.imaging"], quote=q(), sched=s, exec_s=50.0)

    async def main():
        await net.register(p)
        ix = await net.delegate("req", p.aid, "thermal.imaging", {}, task_id="T-5")
        fut = asyncio.ensure_future(net.result("req", ix, 60.0))
        await _pump(s, lambda: s.now_s() > 3.0)
        await net.cancel("req", ix)
        await _pump(s, fut.done)
        return fut.result()

    r = asyncio.run(main())
    assert r.effect.status is EffectStatus.UNVERIFIED and r.effect.message == "canceled"
