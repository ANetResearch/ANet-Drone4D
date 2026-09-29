"""派生缓存（M04 §6.2.4、FR-005）：`worlds/.geo-cache/<world>/<contentVersion>-<derive_sha8>/`。

键 = sha256(contentVersion, coordinate.sha256, GeoParams, DERIVE_VERSION) 取 8 位；先写 `tmp-<pid>-<ns>/` 再原子
rename（他人已发布则丢弃本次结果）；`manifest.json` 记录各数组形状、dtype、sha256 与栅格参数；每个世界保留最近 3 个条目。
缓存不属于 World Package，不进入 `files[]` 与 `contentVersion`。
"""

from __future__ import annotations

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
_DT = {"float32": "<f4", "uint8": "u1"}


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
    return out


def write_entry(final: Path, ds: DerivedSet, meta: dict) -> Path:
    final = Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / f"tmp-{os.getpid()}-{time.monotonic_ns()}"
    tmp.mkdir()
    try:
        arrays = []
        for name, a in _arrays(ds):
            dtype = "uint8" if a.dtype == np.uint8 else "float32"
            ext = "u8" if dtype == "uint8" else "f32"
            arr = np.ascontiguousarray(a, _DT[dtype])
            data = arr.tobytes()
            (tmp / f"{name}.{ext}").write_bytes(data)
            arrays.append({"name": name, "href": f"{name}.{ext}", "shape": list(arr.shape), "dtype": dtype,
                           "sha256": hashlib.sha256(data).hexdigest()})
        doc = dict(meta, arrays=arrays, qa=ds.qa)
        (tmp / "qa.json").write_text(json.dumps(ds.qa, indent=1) + "\n", encoding="utf-8")
        (tmp / "manifest.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            os.rename(tmp, final)
        except OSError as e:
            if e.errno in (errno.ENOTEMPTY, errno.EEXIST):      # 他人已发布同一键：丢弃本次
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                raise
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
    for a in m.get("arrays", []):
        p = Path(d) / a["href"]
        size = int(np.prod(a["shape"])) * (1 if a["dtype"] == "uint8" else 4)
        if not p.exists() or p.stat().st_size != size:
            return False
        if deep and hashlib.sha256(p.read_bytes()).hexdigest() != a["sha256"]:
            return False
    return True


def load_entry(d: Path) -> tuple[dict, dict[str, np.ndarray]]:
    m = read_manifest(d)
    if m is None:
        raise GeoLoadError("GEO_CACHE_MISS", str(d))
    arrs = {}
    for a in m["arrays"]:
        arrs[a["name"]] = np.memmap(Path(d) / a["href"], dtype=_DT[a["dtype"]], mode="r", shape=tuple(a["shape"]))
    return m, arrs
