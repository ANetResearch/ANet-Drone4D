"""解析场 AWSL 流线（D1-ext；M07-FR-048；M07 §6.8.3；g06 §7.4；16 §8.6；n04 §3.3.1）。

对解析均值场 M(p) = f(z_agl)·e(theta)（单位参考风速）播种：世界 AABB 内均匀取 xy、避开 DSM 实体，高度
z = dtm + zmin + (zmax − zmin)·r^1.9（AGL 2–150 m，近地更密），RK4 步长 12 m，进入实体、出界或满 64 点停止，少于 8 点丢弃，
线序打乱（种子固定）。每个顶点 (x, y, z, tau_hat, s_hat)：tau_hat = ∫ ds / |M|（参考风速 1 m/s 时的"飞行距离"，m），
前端相位 phi = fract((tau_hat − S) / 48)，S 为锚点 ∫ speed_ref dt，风速变化时几何不变、相位连续；s_hat = |M|。
场 id：`analytic-<hex8>`，hex8 = sha256("awsl|<world_id>|<coordinate_sha256>|<profile 规范 JSON>|v=1") 前 8 位。
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from ..conventions import e
from .profile import profile_arr

__all__ = ["AWSL_MAGIC", "StreamlineCache", "analytic_field_id", "decode_awsl", "encode_awsl", "generate"]

AWSL_MAGIC = 0x4C535741  # 'AWSL'
HEADER = struct.Struct("<IHHIIffIIffII")
assert HEADER.size == 48
STEP_M = 12.0
MIN_PTS, MAX_PTS = 8, 64
Z_AGL = (2.0, 150.0)


def analytic_field_id(world_id: str, coordinate_sha256: str, profile: Mapping[str, Any]) -> str:
    prof = json.dumps(dict(profile), sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(f"awsl|{world_id}|{coordinate_sha256}|{prof}|v=1".encode()).hexdigest()[:8]
    return f"analytic-{h}"


def _field_version(field_id: str, dir_deg: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{field_id}|d={dir_deg}".encode()).digest()[:4], "little")


def generate(dir_from_deg: float, profile: Mapping[str, Any], bounds: tuple, *, ground: Callable[[np.ndarray], np.ndarray] | None = None,
             solid_top: Callable[[np.ndarray], np.ndarray] | None = None, n_lines: int = 1000, k: int = MAX_PTS,
             seed: int = 1) -> list[np.ndarray]:
    """-> 线列表，每条 (m, 5) float32：x, y, z, tau_hat, s_hat（m 在 [8, 64]）。"""
    rng = np.random.default_rng(seed)
    (x0, y0, _), (x1, y1, _) = bounds
    ex, ey = e(dir_from_deg)
    n_try = n_lines * 3
    xy = np.column_stack((rng.uniform(x0, x1, n_try), rng.uniform(y0, y1, n_try)))
    g = ground(xy) if ground is not None else np.zeros(n_try)
    z = g + Z_AGL[0] + (Z_AGL[1] - Z_AGL[0]) * rng.uniform(0, 1, n_try) ** 1.9
    ok = np.ones(n_try, bool) if solid_top is None else z > solid_top(xy) + 1.0
    idx = np.flatnonzero(ok)[:n_lines]
    p = np.column_stack((xy[idx], z[idx]))
    n = p.shape[0]
    pts = np.zeros((k, n, 5), np.float64)
    alive = np.ones(n, bool)
    length = np.zeros(n, np.int64)
    tau = np.zeros(n)

    def vel(q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        gq = ground(q[:, :2]) if ground is not None else np.zeros(q.shape[0])
        f = profile_arr(q[:, 2] - gq, profile)
        v = np.column_stack((f * ex, f * ey, np.zeros_like(f)))
        return v, f

    for i in range(k):
        _, f = vel(p)
        pts[i, :, :3] = p
        pts[i, :, 3] = tau
        pts[i, :, 4] = f
        length[alive] = i + 1
        if i == k - 1:
            break
        # RK4 on the unit-speed direction field (arc-length steps of 12 m)
        def unit(q: np.ndarray) -> np.ndarray:
            vv, _ = vel(q)
            nrm = np.linalg.norm(vv, axis=1, keepdims=True)
            return np.where(nrm > 1e-9, vv / np.maximum(nrm, 1e-9), 0.0)

        k1 = unit(p)
        k2 = unit(p + 0.5 * STEP_M * k1)
        k3 = unit(p + 0.5 * STEP_M * k2)
        k4 = unit(p + STEP_M * k3)
        q = p + STEP_M / 6.0 * (k1 + 2 * k2 + 2 * k3 + k4)
        still = (f > 1e-6) & (q[:, 0] >= x0) & (q[:, 0] <= x1) & (q[:, 1] >= y0) & (q[:, 1] <= y1)
        if solid_top is not None:
            still &= q[:, 2] > solid_top(q[:, :2])
        _, fq = vel(q)
        tau = tau + STEP_M / np.maximum(0.5 * (f + fq), 1e-6)
        alive &= still
        p = np.where(alive[:, None], q, p)
        if not alive.any():
            break
    lines = [pts[: length[j], j, :].astype(np.float32) for j in range(n) if length[j] >= MIN_PTS]
    order = rng.permutation(len(lines))
    return [lines[i] for i in order]


def encode_awsl(lines: list[np.ndarray], dir_from_deg: float, field_version: int, seed: int) -> bytes:
    n_lines = len(lines)
    offs = np.zeros(n_lines + 1, np.uint32)
    for i, ln in enumerate(lines):
        offs[i + 1] = offs[i] + ln.shape[0]
    n_verts = int(offs[-1])
    verts = np.concatenate(lines).astype("<f4") if lines else np.zeros((0, 5), "<f4")
    tau_max = float(verts[:, 3].max()) if n_verts else 0.0
    s_max = float(verts[:, 4].max()) if n_verts else 0.0
    hdr = HEADER.pack(AWSL_MAGIC, 1, 0b11, n_lines, n_verts, float(dir_from_deg), 1.0, field_version & 0xFFFFFFFF, 20, tau_max, s_max,
                      seed & 0xFFFFFFFF, 0)
    ob = offs.astype("<u4").tobytes()
    pad = (-len(ob)) % 8
    return hdr + ob + b"\0" * pad + verts.tobytes()


def decode_awsl(b: bytes) -> tuple[dict, np.ndarray, np.ndarray]:
    (magic, ver, flags, n_lines, n_verts, dirf, ref, fv, stride, tau_max, s_max, seed, _r) = HEADER.unpack_from(b, 0)
    if magic != AWSL_MAGIC or ver != 1 or stride != 20:
        raise ValueError("AWSL header invalid")
    ob = 4 * (n_lines + 1)
    offs = np.frombuffer(b, "<u4", n_lines + 1, 48)
    start = 48 + ob + ((-ob) % 8)
    verts = np.frombuffer(b, "<f4", n_verts * 5, start).reshape(n_verts, 5)
    if int(offs[-1]) != n_verts:
        raise ValueError("AWSL offsets invalid")
    return ({"flags": flags, "n_lines": n_lines, "n_verts": n_verts, "dir_from_deg": dirf, "ref_speed_mps": ref, "field_version": fv,
             "tau_hat_max_m": tau_max, "s_hat_max": s_max, "seed": seed}, offs, verts)


class StreamlineCache:
    """(field_id, round(dir)) LRU 16 份（M07 §6.8.3）。"""

    def __init__(self, cap: int = 16) -> None:
        self.cap = cap
        self._d: OrderedDict[tuple[str, int], bytes] = OrderedDict()

    def get(self, field_id: str, dir_deg: int, build: Callable[[], list[np.ndarray]], seed: int) -> bytes:
        key = (field_id, int(dir_deg) % 360)
        b = self._d.get(key)
        if b is None:
            b = encode_awsl(build(), float(key[1]), _field_version(field_id, key[1]), seed)
            self._d[key] = b
            while len(self._d) > self.cap:
                self._d.popitem(last=False)
        else:
            self._d.move_to_end(key)
        return b
