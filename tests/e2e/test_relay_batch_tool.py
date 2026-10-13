"""接力批处理验收工具 `tools/bench/sandbox/relay_batch.py` 的功能用例（WP-13；D2-AC-19、20、32、35 的判据实现）。

判据函数以合成的 `relay_stats` 与事件流单测（通过、各类不通过、对拍不一致、昼夜切换计数、不可行时长预测）；配置构造与 schema；
needs_data 时以 120 仿真秒的用例 c 走一遍子进程批处理、探测与报告（不判定结果：24 h 与 7×24 h 的正式运行按 ADR-033 在 MS5 执行）。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from awrproc import ROOT, world_ready

sys.path.insert(0, str(ROOT / "tools" / "bench" / "sandbox"))
import relay_batch as RB

from awr.datasets.scenarios.catalog import schema_errors


def _ev(kind_: str, t: float, **data) -> dict:
    return {"kind": kind_, "t_sim_ns": int(t * 1e9), "data": data, "severity": 1, "uav": data.get("uav")}


def _item(**over) -> dict:
    base = {"tid": "t-1", "target": "person-01", "t_monitor_s": 86000.0, "coverage": 1.0,
            "gaps": {"count": 0, "max_s": 0.0, "by_cause": {"keepout_priority": 0, "illum_transition": 0, "infeasible": 0, "battery": 0,
                                                            "other": 0},
                     "total_s_by_cause": {"keepout_priority": 0.0, "illum_transition": 0.0, "infeasible": 0.0, "battery": 0.0, "other": 0.0}},
            "discovered": 0, "config_entries": {"count": 0, "total_s": 0.0}, "handovers": {"count": 2, "mean_overlap_s": 18.0,
                                                                                         "min_overlap_s": 16.0},
            "energy_rtl": 0, "dispatch_delays": {"battery": 0}, "ext": {"first_capture_s": 120.0, "infeasible_periods": []}}
    base.update(over)
    return base


def _kf(t: float, band: str) -> dict:
    return _ev("env.keyframe", t, config={"sun": {"band": band}})


def _res(item: dict, events: list[dict], t_sim: float = 86400.0) -> dict:
    return {"summary": {"exit_code": 0, "config_problems": [], "t_sim_s": t_sim}, "relay_stats": {"v": 1, "items": [item]},
            "events": events, "probe": {"sustainability": {"verdict": "yes"}}}


BASE_EV = [_kf(0, "day"), _kf(21000, "night"), _kf(65000, "day"),
           _ev("relay.handover", 3000, tid="t-1", from_uav="p600-01", to_uav="p600-02", overlap_s=16.0),
           _ev("relay.handover", 6000, tid="t-1", from_uav="p600-02", to_uav="p600-03", overlap_s=20.0)]


def test_band_switches_counts_day_night_changes() -> None:
    assert RB.band_switches(BASE_EV) == 2
    assert RB.band_switches([_kf(0, "day"), _kf(10, "low"), _kf(20, "day")]) == 0


def test_evaluate_ab_pass_and_exemptions() -> None:
    ev = [*BASE_EV, _ev("relay.gap", 21000, tid="t-1", cause="illum_transition", duration_s=12.0),
          _ev("relay.gap", 40000, tid="t-1", cause="keepout_priority", duration_s=20.0)]
    item = _item(gaps={"count": 2, "max_s": 20.0, "by_cause": {"keepout_priority": 1, "illum_transition": 1, "infeasible": 0, "battery": 0,
                                                              "other": 0}})
    checks = RB.evaluate_ab(_res(item, ev), expect_sustainable=True)
    bad = [c for c in checks if not c["ok"]]
    assert not bad, bad


@pytest.mark.parametrize("mutate, failing", [
    (lambda i, e: (i | {"discovered": 1}, [*e, _ev("target.discovered", 5, target="person-01", uav="p600-01")]), "discovered"),
    (lambda i, e: (i | {"energy_rtl": 1}, e), "energy_rtl"),
    (lambda i, e: (i | {"handovers": {"count": 2, "mean_overlap_s": 9.0, "min_overlap_s": 8.0}}, e), "handover.overlap_min"),
    (lambda i, e: (i | {"dispatch_delays": {"battery": 2}}, e), "dispatch_delays.battery"),
    (lambda i, e: (i | {"gaps": {"count": 1, "max_s": 50.0, "by_cause": {"battery": 1}}},
                   [*e, _ev("relay.gap", 100, tid="t-1", cause="battery", duration_s=50.0)]), "gaps.non_exempt"),
    (lambda i, e: (i | {"gaps": {"count": 1, "max_s": 45.0, "by_cause": {"keepout_priority": 1}}},
                   [*e, _ev("relay.gap", 100, tid="t-1", cause="keepout_priority", duration_s=45.0)]), "gaps.keepout_priority"),
    (lambda i, e: (i, [*e, _ev("sim.contact.collision", 50, kind="IMPACT", uav="p600-01")]), "collisions"),
    (lambda i, e: (i | {"handovers": {"count": 3, "mean_overlap_s": 18.0, "min_overlap_s": 16.0}}, e), "stats_vs_events.handovers"),
])
def test_evaluate_ab_failures(mutate, failing: str) -> None:
    item, ev = mutate(_item(), list(BASE_EV))
    checks = {c["check"]: c for c in RB.evaluate_ab(_res(item, ev))}
    assert not checks[failing]["ok"], checks[failing]


def test_evaluate_ab_sustainability_expectation() -> None:
    res = _res(_item(), list(BASE_EV))
    res["probe"] = {"sustainability": {"verdict": "no", "bottlenecks": [{"code": "charger"}]}}
    checks = {c["check"]: c for c in RB.evaluate_ab(res, expect_sustainable=True)}
    assert not checks["dry_run.sustainability"]["ok"]
    checks = {c["check"]: c for c in RB.evaluate_ab(res, expect_sustainable=False)}
    assert checks["dry_run.sustainability"]["ok"]


def test_evaluate_c_prediction_tolerance() -> None:
    cal = {"start_utc": "2026-10-05T04:00:00Z", "tz": "Asia/Shanghai"}
    t0 = 1791172800                                              # 2026-10-05T04:00:00Z
    wins = [{"t_from_unix_ns": str((t0 + 0) * 10**9), "t_to_unix_ns": str((t0 + 21600) * 10**9), "band": "day", "feasible": True},
            {"t_from_unix_ns": str((t0 + 21600) * 10**9), "t_to_unix_ns": str((t0 + 64800) * 10**9), "band": "night", "feasible": False},
            {"t_from_unix_ns": str((t0 + 64800) * 10**9), "t_to_unix_ns": str((t0 + 90000) * 10**9), "band": "day", "feasible": True}]
    probe = {"sustainability": {"verdict": "no", "windows": wins}}
    assert RB.predicted_infeasible_s(probe, cal, 86400.0) == pytest.approx(43200.0)
    gaps = {"count": 1, "max_s": 42000.0, "by_cause": {"infeasible": 1}, "total_s_by_cause": {"infeasible": 42000.0}}
    item = _item(gaps=gaps, handovers={"count": 0, "mean_overlap_s": None, "min_overlap_s": None})
    ev = [_ev("relay.gap", 21600, tid="t-1", cause="infeasible", duration_s=42000.0)]
    res = {"summary": {"exit_code": 0, "t_sim_s": 86400.0}, "relay_stats": {"items": [item]}, "events": ev, "probe": probe}
    assert all(c["ok"] for c in RB.evaluate_c(res, cal)), RB.evaluate_c(res, cal)
    item["gaps"]["total_s_by_cause"]["infeasible"] = 30000.0
    assert not {c["check"]: c for c in RB.evaluate_c(res, cal)}["infeasible_total_vs_prediction"]["ok"]


def test_haze_schedule_crosses_midnight() -> None:
    s = RB.haze_schedule("12:00")
    assert [e["at_s"] for e in s] == [9 * 3600.0, 12 * 3600.0]
    assert s[0]["patch"] == {"atmosphere": {"mor_bg_m": 3000}} and s[1]["patch"]["atmosphere"]["mor_bg_m"] == 30000


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_relay_configs_are_valid_snapshots(case: str) -> None:
    cfg = RB.relay_config(case)
    assert not schema_errors(cfg, "sandbox/snapshot.schema.json")
    assert cfg["calendar"] == {"start_utc": "2026-10-05T04:00:00Z", "tz": "Asia/Shanghai"}
    (n,) = cfg["nests"]
    assert sum(c["count"] for c in n["chargers"]) == 4 and n["energy"]["packs"][0]["count"] + len(cfg["vehicles"]) == 6
    assert all(("sensors" in v) is (case != "c") for v in cfg["vehicles"])
    t = cfg["targets"][0]
    assert t["class"] == ("dog" if case == "c" else "person") and "sensitivity" not in t     # 类别缺省模板（AWR-04 §9.2）
    assert RB.relay_config("a", chargers=3)["nests"][0]["chargers"][0]["count"] == 3


def test_t5_config_from_template() -> None:
    cfg = RB.t5_config("sanfrancisco")
    assert not schema_errors(cfg, "sandbox/snapshot.schema.json")
    assert cfg["calendar"]["tz"] == "America/Los_Angeles" and cfg["tasks"][0]["type"] == "area_guard"


@pytest.mark.needs_data
@pytest.mark.slow
def test_relay_batch_pipeline_smoke(tmp_path) -> None:
    """子进程批处理 + bench_probe 探测 + 报告：120 仿真秒的用例 c（不判定结果，只检查流水线产物齐全）。"""
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "bench" / "sandbox" / "relay_batch.py"), "--case", "c", "--hours",
                        str(120 / 3600), "--no-lock", "--no-load-wait", "--out", str(tmp_path), "--jobs", "1"],
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    rep = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    (case,) = rep["cases"]
    assert rep["schema"] == "awr.bench.relay.v1" and case["t_sim_s"] == pytest.approx(120.0), p.stdout[-2000:]
    assert case["probe"] is not None and case["probe"]["sustainability"]["verdict"] in ("yes", "no")
    assert case["relay_stats"] is not None and {c["check"] for c in case["checks"]} >= {"dry_run.not_sustainable", "discovered"}
