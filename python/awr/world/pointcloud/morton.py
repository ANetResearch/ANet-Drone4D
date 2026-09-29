"""21 位/轴 Morton 码：查表展开、压缩与节点名换算（M03 §6.7、O-2；AWR-16 §4.2）。

Morton 位序为 x 在每个三元组的最高位（`x<<2 | y<<1 | z`），与 Potree 子序一致。
"""

from __future__ import annotations

import numpy as np

B = 21
_MASK21 = (1 << B) - 1


def _make_lut() -> np.ndarray:
    v = np.arange(128, dtype=np.uint64)
    out = np.zeros(128, np.uint64)
    for b in range(7):
        out |= ((v >> np.uint64(b)) & np.uint64(1)) << np.uint64(3 * b)
    return out


_LUT = _make_lut()


def spread21(x: np.ndarray) -> np.ndarray:
    """int 数组（每个值 < 2^21）按每 3 位一位展开为 uint64（3 次 7 位查表）。"""
    x = np.asarray(x).astype(np.int64, copy=False)
    return _LUT[x & 127] | (_LUT[(x >> 7) & 127] << np.uint64(21)) | (_LUT[x >> 14] << np.uint64(42))


def compact3(v: np.ndarray | np.uint64 | int) -> np.ndarray:
    """spread21 的逆：取每 3 位的最低位并压缩（逐元素位运算）。"""
    v = np.asarray(v, dtype=np.uint64) & np.uint64(0x1249249249249249)
    v = (v ^ (v >> np.uint64(2))) & np.uint64(0x10C30C30C30C30C3)
    v = (v ^ (v >> np.uint64(4))) & np.uint64(0x100F00F00F00F00F)
    v = (v ^ (v >> np.uint64(8))) & np.uint64(0x1F0000FF0000FF)
    v = (v ^ (v >> np.uint64(16))) & np.uint64(0x1F00000000FFFF)
    v = (v ^ (v >> np.uint64(32))) & np.uint64(0x1FFFFF)
    return v


def quantize(P: np.ndarray, cube_min: np.ndarray, size: float, bits: int = B) -> np.ndarray:
    """`q = clip(floor((p - cubeMin)/size * 2^bits), 0, 2^bits - 1)`，int64 (N,3)。

    与 g03 原型逐元素相同：先除以 size 再乘 2^bits，然后截断为整数。
    """
    q = ((np.asarray(P, np.float64) - np.asarray(cube_min, np.float64)) / size * (1 << bits)).astype(np.int64)
    np.clip(q, 0, (1 << bits) - 1, out=q)
    return q


def morton_codes(q: np.ndarray) -> np.ndarray:
    """(N,3) 量化坐标 → uint64 Morton 码（x 为每个三元组的最高位）。"""
    return (spread21(q[:, 0]) << np.uint64(2)) | (spread21(q[:, 1]) << np.uint64(1)) | spread21(q[:, 2])


def node_xyz(nid: int, level: int) -> tuple[int, int, int]:
    """节点 Morton 前缀 → 节点在该层的整数格 (x, y, z)。"""
    k = np.uint64(nid)
    return int(compact3(k >> np.uint64(2))), int(compact3(k >> np.uint64(1))), int(compact3(k))


def name_of(nid: int, level: int) -> str:
    """节点名：'r' 加每层一位子序数字（由 nid 的八进制各位得到，16 §4.2）。"""
    if level == 0:
        return "r"
    return "r" + "".join(str((nid >> (3 * (level - 1 - i))) & 7) for i in range(level))


def name_to_key(name: str) -> tuple[int, int, int, int]:
    """节点名 → (L, x, y, z)（16 §4.2；r09 `name_to_key`）。"""
    level = len(name) - 1
    x = y = z = 0
    for ch in name[1:]:
        c = int(ch)
        x = 2 * x + ((c >> 2) & 1)
        y = 2 * y + ((c >> 1) & 1)
        z = 2 * z + (c & 1)
    return level, x, y, z
