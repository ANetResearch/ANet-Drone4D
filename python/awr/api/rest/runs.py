"""运行与录制 REST（M12 §7.3；AWR-17 §4.2 R33–R37、R66–R72、§4.3.9；M12-FR-052、NFR-018）。所有者 M12。

- R33 `GET /api/runs`：运行与段列表（`segments[].speed_max`、`events_per_sim_s`、`sidecars{ovw, evx}`、`epochs[]`、`compatible`）；
- R34 `GET /api/runs/{run}`：`meta.json`；
- R35、R66、R67 `GET /api/runs/{run}/segments/{seg}.{mcap|ovw|evx}`：单区间 Range；已关闭段 `private, max-age=31536000,
  immutable`，写入中的段 `no-store`；
- R36 `POST /api/runs/{run}/keep`（operator 席）、R37 `DELETE /api/runs/{run}`（admin；当前运行或回放中 409 `105`）；
- R68 `GET /api/runs/{run}/events`：该运行处于回放打开状态时经 replay-worker `ctl/replay-worker/query{events}` 分页，
  否则 409 `463 REPLAY_NOT_OPEN`；
- R69–R72 书签 `runs/<run>/bookmarks.json`（awr.run.bookmarks.v1，契约 `rec/markers.schema.json` 的 bookmark 定义）：
  列表（viewer）、新建 / 改标签 / 删除（operator、admin）；每运行 ≤ 1000 条（409 `462`）；标签服务端净化（去 emoji 与
  禁用字形、折叠空白、≤ 64 字符）；以"写临时文件后原子改名"更新并审计 `runs.bookmark`。
`run` 须匹配 `^r\\d{8}-\\d{6}-[0-9a-f]{4}$`，`seg` 须匹配 `^\\d{1,3}$`，拼接路径后再做 `resolve()` 前缀检查（防路径穿越）。
事件循环内只做文件服务、JSON 小对象读写与纯函数；MCAP 解压与解析一律在 replay-worker（AWR-03 §4.2 第 1 条）。
本模块不 import `awr.recorder`（api 的 import 边界，tools/lint/check_py_imports.py），书签文件读写在此实现。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from awr.contracts import CONTRACTS_VERSION, LAYOUT_ID, bus_keys
from awr.runtime.bus import BusError, BusTimeout

from ..deps import Admin, Operator, Viewer, app_ctx
from ..problem import ApiProblem
from ..static import RangeFileResponse, parse_range

router = APIRouter(tags=["runs"])

RUN_RE = re.compile(r"^r\d{8}-\d{6}-[0-9a-f]{4}$")
SEG_RE = re.compile(r"^\d{1,3}$")
EXT_MEDIA = {"mcap": "application/octet-stream", "ovw": "application/octet-stream", "evx": "application/octet-stream"}
BOOKMARKS_MAX = 1000
LABEL_MAX = 64
IMMUTABLE = "private, max-age=31536000, immutable"
# 服务端净化（与前端 lib/sanitize.ts 同一禁用集合：emoji 区段、U+25A0–25FF、U+2600–27BF、U+2194–21FF、变体选择符与连接符）
_STRIP_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x25A0, 0x25FF), (0x2194, 0x21FF), (0x2B00, 0x2BFF), (0x2300, 0x23FF),
                 (0xFE0F, 0xFE0F), (0x200D, 0x200D), (0x20E3, 0x20E3), (0xE0020, 0xE007F))
_STRIP = re.compile("[" + "".join(re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b != a else "") for a, b in _STRIP_RANGES) + "]")
_lock = threading.Lock()


def _runs_root(request: Request) -> Path:
    s = app_ctx(request).settings
    env = os.environ.get("AWR_RUNS_DIR")
    if env:
        return Path(env)
    if s.persist_dir is not None:
        return Path(s.persist_dir).parent
    return Path(__file__).resolve().parents[4] / "runs"


def _run_dir(request: Request, run: str) -> Path:
    if not RUN_RE.match(run):
        raise ApiProblem(110, status=422, detail={"param": "run"})
    root = _runs_root(request).resolve()
    d = (root / run).resolve()
    if d.parent != root:
        raise ApiProblem(305, status=404, detail={"run": run})
    if not d.is_dir():
        raise ApiProblem(305, status=404, detail={"run": run})
    return d


def _seg_no(seg: str) -> int:
    if not SEG_RE.match(seg):
        raise ApiProblem(110, status=422, detail={"param": "seg"})
    return int(seg)


def _read_json(p: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _atomic_json(p: Path, obj: Any) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def sanitize_label(s: str, max_len: int = LABEL_MAX) -> str:
    out = " ".join(_STRIP.sub("", s or "").split())
    return out if len(out) <= max_len else out[: max_len - 1] + "—"


def _replaying(request: Request, run: str) -> bool:
    gw = app_ctx(request).gateway
    pb = getattr(gw, "playback", None) if gw is not None else None
    return bool(gw is not None and getattr(gw, "mode", "live") == "replay" and pb is not None and getattr(pb, "run", "") == run)


def _compatible(request: Request, meta: dict[str, Any]) -> bool:
    ctx = app_ctx(request)
    b = meta.get("binding") or {}
    wi = ctx.world_info or {}
    if b.get("world_id") != ctx.settings.world_id or int(b.get("layout_id", -1)) != LAYOUT_ID:
        return False
    cv = wi.get("contentVersion")
    if cv and b.get("content_version") and b.get("content_version") != cv:
        return False
    return str(b.get("contracts_version", CONTRACTS_VERSION)).split(".")[0] == CONTRACTS_VERSION.split(".")[0]


def _segment_items(meta: dict[str, Any], d: Path) -> list[dict[str, Any]]:
    side = meta.get("sidecars") or {}
    out = []
    for s in meta.get("segments", []):
        k = int(s.get("segment", 0))
        extra = side.get(f"{k:03d}") if isinstance(side, dict) else None
        extra = extra if isinstance(extra, dict) else {}
        lin = s.get("lineage") or []
        epochs = []
        for i, e in enumerate(lin):
            epochs.append({"epoch": int(e.get("epoch", 0)), "t_from_ns": int(e.get("t_start_ns", 0)),
                           "t_to_ns": int(e["t_end_ns"]) if e.get("t_end_ns") is not None else (s.get("t_end_ns")),
                           "valid": i == len(lin) - 1 or not e.get("invalid")})
        out.append({"seg": k, "file": s.get("file"), "state": s.get("state"), "closed": s.get("state") == "CLOSED",
                    "t0_ns": s.get("t_start_ns"), "t1_ns": s.get("t_end_ns"), "bytes": s.get("bytes", 0),
                    "bytes_per_sim_s": s.get("bytes_per_sim_s", 0.0), "events_per_sim_s": extra.get("events_per_sim_s", 0.0),
                    "speed_max": extra.get("speed_max", 20.0), "decimation": s.get("decimation"),
                    "sidecars": {"ovw": (d / f"rec-{k:03d}.ovw").is_file(), "evx": (d / f"rec-{k:03d}.evx").is_file()},
                    "epochs": epochs})
    return out


def _run_item(request: Request, d: Path, meta: dict[str, Any]) -> dict[str, Any]:
    b = meta.get("binding") or {}
    segs = _segment_items(meta, d)
    return {"run_id": meta.get("run_id", d.name), "world_id": b.get("world_id"), "created_unix_ns": str(meta.get("created_wall_ns", "0")),
            "keep": bool(meta.get("keep", False)), "bytes": sum(int(s["bytes"] or 0) for s in segs), "segments": segs,
            "binding": {"content_version": b.get("content_version"), "coordinate_sha256": b.get("coordinate_sha256"),
                        "layout_id": b.get("layout_id"), "contracts": b.get("contracts_version")},
            "scenario": (meta.get("scenario") or {}).get("scenario_id") if isinstance(meta.get("scenario"), dict) else None,
            "compatible": _compatible(request, meta), "current": d.name == app_ctx(request).settings.run_id}


# ------------------------------------------------------------------ R33、R34
@router.get("/api/runs")
async def list_runs(request: Request, _p: Viewer, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                    cursor: str | None = None) -> dict[str, Any]:
    root = _runs_root(request)

    def scan() -> list[tuple[Path, dict[str, Any]]]:
        out = []
        if root.is_dir():
            for d in sorted((x for x in root.iterdir() if x.is_dir() and RUN_RE.match(x.name)), key=lambda x: x.name, reverse=True):
                m = _read_json(d / "meta.json")
                if m is not None:
                    out.append((d, m))
        return out

    runs = await anyio.to_thread.run_sync(scan)
    if cursor:
        runs = [x for x in runs if x[0].name < cursor]
    page = runs[:limit]
    items = [_run_item(request, d, m) for d, m in page]
    return {"items": items, "next_cursor": page[-1][0].name if len(runs) > limit else None}


@router.get("/api/runs/{run}")
async def get_run(request: Request, run: str, _p: Viewer) -> dict[str, Any]:
    d = _run_dir(request, run)
    m = await anyio.to_thread.run_sync(_read_json, d / "meta.json")
    if m is None:
        raise ApiProblem(305, status=404, detail={"run": run, "file": "meta.json"})
    return m


# ------------------------------------------------------------------ R35、R66、R67
@router.api_route("/api/runs/{run}/segments/{name}", methods=["GET", "HEAD"])
async def segment_file(request: Request, run: str, name: str, _p: Viewer) -> Response:
    seg, dot, ext = name.partition(".")
    if not dot or ext not in EXT_MEDIA:
        raise ApiProblem(305, status=404, detail={"file": name})
    k = _seg_no(seg)
    d = _run_dir(request, run)
    p = (d / f"rec-{k:03d}.{ext}").resolve()
    if p.parent != d or not p.is_file():
        raise ApiProblem(305, status=404, detail={"run": run, "segment": k, "ext": ext})
    meta = _read_json(d / "meta.json") or {}
    closed = any(int(s.get("segment", -1)) == k and s.get("state") == "CLOSED" for s in meta.get("segments", []))
    st = p.stat()
    headers = {"cache-control": IMMUTABLE if closed else "no-store"}
    if closed:
        headers["etag"] = f'"{st.st_size:x}-{int(st.st_mtime_ns):x}"'
    rng = parse_range(request.headers.get("range"), st.st_size)
    head = request.method == "HEAD"
    if rng == "416":
        raise ApiProblem(309, status=416, detail={"size": st.st_size},
                         headers={"Content-Range": f"bytes */{st.st_size}", "Accept-Ranges": "bytes"})
    if rng is None:
        return RangeFileResponse(p, start=0, end=st.st_size, size=st.st_size, status=200, headers=headers,
                                 media_type=EXT_MEDIA[ext], head=head)
    a, b = rng
    return RangeFileResponse(p, start=a, end=b, size=st.st_size, status=206, headers=headers, media_type=EXT_MEDIA[ext], head=head)


# ------------------------------------------------------------------ R36、R37
class KeepBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep: bool


@router.post("/api/runs/{run}/keep")
async def keep_run(request: Request, run: str, body: KeepBody, p: Operator) -> dict[str, Any]:
    ctx = app_ctx(request)
    gw = ctx.gateway
    if gw is not None and not gw.is_seat_holder(p.id):
        raise ApiProblem(116, status=409)
    d = _run_dir(request, run)

    def apply() -> None:
        with _lock:
            m = _read_json(d / "meta.json")
            if m is None:
                raise ApiProblem(305, status=404, detail={"run": run, "file": "meta.json"})
            m["keep"] = bool(body.keep)
            _atomic_json(d / "meta.json", m)

    await anyio.to_thread.run_sync(apply)
    ctx.audit.write("runs.kept" if body.keep else "runs.unkept", principal_id=p.id, role=p.role, detail={"run": run})
    return {"run_id": run, "keep": bool(body.keep)}


@router.delete("/api/runs/{run}", status_code=204)
async def delete_run(request: Request, run: str, p: Admin) -> Response:
    ctx = app_ctx(request)
    d = _run_dir(request, run)
    if run == ctx.settings.run_id or _replaying(request, run):
        raise ApiProblem(105, status=409, detail={"why": "RUN_ACTIVE", "run": run})
    await anyio.to_thread.run_sync(shutil.rmtree, d)
    ctx.audit.write("runs.deleted", principal_id=p.id, role=p.role, detail={"run": run})
    return Response(status_code=204)


# ------------------------------------------------------------------ R68
@router.get("/api/runs/{run}/events")
async def run_events(request: Request, run: str, _p: Viewer, seg: Annotated[str | None, Query()] = None,
                     from_ns: int | None = None, to_ns: int | None = None,
                     level_min: Annotated[int, Query(ge=0, le=3)] = 0, limit: Annotated[int, Query(ge=1, le=1000)] = 200,
                     cursor: int | None = None) -> dict[str, Any]:
    _run_dir(request, run)
    if seg is not None:
        _seg_no(seg)
    ctx = app_ctx(request)
    gw = ctx.gateway
    if not _replaying(request, run) or (seg is not None and int(getattr(gw.playback, "segment", -1)) != int(seg)):
        raise ApiProblem(463, status=409, detail={"run": run})
    q: dict[str, Any] = {"kind": "events", "level_min": level_min, "limit": limit}
    if from_ns is not None:
        q["from_ns"] = from_ns
    if to_ns is not None:
        q["to_ns"] = to_ns
    if cursor is not None:
        q["cursor"] = cursor
    try:
        rep = await ctx.bus.call(bus_keys.ctl_replay_worker("query"), {"v": 1, "cid": "rq-" + secrets.token_hex(6), "query": q},
                                 timeout=2.0, retries=1, retry_gap=0.3)
    except (BusTimeout, BusError) as e:
        raise ApiProblem(213, status=503) from e
    if not isinstance(rep, dict) or int(rep.get("code") or 0):
        code = int((rep or {}).get("code") or 213) if isinstance(rep, dict) else 213
        raise ApiProblem(code)
    items = [{"mseq": e.get("mseq"), "seq": e.get("seq"), "t_sim_ns": e.get("t_sim_ns"), "t_wall_ns": str(e.get("t_wall_ns", "0")),
              "type": e.get("kind"), "level": e.get("severity", 0), "producer": e.get("producer"), "uav": e.get("uav"),
              "cid": e.get("cid"), "data": e.get("data") or {}} for e in rep.get("items", []) if isinstance(e, dict)]
    return {"items": items, "next_cursor": rep.get("next_cursor")}


# ------------------------------------------------------------------ R69–R72 书签
def _bm_path(d: Path) -> Path:
    return d / "bookmarks.json"


def _bm_load(d: Path) -> dict[str, Any]:
    m = _read_json(_bm_path(d))
    if m is None or not isinstance(m.get("items"), list):
        return {"schema": "awr.run.bookmarks.v1", "items": []}
    return {"schema": "awr.run.bookmarks.v1", "items": [x for x in m["items"] if isinstance(x, dict)]}


class BookmarkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment: int = Field(ge=0, le=999)
    t_sim_ns: int = Field(ge=0)
    label: str = Field(default="", max_length=256)


class LabelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=256)


@router.get("/api/runs/{run}/bookmarks")
async def list_bookmarks(request: Request, run: str, _p: Viewer) -> dict[str, Any]:
    d = _run_dir(request, run)
    return await anyio.to_thread.run_sync(_bm_load, d)


@router.post("/api/runs/{run}/bookmarks", status_code=201)
async def add_bookmark(request: Request, run: str, body: BookmarkBody, p: Operator) -> dict[str, Any]:
    ctx = app_ctx(request)
    d = _run_dir(request, run)
    label = sanitize_label(body.label)
    bid = "bm-" + secrets.token_hex(4)

    def apply() -> None:
        with _lock:
            m = _bm_load(d)
            if len(m["items"]) >= BOOKMARKS_MAX:
                raise ApiProblem(462, status=409, detail={"max": BOOKMARKS_MAX})
            m["items"].append({"id": bid, "segment": body.segment, "t_sim_ns": body.t_sim_ns, "label": label,
                               "created_wall_ns": str(time.time_ns()), "principal_id": p.id})
            _atomic_json(_bm_path(d), m)

    await anyio.to_thread.run_sync(apply)
    ctx.audit.write("runs.bookmark", principal_id=p.id, role=p.role, detail={"run": run, "op": "add", "id": bid})
    return {"id": bid, "label": label}


@router.patch("/api/runs/{run}/bookmarks/{bid}")
async def edit_bookmark(request: Request, run: str, bid: str, body: LabelBody, p: Operator) -> dict[str, Any]:
    ctx = app_ctx(request)
    d = _run_dir(request, run)
    label = sanitize_label(body.label)

    def apply() -> dict[str, Any]:
        with _lock:
            m = _bm_load(d)
            for it in m["items"]:
                if it.get("id") == bid:
                    it["label"] = label
                    _atomic_json(_bm_path(d), m)
                    return it
        raise ApiProblem(305, status=404, detail={"bookmark": bid})

    it = await anyio.to_thread.run_sync(apply)
    ctx.audit.write("runs.bookmark", principal_id=p.id, role=p.role, detail={"run": run, "op": "edit", "id": bid})
    return it


@router.delete("/api/runs/{run}/bookmarks/{bid}", status_code=204)
async def delete_bookmark(request: Request, run: str, bid: str, p: Operator) -> Response:
    ctx = app_ctx(request)
    d = _run_dir(request, run)

    def apply() -> None:
        with _lock:
            m = _bm_load(d)
            n = len(m["items"])
            m["items"] = [x for x in m["items"] if x.get("id") != bid]
            if len(m["items"]) == n:
                raise ApiProblem(305, status=404, detail={"bookmark": bid})
            _atomic_json(_bm_path(d), m)

    await anyio.to_thread.run_sync(apply)
    ctx.audit.write("runs.bookmark", principal_id=p.id, role=p.role, detail={"run": run, "op": "delete", "id": bid})
    return Response(status_code=204)
