"""目标搜索先验（M23 §15.3 规则 2，`brief.v1` 的 `search` 段；M26 §14.3 `coverage_scan`"目标搜索类按先验密度加权排序航带"）。

简报 `brief.search` 只含先验、搜索区、运动方式与窗口，不含实际位置。本模块把先验换算为基线可用的量：
- `density(x, y)`：gmm 为各向同性高斯分量的加权和 Σ w·exp(−d²/(2·s²))/(2π·s²)（s 为 sigma_m）；grid 为格点质量 / 单元面积（行优先，y 外 x 内）；
- `centers(n)`：观察位候选。gmm 为分量中心（按权重降序，同权重按分量次序）；grid 为质量降序的格心，两两间隔 ≥ 40 m，至多 n 个；
- `lane_order(lanes, y0, y1, step)`：沿航带等距取样的质量和，按降序排出航带次序（同质量按航带序号）。
分量以锚点给出中心时只解析简报公开锚点（`brief.anchors`），未公开的锚点跳过。全部计算为确定性浮点运算，无随机数。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["CENTER_SEP_M", "SearchPrior", "point_in_polygon", "search_prior"]

CENTER_SEP_M = 40.0


def point_in_polygon(x: float, y: float, poly) -> bool:
    """射线法（边界上的点按半开规则计入一侧）。"""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i][0], poly[i][1]
        x2, y2 = poly[(i + 1) % n][0], poly[(i + 1) % n][1]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


@dataclass(frozen=True)
class SearchPrior:
    target_class: str
    motion: str
    window_s: float
    area: tuple[tuple[float, float], ...]
    comps: tuple[tuple[float, float, float, float], ...] = ()       # gmm：(x, y, sigma_m, weight)
    grid: tuple | None = None                                        # grid：(x0, y0, cell_m, nx, ny, weights)

    def density(self, x: float, y: float) -> float:
        if self.grid is not None:
            x0, y0, c, nx, ny, w = self.grid
            i, j = math.floor((x - x0) / c), math.floor((y - y0) / c)
            return w[j * nx + i] / (c * c) if 0 <= i < nx and 0 <= j < ny else 0.0
        return sum(wt * math.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * s * s)) / (2 * math.pi * s * s) for cx, cy, s, wt in self.comps)

    def centers(self, n: int) -> list[tuple[float, float]]:
        if self.grid is not None:
            x0, y0, c, nx, ny, w = self.grid
            cells = sorted((-w[j * nx + i], j * nx + i) for j in range(ny) for i in range(nx) if w[j * nx + i] > 0)
            out: list[tuple[float, float]] = []
            for _neg, idx in cells:
                p = (x0 + (idx % nx + 0.5) * c, y0 + (idx // nx + 0.5) * c)
                if all(math.dist(p, q) >= CENTER_SEP_M for q in out):
                    out.append(p)
                if len(out) >= n:
                    break
            return out
        order = sorted(range(len(self.comps)), key=lambda k: (-self.comps[k][3], k))
        return [(self.comps[k][0], self.comps[k][1]) for k in order][:n]

    def lane_order(self, lanes: list[float], y0: float, y1: float, step: float) -> list[int]:
        """航带（x 常数，y0 → y1）沿 y 每 step 米取样的先验质量和 → 航带下标按质量降序（同质量按下标升序）。"""
        n = max(1, int((y1 - y0) / step))
        ys = [y0 + (k + 0.5) * (y1 - y0) / n for k in range(n)]
        mass = [sum(self.density(x, y) for y in ys) for x in lanes]
        return sorted(range(len(lanes)), key=lambda i: (-round(mass[i], 12), i))


def search_prior(brief: dict) -> SearchPrior | None:
    """`brief.search`（缺省为 None：非目标搜索事件，或 Env 未投影先验）→ SearchPrior。"""
    s = brief.get("search")
    if not isinstance(s, dict) or "prior" not in s:
        return None
    pr = s["prior"]
    area = tuple((float(p[0]), float(p[1])) for p in s.get("area_polygon") or brief["area"]["polygon_enu_m"])
    anchors = {a["id"]: a["pos_enu_m"] for a in brief.get("anchors", [])}
    comps: list[tuple[float, float, float, float]] = []
    grid = None
    if pr.get("type") == "grid" and pr.get("grid"):
        g = pr["grid"]
        grid = (float(g["origin_xy"][0]), float(g["origin_xy"][1]), float(g["cell_m"]), int(g["nx"]), int(g["ny"]),
                tuple(float(v) for v in g["weights"]))
    else:
        for c in pr.get("components", []):
            xy = c.get("xy") or anchors.get(c.get("anchor"))
            if xy is not None:
                comps.append((float(xy[0]), float(xy[1]), float(c["sigma_m"]), float(c.get("weight", 1.0))))
        if not comps:
            return None
    return SearchPrior(str(s.get("target_class", "person")), str(s.get("motion", "static")), float(s.get("window_s", 60.0)), area,
                       tuple(comps), grid)
