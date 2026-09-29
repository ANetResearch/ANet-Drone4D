"""4 m 规划栅格（M10-FR-034；M10 §6.5.2）：`Hf = max(grid_2p5d(4), dtm + alt_min_agl_m)`。

`grid_2p5d(4)` 为 M04 Height_map（2 m，已含 5×5 最大值滤波与 10 m 安全距离）最大值金字塔的第 1 层（M04-FR-022），因此任一
4 m 格的 Hf 不低于其覆盖范围内及水平 4–6 m 以内全部 DSM 高度加 10 m；DTM 地板取格心双线性。栅格在 plan-pool 进程中按
(world_id, contentVersion, coordinate.sha256) 缓存（worker `_grid25`）。

禁飞区与世界边界在栅格上直接阻塞（Hf = +inf，外扩 1 格），使 A* 不把路径引入禁区；最终仍由 `path_valid`（含 zones）
校验（§6.5.7）。起终点所在格不阻塞（`search` 用 `base` 恢复），由端点规则单独判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = ["Grid25"]


@dataclass
class Grid25:
    a: np.ndarray        # float64 (rows, cols)，行 0 在南；阻塞格为 +inf
    x0: float
    y0: float
    cell: float
    alt_min_agl_m: float
    base: np.ndarray | None = None     # 未叠加区域阻塞前的 Hf

    @property
    def h(self) -> int:
        return int(self.a.shape[0])

    @property
    def w(self) -> int:
        return int(self.a.shape[1])

    @classmethod
    def from_world(cls, world: Any, alt_min_agl_m: float = 20.0, res_m: float = 4.0, zones: bool = True) -> Grid25:
        top, meta = world.grid_2p5d(res_m)
        top = np.asarray(top, np.float64)
        x0, y0 = (float(v) for v in meta["originXY"])
        cell = float(meta["cellM"])
        H, W = top.shape
        cx = x0 + (np.arange(W) + 0.5) * cell
        out = np.empty((H, W), np.float64)
        for r0 in range(0, H, 256):                       # 分块查询 DTM，避免一次性大数组
            r1 = min(H, r0 + 256)
            cy = y0 + (np.arange(r0, r1) + 0.5) * cell
            X, Y = np.meshgrid(cx, cy)
            g = np.asarray(world.ground_dtm(np.c_[X.ravel(), Y.ravel()]), np.float64).reshape(r1 - r0, W)
            out[r0:r1] = np.maximum(top[r0:r1], g + float(alt_min_agl_m))
        grid = cls(out, x0, y0, cell, float(alt_min_agl_m))
        zi = getattr(world, "zones", None) if zones else None
        if zi is not None:
            grid.block_zones(zi)
        return grid

    def block_zones(self, zi: Any, dilate: int = 1) -> None:
        """nofly 棱柱内（格心判定）与 border 外的格置 +inf，并按 `dilate` 格做 8 邻域外扩。"""
        H, W = self.a.shape
        blocked = np.zeros((H, W), bool)
        cx = self.x0 + (np.arange(W) + 0.5) * self.cell
        prisms = list(getattr(zi, "nofly", []))
        border = getattr(zi, "border", None)
        step = max(1, 32768 // max(W, 1))
        for r0 in range(0, H, step):
            r1 = min(H, r0 + step)
            cy = self.y0 + (np.arange(r0, r1) + 0.5) * self.cell
            X, Y = np.meshgrid(cx, cy)
            xy = np.c_[X.ravel(), Y.ravel()]
            m = np.zeros(len(xy), bool)
            for p in prisms:
                m |= p.contains_xy(xy)
            if border is not None:
                m |= ~border.contains_xy(xy)
            blocked[r0:r1] = m.reshape(r1 - r0, W)
        for _ in range(int(dilate)):
            b = blocked.copy()
            b[1:] |= blocked[:-1]
            b[:-1] |= blocked[1:]
            b[:, 1:] |= blocked[:, :-1]
            b[:, :-1] |= blocked[:, 1:]
            b[1:, 1:] |= blocked[:-1, :-1]
            b[:-1, :-1] |= blocked[1:, 1:]
            b[1:, :-1] |= blocked[:-1, 1:]
            b[:-1, 1:] |= blocked[1:, :-1]
            blocked = b
        if blocked.any():
            self.base = self.a.copy()
            self.a = np.where(blocked, np.inf, self.a)

    def idx(self, x: float, y: float) -> tuple[int, int]:
        c = int(np.clip(np.floor((x - self.x0) / self.cell), 0, self.w - 1))
        r = int(np.clip(np.floor((y - self.y0) / self.cell), 0, self.h - 1))
        return r, c

    def center(self, r: np.ndarray, c: np.ndarray) -> np.ndarray:
        return np.c_[self.x0 + (np.asarray(c) + 0.5) * self.cell, self.y0 + (np.asarray(r) + 0.5) * self.cell]

    @property
    def nbytes(self) -> int:
        return int(self.a.nbytes)
