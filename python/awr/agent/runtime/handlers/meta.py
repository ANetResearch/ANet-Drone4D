"""元能力处理器（M14 §6.4.3、§6.10.2；M14-FR-013、FR-014、FR-023）。

`task.quote`（提供方侧）：健康检查（不健康即 UNAVAILABLE，message 为原因）→ 忙则 UNAVAILABLE（BUSY）→ 观测高度越界即不可行
（110）→ 计算观测点 → 调用 `ctl/sim-core/estimate`（`vehicle_id`、`target_enu_m` 为观测点、`dwell_s`、`capability`、
`speed_mps = null`）→ 返回 OK 效果，`metrics = {eta_s, energy_wh, soc_after_pct, feasible, code, conf_expected, load, wind_mps,
rain_mmh, mor_m}`。估价回复缺 `conf_expected`、`env_target` 时按 §6.10.5 退化式与 `env/query` 补齐。可行性只由 estimate 判定。
观测点高度经 SharedReads 缓存，环境查询与估价并行发出（§6.13 规则 ⑥：报价在 t_send + L 之前完成，到达时刻与倍速无关）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from awr.contracts.enums import FLIGHTSTATE_BY_NAME

from ..drone_agent import consume
from ..scoring import EnvAtTarget, conf_expected_fallback
from ..types import CapabilityCall, Effect, EffectStatus

if TYPE_CHECKING:
    from ..drone_agent import DroneAgent, HandlerCtx

__all__ = ["agent_describe", "agent_state", "task_quote"]

log = logging.getLogger("awr.agent.meta")


async def agent_describe(agent: DroneAgent, call: CapabilityCall, ctx: HandlerCtx) -> AsyncIterator[Effect]:
    yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, protocol="awr.agent",
                 observed_state=json.dumps(agent.describe(), sort_keys=True, separators=(",", ":"), ensure_ascii=False))


async def agent_state(agent: DroneAgent, call: CapabilityCall, ctx: HandlerCtx) -> AsyncIterator[Effect]:
    row = ctx.row()
    if row is None:
        yield Effect(EffectStatus.UNAVAILABLE, message="NO_STATE")
        return
    m = {"soc_pct": float(row.battery_pct), "load": float(agent.load), "flight_state": float(FLIGHTSTATE_BY_NAME.get(row.flight_state, 0)),
         "pos_x_m": row.pos[0], "pos_y_m": row.pos[1], "pos_z_m": row.pos[2]}
    yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, protocol="awr.agent", metrics=m,
                 observed_state=json.dumps({"flight_state": row.flight_state, "lifecycle": row.lifecycle, "owner": row.owner}))


def _num(v: Any, d: float) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else d


async def task_quote(agent: DroneAgent, call: CapabilityCall, ctx: HandlerCtx) -> AsyncIterator[Effect]:
    a = dict(call.args)
    cap = str(a.get("capability", ""))
    if cap not in agent.member_caps:
        yield Effect(EffectStatus.UNAVAILABLE, message="not served")
        return
    h = agent.health()
    if h is not None:
        yield Effect(EffectStatus.UNAVAILABLE, message=h)
        return
    if agent.busy_for(cap):
        yield Effect(EffectStatus.UNAVAILABLE, message="BUSY")
        return
    entry = agent.cat.get(cap) or {}
    defaults = agent.cat.input_defaults(cap)
    cargs = {**defaults, **(a.get("args") or {})}
    tgt = a.get("target_enu_m") or cargs.get("target_enu_m")
    if not isinstance(tgt, (list, tuple)) or len(tgt) != 3:
        yield Effect(EffectStatus.FAILED, message="code=110 target_enu_m")
        return
    phys = entry.get("physical") or {}
    alt = _num(cargs.get("alt_agl_m"), 60.0)
    dwell = _num(cargs.get("dwell_s"), 10.0)
    rng = phys.get("alt_agl_m")
    load = float(agent.load)
    if isinstance(rng, list) and len(rng) == 2 and not (float(rng[0]) <= alt <= float(rng[1])):
        yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, protocol="awr.sim",
                     metrics={"eta_s": 0.0, "energy_wh": 0.0, "soc_after_pct": 0.0, "feasible": 0.0, "code": 110.0,
                              "conf_expected": 0.0, "load": load})
        return
    st = await ctx.station(tgt, alt)
    # 估价与目标处环境互不依赖，并行发出（估价回复缺 env_target 时才用环境查询的结果；同一目标的并发查询由 SharedReads 合并）
    env_f = asyncio.ensure_future(agent.reads.env(st.pos))
    env_f.add_done_callback(consume)
    try:
        est = await agent.bridge.estimate(agent.vehicle_id, st.pos, dwell, cap, None)
    except Exception as ex:
        env_f.cancel()
        log.warning("estimate failed", extra={"kv": {"uav": agent.vehicle_id, "err": repr(ex)}})
        yield Effect(EffectStatus.UNAVAILABLE, message="code=211")
        return
    code = int(est.get("code", 0) or 0)
    if "eta_s" not in est and code in (111, 211, 213):
        env_f.cancel()
        yield Effect(EffectStatus.UNAVAILABLE, message=f"code={code}")
        return
    env_t = est.get("env_target")
    if isinstance(env_t, dict):
        env_f.cancel()
        env = EnvAtTarget(_num(env_t.get("wind_mps"), 0.0), _num(env_t.get("rain_mmh"), 0.0), _num(env_t.get("mor_m"), 20000.0))
    else:
        try:
            env = await env_f or EnvAtTarget()
        except Exception:
            env = EnvAtTarget()
    sensor = (agent.manifest_skill(cap) or {}).get("physical", {}).get("sensor") if hasattr(agent, "manifest_skill") else None
    sensor = sensor or (phys.get("sensor") or {})
    conf = est.get("conf_expected")
    if not isinstance(conf, (int, float)):
        conf = conf_expected_fallback(_num(sensor.get("p0"), 0.9), _num(sensor.get("r_fp_m"), 150.0), alt, env.mor_m)
    feasible = bool(est.get("feasible", False))
    m = {"eta_s": _num(est.get("eta_s"), 0.0), "energy_wh": _num(est.get("energy_wh"), 0.0),
         "soc_after_pct": _num(est.get("soc_after_pct"), 0.0), "feasible": 1.0 if feasible else 0.0, "code": float(code),
         "conf_expected": round(float(conf), 6), "load": load, "wind_mps": env.wind_mps, "rain_mmh": env.rain_mmh,
         "mor_m": env.mor_m}
    yield Effect(EffectStatus.OK, verify_trust=4, simulated=True, protocol="awr.sim", requested=f"task.quote {cap}", metrics=m)
