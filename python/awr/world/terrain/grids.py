"""栅格基础：一次排序加 reduceat 的格内归约、`Grid` 与 sidecar 写出（AWR-16 §6.2；M03 §6.6）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..package.jsonio import write_json

ITEMSIZE = {"float32": 4, "float16": 2, "uint8": 1, "uint16": 2}
NP_DTYPE = {"float32": "<f4", "float16": "<f2", "uint8": "u1", "uint16": "<u2"}


@dataclass(slots=True)
class Grid:
    """行主序、第 0 行在南；`origin_xy` 为格 (0, 0) 的西南角（16 §6.2）。"""

    a: np.ndarray
    origin_xy: tuple[float, float]
    cell_m: float

    @property
    def height(self) -> int:
        return int(self.a.shape[0])

    @property
    def width(self) -> int:
        return int(self.a.shape[1])

    def cell_centres(self) -> tuple[np.ndarray, np.ndarray]:
        cx = self.origin_xy[0] + (np.arange(self.width) + 0.5) * self.cell_m
        cy = self.origin_xy[1] + (np.arange(self.height) + 0.5) * self.cell_m
        return cx, cy

    def bilinear(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """以格心为节点的双线性采样，界外钳制（16 §6.2）。"""
        a = self.a
        fx = (np.asarray(x, np.float64) - self.origin_xy[0]) / self.cell_m - 0.5
        fy = (np.asarray(y, np.float64) - self.origin_xy[1]) / self.cell_m - 0.5
        fx = np.clip(fx, 0.0, max(self.width - 1, 0))
        fy = np.clip(fy, 0.0, max(self.height - 1, 0))
        c0 = np.minimum(np.floor(fx).astype(np.int64), max(self.width - 2, 0))
        r0 = np.minimum(np.floor(fy).astype(np.int64), max(self.height - 2, 0))
        tx = fx - c0
        ty = fy - r0
        c1 = np.minimum(c0 + 1, self.width - 1)
        r1 = np.minimum(r0 + 1, self.height - 1)
        a00 = a[r0, c0].astype(np.float64)
        a01 = a[r0, c1].astype(np.float64)
        a10 = a[r1, c0].astype(np.float64)
        a11 = a[r1, c1].astype(np.float64)
        return (a00 * (1 - tx) + a01 * tx) * (1 - ty) + (a10 * (1 - tx) + a11 * tx) * ty


def bilinear_weights(n: int, origin: float, cell: float, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """一维双线性下标与权重（以格心为节点，界外钳制）；与 `Grid.bilinear` 逐元素相同。"""
    f = np.clip((np.asarray(x, np.float64) - origin) / cell - 0.5, 0.0, max(n - 1, 0))
    i0 = np.minimum(np.floor(f).astype(np.int64), max(n - 2, 0))
    t = f - i0
    return i0, np.minimum(i0 + 1, n - 1), t


def bilinear_on_centres(src: Grid, origin_xy: tuple[float, float], cell: float, H: int, W: int,
                        rows: tuple[int, int] | None = None) -> np.ndarray:
    """在规则网格格心上对 src 做双线性采样（可分离实现，结果与逐点公式逐位相同）。返回 float64 (h, W)。"""
    r0, r1 = rows if rows is not None else (0, H)
    cx = origin_xy[0] + (np.arange(W) + 0.5) * cell
    cy = origin_xy[1] + (np.arange(r0, r1) + 0.5) * cell
    c0, c1, tx = bilinear_weights(src.width, src.origin_xy[0], src.cell_m, cx)
    q0, q1, ty = bilinear_weights(src.height, src.origin_xy[1], src.cell_m, cy)
    a = src.a
    need = np.unique(np.r_[q0, q1])
    X = np.empty((src.height, W), np.float64)
    X[need] = a[need][:, c0].astype(np.float64) * (1 - tx) + a[need][:, c1].astype(np.float64) * tx
    return X[q0] * (1 - ty)[:, None] + X[q1] * ty[:, None]


def grid_shape(extent_min_xy: np.ndarray, extent_max_xy: np.ndarray, cell: float) -> tuple[int, int]:
    """尺寸规则：`width = floor((maxE − originX)/cell) + 1`，`height` 同理（16 §6.2）。"""
    W = int(np.floor((extent_max_xy[0] - extent_min_xy[0]) / cell)) + 1
    H = int(np.floor((extent_max_xy[1] - extent_min_xy[1]) / cell)) + 1
    return H, W


@dataclass(slots=True)
class CellIndex:
    """一组点在某网格上的格下标与"按格排序"的结果，供多次 reduce 复用（M03 O-4）。"""

    ix: np.ndarray        # int32 [N]
    iy: np.ndarray        # int32 [N]
    H: int
    W: int
    order: np.ndarray     # 按线性格号稳定排序
    starts: np.ndarray    # 各非空格在 order 中的起点
    lin: np.ndarray       # 各非空格的线性格号（升序）

    @classmethod
    def build(cls, x: np.ndarray, y: np.ndarray, origin_xy, cell: float, H: int | None = None, W: int | None = None
              ) -> CellIndex:
        ix = ((x - origin_xy[0]) // cell).astype(np.int32)
        iy = ((y - origin_xy[1]) // cell).astype(np.int32)
        H = int(iy.max()) + 1 if H is None else H
        W = int(ix.max()) + 1 if W is None else W
        np.clip(ix, 0, W - 1, out=ix)
        np.clip(iy, 0, H - 1, out=iy)
        key = iy.astype(np.int64) * W + ix
        order = np.argsort(key, kind="stable")
        ks = key[order]
        starts = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]])
        return cls(ix=ix, iy=iy, H=H, W=W, order=order, starts=starts, lin=ks[starts])

    def reduce(self, v: np.ndarray, op: str, fill: float, dtype=None) -> np.ndarray:
        """格内 min / max（`reduceat`），空格为 `fill`；返回 (H, W)。"""
        vs = np.asarray(v)[self.order]
        if dtype is not None:
            vs = vs.astype(dtype, copy=False)
        fn = {"min": np.minimum, "max": np.maximum}[op]
        vals = fn.reduceat(vs, self.starts)
        out = np.full(self.H * self.W, fill, dtype=vals.dtype)
        out[self.lin] = vals
        return out.reshape(self.H, self.W)

    def counts(self) -> np.ndarray:
        n = np.diff(np.r_[self.starts, len(self.order)])
        out = np.zeros(self.H * self.W, np.int64)
        out[self.lin] = n
        return out.reshape(self.H, self.W)


def grid_reduce(iy: np.ndarray, ix: np.ndarray, v: np.ndarray, op: str, fill: float, H: int, W: int) -> np.ndarray:
    """一次 argsort + reduceat 的格内归约（避免 np.minimum.at 的逐元素慢路径）。"""
    key = iy.astype(np.int64) * W + ix
    order = np.argsort(key, kind="stable")
    ks = key[order]
    starts = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]])
    fn = {"min": np.minimum, "max": np.maximum}[op]
    vals = fn.reduceat(np.asarray(v)[order], starts)
    out = np.full(H * W, fill, dtype=vals.dtype)
    out[ks[starts]] = vals
    return out.reshape(H, W)


def write_grid(path_json: Path, grid: Grid, kind: str, method: str, *, dtype: str = "float32",
               value_frame: str = "world-z-m", scale: float | None = None, offset: float | None = None) -> dict:
    """写 `<name>.json` sidecar 与同名原始数组（小端、行主序，16 §6.2）。"""
    path_json = Path(path_json)
    raw_name = path_json.with_suffix({"float32": ".f32", "float16": ".f16", "uint8": ".u8", "uint16": ".u16"}[dtype]).name
    arr = np.ascontiguousarray(grid.a, dtype=NP_DTYPE[dtype])
    path_json.parent.mkdir(parents=True, exist_ok=True)
    arr.tofile(path_json.parent / raw_name)
    sc = {"schemaVersion": "1.0.0", "kind": kind, "dtype": dtype, "href": raw_name,
          "width": grid.width, "height": grid.height, "cellM": float(grid.cell_m),
          "originXY": [float(grid.origin_xy[0]), float(grid.origin_xy[1])], "rowOrder": "south-to-north",
          "nodata": None, "valueFrame": value_frame}
    if scale is not None:
        sc["scale"] = float(scale)
        sc["offset"] = float(offset if offset is not None else 0.0)
    sc["method"] = method
    write_json(path_json, sc)
    return sc


def read_grid(path_json: Path) -> tuple[dict, np.ndarray]:
    import json

    path_json = Path(path_json)
    sc = json.loads(path_json.read_text(encoding="utf-8"))
    a = np.fromfile(path_json.parent / sc["href"], NP_DTYPE[sc["dtype"]]).reshape(sc["height"], sc["width"])
    return sc, a
