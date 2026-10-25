"""World Package v1 校验器（AWR-16 §15.3 的 84 条规则；M03-FR-047、FR-048、§6.11）与 D3 V-GLB 规则组（M22 §7.8 的 16 条）。

三层：结构（JSON Schema）、语义（跨文件一致性）、deep（逐节点解码、全部 sha256）。规则以
`@rule(id, layer, severity)` 注册，ID 稳定，供变异测试与 `qa/report.json` 使用。由 g03 `worldpkg_validate.py`
迁移并编号；坐标与精度复算调用 M02 `frames`（M03-FR-050）。V-GLB 规则以 `@glb_rule` 另表登记，只在 `profile = d3-glb`
的世界上于 D2 规则之后执行（M22-FR-042），D2 世界的规则表与结果不变。
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
    skipped_rules: dict[str, str] = field(default_factory=dict)   # 规则 id → 不适用原因（Skip）；V-GLB 结果登记用
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
GLB_RULES: dict[str, RuleDef] = {}     # V-GLB 规则组（M22 §7.8）：只对 profile = d3-glb 的世界执行，D2 的 84 条规则表不变


def rule(rid: str, layer: str = "semantic", severity: str = "E", registry: dict[str, RuleDef] | None = None):
    def deco(fn):
        (RULES if registry is None else registry)[rid] = RuleDef(rid, layer, severity, fn)
        return fn
    return deco


def glb_rule(rid: str, layer: str = "semantic", severity: str = "E"):
    return rule(rid, layer, severity, registry=GLB_RULES)


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
        self._cache: dict[Any, Any] = {}

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
    if w.world.get("profile") == "d3-glb":
        # D3 GLB 世界：DSM 为网格竖向采样最高命中（M22 §6.6.6），包围盒取网格顶点，采样不必落在最高顶点上，只要求不超出
        rep.check(m <= zmax + 1e-3, f"max(dsm) {m:.4f} > extent.max.z {zmax:.4f}", "geometry/terrain/dsm_2m.json")
        return
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
    if w.world.get("profile") != "d3-glb":       # d3-glb：border 为覆盖多边形内缩（M22 §6.6.7），由 V-GLB-12 校验
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


# ================================================================ V-GLB D3 GLB 世界（M22 §7.8；AWR-05 §5.1.3 第 13 行；D3-AC-02、24）
#
# 只对 `profile = d3-glb` 的世界生效，D2 世界整组跳过。本组不导入 D3 包（AWR-05 §4.8 导入方向）：竖向复算、GLB 解析、多边形
# 栅格化与占据砖块解码均在本文件以 numpy 实现。规则所需图层不存在时记为不适用（`Report.skipped_rules`），qa 报告 `glb.vglb`
# 逐条登记（`vglb_results`）。V-GLB-08 失败归原因码 602 WORLD_LICENSE_MISSING（`RULE_REASONS`）。

VGLB_SCALE_EVIDENCE = ("landmark", "assumed", "gnss", "lidar", "rtk", "survey")
VGLB_OSM_METHODS = ("osm-road-correlation", "osm-footprint-correlation", "control-points", "inherit-frame")
VGLB_DSM_CELLS = 10_000           # V-GLB-04 抽样格数（M22-AC-007）
VGLB_DSM_K = 4                    # 每格 4 × 4 采样（M22 §6.6.6）
VGLB_DSM_TOL_M = 0.01
VGLB_SEED = 20261012
VGLB_BOUNDS_TOL_M = 1e-3
VGLB_APPEARANCE_TOL_M = 0.01
VGLB_SNAP_WARN_M = 3.0            # M22 §6.8 吸附告警阈值
VGLB_SNAP_WARN_FRAC = 0.05
VGLB_COVER_IOU = 0.98
VGLB_COVER_CLOSING_CELLS = 2      # M22 §6.8 覆盖闭运算半径（格）
VGLB_RING_MAX = 1024
VGLB_OCC_SAMPLES = 10_000
VGLB_KM_DIAG_M = 1000.0           # 公里级判据（M22-FR-012）
VGLB_AGENTS = {"person": {"radius_m": 0.3, "height_m": 1.8, "max_climb_m": 0.4, "max_slope_deg": 35.0},
               "vehicle": {"radius_m": 1.2, "height_m": 2.0, "max_climb_m": 0.3, "max_slope_deg": 20.0}}
VGLB_RULE_IDS = tuple(f"V-GLB-{i:02d}" for i in range(1, 17))
RULE_REASONS = {"V-GLB-08": 602}
ODBL = "ODbL-1.0"
D3_SIDECAR_SHA = {"awr-trimesh@1": True, "gltf-glb@2": True, "geojson/awr-navgraph@1": True, "awr-anchors@1": True,
                  "geojson/awr-quality@1": True, "detour-navmesh@1": False, "awr-occ@1": False}   # True：格式规定必须写 coordinate_sha256
OCC_UNKNOWN, OCC_FREE, OCC_OCCUPIED, OCC_BELOW = 0, 1, 2, 3
OCC_IDX_DTYPE = np.dtype([("key", "<u8"), ("offset", "<u8"), ("length", "<u4"), ("flags", "u1")])


def _glb(w: WorldView) -> None:
    if w.world.get("profile") != "d3-glb":
        raise Skip("not a d3-glb world")


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-300)


def _layers_by(w: WorldView, *, fmt: str | None = None, type_: str | None = None) -> list[dict]:
    return [L for L in w.world.get("layers", []) if isinstance(L, dict) and (fmt is None or L.get("format") == fmt)
            and (type_ is None or L.get("type") == type_)]


def _sidecar_rel(L: dict) -> str:
    h = str(L.get("href", ""))
    return h + "index.json" if h.endswith("/") else h


def _qa_glb(w: WorldView) -> dict | None:
    try:
        r = w.load_json("qa/report.json")
    except Skip:
        return None
    g = r.get("glb") if isinstance(r, dict) else None
    return g if isinstance(g, dict) else None


def _trimesh(w: WorldView, L: dict) -> tuple[dict, np.ndarray, np.ndarray, np.ndarray]:
    """`awr-trimesh@1` 解码：(index.json, 世界坐标 float64 (n, 3), 全局顶点下标 int64 (m, 3), 每顶点所属瓦片 int64 (n,))。"""
    key = ("trimesh", L.get("href"))
    if key not in w._cache:
        rel = _sidecar_rel(L)
        doc = w.load_json(rel)
        base = (w.dir / rel).parent
        st = {s["name"]: s for s in doc.get("streams", [])}
        xyz = np.fromfile(base / st["xyz"]["href"], "<f4").reshape(-1, 3)
        tri = np.fromfile(base / st["tri"]["href"], "<u4").reshape(-1, 3)
        V = np.empty(xyz.shape, np.float64)
        F = np.empty(tri.shape, np.int64)
        tile_of = np.empty(len(xyz), np.int64)
        for t in doc["tiles"]:
            v0, nv, t0, nt = int(t["v0"]), int(t["nv"]), int(t["t0"]), int(t["nt"])
            V[v0:v0 + nv] = xyz[v0:v0 + nv].astype(np.float64) + np.asarray(t["origin_m"], np.float64)
            F[t0:t0 + nt] = tri[t0:t0 + nt].astype(np.int64) + v0
            tile_of[v0:v0 + nv] = int(t["id"])
        w._cache[key] = (doc, V, F, tile_of)
    return w._cache[key]


def vertical_hits(V: np.ndarray, F: np.ndarray, origin_xy: tuple[float, float], cell: float, rows: np.ndarray, cols: np.ndarray,
                  k: int = VGLB_DSM_K, *, max_pairs: int = 4_000_000) -> tuple[np.ndarray, np.ndarray]:
    """抽样格的竖向射线复算（M22 §6.6.6 同一采样位置与覆盖测试）：返回每格 (最高命中 z（无命中为 −inf），有命中的采样数)。

    采样点 `x = x0 + (j + 0.5) · cell / k`、`y = y0 + (i + 0.5) · cell / k`（j = c·k + a，i = r·k + b）；竖向射线命中等价于三角形在水平面
    上的覆盖测试（边函数 float64，含边界），竖直面与退化面（|2A| < 1e-12）不命中；z 由重心插值。与构建侧栅格化核逐式相同。
    """
    rows = np.asarray(rows, np.int64)
    cols = np.asarray(cols, np.int64)
    n = len(rows)
    top = np.full(n * k * k, -np.inf)
    hit = np.zeros(n * k * k, bool)
    if n == 0 or len(F) == 0:
        return top.reshape(n, k * k).max(1) if n else np.zeros(0), np.zeros(n, np.int64)
    ox, oy = float(origin_xy[0]), float(origin_xy[1])
    H, W = int(rows.max()) + 1, int(cols.max()) + 1
    ids = np.full((H, W), -1, np.int64)
    ids[rows, cols] = np.arange(n)
    integ = np.zeros((H + 1, W + 1), np.int64)
    integ[1:, 1:] = np.cumsum(np.cumsum(ids >= 0, 0), 1)
    P = V[F]                                                    # (m, 3, 3)
    c0 = np.floor((P[:, :, 0].min(1) - ox) / cell).astype(np.int64)
    c1 = np.floor((P[:, :, 0].max(1) - ox) / cell).astype(np.int64)
    r0 = np.floor((P[:, :, 1].min(1) - oy) / cell).astype(np.int64)
    r1 = np.floor((P[:, :, 1].max(1) - oy) / cell).astype(np.int64)
    c0, r0 = np.maximum(c0, 0), np.maximum(r0, 0)
    c1, r1 = np.minimum(c1, W - 1), np.minimum(r1, H - 1)
    ok = (c0 <= c1) & (r0 <= r1)
    cand = np.flatnonzero(ok)
    a, b, cc, d = r0[cand], c0[cand], r1[cand] + 1, c1[cand] + 1
    cnt = integ[cc, d] - integ[a, d] - integ[cc, b] + integ[a, b]
    cand = cand[cnt > 0]
    s = cell / k
    sub = np.arange(k * k)
    sa, sb = sub % k, sub // k                                    # a：列内序，b：行内序
    nc = (c1[cand] - c0[cand] + 1) * (r1[cand] - r0[cand] + 1)
    bounds = np.r_[0, np.cumsum(nc)]
    start = 0
    while start < len(cand):
        stop = int(np.searchsorted(bounds, bounds[start] + max_pairs, "right")) - 1
        stop = max(stop, start + 1)
        ch = cand[start:stop]
        m = nc[start:stop]
        t = np.repeat(ch, m)
        off = np.arange(int(m.sum())) - np.repeat(np.r_[0, np.cumsum(m)[:-1]], m)
        wid = np.repeat(c1[ch] - c0[ch] + 1, m)
        rr = np.repeat(r0[ch], m) + off // wid
        cq = np.repeat(c0[ch], m) + off % wid
        cid = ids[rr, cq]
        keep = cid >= 0
        t, rr, cq, cid = t[keep], rr[keep], cq[keep], cid[keep]
        start = stop
        if len(t) == 0:
            continue
        px = ox + ((cq[:, None] * k + sa[None, :]) + 0.5) * s
        py = oy + ((rr[:, None] * k + sb[None, :]) + 0.5) * s
        T = P[t]
        ax, ay, az = T[:, 0, 0][:, None], T[:, 0, 1][:, None], T[:, 0, 2][:, None]
        bx, by, bz = T[:, 1, 0][:, None], T[:, 1, 1][:, None], T[:, 1, 2][:, None]
        cx, cy, cz = T[:, 2, 0][:, None], T[:, 2, 1][:, None], T[:, 2, 2][:, None]
        A2 = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        w0 = (bx - px) * (cy - py) - (cx - px) * (by - py)
        w1 = (cx - px) * (ay - py) - (ax - px) * (cy - py)
        w2 = (ax - px) * (by - py) - (bx - px) * (ay - py)
        pos = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        neg = (w0 <= 0) & (w1 <= 0) & (w2 <= 0)
        inside = np.where(A2 > 0, pos, neg) & (np.abs(A2) >= 1e-12)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = (w0 * az + w1 * bz + w2 * cz) / A2
        si = (cid[:, None] * (k * k) + sub[None, :])[inside]
        np.maximum.at(top, si, z[inside])
        hit[si] = True
    return top.reshape(n, k * k).max(1), hit.reshape(n, k * k).sum(1)


def _poly_rings(geom: dict) -> list[list[np.ndarray]]:
    """GeoJSON Polygon / MultiPolygon → [[外环, 洞...], ...]（只取 E、N）。"""
    t = geom.get("type")
    polys = [geom["coordinates"]] if t == "Polygon" else list(geom.get("coordinates", [])) if t == "MultiPolygon" else []
    return [[np.asarray(r, np.float64)[:, :2] for r in p] for p in polys]


def rasterize_rings(rings: list[np.ndarray], origin_xy: tuple[float, float], cell: float, H: int, W: int) -> np.ndarray:
    """偶奇规则把一组环栅格化到格心（行 0 在南）。"""
    out = np.zeros((H, W), bool)
    if not rings:
        return out
    seg = np.concatenate([np.stack([r[:-1], r[1:]], 1) if np.allclose(r[0], r[-1]) else np.stack([r, np.roll(r, -1, 0)], 1)
                          for r in rings if len(r) >= 3])
    x0, y0, x1, y1 = seg[:, 0, 0], seg[:, 0, 1], seg[:, 1, 0], seg[:, 1, 1]
    cx = origin_xy[0] + (np.arange(W) + 0.5) * cell
    for r in range(H):
        y = origin_xy[1] + (r + 0.5) * cell
        m = (y0 <= y) != (y1 <= y)
        if not m.any():
            continue
        xs = np.sort(x0[m] + (y - y0[m]) * (x1[m] - x0[m]) / (y1[m] - y0[m]))
        inside = (np.searchsorted(xs, cx, "right") % 2) == 1
        out[r] = inside
    return out


def _points_in_rings(P: np.ndarray, rings: list[np.ndarray]) -> np.ndarray:
    P = np.asarray(P, np.float64).reshape(-1, 2)
    inside = np.zeros(len(P), bool)
    for r in rings:
        a, b = r, np.roll(r, -1, 0)
        for (xa, ya), (xb, yb) in zip(a, b, strict=True):
            m = (ya <= P[:, 1]) != (yb <= P[:, 1])
            if m.any():
                xi = xa + (P[m, 1] - ya) * (xb - xa) / (yb - ya)
                inside[np.flatnonzero(m)[P[m, 0] < xi]] ^= True
    return inside


def coverage_mask(nhit: np.ndarray, radius: int = VGLB_COVER_CLOSING_CELLS) -> np.ndarray:
    """`fill_holes(largest_component(binary_closing(dsm_2m_n > 0, disk(radius))))`（M22 §6.6.7 第 2 行）。"""
    from scipy import ndimage

    yy, xx = np.mgrid[-radius:radius + 1, -radius:radius + 1]
    disk = (xx * xx + yy * yy) <= radius * radius
    m = np.pad(np.asarray(nhit) > 0, radius + 1)
    m = ndimage.binary_closing(m, structure=disk)[radius + 1:-radius - 1, radius + 1:-radius - 1]
    lab, n = ndimage.label(m)
    if n > 1:
        sizes = np.bincount(lab.ravel())
        sizes[0] = 0
        m = lab == int(np.argmax(sizes))
    return ndimage.binary_fill_holes(m)


def _cover_rings(w: WorldView) -> list[np.ndarray] | None:
    Ls = _layers_by(w, fmt="geojson/awr-quality@1")
    if not Ls:
        return None
    fc = w.load_json(Ls[0]["href"])
    rings: list[np.ndarray] = []
    for f in fc.get("features", []):
        if (f.get("properties") or {}).get("kind") == "scan-footprint":
            for poly in _poly_rings(f.get("geometry") or {}):
                rings.extend(poly)
    return rings


def _glb_json(p: Path) -> tuple[dict, int, int]:
    """GLB 头与 JSON 块：(json, BIN 块数据起点, BIN 块长度)。"""
    with open(p, "rb") as f:
        magic, ver, _total = struct.unpack("<4sII", f.read(12))
        if magic != b"glTF" or ver != 2:
            raise ValueError("not a glTF 2.0 binary")
        jl, jt = struct.unpack("<I4s", f.read(8))
        if jt != b"JSON":
            raise ValueError("first GLB chunk is not JSON")
        js = json.loads(f.read(jl))
        bin_off, bin_len = 12 + 8 + jl + 8, 0
        hdr = f.read(8)
        if len(hdr) == 8:
            bin_len, bt = struct.unpack("<I4s", hdr)
            if bt != b"BIN\0":
                raise ValueError("second GLB chunk is not BIN")
    return js, bin_off, bin_len


@glb_rule("V-GLB-01", "semantic")
def glb01(w: WorldView, rep: Report):
    _glb(w)
    src = w.coordinate["source"]
    u = float(src["unitsToMeters"])
    det = float(np.linalg.det(_mat(src["T_world_source"])[:3, :3]))
    cbrt = math.copysign(abs(det) ** (1.0 / 3.0), det)
    rep.check(det > 0 and _rel(u, cbrt) <= 1e-4, f"unitsToMeters {u} != cbrt(det(T_world_source)) {cbrt:.9g} (rel 1e-4)", "coordinate.json")
    s = ((w.coordinate.get("registration") or {}).get("T_world_map") or {}).get("s")
    rep.check(s is not None and _rel(u, float(s)) <= 1e-9, f"unitsToMeters {u} != registration.T_world_map.s {s} (rel 1e-9)",
              "coordinate.json")


@glb_rule("V-GLB-02", "semantic", "E/W")
def glb02(w: WorldView, rep: Report):
    _glb(w)
    c = w.coordinate
    ss = c.get("scaleStatus")
    method = (c.get("registration") or {}).get("method")
    if ss in VGLB_SCALE_EVIDENCE:
        rep.check(bool((c.get("source") or {}).get("evidence")), f"scaleStatus {ss} without source.evidence[]", "coordinate.json")
        rep.check(method not in (None, "none"), f"scaleStatus {ss} without registration.method", "coordinate.json")
    if ss == "assumed":
        rep.check(method == "manual", f"scaleStatus assumed requires registration.method manual, got {method}", "coordinate.json")
    elif method in VGLB_OSM_METHODS:
        rep.check(ss in VGLB_SCALE_EVIDENCE, f"registration.method {method} with scaleStatus {ss}", "coordinate.json")
    elif method == "manual" and ss in VGLB_SCALE_EVIDENCE:
        rep.warn(f"registration.method manual with scaleStatus {ss}: the evidence must name the measured reference", "coordinate.json")
    if not w.staging:
        g = _qa_glb(w)
        rep.check(bool(g and isinstance(g.get("registration"), dict) and g["registration"]),
                  "qa/report.json glb.registration missing", "qa/report.json")


@glb_rule("V-GLB-03", "deep")
def glb03(w: WorldView, rep: Report):
    _glb(w)
    Ls = [L for L in _layers_by(w, fmt="awr-trimesh@1") if L.get("id") == "mesh.raycast"]
    if not Ls:
        raise Skip("mesh.raycast missing")
    b = w.world["bounds"]
    lo, hi = np.asarray(b["min"], np.float64), np.asarray(b["max"], np.float64)
    _doc, V, _F, _t = _trimesh(w, Ls[0])
    ex = np.maximum(lo - V, V - hi).max(1) if len(V) else np.zeros(0)
    bad = int((ex > VGLB_BOUNDS_TOL_M).sum())
    rep.check(bad == 0, f"{bad} raycast vertices outside world.json bounds (max excess {float(ex.max(initial=0.0)):.4f} m, "
              f"tolerance {VGLB_BOUNDS_TOL_M} m)", Ls[0]["href"])


@glb_rule("V-GLB-04", "deep")
def glb04(w: WorldView, rep: Report):
    _glb(w)
    Ls = [L for L in _layers_by(w, fmt="awr-trimesh@1") if L.get("id") == "mesh.raycast"]
    if not Ls or "terrain.dsm-n" not in w.layers:
        raise Skip("mesh.raycast or terrain.dsm-n missing")
    dsc, dsm = w.grid("terrain.dsm")
    _nsc, cnt = w.grid("terrain.dsm-n")
    tsc, dtm = w.grid("terrain.dtm")
    if dsm is None or cnt is None or dtm is None:
        raise Skip("raster unreadable")
    _doc, V, F, _t = _trimesh(w, Ls[0])
    H, W = dsm.shape
    cell = float(dsc["cellM"])
    org = (float(dsc["originXY"][0]), float(dsc["originXY"][1]))
    rng = np.random.default_rng(VGLB_SEED)
    flat = np.arange(H * W) if H * W <= VGLB_DSM_CELLS else np.sort(rng.choice(H * W, VGLB_DSM_CELLS, replace=False))
    rows, cols = flat // W, flat % W
    top, nh = vertical_hits(V, F, org, cell, rows, cols)
    fill = bilinear_on_centres(Grid(dtm, (float(tsc["originXY"][0]), float(tsc["originXY"][1])), float(tsc["cellM"])), org, cell, H, W)
    t = fill[rows, cols]
    want = np.where(nh > 0, np.maximum(top, t), t)
    d = np.abs(dsm[rows, cols].astype(np.float64) - want)
    bad = int((d >= VGLB_DSM_TOL_M).sum())
    rep.check(bad == 0, f"{bad}/{len(flat)} sampled 2 m cells differ from the vertical mesh recompute by >= 1 cm "
              f"(max {float(d.max(initial=0.0)):.4f} m)", "geometry/terrain/dsm_2m.json")
    badn = int((cnt[rows, cols].astype(np.int64) != nh).sum())
    rep.check(badn == 0, f"{badn}/{len(flat)} sampled cells: dsm_2m_n != recomputed hit samples (0-16)", "geometry/terrain/dsm_2m_n.json")
    rep.info.setdefault("vglb", {})["V-GLB-04"] = {"cells": len(flat), "max_abs_m": round(float(d.max(initial=0.0)), 6),
                                                    "hit_cells": int((nh > 0).sum())}


@glb_rule("V-GLB-05", "semantic")
def glb05(w: WorldView, rep: Report):
    _glb(w)
    Ls = _layers_by(w, type_="navmesh")
    if not Ls:
        raise Skip("no navmesh layer")
    for L in Ls:
        nm = w.load_json(L["href"])
        isl = nm.get("islands") or {}
        rep.check(int(isl.get("kept", 0)) >= 1, f"{nm.get('profile')}: islands.kept < 1", L["href"])
        area = isl.get("seed_area_m2", isl.get("walkable_area_m2", 0.0))
        rep.check(float(area or 0.0) > 0.0, f"{nm.get('profile')}: seed island area is 0", L["href"])
        rep.check(bool(nm.get("seeds")), f"{nm.get('profile')}: seeds[] empty", L["href"])
        b = nm.get("bin") or {}
        p = (w.dir / L["href"]).parent / str(b.get("href", ""))
        rep.check(bool(b.get("href")) and p.is_file() and p.stat().st_size == int(b.get("bytes", -1)),
                  f"{nm.get('profile')}: navmesh bin missing or byte size differs", L["href"])


@glb_rule("V-GLB-06", "semantic", "W")
def glb06(w: WorldView, rep: Report):
    _glb(w)
    Ls = _layers_by(w, type_="anchors")
    if not Ls:
        raise Skip("no anchors layer")
    doc = w.load_json(Ls[0]["href"])
    pts = [a for a in doc.get("anchors", []) if a.get("kind") == "point" and a.get("status") != "deprecated"]
    far = 0
    for a in pts:
        d = (a.get("snap_dist_m") or {}).get("person")
        if d is None or not float(d) < VGLB_SNAP_WARN_M:
            far += 1
            rep.warn(f"anchor {a.get('id')}: snap_dist_m.person {d} >= {VGLB_SNAP_WARN_M} m", Ls[0]["href"])
    if pts and far / len(pts) > VGLB_SNAP_WARN_FRAC:
        rep.warn(f"{far}/{len(pts)} point anchors ({100.0 * far / len(pts):.1f}%) snap farther than {VGLB_SNAP_WARN_M} m "
                 f"(> {100 * VGLB_SNAP_WARN_FRAC:.0f}%)", Ls[0]["href"])


@glb_rule("V-GLB-07", "deep")
def glb07(w: WorldView, rep: Report):
    _glb(w)
    rings = _cover_rings(w)
    if rings is None:
        raise Skip("no geojson/awr-quality@1 layer")
    nsc, cnt = w.grid("terrain.dsm-n")
    if cnt is None:
        raise Skip("dsm_2m_n unreadable")
    H, W = cnt.shape
    org = (float(nsc["originXY"][0]), float(nsc["originXY"][1]))
    poly = rasterize_rings(rings, org, float(nsc["cellM"]), H, W)
    cov = coverage_mask(cnt)
    inter, union = int((poly & cov).sum()), int((poly | cov).sum())
    iou = inter / union if union else 0.0
    rep.check(iou >= VGLB_COVER_IOU, f"coverage polygon IoU {iou:.4f} < {VGLB_COVER_IOU} against closing(dsm_2m_n > 0)",
              _layers_by(w, fmt="geojson/awr-quality@1")[0]["href"])
    rep.info.setdefault("vglb", {})["V-GLB-07"] = {"iou": round(iou, 5)}


def _odbl_layers(w: WorldView) -> list[tuple[dict, dict]]:
    """ODbL 派生层：锚点、道路图，以及声明了 OSM 来源的质量多边形（M22 §7.3.4–§7.3.6）。返回 (layer, 文件头)。"""
    out = []
    for L in w.world.get("layers", []):
        fmt = L.get("format")
        if fmt not in ("awr-anchors@1", "geojson/awr-navgraph@1", "geojson/awr-quality@1"):
            continue
        doc = w.load_json(L["href"])
        head = doc.get("awr", {}) if fmt.startswith("geojson/") else doc
        if fmt == "geojson/awr-quality@1" and not (head.get("license") or head.get("attribution")):
            continue                                              # 不含 OSM 要素的质量多边形不属于 ODbL 派生层
        out.append((L, head))
    return out


@glb_rule("V-GLB-08", "semantic")
def glb08(w: WorldView, rep: Report):
    _glb(w)
    d = w.world.get("dataset")
    if not isinstance(d, dict):
        rep.error("dataset missing (602 WORLD_LICENSE_MISSING)", "world.json")
        return
    lic = str(d.get("license") or "")
    for k in ("license", "citation", "notice"):
        rep.check(bool(str(d.get(k) or "").strip()) and str(d.get(k)).strip().lower() != "unknown", f"dataset.{k} missing", "world.json")
    rep.check(bool(d.get("sourceFiles")) and all(re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))) for f in d.get("sourceFiles", [])),
              "dataset.sourceFiles[] missing or without sha256", "world.json")
    if lic.startswith("CC-BY"):
        rep.check(lic in str(d.get("citation", "")), f"dataset.citation must name the author and {lic}", "world.json")
        rep.check(bool(d.get("url")), f"dataset.url missing for {lic} source", "world.json")
    df = d.get("derivedFrom")
    if not isinstance(df, list):
        rep.error("dataset.derivedFrom[] missing", "world.json")
        df = []
    for i, e in enumerate(df):
        miss = [k for k in ("name", "version", "license", "use", "attribution") if not str(e.get(k) or "").strip()]
        rep.check(not miss, f"dataset.derivedFrom[{i}] missing {miss}", "world.json")
        rep.check(bool(re.fullmatch(r"[0-9a-f]{64}", str(e.get("sha256", "")))), f"dataset.derivedFrom[{i}].sha256 missing", "world.json")
    osm = [e for e in df if e.get("license") == ODBL and "OpenStreetMap" in str(e.get("attribution", ""))]
    dem = [e for e in df if {"levelling", "hMsl"} & set(str(e.get("use", "")).split(","))]
    method = (w.coordinate.get("registration") or {}).get("method")
    odbl = _odbl_layers(w)
    if method in VGLB_OSM_METHODS or odbl:
        rep.check(bool(osm), "dataset.derivedFrom[] has no OpenStreetMap entry (ODbL-1.0, 'OpenStreetMap contributors')", "world.json")
    if method in VGLB_OSM_METHODS:
        rep.check(bool(dem), "dataset.derivedFrom[] has no DEM entry (use levelling or hMsl)", "world.json")
    if osm:
        rep.check("ODbL" in str(d.get("notice", "")), "dataset.notice must state that OpenStreetMap-derived layers are ODbL-1.0", "world.json")
    for L, head in odbl:
        rep.check(bool(head.get("license")) and bool(head.get("attribution")), f"{L.get('id')}: ODbL layer without license and attribution",
                  L["href"])
        if head.get("license") == ODBL:
            rep.check(bool(osm), f"{L.get('id')}: ODbL layer but dataset.derivedFrom[] has no OpenStreetMap entry", L["href"])


@glb_rule("V-GLB-09", "deep")
def glb09(w: WorldView, rep: Report):
    _glb(w)
    Ls = _layers_by(w, fmt="awr-trimesh@1")
    if not Ls:
        raise Skip("no awr-trimesh@1 layer")
    n_app = None
    ap = _layers_by(w, fmt="gltf-glb@2")
    if ap:
        try:
            n_app = int(w.load_json(_sidecar_rel(ap[0]))["triangles"])
        except (Skip, KeyError, TypeError, ValueError):
            n_app = None
    for L in Ls:
        rel = _sidecar_rel(L)
        doc = w.load_json(rel)
        base = (w.dir / rel).parent
        cn = doc.get("counts") or {}
        nv, nt = int(cn.get("vertices", -1)), int(cn.get("triangles", -1))
        tiles = doc.get("tiles", [])
        rep.check(int(cn.get("tiles", -1)) == len(tiles), "counts.tiles != len(tiles)", rel)
        v0 = t0 = 0
        for t in tiles:
            if int(t["v0"]) != v0 or int(t["t0"]) != t0:
                rep.error(f"tile {t.get('id')}: v0/t0 not contiguous", rel)
                break
            v0 += int(t["nv"])
            t0 += int(t["nt"])
        rep.check(v0 == nv and t0 == nt, f"tiles sum (vertices {v0}, triangles {t0}) != counts ({nv}, {nt})", rel)
        per = {"xyz": nv, "tri": nt, "src": nt, "cls": nt, "q": nt}
        for s in doc.get("streams", []):
            if s.get("name") not in per:
                continue
            p = base / s["href"]
            exp = per[s["name"]] * int(s.get("components", 1)) * {**ITEMSIZE, "uint32": 4}.get(s.get("dtype", ""), 0)
            rep.check(p.is_file() and p.stat().st_size == exp, f"stream {s['name']}: bytes != {exp}", rel)
        if not rep.ok and any(e["where"] == rel for e in rep.errors):
            continue
        st = {s["name"]: s for s in doc.get("streams", [])}
        xyz = np.fromfile(base / st["xyz"]["href"], "<f4").reshape(-1, 3)
        tri = np.fromfile(base / st["tri"]["href"], "<u4").reshape(-1, 3)
        tiling = doc.get("tiling") or {}
        for t in tiles:
            a, n = int(t["v0"]), int(t["nv"])
            loc = xyz[a:a + n, :2].astype(np.float64)
            # 局部坐标以瓦片包围盒中心为参照取绝对值（origin_m 为瓦片网格角点，按重心分瓦片的三角形可越出网格边）
            lo_t, hi_t = np.asarray(t["aabb_m"]["min"][:2], np.float64), np.asarray(t["aabb_m"]["max"][:2], np.float64)
            ctr = (lo_t + hi_t) / 2.0 - np.asarray(t["origin_m"][:2], np.float64)
            lim = float(tiling["size_m"]) if tiling.get("kind") == "grid" else float(np.max(hi_t - lo_t))
            dev = float(np.abs(loc - ctr).max()) if n else 0.0
            if dev > lim:
                rep.error(f"tile {t['id']}: |local xy - tile centre| {dev:.3f} m > tile edge {lim} m", rel)
            tt = tri[int(t["t0"]):int(t["t0"]) + int(t["nt"])]
            if tt.size and int(tt.max()) >= n:
                rep.error(f"tile {t['id']}: tri index {int(tt.max())} >= tile vertices {n}", rel)
        if "src" in st and n_app is not None:
            src = np.fromfile(base / st["src"]["href"], "<u4")
            rep.check(src.size == 0 or int(src.max()) < n_app, f"src index >= appearance triangles {n_app}", rel)


@glb_rule("V-GLB-10", "semantic")
def glb10(w: WorldView, rep: Report):
    _glb(w)
    want = (w.world.get("coordinate") or {}).get("sha256")
    seen = 0
    for L in w.world.get("layers", []):
        need = D3_SIDECAR_SHA.get(L.get("format"))
        if need is None:
            continue
        rel = _sidecar_rel(L)
        doc = w.load_json(rel)
        got = doc.get("coordinate_sha256", (doc.get("awr") or {}).get("coordinate_sha256") if isinstance(doc.get("awr"), dict) else None)
        if got is None and not need:
            continue
        seen += 1
        rep.check(got == want, f"{L.get('id')}: coordinate_sha256 {str(got)[:12]} != world.coordinate.sha256 {str(want)[:12]}", rel)
    if not seen:
        raise Skip("no D3 sidecar")


@glb_rule("V-GLB-11", "deep")
def glb11(w: WorldView, rep: Report):
    _glb(w)
    Ls = _layers_by(w, fmt="gltf-glb@2", type_="mesh")
    if not Ls:
        raise Skip("no appearance layer")
    rel = _sidecar_rel(Ls[0])
    idx = w.load_json(rel)
    p = (w.dir / rel).parent / idx["glb"]["href"]
    js, bin_off, _bin_len = _glb_json(p)
    mats = js.get("materials", [])
    bad = [i for i, m in enumerate(mats) if "KHR_materials_unlit" not in (m.get("extensions") or {}) or m.get("doubleSided") is not True]
    rep.check(bool(mats) and not bad, f"materials not unlit and double-sided: {bad[:5]}", rel)
    acc = js.get("accessors", [])
    ntri = 0
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for mesh in js.get("meshes", []):
        for pr in mesh.get("primitives", []):
            if pr.get("mode", 4) != 4:
                rep.error("primitive mode is not TRIANGLES", rel)
            ntri += int(acc[pr["indices"]]["count"]) // 3
            a = acc[pr["attributes"]["POSITION"]]
            lo, hi = np.minimum(lo, a["min"]), np.maximum(hi, a["max"])
    rep.check(ntri == int(idx.get("triangles", -1)), f"appearance triangles {ntri} != index.json {idx.get('triangles')}", rel)
    rc = _layers_by(w, fmt="awr-trimesh@1")
    rc = [L for L in rc if L.get("id") == "mesh.raycast"]
    if rc:
        n_rc = int(w.load_json(_sidecar_rel(rc[0]))["counts"]["triangles"])
        rep.check(ntri == n_rc, f"appearance triangles {ntri} != raycast triangles {n_rc}", rel)
    enu_lo = np.array([lo[0], -hi[2], lo[1]])                       # (E, U, −N) → (E, N, U)
    enu_hi = np.array([hi[0], -lo[2], hi[1]])
    b = w.world["bounds"]
    dev = max(float(np.abs(enu_lo - np.asarray(b["min"])).max()), float(np.abs(enu_hi - np.asarray(b["max"])).max()))
    rep.check(dev <= VGLB_APPEARANCE_TOL_M, f"appearance bounding box differs from bounds by {dev:.4f} m (> {VGLB_APPEARANCE_TOL_M} m)", rel)
    rep.check(p.stat().st_size == int(idx["glb"]["bytes"]) and file_sha(p) == idx["glb"]["sha256"], "appearance.glb bytes or sha256 != index.json", rel)
    want = {int(t["index"]): t["sha256"] for t in idx.get("textures", [])}
    views = js.get("bufferViews", [])
    with open(p, "rb") as f:
        for i, im in enumerate(js.get("images", [])):
            v = views[im["bufferView"]]
            f.seek(bin_off + int(v.get("byteOffset", 0)))
            got = hashlib.sha256(f.read(int(v["byteLength"]))).hexdigest()
            rep.check(want.get(i) == got, f"image {i} sha256 {got[:12]} != source {str(want.get(i))[:12]}", rel)
    rep.check(len(js.get("images", [])) == len(want), f"images {len(js.get('images', []))} != index.json textures {len(want)}", rel)


@glb_rule("V-GLB-12", "semantic")
def glb12(w: WorldView, rep: Report):
    _glb(w)
    z = _zones(w)
    border = [f for f in z.get("features", []) if (f.get("properties") or {}).get("kind") == "border"]
    if not border:
        raise Skip("no border zone")
    _sc, dsm = w.grid("terrain.dsm")
    if dsm is None:
        raise Skip("dsm unreadable")
    head = float((w.world.get("generator") or {}).get("params", {}).get("borderHeadroomM", 50))
    want = round(float(dsm.max()) + head, 2)
    cover = _cover_rings(w)
    for f in border:
        polys = _poly_rings(f.get("geometry") or {})
        n = max((len(r) for p in polys for r in p), default=0)
        rep.check(n <= VGLB_RING_MAX, f"border ring has {n} vertices (> {VGLB_RING_MAX})", "semantic/zones.geojson")
        got = (f.get("properties") or {}).get("max_z_m")
        rep.check(got is not None and abs(float(got) - want) <= 0.005, f"border max_z_m {got} != round(max(dsm_2m) + {head:g}, 2) = {want}",
                  "semantic/zones.geojson")
        if cover:
            P = np.concatenate([r for p in polys for r in p])
            out = int((~_points_in_rings(P, cover)).sum())
            rep.check(out == 0, f"{out} border vertices outside the coverage polygon", "semantic/zones.geojson")


def _occ_state(w: WorldView, L: dict, doc: dict, vox: np.ndarray, cover_col: np.ndarray) -> np.ndarray:
    """`awr-occ@1` 抽样解码（M22 §7.3.2）：vox (n, 3) 体素下标 → u8 状态。砖块键为 (bx, by, bz) 的 3D Morton 交织（x 在最低位），
    砖块内体素按 (z, y, x) 行主序；`flags & 1` 的砖块数据为 1 字节全同值；`top = 0xFFFF` 的柱整柱 unknown，序号 > top 的砖块隐式
    （覆盖内 free、覆盖外 unknown）。"""
    import zstandard

    base = (w.dir / _sidecar_rel(L)).parent
    B = int(doc.get("brick", 16))
    tp = doc["top"]
    top = np.fromfile(base / tp["href"], "<u2").reshape(int(tp["height"]), int(tp["width"]))
    bi = doc["bricks"]
    idx = np.fromfile(base / bi["index"], OCC_IDX_DTYPE)
    data = np.memmap(base / bi["data"], np.uint8, "r") if (base / bi["data"]).stat().st_size else np.zeros(0, np.uint8)
    keys = idx["key"]
    bricks = vox // B
    out = np.full(len(vox), OCC_UNKNOWN, np.uint8)
    dctx = zstandard.ZstdDecompressor()
    cache: dict[int, np.ndarray] = {}
    for i, (bx, by, bz) in enumerate(bricks):
        t = int(top[by, bx])
        if t == 0xFFFF:
            continue
        if bz > t:
            out[i] = OCC_FREE if cover_col[i] else OCC_UNKNOWN
            continue
        key = _morton3(int(bx), int(by), int(bz))
        j = int(np.searchsorted(keys, key))
        if j >= len(keys) or int(keys[j]) != key:
            out[i] = OCC_FREE if cover_col[i] else OCC_UNKNOWN
            continue
        if j not in cache:
            off, ln, fl = int(idx["offset"][j]), int(idx["length"][j]), int(idx["flags"][j])
            raw = bytes(data[off:off + ln])
            if fl & 1:
                cache[j] = np.full(B * B * B, raw[0] if len(raw) == 1 else dctx.decompress(raw)[0], np.uint8)
            else:
                cache[j] = np.frombuffer(dctx.decompress(raw, max_output_size=B * B * B), np.uint8)
        lx, ly, lz = (vox[i] % B).tolist()
        out[i] = cache[j][(lz * B + ly) * B + lx]
    return out


def _morton3(x: int, y: int, z: int) -> int:
    k = 0
    for b in range(21):
        k |= ((x >> b) & 1) << (3 * b) | ((y >> b) & 1) << (3 * b + 1) | ((z >> b) & 1) << (3 * b + 2)
    return k


@glb_rule("V-GLB-13", "deep")
def glb13(w: WorldView, rep: Report):
    _glb(w)
    Ls = [L for L in _layers_by(w, fmt="awr-occ@1", type_="voxel")]
    rc = [L for L in _layers_by(w, fmt="awr-trimesh@1") if L.get("id") == "mesh.raycast"]
    if not Ls or not rc or "terrain.dsm-n" not in w.layers:
        raise Skip("occupancy, mesh.raycast or terrain.dsm-n missing")
    L = Ls[0]
    doc = w.load_json(_sidecar_rel(L))
    if doc.get("kind", "occupancy") != "occupancy":
        raise Skip("not an occupancy layer")
    v = float(doc["voxel_m"])
    o = np.asarray(doc["origin_m"], np.float64)
    dims = np.asarray(doc["dims"], np.int64)
    nsc, cnt = w.grid("terrain.dsm-n")
    if cnt is None:
        raise Skip("dsm_2m_n unreadable")
    cov = coverage_mask(cnt)
    H, W = cov.shape
    c2 = float(nsc["cellM"])
    n_org = np.asarray(nsc["originXY"], np.float64)
    rng = np.random.default_rng(VGLB_SEED + 13)
    # 覆盖外：距覆盖 > 2 格的 2 m 格内的体素柱（避开覆盖边界的栅格化差异）
    from scipy import ndimage

    far = ~ndimage.binary_dilation(cov, iterations=2)
    fr, fc = np.nonzero(far)
    pairs: list[tuple[str, np.ndarray, np.ndarray, int]] = []
    if len(fr):
        pick = rng.integers(0, len(fr), VGLB_OCC_SAMPLES)
        xy = n_org + (np.stack([fc[pick], fr[pick]], 1) + rng.random((VGLB_OCC_SAMPLES, 2))) * c2
        vx = np.floor((xy - o[:2]) / v).astype(np.int64)
        vz = rng.integers(0, dims[2], VGLB_OCC_SAMPLES)
        vox = np.column_stack([vx, vz])
        m = np.all((vox >= 0) & (vox < dims), axis=1)
        pairs.append(("outside", vox[m], np.zeros(int(m.sum()), bool), OCC_UNKNOWN))
    _d, V, F, _t = _trimesh(w, rc[0])
    used = np.unique(F.reshape(-1))
    pv = V[used[rng.integers(0, len(used), VGLB_OCC_SAMPLES)]] if len(used) else np.zeros((0, 3))
    vox = np.floor((pv - o) / v).astype(np.int64)
    m = np.all((vox >= 0) & (vox < dims), axis=1)
    cr = np.clip(np.floor((pv[m, 1] - n_org[1]) / c2).astype(np.int64), 0, H - 1)
    cc = np.clip(np.floor((pv[m, 0] - n_org[0]) / c2).astype(np.int64), 0, W - 1)
    pairs.append(("surface", vox[m], cov[cr, cc], OCC_OCCUPIED))
    for name, vx, ccol, want in pairs:
        if name == "surface":
            vx, ccol = vx[ccol], ccol[ccol]                      # 覆盖外的表面体素按约定为 unknown，不计入
        st = _occ_state(w, L, doc, vx, ccol)
        bad = int((st != want).sum())
        rep.check(bad == 0, f"{bad}/{len(st)} sampled {name} voxels are not {('unknown', 'free', 'occupied', 'below_surface')[want]}",
                  _sidecar_rel(L))


@glb_rule("V-GLB-14", "semantic")
def glb14(w: WorldView, rep: Report):
    _glb(w)
    Ls = _layers_by(w, type_="navmesh")
    if not Ls:
        raise Skip("no navmesh layer")
    from awr.world.ingest.manifest import repo_root

    vf = repo_root() / "tools" / "worldglb" / "RECAST_VERSION"
    want_v = vf.read_text(encoding="utf-8").strip() if vf.is_file() else None
    recipe = {}
    rp = repo_root() / "configs" / "worlds.d3" / str(w.world.get("id")) / "recipe.yaml"
    if rp.is_file():
        import yaml

        recipe = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
    for L in Ls:
        nm = w.load_json(L["href"])
        prof = nm.get("profile")
        rep.check(want_v is not None and nm.get("recast_version") == want_v,
                  f"{prof}: recast_version {nm.get('recast_version')} != tools/worldglb/RECAST_VERSION {want_v}", L["href"])
        agent = {**VGLB_AGENTS.get(prof, {}), **(((recipe.get("build") or {}).get("nav") or {}).get(prof, {}).get("agent") or {})}
        got = nm.get("agent") or {}
        diff = [k for k, val in agent.items() if not (k in got and abs(float(got[k]) - float(val)) <= 1e-9)]
        rep.check(not diff, f"{prof}: agent {diff} differ from the recipe ({agent})", L["href"])


@glb_rule("V-GLB-15", "semantic", "W")
def glb15(w: WorldView, rep: Report):
    """调平证据：qa/report.json `glb.leveling`（L1–L5 判定位优先，缺省时按量复核）或 G-11/G-12/G-13 门禁；staging 中报告尚未写出，
    由构建在报告后补验。"""
    _glb(w)
    if w.staging:
        raise Skip("qa/report.json is written after validation")
    g = _qa_glb(w) or {}
    lv = g.get("leveling") if isinstance(g.get("leveling"), dict) else {}
    try:
        gates = {x.get("id"): x for x in w.load_json("qa/report.json").get("gates", [])}
    except Skip:
        gates = {}

    def gate(*ids: str) -> bool | None:
        x = next((gates[i] for i in ids if i in gates), None)
        return None if x is None else bool(x.get("pass"))

    b = w.world.get("bounds") or {}
    diag = math.hypot(*(np.subtract(b["max"], b["min"])[:2])) if b else 0.0
    km = bool(lv.get("km_scale", diag >= VGLB_KM_DIAG_M))
    msgs = []
    if km:
        d = lv.get("delta_deg")
        ok1 = lv["L1"] if "L1" in lv else (float(d) <= 0.5) if d is not None else gate("G-11", "G-L1")
        if not ok1:
            msgs.append(f"two-method levelling difference {d} deg > 0.5 deg or missing (L1)")
    else:
        sd, ci = lv.get("subset_delta_deg"), lv.get("ci95_deg")
        ok2 = lv["L2"] if "L2" in lv else (float(sd) <= 0.3 and (ci is None or float(ci) <= 0.3)) if sd is not None else gate("G-12", "G-L2")
        if not ok2:
            msgs.append(f"wall-subset consistency {sd} deg / CI95 {ci} deg > 0.3 deg or missing (L2)")
    lim = float(lv.get("planes_limit_deg", 0.2 if km else 0.5))
    planes = [p for p in lv.get("planes", []) if isinstance(p, dict) and p.get("slope_deg") is not None and p.get("evaluated", True)]
    cp = lv.get("control_points") if isinstance(lv.get("control_points"), dict) else {}
    if "L3" in lv or "L4" in lv:          # L5：公开 DEM 交叉核对（两 DEM 与墙面三法，W07；L5 成立时 L3 只作 G-16 告警）
        ok34 = bool(lv.get("L3")) or bool(lv.get("L4")) or bool(lv.get("L5"))
    elif planes or cp:
        ok34 = (len(planes) >= 3 and max(float(p["slope_deg"]) for p in planes) <= lim) or \
            (int(cp.get("n", 0)) >= 20 and cp.get("rms_m") is not None and float(cp["rms_m"]) <= 2.0)
    else:
        ok34 = gate("G-13", "G-L34")
    if not ok34:
        msgs.append(f"no acceptance: >= 3 horizontal planes with slope <= {lim:g} deg, >= 20 control points with AGL RMS <= 2 m "
                    "or the public-DEM cross-check (L3, L4, L5)")
    if lv.get("needs_control_points"):
        msgs.append("needs_control_points = true")
    for m in msgs:
        rep.warn(f"levelling: {m}", "qa/report.json")


@glb_rule("V-GLB-16", "structure")
def glb16(w: WorldView, rep: Report):
    _glb(w)
    rep.check(w.world.get("schemaVersion") == "1.1.0", f"schemaVersion {w.world.get('schemaVersion')} != 1.1.0", "world.json")
    rep.check((w.world.get("render") or {}).get("defaultColorMode") == "rgb", "render.defaultColorMode != rgb", "world.json")
    for rv in w.roots:
        if rv.metadata is None:
            continue
        col = ((rv.metadata.get("anet") or {}).get("col") or {})
        rep.check(col.get("rgb") == "srgb", f"point cloud col.rgb {col.get('rgb')} != srgb", f"{rv.entry.get('href', '')}metadata.json")


def vglb_results(rep: Report) -> list[dict]:
    """V-GLB 每条规则的结果（qa/report.json `glb.vglb`，M22 §7.3.8）：status ∈ pass、warn、fail、n/a。"""
    out = []
    for rid in VGLB_RULE_IDS:
        rd = GLB_RULES[rid]
        errs = [e for e in rep.errors if e["rule"] == rid]
        warns = [e for e in rep.warnings if e["rule"] == rid]
        if rid in rep.skipped_rules or (rd.layer == "deep" and not rep.deep):
            st = "n/a"
        else:
            st = "fail" if errs else "warn" if warns else "pass"
        row = {"id": rid, "severity": rd.severity, "status": st}
        if st == "n/a":
            row["reason"] = rep.skipped_rules.get(rid, "deep rule not run")
        msgs = [e["message"] for e in errs + warns][:5]
        if msgs:
            row["messages"] = msgs
        if rid in RULE_REASONS and errs:
            row["reason_code"] = RULE_REASONS[rid]
        if rid in (rep.info.get("vglb") or {}):
            row["measured"] = rep.info["vglb"][rid]
        out.append(row)
    return out


def report_reason(rep: Report) -> int | None:
    """失败报告对应的原因码：V-GLB-08 失败为 602（WORLD_LICENSE_MISSING），其余 error 为 None（构建退出码 1）。"""
    codes = sorted({RULE_REASONS[e["rule"]] for e in rep.errors if e["rule"] in RULE_REASONS})
    return codes[0] if codes else None


# ---------------------------------------------------------------- 驱动


def is_world_dir(p: Path) -> bool:
    return p.is_dir() and bool(WORLD_ID_RE.match(p.name))


def _rules_for(w: WorldView) -> dict[str, RuleDef]:
    """D2 规则全部执行；`profile = d3-glb` 的世界其后追加 V-GLB 规则组（M22-FR-042：先 D2 再 V-GLB）。"""
    try:
        glb = isinstance(w.world, dict) and w.world.get("profile") == "d3-glb"
    except Skip:
        glb = False
    return {**RULES, **GLB_RULES} if glb else RULES


def validate_world(world_dir: Path, *, deep: bool = False, rules: set[str] | None = None, staging: bool = False) -> Report:
    """`staging=True` 时跳过 V-Q 组，V-W-02 降为告警（16 §15.3）；D3 GLB 世界追加 V-GLB 规则组。"""
    t0 = time.perf_counter()
    world_dir = Path(world_dir)
    rep = Report(path=str(world_dir), deep=deep, staging=staging)
    w = WorldView(world_dir, staging=staging)
    try:
        rep.world_id = w.world.get("id")
    except Skip:
        rep.world_id = None
    for rid, rd in _rules_for(w).items():
        if rules is not None and rid not in rules:
            continue
        if rd.layer == "deep" and not deep:
            continue
        rep.rules_checked += 1
        rep._rule = rid
        try:
            rd.fn(w, rep)
        except Skip as e:
            rep.skipped_rules[rid] = str(e)
            continue
        except (KeyError, TypeError, ValueError, IndexError, AttributeError, OSError) as e:
            rep.error(f"cannot evaluate: {type(e).__name__}: {e}", "")
    rep._rule = ""
    rep.seconds = time.perf_counter() - t0
    return rep


def validate_cli_json(reports: list[Report], skipped: list[str]) -> dict:
    return {"validator": VALIDATOR_NAME, "version": VALIDATOR_VERSION, "schema_version": "1.0.0",
            "results": [r.to_json() for r in reports], "skipped": skipped}


def rules_catalog(*, glb: bool = False) -> list[dict]:
    return [{"id": r.id, "layer": r.layer, "severity": r.severity} for r in ({**RULES, **GLB_RULES} if glb else RULES).values()]


def file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    return h.hexdigest()
