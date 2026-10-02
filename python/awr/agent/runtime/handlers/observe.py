"""`thermal.imaging` 与 `rgb.zoom` 两段式处理器（同一实现，传感器参数化；M14 §6.11.1；M14-FR-039、FR-040、FR-042）。

流程：参数校验（非法 → FAILED 110）→ 健康（不健康 → UNAVAILABLE）→ 忙（BUSY）→ 观测点 → 执行前复核估价（不可行 →
UNAVAILABLE code；观测点与复核估价在委派在途期间由 `prepare_observe` 预取，投递时直接取用）→ 第一段 UNVERIFIED（`accepted = 1`、`eta_s`、`task_ref`）→ 从第一段起订阅本机对该目标的检出 →
phase lease：申请 AGENT 租约（被拒 → FAILED 100）→ 必要时 takeoff → phase enroute：goto 观测点（route auto；
`station_reached` = goto succeeded 且 dist_err_m ≤ 3）→ phase on_station：orbit（以目标为圆心，半径 orbit_radius_m，turns 0）→
phase executing：驻留 dwell_s【仿真】（`dwell_complete`，自 goto 终态事件的仿真时刻起算）→ hover → 汇总检出 → 产物描述符 →
最终效果（OK、V4、simulated；无检出时 confidence = 0；结果产生时刻取 hover 终态事件的仿真时刻）；finally：phase returning，
按 previous 交还租约。取消（A14）：先 hover，再以 UNVERIFIED 结束。两个锚点见 §6.13 规则 ⑥（D1-AC-16 倍速一致）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ...guard.pipeline import LeaseDenied
from ..types import CapabilityCall, Effect, EffectStatus, Phase

if TYPE_CHECKING:
    from ..drone_agent import DroneAgent, HandlerCtx, Station

__all__ = ["observe", "parse_observe_args", "prepare_observe"]

log = logging.getLogger("awr.agent.observe")
STATION_TOL_M = 3.0


def parse_observe_args(agent: DroneAgent, capability: str, args: dict[str, Any]) -> dict[str, Any] | str:
    """input_schema 校验与默认值；返回参数或错误说明。"""
    d = {**agent.cat.input_defaults(capability), **args}
    tgt = d.get("target_enu_m")
    if not isinstance(tgt, (list, tuple)) or len(tgt) != 3 or not all(isinstance(v, (int, float)) for v in tgt[:2]) or \
            not (tgt[2] is None or isinstance(tgt[2], (int, float))):
        return "target_enu_m"
    props = ((agent.cat.get(capability) or {}).get("input_schema") or {}).get("properties") or {}
    for k in ("dwell_s", "alt_agl_m", "orbit_radius_m"):
        v = d.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return k
        p = props.get(k) or {}
        if ("minimum" in p and v < p["minimum"]) or ("maximum" in p and v > p["maximum"]):
            return k
    phys = (agent.cat.get(capability) or {}).get("physical") or {}
    rng = phys.get("alt_agl_m")
    if isinstance(rng, list) and len(rng) == 2 and not (float(rng[0]) <= float(d["alt_agl_m"]) <= float(rng[1])):
        return "alt_agl_m"
    if d.get("target_id") is not None and not isinstance(d.get("target_id"), str):
        return "target_id"
    return d


async def prepare_observe(agent: DroneAgent, call: CapabilityCall) -> tuple[Station, dict[str, Any]] | None:
    """委派在途预取（只读，§6.12.2、§6.13 规则 ⑥）：观测点与执行前复核估价；参数非法时不预取（投递后由处理器报 110）。"""
    from ..drone_agent import station_point

    a = parse_observe_args(agent, call.capability, dict(call.args))
    if isinstance(a, str):
        return None
    st = await station_point(agent, a["target_enu_m"], float(a["alt_agl_m"]))
    est = await agent.bridge.estimate(agent.vehicle_id, st.pos, float(a["dwell_s"]), call.capability, None)
    return st, est


async def _take_prepared(agent: DroneAgent, ix: str) -> tuple[Station, dict[str, Any]] | None:
    """取用预取结果；尚未完成时等待它（仍早于重新发起），失败或缺失时返回 None 由处理器现场计算。"""
    take = getattr(agent, "take_prepared", None)
    fut = take(ix) if take is not None else None
    if fut is None:
        return None
    try:
        return await fut
    except asyncio.CancelledError:
        if asyncio.current_task() is not None and asyncio.current_task().cancelling():  # type: ignore[union-attr]
            raise
        return None
    except Exception:
        return None


async def observe(agent: DroneAgent, call: CapabilityCall, ctx: HandlerCtx) -> AsyncIterator[Effect]:
    cap = call.capability
    a = parse_observe_args(agent, cap, dict(call.args))
    if isinstance(a, str):
        yield Effect(EffectStatus.FAILED, message=f"code=110 {a}")
        return
    h = agent.health()
    if h is not None:
        yield Effect(EffectStatus.UNAVAILABLE, message=h)
        return
    if agent.busy_for(cap):
        yield Effect(EffectStatus.UNAVAILABLE, message="BUSY")
        return
    tgt = a["target_enu_m"]
    pre = await _take_prepared(agent, call.ix)
    if pre is not None:
        st, est = pre  # 委派在途期间预取的观测点与执行前复核估价（§6.12.2）
        ctx.register_station(st, float(a["alt_agl_m"]))
    else:
        st = await ctx.station(tgt, float(a["alt_agl_m"]))
        try:
            est = await agent.bridge.estimate(agent.vehicle_id, st.pos, float(a["dwell_s"]), cap, None)
        except Exception as ex:
            log.warning("estimate failed", extra={"kv": {"uav": agent.vehicle_id, "err": repr(ex)}})
            yield Effect(EffectStatus.UNAVAILABLE, message="code=211")
            return
    if not est.get("feasible", False):
        yield Effect(EffectStatus.UNAVAILABLE, message=f"code={int(est.get('code', 0) or 0)}")
        return
    eta = float(est.get("eta_s") or 0.0)
    ctx.last_eta_s = eta
    yield Effect(EffectStatus.UNVERIFIED, verify_trust=0, protocol="awr.sim", requested=f"{cap} {list(tgt)}",
                 metrics={"accepted": 1.0, "eta_s": eta}, observed_state=f"task_ref={call.ix}")
    sub = ctx.subscribe_detections(capability=cap, target_id=a.get("target_id"), near=tgt)
    ctx.phase(Phase.LEASE, eta_s=eta, progress=0.0)
    try:
        await ctx.port.acquire()
    except LeaseDenied as ex:
        sub.close()
        yield Effect(EffectStatus.FAILED, verify_trust=2, message=f"code={ex.code} lease", observed_state="LEASE_DENIED")
        return
    final: Effect | None = None
    dist_err: float | None = None
    t_on = 0
    try:
        if not ctx.airborne():
            r = await ctx.port.call("takeoff", alt_m=st.takeoff_agl_m)
            if not r.ok:
                final = Effect(EffectStatus.FAILED, verify_trust=2, observed_state=f"takeoff {r.status} {r.code}",
                               message=f"code={r.code}")
        if final is None:
            ctx.phase(Phase.ENROUTE, eta_s=eta, progress=0.1)
            r = await ctx.port.call("goto", pos=list(st.pos), route="auto")
            t_on = r.t_sim_ns  # 到站时刻（goto 终态事件的仿真时刻），驻留从此计时
            dist_err = (r.effect.metrics.get("dist_err_m") if r.effect is not None else None)
            ctx.test("station_reached", r.ok and (dist_err if dist_err is not None else 1e9) <= STATION_TOL_M)
            if not r.ok:
                final = Effect(EffectStatus.FAILED, verify_trust=2, observed_state=f"goto {r.status} {r.code}",
                               message=f"code={r.code}")
        if final is None:
            ctx.phase(Phase.ON_STATION, eta_s=0.0, progress=0.6)
            if float(a["orbit_radius_m"]) >= 1.0:
                await ctx.port.call_nowait("orbit", center=list(st.orbit_center), radius_m=float(a["orbit_radius_m"]), turns=0)
            ctx.phase(Phase.EXECUTING, eta_s=0.0, progress=0.7)
            # 驻留 dwell_s【仿真】自到站时刻起算：多进程下处理器收到 goto 终态事件有墙钟滞后（×10 时约 0.1–0.5 s 仿真），
            # 以事件时刻为锚点使 hover 下发时刻与倍速无关（§6.13 规则 ⑥）；锚点缺失时退回当前时刻
            t0 = t_on if t_on > 0 else ctx.now_ns()
            await ctx.sleep_until(t0 + round(float(a["dwell_s"]) * 1e9))
            ctx.test("dwell_complete", True)
            rh = await ctx.port.call("hover")
            ctx.event_time(rh.t_sim_ns)  # 结果产生于 hover 终态时刻；其后交还租约的往返不计入结果回传时刻
            dets = sub.close()
            arts = [ctx.artifact_descriptor(d) for d in dets if d.artifact]
            final = ctx.effect_from(dets, arts, dist_err, t_end_ns=rh.t_sim_ns)
    except asyncio.CancelledError:
        # 取消（A14）：先下发 hover（不等待仿真时间内的完成），再交还租约；两者只等待墙钟 bus 回复
        with contextlib.suppress(Exception):
            await ctx.port.call_nowait("hover")
        sub.close()
        ctx.phase(Phase.RETURNING)
        with contextlib.suppress(Exception):
            await ctx.port.release("previous")
        raise
    except Exception as ex:
        log.exception("observe handler failed")
        final = Effect(EffectStatus.FAILED, message=f"handler {type(ex).__name__}")
    sub.close()
    ctx.phase(Phase.RETURNING, eta_s=0.0, progress=1.0)
    with contextlib.suppress(Exception):
        await ctx.port.release("previous")
    yield final
