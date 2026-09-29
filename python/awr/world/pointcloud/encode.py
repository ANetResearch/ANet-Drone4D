"""ANET_Q16 v1 编码：oct16 法线、baked-height 底色、节点立方体量化（AWR-16 §4.3–§4.5；M03-FR-035）。

oct16 的编码只有这一处实现（Python 为唯一编码方，TS 只解码）。
"""

from __future__ import annotations

import numpy as np

from .npx import row_norm3

# 16 §4.4：Graphite 五档（15 `--pc-ramp-0…4`：g800、g600、g400、g200、g50）的 sRGB 字节。
# 这里是数据格式常量（写入 octree.bin 的字节），不是 UI 颜色；以十进制书写。
BAKED_HEIGHT_RAMP = np.array(
    [[29, 31, 35], [62, 66, 73], [129, 134, 143], [202, 205, 211], [242, 243, 245]], np.float64
)


def oct16_encode(n: np.ndarray) -> np.ndarray:
    """(N,3) 非零向量 → uint16 `(octU << 8) | octV`；0x0000 保留为"无法线"（16 §4.5）。"""
    n = np.asarray(n, np.float64)
    s = np.abs(n[:, 0]) + np.abs(n[:, 1]) + np.abs(n[:, 2])
    n = n / np.maximum(s, 1e-12)[:, None]
    x, y, z = n[:, 0], n[:, 1], n[:, 2]
    sx = np.where(x >= 0, 1.0, -1.0)
    sy = np.where(y >= 0, 1.0, -1.0)
    neg = z < 0
    ox = np.where(neg, (1 - np.abs(y)) * sx, x)
    oy = np.where(neg, (1 - np.abs(x)) * sy, y)
    u = np.clip(np.round((ox * 0.5 + 0.5) * 255), 0, 255).astype(np.uint16)
    v = np.clip(np.round((oy * 0.5 + 0.5) * 255), 0, 255).astype(np.uint16)
    w = (u << 8) | v
    w[w == 0] = 0xFFFF
    return w


def oct16_decode(w: np.ndarray) -> np.ndarray:
    """uint16 → (N,3) 单位向量；w == 0 的行返回 (0,0,0)。"""
    w = np.asarray(w, np.uint16)
    u = (w >> 8).astype(np.float64) / 255 * 2 - 1
    v = (w & 255).astype(np.float64) / 255 * 2 - 1
    nz = 1 - np.abs(u) - np.abs(v)
    neg = nz < 0
    sx = np.where(u >= 0, 1.0, -1.0)
    sy = np.where(v >= 0, 1.0, -1.0)
    nx = np.where(neg, (1 - np.abs(v)) * sx, u)
    ny = np.where(neg, (1 - np.abs(u)) * sy, v)
    out = np.stack([nx, ny, nz], 1)
    norm = row_norm3(out)[:, None]
    out = out / np.where(norm > 0, norm, 1.0)
    out[w == 0] = 0.0
    return out


def baked_height(z: np.ndarray, z1: float, z99: float) -> np.ndarray:
    """`t = clip((z − zP1)/(zP99 − zP1), 0, 1)^0.6`，五档线性插值后 rint（16 §4.4）。返回 (N,3) uint8。"""
    t = np.clip((np.asarray(z, np.float64) - z1) / max(z99 - z1, 1e-6), 0, 1) ** 0.6 * 4
    i = np.minimum(t.astype(int), 3)
    f = (t - i)[:, None]
    return np.round(BAKED_HEIGHT_RAMP[i] * (1 - f) + BAKED_HEIGHT_RAMP[i + 1] * f).astype(np.uint8)


def quantize_node(P: np.ndarray, node_min: np.ndarray, node_size: float) -> np.ndarray:
    """节点立方体归一化 u16：`clamp(round((p − nodeMin)/nodeSize·65535), 0, 65535)`。"""
    return np.clip(np.round((P - node_min) / node_size * 65535), 0, 65535).astype("<u2")


def decode_node_positions(q: np.ndarray, node_min: np.ndarray, node_size: float) -> np.ndarray:
    """u16 (N,3) → float64 world 坐标：`p = nodeMin + q/65535·nodeSize`。"""
    return np.asarray(node_min, np.float64) + q.astype(np.float64) / 65535.0 * node_size
