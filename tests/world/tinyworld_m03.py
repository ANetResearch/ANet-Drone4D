"""tiny world 夹具（M03 §10.1）：20 万点合成城市，按已知的单位 ×10、Y-up、2° 倾斜、+90° 北向与原点偏移写成
UrbanScene3D 布局的 PLY（binary_little_endian，x y z nx ny nz float32），期望值由构造给出。

场景（真值帧 = 规范化后的 world ENU）：平地 z = 0（1000 m × 800 m，四角放置锚定点使包围盒中心为原点），10 栋长方体楼，
1 座 250 m 塔（地标），5% 的水平面法线朝下。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from pathlib import Path

import numpy as np

from awr.world.ingest.normalize import UP_ROT, exact_rz
from awr.world.ingest.rawply import read_ply
from awr.world.ingest.types import IngestConfig, Landmark, RawCloud

HALF_X, HALF_Y = 500.0, 400.0
TOWER_XY = (120.0, -80.0)
TOWER_SIZE = 20.0
TOWER_H = 250.0
UNITS = 10.0
TILT_DEG = 2.0
YAW_DEG = 90.0
SHIFT = np.array([1234.5, -567.25, 89.0])     # 源帧中的任意平移（规范化时由原点重定吸收）

BASE_CFG = IngestConfig(
    "tiny", "dataset", UNITS, "+y", True, YAW_DEG, "verified", "landmark",
    Landmark("Tiny Tower", 31.0, 121.0, 5.0), None,
    evidence=("synthetic tower 250 m",), north_evidence=("synthetic",))


def _box(rng, cx, cy, sx, sy, h, n):
    """长方体楼：屋顶 + 四个立面；返回 (P, N)。"""
    n_roof = n // 3
    n_fac = n - n_roof
    P = [np.c_[rng.uniform(cx - sx / 2, cx + sx / 2, n_roof), rng.uniform(cy - sy / 2, cy + sy / 2, n_roof), np.full(n_roof, h)]]
    N = [np.tile([0.0, 0.0, 1.0], (n_roof, 1))]
    per = n_fac // 4
    for k, (nx, ny) in enumerate(((1, 0), (-1, 0), (0, 1), (0, -1))):
        m = per if k < 3 else n_fac - 3 * per
        if nx:
            x = np.full(m, cx + nx * sx / 2)
            y = rng.uniform(cy - sy / 2, cy + sy / 2, m)
        else:
            x = rng.uniform(cx - sx / 2, cx + sx / 2, m)
            y = np.full(m, cy + ny * sy / 2)
        P.append(np.c_[x, y, rng.uniform(0.5, h - 0.5, m)])
        N.append(np.tile([float(nx), float(ny), 0.0], (m, 1)))
    return np.concatenate(P), np.concatenate(N)


def true_cloud(n: int = 200_000, seed: int = 7, tower_h: float = TOWER_H) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    n_ground = n // 2
    Pg = np.c_[rng.uniform(-HALF_X, HALF_X, n_ground), rng.uniform(-HALF_Y, HALF_Y, n_ground), np.zeros(n_ground)]
    Pg[:4, :2] = [[-HALF_X, -HALF_Y], [HALF_X, -HALF_Y], [HALF_X, HALF_Y], [-HALF_X, HALF_Y]]
    Ng = np.tile([0.0, 0.0, 1.0], (n_ground, 1))
    parts_P, parts_N = [Pg], [Ng]
    centers = [(-350, 250), (-200, 250), (-50, 250), (100, 250), (300, 250), (-350, -250), (-200, -250), (-50, -250),
               (300, -250), (-300, 0)]
    heights = [20, 35, 50, 65, 80, 25, 40, 55, 70, 30]
    n_tower = max(200, n // 20)
    n_bld = (n - n_ground - n_tower) // 10
    for (cx, cy), h in zip(centers, heights, strict=True):
        P, N = _box(rng, cx, cy, 40.0, 30.0, h, n_bld)
        parts_P.append(P)
        parts_N.append(N)
    rest = n - sum(len(p) for p in parts_P)
    P, N = _box(rng, TOWER_XY[0], TOWER_XY[1], TOWER_SIZE, TOWER_SIZE, tower_h, rest)
    P[0] = [TOWER_XY[0], TOWER_XY[1], tower_h]                  # 塔心屋顶点（最高 HAG 点）
    parts_P.append(P)
    parts_N.append(N)
    P = np.concatenate(parts_P)
    N = np.concatenate(parts_N)
    horiz = np.flatnonzero(N[:, 2] > 0.5)
    flip = rng.choice(horiz, int(0.05 * len(horiz)), replace=False)
    flip = flip[flip >= 4]
    N[flip] *= -1                                                # 5% 水平面法线朝下
    return P, N


def _mat(A) -> np.ndarray:
    return np.asarray(A, np.float64)


def tilt_matrix(deg: float = TILT_DEG) -> np.ndarray:
    t = math.radians(deg)
    return np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)], [0, math.sin(t), math.cos(t)]])


def to_source(P: np.ndarray, N: np.ndarray, *, units: float = UNITS, up: str = "+y", tilt_deg: float = TILT_DEG,
              yaw_deg: float = YAW_DEG, shift: np.ndarray = SHIFT) -> tuple[np.ndarray, np.ndarray]:
    """真值 world → 源帧：p_src = R_upᵀ · T · Rz(−yaw) · (p + shift) / units（法线不含尺度与平移）。"""
    Rz_inv = _mat(exact_rz(-yaw_deg))
    T = tilt_matrix(tilt_deg)
    Ru_t = _mat(UP_ROT[up]).T
    M = Ru_t @ T @ Rz_inv
    return ((P + shift) @ M.T) / units, N @ M.T


def write_ply(path: Path, P: np.ndarray, N: np.ndarray, *, props: tuple[str, ...] = ("x", "y", "z", "nx", "ny", "nz")) -> str:
    header = f"ply\nformat binary_little_endian 1.0\ncomment tiny world\nelement vertex {len(P)}\n"
    header += "".join(f"property float {p}\n" for p in props) + "end_header\n"
    body = np.c_[P, N].astype("<f4").tobytes()
    data = header.encode("ascii") + body
    Path(path).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


class TinyAdapter:
    """测试用 IngestAdapter：读取 tiny world PLY（字节数与 sha256 在生成时记录）。"""

    kind = "urbanscene3d"

    def __init__(self, ply: Path, sha: str, cfg: IngestConfig = BASE_CFG, world_id: str | None = None):
        self.ply = Path(ply)
        self.sha = sha
        self.cfg = replace(cfg, world_id=world_id) if world_id else cfg
        self._files: list[dict] = []

    def config(self) -> IngestConfig:
        return self.cfg

    def raw_path(self) -> Path:
        return self.ply

    def load(self) -> RawCloud:
        xyz, nrm, info, sha = read_ply(self.ply, expect_sha256=self.sha, expect_bytes=self.ply.stat().st_size)
        self._files = [{"name": self.ply.name, "bytes": info.file_size, "points": info.n, "sha256": sha,
                        "header_bytes": info.header_bytes}]
        return RawCloud(xyz=xyz, normal=nrm, files=list(self._files))

    def provenance(self) -> dict:
        files = self._files or [{"name": self.ply.name, "bytes": self.ply.stat().st_size, "sha256": self.sha}]
        return {"name": "tiny", "version": "1", "url": "https://example.invalid/tiny", "citation": "synthetic",
                "license": "test fixture", "redistribution": True, "notice": "test",
                "sourceFiles": [{"name": f["name"], "bytes": f["bytes"], "sha256": f["sha256"]} for f in files]}

    def manifest_extras(self) -> dict:
        return {"name": f"{self.cfg.world_id} (tiny)", "nameZh": "测试世界", "description": "tiny synthetic world",
                "tags": ["synthetic"], "source_dataset": "tiny synthetic", "default_color_mode": "height"}


def make_tiny(dirpath: Path, *, n: int = 200_000, tower_h: float = TOWER_H, name: str = "tiny.ply") -> tuple[Path, str]:
    P, N = true_cloud(n, tower_h=tower_h)
    Ps, Ns = to_source(P, N)
    p = Path(dirpath) / name
    return p, write_ply(p, Ps, Ns)
