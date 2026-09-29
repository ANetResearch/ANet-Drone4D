"""M14-AC-028：受信守卫流水线（G0、①、②、③、A1–A4 各有拒绝用例并返回规定码；检查器异常 470 且 bus 上无消息；审计全记录）。"""

from __future__ import annotations

import asyncio
import math
import time

import pytest
from fakes.s3 import build_s3, run, start

from awr.agent.guard.policy import Envelope
from awr.agent.guard.ratelimit import RateLimiter

IX = "ix_guardtest"


def _setup(*, lease: bool = True):
    sched, fake, core = build_s3()
    g = core.guard

    async def main():
        await start(core, fake)
        await run(sched, fake, t_end_s=45.0)  # b1 已在空中（MISSION）
        aid = core.by_vehicle["p600-b1"]
        g.limiter = RateLimiter(rate_total=1e6, burst_total=1e6, rate_aid=1e6, burst_aid=1e6)
        g.open_scope(aid, IX, "thermal.imaging", core.cat.ops("thermal.imaging"))
        g.set_envelope(aid, IX, Envelope((-24.0, -1253.0), 84.0, 0.0, 30.0, 120.0, None))
        if lease:
            adm = await g.submit(aid, "acquire", "p600-b1", {}, ix=IX)
            assert adm["status"] == "accepted"
        return aid

    loop = asyncio.new_event_loop()
    aid = loop.run_until_complete(main())
    return loop, sched, fake, core, g, aid


CASES = [
    ("G0 safety_stop", "safety_stop", {}, 115), ("G0 velocity", "velocity", {}, 115), ("G0 env/set", "env/set", {}, 115),
    ("G0 fleet", "fleet/cmd/rtl", {}, 115), ("G0 NaN", "goto", {"pos": [math.nan, 0.0, 60.0]}, 110),
    ("G0 extra field", "goto", {"pos": [-24.0, -1253.0, 60.0], "evil": 1}, 300),
    ("G0 waypoints 65", "follow_path", {"waypoints": [[-24.0, -1253.0, 60.0]] * 65}, 110),
    ("G0 speed > 12", "goto", {"pos": [-24.0, -1253.0, 60.0], "speed_mps": 20.0}, 110),
    ("③ kill", "kill", {}, 115), ("③ escalate", "escalate", {}, 115), ("③ override acquire", "acquire", {"priority": "override"}, 115),
    ("A1 op not in capability", "follow_path", {"waypoints": [[-24.0, -1253.0, 60.0], [-20.0, -1250.0, 60.0]]}, 482),
    ("A1 foreign cancel", "cancel", {"call_id": "someone-else"}, 482),
    ("A3 far goto", "goto", {"pos": [200.0, -1253.0, 60.0]}, 110),
    ("A3 too high", "goto", {"pos": [-24.0, -1253.0, 500.0]}, 110),
    ("A3 orbit far", "orbit", {"center": [-24.0, -1100.0, 60.0], "radius_m": 20.0}, 110),
]


@pytest.mark.parametrize("name,op,args,code", CASES, ids=[c[0] for c in CASES])
def test_rejections(name: str, op: str, args: dict, code: int) -> None:
    loop, _s, fake, core, g, aid = _setup()
    n_cmd = fake.counts["command"]
    adm = loop.run_until_complete(g.submit(aid, op, "p600-b1", args, ix=IX))
    assert adm["status"] == "rejected" and adm["code"] == code, adm
    assert fake.counts["command"] == n_cmd  # 被拒命令不发出任何 bus 消息
    assert core.ledger(aid).rows[-1]["type"] == "agent.guard.rejected"
    loop.close()


def test_identity_role_session() -> None:
    loop, _s, _f, core, g, aid = _setup()
    run_ = loop.run_until_complete
    assert run_(g.submit("bafyreinotregistered", "hover", "p600-b1", {}, ix=IX))["code"] == 115
    assert run_(g.submit(core.coord_aid, "hover", "p600-b1", {}, ix=IX))["code"] == 115
    assert run_(g.submit(aid, "goto", "p600-b2", {"pos": [-24.0, -1253.0, 60.0]}, ix=IX))["code"] == 482  # 他机
    assert run_(g.submit(aid, "hover", "p600-b1", {}, ix="ix_none"))["code"] == 482  # 不属于活动委派
    core.session_state = "replay"
    assert run_(g.submit(aid, "hover", "p600-b1", {}, ix=IX))["code"] == 118
    loop.close()


def test_lease_precondition_a2() -> None:
    loop, _s, _f, _core, g, aid = _setup(lease=False)
    assert loop.run_until_complete(g.submit(aid, "goto", "p600-b1", {"pos": [-24.0, -1253.0, 60.0]}, ix=IX))["code"] == 100
    assert loop.run_until_complete(g.submit(aid, "hover", "p600-b1", {}, ix=IX))["code"] == 100
    assert loop.run_until_complete(g.submit(aid, "release", "p600-b1", {}, ix=IX))["code"] == 100
    loop.close()


def test_unhealthy_only_safety_a4() -> None:
    loop, _s, fake, _core, g, aid = _setup()
    fake.vehicles["p600-b1"].lifecycle = "DEGRADED"
    assert loop.run_until_complete(g.submit(aid, "goto", "p600-b1", {"pos": [-24.0, -1253.0, 60.0]}, ix=IX))["code"] == 105
    assert loop.run_until_complete(g.submit(aid, "hover", "p600-b1", {}, ix=IX))["status"] == "accepted"
    loop.close()


def test_rate_limit_111() -> None:
    loop, _s, _f, _core, g, aid = _setup()
    g.limiter = RateLimiter(mono_ns=lambda: 1_000_000_000)  # 冻结墙钟：只剩突发额度
    codes = [loop.run_until_complete(g.submit(aid, "hover", "p600-b1", {}, ix=IX))["code"] for _ in range(12)]
    assert codes[:10] == [0] * 10 and codes[10:] == [111, 111]
    loop.close()


def test_internal_error_470_no_bus_message() -> None:
    loop, _s, fake, _core, g, aid = _setup()

    def boom(_op: str) -> None:
        raise RuntimeError("checker bug")

    g.fault_inject = boom
    n = fake.counts["command"] + fake.counts["lease"]
    adm = loop.run_until_complete(g.submit(aid, "hover", "p600-b1", {}, ix=IX))
    assert adm["code"] == 470 and fake.counts["command"] + fake.counts["lease"] == n
    loop.close()


def test_audit_records_allow_and_deny() -> None:
    loop, _s, _f, _core, g, aid = _setup()
    before = len(g.audit.rows)
    loop.run_until_complete(g.submit(aid, "hover", "p600-b1", {}, ix=IX))
    loop.run_until_complete(g.submit(aid, "safety_stop", "p600-b1", {}, ix=IX))
    rows = g.audit.rows[before:]
    assert [r["status"] for r in rows] == ["accepted", "rejected"]
    assert rows[0]["principal_id"] == f"agent:{aid}" and rows[1]["code"] == 115
    loop.close()


@pytest.mark.perf
def test_guard_p99_under_1ms() -> None:
    loop, _s, _f, _core, g, aid = _setup()
    ts = []
    for _ in range(10_000):
        t0 = time.perf_counter()
        g.check(aid, "orbit", "p600-b1", {"center": [-24.0, -1253.0, 60.0], "radius_m": 20.0}, IX)
        g.principal(aid, "c")
        ts.append(time.perf_counter() - t0)
    ts.sort()
    assert ts[int(0.99 * len(ts))] <= 1e-3
    loop.close()
