"""语义区域 `semantic/zones.geojson`（AWR-16 §7；M03-FR-026）：派生唯一 `border`，合并人工整理的
`scenarios/zones/<id>.zones.geojson`（M16 所有，`origin = curated`），写 `awr.source_sha256`。

几何规则（V-Z-03、V-Z-04、V-Z-05）的检查函数也在这里，供派生与校验器共用。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from ..ingest.types import ConfigError
from ..package.derivers import DeriveContext, LayerSpec
from ..package.jsonio import write_json

ZONES_HREF = "semantic/zones.geojson"
MAX_RING_VERTICES = 1024
MAX_ZONES = 64


def curated_zones_path(repo_root: Path, world_id: str) -> Path:
    return Path(repo_root) / "scenarios" / "zones" / f"{world_id}.zones.geojson"


# ---------------------------------------------------------------- 几何检查（V-Z-03 至 V-Z-05）


def ring_area2(ring: list[list[float]]) -> float:
    """有向面积的 2 倍（>0 为逆时针）；ring 首尾闭合。"""
    a = np.asarray(ring, np.float64)
    x, y = a[:-1, 0], a[:-1, 1]
    x2, y2 = a[1:, 0], a[1:, 1]
    return float(np.sum(x * y2 - x2 * y))


def ring_self_intersects(ring: list[list[float]]) -> bool:
    """非相邻边之间是否相交（含接触）；O(n²) 向量化，n ≤ 1024。"""
    a = np.asarray(ring, np.float64)[:-1]
    n = len(a)
    if n < 4:
        return False
    p = a
    q = np.roll(a, -1, axis=0)
    i, j = np.triu_indices(n, k=2)
    keep = ~((i == 0) & (j == n - 1))             # 首边与末边相邻
    i, j = i[keep], j[keep]
    p1, q1, p2, q2 = p[i], q[i], p[j], q[j]

    def orient(a_, b_, c_):
        return (b_[:, 0] - a_[:, 0]) * (c_[:, 1] - a_[:, 1]) - (b_[:, 1] - a_[:, 1]) * (c_[:, 0] - a_[:, 0])

    o1, o2 = orient(p1, q1, p2), orient(p1, q1, q2)
    o3, o4 = orient(p2, q2, p1), orient(p2, q2, q1)
    proper = (np.sign(o1) * np.sign(o2) < 0) & (np.sign(o3) * np.sign(o4) < 0)
    return bool(proper.any())


def polygons_of(geom: dict) -> list[list[list[list[float]]]]:
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    return list(geom["coordinates"])


def check_zone_geometry(fc: dict, bounds_min=None, bounds_max=None, *, tol: float = 1e-6) -> list[tuple[str, str]]:
    """返回 [(规则 ID, 说明)]；空列表即通过。"""
    errs: list[tuple[str, str]] = []
    feats = fc.get("features", [])
    n_restr = 0
    ids = set()
    for f in feats:
        zid = f.get("id")
        props = f.get("properties", {})
        if props.get("zone_id") != zid:
            errs.append(("V-Z-05", f"feature {zid}: zone_id {props.get('zone_id')!r} != id"))
        if zid in ids:
            errs.append(("V-Z-05", f"duplicate zone_id {zid}"))
        ids.add(zid)
        zmin, zmax = props.get("min_z_m"), props.get("max_z_m")
        if zmin is not None and zmax is not None and not zmin < zmax:
            errs.append(("V-Z-05", f"{zid}: min_z_m {zmin} >= max_z_m {zmax}"))
        if props.get("kind") in ("nofly", "restricted"):
            n_restr += 1
        for poly in polygons_of(f["geometry"]):
            for ri, ring in enumerate(poly):
                if len(ring) < 4 or ring[0] != ring[-1]:
                    errs.append(("V-Z-03", f"{zid}: ring {ri} not closed"))
                    continue
                if len(ring) - 1 > MAX_RING_VERTICES:
                    errs.append(("V-Z-03", f"{zid}: ring {ri} has {len(ring) - 1} vertices > {MAX_RING_VERTICES}"))
                a2 = ring_area2(ring)
                if ri == 0 and a2 <= 0:
                    errs.append(("V-Z-03", f"{zid}: outer ring is not counter-clockwise"))
                if ri > 0 and a2 >= 0:
                    errs.append(("V-Z-03", f"{zid}: hole ring {ri} is not clockwise"))
                if len(ring) - 1 <= MAX_RING_VERTICES and ring_self_intersects(ring):
                    errs.append(("V-Z-03", f"{zid}: ring {ri} self-intersects"))
                if bounds_min is not None:
                    a = np.asarray(ring, np.float64)
                    if (a[:, 0].min() < bounds_min[0] - tol or a[:, 0].max() > bounds_max[0] + tol
                            or a[:, 1].min() < bounds_min[1] - tol or a[:, 1].max() > bounds_max[1] + tol):
                        errs.append(("V-Z-04", f"{zid}: vertices outside world bounds"))
    if n_restr > MAX_ZONES:
        errs.append(("V-Z-03", f"{n_restr} nofly/restricted zones > {MAX_ZONES}"))
    return errs


# ---------------------------------------------------------------- 派生


def border_feature(bounds_min, bounds_max, dsm_max_m: float, inset_m: float = 20.0, headroom_m: float = 50.0) -> dict:
    x0, y0 = round(bounds_min[0] + inset_m, 3) + 0.0, round(bounds_min[1] + inset_m, 3) + 0.0
    x1, y1 = round(bounds_max[0] - inset_m, 3) + 0.0, round(bounds_max[1] - inset_m, 3) + 0.0
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {"type": "Feature", "id": "border",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"zone_id": "border", "kind": "border", "min_z_m": None,
                           "max_z_m": round(float(dsm_max_m) + headroom_m, 2),
                           "label": "World border", "label_zh": "世界边界", "origin": "derived", "editable": False}}


def load_curated(path: Path, world_id: str) -> tuple[list[dict], str]:
    """读取并检查人工整理的区域文件；返回 (要素列表, 文件 sha256)。不合法时 ConfigError（退出码 3）。"""
    data = Path(path).read_bytes()
    try:
        fc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConfigError(f"{path}: 不是合法 JSON：{e}") from e
    from ..package.schemas import schema_errors

    if isinstance(fc, dict) and fc.get("features") == []:           # 空集合（例如苏州）：只检查 awr 块
        probe = dict(fc, features=[border_feature([0, 0], [100, 100], 0.0)])
        errs = schema_errors(probe, "zones.schema.json")
        if errs:
            raise ConfigError(f"{path}: 不符合 zones.schema.json：{errs[0]}")
        if fc["awr"].get("world_id") != world_id:
            raise ConfigError(f"{path}: awr.world_id {fc['awr'].get('world_id')!r} != {world_id}")
        return [], hashlib.sha256(data).hexdigest()
    errs = schema_errors(fc, "zones.schema.json")
    if errs:
        raise ConfigError(f"{path}: 不符合 zones.schema.json：{errs[0]}")
    if fc["awr"].get("world_id") != world_id:
        raise ConfigError(f"{path}: awr.world_id {fc['awr'].get('world_id')!r} != {world_id}")
    feats = fc["features"]
    for f in feats:
        if f["properties"]["kind"] == "border" or f["id"] == "border":
            raise ConfigError(f"{path}: 人工整理文件不得包含 border")
        if f["properties"].get("origin") != "curated":
            raise ConfigError(f"{path}: 要素 {f['id']} 的 origin 必须为 curated")
    geo = [e for e in check_zone_geometry(fc) if e[0] in ("V-Z-03", "V-Z-05")]
    if geo:
        raise ConfigError(f"{path}: {geo[0][0]} {geo[0][1]}")
    return feats, hashlib.sha256(data).hexdigest()


def build_zones(world_id: str, coordinate_sha256: str, bounds_min, bounds_max, dsm_max_m: float, *,
                curated_path: Path | None, inset_m: float = 20.0, headroom_m: float = 50.0) -> tuple[dict, str | None]:
    feats = [border_feature(bounds_min, bounds_max, dsm_max_m, inset_m, headroom_m)]
    src_sha = None
    if curated_path is not None and Path(curated_path).exists():
        cur, src_sha = load_curated(curated_path, world_id)
        feats.extend(cur)
    fc = {"type": "FeatureCollection",
          "awr": {"schema": "awr.zones.v1", "schema_version": "1.0.0", "world_id": world_id, "frame": "world", "units": "m",
                  "coordinate_sha256": coordinate_sha256, "source_sha256": src_sha},
          "features": feats}
    errs = check_zone_geometry(fc, bounds_min, bounds_max)
    if errs:
        raise ConfigError(f"zones.geojson: {errs[0][0]} {errs[0][1]}")
    return fc, src_sha


def derive_zones(ctx: DeriveContext) -> list[LayerSpec]:
    p = ctx.params
    cur = curated_zones_path(ctx.repo_root, ctx.world_id)
    fc, src_sha = build_zones(ctx.world_id, ctx.coordinate_sha256, ctx.bounds_min, ctx.bounds_max, ctx.dsm_max_m,
                              curated_path=cur, inset_m=float(p.border_inset_m), headroom_m=float(p.border_headroom_m))
    write_json(ctx.stage / ZONES_HREF, fc)
    ctx.zones_source_sha256 = src_sha
    if src_sha is not None:
        ctx.inputs.append({"name": f"scenarios/zones/{ctx.world_id}.zones.geojson", "bytes": cur.stat().st_size,
                           "sha256": src_sha})
    return [LayerSpec("semantic.zones", "semantic-zones", "semantic", "geojson/awr-zones@1", ZONES_HREF, True,
                      files=[ZONES_HREF])]


# ---------------------------------------------------------------- M16 §6.2.4 的示例区域（供 M16 定稿 curated 文件）

M16_EXAMPLE_ZONES: dict[str, list[tuple[str, str, tuple[float, float], float, str]]] = {
    "shenzhen": [("nofly-sz-t2", "nofly", (-98.0, 346.5), 60.0, "No-fly: tower T2"),
                 ("restricted-sz-t3", "restricted", (110.0, 162.5), 50.0, "Restricted: tower T3")],
    "shanghai": [("nofly-sh-pearl", "nofly", (-3427.2, 1547.6), 100.0, "No-fly: Oriental Pearl")],
    "newyork": [("nofly-ny-70pine", "nofly", (-434.1, -741.3), 60.0, "No-fly: 70 Pine Street")],
    "sanfrancisco": [("nofly-sf-sutro", "nofly", (-2795.9, -2612.5), 120.0, "No-fly: Sutro Tower")],
    "chicago": [("nofly-chi-hancock", "nofly", (-15.0, 1636.0), 80.0, "No-fly: John Hancock Center")],
    "suzhou": [],
}
_LABEL_ZH = {"nofly": "禁飞区", "restricted": "限制区"}


def circle_ring(center: tuple[float, float], r: float, n: int = 32) -> list[list[float]]:
    """逆时针 n 边形（首尾闭合），坐标保留 3 位小数。"""
    pts = [[round(center[0] + r * math.cos(2 * math.pi * k / n), 3) + 0.0,
            round(center[1] + r * math.sin(2 * math.pi * k / n), 3) + 0.0] for k in range(n)]
    return [*pts, pts[0]]


def example_curated(world_id: str) -> dict:
    """按 M16 §6.2.4 表生成 curated 区域草稿（`coordinate_sha256` 与 `source_sha256` 由构建时决定，草稿写占位）。"""
    feats = []
    for zid, kind, c, r, label in M16_EXAMPLE_ZONES.get(world_id, []):
        feats.append({"type": "Feature", "id": zid, "geometry": {"type": "Polygon", "coordinates": [circle_ring(c, r)]},
                      "properties": {"zone_id": zid, "kind": kind, "min_z_m": None, "max_z_m": None, "label": label,
                                     "label_zh": _LABEL_ZH[kind], "origin": "curated", "editable": False}})
    return {"type": "FeatureCollection",
            "awr": {"schema": "awr.zones.v1", "schema_version": "1.0.0", "world_id": world_id, "frame": "world",
                    "units": "m", "coordinate_sha256": "0" * 64, "source_sha256": None},
            "features": feats}
