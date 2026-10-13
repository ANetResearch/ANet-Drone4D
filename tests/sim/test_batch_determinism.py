"""批处理自由运行（M08-AC-211；M08-FR-221、FR-222；ADR-106；D2-AC-32）。

以桩插件（识别物实体块、10 Hz 随机游走 stage 使用 RNG 流 `targets_motion`、会话配置的 targets 段装配者、批处理结果文件
`relay_stats`、1 Hz【仿真】慢任务在 t = 1 s 经内部 principal 下发起飞）驱动 sandbox100 批处理：

- 同一会话配置与种子跑两次：`events.jsonl`（`t_wall_ns` 置 null）、识别物轨迹与 `relay_stats.json` 逐字节一致；不同种子不一致；
- 批处理不写 StateRing 槽（tap 关闭）；`summary.json` 给出墙钟用时、平均 RTF、各 stage 耗时与 tick 数，退出码 0；
- `BatchCtl.lat_ticks`：分配 200 ms → 20 tick、扫描 2000 ms → 200 tick（sandbox100）；`fail()` 结束批处理，退出码 3；
- 命令行 `python -m awr.sim.runtime --batch --until … --config …` 写出结果文件。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from awr.sim.fleet.stages import registry as R
from awr.sim.runtime import clock_profile as CP
from awr.sim.runtime.batch import BatchCtl, run_batch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "fake"))
import targets_stub as TS  # noqa: E402

CONFIG = {
    "schema": "awr.sandbox.snapshot.v1", "contracts": "1.1.0", "world_id": "synthcity", "seed": 7,
    "calendar": {"start_utc": "2026-10-05T00:00:00Z", "tz": "Asia/Shanghai"}, "nests": [], "tasks": [],
    "vehicles": [{"id": "p600-01", "model_id": "p600_mid360", "home_enu_m": [0.0, 0.0, 0.0]},
                 {"id": "p600-02", "model_id": "p600_mid360", "home_enu_m": [12.0, 0.0, 0.0]}],
    "targets": [{"class": "person", "pos": [50.0, 10.0, 0.0]}, {"class": "dog", "pos": [-30.0, 40.0, 0.0]}],
}


def _plugins(fail_at_s: float | None = None) -> dict:
    """在当前（隔离）登记表中登记桩插件；返回轨迹记录。"""
    rec: dict = {"traj": []}
    R.register_entity_block("target", "targets", "M18", TS.M18_FIELDS)

    @R.register_stage("targets", hz=10, order=170, owner="M18")
    def st_targets(S, ctx) -> None:
        tab = ctx.entities["target"]
        rows = tab.rows()
        if rows.size == 0:
            return
        g = ctx.rng["targets_motion"]
        step = g.normal(0.0, 0.3, size=(rows.size, 2))
        tab.cols["tgt_pos"][rows, :2] += step
        tab.cols["tgt_speed"][rows] = np.hypot(step[:, 0], step[:, 1]) * 10
        rec["traj"].append(tab.cols["tgt_pos"][rows].copy())

    def load_targets(items, api) -> list:
        for t in items:
            m = api.entities.add("target", model=t["class"])
            api.entities["target"].cols["tgt_pos"][m.row] = t["pos"]
            api.entities["target"].cols["tgt_enabled"][m.row] = 1
        return []

    R.register_session_loader("targets", load_targets, owner="M18", order=50)

    def relay_stats(ctx) -> dict:
        tab = ctx.entities["target"]
        return {"v": 1, "items": [{"id": m.id, "pos": [round(float(x), 9) for x in tab.cols["tgt_pos"][m.row]]}
                                  for m in tab.ordered()]}

    R.register_batch_output("relay_stats", relay_stats)
    state = {"done": False}

    def slow(ctx) -> None:
        if not state["done"] and ctx.t_ns >= 1_000_000_000:
            state["done"] = True
            for vid in ("p600-01", "p600-02"):
                ctx.calls.submit_internal({"cid": f"bt-to-{vid}", "op": "takeoff", "uav": vid, "args": {"alt_m": 15.0}},
                                          {"principal_id": "batch-test", "role": "scenario", "entry": "scenario"})
        if fail_at_s is not None and ctx.t_ns >= fail_at_s * 1e9:
            ctx.batch.fail("plan wait > 60 s")

    R.register_slow_task("test.batch", slow, period_sim_s=1.0)
    return rec


def _run(seed: int, until: float = 20.0, out: Path | None = None, fail_at_s: float | None = None) -> dict:
    with R.isolated_registry():
        rec = _plugins(fail_at_s)
        s = run_batch(until, CONFIG, seed=seed, out=out, clock="sandbox100", plugins=(), load_world=False)
        s["_traj"] = np.array(rec["traj"])
        return s


def test_batch_byte_identical(tmp_path: Path) -> None:
    a = _run(7, out=tmp_path / "a")
    b = _run(7, out=tmp_path / "b")
    c = _run(8, out=tmp_path / "c")
    assert a["exit_code"] == 0 and a["config_problems"] == [] and a["n_uav"] == 2 and a["n_target"] == 2
    assert a["ticks"] == 2000 and a["t_sim_s"] == pytest.approx(20.0)
    assert (tmp_path / "a" / "events.jsonl").read_bytes() == (tmp_path / "b" / "events.jsonl").read_bytes()
    assert (tmp_path / "a" / "relay_stats.json").read_bytes() == (tmp_path / "b" / "relay_stats.json").read_bytes()
    assert np.array_equal(a["_traj"], b["_traj"]) and a["_traj"].shape == (200, 2, 3)
    assert not np.array_equal(a["_traj"], c["_traj"])
    ev = [json.loads(x) for x in (tmp_path / "a" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [e["kind"] for e in ev]
    assert "cmd.succeeded" in kinds and all(e["t_wall_ns"] is None for e in ev)
    summ = json.loads((tmp_path / "a" / "summary.json").read_text(encoding="utf-8"))
    assert summ["clock_profile"] == "sandbox100" and summ["rtf_avg"] > 1.0 and "l1" in summ["stage_ms_per_sim_s"]
    core = a["_core"]
    assert core.fleet.tap.published == 0  # 不写 StateRing 槽


def test_batch_fail_exit_3() -> None:
    s = _run(7, until=30.0, fail_at_s=3.0)
    assert s["exit_code"] == 3 and s["failed"] == "plan wait > 60 s" and s["t_sim_s"] < 5.0


def test_lat_ticks() -> None:
    ctl = BatchCtl(CP.get("sandbox100"))
    assert ctl.lat_ticks("allocate") == 20 and ctl.lat_ticks("scan") == 200 and ctl.lat_ticks("transit") == 5
    assert ctl.lat_ticks("other", 750.0) == 75 and BatchCtl(CP.get("d1_250")).lat_ticks("station") == 125


def test_batch_cli(tmp_path: Path) -> None:
    cfg = dict(CONFIG, targets=[])  # 无 M18 装配者时 targets 段只报 NO_LOADER；此处省略
    p = tmp_path / "session.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "-m", "awr.sim.runtime", "--batch", "--until", "3", "--config", str(p), "--out",
                        str(out), "--clock", "sandbox100", "--no-world"], cwd=ROOT, capture_output=True, text=True,
                       timeout=300, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "python"), "AWR_PLUGINS": ""})
    assert r.returncode == 0, r.stderr[-2000:]
    summ = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summ["ticks"] == 300 and summ["n_uav"] == 2
    assert json.loads((out / "relay_stats.json").read_text(encoding="utf-8")) == {"items": [], "v": 1}


# ---------------------------------------------------------------- D2-AC-32：规划作业（M10 PlanPoolClient）按固定延迟生效
def _run_m10(seed: int, out: Path, delay_s: float, monkeypatch: pytest.MonkeyPatch) -> dict:
    """M10 插件装配的批处理：t = 1 s 起飞，t = 15 s 经内部 principal 下发 follow_path（M10 提供者提交 `follow_path` 规划作业）。
    规划 worker 被替换为先墙钟休眠 `delay_s` 再执行的版本：实时生效语义下这会推迟结果的生效 tick，批处理不应受影响。"""
    import awr.sim.mission as M10
    from awr.sim.planning import worker as W

    real = W.worker_main

    def slow_worker(req):
        import time as _t

        _t.sleep(delay_s)
        return real(req)

    monkeypatch.setattr(W, "worker_main", slow_worker)
    monkeypatch.setenv("AWR_PLAN_POOL", "thread")  # 线程池：被替换的 worker 在本进程生效（进程池的 spawn 子进程不受 monkeypatch 影响）
    monkeypatch.setenv("AWR_SCENARIO_LOAD", "0")
    cfg = dict(CONFIG, targets=[], vehicles=CONFIG["vehicles"][:1])
    with R.isolated_registry():
        M10.install()
        st = {"to": False, "fp": False}

        def slow(ctx) -> None:
            pr = {"principal_id": "batch-test", "role": "scenario", "entry": "scenario"}
            if not st["to"] and ctx.t_ns >= 1_000_000_000:
                st["to"] = True
                ctx.calls.submit_internal({"cid": "bt-to", "op": "takeoff", "uav": "p600-01", "args": {"alt_m": 15.0}}, pr)
            if not st["fp"] and ctx.t_ns >= 15_000_000_000:
                st["fp"] = True
                ctx.calls.submit_internal({"cid": "bt-fp", "op": "follow_path", "uav": "p600-01",
                                           "args": {"waypoints": [[20.0, 0.0, 15.0], [20.0, 20.0, 15.0]], "speed_mps": 4.0}}, pr)

        R.register_slow_task("test.batch.m10", slow, period_sim_s=0.5)
        s = run_batch(30.0, cfg, seed=seed, out=out, clock="sandbox100", plugins=(), load_world=False)
        return s


def test_batch_plan_jobs_apply_at_fixed_latency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = _run_m10(7, tmp_path / "a", 0.0, monkeypatch)
    b = _run_m10(7, tmp_path / "b", 0.6, monkeypatch)  # 结果晚于生效 tick 到达：主循环阻塞等待（plan_wait_s 只作诊断，随负载变化）
    assert a["exit_code"] == 0 and b["exit_code"] == 0
    ea = (tmp_path / "a" / "events.jsonl").read_bytes()
    assert ea == (tmp_path / "b" / "events.jsonl").read_bytes()
    ev = [json.loads(x) for x in ea.decode().splitlines()]
    ready = [e for e in ev if e["kind"] == "plan.ready" and e["data"]["job_id"].startswith("follow_path:bt-fp")]
    assert len(ready) == 1
    req_tick = int(ready[0]["data"]["job_id"].rsplit(":", 1)[1])
    lat = BatchCtl(CP.get("sandbox100")).lat_ticks("follow_path", 500.0)
    assert lat == 50 and ready[0]["data"]["apply_tick"] == req_tick + lat
    assert ready[0]["data"]["t_ms"] is None  # 墙钟诊断量置 null（WALL_FIELDS）
    kinds = [e["kind"] for e in ev if e.get("cid") == "bt-fp"]
    assert "cmd.running" in kinds
    assert all(e["data"].get("load_ms") is None for e in ev if e["kind"] == "geo.ready")


def test_batch_plan_wait_limit_exit_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """规划结果在生效 tick 仍未返回且墙钟等待超过上限：批处理以退出码 3 结束（M08-FR-221）。"""
    import awr.sim.runtime.batch as BT

    monkeypatch.setattr(BT, "WAIT_LIMIT_S", 0.3)
    s = _run_m10(7, tmp_path / "w", 1.5, monkeypatch)
    assert s["exit_code"] == 3 and str(s["failed"]).startswith("plan wait > 0.3 s")
    assert s["t_sim_s"] < 30.0


def test_pool_batch_order_and_supersede() -> None:
    """PlanPoolClient 批处理语义（无 sim-core）：结果按 (request_tick + lat_ticks, 提交序) 生效；暂存期间被取代的作业丢弃。"""
    from awr.sim.planning.jobs import PlanRequest
    from awr.sim.planning.pool import PlanPoolClient

    def req(jid: str, key: str, tick: int) -> PlanRequest:
        return PlanRequest(jid, "path_valid", None, (), {"polyline": np.array([[0, 0, 10.0], [1, 0, 10.0]])}, {}, 1, tick,
                           100, key)  # type: ignore[arg-type]

    pool = PlanPoolClient(None, mode="inline")
    ctl = BatchCtl(CP.get("sandbox100"))
    pool.batch_of = lambda: ctl
    got: list[tuple[str, int]] = []

    def cb(r, t) -> None:
        got.append((r.job_id, int(t)))

    assert ctl.lat_ticks("path_valid", 100.0) == 10
    pool.submit(req("a", "k1", 0), cb)
    pool.submit(req("b", "k2", 3), cb)
    pool.submit(req("c", "k3", 1), cb)
    assert pool.pending_of(["a", "b", "c"]) == []  # 已到达（暂存）的作业不算未到达
    for t in range(20):
        pool.drain(t)
    assert got == [("a", 10), ("c", 11), ("b", 13)]
    got.clear()
    pool.submit(req("d", "k4", 20), cb)
    pool.drain(21)
    pool.submit(req("e", "k4", 22), cb)  # 同键取代暂存中的 d
    for t in range(22, 40):
        pool.drain(t)
    assert got == [("e", 32)] and not pool._got and pool.pending() == 0
