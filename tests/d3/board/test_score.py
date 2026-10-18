"""D3-AC-57 赛道指标与置信区间：按 score_version 由存储分量聚合、两阶段分层自助法、逐字节确定、非排名运行不进榜、版本冻结、重算性能。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from awr.agentsdk.schemas import bench_validator
from awr.bench.boardstore import BoardVersionError, append_rows, load_rows, publish
from awr.bench.eval.aggregate import METRICS, build_board, lane_metrics

sys.path.insert(0, str(Path(__file__).parent))
from _rows import entries, row


def _rows() -> list[dict]:
    out = []
    for s in range(6):
        for rep in range(2):
            out.append(row("t1/a", s, 0.8, 0.5 + 0.02 * s, 0.6, 0.3 + 0.01 * rep, event="ev1"))
            out.append(row("t1/a", s, 0.4, 0.3, 0.2 + 0.01 * s, 0.1, event="ev2"))
            out.append(row("baseline/x", s, 0.5, 0.2, 0.3, 0.2, event="ev1", baseline=True))
            out.append(row("baseline/x", s, 0.5, 0.2, 0.3, 0.2, event="ev2", baseline=True))
    return out


def test_lane_values_are_event_equal_means_of_stored_components() -> None:
    rows = _rows()
    b = build_board(rows, board_version="v1", n_boot=500)
    assert not list(bench_validator("board").iter_errors(b))
    a = entries(b)["t1/a"]["metrics"]
    for m in METRICS:
        per_event = [np.mean([r["evaluation"]["components"][m] for r in rows if r["entry_id"] == "t1/a" and r["event"] == ev]) for ev in ("ev1", "ev2")]
        assert a[m]["value"] == round(float(np.mean(per_event)), 6)
        lo, hi = a[m]["ci95"]
        assert lo <= a[m]["value"] <= hi
    assert a["ce"]["repeat_delta"] == pytest.approx(0.01)                     # 同一种子两次重复的最大差
    assert json.dumps(b) == json.dumps(build_board(rows, board_version="v1", n_boot=500))   # 同一输入逐字节一致
    assert json.dumps(b) != json.dumps(build_board(rows, board_version="v2", n_boot=500))   # 重采样种子随榜单版本


def test_board_display_equals_stored_aggregation(tmp_path: Path) -> None:
    d = tmp_path / "v1"
    d.mkdir()
    (d / "config.json").write_text("{}")
    append_rows(d, [{**r, "baseline": True} for r in _rows()])                  # publish 只列基线与已完成的上榜提交
    board = publish(d, n_boot=400)
    on_disk = json.loads((d / "board.json").read_text())
    assert on_disk == board
    met, _ = lane_metrics([r for r in load_rows(d) if r["entry_id"] == "t1/a"], key=("t1/a", "v1", "rendezvous_handoff"), n_boot=400)
    shown = entries(on_disk)["t1/a"]["metrics"]
    assert {m: (shown[m]["value"], shown[m]["ci95"]) for m in METRICS} == {m: (met[m]["value"], met[m]["ci95"]) for m in METRICS}
    assert "composite" not in json.dumps(on_disk) and '"weights"' not in json.dumps(on_disk)   # weights = null：不出现合成主得分


def test_unranked_runs_never_enter_the_board() -> None:
    rows = [row("t1/a", s, 0.5, 0.5, 0.5, 0.5) for s in range(3)]
    rows += [row("t2/fast", s, 0.9, 0.9, 0.9, 0.9, valid=False, reasons=["physics_not_ranked"]) for s in range(3)]   # Fast 档
    rows += [row("t3/dev", s, 0.9, 0.9, 0.9, 0.9, ranked=False) for s in range(3)]                                      # 开发运行
    b = build_board(rows, board_version="v1", n_boot=200)
    assert set(entries(b)) == {"t1/a"}
    dev = build_board(rows, board_version="v1", n_boot=200, track={"dev": True})                                      # 开发榜（自检）
    assert set(entries(dev)) == {"t1/a", "t2/fast", "t3/dev"}


def test_score_version_or_lane_change_needs_new_board_version(tmp_path: Path) -> None:
    d = tmp_path / "v1"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"score_version": "d3-score-0"}))
    append_rows(d, [row("baseline/a", s, 0.5, 0.5, 0.5, 0.5, baseline=True) for s in range(3)])
    publish(d, n_boot=100)
    before = (d / "board.json").read_bytes()
    append_rows(d, [row("baseline/a", s, 0.5, 0.5, 0.5, 0.5, baseline=True, lane="vehicle_track") for s in range(3)])
    with pytest.raises(BoardVersionError):
        publish(d, n_boot=100)
    (d / "config.json").write_text(json.dumps({"score_version": "d3-score-1"}))
    with pytest.raises(BoardVersionError):
        publish(d, n_boot=100)
    assert (d / "board.json").read_bytes() == before                      # 旧版本只读


@pytest.mark.skipif(os.environ.get("AWR_D3_PERF") == "0", reason="perf bound disabled (AWR_D3_PERF=0)")
def test_recompute_100_entries_4_lanes_within_60s() -> None:
    rng = np.random.default_rng(7)
    rows = []
    for lane in ("reid_occlusion", "rendezvous_handoff", "vehicle_track", "target_search"):
        for i in range(100):
            mu = rng.random(4)
            for s in range(10):
                for _rep in range(2):
                    c = np.clip(mu + rng.normal(0, 0.05, 4), 0, 1)
                    rows.append(row(f"team{i}/e{i}", s, *map(float, c), lane=lane, event=f"{lane}-001"))
    t0 = time.perf_counter()
    b = build_board(rows, board_version="perf", n_boot=10_000)
    dt = time.perf_counter() - t0
    assert len(b["entries"]) == 400 and dt <= 60.0, dt
