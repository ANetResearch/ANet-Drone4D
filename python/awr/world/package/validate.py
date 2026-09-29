"""World Package v1 校验器（AWR-16 §15.3 的 84 条规则；M03-FR-047、FR-048、§6.11）。

三层：结构（JSON Schema）、语义（跨文件一致性）、deep（逐节点解码、全部 sha256）。规则以
`@rule(id, layer, severity)` 注册，ID 稳定，供变异测试与 `qa/report.json` 使用。由 g03 `worldpkg_validate.py`
迁移并编号；坐标与精度复算调用 M02 `frames`（M03-FR-050）。
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import math
import os
import re
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np

from awr.world.georef.frames import Anchor, T_ecef_world, curvature_drop_m

from ..pointcloud.encode import oct16_decode
from ..pointcloud.morton import B as MORTON_B
from ..pointcloud.morton import morton_codes
from ..pointcloud.npx import row_norm3
from ..pointcloud.reader import EXT_REC, LEAF, NORMAL, PROXY, REC, HierarchyError, parse_hierarchy
from ..pointcloud.writer import rule_g_level
from ..semantic.zones import check_zone_geometry
from ..terrain.grids import Grid, bilinear_on_centres
from .jsonio import sha256_file
from .manifest import NON_CONTENT, NON_CONTENT_DIRS, content_version
from .schemas import classes_source_path, presets_path, schema_errors

VALIDATOR_NAME = "awr.worldpkg.validate"
VALIDATOR_VERSION = "0.1.0"
WORLD_ID_RE = re.compile(r"^[a-z0-9-]{1,63}$")
REQUIRED_LAYERS = [
    ("pointcloud.visual", "pointcloud", "visual", "potree2/anet-q16@1", "visual/pointcloud/"),
    ("pointcloud.source", "pointcloud", "geometry", "awr-pts@1", "geometry/pointcloud/source/source.json"),
    ("terrain.dtm", "terrain", "geometry", "f32-grid@1", "geometry/terrain/dtm_10m.json"),
    ("terrain.dsm", "terrain", "geometry", "f32-grid@1", "geometry/terrain/dsm_2m.json"),
    ("semantic.classes", "class-table", "semantic", "anet-classes@1", "semantic/anet-classes@1.json"),
    ("semantic.zones", "semantic-zones", "semantic", "geojson/awr-zones@1", "semantic/zones.geojson"),
    ("environment.config", "environment", "environment", "awr-env-world@1", "environment/env.json"),
]
ITEMSIZE = {"float32": 4, "float16": 2, "uint8": 1, "uint16": 2}
NP_DTYPE = {"float32": "<f4", "float16": "<f2", "uint8": "u1", "uint16": "<u2"}


# ---------------------------------------------------------------- 报告


@dataclass
class Report:
    path: str = ""
    world_id: str | None = None
    deep: bool = False
    staging: bool = False
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    rules_checked: int = 0
    seconds: float = 0.0
    skipped: bool = False
    _rule: str = ""

    def error(self, message: str, where: str = "", rule: str | None = None) -> None:
        self.errors.append({"rule": rule or self._rule, "where": where, "message": message})

    def warn(self, message: str, where: str = "", rule: str | None = None) -> None:
        self.warnings.append({"rule": rule or self._rule, "where": where, "message": message})

    def check(self, cond: bool, message: str, where: str = "", *, warn: bool = False) -> bool:
        if not cond:
            (self.warn if warn else self.error)(message, where)
        return bool(cond)

    @property
    def ok(self) -> bool:
        return not self.errors

    def rule_ids(self, kind: str = "errors") -> set[str]:
        return {e["rule"] for e in getattr(self, kind)}

    def to_json(self) -> dict:
        return {"path": self.path, "world_id": self.world_id, "ok": self.ok, "deep": self.deep,
                "seconds": round(self.seconds, 3), "errors": self.errors, "warnings": self.warnings,
                "info": {**self.info, "rules_checked": self.rules_checked}}


@dataclass(frozen=True)
class RuleDef:
    id: str
    layer: str          # structure | semantic | deep
    severity: str       # E | W | E/W
    fn: Callable


RULES: dict[str, RuleDef] = {}


def rule(rid: str, layer: str = "semantic", severity: str = "E"):
    def deco(fn):
        RULES[rid] = RuleDef(rid, layer, severity, fn)
        return fn
    return deco


class Skip(Exception):
    """前置文件缺失或已由其他规则报告：本规则不再重复报告。"""


# ---------------------------------------------------------------- 惰性视图


@dataclass
class RootView:
    entry: dict
    dir: Path
    metadata: dict | None = None
    md_error: str | None = None
    hier: bytes | None = None
    records: list | None = None
    chunks: list | None = None
    parse_error: str | None = None
    ext: np.ndarray | None = None
    ext_bytes: bytes | None = None
    octree_size: int = 0

    @property
    def real(self) -> list:
        return [r for r in (self.records or []) if r.type != PROXY]


class WorldView:
    def __init__(self, world_dir: Path, *, staging: bool = False):
        self.dir = Path(world_dir)
        self.staging = staging
        self._json_cache: dict[str, Any] = {}

    def load_json(self, rel: str):
        if rel not in self._json_cache:
            p = self.dir / rel
            try:
                self._json_cache[rel] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                self._json_cache[rel] = e
        v = self._json_cache[rel]
        if isinstance(v, Exception):
            raise Skip(f"{rel}: {v}")
        return v

    def exists(self, rel: str) -> bool:
        return (self.dir / rel).exists()

    @cached_property
    def world(self) -> dict:
        w = self.load_json("world.json")
        if not isinstance(w, dict):
            raise Skip("world.json is not an object")
        return w

    @cached_property
    def coordinate(self) -> dict:
        href = (self.world.get("coordinate") or {}).get("href", "coordinate.json")
        c = self.load_json(href)
        if not isinstance(c, dict):
            raise Skip("coordinate.json is not an object")
        return c

    @cached_property
    def layers(self) -> dict[str, dict]:
        return {L.get("id"): L for L in self.world.get("layers", []) if isinstance(L, dict)}

    def layer(self, lid: str) -> dict:
        L = self.layers.get(lid)
        if L is None:
            raise Skip(f"layer {lid} missing")
        return L

    @cached_property
    def extent(self) -> tuple[np.ndarray, np.ndarray]:
        e = self.coordinate["extent"]
        return np.asarray(e["min"], np.float64), np.asarray(e["max"], np.float64)

    @cached_property
    def pc_layer(self) -> dict:
        return self.layer("pointcloud.visual")

    @cached_property
    def roots(self) -> list[RootView]:
        out = []
        for r in self.pc_layer.get("roots", []):
            rv = RootView(entry=r, dir=self.dir / r.get("href", ""))
            try:
                rv.metadata = json.loads((rv.dir / "metadata.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                rv.md_error = str(e)
                out.append(rv)
                continue
            try:
                rv.hier = (rv.dir / "hierarchy.bin").read_bytes()
                rv.octree_size = (rv.dir / "octree.bin").stat().st_size
            except OSError as e:
                rv.parse_error = str(e)
                out.append(rv)
                continue
            try:
                rv.records, rv.chunks = parse_hierarchy(rv.hier, int(rv.metadata["hierarchy"]["firstChunkSize"]))
            except (HierarchyError, KeyError, TypeError, struct.error) as e:
                rv.parse_error = str(e)
            a = rv.metadata.get("anet") or {}
            he = a.get("hierarchyExt")
            if he:
                try:
                    rv.ext_bytes = (rv.dir / he["href"]).read_bytes()
                    if len(rv.ext_bytes) % EXT_REC == 0:
                        rv.ext = np.frombuffer(rv.ext_bytes, "<u2").reshape(-1, 6)
                except OSError:
                    rv.ext_bytes = None
            out.append(rv)
        return out

    @cached_property
    def class_table(self) -> dict:
        return self.load_json("semantic/anet-classes@1.json")

    @cached_property
    def n_classes(self) -> int:
        try:
            return len(self.class_table["classes"])
        except (Skip, KeyError, TypeError):
            return 16

    def grid(self, lid: str) -> tuple[dict, np.ndarray | None]:
        L = self.layer(lid)
        sc = self.load_json(L["href"])
        p = (self.dir / L["href"]).parent / sc.get("href", "")
        try:
            a = np.fromfile(p, NP_DTYPE[sc["dtype"]])
            if a.size != int(sc["width"]) * int(sc["height"]):
                return sc, None
            return sc, a.reshape(int(sc["height"]), int(sc["width"]))
        except (OSError, KeyError, ValueError):
            return sc, None

    @cached_property
    def coordinate_sha(self) -> str:
        href = (self.world.get("coordinate") or {}).get("href", "coordinate.json")
        return sha256_file(self.dir / href)


# ---------------------------------------------------------------- 工具


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _mat(m) -> np.ndarray:
    return np.asarray(m, np.float64)


def _rule_g(w: WorldView) -> tuple[int, int, list[tuple[RootView, int]]]:
    """规则 G 按各根复算：返回 (世界首屏点数, 字节, [(根, 层)])。"""
    k = len(w.roots)
    pts = byt = 0
    out = []
    for rv in w.roots:
        if rv.metadata is None or not rv.real:
            raise Skip("root unreadable")
        lp, lbe, depth = _levels(rv)
        L = rule_g_level(lp, depth, k)
        pts += lp[L]
        byt += lbe[L]
        out.append((rv, L))
    return pts, byt, out


def _levels(rv: RootView) -> tuple[list[int], list[int], int]:
    real = rv.real
    depth = max(n.level for n in real)
    lvl_end = [0] * (depth + 1)
    lvl_pts = [0] * (depth + 1)
    pos = 0
    for n in sorted(real, key=lambda n: (n.level, n.name)):
        if n.n:
            pos = max(pos, n.off + n.size)
        lvl_end[n.level] = pos
        lvl_pts[n.level] += n.n
    for L in range(1, depth + 1):
        lvl_pts[L] += lvl_pts[L - 1]
        lvl_end[L] = max(lvl_end[L], lvl_end[L - 1])
    return lvl_pts, lvl_end, depth


def _name_key(name: str) -> tuple[int, int, int, int]:
    x = y = z = 0
    for ch in name[1:]:
        c = int(ch)
        x, y, z = 2 * x + ((c >> 2) & 1), 2 * y + ((c >> 1) & 1), 2 * z + (c & 1)
    return len(name) - 1, x, y, z


# ================================================================ V-C 坐标


@rule("V-C-01", "semantic")
def c01(w: WorldView, rep: Report):
    c = w.coordinate
    rep.check(np.allclose(_mat(c["T_ecef_world"])[3], [0, 0, 0, 1]), "T_ecef_world last row must be [0,0,0,1]", "coordinate.json")
    if c["source"].get("T_world_source") is not None:
        rep.check(np.allclose(_mat(c["source"]["T_world_source"])[3], [0, 0, 0, 1]),
                  "T_world_source last row must be [0,0,0,1]", "coordinate.json")


@rule("V-C-02", "semantic")
def c02(w: WorldView, rep: Report):
    c = w.coordinate
    T = _mat(c["T_ecef_world"])
    Te = _mat(T_ecef_world(Anchor.from_coordinate(c)))
    dr = float(np.abs(T[:3, :3] - Te[:3, :3]).max())
    dt = float(np.abs(T[:3, 3] - Te[:3, 3]).max())
    rep.check(dr <= 1e-6 and dt <= 1e-2, f"T_ecef_world inconsistent with anchor (rotation {dr:.2e}, translation {dt:.3f} m)",
              "coordinate.json")


def _src_T(w: WorldView) -> np.ndarray:
    T = w.coordinate["source"].get("T_world_source")
    if T is None:
        raise Skip("T_world_source is null")
    return _mat(T)


@rule("V-C-03", "semantic")
def c03(w: WorldView, rep: Report):
    det = float(np.linalg.det(_src_T(w)[:3, :3]))
    hand = w.coordinate["source"]["handedness"]
    rep.check((det > 0) == (hand == "right"), f"det(T_world_source)={det:.4g} contradicts handedness={hand}", "coordinate.json")


@rule("V-C-04", "semantic")
def c04(w: WorldView, rep: Report):
    s = abs(float(np.linalg.det(_src_T(w)[:3, :3]))) ** (1 / 3)
    u = float(w.coordinate["source"]["unitsToMeters"])
    rep.check(_close(s, u, 1e-4), f"cbrt|det|={s:.6g} != unitsToMeters={u}", "coordinate.json")


@rule("V-C-05", "semantic")
def c05(w: WorldView, rep: Report):
    M = _src_T(w)[:3, :3]
    s = abs(float(np.linalg.det(M))) ** (1 / 3)
    Rn = M / s
    dev = float(np.abs(Rn @ Rn.T - np.eye(3)).max())
    rep.check(dev <= 1e-4, f"T_world_source[:3,:3]/scale not orthonormal (max deviation {dev:.2e})", "coordinate.json")


@rule("V-C-06", "semantic")
def c06(w: WorldView, rep: Report):
    src = w.coordinate["source"]
    M = _src_T(w)[:3, :3]
    s = abs(float(np.linalg.det(M))) ** (1 / 3)
    Rn = M / s
    ax = {"x": 0, "y": 1, "z": 2}[src["upAxis"][1]]
    sign = 1.0 if src["upAxis"][0] == "+" else -1.0
    dot = float(Rn[2, ax] * sign)
    rep.check(dot > math.cos(math.radians(30)), f"source.upAxis {src['upAxis']} does not map to world +Z (dot {dot:.3f})",
              "coordinate.json")


@rule("V-C-07", "semantic")
def c07(w: WorldView, rep: Report):
    src = w.coordinate["source"]
    if src.get("T_world_source") is None:
        rep.check(bool(src.get("projPipeline")), "T_world_source null requires projPipeline", "coordinate.json")


@rule("V-C-08", "semantic")
def c08(w: WorldView, rep: Report):
    a = w.coordinate["anchor"]
    if a.get("geoid") and a.get("hMslM") is not None:
        d = abs(float(a["hEllipsoidM"]) - float(a["geoid"]["undulationM"]) - float(a["hMslM"]))
        rep.check(d <= 0.05, f"hMslM != hEllipsoidM - geoid.undulationM (diff {d:.3f} m)", "coordinate.json")


@rule("V-C-09", "semantic", "E/W")
def c09(w: WorldView, rep: Report):
    lo, hi = w.extent
    rmax = max(math.hypot(x, y) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]))
    p = w.coordinate["precision"]
    rep.check(_close(float(p["maxRadiusM"]), rmax, 1e-3), f"precision.maxRadiusM {p['maxRadiusM']} != {rmax:.1f}", "coordinate.json")
    rep.check(_close(float(p["curvatureDropM"]), curvature_drop_m(rmax), 2e-2), "precision.curvatureDropM != r^2/(2R)",
              "coordinate.json")
    rep.check(rmax <= 10000.0, f"horizontal radius {rmax:.0f} m > 10 km: split into zones", "coordinate.json", warn=True)


@rule("V-C-10", "semantic", "W")
def c10(w: WorldView, rep: Report):
    ulp = float(w.coordinate["precision"]["float32UlpMm"])
    rep.check(ulp < 1.0, f"float32 ULP {ulp:.3f} mm >= 1 mm: split world or move origin", "coordinate.json", warn=True)


@rule("V-C-11", "semantic")
def c11(w: WorldView, rep: Report):
    c = w.coordinate
    if c["anchor"]["kind"] == "synthetic":
        rep.check(c["scaleStatus"] not in ("rtk", "survey"), "synthetic anchor with rtk/survey scaleStatus", "coordinate.json")


@rule("V-C-12", "semantic")
def c12(w: WorldView, rep: Report):
    a = w.coordinate["anchor"]
    if a["kind"] == "synthetic":
        rep.check(str(a.get("label", "")).startswith("illustrative:"), "synthetic anchor label must start with 'illustrative:'",
                  "coordinate.json")


@rule("V-C-13", "semantic", "E/W")
def c13(w: WorldView, rep: Report):
    g = w.coordinate["ground"]
    href = (g.get("dtm") or {}).get("href")
    rep.check(bool(href) and w.exists(href), f"ground.dtm.href missing: {href}", "coordinate.json")
    rep.check(abs(float(g["zM"])) < 50.0, "ground.zM far from 0: origin z should be the DTM median", "coordinate.json", warn=True)


@rule("V-C-14", "semantic")
def c14(w: WorldView, rep: Report):
    lo, hi = w.extent
    rep.check(bool(np.all(lo <= hi)), "extent.min > extent.max", "coordinate.json")


# ================================================================ V-W 清单


@rule("V-W-01", "structure")
def w01(w: WorldView, rep: Report):
    if not w.exists("world.json"):
        rep.error("world.json missing", "world.json")
        return
    try:
        doc = w.world
    except Skip as e:
        rep.error(f"world.json unreadable: {e}", "world.json")
        return
    for e in schema_errors(doc, "world.schema.json"):
        rep.error(f"schema {e}", "world.json")
    try:
        c = w.coordinate
    except Skip as e:
        rep.error(f"coordinate.json unreadable: {e}", "coordinate.json")
        return
    for e in schema_errors(c, "coordinate.schema.json"):
        rep.error(f"schema {e}", "coordinate.json")


@rule("V-W-02", "semantic", "E/W")
def w02(w: WorldView, rep: Report):
    name = w.dir.resolve().name
    wid = str(w.world.get("id"))
    ok = name == wid or (w.staging and name.startswith(wid + "-"))       # staging 目录名为 <id>-<nonce>
    rep.check(ok, f"directory name {name!r} != id {wid!r}", "world.json", warn=w.staging)


@rule("V-W-03", "semantic")
def w03(w: WorldView, rep: Report):
    rep.check(w.coordinate.get("worldId") == w.world.get("id"), "coordinate.worldId != world.id", "coordinate.json")


@rule("V-W-04", "semantic")
def w04(w: WorldView, rep: Report):
    rep.check(w.world.get("scaleStatus") == w.coordinate.get("scaleStatus"), "scaleStatus differs from coordinate.json", "world.json")


@rule("V-W-05", "semantic")
def w05(w: WorldView, rep: Report):
    lo, hi = w.extent
    b = w.world["bounds"]
    ok = np.allclose(b["min"], lo, atol=0.01, rtol=0) and np.allclose(b["max"], hi, atol=0.01, rtol=0)
    rep.check(bool(ok), "bounds != coordinate.extent (tolerance 0.01 m)", "world.json")


@rule("V-W-06", "semantic")
def w06(w: WorldView, rep: Report):
    ids = [L.get("id") for L in w.world.get("layers", [])]
    rep.check(len(ids) == len(set(ids)), "duplicate layer ids", "world.json")
    for L in w.world.get("layers", []):
        if L.get("status") == "ready":
            rep.check(w.exists(L["href"]), f"layer {L['id']}: href missing {L['href']}", "world.json")
    for lid, typ, role, fmt, href in REQUIRED_LAYERS:
        L = w.layers.get(lid)
        if L is None:
            rep.error(f"required layer {lid} missing", "world.json")
            continue
        ok = L.get("type") == typ and L.get("role") == role and L.get("format") == fmt and L.get("href") == href
        rep.check(ok, f"required layer {lid} has wrong type/role/format/href", "world.json")
        rep.check(L.get("status") == "ready", f"required layer {lid} is not ready", "world.json")


@rule("V-W-07", "semantic")
def w07(w: WorldView, rep: Report):
    cubes = [(np.asarray(r["cubeMin"], float), float(r["cubeSize"])) for r in w.pc_layer.get("roots", [])]
    for i in range(len(cubes)):
        for j in range(i + 1, len(cubes)):
            (a0, s0), (a1, s1) = cubes[i], cubes[j]
            ov = np.minimum(a0 + s0, a1 + s1) - np.maximum(a0, a1)
            rep.check(not bool(np.all(ov > 1e-3)), f"roots {i} and {j} overlap", "world.json")


@rule("V-W-08", "semantic")
def w08(w: WorldView, rep: Report):
    for rv in w.roots:
        if rv.metadata is None:
            continue
        bb = rv.metadata["boundingBox"]
        r = rv.entry
        ok = np.allclose(bb["min"], r["cubeMin"], atol=1e-6, rtol=0) and _close(float(r["cubeSize"]), bb["max"][0] - bb["min"][0], 1e-9)
        rep.check(bool(ok), "roots[] cubeMin/cubeSize != metadata.boundingBox", f"root {r.get('name')}")


@rule("V-W-09", "semantic")
def w09(w: WorldView, rep: Report):
    total = 0
    for rv in w.roots:
        r = rv.entry
        total += int(r.get("points", 0))
        if rv.metadata is None:
            continue
        rep.check(r["points"] == rv.metadata["points"] and r["depth"] == rv.metadata["hierarchy"]["depth"],
                  "roots[] points/depth != metadata", f"root {r.get('name')}")
    rep.check(total == w.pc_layer.get("points"), f"sum(roots.points) {total} != layer.points {w.pc_layer.get('points')}", "world.json")


@rule("V-W-10", "semantic")
def w10(w: WorldView, rep: Report):
    pts, byt, per = _rule_g(w)
    for rv, L in per:
        _lp, lbe, _ = _levels(rv)
        rep.check(rv.entry["firstScreenBytes"] == lbe[L], f"firstScreenBytes {rv.entry['firstScreenBytes']} != rule G {lbe[L]}",
                  f"root {rv.entry.get('name')}")
    f = w.world["lod"]["firstScreen"]
    rep.check(f["points"] == pts and f["bytes"] == byt and f["requests"] == len(w.roots),
              f"lod.firstScreen {f} != rule G ({pts}, {byt}, {len(w.roots)})", "world.json")
    rep.info["first_screen"] = [pts, byt]


def _scan_content(w: WorldView) -> dict[str, int]:
    out = {}
    for dp, dns, fns in os.walk(w.dir):
        dns[:] = [d for d in dns if not d.startswith(".")]
        for fn in fns:
            p = Path(dp) / fn
            rel = p.relative_to(w.dir).as_posix()
            if rel in NON_CONTENT or rel.startswith(NON_CONTENT_DIRS) or fn.startswith("."):
                continue
            out[rel] = p.stat().st_size
    return out


@rule("V-W-11", "semantic")
def w11(w: WorldView, rep: Report):
    files = w.world.get("files")
    if files is None:
        rep.error("files[] missing (generator must write it)", "world.json")
        return
    listed = {f["path"]: f for f in files}
    actual = _scan_content(w)
    missing = sorted(set(listed) - set(actual))
    extra = sorted(set(actual) - set(listed))
    rep.check(not missing, f"files[] lists missing files: {missing[:5]}", "world.json")
    rep.check(not extra, f"content files not listed in files[]: {extra[:5]}", "world.json")
    bad = [p for p in listed if p in actual and listed[p].get("bytes") is not None and listed[p]["bytes"] != actual[p]]
    rep.check(not bad, f"files[] byte size mismatch: {bad[:5]}", "world.json")
    order = [f["path"].encode("utf-8") for f in files]
    rep.check(order == sorted(order), "files[] not sorted by path", "world.json")
    cv = content_version(files)
    rep.check(cv == w.world.get("contentVersion"), f"contentVersion {w.world.get('contentVersion')} != derived {cv}", "world.json")
    # 每个内容文件必须被 layers[]、coordinate.json 或其他清单引用（16 §3.1 规则 2）
    refs = {"coordinate.json"}
    for L in w.world.get("layers", []):
        refs.add(L["href"])
    unref = [p for p in actual if not any(p == h or (h.endswith("/") and p.startswith(h)) or _same_dir_sidecar(p, h)
                                          for h in refs)]
    rep.check(not unref, f"content files not referenced by any layer: {unref[:5]}", "world.json")


def _same_dir_sidecar(p: str, href: str) -> bool:
    """图层 href 指向 sidecar（grid、source.json）时，同目录中由该 sidecar 引用的数据文件视为已登记。"""
    if href.endswith("/"):
        return False
    d = href.rsplit("/", 1)[0] + "/" if "/" in href else ""
    if not p.startswith(d):
        return False
    stem = href.rsplit("/", 1)[-1]
    if stem == "source.json":
        return True
    base = stem.rsplit(".", 1)[0]
    rest = p[len(d):]
    return "/" not in rest and rest.rsplit(".", 1)[0] == base


@rule("V-W-12", "semantic")
def w12(w: WorldView, rep: Report):
    if "builtin" not in (w.world.get("tags") or []):
        return
    d = w.world.get("dataset") or {}
    for k in ("name", "version", "url", "citation", "license"):
        rep.check(bool(str(d.get(k) or "").strip()), f"dataset.{k} is empty", "world.json")
    sf = d.get("sourceFiles") or []
    rep.check(bool(sf) and all(re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))) for f in sf),
              "dataset.sourceFiles[].sha256 missing", "world.json")


@rule("V-W-13", "semantic", "W")
def w13(w: WorldView, rep: Report):
    pts, byt, _ = _rule_g(w)
    rep.check(pts <= 450_000 and byt <= 8 * 2**20, f"world first screen {pts} points / {byt / 2**20:.1f} MiB too large",
              "world.json", warn=True)


@rule("V-W-14", "semantic")
def w14(w: WorldView, rep: Report):
    rep.check(w.world["coordinate"].get("sha256") == w.coordinate_sha, "world.coordinate.sha256 != sha256(coordinate.json)",
              "world.json")


@rule("V-W-15", "semantic", "W")
def w15(w: WorldView, rep: Report):
    wj = w.world
    for k, present in (("lod.budgets", "budgets" in wj.get("lod", {})), ("render.pointSizeK", "pointSizeK" in wj.get("render", {})),
                       ("render.edl", "edl" in wj.get("render", {}))):
        rep.check(not present, f"{k} is deprecated and must not be written", "world.json", warn=True)


# ================================================================ V-P 点云容器


def _roots_md(w: WorldView):
    for rv in w.roots:
        if rv.metadata is not None:
            yield rv, rv.metadata, f"root {rv.entry.get('name')}"


@rule("V-P-01", "structure")
def p01(w: WorldView, rep: Report):
    for rv in w.roots:
        where = f"root {rv.entry.get('name')}"
        if rv.metadata is None:
            rep.error(f"metadata.json unreadable: {rv.md_error}", where)
            continue
        for e in schema_errors(rv.metadata, "pointcloud-metadata.schema.json"):
            rep.error(f"schema {e}", where)


@rule("V-P-02", "semantic")
def p02(w: WorldView, rep: Report):
    for _rv, md, where in _roots_md(w):
        ext = np.asarray(md["boundingBox"]["max"]) - np.asarray(md["boundingBox"]["min"])
        rep.check(bool(np.allclose(ext, ext.max(), rtol=1e-9, atol=1e-6)), f"boundingBox is not a cube: {ext.tolist()}", where)


@rule("V-P-03", "semantic")
def p03(w: WorldView, rep: Report):
    for _rv, md, where in _roots_md(w):
        rep.check(bool(np.allclose(md["offset"], md["boundingBox"]["min"], atol=1e-9, rtol=0)), "offset != boundingBox.min", where)


@rule("V-P-04", "semantic")
def p04(w: WorldView, rep: Report):
    for _rv, md, where in _roots_md(w):
        a = md.get("anet") or {}
        size = float(np.max(np.asarray(md["boundingBox"]["max"]) - np.asarray(md["boundingBox"]["min"])))
        G = (a.get("sampling") or {}).get("G")
        if G:
            rep.check(_close(float(md["spacing"]), size / G, 1e-9), f"spacing {md['spacing']} != cube/G {size / G}", where)


@rule("V-P-05", "semantic")
def p05(w: WorldView, rep: Report):
    for rv, md, where in _roots_md(w):
        if rv.hier is None:
            rep.error(f"hierarchy.bin unreadable: {rv.parse_error}", where)
            continue
        rep.check(len(rv.hier) % REC == 0, f"hierarchy.bin size {len(rv.hier)} not a multiple of 22", where)
        rep.check(int(md["hierarchy"]["firstChunkSize"]) <= len(rv.hier), "firstChunkSize > hierarchy.bin size", where)


@rule("V-P-06", "semantic")
def p06(w: WorldView, rep: Report):
    for rv, _md, where in _roots_md(w):
        if rv.hier is None:
            continue
        if rv.parse_error:
            rep.error(f"hierarchy parse failed: {rv.parse_error}", where)
            continue
        covered = sum(s for _, s in rv.chunks)
        rep.check(covered == len(rv.hier), f"hierarchy chunks cover {covered} of {len(rv.hier)} bytes (orphan records)", where)
        names = [n.name for n in rv.real]
        rep.check(len(names) == len(set(names)), "duplicate node records", where)


def _parsed(w: WorldView):
    for rv, md, where in _roots_md(w):
        if rv.records is not None and rv.real:
            yield rv, md, where


@rule("V-P-07", "semantic")
def p07(w: WorldView, rep: Report):
    for rv, md, where in _parsed(w):
        s = sum(n.n for n in rv.real)
        rep.check(s == md["points"], f"sum(numPoints)={s} != points={md['points']}", where)


@rule("V-P-08", "semantic")
def p08(w: WorldView, rep: Report):
    for rv, md, where in _parsed(w):
        d = max(n.level for n in rv.real)
        rep.check(d == md["hierarchy"]["depth"], f"hierarchy.depth {md['hierarchy']['depth']} != parsed {d}", where)


@rule("V-P-09", "semantic")
def p09(w: WorldView, rep: Report):
    for rv, _md, where in _parsed(w):
        for n in rv.real:
            if n.type == NORMAL and n.mask == 0:
                rep.error(f"{n.name}: NORMAL with empty childMask", where)
                break
            if n.type == LEAF and n.mask != 0:
                rep.error(f"{n.name}: LEAF with childMask", where)
                break


@rule("V-P-10", "semantic")
def p10(w: WorldView, rep: Report):
    for rv, _md, where in _parsed(w):
        bad = [n.name for n in rv.real if n.n == 0 and n.size != 0]
        rep.check(not bad, f"empty nodes with byteSize != 0: {bad[:5]}", where)


@rule("V-P-11", "semantic")
def p11(w: WorldView, rep: Report):
    for rv, _md, where in _parsed(w):
        pos = 0
        bad = 0
        for n in sorted(rv.real, key=lambda n: (n.level, n.name)):
            if n.n:
                if n.off != pos:
                    bad += 1
                pos = n.off + n.size
        rep.check(bad == 0, f"{bad} node payloads not contiguous in level-major BFS order", where)
        rep.check(pos == rv.octree_size, f"payload end {pos} != octree.bin size {rv.octree_size}", where)


@rule("V-P-12", "semantic")
def p12(w: WorldView, rep: Report):
    for rv, md, where in _parsed(w):
        a = md.get("anet") or {}
        if a.get("compression") != "none":
            continue
        bpp = int(a.get("bytesPerPoint", 12))
        for n in rv.real:
            if n.n and n.size != n.n * bpp:
                rep.error(f"{n.name}: byteSize {n.size} != numPoints*{bpp}", where)
                break
            if n.n and n.off % 4:
                rep.error(f"{n.name}: payload not 4-byte aligned", where)
                break


@rule("V-P-13", "semantic")
def p13(w: WorldView, rep: Report):
    for _rv, md, where in _roots_md(w):
        a = md.get("anet") or {}
        streams = a.get("streams") or []
        rep.check(a.get("bytesPerPoint") == 4 * (len(streams) + 1), f"bytesPerPoint {a.get('bytesPerPoint')} inconsistent with {streams}", where)
        rep.check((a.get("ext") is None) == ("ext" not in streams), "anet.ext must be null iff 'ext' not in streams", where)


@rule("V-P-14", "semantic")
def p14(w: WorldView, rep: Report):
    for rv, md, where in _parsed(w):
        a = md["anet"]
        lp, lbe, depth = _levels(rv)
        nodes = [0] * (depth + 1)
        for n in rv.real:
            nodes[n.level] += 1
        rep.check(a["levelsByteEnd"] == lbe, f"levelsByteEnd {a['levelsByteEnd']} != derived {lbe}", where)
        rep.check(a["levelsPoints"] == lp, f"levelsPoints {a['levelsPoints']} != derived {lp}", where)
        rep.check(a["levelsNodes"] == nodes, f"levelsNodes {a['levelsNodes']} != derived {nodes}", where)
        rep.check(a["nodeCount"] == len(rv.real), f"nodeCount {a['nodeCount']} != {len(rv.real)}", where)


@rule("V-P-15", "semantic", "E/W")
def p15(w: WorldView, rep: Report):
    k = len(w.roots)
    for rv, md, where in _parsed(w):
        a = md["anet"]
        lp, _lbe, depth = _levels(rv)
        fsl = int(a["firstScreenLevel"])
        if not rep.check(fsl <= depth, "firstScreenLevel > depth", where):
            continue
        rep.check(fsl == rule_g_level(lp, depth, k), f"firstScreenLevel {fsl} does not follow rule G", where, warn=True)


@rule("V-P-16", "semantic")
def p16(w: WorldView, rep: Report):
    lo, hi = w.extent
    for _rv, md, where in _roots_md(w):
        tb = md["anet"]["tightBounds"]
        tmin, tmax = np.asarray(tb["min"]), np.asarray(tb["max"])
        bmin, bmax = np.asarray(md["boundingBox"]["min"]), np.asarray(md["boundingBox"]["max"])
        rep.check(bool(np.all(tmin >= bmin - 1e-6) and np.all(tmax <= bmax + 1e-6)), "tightBounds outside the cube", where)
        rep.check(bool(np.all(tmin >= lo - 0.01) and np.all(tmax <= hi + 0.01)), "tightBounds outside coordinate.extent", where)


@rule("V-P-17", "semantic")
def p17(w: WorldView, rep: Report):
    for rv, md, where in _parsed(w):
        a = md["anet"]
        if not a.get("hierarchyExt"):
            continue
        if rv.ext_bytes is None:
            rep.error("hierarchy_ext.bin missing", where)
            continue
        nrec = len(rv.hier) // REC
        if not rep.check(len(rv.ext_bytes) == nrec * EXT_REC, f"hierarchy_ext.bin {len(rv.ext_bytes)} B != records*12 = {nrec * EXT_REC}", where):
            continue
        e = rv.ext
        rep.check(bool(np.all(e[:, :3] <= e[:, 3:])), "hierarchy_ext min > max", where)
        root = next((n for n in rv.real if n.name == "r"), None)
        if root is None:
            continue
        bmin = np.asarray(md["boundingBox"]["min"], np.float64)
        size = float(np.max(np.asarray(md["boundingBox"]["max"]) - bmin))
        r = e[root.rec].astype(np.float64)
        rmin = bmin + r[:3] / 65535 * size
        rmax = bmin + r[3:] / 65535 * size
        q = size / 65535 * 1.01
        tb = a["tightBounds"]
        ok = np.all(np.abs(rmin - np.asarray(tb["min"])) <= q) and np.all(np.abs(rmax - np.asarray(tb["max"])) <= q)
        rep.check(bool(ok), "root hierarchy_ext box differs from tightBounds by more than one quantisation step", where)


@rule("V-P-18", "semantic")
def p18(w: WorldView, rep: Report):
    nc = w.n_classes
    for _rv, md, where in _roots_md(w):
        h = md["anet"]["stats"]["classHistogram"]
        rep.check(sum(h.values()) == md["points"], f"classHistogram sums to {sum(h.values())} != points {md['points']}", where)
        rep.check(all(int(k) < nc for k in h), f"class index >= {nc} in classHistogram", where)


@rule("V-P-19", "semantic")
def p19(w: WorldView, rep: Report):
    for _rv, md, where in _roots_md(w):
        s = md["anet"]["stats"]
        rep.check(s["zP1"] <= s["zP99"], "zP1 > zP99", where)


@rule("V-P-20", "semantic")
def p20(w: WorldView, rep: Report):
    for _rv, md, where in _roots_md(w):
        root = md["anet"].get("root") or {"forestIndex": 0, "forestSize": 1}
        rep.check(root["forestSize"] == len(w.roots), f"anet.root.forestSize {root['forestSize']} != len(roots) {len(w.roots)}", where)


# ================================================================ V-D deep


@dataclass
class _DeepState:
    hist: np.ndarray
    bad_ext: int = 0
    bad_n: int = 0
    zero_n: int = 0
    total: int = 0
    reserved: int = 0
    read_ok: bool = True


@functools.cache
def _oct16_bad_codes() -> np.ndarray:
    """解码后不是单位向量（|‖n‖ − 1| > 1e-6）的非零 oct16 码。"""
    codes = np.arange(65536, dtype=np.uint16)
    bad = ~(np.abs(row_norm3(oct16_decode(codes)) - 1) <= 1e-6)
    bad[0] = False
    return np.flatnonzero(bad)


def _deep(w: WorldView) -> dict[str, _DeepState]:
    cache = w.__dict__.setdefault("_deep_cache", {})
    if cache:
        return cache
    for rv, md, where in _parsed(w):
        a = md["anet"]
        bpp = int(a["bytesPerPoint"])
        st = _DeepState(hist=np.zeros(256, np.int64))
        try:
            data = (rv.dir / "octree.bin").read_bytes()
        except OSError:
            st.read_ok = False
            cache[where] = st
            continue
        ws = []
        for n in rv.real:
            if not n.n:
                continue
            buf = data[n.off:n.off + n.size]
            if a["compression"] == "gzip":
                try:
                    buf = gzip.decompress(buf)
                except (OSError, EOFError, zlib.error):
                    st.read_ok = False
                    continue
            if len(buf) != n.n * bpp:
                st.read_ok = False
                continue
            cnt = n.n
            pos = np.frombuffer(buf, "<u2", 4 * cnt).reshape(cnt, 4)
            col = np.frombuffer(buf, np.uint8, 4 * cnt, 8 * cnt).reshape(cnt, 4)
            st.hist += np.bincount(col[:, 3], minlength=256)
            if rv.ext is not None and n.rec < len(rv.ext):
                e = rv.ext[n.rec]
                st.bad_ext += int(np.any(pos[:, :3] < e[:3]) or np.any(pos[:, :3] > e[3:]))
            if a["pos"]["w"] == "oct16":
                ws.append(pos[:, 3])
            st.total += cnt
        if ws:                                   # oct16 只有 65536 种码：按码直方图查表，等价于逐点解码
            hw = np.bincount(np.concatenate(ws), minlength=65536)
            st.zero_n = int(hw[0])
            st.bad_n = int(hw[_oct16_bad_codes()].sum())
        st.reserved = int(st.hist[15]) if w.n_classes > 15 else 0
        cache[where] = st
    return cache


@rule("V-D-01", "deep")
def d01(w: WorldView, rep: Report):
    for where, st in _deep(w).items():
        rep.check(st.read_ok, "node payload unreadable, gzip member invalid or inflated size != n*bpp", where)


@rule("V-D-02", "deep")
def d02(w: WorldView, rep: Report):
    for where, st in _deep(w).items():
        rep.check(st.bad_ext == 0, f"{st.bad_ext} nodes have points outside their hierarchy_ext box", where)


@rule("V-D-03", "deep")
def d03(w: WorldView, rep: Report):
    for where, st in _deep(w).items():
        rep.check(st.bad_n == 0, f"{st.bad_n} normals decode to non-unit vectors", where)


@rule("V-D-04", "deep", "W")
def d04(w: WorldView, rep: Report):
    for where, st in _deep(w).items():
        rep.check(st.zero_n <= 0.01 * max(st.total, 1), f"{st.zero_n} points without normal (w == 0) in an oct16 cloud", where, warn=True)


@rule("V-D-05", "deep")
def d05(w: WorldView, rep: Report):
    nc = w.n_classes
    for where, st in _deep(w).items():
        over = int(st.hist[nc:].sum())
        rep.check(over == 0, f"{over} points with class index >= {nc}", where)
        rep.check(st.reserved == 0, f"{st.reserved} points use the reserved class 15", where)


@rule("V-D-06", "deep")
def d06(w: WorldView, rep: Report):
    mds = {f"root {rv.entry.get('name')}": md for rv, md, _ in _parsed(w)}
    for where, st in _deep(w).items():
        derived = {str(i): int(v) for i, v in enumerate(st.hist) if v}
        rep.check(derived == mds[where]["anet"]["stats"]["classHistogram"], "classHistogram != decoded class bytes", where)


@rule("V-D-07", "deep")
def d07(w: WorldView, rep: Report):
    bad = []
    for f in w.world.get("files") or []:
        p = w.dir / f["path"]
        if p.exists() and sha256_file(p) != f.get("sha256"):
            bad.append(f["path"])
    rep.check(not bad, f"sha256 mismatch: {bad[:5]}", "world.json")


# ================================================================ V-K 码表


@rule("V-K-01", "structure")
def k01(w: WorldView, rep: Report):
    try:
        ct = w.class_table
    except Skip as e:
        rep.error(f"class table unreadable: {e}", "semantic/anet-classes@1.json")
        return
    for e in schema_errors(ct, "class-table.schema.json"):
        rep.error(f"schema {e}", "semantic/anet-classes@1.json")


@rule("V-K-02", "semantic")
def k02(w: WorldView, rep: Report):
    idx = [c["index"] for c in w.class_table["classes"]]
    rep.check(idx == list(range(len(idx))), "class indices must be 0..n-1 in order", "semantic/anet-classes@1.json")


@rule("V-K-03", "semantic")
def k03(w: WorldView, rep: Report):
    las = [c["lasCode"] for c in w.class_table["classes"] if c.get("lasCode") is not None]
    rep.check(len(las) == len(set(las)), "duplicate lasCode", "semantic/anet-classes@1.json")


@rule("V-K-04", "semantic")
def k04(w: WorldView, rep: Report):
    p = w.dir / "semantic/anet-classes@1.json"
    if not p.exists():
        raise Skip("class table missing")
    rep.check(p.read_bytes() == classes_source_path().read_bytes(),
              "semantic/anet-classes@1.json differs from packages/contracts/classes/anet-classes-v1.json", str(p.name))


# ================================================================ V-G 栅格


def _terrain_layers(w: WorldView) -> list[dict]:
    return [L for L in w.world.get("layers", []) if L.get("type") == "terrain" and w.exists(L.get("href", ""))]


@rule("V-G-01", "structure")
def g01(w: WorldView, rep: Report):
    for L in _terrain_layers(w):
        try:
            sc = w.load_json(L["href"])
        except Skip as e:
            rep.error(f"sidecar unreadable: {e}", L["href"])
            continue
        for e in schema_errors(sc, "grid.schema.json"):
            rep.error(f"schema {e}", L["href"])


@rule("V-G-02", "semantic")
def g02(w: WorldView, rep: Report):
    for L in _terrain_layers(w):
        sc = w.load_json(L["href"])
        raw = (w.dir / L["href"]).parent / sc["href"]
        exp = int(sc["width"]) * int(sc["height"]) * ITEMSIZE[sc["dtype"]]
        rep.check(raw.exists() and raw.stat().st_size == exp, f"raster bytes != width*height*itemsize ({exp})", L["href"])


@rule("V-G-03", "semantic")
def g03(w: WorldView, rep: Report):
    lo, hi = w.extent
    for L in _terrain_layers(w):
        sc = w.load_json(L["href"])
        ox, oy = sc["originXY"]
        c = float(sc["cellM"])
        tol = 1e-3                                       # extent 按 1 mm 取整
        ok = ox <= lo[0] + tol and oy <= lo[1] + tol and ox + sc["width"] * c >= hi[0] - tol and oy + sc["height"] * c >= hi[1] - tol
        rep.check(bool(ok), "raster does not cover the horizontal extent", L["href"])


@rule("V-G-04", "semantic")
def g04(w: WorldView, rep: Report):
    for L in _terrain_layers(w):
        sc, a = w.grid(L["id"])
        if sc.get("valueFrame") == "world-z-m":
            rep.check(sc.get("dtype") != "float16", "world-z-m raster must not be float16", L["href"])
        if a is not None and sc.get("dtype", "").startswith("float"):
            rep.check(not bool(np.isnan(a).any()), "float raster contains NaN after filling", L["href"])


@rule("V-G-05", "deep")
def g05(w: WorldView, rep: Report):
    dsc, dsm = w.grid("terrain.dsm")
    tsc, dtm = w.grid("terrain.dtm")
    if dsm is None or dtm is None:
        raise Skip("raster unreadable")
    cnt = None
    if "terrain.dsm-n" in w.layers:
        _nsc, cnt = w.grid("terrain.dsm-n")
        if cnt is not None and cnt.shape != dsm.shape:
            rep.error("dsm_2m_n shape != dsm_2m shape", "geometry/terrain/dsm_2m_n.json")
            cnt = None
    H, W = dsm.shape
    src = Grid(dtm, (float(tsc["originXY"][0]), float(tsc["originXY"][1])), float(tsc["cellM"]))
    org = (float(dsc["originXY"][0]), float(dsc["originXY"][1]))
    bad = 0
    bad_n = 0
    for r0 in range(0, H, 512):
        r1 = min(H, r0 + 512)
        t = bilinear_on_centres(src, org, float(dsc["cellM"]), H, W, rows=(r0, r1))
        blk = dsm[r0:r1].astype(np.float64)
        bad += int((blk < t - 0.01).sum())
        if cnt is not None:
            e = cnt[r0:r1] == 0
            bad_n += int((np.abs(blk[e] - t[e]) > 0.01).sum())
    rep.check(bad == 0, f"{bad} DSM cells below DTM - 0.01 m", "geometry/terrain/dsm_2m.json")
    if cnt is not None:
        rep.check(bad_n == 0, f"{bad_n} unobserved DSM cells (dsm_2m_n = 0) differ from the DTM fill", "geometry/terrain/dsm_2m_n.json")


@rule("V-G-06", "deep")
def g06(w: WorldView, rep: Report):
    _sc, dsm = w.grid("terrain.dsm")
    if dsm is None:
        raise Skip("dsm unreadable")
    zmax = float(w.extent[1][2])
    m = float(dsm.max())
    rep.check(abs(m - zmax) <= 1e-3, f"max(dsm) {m:.4f} != extent.max.z {zmax:.4f}", "geometry/terrain/dsm_2m.json")


# ================================================================ V-Z 区域


@rule("V-Z-01", "structure")
def z01(w: WorldView, rep: Report):
    try:
        fc = w.load_json(w.layer("semantic.zones")["href"])
    except Skip as e:
        rep.error(f"zones.geojson unreadable: {e}", "semantic/zones.geojson")
        return
    for e in schema_errors(fc, "zones.schema.json"):
        rep.error(f"schema {e}", "semantic/zones.geojson")


def _zones(w: WorldView) -> dict:
    return w.load_json(w.layer("semantic.zones")["href"])


@rule("V-Z-02", "semantic")
def z02(w: WorldView, rep: Report):
    fc = _zones(w)
    borders = [f for f in fc["features"] if f["properties"]["kind"] == "border"]
    if not rep.check(len(borders) == 1, f"{len(borders)} border features (exactly one required)", "semantic/zones.geojson"):
        return
    b = borders[0]
    params = (w.world.get("generator") or {}).get("params") or {}
    inset = float(params.get("borderInsetM", 20))
    head = float(params.get("borderHeadroomM", 50))
    bmin, bmax = w.world["bounds"]["min"], w.world["bounds"]["max"]
    ring = np.asarray(b["geometry"]["coordinates"][0], np.float64)
    exp = np.array([[bmin[0] + inset, bmin[1] + inset], [bmax[0] - inset, bmin[1] + inset], [bmax[0] - inset, bmax[1] - inset],
                    [bmin[0] + inset, bmax[1] - inset], [bmin[0] + inset, bmin[1] + inset]])
    rep.check(ring.shape == exp.shape and bool(np.allclose(ring, exp, atol=0.01, rtol=0)),
              "border polygon != bounds inset by borderInsetM", "semantic/zones.geojson")
    _sc, dsm = w.grid("terrain.dsm")
    if dsm is not None:
        want = float(dsm.max()) + head
        got = b["properties"].get("max_z_m")
        rep.check(got is not None and abs(got - want) <= 0.01, f"border max_z_m {got} != max(dsm) + {head} = {want:.2f}",
                  "semantic/zones.geojson")
    rep.check(b["properties"].get("min_z_m") is None, "border min_z_m must be null", "semantic/zones.geojson")


@rule("V-Z-03", "semantic")
def z03(w: WorldView, rep: Report):
    for rid, msg in check_zone_geometry(_zones(w)):
        if rid == "V-Z-03":
            rep.error(msg, "semantic/zones.geojson")


@rule("V-Z-04", "semantic")
def z04(w: WorldView, rep: Report):
    b = w.world["bounds"]
    for rid, msg in check_zone_geometry(_zones(w), b["min"], b["max"], tol=1e-6):
        if rid == "V-Z-04":
            rep.error(msg, "semantic/zones.geojson")


@rule("V-Z-05", "semantic")
def z05(w: WorldView, rep: Report):
    for rid, msg in check_zone_geometry(_zones(w)):
        if rid == "V-Z-05":
            rep.error(msg, "semantic/zones.geojson")


@rule("V-Z-06", "semantic")
def z06(w: WorldView, rep: Report):
    rep.check(_zones(w)["awr"].get("coordinate_sha256") == w.world["coordinate"]["sha256"],
              "zones awr.coordinate_sha256 != world coordinate.sha256", "semantic/zones.geojson")


# ================================================================ V-S 源点云


def _source(w: WorldView) -> tuple[dict, Path]:
    L = w.layer("pointcloud.source")
    return w.load_json(L["href"]), (w.dir / L["href"]).parent


@rule("V-S-01", "structure")
def s01(w: WorldView, rep: Report):
    try:
        sc, _ = _source(w)
    except Skip as e:
        rep.error(f"source.json unreadable: {e}", "geometry/pointcloud/source/source.json")
        return
    for e in schema_errors(sc, "pointcloud-source.schema.json"):
        rep.error(f"schema {e}", "geometry/pointcloud/source/source.json")


@rule("V-S-02", "semantic")
def s02(w: WorldView, rep: Report):
    sc, d = _source(w)
    rep.check(sc["count"] == w.pc_layer.get("points"), f"source count {sc['count']} != point cloud points {w.pc_layer.get('points')}",
              "source.json")
    names = [s["name"] for s in sc["streams"]]
    rep.check("xyz" in names and "class" in names, "xyz and class streams are required", "source.json")
    for s in sc["streams"]:
        p = d / s["href"]
        exp = int(sc["count"]) * int(s["components"]) * ITEMSIZE[s["dtype"]]
        rep.check(p.exists() and p.stat().st_size == exp, f"stream {s['name']} bytes != count*components*itemsize ({exp})", "source.json")


@rule("V-S-03", "deep")
def s03(w: WorldView, rep: Report):
    sc, d = _source(w)
    n = int(sc["count"])
    xyz = np.fromfile(d / "xyz.f32", "<f4")
    if xyz.size != 3 * n:
        raise Skip("xyz.f32 size mismatch (V-S-02)")
    P32 = xyz.reshape(n, 3)
    cm = np.asarray(sc["cube_min_m"], np.float64)
    size = float(sc["cube_size_m"])

    def codes(Q):
        q = ((Q - cm) / size * (1 << MORTON_B)).astype(np.int64)
        np.clip(q, 0, (1 << MORTON_B) - 1, out=q)
        return morton_codes(q)

    c = codes(P32.astype(np.float64))
    desc = np.flatnonzero(c[1:] < c[:-1])            # float32 取整可能让边界附近的点看似逆序
    bad = 0
    if desc.size:
        i = np.unique(np.r_[desc, desc + 1])
        Pi = P32[i].astype(np.float64)
        eps = np.maximum(np.spacing(np.abs(P32[i])).astype(np.float64), 1e-9) * 2
        lo = dict(zip(i.tolist(), codes(Pi - eps).tolist(), strict=True))
        hi = dict(zip(i.tolist(), codes(Pi + eps).tolist(), strict=True))
        bad = sum(1 for k in desc.tolist() if hi[k + 1] < lo[k])   # 取误差上下界仍逆序才判定为违例
    rep.check(bad == 0, f"{bad} points out of Morton order", "geometry/pointcloud/source/xyz.f32")


@rule("V-S-04", "semantic")
def s04(w: WorldView, rep: Report):
    sc, _ = _source(w)
    rep.check(sc.get("coordinate_sha256") == w.world["coordinate"]["sha256"], "source.json coordinate_sha256 != world coordinate.sha256",
              "source.json")


# ================================================================ V-E 环境


def _env(w: WorldView) -> dict:
    return w.load_json(w.layer("environment.config")["href"])


@rule("V-E-01", "semantic")
def e01(w: WorldView, rep: Report):
    want = "sha256:" + w.world["coordinate"]["sha256"]
    rep.check(_env(w).get("coordinate_hash") == want, "env.json coordinate_hash != world coordinate.sha256", "environment/env.json")
    man = w.dir / "environment/wind/l2/manifest.json"
    if man.exists():
        m = w.load_json("environment/wind/l2/manifest.json")
        rep.check(m.get("coordinate_hash") == want, "wind library coordinate_hash != world coordinate.sha256", str(man.name))


@rule("V-E-02", "semantic")
def e02(w: WorldView, rep: Report):
    ids = [p["id"] for p in json.loads(presets_path().read_text(encoding="utf-8"))["presets"]]
    rep.check(_env(w).get("default_preset") in ids, f"default_preset {_env(w).get('default_preset')!r} not in presets.json", "environment/env.json")


@rule("V-E-03", "structure")
def e03(w: WorldView, rep: Report):
    try:
        env = _env(w)
    except Skip as e:
        rep.error(f"env.json unreadable: {e}", "environment/env.json")
        return
    for e in schema_errors(env, "env_world.schema.json"):
        rep.error(f"schema {e}", "environment/env.json")


@rule("V-E-04", "semantic")
def e04(w: WorldView, rep: Report):
    for p in sorted(w.dir.rglob("*.awrv")):
        b = p.read_bytes()
        if len(b) < 64:
            rep.error("AWRV shorter than its 64-byte header", str(p.relative_to(w.dir)))
            continue
        magic, ver = b[:4], struct.unpack_from("<H", b, 4)[0]
        payload_bytes, crc = struct.unpack_from("<II", b, 56)
        rep.check(magic == b"AWRV" and ver == 1 and payload_bytes == len(b) - 64 and (zlib.crc32(b[64:]) & 0xFFFFFFFF) == crc,
                  "AWRV magic/version/payload_bytes/crc mismatch", str(p.relative_to(w.dir)))


@rule("V-E-05", "semantic")
def e05(w: WorldView, rep: Report):
    for p in sorted(w.dir.rglob("*.awsl")):
        b = p.read_bytes()
        if len(b) < 48 or b[:4] != b"AWSL" or struct.unpack_from("<H", b, 4)[0] != 1:
            rep.error("AWSL magic/version mismatch", str(p.relative_to(w.dir)))
            continue
        n_lines, n_verts = struct.unpack_from("<II", b, 8)
        off = np.frombuffer(b, "<u4", n_lines + 1, 48)
        seg = np.diff(off.astype(np.int64))
        rep.check(bool(np.all(seg >= 0)) and int(off[-1]) == n_verts and bool(np.all((seg >= 8) & (seg <= 64))),
                  "AWSL line_offsets invalid", str(p.relative_to(w.dir)))


@rule("V-E-06", "semantic")
def e06(w: WorldView, rep: Report):
    man = w.dir / "environment/wind/l2/manifest.json"
    if not man.exists():
        return
    m = w.load_json("environment/wind/l2/manifest.json")
    for href in [*(m.get("files") or []), *([m["solid"]] if m.get("solid") else [])]:
        rep.check((man.parent / href).exists(), f"wind library file missing: {href}", "environment/wind/l2/manifest.json")


# ================================================================ V-Q 报告


@rule("V-Q-01", "semantic")
def q01(w: WorldView, rep: Report):
    if w.staging:
        return
    try:
        r = w.load_json("qa/report.json")
    except Skip:
        rep.error("qa/report.json missing in a published package", "qa/report.json")
        return
    for e in schema_errors(r, "qa-report.schema.json"):
        rep.error(f"schema {e}", "qa/report.json")
    gates = r.get("gates") or []
    fail = any((not g["pass"]) and g["severity"] == "error" for g in gates)
    warn = any((not g["pass"]) and g["severity"] == "warn" for g in gates)
    rep.check(not (fail and r.get("status") != "fail"), "report status inconsistent with failed error gates", "qa/report.json")
    rep.check(not (r.get("status") == "pass" and warn), "report status pass with failed warn gates", "qa/report.json")


@rule("V-Q-02", "semantic")
def q02(w: WorldView, rep: Report):
    if w.staging:
        return
    try:
        r = w.load_json("qa/report.json")
    except Skip:
        return
    qa = w.world.get("qa") or {}
    rep.check(qa.get("status") == r.get("status"), f"world.json qa.status {qa.get('status')} != report status {r.get('status')}", "world.json")
    rep.check(r.get("status") != "fail", "a package with qa status fail is in the published directory", "qa/report.json")


# ---------------------------------------------------------------- 驱动


def is_world_dir(p: Path) -> bool:
    return p.is_dir() and bool(WORLD_ID_RE.match(p.name))


def validate_world(world_dir: Path, *, deep: bool = False, rules: set[str] | None = None, staging: bool = False) -> Report:
    """`staging=True` 时跳过 V-Q 组，V-W-02 降为告警（16 §15.3）。"""
    t0 = time.perf_counter()
    world_dir = Path(world_dir)
    rep = Report(path=str(world_dir), deep=deep, staging=staging)
    w = WorldView(world_dir, staging=staging)
    try:
        rep.world_id = w.world.get("id")
    except Skip:
        rep.world_id = None
    for rid, rd in RULES.items():
        if rules is not None and rid not in rules:
            continue
        if rd.layer == "deep" and not deep:
            continue
        rep.rules_checked += 1
        rep._rule = rid
        try:
            rd.fn(w, rep)
        except Skip:
            continue
        except (KeyError, TypeError, ValueError, IndexError, AttributeError, OSError) as e:
            rep.error(f"cannot evaluate: {type(e).__name__}: {e}", "")
    rep._rule = ""
    rep.seconds = time.perf_counter() - t0
    return rep


def validate_cli_json(reports: list[Report], skipped: list[str]) -> dict:
    return {"validator": VALIDATOR_NAME, "version": VALIDATOR_VERSION, "schema_version": "1.0.0",
            "results": [r.to_json() for r in reports], "skipped": skipped}


def rules_catalog() -> list[dict]:
    return [{"id": r.id, "layer": r.layer, "severity": r.severity} for r in RULES.values()]


def file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    return h.hexdigest()
