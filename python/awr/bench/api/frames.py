"""BN-42、BN-43、BN-49 至 BN-51：运行列表与状态、帧索引、单帧与区间帧（AWR-17 §18.8；M27-FR-052）。

单帧返回交付字节（`Content-Type` 按通道，`X-Frame-T-Ns`），`ETag` 为内容 sha256，支持单区间 `Range` 与 `If-None-Match`。
帧接口接受会话、bench token 或 `aud = frames` 的短期令牌。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import APIRouter, Query, Request, Response

from awr.api.problem import ApiProblem
from awr.bench.api.deps import FrameReader, Researcher
from awr.bench.recorder.read import FrameLimitError, FrameStore
from awr.bench.runs_store import find_run, list_runs

router = APIRouter()
_IMMUTABLE = "private, max-age=31536000, immutable"


class StoreCache:
    """每个 Run 一个 FrameStore；frames.idx 变化（重新生成）时重开。"""

    def __init__(self, cap: int = 16) -> None:
        self.cap = cap
        self.items: dict[str, tuple[float, FrameStore]] = {}
        self.lock = threading.Lock()

    def get(self, run_dir: Path, run_id: str) -> FrameStore | None:
        idx = run_dir / "frames.idx"
        if not idx.exists():
            return None
        m = idx.stat().st_mtime
        with self.lock:
            hit = self.items.get(run_id)
            if hit and hit[0] == m:
                return hit[1]
            if hit:
                hit[1].close()
            fs = FrameStore(run_dir, run_id)
            self.items[run_id] = (m, fs)
            while len(self.items) > self.cap:
                _k, (_m, old) = self.items.pop(next(iter(self.items)))
                old.close()
            return fs


def _store(request: Request, rid: str) -> FrameStore:
    d = find_run(request.app.state.settings.dataset_dir, rid)
    fs = request.app.state.frame_stores.get(d, rid) if d else None
    if fs is None:
        raise ApiProblem(305, detail={"run_id": rid})
    return fs


@router.get("/runs")
def runs(request: Request, p: Researcher) -> dict:
    items = list_runs(request.app.state.settings.dataset_dir)
    return {"items": items, "next_cursor": None}


@router.get("/runs/{rid}")
def run(rid: str, request: Request, p: Researcher) -> dict:
    d = find_run(request.app.state.settings.dataset_dir, rid)
    if d is None:
        raise ApiProblem(305, detail={"run_id": rid})
    m = d / "run_manifest.json"
    doc = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
    out = {"run_id": rid, "event_id": d.parent.parent.name, "state": doc.get("state", "UNKNOWN"), "kind": doc.get("kind"),
           "world_id": doc.get("world_id"), "fleet": doc.get("fleet", [])}
    fs = request.app.state.frame_stores.get(d, rid)
    if fs is not None:
        out["frames"] = [{"uav_id": u, "sensor_id": s, "latest_seq": fs.latest_seq(u, s)} for u, s in fs.idx.channels()]
    return out


@router.get("/runs/{rid}/frames/{uav}/{sensor}:range")
def frame_range(rid: str, uav: str, sensor: str, request: Request, p: FrameReader, seq_from: int = Query(ge=0), seq_to: int = Query(ge=0),
                stride: int = Query(1, ge=1)) -> Response:
    fs = _store(request, rid)
    try:
        b = fs.range_container(uav, sensor, seq_from, seq_to, stride)
    except FrameLimitError as e:
        raise ApiProblem(698, detail=e.detail) from None
    if b is None:
        raise ApiProblem(305, detail={"uav": uav, "sensor": sensor})
    return Response(b, media_type="application/x-d3frames", headers={"Cache-Control": "private, no-store"})


@router.get("/runs/{rid}/frames/{uav}/{sensor}")
def frame_index(rid: str, uav: str, sensor: str, request: Request, p: FrameReader, seq_from: int = Query(0, ge=0),
                seq_to: int | None = Query(None, ge=0)) -> dict:
    page = _store(request, rid).index_page(uav, sensor, seq_from, seq_to)
    if page is None:
        raise ApiProblem(305, detail={"uav": uav, "sensor": sensor})
    return page


def _range(h: str, n: int) -> tuple[int, int] | None:
    """单区间 `bytes=a-b`、`bytes=a-`、`bytes=-k`；越界返回 None（416）。"""
    try:
        unit, spec = h.split("=", 1)
        a, b = spec.split("-", 1)
        if unit.strip() != "bytes" or "," in spec:
            return None
        if a == "":
            k = int(b)
            return (max(n - k, 0), n - 1) if k > 0 else None
        lo = int(a)
        hi = min(int(b), n - 1) if b else n - 1
        return (lo, hi) if lo <= hi and lo < n else None
    except ValueError:
        return None


@router.get("/runs/{rid}/frames/{uav}/{sensor}/{seq}")
def frame(rid: str, uav: str, sensor: str, seq: int, request: Request,
          p: FrameReader) -> Response:
    f = _store(request, rid).frame(uav, sensor, seq)
    if f is None:
        raise ApiProblem(305, detail={"uav": uav, "sensor": sensor, "seq": seq})
    etag = f'"{f.sha256}"'
    hdr = {"ETag": etag, "Cache-Control": _IMMUTABLE, "X-Frame-T-Ns": str(f.t_ns), "Accept-Ranges": "bytes"}
    if f.width:
        hdr["X-Frame-Size"] = f"{f.width}x{f.height}"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=hdr)
    rh = request.headers.get("range")
    if rh:
        r = _range(rh, len(f.payload))
        if r is None:
            raise ApiProblem(309, headers={"Content-Range": f"bytes */{len(f.payload)}"})
        hdr["Content-Range"] = f"bytes {r[0]}-{r[1]}/{len(f.payload)}"
        return Response(f.payload[r[0]:r[1] + 1], status_code=206, media_type=f.content_type, headers=hdr)
    return Response(f.payload, media_type=f.content_type, headers=hdr)
