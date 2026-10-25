"""提交结果与已发布榜单的同步（`awr.bench.board.results`；B21 遗留 10；D3-AC-43、D3-AC-57）：每次发布后提交文档中的名次、并列组与
层级以榜单为准，不在榜的去掉名次与层级，其他榜单版本与未完成的提交不变，内容不变时不写回。"""

from __future__ import annotations

import json
from pathlib import Path

from awr.bench.board.results import refresh_results
from awr.bench.submissions import SubmissionStore


def _met(v: float, rank: int | None = None) -> dict:
    m = {"value": v, "ci95": [v - 0.1, v + 0.1], "repeat_delta": 0.0}
    if rank is not None:
        m.update(rank=rank, tie_group=rank - 1)
    return {k: dict(m) for k in ("ec", "eu", "ao", "ce")}


def _doc(st: SubmissionStore, sid: str, *, team: str, bv: str = "v9", state: str = "DONE", tier: int | None = 1) -> None:
    lane = {"lane": "reid", "metrics": _met(0.5, 1), "runs": {"total": 20, "valid": 20, "invalid": 0}, "status": "valid", "composite": None}
    if tier is not None:
        lane["dominance_tier"] = tier
    st._put({"schema": "drone4d.bench.submission.v1", "submission_id": sid, "track": "std3-l2", "image_digest": "sha256:" + "a" * 64,
             "entry": {"cmd": ["x"]}, "state": state, "visibility": "board", "position": 0, "result": {"lanes": [lane]},
             "_meta": {"owner": "o", "team": team, "role": "researcher", "kind": "formal", "created_unix_ns": 1, "updated_unix_ns": 1,
                       "board_version": bv, "history": [], "gpu": []}})


def test_refresh_results_follows_the_published_board(tmp_path: Path) -> None:
    root = tmp_path / "d3"
    bdir = root / "boards" / "v9"
    bdir.mkdir(parents=True)
    st = SubmissionStore(root / "submissions")
    _doc(st, "sub-aaaaaaaaaaaaaaaa", team="t1")                                # 在榜：层级由 1 变 2
    _doc(st, "sub-bbbbbbbbbbbbbbbb", team="t2")                                # 不在榜（被取代或已撤回）：去掉名次与层级
    _doc(st, "sub-cccccccccccccccc", team="t3", bv="v8")                       # 其他榜单版本：不变
    _doc(st, "sub-dddddddddddddddd", team="t4", state="SCORING")               # 未完成：不变
    board = {"entries": [{"entry_id": "t1/sub-aaaaaaaaaaaaaaaa", "track": "std3-l2", "lane": "reid", "baseline": False, "metrics": _met(0.4, 2),
                          "status": "valid", "dominance_tier": 2},
                         {"entry_id": "baseline/static_guard", "track": "std3-l2", "lane": "reid", "baseline": True, "metrics": _met(0.3),
                          "status": "valid", "dominance_tier": 1}]}
    before = {p.name: p.read_bytes() for p in (root / "submissions").glob("*.json")}
    got = refresh_results(bdir, board, now_ns=123)
    assert sorted(got) == ["sub-aaaaaaaaaaaaaaaa", "sub-bbbbbbbbbbbbbbbb"]
    a = st.get("sub-aaaaaaaaaaaaaaaa")
    ln = a["result"]["lanes"][0]
    assert ln["metrics"] == board["entries"][0]["metrics"] and ln["dominance_tier"] == 2 and ln["runs"]["total"] == 20
    assert a["_meta"]["result_board"]["board_version"] == "v9" and a["_meta"]["result_board"]["refreshed_unix_ns"] == 123
    b = st.get("sub-bbbbbbbbbbbbbbbb")["result"]["lanes"][0]
    assert "dominance_tier" not in b and all("rank" not in m and "tie_group" not in m for m in b["metrics"].values())
    assert b["metrics"]["ec"]["value"] == 0.5                                   # 点值与区间保留评分时的值
    for sid in ("sub-cccccccccccccccc", "sub-dddddddddddddddd"):
        assert (root / "submissions" / f"{sid}.json").read_bytes() == before[f"{sid}.json"]
    snap = {p.name: p.read_bytes() for p in (root / "submissions").glob("*.json")}
    assert refresh_results(bdir, board, now_ns=456) == []                        # 内容不变：不写回
    assert {p.name: p.read_bytes() for p in (root / "submissions").glob("*.json")} == snap
    board["entries"][0]["dominance_tier"] = None                                   # 层级为空（未完成或无效）：去掉字段
    refresh_results(bdir, board)
    assert "dominance_tier" not in st.get("sub-aaaaaaaaaaaaaaaa")["result"]["lanes"][0]
    assert refresh_results(tmp_path / "nowhere" / "boards" / "v9", board) == []   # 无提交目录：不做任何事
    assert not (tmp_path / "nowhere" / "submissions").exists()


def test_publish_refreshes_results(tmp_path: Path) -> None:
    """`boardstore.publish` 每次发布后调用同步：原型榜上的提交文档随之更新（与评分时快照不同的名次被改正）。"""
    from awr.bench.boardstore import append_rows, publish

    root = tmp_path / "d3"
    bdir = root / "boards" / "proto-9"
    bdir.mkdir(parents=True)
    (bdir / "config.json").write_text(json.dumps({"score_version": "d3-score-0"}))
    st = SubmissionStore(root / "submissions")
    rows = []
    for sid, team, v in (("sub-aaaaaaaaaaaaaaaa", "t1", 0.2), ("sub-bbbbbbbbbbbbbbbb", "t2", 0.8)):
        _doc(st, sid, team=team, bv="proto-9")
        rows += [{"entry_id": f"{team}/{sid}", "team": team, "submission_id": sid, "track": "std3-l2", "lane": "reid", "event": "e1", "seed": s,
                  "evaluation": {"components": {"ec": v + 0.01 * s, "eu": v, "ao": v, "ce": v}, "ranked": True, "ranked_valid": True,
                                 "invalid_reasons": []}} for s in range(5)]
    append_rows(bdir, rows)
    board = publish(bdir, st.board_view(), n_boot=200)
    ent = {e["entry_id"]: e for e in board["entries"]}
    for sid, team in (("sub-aaaaaaaaaaaaaaaa", "t1"), ("sub-bbbbbbbbbbbbbbbb", "t2")):
        ln = st.get(sid)["result"]["lanes"][0]
        assert ln["metrics"] == ent[f"{team}/{sid}"]["metrics"]
    assert st.get("sub-aaaaaaaaaaaaaaaa")["result"]["lanes"][0]["metrics"]["ec"]["rank"] == 2   # 评分时快照为第 1 名，按榜单改为第 2 名
