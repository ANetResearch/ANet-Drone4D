"""BN-47 评测结果与 BN-83、BN-84、BN-97 内部排行榜（AWR-17 §18.4；M27 §6.7.9；P0a 原型榜）。

榜单目录 `<data_root>/boards/<board_version>/{board.json, details.json}`（`tools/bench/d3/proto_board.py` 产出后发布到此处）；
当前版本取 `<data_root>/boards/current`（一行版本号），缺省为字典序最大的版本。榜单接口不对外、无静态制品副本（D3-AC-21），
只返回 `bench/board.v1` 的点估计、置信区间、名次与层级，不含合成值。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request

from awr.api.problem import ApiProblem
from awr.bench.api.deps import AnyMember, Researcher
from awr.bench.boardstore import current_version as _current
from awr.bench.boardstore import versions as _versions
from awr.bench.eval.thresholds import SCORE_VERSION
from awr.bench.runs_store import find_run

router = APIRouter()


def _boards_dir(request: Request) -> Path:
    return Path(request.app.state.settings.data_root) / "boards"


def _load(request: Request, version: str | None, name: str = "board.json") -> tuple[str, dict]:
    root = _boards_dir(request)
    versions = _versions(root)
    v = version or _current(root, versions)
    if v is None:
        raise ApiProblem(682, detail={"why": "board_closed"})
    if v not in versions:
        raise ApiProblem(305, detail={"board_version": version})
    p = root / v / name
    if not p.is_file():
        raise ApiProblem(305, detail={"board_version": v, "file": name})
    return v, json.loads(p.read_text(encoding="utf-8"))


@router.get("/runs/{rid}/evaluation")
def evaluation(rid: str, request: Request, p: Researcher) -> dict:
    d = find_run(request.app.state.settings.dataset_dir, rid)
    f = d / "evaluation.json" if d else None
    if f is None or not f.is_file():
        raise ApiProblem(305, detail={"run_id": rid})
    return json.loads(f.read_text(encoding="utf-8"))


@router.get("/board")
def board(request: Request, p: AnyMember) -> dict:
    root = _boards_dir(request)
    versions = _versions(root)
    cur = _current(root, versions)
    out = []
    for v in versions:
        b = json.loads((root / v / "board.json").read_text(encoding="utf-8"))
        out.append({"board_version": v, "current": v == cur, "score_version": SCORE_VERSION, "weights": None, "tracks": b["tracks"],
                    "lanes": b["lanes"], "seed_set_commit": b["seed_set_commit"], "platform_digest": b["platform_digest"],
                    "evaluator_major": b["evaluator_major"], "n_entries": len(b["entries"])})
    return {"current": cur, "versions": out}


@router.get("/leaderboard")
def leaderboard(request: Request, p: AnyMember, track: str | None = None, lane: str | None = None, board_version: str | None = None) -> dict:
    _v, b = _load(request, board_version)
    if track is not None and track not in {t["track"] for t in b["tracks"]}:
        raise ApiProblem(682, detail={"why": "track_unknown", "track": track})
    if lane is not None and lane not in {ln["lane"] for ln in b["lanes"]}:
        raise ApiProblem(682, detail={"why": "lane_unknown", "lane": lane})
    ents = [e for e in b["entries"] if (track is None or e["track"] == track) and (lane is None or e["lane"] == lane)]
    return {**b, "tracks": [t for t in b["tracks"] if track is None or t["track"] == track],
            "lanes": [ln for ln in b["lanes"] if lane is None or ln["lane"] == lane], "entries": ents}


@router.get("/board/{track}/entries/{entry_id:path}")
def entry(track: str, entry_id: str, request: Request, p: AnyMember, board_version: str | None = None) -> dict:
    v, b = _load(request, board_version)
    rows = [e for e in b["entries"] if e["track"] == track and e["entry_id"] == entry_id]
    if not rows:
        raise ApiProblem(305, detail={"track": track, "entry_id": entry_id})
    _v, det = _load(request, v, "details.json")
    d = det.get(entry_id, {})
    return {"board_version": v, "entry_id": entry_id, "track": track, "display_name": rows[0]["display_name"], "team": rows[0]["team"],
            "baseline": rows[0]["baseline"], "lanes": [{"lane": e["lane"], "metrics": e["metrics"], "dominance_tier": e["dominance_tier"],
                                                        "events": d.get("tracks", {}).get(track, {}).get(e["lane"], [])} for e in rows]}
