"""排行榜 v1 的分轨、赛道与排名呈现（D3-AC-43、D3-AC-57；AWR-05 §5.6.9；M27-FR-177 至 FR-182、M27-AC-051）：分轨与赛道取自榜单版本
配置并互不混排；上榜条件为该赛道全部 test 事件的 K × R 个 Run 完成；无效 Run 占比 > 20% 标注无效；逐项名次与统计并列；非支配层级；
缺省显示次序为层级升序、同层按首次上榜时刻；种子明文不得混入评测行；合成接口与重算性能。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from awr.agentsdk.schemas import bench_validator
from awr.bench.boardstore import BoardPhysicsError, append_rows, compose_board, publish
from awr.bench.eval.aggregate import (
    EVENT_LANES,
    BoardConfigError,
    admits,
    build_board,
    compose,
    entry_details,
    run_spec_for,
)

sys.path.insert(0, str(Path(__file__).parent))
from _board_v1 import LANE_EVENTS, seal, v1_rows


def _by(board: dict, track: str = "std3-l2", lane: str = "handoff") -> dict[str, dict]:
    return {e["entry_id"]: e for e in board["entries"] if e["track"] == track and e["lane"] == lane}


def test_tracks_and_lanes_come_from_the_board_version_and_never_mix(tmp_path: Path) -> None:
    _d, cfg, _ = seal(tmp_path)
    rows = v1_rows("t1/a", (0.7, 0.5, 0.5, 0.3), lanes=("handoff", "reid"), track="std3-l2")
    rows += v1_rows("t1/a", (0.2, 0.2, 0.2, 0.2), lanes=("handoff",), track="std3-l1")          # 同一条目在另一分轨：分开计算
    stray = v1_rows("t9/z", (0.9, 0.9, 0.9, 0.9))
    rows += [{**r, "track": "std5-l2"} for r in stray]                                          # 未登记分轨
    rows += [{**r, "lane": "mixed4"} for r in stray]                                            # 未登记赛道
    rows += [{**r, "event": "handoff-d01"} for r in stray]                                      # 不在该赛道 test 事件清单中（如 dev 事件）
    rows += [{**r, "lane": "reid"} for r in stray]                                              # 事件属于另一赛道
    b = build_board(rows, board_version="v1", n_boot=300, board=cfg)
    assert not list(bench_validator("board").iter_errors(b))
    assert [t["track"] for t in b["tracks"]] == ["std3-l2", "std3-l1"] and all(t["appearance"] == "mesh" for t in b["tracks"])
    assert [(ln["lane"], ln["event_type"], ln["test_events"]) for ln in b["lanes"]] == [(k, v, LANE_EVENTS[k]) for k, v in EVENT_LANES.items()]
    assert {(e["entry_id"], e["track"], e["lane"]) for e in b["entries"]} == {("t1/a", "std3-l2", "handoff"), ("t1/a", "std3-l2", "reid"),
                                                                              ("t1/a", "std3-l1", "handoff")}
    assert _by(b)["t1/a"]["metrics"]["ec"]["value"] == 0.7 and _by(b, "std3-l1")["t1/a"]["metrics"]["ec"]["value"] == 0.2
    assert (b["seeds_per_event"], b["repeats"], b["score_version"]) == (5, 2, "d3-score-1")
    assert [(e["track"], e["lane"]) for e in b["entries"]] == [("std3-l2", "reid"), ("std3-l2", "handoff"), ("std3-l1", "handoff")]  # 配置顺序
    det = entry_details(rows, board=cfg)
    assert set(det) == {"t1/a"} and set(det["t1/a"]["tracks"]) == {"std3-l2", "std3-l1"}
    assert [x["event"] for x in det["t1/a"]["tracks"]["std3-l2"]["handoff"]] == LANE_EVENTS["handoff"]


def test_an_entry_is_ranked_only_when_all_k_by_r_runs_are_complete(tmp_path: Path) -> None:
    _d, cfg, _ = seal(tmp_path)
    full = v1_rows("t1/a", (0.7, 0.6, 0.5, 0.4))
    short = v1_rows("t2/b", (0.9, 0.9, 0.9, 0.9))[:-1]                                         # 缺 1 个 Run（handoff-t02 种子 4 的第 2 次重复）
    one_event = [r for r in v1_rows("t3/c", (0.9, 0.9, 0.9, 0.9)) if r["event"] == "handoff-t01"]  # 只完成一个 test 事件
    extra = v1_rows("t4/d", (0.5, 0.5, 0.5, 0.5)) + v1_rows("t4/d", (0.5, 0.5, 0.5, 0.5))[:3]       # 多出的 Run 不影响完成判定
    e = _by(build_board(full + short + one_event + extra, board_version="v1", n_boot=300, board=cfg))
    assert e["t2/b"]["status"] == e["t3/c"]["status"] == "incomplete" and e["t1/a"]["status"] == e["t4/d"]["status"] == "valid"
    for k in ("t2/b", "t3/c"):
        assert e[k]["dominance_tier"] is None and all("rank" not in m for m in e[k]["metrics"].values())
        assert e[k]["metrics"]["ec"]["value"] == 0.9                                           # 指标照常显示
    assert e["t1/a"]["metrics"]["ec"]["rank"] == 1 and e["t1/a"]["dominance_tier"] == 1        # 未完成条目不参与名次与分层


def test_more_than_20_percent_invalid_runs_mark_the_entry_invalid(tmp_path: Path) -> None:
    _d, cfg, _ = seal(tmp_path)
    rows = v1_rows("t1/a", (0.6, 0.6, 0.6, 0.6), invalid=4) + v1_rows("t2/b", (0.6, 0.6, 0.6, 0.6), invalid=5)   # 每赛道 20 个 Run：20% 与 25%
    e = _by(build_board(rows, board_version="v1", n_boot=300, board=cfg))
    assert e["t1/a"]["status"] == "valid" and e["t1/a"]["metrics"]["ec"]["rank"] == 1
    assert e["t2/b"]["status"] == "invalid" and e["t2/b"]["dominance_tier"] is None and "rank" not in e["t2/b"]["metrics"]["ec"]


def test_ties_tiers_and_default_display_order(tmp_path: Path) -> None:
    _d, cfg, _ = seal(tmp_path)
    rows = (v1_rows("t1/a", (0.80, 0.80, 0.50, 0.50), noise=0.15, rng_seed=1, first_listed_unix_ns=300)
            + v1_rows("t2/b", (0.78, 0.80, 0.50, 0.50), noise=0.15, rng_seed=2, first_listed_unix_ns=200)   # 与 A 的 EC 统计并列
            + v1_rows("t3/c", (0.20, 0.95, 0.95, 0.50), noise=0.01, rng_seed=3, first_listed_unix_ns=100)   # 与 A、B 不可比
            + v1_rows("t4/d", (0.10, 0.10, 0.10, 0.10), noise=0.01, rng_seed=4, first_listed_unix_ns=50)    # 被 C 支配
            + v1_rows("t5/e", (0.90, 0.90, 0.90, 0.90), invalid=10, first_listed_unix_ns=10)                 # 无效
            + v1_rows("baseline/x", (0.99, 0.99, 0.99, 0.99), baseline=True))
    b = build_board(rows, board_version="v1", n_boot=2000, board=cfg)
    e = _by(b)
    a_, b_ = e["t1/a"]["metrics"]["ec"], e["t2/b"]["metrics"]["ec"]
    assert a_["rank"] == b_["rank"] == 1 and a_["tie_group"] == b_["tie_group"] and a_["value"] != b_["value"]   # 统计并列：同一名次
    assert e["t3/c"]["metrics"]["ec"]["rank"] == 3 and e["t3/c"]["metrics"]["eu"]["rank"] == 1               # 逐项名次独立
    tiers = {k: v["dominance_tier"] for k, v in e.items()}
    assert tiers["t3/c"] == 1 and tiers["t4/d"] == 2 and tiers["t5/e"] is None and tiers["baseline/x"] == 1
    assert tiers["t1/a"] == tiers["t2/b"] == 1                                                             # 两者互不支配（区间重叠）
    order = [x["entry_id"] for x in b["entries"] if x["lane"] == "handoff"]
    assert order == ["t3/c", "t2/b", "t1/a", "t4/d", "t5/e", "baseline/x"]                                 # 层 1 按首次上榜时刻，层 2，无效，基线
    assert [x["display_order"] for x in b["entries"] if x["lane"] == "handoff"] == [1, 2, 3, 4, 5, 6]
    assert json.dumps(b) == json.dumps(build_board(list(reversed(rows)), board_version="v1", n_boot=2000, board=cfg))  # 与行序无关、逐字节一致


def test_rows_must_carry_seed_indices_not_seed_values(tmp_path: Path) -> None:
    _d, cfg, store = seal(tmp_path)
    rows = v1_rows("t1/a", (0.5, 0.5, 0.5, 0.5))
    rows[0] = {**rows[0], "seed": store["seeds"]["handoff"]["handoff-t01"][0]}
    with pytest.raises(BoardConfigError, match="seed indices only"):
        build_board(rows, board_version="v1", n_boot=100, board=cfg)
    with pytest.raises(BoardConfigError, match="published as v2"):
        build_board(rows[1:], board_version="v2", n_boot=100, board=cfg)


def test_unranked_and_non_gz_rows_never_enter_board_v1(tmp_path: Path) -> None:
    d, _cfg, _ = seal(tmp_path)
    good = v1_rows("baseline/a", (0.5, 0.5, 0.5, 0.5), baseline=True)
    fast = [{**r, "entry_id": "baseline/f", "evaluation": {**r["evaluation"], "invalid_reasons": ["physics_not_ranked"]}} for r in good]
    dev = [{**r, "entry_id": "baseline/g", "evaluation": {**r["evaluation"], "ranked": False}} for r in good]
    append_rows(d, good + fast + dev)
    sitl = [{**r, "entry_id": "baseline/s", "evaluation": {**r["evaluation"], "physics": "sitl"}} for r in good]
    with pytest.raises(BoardPhysicsError):                                                      # 正式榜只收 gz 锁步档
        append_rows(d, sitl)
    b = publish(d, n_boot=200)
    assert {e["entry_id"] for e in b["entries"]} == {"baseline/a"}


def test_compose_recomputes_from_stored_components_with_the_board_key(tmp_path: Path) -> None:
    d, cfg, _ = seal(tmp_path)
    rows = v1_rows("baseline/a", (0.8, 0.6, 0.4, 0.2), noise=0.05, baseline=True) + v1_rows("baseline/b", (0.3, 0.3, 0.3, 0.3), noise=0.05,
                                                                                               rng_seed=9, baseline=True)
    append_rows(d, rows)
    board = publish(d, n_boot=500)
    assert "composite" not in json.dumps(board) and '"weights"' not in json.dumps(board)       # weights = null：榜单无合成值
    w = {"ec": 0.4, "eu": 0.2, "ao": 0.2, "ce": 0.2}                                          # 测试用评分版本的权重
    one, two = compose_board(d, w, n_boot=500), compose_board(d, w, n_boot=500)
    assert json.dumps(one) == json.dumps(two) and [x["entry_id"] for x in one] == ["baseline/a", "baseline/b"]
    a = next(e for e in board["entries"] if e["entry_id"] == "baseline/a")
    assert one[0]["value"] == round(sum(w[m] * a["metrics"][m]["value"] for m in w), 6)
    assert json.dumps(compose(rows, w, board_version="v1", n_boot=500, board=cfg)) == json.dumps(one)


def test_run_spec_and_admission_for_a_track(tmp_path: Path) -> None:
    _d, cfg, _ = seal(tmp_path)
    starts = [{"kind": "pose", "pos_enu_m": [76.0, -78.0 - 4 * i, 0.0], "yaw_deg": 90.0} for i in range(3)]
    rs = run_spec_for(cfg, "std3-l1", world_id="oryahovo-crop1", event_id="handoff-t01", event_version=1, starts=starts,
                      policy={"kind": "reference", "name": "track_share"})
    assert not list(bench_validator("run_spec").iter_errors(rs))
    assert rs["obs_level"] == "L1" and rs["physics"] == "realistic" and rs["ranked"] is True and "seeds" not in rs and "seed" not in rs
    assert [f["vehicle_ref"] for f in rs["fleet"]] == ["p600_bench"] * 3 and rs["comm"]["model"] == "fspl-nlos@1"
    with pytest.raises(BoardConfigError, match="flies 3 UAVs"):
        run_spec_for(cfg, "std3-l2", world_id="w", event_id="e", event_version=1, starts=starts[:2], policy={})
    assert admits(cfg, "std3-l2") is None and admits(cfg, "std3-l2", ["reid", "search"]) is None
    assert admits(cfg, "std5-l2") == "track_unknown" and admits(cfg, "std3-l1", ["mixed4"]) == "lane_unknown"


@pytest.mark.skipif(os.environ.get("AWR_D3_PERF") == "0", reason="perf bound disabled (AWR_D3_PERF=0)")
def test_board_v1_recompute_100_entries_4_lanes_within_60s(tmp_path: Path) -> None:
    _d, cfg, _ = seal(tmp_path)
    rng = np.random.default_rng(11)
    rows = []
    for i in range(100):
        mu = tuple(float(x) for x in rng.random(4))
        rows += v1_rows(f"team{i}/e{i}", mu, lanes=tuple(EVENT_LANES), noise=0.05, rng_seed=i, first_listed_unix_ns=i)
    t0 = time.perf_counter()
    b = build_board(rows, board_version="v1", n_boot=10_000, board=cfg)
    dt = time.perf_counter() - t0
    assert len(b["entries"]) == 400 and all(e["status"] == "valid" for e in b["entries"]) and dt <= 60.0, dt
