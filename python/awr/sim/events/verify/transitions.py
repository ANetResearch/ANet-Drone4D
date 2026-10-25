"""状态转移类检测器（M23 §6.9.1、§6.6.3）：上车、下车、进入建筑、离开建筑。全部为瞬时事件，区间 [t_k, t_k]，k 为转移后的首帧；
显著性 1.0（§6.9.2）。帧 −1 视为状态 0（缺席）。

- `board_vehicle`：行人状态转为 3 且 `container` 为车辆，参与者 (行人, 车辆)，发生点为转移前一帧的行人位置；
- `alight_vehicle`：行人状态由 3 转为 1 或 2，参与者 (行人, 转移前的容器)，发生点为转移后的位置；
- `enter_building`：行人状态由 1 或 2 转为 4，发生点距 door 锚点 < `door_m`（`params.anchor` 给定时只取该锚点，否则取最近者）；
- `exit_building`：行人状态由 4（或 0 且首路点为 `exit_building`）转为 1 或 2，距 door 锚点 < `door_m`。
"""

from __future__ import annotations

import math

import numpy as np

from awr.sim.events.anchors import anchor_xy
from awr.sim.events.verify.base import NO_CONTAINER, VISIBLE, Context, Detection, rnd
from awr.sim.events.verify.registry import detector


def _prev(s: np.ndarray) -> np.ndarray:
    return np.concatenate([np.zeros(1, s.dtype), s[:-1]])


def _xy(ctx: Context, k: int, i: int) -> tuple[float, float]:
    return float(ctx.xy[k, i, 0]), float(ctx.xy[k, i, 1])


@detector("board_vehicle", instant=True)
def board_vehicle(ctx: Context, p: dict) -> list[Detection]:
    out = []
    for i in ctx.scripted("person"):
        s = ctx.state[:, i]
        for k in np.flatnonzero((s == 3) & np.isin(_prev(s), VISIBLE)):
            c = int(ctx.container[k, i])
            if c == NO_CONTAINER or c >= len(ctx.ids):
                continue
            kb = max(int(k) - 1, 0)
            ev = {}
            if c < ctx.n_scripted:
                ev["min_dist_m"] = rnd(math.dist(_xy(ctx, kb, i), _xy(ctx, kb, c)))
            out.append(Detection("board_vehicle", (ctx.ids[i], ctx.ids[c]), (ctx.t_ms(k), ctx.t_ms(k)), 1.0, ev, _xy(ctx, kb, i)))
    return out


@detector("alight_vehicle", instant=True)
def alight_vehicle(ctx: Context, p: dict) -> list[Detection]:
    out = []
    for i in ctx.scripted("person"):
        s = ctx.state[:, i]
        for k in np.flatnonzero(np.isin(s, VISIBLE) & (_prev(s) == 3)):
            c = int(ctx.container[int(k) - 1, i])
            if c == NO_CONTAINER or c >= len(ctx.ids):
                continue
            out.append(Detection("alight_vehicle", (ctx.ids[i], ctx.ids[c]), (ctx.t_ms(k), ctx.t_ms(k)), 1.0, {}, _xy(ctx, int(k), i)))
    return out


def _door(ctx: Context, xy: tuple[float, float], p: dict) -> tuple[str, float] | None:
    doors = [ctx.anchors[p["anchor"]]] if p.get("anchor") in ctx.anchors else ctx.doors()
    best = min(((math.dist(xy, anchor_xy(a)), a["id"]) for a in doors), default=None)
    if best is None or not best[0] < float(p["door_m"]):
        return None
    return best[1], best[0]


@detector("enter_building", defaults={"door_m": 10.0}, instant=True, anchor_param="anchor")
def enter_building(ctx: Context, p: dict) -> list[Detection]:
    out = []
    for i in ctx.scripted("person"):
        s = ctx.state[:, i]
        for k in np.flatnonzero((s == 4) & np.isin(_prev(s), VISIBLE)):
            xy = _xy(ctx, int(k), i)
            hit = _door(ctx, xy, p)
            if hit is not None:
                out.append(Detection("enter_building", (ctx.ids[i],), (ctx.t_ms(k), ctx.t_ms(k)), 1.0,
                                     {"door": hit[0], "door_dist_m": rnd(hit[1])}, xy))
    return out


@detector("exit_building", defaults={"door_m": 10.0}, instant=True, anchor_param="anchor")
def exit_building(ctx: Context, p: dict) -> list[Detection]:
    out = []
    for i in ctx.scripted("person"):
        s = ctx.state[:, i]
        ps = _prev(s)
        first_exit = ctx.routes[ctx.ids[i]][0]["do"] == "exit_building"
        for k in np.flatnonzero(np.isin(s, VISIBLE) & ((ps == 4) | ((ps == 0) & first_exit))):
            xy = _xy(ctx, int(k), i)
            hit = _door(ctx, xy, p)
            if hit is not None:
                out.append(Detection("exit_building", (ctx.ids[i],), (ctx.t_ms(k), ctx.t_ms(k)), 1.0,
                                     {"door": hit[0], "door_dist_m": rnd(hit[1])}, xy))
    return out
