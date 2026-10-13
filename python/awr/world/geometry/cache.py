"""派生缓存（M04 §6.2.4、FR-005）：`worlds/.geo-cache/<world>/<contentVersion>-<derive_sha8>/`。

键 = sha256(contentVersion, coordinate.sha256, GeoParams, DERIVE_VERSION) 取 8 位；先写 `tmp-<pid>-<ns>/` 再原子
rename（他人已发布则丢弃本次结果）；`manifest.json` 记录各数组形状、dtype、sha256 与栅格参数；每个世界保留最近 3 个条目。
缓存不属于 World Package，不进入 `files[]` 与 `contentVersion`。

D2：条目含 Grid25（`grid25.f64`、可选 `grid25_base.f64`、`grid25_floor.f32`、`grid25_blocked.u8`，manifest 的 `grid25`
记录其栅格参数）；缓存根可由 `AWR_GEO_CACHE_DIR` 指定（部署包把派生缓存与世界包分开放置）。
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np

from .derive import DerivedSet
from .types import DERIVE_VERSION, GeoLoadError, GeoParams

KEEP_ENTRIES = 3
_DT = {"float32": "<f4", "uint8": "u1", "float64": "<f8"}
_EXT = {"float32": "f32", "uint8": "u8", "float64": "f64"}
_ITEM = {"float32": 4, "uint8": 1, "float64": 8}
GRID25_ARRAYS = ("grid25", "grid25_base", "grid25_floor", "grid25_blocked")
ENV_CACHE_DIR = "AWR_GEO_CACHE_DIR"


def default_cache_root(world_dir: Path) -> Path:
    """缺省缓存根：环境变量 `AWR_GEO_CACHE_DIR`（非空时），否则 `<世界目录的上级>/.geo-cache`。"""
    env = os.environ.get(ENV_CACHE_DIR, "").strip()
    return Path(env) if env else Path(world_dir).parent / ".geo-cache"


def derive_key(content_version: str, coordinate_sha256: str, params: GeoParams) -> str:
    s = json.dumps([content_version, coordinate_sha256, params.to_json(), DERIVE_VERSION], sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def entry_dir(cache_root: Path, world_id: str, content_version: str, key: str) -> Path:
    return Path(cache_root) / world_id / f"{content_version}-{key}"


def _arrays(ds: DerivedSet) -> list[tuple[str, np.ndarray]]:
    out = [("obs_n", ds.obs_n), ("dsm_eff", ds.dsm_eff), ("dsm_dil1", ds.dsm_dil1), ("hm", ds.hm),
           ("inflated_1m", ds.inflated_1m), ("zone_raster_8m", ds.zone_raster)]
    out += [(f"pyr_dsm_L{i}", a) for i, a in enumerate(ds.pyr_dsm) if i >= 1]
    out += [(f"pyr_hm_L{i}", a) for i, a in enumerate(ds.pyr_hm) if i >= 1]
    out += [(f"pyrdil_hm_L{i}", a) for i, a in enumerate(ds.pyrdil_hm) if a is not None]
    out += [(f"pyr_inflated_1m_L{i}", a) for i, a in enumerate(ds.pyr_inflated_1m) if i >= 1]
    g = ds.grid25
    if g is not None:
        out += [("grid25", g.a)] + ([("grid25_base", g.base)] if g.base is not None else [])
        out += [("grid25_floor", g.floor), ("grid25_blocked", g.blocked)]
    return out


def _dtype_name(a: np.ndarray) -> str:
    if a.dtype == np.uint8:
        return "uint8"
    if a.dtype == np.float64:
        return "float64"
    return "float32"


def write_entry(final: Path, ds: DerivedSet, meta: dict) -> Path:
    final = Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / f"tmp-{os.getpid()}-{time.monotonic_ns()}"
    tmp.mkdir()
    try:
        arrays = []
        for name, a in _arrays(ds):
            dtype = _dtype_name(a)
            ext = _EXT[dtype]
            arr = np.ascontiguousarray(a, _DT[dtype])
            data = arr.tobytes()
            (tmp / f"{name}.{ext}").write_bytes(data)
            arrays.append({"name": name, "href": f"{name}.{ext}", "shape": list(arr.shape), "dtype": dtype,
                           "sha256": hashlib.sha256(data).hexdigest()})
        doc = dict(meta, arrays=arrays, qa=ds.qa, grid25=None if ds.grid25 is None else ds.grid25.meta)
        (tmp / "qa.json").write_text(json.dumps(ds.qa, indent=1) + "\n", encoding="utf-8")
        (tmp / "manifest.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            os.rename(tmp, final)
        except OSError as e:
            if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
            if verify_entry(final):                              # 他人已发布同一键：丢弃本次
                shutil.rmtree(tmp, ignore_errors=True)
            else:                                                # 同键的损坏条目：移开（prune 稍后清理）后发布本次
                with contextlib.suppress(OSError):
                    os.rename(final, final.parent / f"tmp-{os.getpid()}-{time.monotonic_ns()}-stale")
                try:
                    os.rename(tmp, final)
                except OSError:
                    shutil.rmtree(tmp, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    prune(final.parent)
    return final


def prune(world_cache: Path, keep: int = KEEP_ENTRIES) -> None:
    entries = [p for p in Path(world_cache).iterdir() if p.is_dir() and not p.name.startswith("tmp-")]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in entries[keep:]:
        shutil.rmtree(p, ignore_errors=True)
    now = time.time()
    for p in Path(world_cache).glob("tmp-*"):
        try:
            if now - p.stat().st_mtime > 600:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            continue


def read_manifest(d: Path) -> dict | None:
    try:
        return json.loads((Path(d) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_entry(d: Path, *, deep: bool = False) -> bool:
    m = read_manifest(d)
    if m is None or m.get("derive_version") != DERIVE_VERSION:
        return False
    g = m.get("grid25")
    if g is not None:
        names = {a.get("name") for a in m.get("arrays", [])}
        need = {"grid25", "grid25_floor", "grid25_blocked"} | ({"grid25_base"} if g.get("has_base") else set())
        if not need <= names:
            return False
    for a in m.get("arrays", []):
        p = Path(d) / a["href"]
        item = _ITEM.get(a.get("dtype"))
        if item is None:
            return False
        size = int(np.prod(a["shape"])) * item
        if not p.exists() or p.stat().st_size != size:
            return False
        if deep and _sha256_file(p) != a["sha256"]:
            return False
    return True


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def entry_bytes(m: dict) -> dict[str, int]:
    """条目各部分的字节数（manifest 口径）：`total`、`grid25`（Grid25 四个数组之和）。世界文件页工作集的保守上界。"""
    total = g25 = 0
    for a in m.get("arrays", []):
        n = int(np.prod(a["shape"])) * _ITEM.get(a.get("dtype"), 4)
        total += n
        if a["name"] in GRID25_ARRAYS:
            g25 += n
    return {"total": total, "grid25": g25}


def map_array(d: Path, a: dict, mode: str = "r") -> np.memmap:
    return np.memmap(Path(d) / a["href"], dtype=_DT[a["dtype"]], mode=mode, shape=tuple(a["shape"]))


def load_entry(d: Path) -> tuple[dict, dict[str, np.ndarray]]:
    m = read_manifest(d)
    if m is None:
        raise GeoLoadError("GEO_CACHE_MISS", str(d))
    arrs = {}
    for a in m["arrays"]:
        arrs[a["name"]] = map_array(d, a)
    return m, arrs
