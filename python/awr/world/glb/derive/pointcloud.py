"""彩色点云采样（M22 §6.6.14；扩展点 6）：按面积加权在网格表面采样 N 点，颜色取外观纹理在采样点 UV 处的纹素（最近邻，sRGB），
法线取面法线。抽样以计数器随机数（SplitMix64，与 `awr.sim.sensors.cbrng` 同式；world 包不 import awr.sim）按
（seed、点序号、通道）派生，结果只取决于网格、种子与点数，与线程和批次划分无关。
"""

from __future__ import annotations

import io

import numpy as np

__all__ = ["sample_surface", "splitmix_uniform"]

_U = np.uint64
_C1, _C2, _C3 = _U(0x9E3779B97F4A7C15), _U(0xBF58476D1CE4E5B9), _U(0x94D049BB133111EB)
STREAM = 0x4D3232_5043          # "M22" "PC"


def _sm(z: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):
        z = z + _C1
        z = (z ^ (z >> _U(30))) * _C2
        z = (z ^ (z >> _U(27))) * _C3
        return z ^ (z >> _U(31))


def splitmix_uniform(seed: int, idx: np.ndarray, ch: int) -> np.ndarray:
    """U[0, 1)：键 sm(sm(sm(seed) ^ STREAM) ^ idx) ^ ch 再终混，取高 53 位。"""
    k0 = _sm(np.array([seed & ((1 << 64) - 1)], np.uint64))
    k1 = _sm(k0 ^ _U(STREAM))
    k = _sm(_sm(k1 ^ idx.astype(np.uint64)) ^ _U(ch))
    return (k >> _U(11)).astype(np.float64) * (1.0 / 9007199254740992.0)


def _decode_rgb(b: bytes) -> np.ndarray:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(io.BytesIO(b)) as im:
        return np.asarray(im.convert("RGB"))


def sample_surface(V: np.ndarray, F: np.ndarray, UV: np.ndarray | None, tex_of_face: np.ndarray, texture_bytes, n: int,
                   seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """V (nv, 3) float64 世界帧；F (nf, 3)；UV (nv, 2) 或 None；tex_of_face (nf,) 纹理下标（−1 无纹理）；texture_bytes(i) -> bytes。
    返回 xyz (n, 3) float64、normal (n, 3) float32、rgb (n, 3) uint8。"""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    cr = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cr, axis=1)
    cdf = np.cumsum(area)
    if not len(cdf) or cdf[-1] <= 0:
        raise ValueError("mesh has no area")
    idx = np.arange(n, dtype=np.uint64)
    face = np.minimum(np.searchsorted(cdf, splitmix_uniform(seed, idx, 0) * cdf[-1], side="right"), len(F) - 1)
    r1 = np.sqrt(splitmix_uniform(seed, idx, 1))
    r2 = splitmix_uniform(seed, idx, 2)
    w0, w1, w2 = 1.0 - r1, r1 * (1.0 - r2), r1 * r2
    fa, fb, fc = F[face, 0], F[face, 1], F[face, 2]
    xyz = V[fa] * w0[:, None] + V[fb] * w1[:, None] + V[fc] * w2[:, None]
    nrm = cr[face]
    nrm = (nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)).astype(np.float32)
    rgb = np.full((n, 3), 128, np.uint8)
    if UV is not None:
        uv = UV[fa] * w0[:, None].astype(np.float32) + UV[fb] * w1[:, None].astype(np.float32) + UV[fc] * w2[:, None].astype(np.float32)
        tex = tex_of_face[face]
        for t in np.unique(tex[tex >= 0]):
            m = tex == t
            img = _decode_rgb(texture_bytes(int(t)))
            H, W = img.shape[:2]
            u = np.mod(uv[m, 0], 1.0)                      # glTF：UV 原点在左上，缺省采样器 REPEAT
            v = np.mod(uv[m, 1], 1.0)
            rgb[m] = img[np.minimum((v * H).astype(np.int64), H - 1), np.minimum((u * W).astype(np.int64), W - 1)]
    return xyz, nrm, rgb
