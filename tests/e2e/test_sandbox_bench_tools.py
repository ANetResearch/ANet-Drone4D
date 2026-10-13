"""容量、浸泡与世界成本 harness 的功能用例（WP-13；`tools/bench/sandbox/{capacity,soak,world_costs,_sbbench}.py`；D2-AC-04、28、39）。

纯函数：采样汇总（窗口、预热剔除、重启与 cgroup 增量）、RSS 斜率、04:00 跨越计数、判据（通过、不通过、不判定）、满载会话配置的
schema 与规模；浸泡的软重置与日表对账、10 min 窗口 RTF、精简采样（D2-FX-R2-other）。needs_data + slow：三个工具的 `--smoke` 各跑一次（真实栈，不取性能锁、不判定阈值），检查报告结构与关键观测量
（会话 RTF、各进程 CPU、viewer 收帧、世界文件页与匿名页）。正式运行按 ADR-033 在 MS5 的 cgroup 模拟下执行。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from awrproc import ROOT, world_ready

sys.path.insert(0, str(ROOT / "tools" / "bench" / "sandbox"))
import _sbbench as B
import capacity as CAP
import soak as SOAK

from awr.datasets.scenarios.catalog import schema_errors

TOOLS = ROOT / "tools" / "bench" / "sandbox"


def _sample(t: float, *, rtf: float = 1.0, p99: int = 3000, cpu: float = 0.1, rss: float = 200.0, restarts: int = 0,
            thr: int = 0, oom: int = 0) -> dict:
    return {"t": t, "procs": {"sim-core": {"pid": 1, "cpu": 0.2, "VmRSS": rss, "restarts": restarts},
                              "api": {"pid": 2, "cpu": 0.05, "VmRSS": 90.0, "restarts": 0}},
            "sessions": {"sb-aaaaaaaaaa": {"pid": 3, "cpu": cpu, "RssAnon": 120.0, "RssShmem": 10.0,
                                           "ring": {"rtf": rtf, "step_p99_us": p99, "epoch": 1}}},
            "show_ring": {"rtf": 1.0, "step_p99_us": 1500, "epoch": 1}, "sbx_inspect": {"lag_ms": 2.0},
            "cg": {"unit": {"memory_current_mb": 900.0, "anon_mb": 700.0, "shmem_mb": 20.0, "oom_kill": oom},
                   "core": {"nr_throttled": thr}, "sandbox": {"oom_kill": 0}, "sb": {"anon_mb": 130.0, "shmem_mb": 0.0}}}


def test_summarize_skips_warmup_and_reports_deltas() -> None:
    s = [_sample(t, rtf=0.5 if t < 30 else 1.0, thr=0 if t < 50 else 3, restarts=0 if t < 60 else 1) for t in range(0, 90)]
    m = B.summarize(s, skip_s=30.0)
    e = m["sessions"]["sb-aaaaaaaaaa"]
    assert e["rtf_min"] == 1.0 and e["anon_shmem_mb_max"] == 130.0 and e["epochs"] == [1]
    assert m["procs"]["sim-core"]["restarts"] == 1 and m["cgroup"]["core_nr_throttled_delta"] == 3
    assert m["sandbox_api_lag_ms_p99"] == 2.0 and m["cgroup"]["anon_shmem_mb_max"] == 720.0


def test_judge_capacity_a() -> None:
    s = [_sample(t) for t in range(0, 90)]
    r = {"summary": B.summarize(s)}
    assert all(c["ok"] is not False for c in CAP.judge_a(r, cpu_valid=True, cgroup_mode="systemd-scope"))
    bad = {"summary": B.summarize([_sample(t, rtf=0.95, p99=9000, oom=1) for t in range(0, 90)])}
    failed = {c["check"] for c in CAP.judge_a(bad, cpu_valid=True, cgroup_mode="systemd-scope") if c["ok"] is False}
    assert {"sb-aaaaaaaaaa.rtf_min", "sb-aaaaaaaaaa.step_p99_us", "oom_kill"} <= failed
    undecided = CAP.judge_a(bad, cpu_valid=False, cgroup_mode="systemd-scope")
    assert all(c["ok"] is None for c in undecided if c["check"].endswith(("rtf_min", "step_p99_us")))     # 负载过高：不判定


def test_judge_capacity_b() -> None:
    ok = CAP.judge_b({"third": {"code": 508, "detail": {"available_now": ["synthcity", "shenzhen"]}}})
    assert all(c["ok"] for c in ok)
    third = {c["check"]: c["ok"] for c in CAP.judge_b({"third": {"code": 0}})}
    assert third["pair_admitted"] is True and third["third_world_admission_508"] is False


def test_variant_b_budget_on_the_measured_table() -> None:
    """D2-FX-R2：实测成本表下变体 B 以测试配置的额度运行：sanfrancisco + shanghai 准入、chicago 超额（508）、较小的世界仍可开始。"""
    from awr.sandbox.admission import world_admit
    from awr.sandbox.worlds import load_costs

    bud = CAP.variant_b_budget()
    costs = load_costs(CAP.B.ROOT / "configs" / "sandbox" / "world_costs.json")
    m = bud["m_sb_test"]
    assert world_admit(costs, [], "sanfrancisco", n_max=4, m_sb=m)
    assert world_admit(costs, ["sanfrancisco"], "shanghai", n_max=4, m_sb=m)
    assert not world_admit(costs, ["sanfrancisco", "shanghai"], "chicago", n_max=4, m_sb=m)
    assert any(world_admit(costs, ["sanfrancisco", "shanghai"], w, n_max=4, m_sb=m) for w in costs)
    assert bud["natural_third_admitted"] == world_admit(costs, ["sanfrancisco", "shanghai"], "chicago", n_max=4, m_sb=1100.0)
    bad = CAP.judge_b({"create_shanghai": {"code": 508}, "third": {"code": 0}})
    assert [c["ok"] for c in bad][:2] == [False, False]


def test_rss_slope_and_0400_crossing() -> None:
    s = [_sample(t * 60.0, rss=200.0 + 1.5 * (t / 60.0)) for t in range(0, 600)]          # 10 h，1.5 MB/h
    assert B.rss_slope_mb_per_h(s, "sim-core", skip_s=1800.0) == pytest.approx(1.5, abs=0.01)
    base = int(datetime(2026, 10, 5, 19, 0, tzinfo=UTC).timestamp() * 1e9)                 # 北京时间 03:00（次日）
    assert SOAK.crossed_local([base, base + int(2 * 3600e9)]) == 1
    assert SOAK.crossed_local([base + int(2 * 3600e9), base + int(3 * 3600e9)]) == 0
    assert SOAK.crossed_local([base, base + int(49 * 3600e9)]) == 3


def _roll(day: str, t_cal_ns: int, rows: list[dict] | None = None) -> dict:
    return {"type": "relay.day_rollover", "data": {"day": day, "tz": "Asia/Shanghai", "t_cal_unix_ns": str(t_cal_ns), "rows": rows or []}}


def test_soak_soft_reset_and_day_table_judgement() -> None:
    """D2-FX-R2-other：软重置以日历采样（`GET /api/env/state`）的 04:00 跨越与 `relay.day_rollover` 事件对账；日表行校验。"""
    b20 = int(datetime(2026, 10, 11, 20, 0, tzinfo=UTC).timestamp() * 1e9)           # 北京 10-12 04:00
    cal = [(b20 - int(3600e9), "Asia/Shanghai"), (b20 + int(60e9), "Asia/Shanghai")]
    row = {"tid": "t2", "monitor_s": 3600.0, "coverage": 0.99, "discovered": 0}
    ok = {c["check"]: c for c in SOAK.judge_rollovers([_roll("2026-10-11", b20, [row])], cal, 24.5, smoke=False)}
    assert ok["soft_reset_0400"]["ok"] is True and ok["day_table"]["ok"] is True
    assert ok["soft_reset_0400"]["value"] == {"crossings": 1, "rollovers": 1, "tz": "Asia/Shanghai"}
    missing = {c["check"]: c["ok"] for c in SOAK.judge_rollovers([], cal, 24.5, smoke=False)}
    assert missing == {"soft_reset_0400": False, "day_table": None}            # 跨过 04:00 而没有日切事件
    wrong = {c["check"]: c for c in SOAK.judge_rollovers([_roll("2026-10-12", b20, [dict(row, coverage=1.5)])], cal, 24.5, smoke=False)}
    assert wrong["day_table"]["ok"] is False and set(wrong["day_table"]["value"]["bad"]) == {"day:2026-10-12", "t2.coverage"}
    short = [(b20 - int(7200e9), "Asia/Shanghai"), (b20 - int(3600e9), "Asia/Shanghai")]
    assert SOAK.judge_rollovers([], short, 1.0, smoke=False)[0]["ok"] is None          # 短运行没有跨过 04:00：不判定
    assert SOAK.judge_rollovers([], short, 24.0, smoke=False)[0]["ok"] is False        # 24 h 却没有跨过：日历未推进
    assert SOAK.expected_day(b20, "Asia/Shanghai") == "2026-10-11"


def test_soak_rtf_window_and_slim_samples() -> None:
    s = [dict(_sample(float(t)), show_ring={"t_sim_ns": int(t * 1e9 * (0.5 if 300 <= t < 330 else 1.0)) if t < 300 else
                                             int((300 + 15 + (t - 330)) * 1e9) if t >= 330 else int((300 + (t - 300) * 0.5) * 1e9),
                                             "epoch": 1, "rtf": 1.0, "step_p99_us": 1000}) for t in range(0, 1800)]
    w = SOAK.rtf_windows(s, window_s=600.0, step_s=60.0)
    assert w["n"] > 0 and w["min"] == pytest.approx(585.0 / 600.0, abs=0.003)            # 30 s 内 ×0.5：10 min 窗口 0.975
    full = dict(_sample(0.0), sbx_health={"build": {"version": "0.2.0"}}, sbx_inspect={"lag_ms": 2.0, "metrics": {"ticks": 9}})
    slim = B.slim_sample(full)
    assert "sbx_health" not in slim and slim["sbx_inspect"] == {"lag_ms": 2.0}
    assert B.summarize([B.slim_sample(x) for x in s[:90]], skip_s=30.0)["procs"] == B.summarize(s[:90], skip_s=30.0)["procs"]


def test_capacity_config_is_a_full_load_snapshot() -> None:
    cfg = CAP.capacity_config("synthcity")
    assert not schema_errors(cfg, "sandbox/snapshot.schema.json")
    assert len(cfg["vehicles"]) == 10 and len(cfg["targets"]) == 30 and {t["type"] for t in cfg["tasks"]} >= {"relay_watch"}


def _run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)


@pytest.mark.needs_data
@pytest.mark.slow
def test_capacity_smoke(tmp_path) -> None:
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    p = _run([str(TOOLS / "capacity.py"), "--variant", "A", "--minutes", "0.75", "--sessions", "1", "--viewers", "2", "--smoke",
              "--no-cgroup", "--out", str(tmp_path)])
    rep = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    (v,) = rep["variants"]
    r = v["result"]
    assert rep["schema"] == "awr.bench.capacity.v1" and r["n_sessions"] == 1, p.stdout[-2000:]
    assert r["operators"]["rest"] > 20 and r["operators"]["stream_msgs"] > 0 and r["viewers"]["frames"] > 0
    (sess,) = r["summary"]["sessions"].values()
    assert sess["rtf_mean"] > 0 and sess["anon_shmem_mb_max"] > 0
    assert {"sim-core", "api", "sandbox-pool", "sandbox-api"} <= set(r["summary"]["procs"])


@pytest.mark.needs_data
@pytest.mark.slow
def test_soak_smoke(tmp_path) -> None:
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    _run([str(TOOLS / "soak.py"), "--hours", "0.01", "--viewers", "2", "--smoke", "--no-cgroup", "--show-scenario",
          "s0-synthcity-showcase", "--out", str(tmp_path)])
    rep = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert rep["schema"] == "awr.bench.soak.v1" and rep["viewers"]["frames"] > 0 and rep["events"]
    assert {c["check"] for c in rep["checks"]} >= {"duration_h", "rss_slope.sim-core", "unexpected_restarts", "discovered", "soft_reset_0400",
                                                   "day_table", "oom"}
    assert rep["info"]["note"] == "not a D2-AC-39 criterion" and not any(c["check"].startswith("show_rtf") for c in rep["checks"])


@pytest.mark.needs_data
@pytest.mark.slow
def test_world_costs_smoke(tmp_path) -> None:
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    _run([str(TOOLS / "world_costs.py"), "--world", "synthcity", "--seconds", "40", "--smoke", "--out", str(tmp_path)])
    table = json.loads((tmp_path / "world_costs.json").read_text(encoding="utf-8"))
    assert not schema_errors(table, "sandbox/world_costs.schema.json")
    row = next(w for w in table["worlds"] if w["world"] == "synthcity")
    assert row["available"] and row["anon_mb"] > 0 and row["measured"] is False          # smoke 不标记为实测
    assert {w["world"] for w in table["worlds"]} == {"synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco"}
