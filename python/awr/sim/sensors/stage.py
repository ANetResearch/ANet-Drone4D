"""sensors stage 与相位调度（M13-FR-010；M13 §6.5.1；M08 §6.4）。

`@register_stage("sensors", every=5, phase=2, order=100, owner="M13", fidelity=Fidelity.ALL, budget_core=0.010)`，50 Hz；
调用序号 `c = (tick − 2) // 5`（与 M08 `StageCtx.call_index` 等价，但不随流水线重建归零）：

- 每次：装配同步、外部来源锁存、动态云台集（dt = 0.02 s）、GNSS 分片 `slot % 5 == c % 5`（每 slot 10 Hz）；
- `c % 5 == 2`：IMU 偏置分片 `slot % 10 == (c // 5) % 10`（每 slot 1 Hz）；
- `c % 10 == 4`：检测器（5 Hz，目标表为空时直接返回）；
- `c % 10 == 9`：剧本事件桥（`scenario.event` 的 `target.spawn` 与带 `caps`/`sensors` 的 `vehicle.add`，5 Hz；M10 改为直接调用
  `spawn_target`、`configure_vehicle` 后同名目标按 TARGET_ID_DUP 忽略）。
检测 tick 与 IMU 分片不同相位，任一次调用至多叠加 GNSS、IMU、检测中的两项。快进时 stage 频率不变。

D2（M13-FR-203–206，§15.5）：每次调用先执行到期的机体命令（`gimbal`、`zoom`、`sensor/mode`，按 apply_tick），再推进指向源
到期与 track 跟随、照度档变化时的 auto 成像模式切换（同一 tick 生效）、变焦（`dt` = 本 stage 步长），最后才是 D1 云台推进。
调用序号与步长：M08 给出 D2 时钟档（`ctx.profile`）时取 `ctx.call_index`、`ctx.stage_dt`（M08 §15.7 变更请求 ②），否则按 d1_250 推算。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["EVERY", "ORDER", "PHASE", "make_stage"]

EVERY, PHASE, ORDER = 5, 2, 100
log = logging.getLogger("awr.sim.sensors")


def call_index(tick: int) -> int:
    return (int(tick) - PHASE) // EVERY


def _call_and_dt(ctx: Any, tick: int) -> tuple[int, float]:
    """(调用序号, 本 stage 步长 s)：M08 给出 D2 时钟档（`ctx.profile` 非空）时取 `ctx.call_index` 与 `ctx.stage_dt`，否则按 d1_250
    的 `(tick − 2) // 5` 与 5 tick（D1 行为逐位不变；`call_index` 随流水线重建归零，D1 不用它）。"""
    if getattr(ctx, "profile", None) is not None:
        ci = getattr(ctx, "call_index", None)
        sdt = float(getattr(ctx, "stage_dt", 0.0) or 0.0)
        c = int(ci) if isinstance(ci, (int, np.integer)) else call_index(tick)
        return c, (sdt if sdt > 0.0 else EVERY * float(ctx.dt_tick))
    return call_index(tick), EVERY * float(ctx.dt_tick)


def make_stage(rt: SensorRuntime) -> Callable[[Any, Any], None]:
    def sensors_stage(S: Any, ctx: Any) -> None:
        try:
            _body(S, ctx)
        except Exception:  # 故障隔离：传感器仿真的缺陷不得拖垮 sim-core（P-09）；计数并按 2 的幂节流记录
            if getattr(rt, "strict", False):  # 测试中直接抛出
                raise
            rt.stats["errors"] = rt.stats.get("errors", 0) + 1
            n = rt.stats["errors"]
            if n & (n - 1) == 0:
                log.exception("sensors stage failed", extra={"kv": {"tick": int(getattr(ctx, "tick", -1)), "count": n}})

    def _body(S: Any, ctx: Any) -> None:
        rt.sync(S, ctx)
        tick = int(ctx.tick)
        rt.gimbal.apply_pending(tick)
        c, dt = _call_and_dt(ctx, tick)
        rt.stats["calls"] += 1
        if rt.pblk is not None:
            if rt.pending_cmds:
                rt.apply_pending_cmds(tick)
            if rt.d2_live or rt.pblk["src"].any():  # D1 机型（挂载掩码 0、只有缺省指向源）不进入 D2 推进
                t_ns = int(getattr(ctx, "t_ns", 0))
                rt.gimbal.step_sources(t_ns)
                rt.payload.step_modes(ctx)
                rt.payload.step_zoom(dt)
        rt.gimbal.step(S, dt)
        gp = c % 5
        rt.gnss.step_part(S, ctx, gp)
        ip = None
        if c % 5 == 2:
            ip = (c // 5) % 10
            rt.imu.bias_part(S, ctx, ip)
        det = False
        if c % 10 == 4 and rt.detector.enabled():
            det = True
            rt.detector.tick(S, ctx)
        if c % 10 == 9:
            bridge_events(rt, ctx)
        tr = getattr(rt, "trace", None)
        if tr is not None:
            tr.append((tick, c, gp, ip, det))

    return sensors_stage


def bridge_events(rt: SensorRuntime, ctx: Any) -> int:
    """读取 sim-core 事件环中新的 `scenario.event`（M10 剧本导演，AWR-16 §12.3），落实 `target.spawn` 与 `vehicle.add` 的
    `caps`、`sensors`。只读 EventPublisher 的公开 `replay(since, epoch)`；apply_tick 为本次 stage 的 tick（确定）。"""
    from .detector import TargetError

    ev = getattr(ctx, "events", None)
    if ev is None or not callable(getattr(ev, "replay", None)):
        return 0
    last = int(getattr(ev, "last_seq", 0))
    if last < rt.ev_seen:
        rt.ev_seen = 0
    if last == rt.ev_seen:
        return 0
    try:
        rep = ev.replay(rt.ev_seen, getattr(ev, "epoch", 0))
    except Exception:
        return 0
    rt.ev_seen = last
    n = 0
    for e in rep.get("events", ()):
        if e.get("kind") != "scenario.event":
            continue
        data = e.get("data") or {}
        act = data.get("action")
        args = data.get("args") or {}
        if act == "target.spawn":
            try:
                rt.spawn_target(str(args.get("target_id")), args.get("pos_enu_m") or [0, 0, 0],
                                str(args.get("kind", "generic")), conf_first=args.get("conf_first"),
                                conf_confirm=args.get("conf_confirm"), apply_tick=int(ctx.tick))
                rt.stats["bridged_targets"] += 1
                n += 1
            except TargetError:
                pass
        elif act == "vehicle.add" and (args.get("caps") is not None or args.get("sensors") is not None):
            rt.configure_vehicle(str(args.get("vehicle_id")), sensors=args.get("sensors"), caps=args.get("caps"))
    return n
