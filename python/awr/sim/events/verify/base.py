"""门 3 的公共类型（M23 §6.9）：验证上下文、检测结果、一致性检查结果与区间工具。

上下文只读取规范帧数据（与 `tape.bin` 的 F 逐字节相同，生成后经流式复算核对）与解析后的锚点；位置为毫米定点值换算的米，
时刻为整数毫秒 t_k = k · 1000 / tick_hz。检测器与检查函数不得修改帧数据；可在 `cache` 中缓存与参数无关的中间量
（例如 1 Hz 天空可见度序列），供同一次验证内的多次调用复用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.sim.events.anchors import anchor_xy, point_in_polygon

__all__ = ["CheckResult", "Context", "Detection", "dist_to_polygon", "finite_or_none", "in_zone", "rnd", "runs"]

VISIBLE = (1, 2)
NO_CONTAINER = 255


@dataclass(frozen=True)
class Detection:
    """一个检测到的语义事件（瞬时事件区间为 [t, t]）；`at_xy` 为发生点（瞬时取发生位置，区间取中点时参与者质心）。"""
    type: str
    participants: tuple[str, ...]
    interval_ms: tuple[int, int]
    salience: float
    evidence: dict = field(default_factory=dict)
    at_xy: tuple[float, float] | None = None

    @property
    def key(self) -> tuple:
        return self.type, self.participants, self.interval_ms, self.evidence.get("zone", "")


@dataclass
class CheckResult:
    """一致性检查（§6.9.4）的结果：`stat` 写入真值 `consistency`，`fails` 为最差的若干违例（按偏差降序），`level` 为
    `error`（strict 下 613，lenient 下告警）或 `warn`（只告警）。"""
    stat: dict
    fails: list[dict] = field(default_factory=list)
    level: str = "error"
    code: str | None = None                        # 告警级检查的告警代码（缺省用注册代码）


@dataclass
class Context:
    plan: dict
    frames: np.ndarray                             # (n_ticks, n) RECORD
    table: list[dict]                              # 演员表（磁带头 actors，idx 次序）
    n_scripted: int
    anchors: dict[str, dict]
    stage: Any = None                              # gen.Stage（nav、ground）；无则跳过离网与贴地
    mesh: Any = None                               # MeshWorldQuery（raycast_batch）；无则跳过天空可见度
    arrivals: list[dict] | None = None             # gen.arrivals 的逐路点到达
    quality: list[np.ndarray] = field(default_factory=list)      # 不完整区域多边形（M22 semantic.quality）
    zone_refs: tuple[tuple[str, float], ...] = ()  # 注释引用的 (zone 锚点, buffer_m)（zone_* 检测器的缺省对象）
    cache: dict = field(default_factory=dict)
    perf: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        f = self.frames
        n_s = self.n_scripted
        self.tick_ms = 1000 // int(self.plan.get("tick_hz", 10))
        self.n_ticks = len(f)
        self.ids = [a["id"] for a in self.table]
        self.idx_of = {a["id"]: a["idx"] for a in self.table}
        self.cls = [a["class"] for a in self.table]
        self.routes = {a["id"]: a["route"] for a in self.plan["actors"]}
        S = f[:, :n_s]
        self.xy = np.stack([S["x_mm"], S["y_mm"]], -1).astype(np.float64) / 1000.0        # (n_t, n_s, 2) m
        self.z = S["z_mm"].astype(np.float64) / 1000.0
        self.state = S["state"]
        self.container = S["container"]
        self.vis = np.isin(self.state, VISIBLE)

    def t_ms(self, k: int) -> int:
        return int(k) * self.tick_ms

    def scripted(self, *classes: str) -> list[int]:
        return [i for i in range(self.n_scripted) if not classes or self.cls[i] in classes]

    def doors(self) -> list[dict]:
        return [self.anchors[k] for k in sorted(self.anchors) if self.anchors[k].get("kind") == "door"]


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """布尔序列中连续为真的段 [(k0, k1)]（含两端）。"""
    m = np.asarray(mask, bool)
    if not m.any():
        return []
    d = np.diff(np.concatenate([[0], m.view(np.int8), [0]]))
    st, en = np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1
    return [(int(a), int(b)) for a, b in zip(st, en, strict=True)]


def dist_to_polygon(P: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """点到多边形边界的水平距离（m），多边形内为 0。"""
    P = np.asarray(P, np.float64)
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    best = np.full(np.shape(x), np.inf)
    for (x1, y1), (x2, y2) in zip(P, np.roll(P, -1, 0), strict=True):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        u = np.clip(((x - x1) * dx + (y - y1) * dy) / L2, 0.0, 1.0) if L2 > 0 else np.zeros_like(x)
        best = np.minimum(best, np.hypot(x - (x1 + u * dx), y - (y1 + u * dy)))
    return np.where(point_in_polygon(P, x, y), 0.0, best)


def in_zone(anchor: dict, x: np.ndarray, y: np.ndarray, buffer_m: float) -> np.ndarray:
    """zone 判据（§6.9.1）：多边形（或点锚点的 `radius_m` 圆，缺省半径 0）外扩 `buffer_m`。"""
    if anchor.get("polygon"):
        return dist_to_polygon(np.asarray(anchor["polygon"], np.float64), x, y) <= buffer_m
    cx, cy = anchor_xy(anchor)
    return np.hypot(np.asarray(x) - cx, np.asarray(y) - cy) <= float(anchor.get("radius_m", 0.0)) + buffer_m


def rnd(v: float, nd: int = 3) -> float:
    """真值中的实数统一保留 nd 位小数（JCS 哈希稳定；−0 归一为 0）。"""
    r = round(float(v), nd)
    return 0.0 if r == 0 else r


def finite_or_none(v: float) -> float | None:
    return None if not math.isfinite(v) else rnd(v)
