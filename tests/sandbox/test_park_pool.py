"""驻留与恢复经真实池事件循环（真实子进程 + 进程内 FakeSim；M17-AC-027、029 的功能部分，D2-AC-45 的进程级口径）：
SB-69 后进程退出、tmpfs 与运行目录清理、槽位释放、驻留目录内容与大小；恢复到 READY、仿真时刻与机体与驻留前一致、epoch + 1、
驻留目录删除；过渡 checkpoint 路径；checkpoint 不生效时退回配置快照并发出 `resume_snapshot`；池被 kill -9 后驻留会话仍可恢复；
驻留保留到期后配置快照仍可取。"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from brokerkit import hint
from sbharness import PoolThread, make_sb, make_settings

from awr.sandbox.config import ParkSettings
from awr.sandbox.park import CKPT_NAME, META_NAME, SNAP_NAME, read_json


def _create(pt: PoolThread, i: int = 1, **req: Any) -> dict:
    return pt.op("create", {"principal_id": "p-" + hint(i), "addr": f"198.51.{i}.7", "req": {"world": "synthcity", **req}})


@pytest.fixture
def pt(tmp_path: Path):
    sb = make_sb(max_sessions=1, spawn={"timeout_s": 4, "stale_s": 0.8, "startup_grace_s": 2})
    s = make_settings(tmp_path, sb, park=ParkSettings(retention_s=600))
    p = PoolThread(s)
    p.launcher.n_vehicles = 2
    yield p
    p.stop()
    p.launcher.close()
    shutil.rmtree(s.shm_root, ignore_errors=True)


def _active(pt: PoolThread, i: int = 1) -> str:
    sid = _create(pt, i)["result"]["session"]["sid"]
    pt.wait(lambda: pt.broker.sessions[sid].state == "READY", 8, "READY")
    pt.op("activity", {"items": {sid: {"ws_n": 1, "api_calls": 1}}})
    pt.wait(lambda: pt.broker.sessions[sid].state == "ACTIVE", 5, "ACTIVE")
    return sid


def _events(pt: PoolThread, kind: str) -> list[dict]:
    return pt.call(lambda: [e["data"] for e in pt.pool._ev if e["kind"] == kind])


def _capture_events(pt: PoolThread) -> list[dict]:
    """池的事件批在每次 flush 时清空：用例期间把发布的批留存下来。"""
    seen: list[dict] = []

    def hook() -> None:
        pub = pt.pool._pub_events
        orig = pub.put

        def put(raw: bytes) -> None:
            from awr.runtime.bus import unpack

            seen.extend(unpack(raw))
            orig(raw)

        pub.put = put

    pt.call(hook)
    return seen


def test_park_and_resume_from_checkpoint(pt: PoolThread) -> None:
    seen = _capture_events(pt)
    sid = _active(pt)
    sim = pt.launcher.sim(sid)
    sim.add_vehicle("f099")
    time.sleep(0.5)
    rec = pt.broker.sessions[sid]
    pid, e0 = rec.pid, rec.epoch
    shm = pt.settings.shm_root / sid
    t0 = time.monotonic()
    r = pt.op("park", {"sid": sid, "reason": "idle"})
    assert r["http"] == 202
    pt.wait(lambda: pt.broker.sessions[sid].state == "PARKED", 6, "PARKED")
    assert time.monotonic() - t0 <= 5.0                                   # PARKING → PARKED ≤ 5 s
    assert not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2]
    assert not shm.exists()
    pd = pt.settings.parked_dir / sid
    assert (pd / CKPT_NAME).exists() and (pd / SNAP_NAME).exists() and (pd / META_NAME).exists()
    assert pt.broker.sessions[sid].park_bytes <= 8 * (1 << 20)
    rd = pt.settings.runs_dir / sid
    assert {p.name for p in rd.iterdir()} <= {"logs", "secret", "session.json"}
    w = pt.op("get", {"sid": sid})["result"]
    assert w["state"] == "PARKED" and w["resume_mode"] == "checkpoint" and "binding" not in w
    t_parked = int(w["t_sim_ns"])
    assert t_parked > 0
    assert pt.op("stats", {})["result"]["parked"] == 1 and pt.op("stats", {})["result"]["active"] == 0
    snap = pt.op("snapshot", {"sid": sid})
    assert snap["http"] == 200 and snap["result"]["world_id"] == "synthcity"
    t1 = time.monotonic()
    res = pt.op("resume", {"sid": sid})
    assert res["http"] == 201 and res["result"]["token"]
    pt.wait(lambda: pt.broker.sessions[sid].state == "READY", 8, "READY after resume")
    assert time.monotonic() - t1 <= 6.0                                   # 恢复到 READY ≤ 6 s
    assert "AWR_RESUME_FROM" in pt.launcher.envs[sid]
    sim2 = pt.launcher.sim(sid)
    assert sim2 is not sim and abs(sim2.t_sim_ns - t_parked) <= 100_000_000
    assert [v["id"] for v in sim2.vehicles] == [v["id"] for v in sim.vehicles] and "f099" in [v["id"] for v in sim2.vehicles]
    pt.wait(lambda: not pt.broker.sessions[sid].resuming, 5, "resume verified")
    rec = pt.broker.sessions[sid]
    assert rec.epoch == e0 + 1 and rec.resume_mode == "checkpoint" and not pd.exists()
    pt.wait(lambda: any(e["kind"] == "sandbox.resumed" for e in seen), 3, "resumed event")
    ev = next(e["data"] for e in seen if e["kind"] == "sandbox.resumed")
    assert ev["resume_mode"] == "checkpoint" and ev["epoch"] == e0 + 1
    assert any(e["kind"] == "sandbox.parked" for e in seen) and any(e["kind"] == "sandbox.parking" for e in seen)
    assert not any(e["kind"] == "session.switched" for e in seen)


def test_transitional_checkpoint_and_snapshot_fallback(pt: PoolThread) -> None:
    seen = _capture_events(pt)
    pt.launcher.formal_ckpt = False                                       # M08 正式接口不在：过渡路径
    sid = _active(pt)
    time.sleep(0.3)
    pt.op("park", {"sid": sid, "reason": "user"})
    pt.wait(lambda: pt.broker.sessions[sid].state == "PARKED", 8, "PARKED")
    assert pt.broker.sessions[sid].resume_mode == "checkpoint"
    assert (pt.settings.parked_dir / sid / CKPT_NAME).exists()
    pt.launcher.fail_restore.add(sid)                                     # checkpoint 不生效（冷启动）
    assert pt.op("resume", {"sid": sid})["http"] == 201
    pt.wait(lambda: pt.broker.sessions[sid].state == "READY" and not pt.broker.sessions[sid].resuming, 10, "resumed")
    assert pt.broker.sessions[sid].resume_mode == "snapshot"
    pt.wait(lambda: any(e["kind"] == "session.switched" for e in seen), 3, "switched")
    sw = next(e["data"] for e in seen if e["kind"] == "session.switched")
    assert sw["reason"] == "resume_snapshot"


def test_incompatible_meta_resumes_from_snapshot(pt: PoolThread) -> None:
    seen = _capture_events(pt)
    sid = _active(pt)
    pt.op("park", {"sid": sid})
    pt.wait(lambda: pt.broker.sessions[sid].state == "PARKED", 8, "PARKED")
    meta_p = pt.settings.parked_dir / sid / META_NAME
    meta = read_json(meta_p)
    meta["release"] = "20260101-000000-0000000000"                        # 代码已更新
    meta_p.write_text(__import__("json").dumps(meta))
    assert pt.op("resume", {"sid": sid})["http"] == 201
    pt.wait(lambda: pt.broker.sessions[sid].state == "READY" and not pt.broker.sessions[sid].resuming, 10, "resumed")
    assert "AWR_RESUME_FROM" not in pt.launcher.envs[sid]
    pt.wait(lambda: any(e["kind"] == "session.switched" for e in seen), 3, "switched")
    assert next(e["data"] for e in seen if e["kind"] == "sandbox.resumed")["resume_mode"] == "snapshot"


def test_resume_queues_when_full_and_claim_spawns_same_sid(pt: PoolThread) -> None:
    sid = _active(pt, 1)
    pt.op("park", {"sid": sid})
    pt.wait(lambda: pt.broker.sessions[sid].state == "PARKED", 8, "PARKED")
    other = _active(pt, 2)
    r = pt.op("resume", {"sid": sid})
    assert r["http"] == 202 and r["result"]["queue"]["kind"] == "resume"
    q = r["result"]["queue"]
    pt.op("stop", {"sid": other})
    pt.wait(lambda: pt.broker.queue_op({"op": "get", "ticket_id": q["ticket_id"], "ticket_token": q["ticket_token"]})
            ["result"]["state"] == "slot_ready", 10, "slot_ready")
    c = pt.op("queue", {"op": "claim", "ticket_id": q["ticket_id"], "ticket_token": q["ticket_token"]})
    assert c["http"] == 201 and c["result"]["session"]["sid"] == sid
    pt.wait(lambda: pt.broker.sessions[sid].state == "READY", 8, "READY")


def test_parked_survives_pool_crash(tmp_path: Path) -> None:
    sb = make_sb(max_sessions=1, spawn={"timeout_s": 4, "stale_s": 0.8, "startup_grace_s": 2})
    s = make_settings(tmp_path, sb)
    p = PoolThread(s)
    try:
        sid = _active(p)
        p.op("park", {"sid": sid})
        p.wait(lambda: p.broker.sessions[sid].state == "PARKED", 8, "PARKED")
        p.call(lambda: p.pool.store.save(p.broker.dump()))
        p.crash()
        p2 = PoolThread(s, launcher=p.launcher)
        try:
            assert p2.state(sid) == "PARKED"
            assert p2.op("resume", {"sid": sid})["http"] == 201
            p2.wait(lambda: p2.broker.sessions[sid].state == "READY", 8, "READY after pool restart")
        finally:
            p2.stop()
    finally:
        p.launcher.close()
        shutil.rmtree(s.shm_root, ignore_errors=True)


def test_retention_end_keeps_snapshot(tmp_path: Path) -> None:
    sb = make_sb(max_sessions=1, spawn={"timeout_s": 4, "stale_s": 0.8, "startup_grace_s": 2})
    s = make_settings(tmp_path, sb, park=ParkSettings(retention_s=1.5))
    p = PoolThread(s)
    try:
        sid = _active(p)
        p.op("park", {"sid": sid})
        p.wait(lambda: p.broker.sessions[sid].state == "ENDED", 10, "ENDED after retention")
        w = p.op("get", {"sid": sid})["result"]
        assert w["end_reason"] == "parked_expired" and not (s.parked_dir / sid).exists()
        snap = p.op("snapshot", {"sid": sid})
        assert snap["http"] == 200 and snap["result"]["world_id"] == "synthcity"
        assert p.op("resume", {"sid": sid})["code"] == 506
    finally:
        p.stop()
        p.launcher.close()
        shutil.rmtree(s.shm_root, ignore_errors=True)


def test_checkpoint_failure_parks_with_snapshot(pt: PoolThread) -> None:
    seen = _capture_events(pt)
    sid = _active(pt)
    pt.launcher.fail_ckpt.add(sid)                                       # 两条 checkpoint 路径都失败：以配置快照驻留
    pt.op("park", {"sid": sid, "reason": "idle"})
    pt.wait(lambda: pt.broker.sessions[sid].state == "PARKED", 8, "PARKED")
    rec = pt.broker.sessions[sid]
    assert rec.resume_mode == "snapshot" and not (pt.settings.parked_dir / sid / CKPT_NAME).exists()
    assert (pt.settings.parked_dir / sid / SNAP_NAME).exists()
    pt.launcher.fail_ckpt.discard(sid)
    assert pt.op("resume", {"sid": sid})["http"] == 201
    pt.wait(lambda: pt.broker.sessions[sid].state == "READY" and not pt.broker.sessions[sid].resuming, 10, "resumed")
    assert "AWR_RESUME_FROM" not in pt.launcher.envs[sid]
    pt.wait(lambda: any(e["kind"] == "sandbox.resumed" for e in seen), 3, "resumed event")
    assert next(e["data"] for e in seen if e["kind"] == "sandbox.resumed")["resume_mode"] == "snapshot"
    assert not any(e["kind"] == "session.switched" for e in seen)       # 计划即为快照口径：不是"退回"


def test_gc_keeps_shm_dirs_of_other_pools(pt: PoolThread) -> None:
    """共用的 tmpfs 根：另一个池（或测试栈）的会话目录仍有进程在运行时不被回收；无进程的孤儿目录照常删除。"""
    import subprocess
    import sys

    other = pt.settings.shm_root / "sb-otherpool1"
    dead = pt.settings.shm_root / "sb-orphanaaaa"
    for d in (other, dead):
        (d / "ckpt").mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env={"AWR_RUN": "sb-otherpool1"})
    try:
        time.sleep(0.2)
        pt.call(pt.pool._gc_orphans)
        assert other.exists() and not dead.exists()
    finally:
        proc.kill()
        proc.wait()
