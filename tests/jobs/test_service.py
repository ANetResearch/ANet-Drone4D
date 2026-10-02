"""job-worker 服务面（M03-FR-055、FR-057；M01-to-M03 第 4 条；INT-1 §7.5；FX-SIM2）：

- `svc/job/{submit,cancel,status,engines}` 经 LocalBus 往返：提交（124 冲突、未知类型 300）、状态（未知 347）、日志尾部、
  取消（QUEUED 直接 CANCELLED、执行中置 cancel_requested、终态 348）、重试（只接受 FAILED 且 resumable）；
- 主循环：按任务类型的首阶段认领、`JobContext` 在进度回调中写心跳（≥ 1 s 节流）、runner 的 `error` 原样写入 error_json、
  `job.state`/`job.progress` 事件（world_build 等由 worker 代发）；
- 崩溃标记为 344 JOB_WORKER_CRASHED。
"""

from __future__ import annotations

import asyncio
import json
import secrets

import msgpack
import pytest

from awr.contracts import bus_keys
from awr.jobs.context import JobCancelled
from awr.jobs.registry import _REGISTRY, register_job
from awr.jobs.worker import Worker
from awr.runtime.bus import LocalBus

STAGES = ("A", "B")


@pytest.fixture
def fake_kinds():
    calls: list = []

    @register_job("t_ok", STAGES)
    def ok(ctx, params):
        t = [1e9]

        def clock() -> float:  # 假墙钟：每次读取前进 2 s，心跳节流（≥ 1 s）逐次放行
            t[0] += 2.0
            return t[0]

        ctx._clock = clock
        for st in STAGES:
            with ctx.stage(st):
                for k in range(5):
                    calls.append((st, k))
                    ctx.progress((k + 1) / 5)
                    ctx.check_cancel()
        ctx.log("info", "done")
        return {"exit_code": 0, "world_id": ctx.world_id, "published": False}

    @register_job("t_fail", STAGES)
    def fail(ctx, params):
        with ctx.stage("A"):
            pass
        return {"exit_code": 3, "error_code": 342, "error": {"code": 342, "name": "RECON_TILING_FAILED", "stage": "A",
                                                             "resumable": True, "detail": "disk"}}

    @register_job("t_cancel", STAGES)
    def cancel(ctx, params):
        with ctx.stage("A"):
            raise JobCancelled(ctx.job_id)

    yield calls
    for k in ("t_ok", "t_fail", "t_cancel"):
        _REGISTRY.pop(k, None)


class Rig:
    def __init__(self, tmp_path) -> None:
        self.ns = "awr/test/job" + secrets.token_hex(3)
        self.bus = LocalBus.open("job-worker", namespace=self.ns)
        self.beats = 0
        self.w = Worker(tmp_path / "jobs.sqlite", bus=self.bus, heartbeat=self, epoch=1, worlds_dir=tmp_path / "worlds")
        self.events: list[dict] = []
        for cat in ("job", "world"):
            self.bus.subscribe(bus_keys.evt("job-worker", cat), lambda k, raw: self.events.extend(msgpack.unpackb(raw, raw=False)))
        self.w.start()
        self.client = None

    def beat(self) -> None:
        self.beats += 1

    def call(self, op: str, msg: dict) -> dict:
        async def go():
            c = LocalBus.open("api", namespace=self.ns, loop=asyncio.get_running_loop())
            try:
                return await c.call(bus_keys.svc_job(op), msg, timeout=3.0, retries=0)
            finally:
                c.close()
        return asyncio.run(go())

    def kinds(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("kind") == kind]

    def close(self) -> None:
        self.w.close()
        self.bus.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_submit_run_status_log_and_events(rig, fake_kinds) -> None:
    rep = rig.call("submit", {"kind": "t_ok", "target_world_id": "w-ok", "params": {}, "submitted_by": "p-op"})
    assert rep["state"] == "QUEUED" and rep["job_id"].startswith("j-"), rep
    jid = rep["job_id"]
    assert rig.call("submit", {"kind": "t_ok", "target_world_id": "w-ok", "params": {}})["code"] == 124
    assert rig.call("submit", {"kind": "nope", "target_world_id": "w-x"})["code"] == 300
    assert rig.w.step()
    st = rig.call("status", {"job_id": jid})
    assert st["state"] == "SUCCEEDED" and st["progress_pct"] == 100.0 and st["attempt"] == 1, st
    assert rig.beats >= 3, rig.beats                     # 进度回调写心跳（INT-1 §7.5）
    assert rig.call("status", {"job_id": "j-00000000-0000-7000-8000-000000000000"})["code"] == 347
    assert any(i["job_id"] == jid for i in rig.call("status", {})["items"])
    lines = rig.call("status", {"job_id": jid, "tail": 10})["lines"]
    assert lines and "done" in lines[-1]
    states = [e["data"]["state"] for e in rig.kinds("job.state") if e["data"]["job_id"] == jid]
    assert states[0] == "QUEUED" and states[-1] == "SUCCEEDED" and "A" in states and "B" in states, states
    assert rig.kinds("job.progress")
    assert rig.call("cancel", {"job_id": jid})["code"] == 348


def test_failure_error_json_and_retry(rig, fake_kinds) -> None:
    jid = rig.call("submit", {"kind": "t_fail", "target_world_id": "w-f", "params": {}, "submitted_by": "p-op"})["job_id"]
    rig.w.step()
    st = rig.call("status", {"job_id": jid})
    assert st["state"] == "FAILED" and st["resumable"] and st["error"]["code"] == 342 and st["error"]["stage"] == "A", st
    row = rig.w.q.get(jid)
    assert json.loads(row["error_json"])["name"] == "RECON_TILING_FAILED"
    assert rig.call("submit", {"retry_of": jid})["state"] == "QUEUED"
    assert rig.w.q.get(jid)["state"] == "QUEUED"
    rig.w.step()
    assert rig.w.q.get(jid)["attempt"] == 2
    ok = rig.call("submit", {"kind": "t_ok", "target_world_id": "w-o2", "params": {}})["job_id"]
    rig.w.step()
    assert rig.call("submit", {"retry_of": ok})["code"] == 348


def test_cancel_queued_and_running(rig, fake_kinds) -> None:
    j1 = rig.call("submit", {"kind": "t_ok", "target_world_id": "w-c1", "params": {}})["job_id"]
    assert rig.call("cancel", {"job_id": j1})["state"] == "CANCELLED"
    assert rig.w.q.get(j1)["state"] == "CANCELLED"
    assert any(e["data"]["state"] == "CANCELLED" for e in rig.kinds("job.state") if e["data"]["job_id"] == j1)
    j2 = rig.call("submit", {"kind": "t_cancel", "target_world_id": "w-c2", "params": {}})["job_id"]
    rig.w.step()
    assert rig.w.q.get(j2)["state"] == "CANCELLED"


def test_crash_marking_code(tmp_path) -> None:
    from awr.jobs.queue import JobQueue

    q = JobQueue(tmp_path / "j.sqlite")
    j = q.submit("world_build", "w1", {}, "admin")
    q.claim(lambda kind: "INGESTING")
    q.update(j["job_id"], worker_pid=2**22 + 777)
    assert q.mark_crashed() == 1
    e = json.loads(q.get(j["job_id"])["error_json"])
    assert e["code"] == 344 and e["name"] == "JOB_WORKER_CRASHED" and e["stage"] is None
    q.close()


def test_engines_route(rig) -> None:
    rep = rig.call("engines", {})
    assert isinstance(rep.get("items"), list) and any(i.get("engine") == "mock" for i in rep["items"]), rep


def test_workdir_retention(tmp_path) -> None:
    """M01-to-M03 第 6 条：SUCCEEDED/CANCELLED 24 h 后只留 job.log，FAILED 保留 7 天，7 天后整个目录删除；非终态不动。"""
    from awr.jobs.queue import JobQueue
    from awr.jobs.worker import cleanup_workdirs

    q = JobQueue(tmp_path / "jobs.sqlite")
    rows = {}
    for st in ("SUCCEEDED", "FAILED", "QUEUED"):
        j = q.submit("world_build", f"w-{st.lower()}", {}, "admin")
        q.update(j["job_id"], state=st)
        wd = tmp_path / j["job_id"]
        (wd / "frames").mkdir(parents=True)
        (wd / "frames" / "a.bin").write_bytes(b"x")
        (wd / "job.log").write_text("{}\n")
        rows[st] = (j["job_id"], wd)
    t0 = max(int(q.get(r[0])["updated_unix_ns"]) for r in rows.values()) / 1e9
    cleanup_workdirs(q, t0 + 25 * 3600)
    _ok_id, ok_wd = rows["SUCCEEDED"]
    assert [p.name for p in ok_wd.iterdir()] == ["job.log"]
    assert (rows["FAILED"][1] / "frames").exists() and (rows["QUEUED"][1] / "frames").exists()
    cleanup_workdirs(q, t0 + 8 * 24 * 3600)
    assert not ok_wd.exists() and not rows["FAILED"][1].exists() and rows["QUEUED"][1].exists()
    q.close()
