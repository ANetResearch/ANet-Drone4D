"""纪元、健康与 TIME（M11-AC-021、AC-022（网关部分）、AC-023、AC-024；M11-FR-047 至 FR-054、FR-059、FR-075；AWR-17 §6.4、
§6.10、§9.6、§9.7）。FakeSim + 真实 Gateway：
- 生产者无 checkpoint 重开（segment + 1）：先收 TIME（epoch + 1）再收 SNAPSHOT；此前心跳停滞过的重开使已 accepted 未终态的
  调用以 `failed 212` 结束；
- checkpoint 恢复（仅生产者 epoch + 1）：全局 epoch 不变，状态 channel 下一条记录 RESET = 1，roster 不受影响；
- api 重启：epoch 不变、sessionId 变化；api 停机期间生产者重开：api 启动即补做 + 1（gw.seen）；
- 伪造与头部矛盾的 `sim.started{reason}`：以头部为准、不追加 + 1、`epoch_mismatch` + 1；
- 心跳停写 → TIME.state STALLED 与 `status proc.sim-core`，恢复后 `removeStatus`；liveliness DELETE 立即 STALLED；supervisor
  报 BACKOFF → RESTARTING、FAILED → FAILED；其他进程非 RUNNING → `status proc.<name>`；布局不一致 → `status ring.layout_mismatch`；
- TIME 的 `t_srv_ns` 等于头部 `heartbeat_ns − gw_t0`；10 个 TimeState 与 bit7 往返；pong 字段；`serverInfo.clock` 随 caps 变化；
  `/api/health/ready` 不可用时 503。
"""

from __future__ import annotations

import asyncio
import time

import fakesim
import httpx
import pytest
import rtc

from awr.api.rt.clock import GatewayClock, Health
from awr.contracts import frame as F
from awr.contracts.enums import TimeState
from awr.runtime.statering import LocalRing, RingHeader


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=2, supervisor=True)
    s.call_in_loop(setattr, s.gw, "seat_grace_s", 0.5)
    yield s
    s.close()


def _order_ok(c: rtc.Client, epoch: int) -> bool:
    """新纪元的 TIME 出现在任何新纪元 BATCH 之前，且新纪元首帧带 SNAPSHOT。"""
    seq: list[tuple[str, int, int]] = []
    for kind, x in c.order:
        if kind == "time":
            seq.append(("t", x.epoch, 0))
        elif kind == "batch":
            seq.append(("b", x.header.epoch, x.header.flags))
    ti = next(i for i, e in enumerate(seq) if e[0] == "t" and e[1] == epoch)
    bi = next(i for i, e in enumerate(seq) if e[0] == "b" and e[1] == epoch)
    return ti < bi and bool(seq[bi][2] & F.BATCH_SNAPSHOT)


def test_crash_restart_time_before_snapshot_and_212(st) -> None:
    tok = rtc.token(st.base, "operator", rtc.hint_of("epochop"))

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        c.order = []
        orig = c.recv

        async def rec(timeout: float = 5.0):
            kind, x = await orig(timeout)
            c.order.append((kind, x))
            return kind, x

        c.recv = rec  # type: ignore[method-assign]
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"}]})
        await c.until(lambda k, x: k == "batch", 3)
        await c.send({"op": "call", "id": "ep-vel-00001", "service": "uav/f001/cmd/velocity", "args": {}})
        await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "ep-vel-00001"
                      and x["status"] == "running", 3)
        e0 = st.gw.clock.global_epoch
        st.sim.hb_paused = True
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == int(TimeState.STALLED), 2)
        st.sim.crash_restart()
        st.sim.hb_paused = False
        r = await c.result("ep-vel-00001", timeout=3)
        assert r["status"] == "failed" and r["code"] == 212
        await c.until(lambda k, x: k == "batch" and x.header.epoch == ((e0 + 1) & 0xFFFF), 3)
        assert st.gw.clock.global_epoch == e0 + 1
        assert _order_ok(c, (e0 + 1) & 0xFFFF)
        assert (st.settings.run_dir / "gw.epoch").read_bytes() == (e0 + 1).to_bytes(4, "little")
        await c.ws.close()

    asyncio.run(run())


def test_scenario_reset_no_212_and_checkpoint_reset_flag(st) -> None:
    tok = rtc.token(st.base, "operator", rtc.hint_of("epochop"))

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"},
                                                  {"id": 2, "topic": "fleet/roster", "rate": 10, "mode": "latest"}]})
        await c.until(lambda k, x: k == "batch", 3)
        e0 = st.gw.clock.global_epoch
        await c.send({"op": "call", "id": "ep-reset-0001", "service": "sim/reset", "args": {}})
        r = await c.result("ep-reset-0001")
        assert r["status"] == "succeeded"
        await c.until(lambda k, x: k == "time" and x.epoch == ((e0 + 1) & 0xFFFF), 3)
        assert st.gw.stats["restarts"] == 1  # 剧本重置不是崩溃重开：不以 212 结束
        # checkpoint 恢复：全局 epoch 不变，swarm 下一条记录 RESET，roster 不受影响
        e1 = st.gw.clock.global_epoch
        n0 = len(c.batches)
        st.sim.checkpoint_restore()
        swarm = c.topic_ids["swarm/uav/state"]
        _, b = await c.until(lambda k, x: k == "batch" and x.by_channel(swarm) is not None
                             and x.by_channel(swarm).rflags & F.RF_RESET, 3)
        assert b.header.epoch == e1 & 0xFFFF and st.gw.clock.global_epoch == e1
        assert all(not (r.rflags & F.RF_RESET) for bb in c.batches[n0:] for r in bb.records
                   if r.channel_id == c.topic_ids["fleet/roster"])
        await c.drain(0.3)
        later = [bb.by_channel(swarm) for bb in c.batches if bb is not b and bb.by_channel(swarm) is not None][-1:]
        assert later and not later[0].rflags & F.RF_RESET  # 只有下一条记录
        await c.ws.close()

    asyncio.run(run())


def test_api_restart_keeps_epoch_and_seen_catchup(st) -> None:
    tok = rtc.token(st.base, "viewer")
    e0 = st.gw.clock.global_epoch
    sid0 = st.gw.session_id
    st.restart_api()
    assert st.gw.clock.global_epoch == e0 and st.gw.session_id != sid0
    st.stop_api()
    st.sim.crash_restart()  # api 停机期间生产者重开
    st.restart_api()
    st.wait(lambda: st.gw.clock.global_epoch == e0 + 1, 3, "api 启动补做纪元 + 1")

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        info = next(m for m in c.texts if m["op"] == "serverInfo")
        assert info["sessionId"] == st.gw.session_id
        await c.until(lambda k, x: k == "time" and x.epoch == ((e0 + 1) & 0xFFFF), 3)
        await c.ws.close()

    asyncio.run(run())


def test_forged_sim_started_counts_mismatch(st) -> None:
    e0 = st.gw.clock.global_epoch
    m0 = st.gw.metrics.counters["epoch_mismatch"]
    st.sim.emit("sim.started", 1, None, epoch=999, segment=st.sim.segment + 5, reason="crash_restart")
    st.wait(lambda: st.gw.metrics.counters["epoch_mismatch"] == m0 + 1, 3, "epoch_mismatch + 1")
    assert st.gw.clock.global_epoch == e0


def test_health_states_and_status(st) -> None:
    tok = rtc.token(st.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.drain(0.3)
        st.sim.hb_paused = True
        t0 = time.monotonic()
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == int(TimeState.STALLED), 2)
        assert time.monotonic() - t0 < 0.6
        assert any(m["op"] == "status" and m["id"] == "proc.sim-core" for m in c.texts)  # 与 TIME 同一 tick 发出
        r = httpx.get(f"{st.base}/api/health/ready", timeout=5)
        assert r.status_code == 503 and r.json()["sim"] == "stalled" and r.json()["code"] == 211
        st.sim.hb_paused = False
        await c.until(lambda k, x: k == "json" and x["op"] == "removeStatus" and "proc.sim-core" in x["ids"], 2)
        r = httpx.get(f"{st.base}/api/health/ready", timeout=5)
        if rtc.world_ready():
            assert r.status_code == 200
        else:  # 没有已构建的深圳（干净克隆、托管 CI）：健康已恢复，只因 world_loaded 为假仍是 503（SHOW-CI）
            assert r.status_code == 503 and r.json()["sim"] == "ok" and r.json()["world_loaded"] is False
        # liveliness DELETE：心跳新鲜也立即 STALLED
        alive = st.sim.bus._handles[0]
        alive.close()
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == int(TimeState.STALLED), 2)
        from awr.contracts import bus_keys

        st.sim.bus._handles[0] = st.sim.bus.token(bus_keys.proc_alive("sim-core"))
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == int(TimeState.PLAYING), 2)
        # supervisor 报 BACKOFF → RESTARTING；FAILED → FAILED；其他进程非 RUNNING → status proc.<name>
        st.sup.set_state("sim-core", "BACKOFF")
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == int(TimeState.RESTARTING), 2)
        st.sup.set_state("sim-core", "FAILED")
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == int(TimeState.FAILED), 2)
        st.sup.set_state("sim-core", "RUNNING")
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == int(TimeState.PLAYING), 2)
        st.sup.set_state("recorder", "BACKOFF")
        _, s1 = await c.until(lambda k, x: k == "json" and x["op"] == "status" and x["id"] == "proc.recorder", 2)
        assert s1["level"] == "warning"
        st.sup.set_state("recorder", "RUNNING")
        await c.until(lambda k, x: k == "json" and x["op"] == "removeStatus" and "proc.recorder" in x["ids"], 2)
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "sys/procs", "rate": 1, "mode": "latest"}]})
        pid = c.topic_ids["sys/procs"]
        await c.until(lambda k, x: k == "batch" and x.by_channel(pid) is not None, 3)
        procs = c.latest_msgpack(pid)
        assert rtc.schema_errors("rt/payloads/sys_procs.schema.json", procs) == []
        await c.ws.close()

    asyncio.run(run())


def test_layout_mismatch_status() -> None:
    s = fakesim.GwStack(start_sim=False)
    try:
        ring = LocalRing.create(s.settings.ring_path, layout_id=0xDEADBEEF)
        tok = rtc.token(s.base, "viewer")

        async def run() -> None:
            c = await rtc.open_client(s, tok["token"])
            _, stt = await c.until(lambda k, x: k == "json" and x["op"] == "status"
                                   and x["id"] == "ring.layout_mismatch", 3)
            assert stt["code"] == 312 and stt["level"] == "error"
            assert c.times and (c.times[-1].state & 0x0F) == int(TimeState.STALLED)
            await c.ws.close()

        asyncio.run(run())
        ring.close()
        LocalRing.remove(s.settings.ring_path)
    finally:
        s.close()


def test_time_synthesis_unit() -> None:
    clk = GatewayClock(None, mono_ns=lambda: 5_000_000_000)
    h = RingHeader.placeholder(heartbeat_ns=7_000_000_000)._replace(t_sim_ns=123, clock_state=int(TimeState.PLAYING),
                                                                     rate_milli=2000)
    clk.update(h, Health.OK)
    t = F.decode_time(clk.time_bytes())
    assert t.t_srv_ns == 2_000_000_000 and t.t_sim_ns == 123 and t.rate == 2.0 and (t.state & 0x0F) == 1
    clk.update(h, Health.STALLED, replay=True)
    t = F.decode_time(clk.time_bytes())
    assert (t.state & 0x0F) == int(TimeState.STALLED) and t.replay
    clk.update(None, Health.RESTARTING)
    t = F.decode_time(clk.time_bytes())
    assert t.t_sim_ns == 0 and (t.state & 0x0F) == int(TimeState.RESTARTING)
    for ts in TimeState:
        for rep in (False, True):
            b = F.encode_time(int(ts), 3, 1.0, 1, 2, replay=rep)
            d = F.decode_time(b)
            assert (d.state & 0x0F) == int(ts) and d.replay == rep


def test_pong_and_clock_caps(st) -> None:
    tok = rtc.token(st.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "ping", "t": 12.5, "srttMs": 2.0})
        _, p = await c.until(lambda k, x: k == "json" and x["op"] == "pong", 2)
        assert p["t"] == 12.5 and p["epoch"] == st.gw.clock.epoch_u16 and p["unix_ns"].isdigit()
        assert 0 < p["server_ns"] < 10**15
        info = next(m for m in c.texts if m["op"] == "serverInfo")
        assert info["clock"]["mode"] == "lockstep"
        with st.sim.lock:
            st.sim.vehicles.append({**st.sim.vehicles[0], "id": "sih1", "agent_no": 50, "caps_ref": "px4_sih",
                                    "backend": "px4_sih"})
            st.sim.roster_version += 1
        _, si = await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x is not info
                              and x["clock"]["mode"] != "lockstep", 3)
        assert si["clock"] == {"mode": "slaved_realtime", "pausable": False, "max_speed": 1, "steppable": False}
        st.sim.remove_vehicle("sih1")
        await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo" and x["clock"]["mode"] == "lockstep", 3)
        await c.ws.close()

    asyncio.run(run())
