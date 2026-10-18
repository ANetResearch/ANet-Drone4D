"""zone 检测器（M23 §6.9.1）：`zone_enter`、`zone_exit`（瞬时）与 `zone_pass`（持续区间 ≥ `min_dur_s`）。

判据：任一脚本演员可见（状态 1 或 2）且位于 zone 内；zone 为锚点多边形（或点锚点的 `radius_m` 圆）外扩 `buffer_m`（缺省 25 m）。
`params.zone` 指定 zone；未指定时（未声明事件的全量检测）取注释引用的全部 (zone, buffer_m)（`Context.zone_refs`，本文设定：
不对世界中全部 zone 计算，避免大范围 zone 产生大量低信息事件；缓冲取注释给出的值）。进入为"在内"由假变真的首帧（含在 zone 内
出现），离开为由真变假的首帧（含在 zone 内消失）；事件结束时仍在内不记离开。显著性 0.5（§6.9.2）。
"""

from __future__ import annotations

import numpy as np

from awr.sim.events.verify.base import Context, Detection, in_zone, runs
from awr.sim.events.verify.registry import detector

SALIENCE = 0.5
_ZONE = {"buffer_m": 25.0}


def _inside(ctx: Context, p: dict):
    zones = [(p["zone"], float(p["buffer_m"]))] if p.get("zone") else list(ctx.zone_refs)
    for zid, buf in zones:
        a = ctx.anchors.get(zid)
        if a is None:
            continue
        key = ("zone_inside", zid, buf)
        if key not in ctx.cache:
            ctx.cache[key] = in_zone(a, ctx.xy[..., 0], ctx.xy[..., 1], buf) & ctx.vis
        yield zid, ctx.cache[key]


def _xy(ctx: Context, k: int, i: int) -> tuple[float, float]:
    return float(ctx.xy[k, i, 0]), float(ctx.xy[k, i, 1])


@detector("zone_enter", defaults=_ZONE, instant=True, anchor_param="zone")
def zone_enter(ctx: Context, p: dict) -> list[Detection]:
    out = []
    for zid, ins in _inside(ctx, p):
        for i in ctx.scripted():
            m = ins[:, i]
            out += [Detection("zone_enter", (ctx.ids[i],), (ctx.t_ms(k), ctx.t_ms(k)), SALIENCE, {"zone": zid}, _xy(ctx, int(k), i))
                    for k in np.flatnonzero(m & ~np.concatenate([[False], m[:-1]]))]
    return out


@detector("zone_exit", defaults=_ZONE, instant=True, anchor_param="zone")
def zone_exit(ctx: Context, p: dict) -> list[Detection]:
    out = []
    for zid, ins in _inside(ctx, p):
        for i in ctx.scripted():
            m = ins[:, i]
            out += [Detection("zone_exit", (ctx.ids[i],), (ctx.t_ms(k), ctx.t_ms(k)), SALIENCE, {"zone": zid}, _xy(ctx, int(k) - 1, i))
                    for k in np.flatnonzero(~m[1:] & m[:-1]) + 1]
    return out


@detector("zone_pass", defaults={**_ZONE, "min_dur_s": 1.0}, anchor_param="zone")
def zone_pass(ctx: Context, p: dict) -> list[Detection]:
    out = []
    need_ms = round(float(p["min_dur_s"]) * 1000)
    for zid, ins in _inside(ctx, p):
        for i in ctx.scripted():
            for k0, k1 in runs(ins[:, i]):
                if ctx.t_ms(k1) - ctx.t_ms(k0) < need_ms:
                    continue
                out.append(Detection("zone_pass", (ctx.ids[i],), (ctx.t_ms(k0), ctx.t_ms(k1)), SALIENCE, {"zone": zid},
                                     _xy(ctx, (k0 + k1) // 2, i)))
    return out
