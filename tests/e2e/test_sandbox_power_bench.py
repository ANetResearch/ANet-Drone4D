"""D2.1 基准工具的功能用例（WP-13b，M16；`tools/bench/sandbox/capacity.py --variant D`（D2-AC-55）与 `tools/bench/sandbox/power.py`
（D2-AC-46，P1；D2-AC-45 的沙盒组内存回落））。

纯函数：变体 D 与节能节省的判据（通过、不通过、负载过高时不判定、缺 cgroup 时不判定）。needs_data + slow：两个工具的 `--smoke`
各跑一次（真实栈，不取性能锁、不判定阈值），检查报告结构与关键观测量：驻留会话不占槽位且不在进程表中、容量接口的时延样本、
viewer 的节能循环与场订阅、驻留前后的沙盒组 CPU 与内存。正式运行按 ADR-033 在 MS5 的 cgroup 模拟下执行。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from awrproc import ROOT, world_ready

sys.path.insert(0, str(ROOT / "tools" / "bench" / "sandbox"))
import _sbbench as B
import capacity as CAP
import power as POW

TOOLS = ROOT / "tools" / "bench" / "sandbox"


def _sample(t: float) -> dict:
    return {"t": t, "procs": {"api": {"pid": 2, "cpu": 0.05, "VmRSS": 90.0, "restarts": 0},
                              "sandbox-api": {"pid": 4, "cpu": 0.08, "VmRSS": 120.0, "restarts": 0}},
            "sessions": {"sb-aaaaaaaaaa": {"pid": 3, "cpu": 0.2, "RssAnon": 120.0, "RssShmem": 10.0,
                                           "ring": {"rtf": 1.0, "step_p99_us": 3000, "epoch": 1},
                                           "perf": {"slow_ms_per_s": {"m19.field": 0.8}}}},
            "show_ring": {"rtf": 1.0, "step_p99_us": 1500, "epoch": 1}, "sbx_inspect": {"lag_ms": 2.0},
            "cg": {"unit": {"memory_current_mb": 900.0, "anon_mb": 700.0, "shmem_mb": 20.0, "oom_kill": 0},
                   "core": {"nr_throttled": 0}, "sandbox": {"oom_kill": 0}, "sb": {"anon_mb": 130.0, "shmem_mb": 0.0}}}


def _d_result(**over) -> dict:
    s = [_sample(t) for t in range(0, 90)]
    r = {"summary": B.summarize(s), "field_cost": CAP.field_cost(s), "capacity": {"n": 600, "bad": 0, "p95_ms": 12.0, "p50_ms": 4.0},
         "capacity_cpu": {"idle": 0.05, "busy_10hz": 0.054, "delta": 0.004}, "parked": {"sids": ["sb-p1", "sb-p2"], "running": [], "disk_bytes": 5_000_000},
         "internal": True, "degrade_l2_run_s_max": 0.0, "viewers": {"frames": 100, "errors": 0, "power_cycles": 6, "field_msgs": 0}}
    r.update(over)
    return r


def test_judge_variant_d() -> None:
    ok = CAP.judge_d(_d_result(), cpu_valid=True, cgroup_mode="systemd-scope")
    assert all(c["ok"] is not False for c in ok), [c for c in ok if c["ok"] is False]
    assert {c["check"] for c in ok} >= {"capacity.p95_ms", "capacity.cpu_delta", "parked.not_running", "parked.disk_mb", "internal.l2_run_s",
                                        "viewers.power_cycles", "field_cost_ms_per_sim_s", "unexpected_restarts"}
    bad = _d_result(capacity={"n": 10, "bad": 2, "p95_ms": 80.0}, capacity_cpu={"delta": 0.03},
                    parked={"running": ["sb-p1"], "disk_bytes": 20 * 1048576}, degrade_l2_run_s_max=25.0)
    failed = {c["check"] for c in CAP.judge_d(bad, cpu_valid=True, cgroup_mode="systemd-scope") if c["ok"] is False}
    assert {"capacity.p95_ms", "capacity.errors", "capacity.cpu_delta", "parked.not_running", "parked.disk_mb", "internal.l2_run_s"} <= failed
    undecided = {c["check"]: c["ok"] for c in CAP.judge_d(bad, cpu_valid=False, cgroup_mode="systemd-scope")}
    assert undecided["capacity.p95_ms"] is None and undecided["capacity.cpu_delta"] is None     # 负载过高：时间类不判定
    no_self_report = CAP.judge_d(_d_result(field_cost={}), cpu_valid=True, cgroup_mode="systemd-scope")
    assert next(c for c in no_self_report if c["check"] == "field_cost_ms_per_sim_s")["ok"] is None


def test_field_cost_reads_the_perf_self_report() -> None:
    s = [_sample(t) for t in range(3)]
    s[1]["sessions"]["sb-aaaaaaaaaa"]["perf"]["slow_ms_per_s"]["m19.field"] = 2.4
    assert CAP.field_cost(s) == {"sb-aaaaaaaaaa": 2.4}
    assert CAP.field_cost([{"t": 0, "sessions": {"sb-x": {"pid": 1}}}]) == {}


def _viewers(none: float, on: float, off: float, *, profile: str = "browser", batch_hz: float = 19.5, errors: int = 0, n: int = 24) -> dict:
    """`power.viewer_part` 的结果形状（ADR-174 口径）。"""
    inc = on - none
    return {"viewers": n, "profile": profile, "swarm_hz": CAP.VIEWER_PROFILES[profile], "connected": {"frames": 1, "batches": 1, "errors": errors},
            "batch_hz_per_viewer": batch_hz, "api_cpu_none": none, "api_cpu_on": on, "api_cpu_off": off, "drop": round(on - off, 4),
            "increment": round(inc, 4), "residual": round(off - none, 4), "per_viewer": round(inc / n, 5),
            "release_frac": round((on - off) / inc, 3) if inc > 0 else None}


def test_judge_power_savings() -> None:
    good = {"sandbox": {"state": "PARKED", "baseline": {"cpu": 0.03, "anon_shmem_mb": 87.0}, "active": {"cpu": 0.30, "anon_shmem_mb": 240.0},
                        "parked": {"cpu": 0.04, "anon_shmem_mb": 92.0}, "park_dir_bytes": 2_700_000},
            "viewers": _viewers(0.039, 0.185, 0.041)}
    checks = POW.judge(good, cpu_valid=True)
    assert all(c["ok"] is not False for c in checks), [c for c in checks if c["ok"] is False]
    assert {c["check"] for c in checks} >= {"viewers.subscribed", "viewers.residual_core", "viewers.release_frac", "viewers.cost_per_viewer"}
    bad = {"sandbox": {"state": "PARKED", "baseline": {"cpu": 0.03, "anon_shmem_mb": 87.0}, "active": {"cpu": 0.10, "anon_shmem_mb": 150.0},
                       "parked": {"cpu": 0.05, "anon_shmem_mb": 104.0}, "park_dir_bytes": 9 * 1048576},
           "viewers": _viewers(0.04, 0.60, 0.10)}                    # 节能后残余 0.06 核、释放 89%、每 viewer 0.023 核
    failed = {c["check"] for c in POW.judge(bad, cpu_valid=True) if c["ok"] is False}
    assert failed == {"sandbox.cpu_drop", "sandbox.mem_drop_mb", "sandbox.mem_vs_baseline_mb", "sandbox.park_dir_mb",
                      "viewers.residual_core", "viewers.release_frac", "viewers.cost_per_viewer"}
    nocg = POW.judge({"sandbox": {"state": "PARKED", "active": None, "parked": None}}, cpu_valid=True)
    assert any(c["check"] == "sandbox.cgroup" and c["ok"] is None for c in nocg)
    assert not any(c["check"].startswith("viewers.") for c in nocg)   # --part sandbox：viewer 段不判定


def test_judge_power_viewers_adr174() -> None:
    """ADR-174：第 1 轮的数值（只握手不订阅，下降 0.0225 核）不再按"≥ 0.3 核"判定，而是判为测量无效；订阅口径下按残余与释放比例判定。"""
    hello = POW.judge({"viewers": _viewers(0.041, 0.0663, 0.0438, profile="hello", batch_hz=0.0)}, cpu_valid=True)
    assert [(c["check"], c["ok"]) for c in hello] == [("viewers.subscribed", False)]
    starved = POW.judge({"viewers": _viewers(0.039, 0.185, 0.041, batch_hz=4.0)}, cpu_valid=True)   # 订阅没有生效
    assert {c["check"]: c["ok"] for c in starved} == {"viewers.subscribed": False, "viewers.residual_core": None,
                                                       "viewers.release_frac": None, "viewers.cost_per_viewer": None}
    tier_s = POW.judge({"viewers": _viewers(0.039, 0.1187, 0.0407, profile="browser-s", batch_hz=9.6)}, cpu_valid=True)
    assert all(c["ok"] is True for c in tier_s), tier_s
    busy = POW.judge({"viewers": _viewers(0.039, 0.185, 0.041)}, cpu_valid=False)                    # 负载过高：CPU 类不判定
    assert {c["check"]: c["ok"] for c in busy}["viewers.residual_core"] is None
    assert POW.judge({"viewers": {"viewers": 24, "drop": 0.02}}, cpu_valid=True)[0]["ok"] is None     # 缺无 viewer 基线


def test_browser_viewer_profile() -> None:
    """真实浏览器的默认订阅集（R58 采集，13 条）与 Tier 对应的 swarm 频率。"""
    subs = CAP.browser_subs(20)
    assert len(subs) == 13 and len({s["id"] for s in subs}) == 13
    assert {s["topic"]: s["rate"] for s in subs}["swarm/uav/state"] == 20
    assert {s["topic"]: s["rate"] for s in CAP.browser_subs(10)}["swarm/uav/state"] == 10
    assert {s["topic"] for s in subs} >= {"fleet/roster", "env/state", "event", "swarm/target/state", "mission/*/status", "uav/*/path"}
    assert CAP.VIEWER_PROFILES == {"hello": None, "browser": 20, "browser-s": 10}
    with pytest.raises(ValueError):
        CAP.Viewers(None, 1, "tierX")  # type: ignore[arg-type]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    p = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=1500)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-3000:]
    return p


@pytest.mark.needs_data
@pytest.mark.slow
def test_capacity_variant_d_smoke(tmp_path) -> None:
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    p = _run([str(TOOLS / "capacity.py"), "--variant", "D", "--minutes", "0.75", "--sessions", "1", "--viewers", "2", "--smoke",
              "--no-cgroup", "--internal", "--cap-window", "5", "--out", str(tmp_path)])
    rep = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    (v,) = rep["variants"]
    r = v["result"]
    assert rep["schema"] == "awr.bench.capacity.v1" and v["variant"] == "D", p.stdout[-2000:]
    assert len(r["parked"]["sids"]) == 2 and r["parked"]["running"] == [] and 0 < r["parked"]["disk_bytes"] <= 16 * 1048576
    assert r["capacity"]["n"] > 100 and r["capacity"]["bad"] == 0 and r["capacity"]["p95_ms"] is not None
    assert r["capacity_cpu"]["idle"] is not None and r["capacity_cpu"]["busy_10hz"] is not None
    assert r["viewers"]["frames"] > 0 and r["operators"]["rest"] > 10
    assert r["internal"] is True                                     # 预留槽位上的内部档会话（N_max = 1 已占满）
    assert not (tmp_path / "D" / "identity").exists()               # 临时口令文件已删除


@pytest.mark.needs_data
@pytest.mark.slow
def test_power_smoke(tmp_path) -> None:
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    p = _run([str(TOOLS / "power.py"), "--smoke", "--seconds", "6", "--warm", "6", "--viewers", "2", "--no-cgroup", "--out", str(tmp_path)])
    rep = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    r = rep["result"]
    assert rep["schema"] == "awr.bench.power.v1", p.stdout[-2000:]
    assert r["sandbox"]["state"] == "PARKED" and 0 < r["sandbox"]["park_dir_bytes"] <= 8 * 1048576
    assert r["viewers"]["connected"]["frames"] > 0 and r["viewers"]["api_cpu_on"] is not None
    v = r["viewers"]                                                  # ADR-174：S7、浏览器订阅口径、无 viewer 基线
    assert r["show_scenario"] == "s7-synthcity-relay" and v["profile"] == "browser" and v["swarm_hz"] == 20
    assert v["api_cpu_none"] is not None and v["increment"] is not None and v["connected"]["errors"] == 0
    assert v["batch_hz_per_viewer"] >= 0.75 * 20, v                  # 订阅生效（swarm 20 Hz）
