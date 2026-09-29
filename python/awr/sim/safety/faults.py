"""故障注入 FaultInjector 与 faults stage（D1-ext；M09 §6.12；FR-090 至 FR-092；只限 Mock 后端）。

契约（MS1 冻结，core）：`packages/contracts/rt/faults.schema.json`（kind、params、`at_s`、`duration_s`；越界 422 `110`）、RNG 流
`faults`（stream_id 2）、剧本动作 `fault.inject`、REST R16/R62（api 经 `ctl/sim-core/query` 的 `safety/fault` 路由转发，
见实现报告请求）。生命周期 ARMED → ACTIVE → CLEARED；注入与清除按 apply_tick 生效，写 `safety.fault` 事件与输入日志。

L1 模型：
- `thrust_loss{frac}`：faults stage 写 M08 的 `thrust_scale = 1 − frac`（只作用于施加到机体的推力，M08-FR-035）；
- `motor_fail{motor, omega_fail_rad_s}`：清除 `motor_ok` 的对应位（M08 融合核施加不可控滚转，ω_fail 取 M08 FleetConfig）；
- `state_drop`：M08 尚无冻结状态估计的输入（§14 第 10 条），M09 以内部状态年龄叠加量驱动 FastGuard 的状态缺失判据；
- `gnss_denied`：LOC_OK = 0（空中 HOLD/LOC_LOST，30 s 后 LANDING；地面 arm 返回 113）；
- `link_drop{side = fcu}`：FCU_LINK = 0，机上按 B 类阶梯（3 s HOLD、13 s RTL）；地面侧生命周期投影属 M08；
- `battery_drain{rate_pct_s | set_soc}`：电量额外下降或直接置值。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.contracts.reasons import Reason

from .flight_fsm import S_HOLD_LINK, S_HOLD_LOC, Origin
from .state import FS

if TYPE_CHECKING:
    from .service import SafetyRuntime

__all__ = ["FAULT_BITS", "Fault", "FaultError", "FaultInjector"]

KINDS = ("thrust_loss", "motor_fail", "link_drop", "state_drop", "gnss_denied", "battery_drain")
FAULT_BITS = {"thrust_loss": 1, "motor_fail": 2, "link_drop": 4, "state_drop": 8, "gnss_denied": 16, "battery_drain": 32}
TICK_S = 0.004


class FaultError(ValueError):
    def __init__(self, code: int, detail: Any) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = int(code)
        self.detail = detail


@dataclass
class Fault:
    fault_id: str
    slot: int
    kind: str
    params: dict
    apply_tick: int
    end_tick: int | None
    state: str = "ARMED"
    since_t_ns: int = 0
    extra: dict = field(default_factory=dict)

    def row(self) -> dict:
        return {"fault_id": self.fault_id, "kind": self.kind, "since_t_ns": int(self.since_t_ns), "params": dict(self.params)}


def validate(kind: str, params: dict | None, at_s: Any, duration_s: Any, n_rot: int = 4) -> dict:
    """faults.schema.json 的取值范围（越界 110 PARAM_OUT_OF_RANGE，未知字段 300 BAD_REQUEST）。"""
    if kind not in KINDS:
        raise FaultError(int(Reason.PARAM_OUT_OF_RANGE), {"field": "kind", "value": kind})
    p = dict(params or {})

    def num(name: str, default: float, lo: float, hi: float, lo_open: bool = False) -> float:
        v = p.get(name, default)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or (v <= lo if lo_open else v < lo) or v > hi:
            raise FaultError(int(Reason.PARAM_OUT_OF_RANGE), {"field": f"params.{name}", "value": v, "range": [lo, hi]})
        return float(v)

    allowed: set[str] = set()
    if kind == "thrust_loss":
        allowed = {"frac"}
        p["frac"] = num("frac", 0.45, 0.0, 1.0, lo_open=True)
    elif kind == "motor_fail":
        allowed = {"motor", "omega_fail_rad_s"}
        m = p.get("motor", 0)
        if not isinstance(m, int) or isinstance(m, bool) or not 0 <= m <= n_rot - 1:
            raise FaultError(int(Reason.PARAM_OUT_OF_RANGE), {"field": "params.motor", "value": m, "range": [0, n_rot - 1]})
        p["motor"] = m
        p["omega_fail_rad_s"] = num("omega_fail_rad_s", 4.0, 0.0, 20.0, lo_open=True)
    elif kind == "link_drop":
        allowed = {"side"}
        if p.get("side", "fcu") != "fcu":
            raise FaultError(int(Reason.PARAM_OUT_OF_RANGE), {"field": "params.side", "value": p.get("side")})
        p["side"] = "fcu"
    elif kind == "battery_drain":
        allowed = {"rate_pct_s", "set_soc"}
        if "rate_pct_s" in p and "set_soc" in p:
            raise FaultError(int(Reason.PARAM_OUT_OF_RANGE), {"field": "params", "why": "rate_pct_s and set_soc are exclusive"})
        if "set_soc" in p:
            p["set_soc"] = num("set_soc", 0.0, 0.0, 1.0)
        else:
            p["rate_pct_s"] = num("rate_pct_s", 0.5, 0.0, 10.0, lo_open=True)
    extra = set(p) - allowed
    if extra:
        raise FaultError(int(Reason.BAD_REQUEST), {"field": "params", "unknown": sorted(extra)})
    if at_s is not None and (not isinstance(at_s, (int, float)) or not 0 <= at_s <= 3600):
        raise FaultError(int(Reason.PARAM_OUT_OF_RANGE), {"field": "at_s", "value": at_s, "range": [0, 3600]})
    if duration_s is not None and (not isinstance(duration_s, (int, float)) or not 0 < duration_s <= 3600):
        raise FaultError(int(Reason.PARAM_OUT_OF_RANGE), {"field": "duration_s", "value": duration_s, "range": [0, 3600]})
    return p


class FaultInjector:
    def __init__(self, rt: SafetyRuntime) -> None:
        self.rt = rt
        self.faults: dict[str, Fault] = {}
        self.seq = 0
        self.log: list[dict] = []  # 审计与输入日志的本地副本（测试核对）

    def reset(self) -> None:
        self.faults.clear()

    # ---------------------------------------------------------------- 入口
    def inject(self, slot: int, kind: str, params: dict | None = None, at_s: float | None = None,
               duration_s: float | None = None, *, principal: dict | None = None, backend: str = "mock") -> tuple[str, int]:
        rt = self.rt
        if backend != "mock":
            raise FaultError(int(Reason.BACKEND_UNSUPPORTED), {"why": "FAULTS_MOCK_ONLY"})
        if principal is not None and principal.get("role") == "agent":
            raise FaultError(int(Reason.ROLE_FORBIDDEN), {"why": "AGENT_FAULT"})
        if rt.S is None or not (0 <= slot < rt.S.capacity) or not rt.S.active[slot]:
            raise FaultError(int(Reason.NO_VEHICLE), {"slot": slot})
        n_rot = 4
        if rt.profiles is not None:
            n_rot = int(rt.profiles.n_rot[int(rt.S.profile_id[slot])])
        p = validate(kind, params, at_s, duration_s, n_rot)
        tick = rt.tick
        apply_tick = tick + 1 + (0 if at_s is None else round(float(at_s) / TICK_S))
        end = None if duration_s is None else apply_tick + round(float(duration_s) / TICK_S)
        self.seq += 1
        fid = f"f{self.seq:06d}"
        f = Fault(fid, int(slot), kind, p, apply_tick, end)
        self.faults[fid] = f
        rec = {"op": "inject", "fault_id": fid, "slot": int(slot), "kind": kind, "params": p, "apply_tick": apply_tick,
               "end_tick": end}
        self.log.append(rec)
        if rt.inputlog is not None:
            with contextlib.suppress(Exception):
                rt.inputlog.append("fault", apply_tick, rec)
        return fid, apply_tick

    def clear(self, fault_id: str) -> int:
        f = self.faults.get(fault_id)
        if f is None or f.state == "CLEARED":
            raise FaultError(int(Reason.NOT_FOUND), {"fault_id": fault_id})
        f.end_tick = self.rt.tick + 1
        rec = {"op": "clear", "fault_id": fault_id, "apply_tick": f.end_tick}
        self.log.append(rec)
        if self.rt.inputlog is not None:
            with contextlib.suppress(Exception):
                self.rt.inputlog.append("fault", f.end_tick, rec)
        return f.end_tick

    def row_faults(self, slot: int) -> list[dict]:
        return [f.row() for f in self.faults.values() if f.slot == slot and f.state == "ACTIVE"]

    # ---------------------------------------------------------------- faults stage（every 2，order 25）
    def step(self, ctx: Any) -> None:
        rt = self.rt
        if not self.faults:
            return
        S, sb = rt.S, rt.sb
        tick = rt.tick
        t = rt.t_ns
        for f in list(self.faults.values()):
            if f.state == "ARMED" and tick >= f.apply_tick:
                if not S.active[f.slot]:
                    f.state = "CLEARED"
                    continue
                f.state = "ACTIVE"
                f.since_t_ns = t
                f.extra["t0"] = t
                sb["fault_mask"][f.slot] |= FAULT_BITS[f.kind]
                rt.fsm.flags_dirty = True
                rt.set_cond(np.array([f.slot]), "FAULT_ACTIVE", True)
                rt.sink.add(f.slot, rt.code("SAF.FAULT.INJECTED"), t, detail=f"{f.kind}:{f.fault_id}", origin=Origin.OPERATOR)
                if f.kind == "battery_drain" and "set_soc" in f.params and rt.bat is not None:
                    rt.bat.set_soc(np.array([f.slot]), f.params["set_soc"])
            if f.state == "ACTIVE" and ((f.end_tick is not None and tick >= f.end_tick) or not S.active[f.slot]
                                        or sb["fs"][f.slot] == FS.CRASHED):
                self._deactivate(f, t)
        # 执行器故障乘子与内部量（每 2 tick 重写，保证 M08 融合核本 tick 读到）
        active = [f for f in self.faults.values() if f.state == "ACTIVE"]
        for f in active:
            s = f.slot
            if f.kind == "thrust_loss":
                S.thrust_scale[s] = np.float32(1.0 - f.params["frac"])
            elif f.kind == "motor_fail":
                S.motor_ok[s] = np.uint8(0xFF & ~(1 << int(f.params["motor"])))
            elif f.kind == "state_drop":
                sb["est_age_extra_s"][s] = np.float32((t - f.extra["t0"]) * 1e-9)
            elif f.kind == "battery_drain" and "rate_pct_s" in f.params and rt.bat is not None:
                S.blocks["battery"]["drain_pct_s"][s] = np.float32(f.params["rate_pct_s"])
        self._onboard(active, t)
        if not any(f.state != "CLEARED" for f in self.faults.values()):
            self.faults = {k: v for k, v in self.faults.items() if v.state != "CLEARED"}

    def _deactivate(self, f: Fault, t: int) -> None:
        rt = self.rt
        S, sb = rt.S, rt.sb
        f.state = "CLEARED"
        s = f.slot
        sb["fault_mask"][s] &= np.uint8(0xFF & ~FAULT_BITS[f.kind])
        rt.fsm.flags_dirty = True
        if f.kind == "thrust_loss":
            S.thrust_scale[s] = np.float32(1.0)
        elif f.kind == "motor_fail":
            S.motor_ok[s] = np.uint8(0xFF)
        elif f.kind == "state_drop":
            sb["est_age_extra_s"][s] = np.float32(0.0)
        elif f.kind == "battery_drain":
            S.blocks["battery"]["drain_pct_s"][s] = np.float32(0.0)
        if sb["fault_mask"][s] == 0:
            rt.set_cond(np.array([s]), "FAULT_ACTIVE", False)
        rt.sink.add(s, rt.code("SAF.FAULT.CLEARED"), t, detail=f"{f.kind}:{f.fault_id}", origin=Origin.OPERATOR)

    def _onboard(self, active: list[Fault], t: int) -> None:
        """机上侧：link_drop 按 B 类阶梯；gnss_denied 空中 HOLD/LOC_LOST。"""
        rt = self.rt
        S, sb = rt.S, rt.sb
        L = rt.params.link
        for f in active:
            s = f.slot
            if not S.in_air[s]:
                continue
            if f.kind == "link_drop":
                age = (t - f.extra["t0"]) * 1e-9
                fs = int(sb["fs"][s])
                if age >= L.rtl_s and fs in (FS.TAKING_OFF, FS.FLYING, FS.CORRECTING, FS.HOLD):
                    rt.fsm.propose(np.array([s]), int(FS.RTL), 0, Origin.AUTO, "SAF.LINK.LOST_RTL", value=age,
                                   thr=L.rtl_s, detail="FCU")
                elif age >= L.hold_s and fs in (FS.TAKING_OFF, FS.FLYING, FS.CORRECTING):
                    rt.fsm.propose(np.array([s]), int(FS.HOLD), S_HOLD_LINK, Origin.AUTO, "SAF.LINK.LOST_HOLD", value=age,
                                   thr=L.hold_s, detail="FCU")
            elif f.kind == "gnss_denied":
                if int(sb["fs"][s]) in (FS.TAKING_OFF, FS.FLYING, FS.CORRECTING):
                    rt.set_cond(np.array([s]), "LOC_LOST", True)
                    rt.fsm.propose(np.array([s]), int(FS.HOLD), S_HOLD_LOC, Origin.AUTO, "SAF.EST.LOC_LOST")
