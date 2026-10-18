"""World Package → gz-sim 世界级导出（M24 §6.7、FR-025；`physics` 导出档）。

碰撞取 `mesh.raycast`（`awr-trimesh@1`，P0a 尚无减面碰撞层，`collision_source = raycast`）：每瓦片按连续三角形区间切成不超过
`max_faces` 面的块，块内顶点保持瓦片局部 float32 坐标，写为二进制 STL，SDF 中以瓦片原点为碰撞位姿（world ENU，单位旋转），
因此远离原点的瓦片不损失 float32 精度。产物是一个可 `include` 的模型目录 `world_static/`（model.config、world_static.sdf、
meshes/）与 `gz_manifest.json`：

    worlds/<id>/export/gz/<contentVersion>-<exporter_version>/physics/
        world_static/{model.config, world_static.sdf, meshes/c0000.stl ...}
        gz_manifest.json

运行级组合以 `model://world_static` 引用（GZ_SIM_RESOURCE_PATH 加入 `physics/`；容器内挂载为 /opt/d4d/worlds）。
同一输入与导出器版本产物逐字节相同（不写时间戳，STL 头固定，法线以 float64 计算后转 float32）；先写 staging 目录再原子改名。
自动验收（§6.7.4、D3-AC-34）在本模块执行：A1（`gz sdf -k world_static.sdf`，本机有 gz 时）；碰撞同点偏差（`physics` 档的 A2 口径：
视觉层在 P1 前不导出，以 `mesh.raycast` 为参照，见 `collision_deviation`），最大偏差 ≤ 1 cm、未匹配率 ≤ 0.1%，任一失败返回 636 且
不发布；A7（幂等，测试覆盖）。环境重力 `GRAVITY_MPS2` 由运行级组合写为显式 `<gravity>`（awr.phys.gz.compose），清单记录其值供对拍；
样例 run.sdf 的 A1、A5 落地探测与自由落体重力实测在 dmax 的 d3-gz 测试中执行（tests/d3/phys/test_gz_export_checks.py），
A3、A4、A6 属 `xval` 导出档（P1）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

import numpy as np

__all__ = ["DEVIATION_MAX_M", "EXPORTER_VERSION", "GRAVITY_MPS2", "MAX_FACES", "UNMATCHED_MAX", "GzExport", "GzExportError",
           "collision_deviation", "export_world", "load_export", "sdf_check"]

EXPORTER_VERSION = "0.1.1"                      # 0.1.1：清单 qa 增加碰撞同点偏差与环境重力（STL 与 SDF 字节不变）
MAX_FACES = 50_000
GRAVITY_MPS2 = 9.80665                          # 标准重力（§6.7.1 环境行），与 awr.phys.gz.compose.G、PoseHistory G_W 一致
DEVIATION_SAMPLES = 10_000                      # A2：均匀抽样的碰撞顶点数
DEVIATION_RADIUS_M = 0.05                       # A2：最近邻搜索半径
DEVIATION_MAX_M = 0.01                          # A2：最大偏差阈值（D3-AC-34 "同点偏差 ≤ 1 cm"）
UNMATCHED_MAX = 0.001                           # A2：未匹配率阈值
MODEL = "world_static"
STL_HEADER = b"drone4d world_static awr-trimesh@1".ljust(80, b"\0")
_STL = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])


class GzExportError(Exception):
    """600 世界未标定；636 导出验收未通过。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class GzExport:
    root: Path                                  # .../physics（加入 GZ_SIM_RESOURCE_PATH）
    profile: str
    manifest: dict = field(default_factory=dict)

    @property
    def model_dir(self) -> Path:
        return self.root / MODEL

    @property
    def uri(self) -> str:
        return f"model://{MODEL}"

    @property
    def spherical(self) -> dict | None:
        return self.manifest.get("spherical_coordinates")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def stl_bytes(V: np.ndarray, F: np.ndarray) -> bytes:
    """二进制 STL（确定性）：V (nv, 3) float32、F (nt, 3) 索引。"""
    tri = V[F].astype(np.float64)
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(n, axis=1)
    n = np.divide(n, ln[:, None], out=np.zeros_like(n), where=ln[:, None] > 0)
    rec = np.zeros(len(F), _STL)
    rec["n"] = n.astype(np.float32)
    rec["v"] = V[F]
    return STL_HEADER + np.uint32(len(F)).tobytes() + rec.tobytes()


def _chunks(idx: dict, xyz: np.ndarray, tri: np.ndarray, max_faces: int):
    for t in idx["tiles"]:
        V = xyz[t["v0"]:t["v0"] + t["nv"]]
        F = tri[t["t0"]:t["t0"] + t["nt"]]
        for a in range(0, len(F), max_faces):
            f = F[a:a + max_faces]
            used, inv = np.unique(f.ravel(), return_inverse=True)
            yield t, np.ascontiguousarray(V[used]), inv.reshape(-1, 3).astype(np.uint32)


def _f(v) -> str:
    return " ".join(repr(float(x)) for x in v)


def _render_sdf(cols: list[dict]) -> str:
    items = "".join(f"""
      <collision name="{c['name']}">
        <pose>{_f(c['origin_m'])} 0 0 0</pose>
        <geometry><mesh><uri>model://{MODEL}/{c['href']}</uri></mesh></geometry>
      </collision>""" for c in cols)
    return f"""<?xml version="1.0" ?>
<sdf version="1.9">
  <model name="{MODEL}">
    <static>true</static>
    <link name="mesh">{items}
    </link>
  </model>
</sdf>
"""


_CONFIG = f"""<?xml version="1.0" ?>
<model>
  <name>{MODEL}</name>
  <version>1.0</version>
  <sdf version="1.9">{MODEL}.sdf</sdf>
  <description>World collision exported by awr.world.export.gz (physics profile).</description>
</model>
"""


def sdf_check(path: Path, *, resource_paths: tuple[Path, ...] = ()) -> tuple[bool, str]:
    """A1：`gz sdf -k <path>`。`gz sdf` 只按 SDF_PATH 解析 `model://`（不读 GZ_SIM_RESOURCE_PATH），因此 SDF_PATH 取
    SDF_PATH + GZ_SIM_RESOURCE_PATH + `resource_paths`（转绝对路径，见 GzInstance 的相对路径说明）。"""
    env = dict(os.environ)
    extra = [str(Path(p).resolve()) for p in resource_paths]
    env["GZ_SIM_RESOURCE_PATH"] = ":".join(p for p in (env.get("GZ_SIM_RESOURCE_PATH", ""), *extra) if p)
    env["SDF_PATH"] = ":".join(p for p in (env.get("SDF_PATH", ""), env["GZ_SIM_RESOURCE_PATH"]) if p)
    r = subprocess.run(["gz", "sdf", "-k", str(path)], capture_output=True, text=True, check=False, env=env)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _rot_rpy(r: float, p: float, y: float) -> np.ndarray:
    """SDF `<pose>` 的固定轴 roll-pitch-yaw：R = Rz(y)·Ry(p)·Rx(r)。"""
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(y), np.sin(y)
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp, cp * sr, cp * cr]])


def _read_collisions(model_dir: Path) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, Path]]:
    """从已写出的 world_static.sdf 独立回读每个 `<collision>`：(R, t, scale, STL 路径)；不依赖导出时的中间量。"""
    root = ET.parse(model_dir / f"{MODEL}.sdf").getroot()
    out = []
    for c in root.findall("model/link/collision"):
        pose = [float(x) for x in (c.findtext("pose") or "0 0 0 0 0 0").split()]
        mesh = c.find("geometry/mesh")
        if len(pose) != 6 or mesh is None:
            raise GzExportError(636, f"collision {c.get('name')!r}: pose or mesh geometry missing")
        uri = mesh.findtext("uri") or ""
        pre = f"model://{MODEL}/"
        if not uri.startswith(pre):
            raise GzExportError(636, f"collision {c.get('name')!r}: uri {uri!r} is outside model://{MODEL}")
        sc = np.array([float(x) for x in (mesh.findtext("scale") or "1 1 1").split()], np.float64)
        out.append((_rot_rpy(*pose[3:]), np.asarray(pose[:3], np.float64), sc, model_dir / uri[len(pre):]))
    return out


def _stl_corners(path: Path) -> np.ndarray:
    """二进制 STL → (3·n, 3) float32 三角形顶点（按记录顺序）；长度与面数不符时 636。"""
    raw = path.read_bytes()
    n = int(np.frombuffer(raw[80:84], "<u4")[0]) if len(raw) >= 84 else -1
    if n < 0 or len(raw) != 84 + 50 * n:
        raise GzExportError(636, f"{path.name}: binary STL size does not match its face count")
    return np.frombuffer(raw, _STL, count=n, offset=84)["v"].reshape(-1, 3)


def _pack_keys(k: np.ndarray, lo: np.ndarray, span: np.ndarray) -> np.ndarray:
    k = k - lo
    return (k[:, 0] * span[1] + k[:, 1]) * span[2] + k[:, 2]


def _nearest_within(ref: np.ndarray, q: np.ndarray, radius: float) -> np.ndarray:
    """q 中每点到 ref 最近点的距离（> radius 记 inf）；均匀格哈希（格长 = radius，查 27 邻格），只用 numpy。"""
    kr = np.floor(ref / radius).astype(np.int64)
    kq = np.floor(q / radius).astype(np.int64)
    lo = np.minimum(kr.min(0), kq.min(0)) - 1
    span = np.maximum(kr.max(0), kq.max(0)) - lo + 2
    keys = _pack_keys(kr, lo, span)
    order = np.argsort(keys, kind="stable")
    sk = keys[order]
    best = np.full(len(q), np.inf)
    for off in product((-1, 0, 1), repeat=3):
        kk = _pack_keys(kq + np.asarray(off, np.int64), lo, span)
        a = np.searchsorted(sk, kk, "left")
        cnt = np.searchsorted(sk, kk, "right") - a
        tot = int(cnt.sum())
        if tot == 0:
            continue
        qi = np.repeat(np.arange(len(q)), cnt)
        ri = order[np.repeat(a, cnt) + (np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt))]
        np.minimum.at(best, qi, np.linalg.norm(ref[ri] - q[qi], axis=1))
    best[best > radius] = np.inf
    return best


def collision_deviation(model_dir: Path, raycast_dir: Path, *, samples: int = DEVIATION_SAMPLES, radius_m: float = DEVIATION_RADIUS_M,
                        seed: int = 0) -> dict:
    """碰撞 STL 与 `mesh.raycast` 网格的同点偏差（§6.7.4 A2 方法，D3-AC-34）。

    自已写出的 SDF 与 STL 独立回读：对全部碰撞三角形顶点均匀抽样 `samples` 个（固定种子，不放回），按 SDF `<pose>` 与 `<scale>`
    以 float64 变换到 world ENU；参照点为 raycast 顶点（瓦片局部 float32 + 瓦片原点，float64），在半径 `radius_m` 内做最近邻。
    返回 {samples, max_m, p99_m, mean_m, unmatched_rate, radius_m, threshold_m, pass}；未匹配样本不计入偏差统计。
    """
    raycast_dir = Path(raycast_dir)
    idx = json.loads((raycast_dir / "index.json").read_text(encoding="utf-8"))
    xyz = np.fromfile(raycast_dir / "xyz.f32", "<f4").reshape(-1, 3)
    ref = np.empty(xyz.shape, np.float64)
    for t in idx["tiles"]:
        sl = slice(t["v0"], t["v0"] + t["nv"])
        ref[sl] = xyz[sl].astype(np.float64) + np.asarray(t["origin_m"], np.float64)
    cols = _read_collisions(Path(model_dir))
    corners = []
    for *_x, path in cols:
        with path.open("rb") as fh:
            head = fh.read(84)
        corners.append(3 * int(np.frombuffer(head[80:84], "<u4")[0]) if len(head) == 84 else 0)
    total = int(sum(corners))
    if total == 0:
        raise GzExportError(636, "collision model has no triangles")
    pick = np.sort(np.random.default_rng(seed).choice(total, size=min(samples, total), replace=False))
    bounds = np.cumsum([0, *corners])
    pts = []
    for i, (R, tr, sc, path) in enumerate(cols):
        sel = pick[(pick >= bounds[i]) & (pick < bounds[i + 1])] - bounds[i]
        if len(sel):
            V = _stl_corners(path)[sel].astype(np.float64) * sc
            pts.append(V @ R.T + tr)
    q = np.concatenate(pts)
    d = _nearest_within(ref, q, radius_m)
    ok = np.isfinite(d)
    unmatched = float(1.0 - ok.mean())
    dm = d[ok]
    mx = float(dm.max()) if len(dm) else float("inf")
    return {"samples": len(q), "max_m": mx, "p99_m": float(np.quantile(dm, 0.99)) if len(dm) else float("inf"),
            "mean_m": float(dm.mean()) if len(dm) else float("inf"), "unmatched_rate": unmatched, "radius_m": radius_m,
            "threshold_m": DEVIATION_MAX_M, "pass": bool(mx <= DEVIATION_MAX_M and unmatched <= UNMATCHED_MAX)}


def load_export(root: Path) -> GzExport:
    m = json.loads((Path(root) / "gz_manifest.json").read_text(encoding="utf-8"))
    return GzExport(Path(root), m["profile"], m)


def export_world(world_dir: Path, profile: str = "physics", *, out_root: Path | None = None, max_faces: int = MAX_FACES,
                 check_sdf: bool | None = None, force: bool = False) -> GzExport:
    """导出世界级 `world_static`；已存在且未要求 `force` 时直接返回。`check_sdf` 缺省为本机有 `gz` 时执行 A1。"""
    if profile != "physics":
        raise GzExportError(636, f"export profile {profile!r} is not available before P1 (physics only)")
    world_dir = Path(world_dir)
    head = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    coord = json.loads((world_dir / "coordinate.json").read_text(encoding="utf-8"))
    if coord.get("scaleStatus") not in ("landmark", "assumed"):
        raise GzExportError(600, f"world {head['id']} scaleStatus {coord.get('scaleStatus')!r} is not metric")
    lay = next((L for L in head.get("layers", []) if L.get("role") == "geometry" and L.get("type") == "mesh"
                and L.get("format") == "awr-trimesh@1"), None)
    if lay is None:
        raise GzExportError(636, f"world {head['id']} has no awr-trimesh@1 geometry layer")
    base = Path(out_root) if out_root else world_dir / "export" / "gz" / f"{head['contentVersion']}-{EXPORTER_VERSION}"
    final = base / profile
    if final.exists() and not force:
        return load_export(final)
    src = world_dir / lay["href"]
    idx = json.loads((src / "index.json").read_text(encoding="utf-8"))
    xyz = np.fromfile(src / "xyz.f32", "<f4").reshape(-1, 3)
    tri = np.fromfile(src / "tri.u32", "<u4").reshape(-1, 3)
    if len(xyz) != idx["counts"]["vertices"] or len(tri) != idx["counts"]["triangles"]:
        raise GzExportError(636, "mesh.raycast stream sizes do not match index.json counts")

    stage = base / f"{profile}.staging-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / MODEL / "meshes").mkdir(parents=True)
    cols, files = [], {}
    for i, (t, V, F) in enumerate(_chunks(idx, xyz, tri, max_faces)):
        href = f"meshes/c{i:04d}.stl"
        b = stl_bytes(V, F)
        (stage / MODEL / href).write_bytes(b)
        files[f"{MODEL}/{href}"] = _sha(b)
        cols.append({"name": f"c{i:04d}", "href": href, "tile": int(t["id"]), "origin_m": [float(x) for x in t["origin_m"]],
                     "triangles": len(F), "vertices": len(V)})
    sdf = _render_sdf(cols).encode()
    (stage / MODEL / f"{MODEL}.sdf").write_bytes(sdf)
    (stage / MODEL / "model.config").write_text(_CONFIG, encoding="utf-8")
    files[f"{MODEL}/{MODEL}.sdf"] = _sha(sdf)
    files[f"{MODEL}/model.config"] = _sha(_CONFIG.encode())

    qa = {"A1_sdf_check": "skipped"}
    if check_sdf is None:
        check_sdf = shutil.which("gz") is not None
    if check_sdf:
        ok, msg = sdf_check(stage / MODEL / f"{MODEL}.sdf")
        if not ok:
            shutil.rmtree(stage, ignore_errors=True)
            raise GzExportError(636, f"A1 gz sdf -k failed: {msg[:400]}")
        qa["A1_sdf_check"] = "pass"
    dev = collision_deviation(stage / MODEL, src)
    qa["A2_collision_deviation"] = {k: (round(v, 9) if isinstance(v, float) else v) for k, v in dev.items()}
    if not dev["pass"]:
        shutil.rmtree(stage, ignore_errors=True)
        raise GzExportError(636, f"A2 collision vs raycast deviation max {dev['max_m']:.4f} m, unmatched {dev['unmatched_rate']:.4%} "
                                 f"(thresholds {DEVIATION_MAX_M} m, {UNMATCHED_MAX:.1%})")
    a = coord.get("anchor", {})
    manifest = {
        "schema": "drone4d.gz_export.v1", "exporter_version": EXPORTER_VERSION, "profile": profile, "world_id": head["id"],
        "content_version": head["contentVersion"], "coordinate_sha256": idx.get("coordinate_sha256"),
        "collision_source": "raycast", "max_faces_per_chunk": max_faces, "model": MODEL,
        "environment": {"gravity_mps2": [0.0, 0.0, -GRAVITY_MPS2]},
        "counts": {"chunks": len(cols), "triangles": int(sum(c["triangles"] for c in cols)), "tiles": len(idx["tiles"])},
        "spherical_coordinates": {"latitude_deg": a.get("latDeg"), "longitude_deg": a.get("lonDeg"),
                                  "elevation_m": a.get("hEllipsoidM"), "heading_deg": 0.0, "georeferenced": bool(a.get("georeferenced"))},
        "aabb_m": {"min": [min(t["aabb_m"]["min"][k] for t in idx["tiles"]) for k in range(3)],
                   "max": [max(t["aabb_m"]["max"][k] for t in idx["tiles"]) for k in range(3)]},
        "collisions": cols, "files": dict(sorted(files.items())), "qa": qa,
    }
    (stage / "gz_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if final.exists():
        shutil.rmtree(final)
    os.replace(stage, final)
    return GzExport(final, profile, manifest)
