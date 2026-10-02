"""sim-core 服务面加固（FX-SIM2；ADR-057、ADR-058）：

- 插件命令生命周期：accepted → running（apply_tick）→ succeeded（OK、V4、simulated、result）；`watch` 判 failed；截止 202；
  剧本重置 canceled；幂等重复返回调用状态（M07-to-M08 第 1 条）；
- agent 角色的 `ctl/sim-core/lease` acquire/release：抢占 MISSION 入栈、release(previous) 弹栈、拒绝抢占 OPERATOR（100）、
  agent 不能做席位操作与非 AGENT 类别（115）；`lease.*` 事件带 by（M14-to-M08 第 1 条）；
- 外部度量接收器 `scenario/metric{value}`：agent 写入、apply_tick 生效、导演读取、未声明 110、viewer 115、重置清空
  （M14-to-M08 第 3 条）；
- 锁步钩子：登记后逐 tick 同步调用（M14-to-M08 第 5 条）；
- state_ext 钩子：登记的钩子原地合并字段（M13-to-M08 第 2 条）；
- 查询在大耗时估价与占满预算的慢任务下仍有界返回（ADR-057）。
"""

from __future__ import annotations

import numpy as np
from simlib import CoreHarness, EventTap

from awr.contracts.enums import Owner
from awr.contracts.reasons import Reason
from awr.sim.core import metrics as MET
from awr.sim.fleet.stages import registry as R


def _agent_principal(h: CoreHarness, cid: str, aid: str = "agent:bafyrei-test") -> dict:
    from awr.runtime.principal import Principal, sign_principal

    p = Principal(aid, "agent", "agent-runtime", None, False)
    d = p.fields()
    d["sig"] = sign_principal(p, cid, h.k_entry)
    return d


# ---------------------------------------------------------------------------------------------- 插件命令生命周期
def test_plugin_command_lifecycle() -> None:
    with R.isolated_registry() as reg:
        state = {"done": False, "fail": False}

        def env_set(msg, apply_tick, ctx):
            return {"status": "accepted", "code": 0, "result": {"version": 7, "t_apply_ns": apply_tick * 4_000_000}}

        def slow_op(msg, apply_tick, ctx):
            def watch(c):
                if state["fail"]:
                    return {"status": "failed", "code": 204, "message": "preempted"}
                return {"status": "succeeded", "result": {"ok": True}, "metrics": {"n": 3}} if state["done"] else None
            return {"status": "accepted", "code": 0, "watch": watch, "deadline_s": 2.0}

        R.register_command_handler("env/set", env_set, owner="M07")
        R.register_command_handler("mission/start", slow_op, owner="M10")
        h = CoreHarness(n=1, reg=reg)
        tap = EventTap(h)
        try:
            adm = h.cmd("env/set", {"patch": {}}, uav=None, cid="e-1")
            assert adm["status"] == "accepted" and adm["detail"]["result"]["version"] == 7
            c = h.call("e-1")
            assert c is not None and c.status == "accepted" and not c.final
            h.advance(0.1)
            assert c.final and c.status == "succeeded" and c.effect["status"] == "OK" and c.effect["simulated"] is True
            assert c.effect["verify_trust"] == 4 and "t_exec_s" in c.effect["metrics"]
            evs = [e for e in tap.items if e.get("cid") == "e-1"]
            assert [e["kind"] for e in evs] == ["cmd.accepted", "cmd.running", "cmd.succeeded"]
            assert evs[-1]["data"]["result"]["version"] == 7 and evs[1]["data"]["effect"]["verify_trust"] == 2
            dup = h.cmd("env/set", {}, uav=None, cid="e-1")
            assert dup["status"] == "duplicate" and dup["call_state"]["status"] == "succeeded"
            # watch：先 running，完成条件成立后 succeeded 并带处理者的 metrics
            assert h.cmd("mission/start", {"mid": "m1"}, uav=None, cid="m-1")["status"] == "accepted"
            h.advance(0.3)
            m1 = h.call("m-1")
            assert m1.status == "running" and not m1.final
            state["done"] = True
            h.advance(0.1)
            assert m1.status == "succeeded" and m1.effect["metrics"]["n"] == 3
            # watch 判 failed
            state["done"] = False
            state["fail"] = True
            h.cmd("mission/start", {"mid": "m2"}, uav=None, cid="m-2")
            h.advance(0.1)
            assert h.call("m-2").status == "failed" and h.call("m-2").code == 204
            # 截止 202
            state["fail"] = False
            h.cmd("mission/start", {"mid": "m3"}, uav=None, cid="m-3")
            assert h.core.engine.extend_deadline("m-3", h.core.clock.t_ns + 3_000_000_000)
            h.advance(2.5)
            assert not h.call("m-3").final
            h.advance(1.0)
            assert h.call("m-3").status == "failed" and h.call("m-3").code == int(Reason.PROGRESS_TIMEOUT)
            # 重置：在途的插件调用 canceled
            h.cmd("mission/start", {"mid": "m4"}, uav=None, cid="m-4")
            m4 = h.call("m-4")
            h.clock("reset")
            assert m4.final and m4.status == "canceled"
        finally:
            h.close()


# ---------------------------------------------------------------------------------------------- agent 租约
def test_agent_lease_acquire_release() -> None:
    h = CoreHarness(n=2, spacing=15.0)
    tap = EventTap(h)
    try:
        a, b = h.ids()
        core = h.core
        sa = h.slot(a)
        # MISSION 持有 a：agent acquire 抢占并入栈
        assert core.lease.acquire(sa, int(Owner.MISSION), "mission:m1") == 0
        cid = "ag-l1"
        rep = core._lease_op({"v": 1, "cid": cid, "op": "acquire", "uav": a, "owner": "AGENT",
                              "principal": _agent_principal(h, cid)})
        assert rep["status"] == "accepted" and rep["lease"]["owner"] == "AGENT", rep
        assert rep["lease"]["holder"] == "agent:bafyrei-test"
        core.events.flush()
        pre = [e for e in tap.items if e.get("kind") == "lease.preempted"]
        acq = [e for e in tap.items if e.get("kind") == "lease.acquired"]
        assert pre and pre[-1]["data"]["owner"] == "MISSION" and acq[-1]["data"]["by"] == "agent:bafyrei-test"
        # 另一 agent 不能释放
        cid = "ag-l2"
        rep = core._lease_op({"v": 1, "cid": cid, "op": "release", "uav": a, "return_to": "previous",
                              "principal": _agent_principal(h, cid, "agent:other")})
        assert rep["code"] == int(Reason.LEASE_DENIED)
        # release(previous) 弹栈回到 MISSION
        cid = "ag-l3"
        rep = core._lease_op({"v": 1, "cid": cid, "op": "release", "uav": a, "return_to": "previous",
                              "principal": _agent_principal(h, cid)})
        assert rep["status"] == "accepted" and rep["lease"]["owner"] == "MISSION" and rep["lease"]["holder"] == "mission:m1"
        # OPERATOR 持有 b：agent acquire 100
        h.takeoff(5.0, vid=b)
        assert core.lease.lease_json(h.slot(b))["owner"] == "OPERATOR"
        cid = "ag-l4"
        rep = core._lease_op({"v": 1, "cid": cid, "op": "acquire", "uav": b, "principal": _agent_principal(h, cid)})
        assert rep["code"] == int(Reason.LEASE_DENIED)
        # agent：席位操作、非 AGENT 类别、伪造签名 115
        for cid, msg in (("ag-l5", {"op": "seat_claim"}), ("ag-l6", {"op": "acquire", "uav": a, "owner": "OPERATOR"})):
            rep = core._lease_op({"v": 1, "cid": cid, **msg, "principal": _agent_principal(h, cid)})
            assert rep["code"] == int(Reason.ROLE_FORBIDDEN), (cid, rep)
        bad = _agent_principal(h, "other-cid")
        rep = core._lease_op({"v": 1, "cid": "ag-l7", "op": "acquire", "uav": a, "principal": bad})
        assert rep["code"] == int(Reason.ROLE_FORBIDDEN)
    finally:
        h.close()


# ---------------------------------------------------------------------------------------------- 外部度量接收器
def test_external_metric_receiver() -> None:
    with MET.isolated_metrics():
        h = CoreHarness(n=1)
        try:
            assert MET.is_external("target_confidence") and MET.is_external("t_conf_s")
            cid = "ag-metric-000001"
            msg = {"v": 1, "cid": cid, "op": "scenario/metric", "uav": None,
                   "args": {"name": "target_confidence", "args": {"target_id": "t1"}, "value": 0.42},
                   "principal": _agent_principal(h, cid), "lease": None, "t_wall_ns": 0, "epoch_seen": 1, "batch_id": None}
            adm = h.core.engine.handle(msg)
            assert adm["status"] == "accepted" and adm["apply_tick"] == h.core.clock.tick + 1, adm
            try:
                MET.metric("target_confidence", target_id="t1")
                raise AssertionError("value visible before apply_tick")
            except LookupError:
                pass
            h.advance(0.02)
            assert MET.metric("target_confidence", target_id="t1") == 0.42
            cid = "ag-metric-000002"
            h.core.engine.handle({**msg, "cid": cid, "principal": _agent_principal(h, cid),
                                  "args": {"name": "t_conf_s", "args": {"target_id": "t1", "threshold": 0.9}, "value": 154.6}})
            h.advance(0.02)
            assert MET.metric("t_conf_s", target_id="t1", threshold=0.9) == 154.6
            # 读：不带 value
            rd = h.cmd("scenario/metric", {"name": "t_conf_s", "args": {"target_id": "t1", "threshold": 0.9}}, uav=None)
            assert rd["status"] == "accepted" and rd["detail"]["value"] == 154.6
            # 计算型度量不可写（110）；viewer 写 115；非有限值 110
            cid = "ag-metric-000003"
            r = h.core.engine.handle({**msg, "cid": cid, "principal": _agent_principal(h, cid),
                                      "args": {"name": "elapsed_s", "args": {}, "value": 1.0}})
            assert r["code"] == int(Reason.PARAM_OUT_OF_RANGE)
            r = h.cmd("scenario/metric", {"name": "target_confidence", "args": {"target_id": "t1"}, "value": 1.0}, uav=None,
                      pid="p-v", role="viewer", seat=False)
            assert r["code"] == int(Reason.ROLE_FORBIDDEN)
            cid = "ag-metric-000004"
            r = h.core.engine.handle({**msg, "cid": cid, "principal": _agent_principal(h, cid),
                                      "args": {"name": "target_confidence", "args": {"target_id": "t1"}, "value": float("nan")}})
            assert r["code"] == int(Reason.PARAM_OUT_OF_RANGE)
            # checkpoint 扩展段带外部度量；重置清空
            from awr.sim.runtime.ckpt import capture_ext

            assert capture_ext(h.core)["metrics"]["target_confidence"]
            h.clock("reset")
            try:
                MET.metric("target_confidence", target_id="t1")
                raise AssertionError("external metric survived reset")
            except LookupError:
                pass
        finally:
            h.close()


# ---------------------------------------------------------------------------------------------- 锁步钩子
def test_post_step_hooks_lockstep() -> None:
    import asyncio

    from awr.agent.runtime.clock import LockstepDriver, SimScheduler

    h = CoreHarness(n=1)
    try:
        sched = SimScheduler(h.core.clock.t_ns)
        drv = LockstepDriver(sched)
        loop = asyncio.new_event_loop()
        seen: list[int] = []
        fired: list[int] = []
        sched.call_at(h.core.clock.t_ns + 100_000_000, lambda: fired.append(sched.now_ns()))

        def hook(t_ns: int, epoch: int, segment: int) -> None:
            seen.append(t_ns)
            loop.run_until_complete(drv.step_to(t_ns, epoch=epoch, segment=segment))

        remove = h.core.register_post_step_hook(hook)
        t0 = h.core.clock.t_ns
        h.advance(0.2)
        assert seen and seen[0] == t0 + 4_000_000 and np.all(np.diff(seen) == 4_000_000), seen[:5]
        assert fired == [t0 + 100_000_000] and sched.now_ns() == seen[-1]
        remove()
        n = len(seen)
        h.advance(0.1)
        assert len(seen) == n
        loop.close()
    finally:
        h.close()


# ---------------------------------------------------------------------------------------------- state_ext 钩子
def test_state_ext_hook_merges_fields() -> None:
    with R.isolated_registry() as reg:
        def hook(slots, t_ns, out):
            for i in range(len(np.asarray(slots))):
                out[i].setdefault("loc", {"status": "TRACKING"}).update({"gnss_fix": 4, "sats": 21, "hdop": 0.7})
                out[i]["sens"] = {"gimbal": [{"sensor_no": 0, "mode": "fixed", "az_rad": 0.0, "el_rad": -0.26,
                                              "limited": False}]}

        R.register_state_ext_hook(hook, owner="M13")
        R.register_state_ext_hook(hook, owner="M13")  # 幂等
        assert len(reg.state_ext_hooks) == 1
        h = CoreHarness(n=2, reg=reg, spacing=15.0)
        try:
            rows = h.core.state_ext_items()
            assert len(rows) == 2 and all(r[1]["loc"]["gnss_fix"] == 4 and r[1]["sens"]["gimbal"] for r in rows)
        finally:
            h.close()


# ---------------------------------------------------------------------------------------------- 查询有界时延
def test_query_bounded_latency_under_slow_estimate() -> None:
    """估价单次耗时远超每轮预算、且查询排在其后时，`ctl/sim-core/query` 仍在有界轮数内得到回复（ADR-057）。"""
    with R.isolated_registry() as reg:
        R.register_query("test/echo", lambda m, c: {"v": 1, "code": 0, "echo": m.get("args")})
        clock = [0]
        h = CoreHarness(n=1, reg=reg)
        try:
            core = h.core
            core.slow.perf_ns = lambda: clock[0]
            core.perf_ns = lambda: clock[0]

            class Req:
                def __init__(self, msg):
                    self.m = msg
                    self.rep = None

                def msg(self):
                    return self.m

                def reply_msg(self, x):
                    self.rep = x

            orig = core._slow_estimate

            def heavy(budget_us):
                out = orig(budget_us)
                clock[0] += 3_000_000  # 3 ms（> 1000 µs 每轮预算）
                return out

            core.slow.get("estimate").fn = heavy
            est = core.slow.get("estimate")
            est.us.extend([3000.0] * 8)
            est.runs = 8
            waits = []
            for k in range(20):
                core._est_q.append((Req({"v": 1, "vehicle_id": h.ids()[0], "target_enu_m": [10.0, 0.0, 20.0]}), 0))
                q = Req({"v": 1, "op": "test/echo", "args": {"k": k}})
                core._query_q.append(q)
                n = 0
                while q.rep is None and n < 50:
                    h.W[0] += 4_000_000
                    core.iterate()
                    n += 1
                waits.append(n)
                assert q.rep == {"v": 1, "code": 0, "echo": {"k": k}}
            assert max(waits) <= 11, waits
            assert core.perf_msg()["slow_ms_per_s"]
        finally:
            h.close()
