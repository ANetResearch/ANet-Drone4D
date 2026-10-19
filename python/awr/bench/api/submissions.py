"""BN-82、BN-85、BN-95、BN-96、BN-98、BN-99 内部提交与推送令牌（AWR-17 §18.23；M27 §6.7.9；D3-AC-59）。最低角色 `invited`；受邀成员
只能访问本团队的提交（越权 403 `115`，`why = invited_scope`，由 require 依赖写 `deny` 审计），研究员及以上可查看全部，但结果缺省只对
提交者团队可见（§5.6.7"结果可见性"）：`visibility = private` 的提交对团队以外的研究员不返回 `result`（评测员及以上不受限）。团队取会话的
首个项目（无项目时为本人）。登记时按 `awr.bench.submissions.Quota` 判定配额（681 `submit_quota`、`gpu_hours`）：当前榜单为开发榜
（`track.dev`）时计为冒烟验证，否则为正式提交；BN-85 的响应带请求者的配额用量 `quota`。BN-82 签发 `d3-registry` 团队命名空间的推送令牌
（10 min，`awr.bench.auth.BenchAuth.sign_push`），任一项可用配额都已用尽时返回 681。提交登记到当前榜单
版本；`visibility = board` 且已完成的提交在可见性切换时立即重算该版本榜单。当前版本切换后，旧榜单版本只读（D3-AC-57）：登记在旧版本上的
提交不可再进入榜单（680 `board_frozen`），撤回（改回 `private`）仍允许并由榜单读取侧立即隐藏（D3-AC-43），旧版本的 board.json 与
details.json 不再被重算。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request, Response

from awr.agentsdk.schemas import bench_validator
from awr.api.problem import ApiProblem
from awr.bench.api.board import _boards_dir, _current, _versions
from awr.bench.api.deps import AnyMember
from awr.bench.auth import AuthError, Principal
from awr.bench.boardstore import load_config, publish
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


def _own(doc: dict, p: Principal) -> bool:
    """提交属于请求者的团队（受邀成员：团队；研究者：本人或所属项目）。"""
    m = doc["_meta"]
    return m["team"] == team_of(p) or m["owner"] == p.name or (p.at_least("researcher") and m["team"] in p.projects)


def _scoped(doc: dict, p: Principal) -> dict:
    if not p.at_least("researcher") and doc["_meta"]["team"] != team_of(p):
        raise ApiProblem(115, detail={"why": "invited_scope"})
    return doc


def _out(doc: dict, p: Principal) -> dict:
    """接口视图：去掉服务端字段；未上榜（`private`）的提交对团队以外的研究员不返回结果。"""
    v = public_view(doc)
    if doc["visibility"] != "board" and not p.at_least("evaluator") and not _own(doc, p):
        v.pop("result", None)
    return v


def _quota(request: Request, p: Principal) -> dict:
    return store(request).quota_status(owner=p.name, team=team_of(p), role=p.role)


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
    dev = (root / v / "config.json").is_file() and bool((load_config(root / v).get("track") or {}).get("dev"))
    try:
        doc = store(request).create(body, owner=p.name, team=team_of(p), board_version=v, tracks={t["track"] for t in b["tracks"]},
                                    lanes={ln["lane"] for ln in b["lanes"]}, role=p.role, kind="smoke" if dev else "formal")
    except SubmissionError as e:
        _raise(e)
    return {"submission_id": doc["submission_id"], "position": doc["position"]}


@router.get("/submissions")
def listing(request: Request, p: AnyMember, track: str | None = None, state: str | None = None) -> dict:
    team = None if p.at_least("researcher") else team_of(p)
    return {"items": [_out(d, p) for d in store(request).listing(team=team, track=track, state=state)], "next_cursor": None,
            "quota": _quota(request, p)}


@router.get("/submissions/{sid}")
def detail(sid: str, request: Request, p: AnyMember) -> dict:
    try:
        return _out(_scoped(store(request).view(sid), p), p)
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
    root = _boards_dir(request)
    want = str((body or {}).get("visibility", ""))
    try:
        v = _scoped(st.get(sid), p)["_meta"]["board_version"]
        vs = _versions(root)
        frozen = v in vs and v != _current(root, vs)
        if frozen and want != "private":                     # 撤回（private）始终允许：旧版本不重算，由榜单读取侧隐藏（D3-AC-43）
            raise SubmissionError(680, "board_frozen", board_version=v)
        doc = st.set_visibility(sid, want)
    except SubmissionError as e:
        _raise(e)
    bdir = root / doc["_meta"]["board_version"]
    if doc["state"] == "DONE" and not frozen and (bdir / "config.json").exists():
        publish(bdir, st.board_view())
    return _out(st.view(sid), p)


@router.post("/registry/token")
def registry_token(request: Request, p: AnyMember) -> dict:
    """BN-82：`d3-registry` 推送令牌（团队命名空间，10 min）。`docker login <registry> -u <principal> --password-stdin` 以令牌为密码。"""
    q = _quota(request, p)
    errs = [SubmissionStore.quota_error(q, k, queued=False) for k in ("formal", "smoke")]      # 排队已满不妨碍推送下一个镜像
    if all(errs):
        _raise(next((e for e in errs if e.detail["why"] == "gpu_hours"), errs[0]))
    try:
        tok, exp, ns = request.app.state.auth.sign_push(p)
    except AuthError as e:
        raise ApiProblem(e.code, detail={"why": e.detail} if e.code == 115 else e.detail or None) from None
    return {"token": tok, "username": p.name, "namespace": ns, "repository_prefix": f"{ns}/", "exp_unix_ns": str(exp * 1_000_000_000)}
