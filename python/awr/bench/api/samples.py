"""样例包目录与 Run 派生物（D3-AC-56"样例包在内部工作区可浏览"；AWR-17 §18.8.4 BN-53、BN-54；14 §21.22；M27 §7.6）。

- `GET /samples`（BN-53.samples，研究者及以上）：`<data_root>/samples/<pack_id>/pack.json`（`drone4d.bench.sample_pack.v1`，由
  `tools/bench/d3/sample_pack.py` 写出）的列表 `{items: [...]}`，按生成时刻由新到旧。只保留运行已安装到数据集目录的样例；
  test 划分的运行对评测员以下不列出（M27-FR-120"test 结果只含指标"）。
- BN-53 `GET /runs/{rid}/artifacts`：派生物列表 `{items[]{path, kind, layer, bytes, sha256, job_id, retention, retain_until_unix_ns}}`，
  路径相对运行目录的 `derived/`。来源为数据集导出（`awr.bench.export.dataset.artifact_items`）与派生物索引
  `derived/index.json`（`drone4d.bench.derived_index.v1`：样例包的视频、轨迹、磁带、录制与评测）；文件缺失或字节数与索引不符的
  条目不列出，已按保留期清理的种类不列出；`layer = truth` 只对评测员及以上。
- BN-54 `GET /runs/{rid}/artifacts/{path}`：下载列表中的条目。路径只接受 `derived/` 内的相对路径（305）；已清理 410 `697`；
  真值层对评测员以下 403 `691`；test 划分的运行对评测员以下 403 `691`。`ETag` 为 sha256，支持 `If-None-Match` 与单区间 `Range`
  （206；越界 416 `309`），正文以 1 MiB 分块流式发送。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from awr.api.problem import ApiProblem
from awr.bench import retention
from awr.bench.api.deps import Researcher
from awr.bench.api.frames import _range
from awr.bench.api.truth import guard_run, run_split_of
from awr.bench.auth import Principal
from awr.bench.runs_store import find_run
from awr.bench.split import INTERNAL_ROLES

__all__ = ["INDEX_SCHEMA", "PACK_SCHEMA", "derived_items", "router", "safe_path"]

router = APIRouter()
PACK_SCHEMA = "drone4d.bench.sample_pack.v1"
INDEX_SCHEMA = "drone4d.bench.derived_index.v1"
_SEG = re.compile(r"^[A-Za-z0-9._-]+$")
_PACK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CHUNK = 1 << 20
_MEDIA = {".mp4": "video/mp4", ".mcap": "application/x-mcap", ".json": "application/json", ".rrd": "application/vnd.rerun.rrd",
          ".bin": "application/octet-stream", ".db3": "application/octet-stream"}


def safe_path(p: str) -> bool:
    """派生物相对路径：不以 / 开头，不含 ..、反斜杠、空段，段名只含安全字符（与 `apps/web/.../samplePack.ts` 的 `safeArtifactPath` 一致）。"""
    if not p or len(p) > 256 or p.startswith("/"):
        return False
    return all(seg not in ("", ".", "..") and _SEG.match(seg) for seg in p.split("/"))


def _read(p: Path) -> dict | None:
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _derived_kind(path: str) -> str:
    head = path.split("/", 1)[0]
    return head if "/" in path and head in retention.DERIVED_KINDS else "files"


def _retention(rec: dict, kind: str, policy: retention.RetentionPolicy = retention.POLICY) -> tuple[str, int | None]:
    """派生物种类的保留类别与保留至（pinned 为 None）。"""
    d = rec.get("derived", {}).get(kind) or {}
    cls = d.get("retention") or retention.DERIVED_DEFAULT.get(kind, "standard")
    if cls == "pinned" or d.get("pinned_by"):
        return "pinned", None
    t0 = d.get("created_unix_ns")
    return cls, (int(t0) + policy.derived_days[cls] * policy.day_ns if t0 is not None and cls in policy.derived_days else None)


def derived_items(run_dir: Path) -> list[dict]:
    """BN-53 的全部条目（不按角色过滤）：数据集导出部分加派生物索引部分，同一路径以导出为准。"""
    from awr.bench.export.dataset import artifact_items

    run_dir = Path(run_dir)
    out = {it["path"]: it for it in artifact_items(run_dir)}
    idx = _read(run_dir / "derived" / "index.json")
    if idx and idx.get("schema") == INDEX_SCHEMA:
        rec = retention.load_record(run_dir)
        expired = rec.get("expired", {}).get("derived", {})
        for it in idx.get("items") or []:
            p = str(it.get("path", ""))
            if p in out or not safe_path(p):
                continue
            kind = _derived_kind(p)
            f = run_dir / "derived" / p
            if kind in expired or (rec.get("expired", {}).get("run") and p == "raw.mcap") or not f.is_file() \
                    or f.stat().st_size != it.get("bytes"):
                continue
            cls, until = _retention(rec, kind)
            out[p] = {"path": p, "kind": it.get("kind") or kind, "layer": it.get("layer", "obs"), "bytes": int(it["bytes"]),
                      "sha256": it.get("sha256"), "job_id": None, "retention": cls, "retain_until_unix_ns": until,
                      "media_type": it.get("media_type")}
    return sorted(out.values(), key=lambda x: x["path"])


def _run(request: Request, p: Principal, rid: str) -> Path:
    d = find_run(request.app.state.settings.dataset_dir, rid)
    if d is None:
        raise ApiProblem(305, detail={"run_id": rid})
    guard_run(request, p, rid, d)                                    # test Run：评测员以下 691
    return d


def _visible(p: Principal, it: dict) -> bool:
    return it.get("layer") != "truth" or p.role in INTERNAL_ROLES


@router.get("/samples")
def samples(request: Request, p: Researcher) -> dict:
    """样例包目录（BN-53.samples）：每份 `pack.json` 原样返回，样例按可见性收窄。"""
    root = Path(request.app.state.settings.data_root) / "samples"
    items = []
    for f in sorted(root.glob("*/pack.json")) if root.is_dir() else []:
        doc = _read(f)
        if not doc or doc.get("schema") != PACK_SCHEMA or not _PACK_ID.match(str(doc.get("pack_id", ""))):
            continue
        keep = []
        for s in doc.get("samples") or []:
            rid = str((s or {}).get("run_id", ""))
            d = find_run(request.app.state.settings.dataset_dir, rid)
            if d is None:
                continue
            split, _eid = run_split_of(request, rid, d)
            if split == "dev" or p.role in INTERNAL_ROLES:
                keep.append(s)
        items.append({**doc, "samples": keep})
    items.sort(key=lambda x: (str(x.get("generated_utc") or ""), str(x["pack_id"])), reverse=True)
    return {"items": items}


@router.get("/runs/{rid}/artifacts")
def artifacts(rid: str, request: Request, p: Researcher) -> dict:
    """BN-53：派生物列表（路径相对 `derived/`）。"""
    d = _run(request, p, rid)
    return {"items": [it for it in derived_items(d) if _visible(p, it)]}


def _chunks(path: Path, lo: int, hi: int) -> Iterator[bytes]:
    with open(path, "rb") as f:
        f.seek(lo)
        left = hi - lo + 1
        while left > 0:
            b = f.read(min(_CHUNK, left))
            if not b:
                return
            left -= len(b)
            yield b


@router.get("/runs/{rid}/artifacts/{path:path}")
def artifact(rid: str, path: str, request: Request, p: Researcher) -> Response:
    """BN-54：派生物下载（Range、ETag = sha256；已清理 410 `697`；真值层评测员以下 691）。"""
    d = _run(request, p, rid)
    if not safe_path(path):
        raise ApiProblem(305, detail={"run_id": rid, "path": path})
    kind = _derived_kind(path)
    gone = retention.expired_reason(d, kind) or (retention.expired_reason(d, "run") if path == "raw.mcap" else None)
    if gone is not None:
        raise ApiProblem(697, detail=gone["detail"])
    it = next((x for x in derived_items(d) if x["path"] == path), None)
    if it is None:
        raise ApiProblem(305, detail={"run_id": rid, "path": path})
    if not _visible(p, it):
        raise ApiProblem(691, detail={"why": "truth_layer", "run_id": rid, "path": path})
    f = d / "derived" / path
    if not f.resolve().is_relative_to((d / "derived").resolve()):
        raise ApiProblem(305, detail={"run_id": rid, "path": path})
    n = f.stat().st_size
    hdr = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-cache"}
    if it.get("sha256"):
        hdr["ETag"] = f'"{it["sha256"]}"'
        if request.headers.get("if-none-match") == hdr["ETag"]:
            return Response(status_code=304, headers=hdr)
    media = it.get("media_type") or _MEDIA.get(f.suffix, "application/octet-stream")
    rh = request.headers.get("range")
    if rh:
        r = _range(rh, n)
        if r is None:
            raise ApiProblem(309, headers={"Content-Range": f"bytes */{n}"})
        hdr.update({"Content-Range": f"bytes {r[0]}-{r[1]}/{n}", "Content-Length": str(r[1] - r[0] + 1)})
        return StreamingResponse(_chunks(f, r[0], r[1]), status_code=206, media_type=media, headers=hdr)
    hdr["Content-Length"] = str(n)
    return StreamingResponse(_chunks(f, 0, n - 1), media_type=media, headers=hdr)
