"""BN-42 至 BN-45：运行列表、运行状态、取消与手动重试（AWR-17 §18.4、§18.6；M27 §6.3、M27-FR-024、FR-039；D3-AC-51）。

运行有两类来源，列表合并返回：

- **受管运行**（`managed = true`）：经 `RunStore`（`bench.sqlite`）登记，状态取 M27 §6.3 状态机（QUEUED、PREPARING、RUNNING、
  FINALIZING、EVALUATING、COMPLETED、FAILED、CANCELLED），带 `attempt`、`queue_position`、`blocked_by[]`、原因码 `reason` 与
  可执行操作 `actions{cancel, retry}`；
- **目录运行**（`managed = false`）：采集工具与原型基准直接写入 `dataset/<event>/runs/<run_id>/` 的运行，状态取运行清单（`SUCCEEDED`、
  `EVALUATED` 等），不可取消或重试（680 `why = unmanaged`）。

取消（BN-44）：QUEUED 立即 CANCELLED；PREPARING、RUNNING 登记取消请求，调度器在 10 s 内终止进程组后进入 CANCELLED（状态载荷
`cancel.deadline_unix_ns`）；FINALIZING、EVALUATING 与终态返回 409 `680`。重试（BN-45）：只对 FAILED、CANCELLED，新 attempt 沿用
同一 `run_id` 与同一种子；上一 attempt 的产物移入 `.attempts/<n>/`；队列上限 429 `681`；支持 `Idempotency-Key`。

角色与数据范围（AWR-17 §18.3.5、§18.23.3；D3-AC-51）：列表与状态最低角色 `invited`，受邀成员只见本团队（`tenant`）的 dev 受管运行，
目录运行与他人运行对其不可见（详情 403 `115`，`why = invited_scope`）；取消与重试最低角色 `researcher`，研究者限本人提交或所属项目
（403 `115`，`why = project_scope`），评测员及以上不限。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import APIRouter, Header, Query, Request, Response

from awr.api.problem import ApiProblem
from awr.bench.api.deps import Invited, Researcher, project_ok, scope_error
from awr.bench.auth import Principal
from awr.bench.runs_store import STATES, RunStateError, RunStore, archive_attempt, env_run_item, find_run, list_runs

router = APIRouter()
_LEGACY_STATES = ("SUCCEEDED", "EVALUATED", "UNKNOWN")


def run_store(request: Request) -> RunStore:
    st = getattr(request.app.state, "runs", None)
    if st is None:
        st = request.app.state.runs = RunStore(request.app.state.settings.db_path)
    return st


def _raise(e: RunStateError):
    raise ApiProblem(e.code, detail=e.detail) from None


def _invited_visible(p: Principal, rec: dict | None) -> bool:
    """受邀成员可见的运行：本团队的 dev 受管运行（目录运行没有团队归属，不可见）。研究者及以上全部可见。"""
    if p.at_least("researcher"):
        return True
    return rec is not None and rec.get("split", "dev") == "dev" and project_ok(p, rec.get("tenant"))


def _for(p: Principal, item: dict) -> dict:
    """按请求者收窄 `actions`：无写权限（受邀成员、他人项目的研究者）时取消与重试一律为 false。"""
    if item.get("managed") and not (p.at_least("researcher") and project_ok(p, item.get("tenant"), item.get("submitted_by"))):
        item["actions"] = {"cancel": False, "retry": False}
    return item


def _legacy(item: dict) -> dict:
    """目录运行的列表项补齐统一字段（不可取消、不可重试，无原因码）。"""
    return {**item, "managed": False, "terminal": True, "attempt": None, "queue_position": None, "blocked_by": [], "reason": None,
            "cancel": None, "actions": {"cancel": False, "retry": False}}


def _csv(v: str | None) -> list[str] | None:
    return None if v is None else [x for x in (s.strip() for s in v.split(",")) if x]


def _cursor(c: str | None) -> int:
    if not c:
        return 0
    try:
        o = int(json.loads(base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)))["o"])
    except (ValueError, KeyError, TypeError):
        raise ApiProblem(300, status=400, detail={"cursor": "invalid"}) from None
    return max(o, 0)


def _next(o: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": o}).encode()).rstrip(b"=").decode()


@router.get("/runs")
def runs(request: Request, p: Invited, event: str | None = None, experiment: str | None = None, policy: str | None = None,
         state: str | None = None, ranked: bool | None = None, ids: str | None = None, cursor: str | None = None,
         limit: int = Query(100, ge=1, le=500)) -> dict:
    """BN-42：过滤 `event`、`experiment`、`policy`、`state`（逗号分隔）、`ranked`、`ids`（逗号分隔）；按（事件，run_id）排序，游标分页。"""
    states = _csv(state)
    if states is not None and (bad := [s for s in states if s not in STATES and s not in _LEGACY_STATES]):
        raise ApiProblem(300, status=400, detail={"state": bad, "allowed": [*STATES, *_LEGACY_STATES]})
    id_list = _csv(ids)
    managed = run_store(request).listing(ids=id_list, event=event, experiment=experiment, states=states, ranked=ranked)
    known = run_store(request).ids()
    legacy = []
    if experiment is None:
        for it in list_runs(request.app.state.settings.dataset_dir):
            if it["run_id"] in known:
                continue
            if (event is not None and it["event_id"] != event) or (id_list is not None and it["run_id"] not in id_list) \
                    or (states is not None and it["state"] not in states) or (ranked is not None and bool(it.get("ranked")) != ranked):
                continue
            legacy.append(_legacy(it))
    d = request.app.state.settings.dataset_dir
    for r in managed:
        rd = Path(d) / r["event_id"] / "runs" / r["run_id"]
        r["has_frames"] = (rd / "frames.idx").exists()
    items = sorted([*managed, *legacy], key=lambda r: (r["event_id"], r["run_id"]))
    if policy is not None:
        items = [r for r in items if r.get("policy") == policy]
    if not p.at_least("researcher"):
        items = [r for r in items if r.get("managed") and _invited_visible(p, r)]
    o = _cursor(cursor)
    page = [_for(p, r) for r in items[o:o + limit]]
    return {"items": page, "next_cursor": _next(o + limit) if o + limit < len(items) else None, "total": len(items)}


@router.get("/runs/{rid}")
def run(rid: str, request: Request, p: Invited) -> dict:
    """BN-43：运行状态。受管运行另带 `attempts[]` 与迁移审计 `transitions[]`；运行目录存在时并入帧通道与事件版本。"""
    st = run_store(request)
    rec = st.get(rid)
    d = find_run(request.app.state.settings.dataset_dir, rid)
    if rec is None and d is None:
        raise ApiProblem(305, detail={"run_id": rid})
    if not _invited_visible(p, rec):
        raise scope_error("invited_scope", run_id=rid)
    if rec is not None:
        out = {**_for(p, rec), "attempts": st.attempts(rid), "transitions": st.transitions(rid), "fleet": [], "has_frames": False}
    else:
        out = _legacy({"run_id": rid, "event_id": d.parent.parent.name, "state": "UNKNOWN", "kind": None, "world_id": None, "fleet": []})
    if d is None:
        return out
    m = d / "run_manifest.json"
    doc = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
    if rec is None:
        out.update({"state": doc.get("state", "UNKNOWN"), "kind": doc.get("kind"), "world_id": doc.get("world_id"), "fleet": doc.get("fleet", [])})
        env = d / "manifest.json"
        if "state" not in doc and env.exists():     # 与 list_runs 同一判定（run_manifest.v1 不含 state）
            out.update(env_run_item(d, json.loads(env.read_text(encoding="utf-8"))))
    else:
        out["fleet"] = doc.get("fleet", [])
        ev = d / "evaluation.json"
        if ev.exists():
            e = json.loads(ev.read_text(encoding="utf-8"))
            out["evaluation"] = {"components": e.get("components"), "ranked_valid": e.get("ranked_valid"), "invalid_reasons": e.get("invalid_reasons")}
    ev = doc.get("event")
    if ev:
        out["event"] = {"event_id": ev["event_id"], "version": ev["version"], "offset_s": ev.get("offset_s", 0.0)}
    fs = request.app.state.frame_stores.get(d, rid)
    out["has_frames"] = fs is not None
    if fs is not None:
        out["frames"] = [{"uav_id": u, "sensor_id": s, "latest_seq": fs.latest_seq(u, s)} for u, s in fs.idx.channels()]
    return out


def _managed(request: Request, rid: str, p: Principal) -> RunStore:
    st = run_store(request)
    rec = st.get(rid)
    if rec is None:
        if find_run(request.app.state.settings.dataset_dir, rid) is not None:
            raise ApiProblem(680, detail={"why": "unmanaged", "run_id": rid})
        raise ApiProblem(305, detail={"run_id": rid})
    if not project_ok(p, rec.get("tenant"), rec.get("submitted_by")):
        raise scope_error("project_scope", run_id=rid, project=rec.get("tenant"))
    return st


@router.delete("/runs/{rid}", status_code=204)
def cancel(rid: str, request: Request, p: Researcher) -> Response:
    """BN-44：取消。重复取消一个已登记取消请求的运行仍返回 204。"""
    st = _managed(request, rid, p)
    try:
        st.request_cancel(rid, actor=p.name)
    except RunStateError as e:
        _raise(e)
    return Response(status_code=204)


@router.post("/runs/{rid}/retry", status_code=202)
def retry(rid: str, request: Request, p: Researcher, idempotency_key: str | None = Header(None, max_length=128)) -> dict:
    """BN-45：手动重试（新 attempt）。→ `{run_id, attempt}`。"""
    st = _managed(request, rid, p)
    try:
        out = st.retry(rid, actor=p.name, idempotency_key=idempotency_key)
    except RunStateError as e:
        _raise(e)
    if not out["replayed"]:                                      # 首次执行（非幂等重放）时归档上一 attempt 的产物
        rec = st.get(rid)
        archive_attempt(Path(request.app.state.settings.dataset_dir) / rec["event_id"] / "runs" / rid, out["attempt"] - 1)
    return {"run_id": out["run_id"], "attempt": out["attempt"]}
