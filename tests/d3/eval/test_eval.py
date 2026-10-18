"""M27 评测（P0a）：EC、EU、CE 归一化、原型榜聚合与 Env → evaluation.v1 的端到端（合成世界）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from awr.agentsdk.schemas import bench_validator
from awr.bench.eval.aggregate import build_board
from awr.bench.eval.ce import collaborative
from awr.bench.eval.ec import event_capture
from awr.bench.eval.eu import event_understanding

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "env"))

E = [{"id": "m", "type": "proximity_encounter", "participants": ["a", "b"], "a_s": 1.0, "b_s": 2.0, "multi": True, "params": {}},
     {"id": "e", "type": "enter_building", "participants": ["a"], "a_s": 3.0, "b_s": 3.0, "multi": False, "params": {}}]


def _present(n: int) -> dict[str, np.ndarray]:
    return {"a": np.ones(n + 1, bool), "b": np.ones(n + 1, bool)}


def test_event_capture_joint_and_coverage() -> None:
    ident = {12: {("u1", "a")}, 14: {("u1", "b")}, 25: {("u2", "a")}}
    _ec, _cov, out = event_capture(E, ["u1", "u2"], ident, _present(40), 40)
    assert out["m"]["captured"] is False                                              # 无同帧联合识别
    assert out["e"]["captured"] is True and out["e"]["by"] == ["u1", "u2"]            # 窗口前后各 2 s
    ident[15] = {("u1", "a"), ("u1", "b")}
    ec, _cov, out = event_capture(E, ["u1", "u2"], ident, _present(40), 40)
    assert out["m"]["captured"] and out["m"]["by"] == ["u1"] and ec == 1.0
    assert out["m"]["coverage"] == pytest.approx(1 / 11) and out["e"]["coverage"] == pytest.approx(3 / 21)   # 瞬时事件取前 2 s


def test_event_understanding_matching() -> None:
    run = SimpleNamespace(det_actor={("u1", 10, "1" * 16): {"actor": "a"}, ("u1", 10, "2" * 16): {"actor": "b"},
                                     ("u1", 11, "3" * 16): {"actor": "a"}, ("u1", 11, "4" * 16): {"actor": "b"}})

    def rep(t, ids, s0=1.0, s1=2.0):
        return {"report": {"type": t, "participants": [{"track_id": f"t{i}", "evidence": [{"uav": "u1", "sensor": "eo", "frame_seq": q, "det_id": d}
                                                                                       for q, d in ev]} for i, ev in enumerate(ids)],
                           "window": {"t_start_ns": int(s0 * 1e9), "t_end_ns": int(s1 * 1e9)}}}

    reports = [rep("meet", [[(10, "1" * 16)], [(10, "2" * 16)]]), rep("meet", [[(11, "3" * 16)], [(11, "4" * 16)]]),
               rep("enter_building", [[(10, "1" * 16), (11, "4" * 16)]], 3, 3), rep("board", [[(10, "1" * 16)]], 3, 3)]
    eu = event_understanding(reports, E, run)
    assert [m["event_id"] for m in eu["matches"]] == ["m"]                             # 一对一：重复的 meet 只匹配一次
    assert eu["precision"] == pytest.approx(0.25) and eu["recall"] == pytest.approx(0.5)
    assert eu["participants"] == {"resolved": 5, "unresolved": 1}                     # 平票证据为未解析


def test_ce_normalisation() -> None:
    ident = {k: {("u1", "a"), ("u2", "b")} for k in range(10, 31)}
    run = SimpleNamespace(fleet={"pos": np.zeros((41, 2, 3))}, manifest={"agents": {"u1": {"comm": {"bytes": 0}}, "u2": {"comm": {"bytes": 0}}}},
                          net=[], task={"budget": {"distance_km_per_uav": 10.0, "comm_kb_per_uav": 2000.0}})
    raw, ce, norm = collaborative(E[:1], ["u1", "u2"], ident, _present(40), 40, run)
    assert norm["V_fleet"] == 1.0 and norm["V_best_single"] == 0.0 and ce == 1.0       # 两机各识别一名参与者：联合覆盖全部
    assert raw["gain"] == 0.0                                                         # 同帧联合识别不成立：EC 无增益


def _comp(ec, eu, ao, ce):
    return {"evaluation": {"components": {"ec": ec, "eu": eu, "ao": ao, "ce": ce}, "ranked_valid": True}}


def test_board_ranks_ties_and_ci() -> None:
    rows = []
    for s in range(5):
        rows.append({"entry_id": "strong", "track": "std3-l1", "lane": "handoff", "event": "ev1", "seed": s, **_comp(0.9, 0.6, 0.5, 0.4 + 0.01 * s)})
        rows.append({"entry_id": "weak", "track": "std3-l1", "lane": "handoff", "event": "ev1", "seed": s, **_comp(0.2, 0.1, 0.1, 0.4 + 0.01 * s)})
        rows.append({"entry_id": "base", "baseline": True, "track": "std3-l1", "lane": "handoff", "event": "ev1", "seed": s, **_comp(0.5, 0.3, 0.3, 0.3)})
    b1 = build_board(rows, board_version="proto-0", n_boot=2000)
    b2 = build_board(rows, board_version="proto-0", n_boot=2000)
    assert b1 == b2 and not list(bench_validator("board").iter_errors(b1))
    e = {x["entry_id"]: x for x in b1["entries"]}
    assert e["strong"]["metrics"]["ec"]["rank"] == 1 and e["weak"]["metrics"]["ec"]["rank"] == 2
    assert e["strong"]["metrics"]["ce"]["tie_group"] == e["weak"]["metrics"]["ce"]["tie_group"]   # CE 相同：统计并列
    assert e["strong"]["dominance_tier"] == 1 and e["weak"]["dominance_tier"] == 2 and e["base"]["dominance_tier"] == 2
    assert "rank" not in e["base"]["metrics"]["ec"]
    lo, hi = e["strong"]["metrics"]["ce"]["ci95"]
    assert lo <= e["strong"]["metrics"]["ce"]["value"] <= hi


def test_env_run_evaluates(synth_event, tmp_path: Path) -> None:
    from drone4d.baselines import BASELINES
    from test_env import _run, _spec

    from awr.agentsdk.env.core import _open_world
    from awr.bench.eval.evaluator import evaluate_run

    w, ev = synth_event
    _run(_spec(synth_event, tmp_path / "r", dur=60.0), lambda u: BASELINES["track_share"]())
    diag: dict = {}
    doc = evaluate_run(tmp_path / "r", ev, _open_world(w), diag=diag)
    errs = [e.message for e in bench_validator("evaluation").iter_errors(doc)]
    assert not errs, errs[:3]
    assert doc["invalid_reasons"] == ["physics_not_ranked"] and doc["ranked_valid"] is False
    assert {x["id"] for x in doc["ec"]["events"]} == {"meet_ab", "board_bc", "enter_c"}
    assert 0.0 <= doc["components"]["ao"] <= 1.0 and doc["ao"]["ao_pos"] <= 1.0
    assert set(doc["actors"]) >= {"person_a", "person_b", "person_c", "vehicle_c"}
    assert diag["n_l1"] > 0
    doc2 = evaluate_run(tmp_path / "r", ev, _open_world(w))
    assert json.dumps(doc, sort_keys=True) == json.dumps(doc2, sort_keys=True)
