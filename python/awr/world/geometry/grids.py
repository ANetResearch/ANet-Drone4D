"""栅格装载与采样（M04 §9.1 `grids.py`；AWR-16 §6.2）：sidecar 解析与校验、只读 memmap、最近格、格心双线性。

语义（M04 §6.3）：DSM 一律按最近格取值（柱体语义），DTM 按格心双线性；界外点查询默认钳制到最近格，
`oob = "nan"` 时返回 NaN。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .types import GeoLoadError

_ITEM = {"float32": 4, "uint8": 1, "uint16": 2, "float16": 2}
_NP = {"float32": "<f4", "uint8": "u1", "uint16": "<u2", "float16": "<f2"}


@dataclass
class Grid:
    a: np.ndarray             # (H, W)，第 0 行在南
    x0: float
    y0: float
    cell: float
    sidecar: dict | None = None

    @property
    def h(self) -> int:
        return int(self.a.shape[0])

    @property
    def w(self) -> int:
        return int(self.a.shape[1])

    @classmethod
    def open(cls, path_json: Path, *, expect_kind: str | None = None, expect_cell: float | None = None,
             expect_dtype: str = "float32", expect_frame: str | None = "world-z-m") -> Grid:
        path_json = Path(path_json)
        try:
            sc = json.loads(path_json.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise GeoLoadError("GEO_MISSING_FILE", str(path_json)) from e
        except (OSError, json.JSONDecodeError) as e:
            raise GeoLoadError("GEO_GRID_INVALID", f"{path_json}: {e}") from e
        try:
            W, H = int(sc["width"]), int(sc["height"])
            dtype = sc["dtype"]
            bad = []
            if expect_kind and sc.get("kind") != expect_kind:
                bad.append(f"kind {sc.get('kind')} != {expect_kind}")
            if dtype != expect_dtype:
                bad.append(f"dtype {dtype} != {expect_dtype}")
            if expect_frame and sc.get("valueFrame") != expect_frame:
                bad.append(f"valueFrame {sc.get('valueFrame')} != {expect_frame}")
            if sc.get("rowOrder") != "south-to-north":
                bad.append(f"rowOrder {sc.get('rowOrder')}")
            if expect_cell is not None and abs(float(sc["cellM"]) - expect_cell) > 1e-9:
                bad.append(f"cellM {sc['cellM']} != {expect_cell}")
            if dtype.startswith("float") and sc.get("nodata") is not None:
                bad.append("nodata must be null after filling")
            raw = path_json.parent / sc["href"]
            if not raw.exists():
                raise GeoLoadError("GEO_MISSING_FILE", str(raw))
            if raw.stat().st_size != W * H * _ITEM[dtype]:
                bad.append(f"{raw.name} is {raw.stat().st_size} B, expected {W * H * _ITEM[dtype]}")
        except (KeyError, TypeError, ValueError) as e:
            raise GeoLoadError("GEO_GRID_INVALID", f"{path_json}: {e}") from e
        if bad:
            raise GeoLoadError("GEO_GRID_INVALID", f"{path_json.name}: " + "; ".join(bad))
        a = np.memmap(raw, dtype=_NP[dtype], mode="r", shape=(H, W))
        return cls(a, float(sc["originXY"][0]), float(sc["originXY"][1]), float(sc["cellM"]), sc)

    # ---- 下标
    def fidx(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        return ((np.atleast_1d(np.asarray(x, np.float64)) - self.x0) / self.cell,
                (np.atleast_1d(np.asarray(y, np.float64)) - self.y0) / self.cell)

    def idx(self, x, y) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(row, col, inside)：钳制到栅格内的最近格下标，以及点是否在栅格范围内。"""
        fx, fy = self.fidx(x, y)
        c = np.floor(fx).astype(np.int64)
        r = np.floor(fy).astype(np.int64)
        inside = (c >= 0) & (c < self.w) & (r >= 0) & (r < self.h)
        np.clip(c, 0, self.w - 1, out=c)
        np.clip(r, 0, self.h - 1, out=r)
        return r, c, inside

    def nearest(self, x, y, oob: str = "clamp") -> np.ndarray:
        r, c, inside = self.idx(x, y)
        v = np.asarray(self.a[r, c], np.float32)
        if oob == "nan":
            v = np.where(inside, v, np.float32(np.nan))
        return v

    def _flat64(self) -> np.ndarray:
        """双线性取值用的一维视图：小栅格（DTM 10 m 等，≤ 2^21 格）缓存 float64 副本，免去 memmap 花式索引与逐次转型。"""
        f = self.__dict__.get("_f64")
        if f is None:
            f = np.ascontiguousarray(self.a, np.float64).reshape(-1) if self.w * self.h <= (1 << 21) \
                else np.asarray(self.a).reshape(-1)
            self.__dict__["_f64"] = f
        return f

    def bilinear(self, x, y, oob: str = "clamp") -> np.ndarray:
        """格心双线性（界外钳制）；与 M03 `terrain.grids.Grid.bilinear` 同式（逐位相同，四邻取值以一维 take 实现）。"""
        fx, fy = self.fidx(x, y)
        w, h = self.w, self.h
        gx = np.clip(fx - 0.5, 0.0, max(w - 1, 0))
        gy = np.clip(fy - 0.5, 0.0, max(h - 1, 0))
        c0 = np.minimum(gx.astype(np.int64), max(w - 2, 0))          # gx ≥ 0：截断即 floor
        r0 = np.minimum(gy.astype(np.int64), max(h - 2, 0))
        tx, ty = gx - c0, gy - r0
        f = self._flat64()
        i = r0 * w + c0
        dc = 1 if w >= 2 else 0
        dr = w if h >= 2 else 0
        v00, v01 = f.take(i).astype(np.float64, copy=False), f.take(i + dc).astype(np.float64, copy=False)
        v10, v11 = f.take(i + dr).astype(np.float64, copy=False), f.take(i + dr + dc).astype(np.float64, copy=False)
        v = (v00 * (1 - tx) + v01 * tx) * (1 - ty) + (v10 * (1 - tx) + v11 * tx) * ty
        if oob == "nan":
            inside = (fx >= 0) & (fx <= w) & (fy >= 0) & (fy <= h)
            v = np.where(inside, v, np.nan)
        return v

    def _bilinear_ref(self, x, y, oob: str = "clamp") -> np.ndarray:
        """参考实现（测试对拍用）。"""
        fx, fy = self.fidx(x, y)
        inside = (fx >= 0) & (fx <= self.w) & (fy >= 0) & (fy <= self.h)
        fx = np.clip(fx - 0.5, 0.0, max(self.w - 1, 0))
        fy = np.clip(fy - 0.5, 0.0, max(self.h - 1, 0))
        c0 = np.minimum(np.floor(fx).astype(np.int64), max(self.w - 2, 0))
        r0 = np.minimum(np.floor(fy).astype(np.int64), max(self.h - 2, 0))
        tx, ty = fx - c0, fy - r0
        c1, r1 = np.minimum(c0 + 1, self.w - 1), np.minimum(r0 + 1, self.h - 1)
        a = self.a
        v = ((a[r0, c0].astype(np.float64) * (1 - tx) + a[r0, c1].astype(np.float64) * tx) * (1 - ty)
             + (a[r1, c0].astype(np.float64) * (1 - tx) + a[r1, c1].astype(np.float64) * tx) * ty)
        if oob == "nan":
            v = np.where(inside, v, np.nan)
        return v

    def centres_bilinear(self, target: Grid) -> np.ndarray:
        """在 target 的全部格心上对本栅格做双线性（可分离实现），返回 float64 (target.h, target.w)。"""
        cx = target.x0 + (np.arange(target.w) + 0.5) * target.cell
        cy = target.y0 + (np.arange(target.h) + 0.5) * target.cell

        def wts(n, o, cell, x):
            f = np.clip((x - o) / cell - 0.5, 0.0, max(n - 1, 0))
            i0 = np.minimum(np.floor(f).astype(np.int64), max(n - 2, 0))
            return i0, np.minimum(i0 + 1, n - 1), f - i0

        c0, c1, tx = wts(self.w, self.x0, self.cell, cx)
        r0, r1, ty = wts(self.h, self.y0, self.cell, cy)
        X = self.a[:, c0].astype(np.float64) * (1 - tx) + self.a[:, c1].astype(np.float64) * tx
        return X[r0] * (1 - ty)[:, None] + X[r1] * ty[:, None]

    def view(self) -> Grid:
        return Grid(self.a, self.x0, self.y0, self.cell, self.sidecar)
