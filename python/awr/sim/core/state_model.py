"""规范状态模型（M08-FR-049、FR-052；由 `.cache/research/g04/state_model.py` 转正，AWR-03 §8.7）。

内容：FlightState 14 态与子模式（取自生成的 `awr.contracts.enums`）、PX4 与 Prometheus 原生态到规范态的推导
（V0.2 适配器用，D1 为纯函数与测试替身）、Mock 的 PX4 显示仿真（`state_ext.px4`）、生命周期与定位覆盖层、
准入函数与准入矩阵（`admission_matrix()`，与 `packages/contracts/rt/commands.json` 的矩阵逐格一致，由
`tests/contracts/test_state_model.py::test_m08_state_model_if_present` 对拍）、Full64 字节打包（`pack_fs`、`pack_ctrl`
直接使用契约生成物）。

迁移要求（AWR-03 §8.7 与附录 B）：枚举与子模式名取契约；命令参数带单位后缀；SafetyStop 对 agent 不可用。
原型中分号连写的单行语句保留，便于与原型逐行对照。
"""
# ruff: noqa: E701, E702

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from awr.contracts.enums import FLIGHT_SUB, FlightFlags, FlightState, Native, Owner, PoseSrc
from awr.contracts.layouts import pack_ctrl, pack_fs, unpack_ctrl, unpack_fs

__all__ = [
    "FS",
    "NAV",
    "SUB",
    "Intent",
    "Native",
    "Owner",
    "PoseSrc",
    "PromRaw",
    "Px4Raw",
    "admission_matrix",
    "admit",
    "apply_lifecycle",
    "custom_mode",
    "derive_prometheus",
    "derive_px4",
    "mock_emulate_px4",
    "mock_native",
    "native_px4",
    "nav_from_custom_mode",
    "pack_ctrl",
    "pack_fs",
    "sub",
    "unpack_ctrl",
    "unpack_fs",
]

FS = FlightState
SUB: dict[FlightState, list[str | None]] = {FS(k): list(v) for k, v in FLIGHT_SUB.items()}


def sub(fs: FlightState, name: str) -> int:
    return SUB[fs].index(name)


F_ARMED, F_IN_AIR, F_LOC_OK, F_FAILSAFE = int(FlightFlags.ARMED), int(FlightFlags.IN_AIR), int(FlightFlags.LOC_OK), int(FlightFlags.FAILSAFE)
F_GCS, F_FCU, F_LOC_DEG, F_ALERT = int(FlightFlags.GCS_LINK), int(FlightFlags.FCU_LINK), int(FlightFlags.LOC_DEGRADED), int(FlightFlags.ALERT)


# ---------------------------------------------------------------- PX4（msg/versioned/VehicleStatus.msg v4，px4_custom_mode.h）
class NAV(IntEnum):
    MANUAL = 0; ALTCTL = 1; POSCTL = 2; AUTO_MISSION = 3; AUTO_LOITER = 4; AUTO_RTL = 5; POSITION_SLOW = 6
    GUIDED_COURSE = 7; ALTITUDE_CRUISE = 8; MANUAL_PARKING = 9; ACRO = 10; FREE2 = 11; DESCEND = 12
    TERMINATION = 13; OFFBOARD = 14; STAB = 15; FREE1 = 16; AUTO_TAKEOFF = 17; AUTO_LAND = 18
    AUTO_FOLLOW_TARGET = 19; AUTO_PRECLAND = 20; ORBIT = 21; AUTO_VTOL_TAKEOFF = 22
    EXTERNAL1 = 23; EXTERNAL2 = 24; EXTERNAL3 = 25; EXTERNAL4 = 26; EXTERNAL5 = 27; EXTERNAL6 = 28
    EXTERNAL7 = 29; EXTERNAL8 = 30


# get_px4_custom_mode()：nav_state -> (main, sub)
NAV_TO_CM = {
    NAV.MANUAL: (1, 0), NAV.ALTCTL: (2, 0), NAV.ALTITUDE_CRUISE: (11, 0), NAV.MANUAL_PARKING: (12, 0),
    NAV.POSCTL: (3, 0), NAV.POSITION_SLOW: (3, 2), NAV.AUTO_MISSION: (4, 4), NAV.AUTO_LOITER: (4, 3),
    NAV.AUTO_RTL: (4, 5), NAV.ACRO: (5, 0), NAV.DESCEND: (4, 20), NAV.TERMINATION: (10, 0),
    NAV.OFFBOARD: (6, 0), NAV.STAB: (7, 0), NAV.AUTO_TAKEOFF: (4, 2), NAV.AUTO_LAND: (4, 6),
    NAV.AUTO_FOLLOW_TARGET: (4, 8), NAV.AUTO_PRECLAND: (4, 9), NAV.ORBIT: (3, 1),
    NAV.AUTO_VTOL_TAKEOFF: (4, 10), NAV.GUIDED_COURSE: (4, 19),
    **{NAV(23 + k): (4, 11 + k) for k in range(8)},
}
CM_TO_NAV = {v: k for k, v in NAV_TO_CM.items()}
def custom_mode(nav: int) -> int:
    m, s = NAV_TO_CM[NAV(nav)]; return (m << 16) | (s << 24)
def nav_from_custom_mode(cm: int) -> int | None:
    return CM_TO_NAV.get(((cm >> 16) & 0xFF, (cm >> 24) & 0xFF))

MANUAL_NAV = {0, 1, 2, 6, 8, 9, 10, 15}
LAND_NAV = {12, 13, 18, 20}
# MAV_STATE（HEARTBEAT.system_status）
MS_UNINIT, MS_STANDBY, MS_ACTIVE, MS_CRITICAL, MS_TERMINATION = 0, 3, 4, 5, 8
# MAV_LANDED_STATE
L_UNDEF, L_GROUND, L_AIR, L_TAKEOFF, L_LANDING = 0, 1, 2, 3, 4


@dataclass
class Intent:                      # 入口侧（Gateway 或适配器）对每架机的认知
    cmd: str | None = None         # 最近一条准入的导航命令：goto|follow_path|orbit|velocity|swarm|hover|rtl|takeoff|land
    arrived: bool = False
    lock: bool = False             # SafetyStop / ABSOLUTE 锁
    supervisor: tuple[FS, int] | None = None   # 活动的监督覆盖，例如 (CORRECTING, GEOFENCE)
    airborne_since_arm: bool = False
    killed: bool = False
    rtl_phase: int = 7             # 未推导时为 OPAQUE
    hold_reason: int | None = None
    owner: Owner = Owner.NONE


@dataclass
class Px4Raw:
    armed: bool
    custom_mode: int
    system_status: int
    landed: int
    hb_age: float = 0.2
    maybe_landed: bool = False


def derive_px4(r: Px4Raw, g: Intent) -> tuple[FS, int, bool]:
    """返回 (fs, sub, failsafe)：HEARTBEAT + EXTENDED_SYS_STATE + 入口意图的纯函数。"""
    nav = nav_from_custom_mode(r.custom_mode)
    failsafe = r.system_status == MS_CRITICAL
    if r.system_status == MS_TERMINATION:
        return (FS.FAILSAFE, sub(FS.FAILSAFE, "TERMINATION"), True) if r.landed in (L_AIR, L_TAKEOFF, L_LANDING) \
            else (FS.DISARMED, sub(FS.DISARMED, "KILLED"), True)
    if not r.armed:
        if g.killed: return FS.DISARMED, sub(FS.DISARMED, "KILLED"), False
        return FS.DISARMED, sub(FS.DISARMED, "READY_TO_ARM" if r.system_status == MS_STANDBY else "NOT_READY"), False
    on_ground = r.landed == L_GROUND
    if on_ground:
        if nav == NAV.AUTO_TAKEOFF: return FS.TAKING_OFF, sub(FS.TAKING_OFF, "SPOOLUP"), failsafe
        if g.airborne_since_arm: return FS.LANDED, 0, failsafe
        return FS.READY, 0, failsafe
    if nav is None: return FS.FLYING, sub(FS.FLYING, "EXTERNAL"), failsafe
    if nav == NAV.TERMINATION: return FS.FAILSAFE, sub(FS.FAILSAFE, "TERMINATION"), True
    if nav == NAV.DESCEND: return FS.ELAND, sub(FS.ELAND, "NO_POSITION"), True
    if nav in (NAV.AUTO_LAND, NAV.AUTO_PRECLAND) or r.landed == L_LANDING:
        return FS.LANDING, sub(FS.LANDING, "TOUCHDOWN" if r.maybe_landed else "DESCEND"), failsafe
    if nav in (NAV.AUTO_TAKEOFF, NAV.AUTO_VTOL_TAKEOFF) or r.landed == L_TAKEOFF:
        return FS.TAKING_OFF, sub(FS.TAKING_OFF, "CLIMB"), failsafe
    if nav == NAV.AUTO_RTL: return FS.RTL, g.rtl_phase, failsafe
    if nav == NAV.AUTO_LOITER:
        if failsafe: return FS.HOLD, g.hold_reason if g.hold_reason is not None else sub(FS.HOLD, "AUTOPILOT"), True
        if g.supervisor: return g.supervisor[0], g.supervisor[1], True
        if g.lock: return FS.HOLD, sub(FS.HOLD, "SAFETY_STOP"), False
        if g.cmd == "land" and not g.arrived: return FS.LANDING, sub(FS.LANDING, "GOTO"), False
        return FS.FLYING, sub(FS.FLYING, "GOTO" if g.cmd == "goto" and not g.arrived else "HOVER"), False
    if nav == NAV.AUTO_MISSION: return FS.FLYING, sub(FS.FLYING, "PATH"), failsafe
    if nav == NAV.ORBIT: return FS.FLYING, sub(FS.FLYING, "ORBIT"), failsafe
    if nav == NAV.OFFBOARD:
        m = {"follow_path": "PATH", "velocity": "VELOCITY", "swarm": "SWARM", "orbit": "ORBIT", "goto": "GOTO"}
        return FS.FLYING, sub(FS.FLYING, m.get(g.cmd or "", "EXTERNAL")), failsafe
    if nav in MANUAL_NAV: return FS.FLYING, sub(FS.FLYING, "MANUAL"), failsafe
    if nav == NAV.GUIDED_COURSE: return FS.FLYING, sub(FS.FLYING, "GOTO"), failsafe
    return FS.FLYING, sub(FS.FLYING, "EXTERNAL"), failsafe


def native_px4(armed: bool, nav: int | None) -> Native:
    if not armed or nav is None: return Native.INIT
    if nav in MANUAL_NAV: return Native.MANUAL
    if nav in LAND_NAV: return Native.LAND
    return Native.COMMAND


# ---------------------------------------------------------------- Prometheus（ROS UAVControlState 语义，不是 Struct.hpp）
P_INIT, P_RC_POS, P_COMMAND, P_LAND = 0, 1, 2, 3
A_INIT_HOVER, A_CUR_HOVER, A_LAND, A_MOVE, A_USER = 1, 2, 3, 4, 5
MOVE_KIND = {0: "GOTO", 1: "VELOCITY", 2: "VELOCITY", 3: "GOTO", 4: "VELOCITY", 5: "VELOCITY",
             6: "PATH", 7: "EXTERNAL", 8: "GOTO"}


@dataclass
class PromRaw:
    connected: bool
    armed: bool
    control_state: int
    failsafe: bool
    odom_valid: bool
    in_air: bool
    agent_cmd: int = A_INIT_HOVER
    move_mode: int = 0
    last_error: str = ""
    z_rel: float = 0.0
    takeoff_h: float = 1.5


def derive_prometheus(r: PromRaw, g: Intent) -> tuple[FS, int, bool, Native]:
    if not r.connected: return FS.UNKNOWN, sub(FS.UNKNOWN, "LINK_LOST"), False, Native.INIT
    cs = r.control_state
    if cs not in (0, 1, 2, 3): cs = P_INIT
    native = Native(cs)
    if not r.armed:
        if g.killed: return FS.DISARMED, sub(FS.DISARMED, "KILLED"), False, Native.INIT
        return FS.DISARMED, sub(FS.DISARMED, "READY_TO_ARM" if r.odom_valid else "NOT_READY"), False, Native.INIT
    if cs == P_LAND:
        if not r.in_air: return FS.LANDED, 0, r.failsafe, native
        if r.failsafe and "Odom invalid" in r.last_error: return FS.ELAND, sub(FS.ELAND, "NO_POSITION"), True, native
        return FS.LANDING, sub(FS.LANDING, "DESCEND"), r.failsafe, native
    if not r.in_air:
        if g.airborne_since_arm: return FS.LANDED, 0, r.failsafe, native
        if cs == P_COMMAND and g.cmd == "takeoff": return FS.TAKING_OFF, sub(FS.TAKING_OFF, "SPOOLUP"), False, native
        return FS.READY, 0, r.failsafe, native
    if cs in (P_INIT, P_RC_POS): return FS.FLYING, sub(FS.FLYING, "MANUAL"), r.failsafe, native
    if g.supervisor: return g.supervisor[0], g.supervisor[1], True, native
    if g.lock: return FS.HOLD, sub(FS.HOLD, "SAFETY_STOP"), False, native
    if g.cmd == "rtl": return FS.RTL, g.rtl_phase, False, native
    if g.cmd == "takeoff" and r.z_rel < r.takeoff_h - 0.2: return FS.TAKING_OFF, sub(FS.TAKING_OFF, "CLIMB"), False, native
    if r.agent_cmd in (A_INIT_HOVER, A_CUR_HOVER): return FS.FLYING, sub(FS.FLYING, "HOVER"), False, native
    if r.agent_cmd == A_MOVE:
        k = MOVE_KIND.get(r.move_mode, "EXTERNAL")
        if g.cmd in ("follow_path", "orbit", "swarm") and k in ("GOTO", "PATH"):
            k = {"follow_path": "PATH", "orbit": "ORBIT", "swarm": "SWARM"}[g.cmd]
        if k == "GOTO" and g.arrived: k = "HOVER"
        return FS.FLYING, sub(FS.FLYING, k), False, native
    return FS.FLYING, sub(FS.FLYING, "EXTERNAL"), False, native


# ---------------------------------------------------------------- Mock：规范态 -> PX4 显示仿真（有损、单向，FR-052）
def mock_emulate_px4(fs: FS, s: int, g: Intent) -> Px4Raw:
    armed = fs not in (FS.UNKNOWN, FS.DISARMED, FS.PREFLIGHT, FS.CRASHED)
    auto_fs = fs in (FS.ELAND, FS.FAILSAFE) or (fs == FS.HOLD and s != sub(FS.HOLD, "SAFETY_STOP"))
    status = MS_TERMINATION if (fs == FS.FAILSAFE and s == 1) else MS_CRITICAL if (armed and auto_fs) \
        else MS_ACTIVE if armed else MS_STANDBY if (fs == FS.DISARMED and s == 0) else MS_UNINIT
    nav = {FS.TAKING_OFF: NAV.AUTO_TAKEOFF, FS.RTL: NAV.AUTO_RTL, FS.LANDING: NAV.AUTO_LAND,
           FS.ELAND: NAV.AUTO_LAND, FS.FAILSAFE: NAV.DESCEND if s == 0 else NAV.TERMINATION,
           FS.LANDED: NAV.AUTO_LAND}.get(fs, NAV.AUTO_LOITER)
    if fs == FS.LANDING and s == 1: nav = NAV.AUTO_LOITER
    if fs == FS.FLYING:
        nav = [NAV.AUTO_LOITER, NAV.AUTO_LOITER, NAV.AUTO_MISSION if g.cmd == "mission" else NAV.OFFBOARD,
               NAV.ORBIT, NAV.OFFBOARD, NAV.OFFBOARD, NAV.POSCTL, NAV.EXTERNAL1][s]
    landed = L_GROUND if fs in (FS.DISARMED, FS.PREFLIGHT, FS.READY, FS.LANDED) else \
        L_TAKEOFF if fs == FS.TAKING_OFF and s == 1 else L_GROUND if fs == FS.TAKING_OFF else \
        L_AIR if (fs == FS.LANDING and s == 1) else L_LANDING if fs in (FS.LANDING, FS.ELAND, FS.FAILSAFE) else L_UNDEF if fs in (FS.UNKNOWN, FS.CRASHED) else L_AIR
    return Px4Raw(armed=armed, custom_mode=custom_mode(nav), system_status=status, landed=landed,
                  maybe_landed=(fs == FS.LANDING and s == 2))


def mock_native(fs: int) -> Native:
    """Mock 的 ctrl.native（M08 §6.10.3；g04 §4.4）：DISARMED、PREFLIGHT、UNKNOWN 为 INIT；降落类为 LAND；其余 COMMAND。"""
    if fs in (FS.DISARMED, FS.PREFLIGHT, FS.UNKNOWN): return Native.INIT
    if fs in (FS.LANDING, FS.ELAND, FS.FAILSAFE, FS.LANDED, FS.CRASHED): return Native.LAND
    return Native.COMMAND


# ---------------------------------------------------------------- 生命周期（r22）与定位（r08）覆盖层
LIFECYCLE_UNKNOWN_SUB = {"PENDING": "BOOTING", "PROVISIONING": "BOOTING", "STARTING": "BOOTING",
                         "LOST": "LINK_LOST", "RESTARTING": "RESTARTING", "FAILED": "FAILED"}
def apply_lifecycle(lc: str, fs: FS, s: int, flags: int) -> tuple[FS, int, int]:
    if lc in LIFECYCLE_UNKNOWN_SUB:
        return FS.UNKNOWN, sub(FS.UNKNOWN, LIFECYCLE_UNKNOWN_SUB[lc]), (flags & ~F_FCU) | F_ALERT
    if lc == "DEGRADED": return fs, s, (flags & ~F_FCU) | F_ALERT
    return fs, s, flags | F_FCU

LOC_BITS = {"TRACKING": F_LOC_OK, "DEGRADED": F_LOC_OK | F_LOC_DEG, "OUT_OF_MAP": F_LOC_OK | F_LOC_DEG,
            "ALIGNING": F_LOC_DEG, "WAIT_INIT": F_LOC_DEG, "LOST": 0, "NO_MAP": 0}
GNSS_BITS = {6: F_LOC_OK, 3: F_LOC_OK, 5: F_LOC_OK | F_LOC_DEG, 4: F_LOC_OK | F_LOC_DEG, 2: F_LOC_DEG, 1: 0, 0: 0}


# ---------------------------------------------------------------- 准入（命令 × FlightState，AWR-12 §5.2）
SAFETY_CLASS = {"land", "rtl", "hover", "safety_stop", "kill"}          # 免租约（operator 角色），对 agent 另有限制
NAV_CMDS = {"goto", "follow_path", "orbit", "velocity"}
def admit(cmd: str, fs: FS, s: int, flags: int, has_task: bool = False, agl: float = 10.0) -> str | None:
    """规范态上的准入（租约、围栏、参数在别处检查）；返回 None 表示通过，否则为原因名。"""
    auto = bool(flags & F_FAILSAFE)
    op_air = fs == FS.FLYING or (fs in (FS.RTL, FS.LANDING) and not auto)
    if cmd == "arm": return None if fs == FS.DISARMED and s == 0 else "STATE"
    if cmd == "disarm": return None if fs in (FS.READY, FS.LANDED, FS.PREFLIGHT) else "STATE"
    if cmd == "kill": return None if flags & F_ARMED else "STATE"
    if cmd == "takeoff":
        if fs == FS.TAKING_OFF: return "DUPLICATE"
        return None if fs in (FS.READY, FS.LANDED) or (fs == FS.DISARMED and s == 0) else "STATE"
    if cmd == "safety_stop":
        if fs == FS.HOLD and s == 0: return "DUPLICATE"
        if fs in (FS.ELAND, FS.FAILSAFE): return "SAFETY_ACTIVE"
        if fs == FS.LANDING and agl < 2.0: return "STATE"
        return None if fs in (FS.TAKING_OFF, FS.FLYING, FS.CORRECTING, FS.RTL, FS.LANDING, FS.HOLD) else "STATE"
    if cmd == "land":
        if fs in (FS.ELAND, FS.FAILSAFE): return "SAFETY_ACTIVE"
        if fs in (FS.LANDING, FS.LANDED): return "DUPLICATE"
        return None if fs in (FS.TAKING_OFF, FS.FLYING, FS.CORRECTING, FS.HOLD, FS.RTL) else "STATE"
    if cmd == "rtl":
        if fs == FS.RTL: return "DUPLICATE"
        if fs in (FS.ELAND, FS.FAILSAFE) or (fs == FS.LANDING and auto): return "SAFETY_ACTIVE"
        return None if fs in (FS.FLYING, FS.CORRECTING, FS.HOLD) or (fs == FS.LANDING and not auto) else "STATE"
    if cmd == "resume":
        if fs == FS.HOLD: return None
        if fs == FS.RTL: return None if auto else "STATE"
        return None if fs == FS.FLYING and has_task else "STATE"
    if cmd == "pause": return None if fs == FS.FLYING and s in (1, 2, 3, 5) and has_task else "STATE"
    if cmd == "hover":
        if fs in (FS.CORRECTING, FS.HOLD, FS.ELAND, FS.FAILSAFE) or (fs in (FS.RTL, FS.LANDING) and auto): return "SAFETY_ACTIVE"
        return None if fs == FS.TAKING_OFF or op_air else "STATE"
    if cmd in NAV_CMDS:
        if fs in (FS.CORRECTING, FS.HOLD, FS.ELAND, FS.FAILSAFE) or (fs in (FS.RTL, FS.LANDING) and auto): return "SAFETY_ACTIVE"
        return None if op_air else "STATE"
    return "UNSUPPORTED"


def admission_matrix() -> dict[str, list[str]]:
    """准入矩阵（16 列，RTL 与 LANDING 按 flags.FAILSAFE 拆 op/auto）；生成 `commands.json` 的准入矩阵（M00 合入）。"""
    cols = [(FS.UNKNOWN, 0, 0), (FS.DISARMED, 0, 0), (FS.PREFLIGHT, 0, 0), (FS.READY, 0, F_ARMED),
            (FS.TAKING_OFF, 1, F_ARMED | F_IN_AIR), (FS.FLYING, 1, F_ARMED | F_IN_AIR),
            (FS.CORRECTING, 0, F_ARMED | F_IN_AIR | F_FAILSAFE), (FS.HOLD, 0, F_ARMED | F_IN_AIR),
            (FS.RTL, 1, F_ARMED | F_IN_AIR), (FS.RTL, 1, F_ARMED | F_IN_AIR | F_FAILSAFE),
            (FS.LANDING, 0, F_ARMED | F_IN_AIR), (FS.LANDING, 0, F_ARMED | F_IN_AIR | F_FAILSAFE),
            (FS.ELAND, 0, F_ARMED | F_IN_AIR | F_FAILSAFE), (FS.FAILSAFE, 0, F_ARMED | F_IN_AIR | F_FAILSAFE),
            (FS.LANDED, 0, F_ARMED), (FS.CRASHED, 0, 0)]
    sym = {None: "Y", "STATE": "-", "SAFETY_ACTIVE": "S", "DUPLICATE": "=", "UNSUPPORTED": "?"}
    out = {}
    for c in ["takeoff", "land", "goto", "follow_path", "orbit", "hover", "rtl", "velocity", "safety_stop",
              "pause", "resume", "arm", "disarm", "kill"]:
        out[c] = [sym[admit(c, fs, s, fl, has_task=True)] for fs, s, fl in cols]
    return out
