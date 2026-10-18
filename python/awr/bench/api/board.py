"""BN-47 评测结果与 BN-83、BN-84、BN-97 内部排行榜（AWR-17 §18.4；M27 §6.7.9；P0a 原型榜）。

榜单目录 `<data_root>/boards/<board_version>/{board.json, details.json}`（`tools/bench/d3/proto_board.py` 产出后发布到此处）；
当前版本取 `<data_root>/boards/current`（一行版本号），缺省为字典序最大的版本。榜单接口不对外、无静态制品副本（D3-AC-21），
只返回 `bench/board.v1` 的点估计、置信区间、名次与层级，不含合成值。

可见性（D3-AC-43；AWR-17 §18.4、API-AC-097）：BN-83、BN-84、BN-97 的最低角色为 `invited`；匿名访问（无凭据或凭据无效、过期）
与经公开站主机名（`Host` 或 `X-Forwarded-Host` 属于 `PUBLIC_HOSTS`，可由 `settings.extra["public_hosts"]` 覆盖）到达的请求一律返回
404 `305`（与未注册路由同形，不暴露接口存在），并写 `deny` 审计（`why = anonymous | public_site`）；角色不足仍为 403 `115`。
撤回下线：条目在读取时对照提交的当前状态（`boardstore.entry_listed`），提交者撤回（可见性改回 `private`）或提交不再满足上榜规则的
条目立即不返回（BN-97 的条目、BN-83 的 `n_entries`、BN-84 返回 305）；榜单重算由提交接口与 `boardstore.delist_withdrawn` 完成。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from awr.api.problem import ApiProblem
from awr.bench.api.deps import Researcher, _audit, authenticate, require
from awr.bench.auth import Principal
from awr.bench.boardstore import current_version as _current
from awr.bench.boardstore import entry_listed, load_listing
from awr.bench.boardstore import versions as _versions
from awr.bench.eval.thresholds import SCORE_VERSION
from awr.bench.runs_store import find_run
from awr.bench.submissions import SubmissionStore

router = APIRouter()

PUBLIC_HOSTS = ("drone4d.agentnetwork.org.cn",)          # 公开站（emax）主机名：榜单接口一律 404（D3-AC-21、43）
_ANON = frozenset({301, 302})
_invited = require("invited")


def _host(v: str | None) -> str:
    v = (v or "").split(",")[0].strip().lower()
    return v[1:].split("]")[0] if v.startswith("[") else v.split(":")[0]


def public_site(request: Request) -> bool:
    """请求是否经公开站主机名到达（`X-Forwarded-Host` 的首个值或 `Host`）。"""
    hosts = {h.lower() for h in (request.app.state.settings.extra or {}).get("public_hosts", PUBLIC_HOSTS)}
    return any(_host(request.headers.get(k)) in hosts for k in ("x-forwarded-host", "host"))


def _board_member(request: Request):
    """榜单读取依赖：最低角色 `invited`（委托 `require`，保留其审计与 OpenAPI 标注）；公开站与匿名访问返回 404 `305`。"""
    if public_site(request):
        _audit(request, None, "deny", code=305, status=404, why="public_site")
        raise ApiProblem(305)
    try:
        authenticate(request)
    except ApiProblem as e:
        if e.code in _ANON:
            _audit(request, None, "deny", code=305, status=404, why="anonymous")
            raise ApiProblem(305) from None
        raise
    yield from _invited(request)


_board_member._d3_role = "invited"            # deps.route_role 从依赖树读取
_board_member._d3_aud = None
BoardMember = Annotated[Principal, Depends(_board_member)]


def _subs(request: Request) -> dict[str, dict]:
    st = getattr(request.app.state, "submissions", None)
    if st is None:
        st = request.app.state.submissions = SubmissionStore(Path(request.app.state.settings.data_root) / "submissions")
    return st.board_view()


def _visible(request: Request, v: str, entries: list[dict], subs: dict[str, dict] | None = None) -> list[dict]:
    """读取时仍可显示的条目（撤回或不再满足上榜规则的提交条目不返回）。"""
    if all(e.get("baseline") for e in entries):
        return entries
    subs = _subs(request) if subs is None else subs
    listing = load_listing(_boards_dir(request) / v)
    return [e for e in entries if entry_listed(e, listing, subs)]


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
def board(request: Request, p: BoardMember) -> dict:
    root = _boards_dir(request)
    versions = _versions(root)
    cur = _current(root, versions)
    out = []
    subs = None
    for v in versions:
        b = json.loads((root / v / "board.json").read_text(encoding="utf-8"))
        if subs is None and not all(e.get("baseline") for e in b["entries"]):
            subs = _subs(request)
        out.append({"board_version": v, "current": v == cur, "score_version": SCORE_VERSION, "weights": None, "tracks": b["tracks"],
                    "lanes": b["lanes"], "seed_set_commit": b["seed_set_commit"], "platform_digest": b["platform_digest"],
                    "evaluator_major": b["evaluator_major"], "n_entries": len(_visible(request, v, b["entries"], subs))})
    return {"current": cur, "versions": out}


@router.get("/leaderboard")
def leaderboard(request: Request, p: BoardMember, track: str | None = None, lane: str | None = None, board_version: str | None = None) -> dict:
    v, b = _load(request, board_version)
    if track is not None and track not in {t["track"] for t in b["tracks"]}:
        raise ApiProblem(682, detail={"why": "track_unknown", "track": track})
    if lane is not None and lane not in {ln["lane"] for ln in b["lanes"]}:
        raise ApiProblem(682, detail={"why": "lane_unknown", "lane": lane})
    ents = [e for e in _visible(request, v, b["entries"]) if (track is None or e["track"] == track) and (lane is None or e["lane"] == lane)]
    return {**b, "tracks": [t for t in b["tracks"] if track is None or t["track"] == track],
            "lanes": [ln for ln in b["lanes"] if lane is None or ln["lane"] == lane], "entries": ents}


@router.get("/board/{track}/entries/{entry_id:path}")
def entry(track: str, entry_id: str, request: Request, p: BoardMember, board_version: str | None = None) -> dict:
    v, b = _load(request, board_version)
    rows = _visible(request, v, [e for e in b["entries"] if e["track"] == track and e["entry_id"] == entry_id])
    if not rows:
        raise ApiProblem(305, detail={"track": track, "entry_id": entry_id})
    _v, det = _load(request, v, "details.json")
    d = det.get(entry_id, {})
    return {"board_version": v, "entry_id": entry_id, "track": track, "display_name": rows[0]["display_name"], "team": rows[0]["team"],
            "baseline": rows[0]["baseline"], "lanes": [{"lane": e["lane"], "metrics": e["metrics"], "dominance_tier": e["dominance_tier"],
                                                        "events": d.get("tracks", {}).get(track, {}).get(e["lane"], [])} for e in rows]}
