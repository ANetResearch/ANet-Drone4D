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
自动验收（§6.7.4）在本模块只做 A1（`gz sdf -k`，本机有 gz 时）与 A7（幂等，测试覆盖）；A5 落地探测在 dmax 锁步测试中执行，
A2–A4、A6 属 `xval` 导出档（P1）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

__all__ = ["EXPORTER_VERSION", "MAX_FACES", "GzExport", "GzExportError", "export_world", "load_export"]

EXPORTER_VERSION = "0.1.0"
MAX_FACES = 50_000
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
        r = subprocess.run(["gz", "sdf", "-k", str(stage / MODEL / f"{MODEL}.sdf")], capture_output=True, text=True, check=False)
        if r.returncode != 0:
            shutil.rmtree(stage, ignore_errors=True)
            raise GzExportError(636, f"A1 gz sdf -k failed: {(r.stdout + r.stderr).strip()[:400]}")
        qa["A1_sdf_check"] = "pass"
    a = coord.get("anchor", {})
    manifest = {
        "schema": "drone4d.gz_export.v1", "exporter_version": EXPORTER_VERSION, "profile": profile, "world_id": head["id"],
        "content_version": head["contentVersion"], "coordinate_sha256": idx.get("coordinate_sha256"),
        "collision_source": "raycast", "max_faces_per_chunk": max_faces, "model": MODEL,
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
