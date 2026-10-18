"""D3-AC-57 排名规则：统计并列 (a)、非支配层级 (b)、无效条目 (c)、团队代表条目 (e)、分轨与赛道不混排 (g)。"""

from __future__ import annotations

import sys
from pathlib import Path

from awr.agentsdk.schemas import bench_validator
from awr.bench.boardstore import _listed
from awr.bench.eval.aggregate import METRICS, build_board

sys.path.insert(0, str(Path(__file__).parent))
from _rows import entries, row


def _noisy(eid: str, base: tuple[float, float, float, float], n: int = 8, amp: float = 0.0, **kw) -> list[dict]:
    """n 个种子；`amp` 为逐种子交替扰动幅度（两条目均值相同、区间重叠时构成统计并列）。"""
    return [row(eid, s, *(min(1.0, max(0.0, v + (amp if s % 2 else -amp))) for v in base), **kw) for s in range(n)]


def test_paired_interval_containing_zero_is_a_statistical_tie() -> None:
    rows = _noisy("t1/a", (0.80, 0.5, 0.5, 0.5), amp=0.2) + _noisy("t2/b", (0.78, 0.5, 0.5, 0.5), amp=0.2) + _noisy("t3/c", (0.2, 0.5, 0.5, 0.5))
    e = entries(build_board(rows, board_version="v1", n_boot=2000))
    a, b, c = (e[k]["metrics"]["ec"] for k in ("t1/a", "t2/b", "t3/c"))
    assert a["rank"] == b["rank"] == 1 and a["tie_group"] == b["tie_group"] == 0      # 区间含 0：同一名次、统计并列
    assert c["rank"] == 3 and c["tie_group"] == 1
    assert a["value"] > b["value"]                                                  # 点估计不同仍并列


def test_dominance_tiers_and_incomparable_entries() -> None:
    rows = (_noisy("t1/a", (0.9, 0.9, 0.5, 0.5)) + _noisy("t2/b", (0.5, 0.5, 0.5, 0.5))      # A 支配 B
            + _noisy("t3/c", (0.2, 0.95, 0.9, 0.5))                                           # C 与 A 不可比（EC 低、EU/AO 高）
            + _noisy("t4/d", (0.1, 0.1, 0.1, 0.1))                                            # B、C 都支配 D
            + _noisy("baseline/x", (0.95, 0.95, 0.95, 0.95), baseline=True))                  # 参考行不影响他者层级
    b = build_board(rows, board_version="v1", n_boot=1000)
    assert not list(bench_validator("board").iter_errors(b))
    tier = {k: v["dominance_tier"] for k, v in entries(b).items()}
    assert tier == {"t1/a": 1, "t3/c": 1, "t2/b": 2, "t4/d": 3, "baseline/x": 1}
    e = entries(b)
    assert e["t3/c"]["metrics"]["ec"]["rank"] == 3 and e["t3/c"]["metrics"]["eu"]["rank"] == 1     # 层级不是名次：逐项名次独立
    assert all("rank" not in e["baseline/x"]["metrics"][m] for m in METRICS)


def test_entry_with_21_percent_invalid_runs_is_marked_invalid() -> None:
    bad = [row("t1/bad", s, 0.99, 0.99, 0.99, 0.99, valid=s >= 21) for s in range(100)]       # 21% 无效
    edge = [row("t2/edge", s, 0.6, 0.6, 0.6, 0.6, valid=s >= 20) for s in range(100)]       # 20%：仍有效
    rest = _noisy("t3/c", (0.5, 0.5, 0.5, 0.5))
    b = build_board(bad + edge + rest, board_version="v1", n_boot=300)
    assert not list(bench_validator("board").iter_errors(b))
    e = entries(b)
    assert e["t1/bad"]["status"] == "invalid" and e["t1/bad"]["dominance_tier"] is None
    assert e["t1/bad"]["metrics"]["ec"]["value"] == 0.99 and all("rank" not in e["t1/bad"]["metrics"][m] for m in METRICS)
    assert e["t2/edge"]["status"] == "valid" and e["t2/edge"]["metrics"]["ec"]["rank"] == 1 and e["t2/edge"]["dominance_tier"] == 1
    assert e["t3/c"]["metrics"]["ec"]["rank"] == 2 and e["t3/c"]["dominance_tier"] == 2       # 无效条目不压低他者


def test_one_representative_entry_per_team_track_lane() -> None:
    def sub(sid: str, team: str, lane: str = "rendezvous_handoff") -> list[dict]:
        return [row(f"{team}/{sid}", s, 0.5, 0.5, 0.5, 0.5, team=team, lane=lane, submission_id=sid) for s in range(2)]

    rows = sub("sub-a1", "ta") + sub("sub-a2", "ta") + sub("sub-a3", "ta", lane="vehicle_track") + sub("sub-b1", "tb") + sub("sub-b2", "tb")
    subs = {"sub-a1": {"state": "DONE", "visibility": "board", "team": "ta", "done_unix_ns": 1},
            "sub-a2": {"state": "DONE", "visibility": "board", "team": "ta", "done_unix_ns": 2},      # 同团队同赛道：最近完成者代表
            "sub-a3": {"state": "DONE", "visibility": "board", "team": "ta", "done_unix_ns": 0},      # 其它赛道：单独代表
            "sub-b1": {"state": "DONE", "visibility": "private", "team": "tb", "done_unix_ns": 9},    # 私有：不上榜
            "sub-b2": {"state": "EVALUATING", "visibility": "board", "team": "tb"}}                   # 未完成：不上榜
    listed = {r["entry_id"] for r in _listed(rows, subs)}
    assert listed == {"ta/sub-a2", "ta/sub-a3"}


def test_tracks_and_lanes_are_ranked_separately() -> None:
    rows = (_noisy("t1/a", (0.9, 0.9, 0.9, 0.9)) + _noisy("t2/b", (0.1, 0.1, 0.1, 0.1), track="std3-l2")
            + _noisy("t3/c", (0.2, 0.2, 0.2, 0.2), lane="vehicle_track"))
    b = build_board(rows, board_version="v1", n_boot=300)
    e = entries(b)
    assert all(e[k]["metrics"]["ec"]["rank"] == 1 and e[k]["dominance_tier"] == 1 for k in e)    # 各组只有一个条目：互不混排
    assert {(x["track"], x["lane"]) for x in b["entries"]} == {("std3-l1", "rendezvous_handoff"), ("std3-l2", "rendezvous_handoff"),
                                                              ("std3-l1", "vehicle_track")}
    assert {t["track"] for t in b["tracks"]} == {"std3-l1", "std3-l2"} and {ln["lane"] for ln in b["lanes"]} == {"rendezvous_handoff", "vehicle_track"}
