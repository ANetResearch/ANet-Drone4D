"""`relay.communication`（P2；M14-FR-040）：只做定点驻留，无链路质量模型（V0.6）。

参数 `station_enu_m`、`alt_agl_m = 150`、`hold_s = 0`（0 表示直到取消）；度量 `hold_s`、`dist_err_m`。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ...guard.pipeline import LeaseDenied
from ..types import CapabilityCall, Effect, EffectStatus, Phase

if TYPE_CHECKING:
    from ..drone_agent import DroneAgent, HandlerCtx

__all__ = ["relay_communication"]


async def relay_communication(agent: DroneAgent, call: CapabilityCall, ctx: HandlerCtx) -> AsyncIterator[Effect]:
    a = dict(call.args)
    stn = a.get("station_enu_m") or a.get("target_enu_m")
    alt = a.get("alt_agl_m", 150.0)
    hold = a.get("hold_s", 0.0)
    if not isinstance(stn, (list, tuple)) or len(stn) != 3 or not isinstance(alt, (int, float)) or not isinstance(hold, (int, float)):
        yield Effect(EffectStatus.FAILED, message="code=110")
        return
    h = agent.health()
    if h is not None:
        yield Effect(EffectStatus.UNAVAILABLE, message=h)
        return
    if agent.busy_for(call.capability):
        yield Effect(EffectStatus.UNAVAILABLE, message="BUSY")
        return
    st = await ctx.station(stn, float(alt))
    yield Effect(EffectStatus.UNVERIFIED, protocol="awr.sim", metrics={"accepted": 1.0}, observed_state=f"task_ref={call.ix}")
    ctx.phase(Phase.LEASE)
    try:
        await ctx.port.acquire()
    except LeaseDenied as ex:
        yield Effect(EffectStatus.FAILED, verify_trust=2, message=f"code={ex.code} lease")
        return
    t0 = ctx.now_s()
    final: Effect
    try:
        if not ctx.airborne():
            await ctx.port.call("takeoff", alt_m=min(120.0, float(alt)))
        ctx.phase(Phase.ENROUTE)
        r = await ctx.port.call("goto", pos=list(st.pos), route="auto")
        dist = r.effect.metrics.get("dist_err_m", 0.0) if r.effect is not None else 0.0
        if not r.ok:
            final = Effect(EffectStatus.FAILED, verify_trust=2, message=f"code={r.code}")
        else:
            ctx.phase(Phase.EXECUTING)
            t0 = ctx.now_s()
            await ctx.sleep_s(float(hold) if hold > 0 else 1e9)
            final = Effect(EffectStatus.OK, verify_trust=4, simulated=True, protocol="awr.sim",
                           metrics={"hold_s": round(ctx.now_s() - t0, 3), "dist_err_m": float(dist)})
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await ctx.port.call_nowait("hover")
        with contextlib.suppress(Exception):
            await ctx.port.release("previous")
        raise
    ctx.phase(Phase.RETURNING)
    with contextlib.suppress(Exception):
        await ctx.port.release("previous")
    yield final
