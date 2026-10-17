"""D3 回归门（D3-AC-01）的配置与对拍逻辑自检：gate.json 覆盖 AWR-05 §4.8 第 1–6 条，摘要比较能发现开关差异与基线差异。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _gate():
    spec = importlib.util.spec_from_file_location("regress_gate", ROOT / "tools/bench/d3/regress_gate.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def test_gate_covers_items_1_to_6() -> None:
    m = _gate()
    g = m.load_gate()
    assert {s["item"] for s in g["steps"]} == {1, 2, 3, 4, 5, 6}
    assert [s["id"] for s in m.select_steps(g, None)].count("perf") == 0           # 缺省不跑性能（需独占主机）
    assert [s["id"] for s in m.select_steps(g, ["perf"])] == ["perf"]
    with pytest.raises(ValueError):
        m.select_steps(g, ["nope"])
    z = g["zero_impact"]
    assert {f["AWR_D3_ENABLED"] for f in z["flags"]} == {"0", "1"} and set(z["files"]) == {"relay_stats.json", "events.jsonl"}


def test_compare_digests() -> None:
    m = _gate()
    a = {"relay_stats.json": "1", "events.jsonl": "2"}
    assert m.compare_digests([a, dict(a)], dict(a)) == []
    assert m.compare_digests([a, {**a, "events.jsonl": "3"}], None)
    assert m.compare_digests([a, dict(a)], {**a, "relay_stats.json": "9"})
    assert m.compare_digests([], None) == ["no runs"]


def test_relay_verdict_gates_on_run_checks_and_determinism() -> None:
    G = _gate()
    ok = lambda c, v=True: {"check": c, "ok": v}  # noqa: E731
    run = lambda n: {"case": n, "checks": [ok("summary.exit_code"), ok("summary.config_problems"), ok("gaps.non_exempt", False)]}  # noqa: E731
    rep = {"cases": [run("relay-b-1h-r1"), run("relay-b-1h-r2"),
                     {"case": "relay-b-1h-determinism", "ok": True}]}
    assert G.relay_verdict(rep, "b", 1.0) == ("relay-b-1h-r1", [])
    rep["cases"][2]["ok"] = False
    rep["cases"][1]["checks"][0]["ok"] = False
    _, problems = G.relay_verdict(rep, "b", 1.0)
    assert problems == ["relay-b-1h-r2: summary.exit_code", "relay-b-1h-determinism: not byte identical"]
    assert G.relay_verdict({}, "b", 1.0)[1] == ["relay-b-1h-r1: missing", "relay-b-1h-r2: missing", "relay-b-1h-determinism: missing"]
