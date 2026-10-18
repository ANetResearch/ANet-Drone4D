"""BN-85、BN-95、BN-96、BN-98、BN-99 内部提交（AWR-17 §18.23；M27 §6.7.9）。最低角色 `invited`；受邀成员只能访问本团队的提交
（越权 403 `115`，`why = invited_scope`），研究员及以上可查看全部。团队取会话的首个项目（无项目时为本人）。提交登记到当前榜单
版本；`visibility = board` 且已完成的提交在可见性切换时立即重算该版本榜单。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request, Response

from awr.agentsdk.schemas import bench_validator
from awr.api.problem import ApiProblem
from awr.bench.api.board import _boards_dir, _current, _versions
from awr.bench.api.deps import AnyMember
from awr.bench.auth import Principal
from awr.bench.boardstore import publish
from awr.bench.submissions import SubmissionError, SubmissionStore, public_view

router = APIRouter()
_REQ_KEYS = {"schema", "track", "lanes", "image_digest", "entry", "notes"}


def store(request: Request) -> SubmissionStore:
    st = getattr(request.app.state, "submissions", None)
    if st is None:
        st = request.app.state.submissions = SubmissionStore(Path(request.app.state.settings.data_root) / "submissions")
    return st


def team_of(p: Principal) -> str:
    return p.projects[0] if p.projects else p.name


def _scoped(doc: dict, p: Principal) -> dict:
    if not p.at_least("researcher") and doc["_meta"]["team"] != team_of(p):
        raise ApiProblem(115, detail={"why": "invited_scope"})
    return doc


def _raise(e: SubmissionError):
    raise ApiProblem(e.code, detail=e.detail or None) from None


@router.post("/submissions", status_code=202)
async def create(request: Request, p: AnyMember) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise ApiProblem(682, detail={"why": "body"}) from None
    if not isinstance(body, dict) or set(body) - _REQ_KEYS:
        raise ApiProblem(682, detail={"why": "fields"})
    errs = [e.message for e in bench_validator("submission").iter_errors({"schema": "drone4d.bench.submission.v1", **body})]
    if errs:
        raise ApiProblem(682, detail={"why": "schema", "errors": errs[:5]})
    root = _boards_dir(request)
    v = _current(root, _versions(root))
    if v is None:
        raise ApiProblem(682, detail={"why": "board_closed"})
    b = json.loads((root / v / "board.json").read_text(encoding="utf-8"))
    try:
        doc = store(request).create(body, owner=p.name, team=team_of(p), board_version=v, tracks={t["track"] for t in b["tracks"]},
                                    lanes={ln["lane"] for ln in b["lanes"]})
    except SubmissionError as e:
        _raise(e)
    return {"submission_id": doc["submission_id"], "position": doc["position"]}


@router.get("/submissions")
def listing(request: Request, p: AnyMember, track: str | None = None, state: str | None = None) -> dict:
    team = None if p.at_least("researcher") else team_of(p)
    return {"items": [public_view(d) for d in store(request).listing(team=team, track=track, state=state)], "next_cursor": None}


@router.get("/submissions/{sid}")
def detail(sid: str, request: Request, p: AnyMember) -> dict:
    try:
        return public_view(_scoped(store(request).view(sid), p))
    except SubmissionError as e:
        _raise(e)


@router.delete("/submissions/{sid}", status_code=204)
def withdraw(sid: str, request: Request, p: AnyMember) -> Response:
    st = store(request)
    try:
        _scoped(st.get(sid), p)
        st.withdraw(sid)
    except SubmissionError as e:
        _raise(e)
    return Response(status_code=204)


@router.post("/submissions/{sid}/visibility")
async def visibility(sid: str, request: Request, p: AnyMember) -> dict:
    st = store(request)
    try:
        body = await request.json()
    except ValueError:
        raise ApiProblem(682, detail={"why": "body"}) from None
    try:
        _scoped(st.get(sid), p)
        doc = st.set_visibility(sid, str((body or {}).get("visibility", "")))
    except SubmissionError as e:
        _raise(e)
    bdir = _boards_dir(request) / doc["_meta"]["board_version"]
    if doc["state"] == "DONE" and (bdir / "config.json").exists():
        publish(bdir, st.board_view())
    return public_view(st.view(sid))
