"""test 划分的接口侧守卫与 BN-27 事件制品（AWR-17 §18.3.5"test 划分规则"、§18.4；M23 §7.3.3、M23-FR-063；M27-FR-114、FR-120、
FR-124；D3-AC-43）。

- 划分判定：事件取 `splits.json` 登记与事件自身声明中最严格者（`event_split`）；Run 另并入 RunStore 记录与运行清单的声明
  （`run_split_of`）。登记表与 HiddenStore 在首次请求时挂到 `app.state.splits`、`app.state.hidden`，审计与 bench-api 访问审计
  共用 `app.state.auth.audit`。
- 越权 691：研究者（及以下）读取 test 事件的编译详情（BN-22）、受限制品（BN-27，`brief.json` 除外）或 test Run 的观测帧
  （BN-49 至 BN-51，"test 结果只含指标"）一律 403 `691`（`detail.why = test_split`），由 `require` 依赖在退出时写 `deny` 审计。
- test 真值读取：评测员及以上读取 test 事件磁带与真值（BN-27、BN-52.tape）或 test Run 真值帧（BN-52）时经 `HiddenStore`
  解析到 `<data_root>/hidden/` 并写 `truth_read` 审计（外部主体计数见 `awr.bench.split.truth_access_summary`）。

BN-27 `GET /events/{eid}/versions/{v}/artifacts/{name}`：最低角色 researcher；`name` 限 M23 §7.3.3 白名单（其余 305）；dev 事件
读事件编译目录 `<data_root>/events/<eid>/v<N>/`；test 事件的 HIDDEN_ONLY 制品（K07 起含 `plan.json`、`anchors.json`、
`compile_report.json` 与事件源）读 `hidden/<eid>/event/v<N>/`（真值文件顶层没有时取最新 `truth/r<k>/`；未迁移时回落到编译目录，
审计路径带 `outside:`），其余受限制品读编译目录，均经 HiddenStore 授权与审计。响应带 `ETag = sha256`，支持 `If-None-Match`。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import APIRouter, Request, Response

from awr.api.problem import ApiProblem
from awr.bench import split as sp
from awr.bench.api.deps import Researcher
from awr.bench.api.runs import run_store
from awr.bench.auth import Principal
from awr.bench.runs_store import truth_paths
from awr.bench.split import HiddenStore, SplitAccessDenied, SplitRegistry

__all__ = ["declared_split", "denied", "event_split", "event_tape_dir", "guard_event", "guard_run", "hidden", "registry", "router", "run_split_of",
           "run_truth_paths"]

router = APIRouter()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MEDIA = {".json": "application/json", ".yaml": "application/yaml", ".bin": "application/x-d4dtape", ".mcap": "application/x-mcap",
          ".npz": "application/octet-stream"}


def registry(request: Request) -> SplitRegistry:
    reg = getattr(request.app.state, "splits", None)
    if reg is None:
        reg = request.app.state.splits = SplitRegistry.for_root(request.app.state.settings.data_root, audit=request.app.state.auth.audit)
    return reg


def hidden(request: Request) -> HiddenStore:
    hs = getattr(request.app.state, "hidden", None)
    if hs is None:
        hs = request.app.state.hidden = HiddenStore.for_root(request.app.state.settings.data_root, request.app.state.auth.audit)
    return hs


def denied(e: SplitAccessDenied) -> ApiProblem:
    return ApiProblem(691, detail=e.detail)


def _internal(p: Principal) -> bool:
    return p.role in sp.INTERNAL_ROLES


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


# ── 事件 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def declared_split(vdir: Path) -> str | None:
    """事件版本目录自身声明的划分：`event.json` 或 `plan.json` 的 `split`（与 BN-22 载荷同一口径）；不可读时按 test。"""
    for n in ("event.json", "plan.json"):
        f = Path(vdir) / n
        if f.is_file():
            try:
                s = json.loads(f.read_text(encoding="utf-8")).get("split")
            except (OSError, ValueError, AttributeError):
                return "test"
            if s:
                return str(s)
    return None


def event_split(request: Request, eid: str, declared: str | None = None) -> str:
    return registry(request).split_of(eid, declared)


def guard_event(request: Request, p: Principal, eid: str, split: str, **detail) -> None:
    """test 事件的编译详情与受限制品只对评测员及以上：其余角色 403 `691`。"""
    if split != "dev" and not _internal(p):
        raise ApiProblem(691, detail={"why": "test_split", "event_id": eid, "split": split, **detail})


def _authorized_read(request: Request, p: Principal, path: Path, *, eid: str, run_id: str | None = None) -> bytes:
    try:
        return hidden(request).read_bytes(p, path, event_id=eid, run_id=run_id, request_id=_rid(request))
    except SplitAccessDenied as e:
        raise denied(e) from None


@router.get("/events/{eid}/versions/{v}/artifacts/{name}")
def event_artifact(eid: str, v: int, name: str, request: Request, p: Researcher) -> Response:
    """BN-27：事件制品下载（M23 §7.3.3 授权；test 事件越权 691）。"""
    root = Path(request.app.state.settings.data_root) / "events"
    vdir = root / eid / f"v{v}"
    if not _ID.match(eid) or v < 1 or name not in sp.EVENT_ARTIFACTS:
        raise ApiProblem(305, detail={"event_id": eid, "version": v, "name": name})
    hs = hidden(request)
    if not vdir.is_dir() and not hs.event_dir(eid, v).is_dir():
        raise ApiProblem(305, detail={"event_id": eid, "version": v})
    split = event_split(request, eid, declared_split(vdir))
    if not sp.artifact_allowed(split, name, p.role):
        raise ApiProblem(691, detail={"why": "test_split", "event_id": eid, "version": v, "name": name, "split": split})
    if split == "dev" or name in sp.PUBLIC_ARTIFACTS:
        f = vdir / name
        if not f.is_file():
            raise ApiProblem(305, detail={"event_id": eid, "version": v, "name": name})
        body = f.read_bytes()
    else:
        f = hs.event_file(eid, v, name) if name in sp.HIDDEN_ONLY else vdir / name   # 真值文件可在 truth/r<k>/ 下（K07）
        if name in sp.HIDDEN_ONLY and not f.is_file() and (vdir / name).is_file():
            f = vdir / name                                   # 未迁移到 hidden 的旧编译目录（misplaced 会列出）
        if not f.is_file():
            raise ApiProblem(305, detail={"event_id": eid, "version": v, "name": name})
        body = _authorized_read(request, p, f, eid=eid)
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    hdr = {"ETag": etag, "Cache-Control": "private, no-cache" if split == "dev" else "private, no-store"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=hdr)
    return Response(body, media_type=_MEDIA.get(Path(name).suffix, "application/octet-stream"), headers=hdr)


# ── 运行 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _run_record(request: Request, rid: str) -> dict | None:
    return run_store(request).get(rid)


def run_split_of(request: Request, rid: str, run_dir: Path | None) -> tuple[str, str | None]:
    """→ (Run 的有效划分, 事件 id)：RunStore 记录、运行清单与所属事件划分中最严格者。"""
    rec = _run_record(request, rid)
    eid = (rec or {}).get("event_id") or (Path(run_dir).parent.parent.name if run_dir is not None else None)
    decl = [(rec or {}).get("split"), *sp.declared_run_splits(run_dir)]
    return sp.run_split(registry(request), event_id=eid, declared=decl), eid


def guard_run(request: Request, p: Principal, rid: str, run_dir: Path | None) -> str:
    """test Run 的观测与录制只对评测员及以上（M27-FR-120"test 结果只含指标"）：其余角色 403 `691`。→ 有效划分。"""
    split, eid = run_split_of(request, rid, run_dir)
    if split != "dev" and not _internal(p):
        raise ApiProblem(691, detail={"why": "test_split", "run_id": rid, "event_id": eid, "split": split})
    return split


def run_truth_paths(request: Request, p: Principal, rid: str, run_dir: Path) -> tuple[Path, Path]:
    """Run 真值文件与索引：dev 为 `<event>/truth/run_<id>.*`（`runs_store.truth_paths`）；test 为 `hidden/<event>/truth/run_<id>.*`，
    经 HiddenStore 授权并写 `truth_read` 审计（非评测员 691）。"""
    split, eid = run_split_of(request, rid, run_dir)
    if split == "dev":
        return truth_paths(run_dir)
    mc, idx = hidden(request).run_truth_paths(str(eid), rid)
    try:
        hidden(request).authorize(p, mc, event_id=str(eid), run_id=rid, request_id=_rid(request))
    except SplitAccessDenied as e:
        raise denied(e) from None
    return mc, idx


def event_tape_dir(request: Request, p: Principal, rid: str, run_dir: Path, ev: dict) -> Path:
    """Run 所用事件版本的磁带目录：dev 为事件编译目录；test 为 `hidden/<eid>/event/v<N>/`（未迁移时回落到编译目录），经 HiddenStore
    授权并写 `truth_read` 审计。"""
    eid, v = str(ev["event_id"]), int(ev["version"])
    edir = Path(request.app.state.settings.data_root) / "events" / eid / f"v{v}"
    split, _ = run_split_of(request, rid, run_dir)
    if split == "dev" and event_split(request, eid) == "dev":
        return edir
    hs = hidden(request)
    tdir = hs.event_dir(eid, v) if (hs.event_dir(eid, v) / "tape.bin").is_file() else edir
    try:
        hs.authorize(p, tdir / "tape.bin", event_id=eid, run_id=rid, request_id=_rid(request))
    except SplitAccessDenied as e:
        raise denied(e) from None
    return tdir
