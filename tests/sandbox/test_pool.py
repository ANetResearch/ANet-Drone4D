"""池进程逻辑的集成用例（真实 Pool 事件循环 + 真实子进程 + 进程内 FakeSim；M17-AC-001、002、005、010、015、016、023 的功能部分）：
拉起到 READY、首个活动启动时钟（K_entry 签名）、kill -9 后重启（epoch + 1）与第 3 次故障 FAILED、挂死判定、结束清理与快照留存、
池重启接管、维护停机、拉起超时、切换底图与回滚、载入模板、池命名空间验签、运维操作。"""

from __future__ import annotations

import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any

import pytest
from brokerkit import hint
from sbharness import PoolThread, make_sb, make_settings

from awr.contracts import bus_keys
from awr.runtime.bus import LocalBus
from awr.runtime.sbauth import sign_pool_request


def _create(pt: PoolThread, i: int = 1, *, world: str = "synthcity", **req: Any) -> dict:
    return pt.op("create", {"principal_id": "p-" + hint(i), "addr": f"198.51.{i}.7", "req": {"world": world, **req}})


@pytest.fixture
def pt(tmp_path: Path):
    sb = make_sb(max_sessions=3, spawn={"timeout_s": 3, "stale_s": 0.6, "startup_grace_s": 1, "crash_window_s": 60})
    s = make_settings(tmp_path, sb)
    p = PoolThread(s)
    yield p
    p.stop()
    p.launcher.close()
    shutil.rmtree(s.shm_root, ignore_errors=True)


def _ready(pt: PoolThread, sid: str, timeout: float = 8.0) -> None:
    pt.wait(lambda: pt.broker.sessions[sid].state in ("READY", "ACTIVE", "IDLE"), timeout, f"{sid} READY")


def test_spawn_ready_play_and_end(pt: PoolThread) -> None:
    r = _create(pt)
    assert r["http"] == 201
    sid = r["result"]["session"]["sid"]
    _ready(pt, sid)
    rec = pt.broker.sessions[sid]
    assert rec.pid and rec.pid_starttime and Path(f"/proc/{rec.pid}").exists()
    lst = pt.op("list", {})["result"]["sessions"]
    assert lst[0]["binding"]["ring_path"].endswith(f"/{sid}/state.sim-core")
    pt.op("activity", {"items": {sid: {"ws_n": 1, "api_calls": 2}}})
    sim = pt.launcher.sim(sid)
    pt.wait(lambda: any(m.get("op") == "play" for m in sim.clock_ops), 5, "play")
    play = next(m for m in sim.clock_ops if m.get("op") == "play")
    assert sim._verified(play) and play["principal"]["principal_id"] == "p-" + hint(1)
    pid = rec.pid
    rd = pt.settings.runs_dir / sid
    assert rd.is_dir()
    assert pt.op("stop", {"sid": sid})["http"] == 202
    pt.wait(lambda: pt.broker.sessions[sid].state == "ENDED", 10, "ENDED")
    assert not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2]
    assert not rd.exists() and not (pt.settings.shm_root / sid).exists()
    snap = pt.op("snapshot", {"sid": sid})
    assert snap["http"] == 200 and snap["result"]["world_id"] == "synthcity"
    ev = [e["kind"] for e in pt.pool._ev] if pt.pool._ev else []
    assert isinstance(ev, list)


def test_crash_restart_and_third_failure(pt: PoolThread) -> None:
    sid = _create(pt)["result"]["session"]["sid"]
    _ready(pt, sid)
    pt.op("activity", {"items": {sid: {"ws_n": 1, "api_calls": 1}}})
    e0 = pt.broker.sessions[sid].epoch
    for n in (1, 2):
        pid = pt.broker.sessions[sid].pid
        t0 = time.monotonic()
        os.kill(pid, signal.SIGKILL)
        pt.wait(lambda pid=pid, n=n: (r := pt.broker.sessions[sid]).pid not in (None, pid) and r.state == "ACTIVE"
                and r.epoch == e0 + n, 8, f"restart {n}")
        assert time.monotonic() - t0 <= 6.0
        assert pt.broker.sessions[sid].restarts == n
        rows = {r["sid"]: r for r in pt.op("ops", {"cmd": "status"})["result"]["sessions"]}
        assert rows[sid]["restarts"] == n                       # 运维状态行（D2-AC-27 观测工具，d2-FX-R2-deploy-to-M17 第 1 条）
    os.kill(pt.broker.sessions[sid].pid, signal.SIGKILL)
    pt.wait(lambda: pt.broker.sessions[sid].state == "FAILED", 10, "FAILED")
    assert pt.op("get", {"sid": sid})["result"]["end_reason"] == "failed"


def test_hung_is_killed_and_restarted(pt: PoolThread) -> None:
    sid = _create(pt)["result"]["session"]["sid"]
    _ready(pt, sid)
    pt.op("activity", {"items": {sid: {"ws_n": 1, "api_calls": 1}}})
    time.sleep(1.2)                                   # 过了启动宽限
    pid = pt.broker.sessions[sid].pid
    pt.launcher.sim(sid).hb_paused = True
    pt.wait(lambda: pt.broker.sessions[sid].pid not in (None, pid) and pt.broker.sessions[sid].restarts == 1, 10, "hung")
    assert pt.pool.metrics["hung"] >= 1


def test_starved_session_gets_longer_heartbeat_tolerance(pt: PoolThread) -> None:
    """D2-FX-R2（M17-FR-008 的饥饿口径）：沙盒组处于 CPU 限流或压力之下且进程仍在消耗 CPU 时，心跳容限放宽到 5 × stale_s；
    超过放宽后的容限仍判挂死。"""
    sid = _create(pt)["result"]["session"]["sid"]
    _ready(pt, sid)
    pt.op("activity", {"items": {sid: {"ws_n": 1, "api_calls": 1}}})
    time.sleep(1.2)                                   # 过了启动宽限
    cpu = [0.0]

    def leaf_stats(_sid: str, _pid: int | None) -> dict:
        cpu[0] += 0.5                                 # 每个检查周期 0.5 核：仍在运行
        return {"cpu_s": cpu[0]}

    pt.call(lambda: (setattr(pt.pool, "_starved", lambda: True), setattr(pt.pool.cgroups, "leaf_stats", leaf_stats)))
    pid = pt.broker.sessions[sid].pid
    pt.launcher.sim(sid).hb_paused = True
    time.sleep(2.0)                                   # 0.6 s 的 3 倍以上：放宽前早已判挂死
    assert pt.broker.sessions[sid].pid == pid and pt.pool.metrics["hung"] == 0 and pt.pool.metrics["late_heartbeat"] >= 1
    pt.wait(lambda: pt.broker.sessions[sid].pid not in (None, pid), 10, "hung after the starved tolerance (3 s)")
    assert pt.pool.metrics["hung"] >= 1


def test_spawn_timeout_fails_and_releases(pt: PoolThread) -> None:
    pt.launcher.fail_worlds.add("suzhou")
    sid = _create(pt, world="suzhou")["result"]["session"]["sid"]
    pt.wait(lambda: pt.broker.sessions[sid].state == "FAILED", 12, "FAILED")
    assert pt.broker.n_live() == 0 and not (pt.settings.runs_dir / sid).exists()


def test_world_switch_and_rollback(pt: PoolThread) -> None:
    sid = _create(pt)["result"]["session"]["sid"]
    _ready(pt, sid)
    ns0 = pt.op("get", {"sid": sid})["result"]["binding"]["namespace"]
    r = pt.op("world", {"sid": sid, "world": "shenzhen"})
    assert r["http"] == 202 and r["result"]["session"]["state"] == "SPAWNING"
    pt.wait(lambda: pt.broker.sessions[sid].state == "READY" and pt.broker.sessions[sid].world == "shenzhen", 10, "switched")
    b = pt.op("get", {"sid": sid})["result"]["binding"]
    assert b["namespace"] == f"awr/shenzhen/{sid}" != ns0 and b["secret_path"].endswith(f"{sid}/secret")
    assert ("sb" not in sid) is False and pt.launcher.launches[-1][:2] == (sid, "shenzhen")
    pt.launcher.fail_worlds.add("newyork")
    assert pt.op("world", {"sid": sid, "world": "newyork"})["http"] == 202
    pt.wait(lambda: pt.broker.sessions[sid].state == "READY" and pt.broker.sessions[sid].world == "shenzhen"
            and pt.broker.sessions[sid].switch is None, 15, "rollback")
    assert [x[1] for x in pt.launcher.launches if x[0] == sid][-2:] == ["newyork", "shenzhen"]


def test_template_reset_reaches_sim(pt: PoolThread) -> None:
    sid = _create(pt)["result"]["session"]["sid"]
    _ready(pt, sid)
    assert pt.op("template", {"sid": sid, "template": "t1_point_watch"})["http"] == 202
    sim = pt.launcher.sim(sid)
    pt.wait(lambda: any(m.get("op") == "reset" for m in sim.clock_ops), 5, "reset")
    m = next(m for m in sim.clock_ops if m.get("op") == "reset")
    assert m["args"]["sandbox_config"]["template_id"] == "t1_point_watch" and sim._verified(m)
    cfg = (pt.settings.runs_dir / sid / "session.json").read_text()
    assert "t1_point_watch" in cfg


def test_pool_crash_and_adoption(tmp_path: Path) -> None:
    sb = make_sb(max_sessions=1, spawn={"timeout_s": 3})
    s = make_settings(tmp_path, sb)
    p1 = PoolThread(s)
    launcher = p1.launcher
    try:
        sid = _create(p1)["result"]["session"]["sid"]
        _ready(p1, sid)
        p1.op("activity", {"items": {sid: {"ws_n": 1, "api_calls": 1}}})
        q = _create(p1, 2)
        assert q["http"] == 202
        t = q["result"]["queue"]
        pid, epoch = p1.broker.sessions[sid].pid, p1.broker.sessions[sid].epoch
        p1.call(lambda: p1.pool.store.save(p1.broker.dump()))
        creates = dict(p1.broker.creates)
        p1.crash()
        assert Path(f"/proc/{pid}").exists()                 # 池崩溃不连带沙盒
        t0 = time.monotonic()
        p2 = PoolThread(s, namespace=p1.namespace, launcher=launcher)
        assert time.monotonic() - t0 < 10
        rec = p2.broker.sessions[sid]
        assert rec.pid == pid and rec.state == "ACTIVE" and rec.epoch == epoch and p2.pool.metrics["adopted"] == 1
        assert p2.broker.creates == creates
        v = p2.op("queue", {"op": "get", "ticket_id": t["ticket_id"], "ticket_token": t["ticket_token"]})
        assert v["result"]["state"] == "waiting"
        assert p2.op("stop", {"sid": sid})["http"] == 202
        p2.wait(lambda: p2.broker.sessions[sid].state == "ENDED", 10, "ENDED after adoption")
        assert not (s.runs_dir / sid).exists()
        p2.wait(lambda: not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2], 5, "exit")
        p2.stop()
    finally:
        launcher.close()
        shutil.rmtree(s.shm_root, ignore_errors=True)


def test_adoption_rejects_forged_pid(tmp_path: Path) -> None:
    sb = make_sb(max_sessions=2, spawn={"timeout_s": 3})
    s = make_settings(tmp_path, sb)
    p1 = PoolThread(s)
    launcher = p1.launcher
    try:
        sid = _create(p1)["result"]["session"]["sid"]
        _ready(p1, sid)
        p1.op("activity", {"items": {sid: {"ws_n": 1, "api_calls": 1}}})
        real = p1.broker.sessions[sid].pid

        def forge() -> None:
            p1.broker.sessions[sid].pid_starttime = (p1.broker.sessions[sid].pid_starttime or 0) + 99
            p1.pool.store.save(p1.broker.dump())

        p1.call(forge)
        p1.crash()
        p2 = PoolThread(s, namespace=p1.namespace, launcher=launcher)
        assert p2.pool.metrics["adopted"] == 0
        p2.wait(lambda: (r := p2.broker.sessions[sid]).pid not in (None, real) and r.restarts == 1, 10, "restart")
        os.kill(real, signal.SIGKILL)
        p2.stop()
    finally:
        launcher.close()
        shutil.rmtree(s.shm_root, ignore_errors=True)


def test_maintenance_stop_exports_snapshots(tmp_path: Path) -> None:
    s = make_settings(tmp_path, make_sb(max_sessions=2, spawn={"timeout_s": 3}))
    p = PoolThread(s)
    try:
        sids = [_create(p, i)["result"]["session"]["sid"] for i in (1, 2)]
        for sid in sids:
            _ready(p, sid)
        pids = [p.broker.sessions[x].pid for x in sids]
        t0 = time.monotonic()
        p.stop(maintenance=True)
        assert time.monotonic() - t0 < 12
        for pid in pids:
            assert not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2]
        doc = __import__("json").loads((s.state_dir / "state.json").read_text())
        st = {d["sid"]: d for d in doc["sessions"]}
        assert all(st[x]["state"] == "ENDED" and st[x]["end_reason"] == "maintenance" and st[x]["snapshot_path"] for x in sids)
        assert all(Path(st[x]["snapshot_path"]).exists() for x in sids) and doc["accepting"] is True
    finally:
        p.launcher.close()
        shutil.rmtree(s.shm_root, ignore_errors=True)


def test_signature_required_and_ops(pt: PoolThread) -> None:
    import asyncio

    async def call(op: str, msg: Any) -> Any:
        bus = LocalBus.open("tester", namespace=pt.namespace, announce=False)
        try:
            return await bus.call(bus_keys.sys_session(op), msg, timeout=2.0, retries=0)
        finally:
            bus.close()

    bad = asyncio.run(call("list", {"v": 1, "cid": "x", "principal": "svc:sandbox-api", "ts": time.time(), "args": {}, "sig": "00"}))
    assert bad["status"] == "rejected" and bad["code"] == 115
    stale = sign_pool_request(pt.pool.keys.k_pool, "list", {}, now_s=time.time() - 30)
    assert asyncio.run(call("list", stale))["code"] == 115
    other_op = sign_pool_request(pt.pool.keys.k_pool, "get", {})
    assert asyncio.run(call("list", other_op))["code"] == 115         # 签名覆盖 op，不能跨 op 重放
    ok = asyncio.run(call("list", sign_pool_request(pt.pool.keys.k_pool, "list", {})))
    assert ok["status"] == "ok" and ok["result"]["n_max"] == 3
    ctl = sign_pool_request(pt.pool.keys.k_pool, "ops", {"cmd": "status"}, principal="svc:sandbox-ctl")
    assert asyncio.run(call("ops", ctl))["result"]["n_max"] == 3
    ctl_create = sign_pool_request(pt.pool.keys.k_pool, "create", {}, principal="svc:sandbox-ctl")
    assert asyncio.run(call("create", ctl_create))["code"] == 115      # 运维 principal 只能调用运维与只读 op
    assert pt.op("ops", {"cmd": "accept", "on": False})["result"]["accepting"] is False
    r = _create(pt)
    assert r["http"] == 503 and r["code"] == 502 and r["detail"]["why"] == "maintenance"
    assert pt.op("ops", {"cmd": "accept", "on": True})["result"]["accepting"] is True
    assert pt.op("ops", {"cmd": "limits", "max_sessions": 1})["result"]["n_max"] == 1
    assert pt.op("ops", {"cmd": "limits", "max_sessions": "auto"})["result"]["n_max"] == 3
