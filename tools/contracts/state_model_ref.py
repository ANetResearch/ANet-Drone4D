"""Reference state model (port of .cache/research/g04/state_model.py onto the generated contract enums).

Oracle for tests/contracts/test_state_model.py (the seven g04 assertion groups): PX4 and Prometheus derivation,
Mock emulation, lifecycle and localisation overlays, and the admission function whose matrix must equal
packages/contracts/rt/commands.json. M08 owns the production implementation (python/awr/sim/core/state_model.py);
the contract test checks it against the same assertions when it is importable. Semicolon one-liners of the
prototype are kept to ease line-by-line comparison.
"""
# ruff: noqa: E701, E702

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from awr.contracts.enums import FLIGHT_SUB, FlightFlags, FlightState, Native, Owner, PoseSrc
from awr.contracts.layouts import pack_ctrl, pack_fs, unpack_ctrl, unpack_fs

FS = FlightState
SUB: dict[FlightState, list[str | None]] = {FS(k): list(v) for k, v in FLIGHT_SUB.items()}


def sub(fs: FlightState, name: str) -> int:
    return SUB[fs].index(name)


F_ARMED, F_IN_AIR, F_LOC_OK, F_FAILSAFE = int(FlightFlags.ARMED), int(FlightFlags.IN_AIR), int(FlightFlags.LOC_OK), int(FlightFlags.FAILSAFE)
F_GCS, F_FCU, F_LOC_DEG, F_ALERT = int(FlightFlags.GCS_LINK), int(FlightFlags.FCU_LINK), int(FlightFlags.LOC_DEGRADED), int(FlightFlags.ALERT)

__all__ = ["FS", "SUB", "Intent", "Native", "Owner", "PoseSrc", "PromRaw", "Px4Raw", "admission_matrix", "admit", "apply_lifecycle",
           "custom_mode", "derive_prometheus", "derive_px4", "mock_emulate_px4", "nav_from_custom_mode", "pack_ctrl", "pack_fs", "sub",
           "unpack_ctrl", "unpack_fs"]


# ---------------------------------------------------------------- PX4 (msg/versioned/VehicleStatus.msg v4, px4_custom_mode.h)
class NAV(IntEnum):
    MANUAL = 0; ALTCTL = 1; POSCTL = 2; AUTO_MISSION = 3; AUTO_LOITER = 4; AUTO_RTL = 5; POSITION_SLOW = 6
    GUIDED_COURSE = 7; ALTITUDE_CRUISE = 8; MANUAL_PARKING = 9; ACRO = 10; FREE2 = 11; DESCEND = 12
    TERMINATION = 13; OFFBOARD = 14; STAB = 15; FREE1 = 16; AUTO_TAKEOFF = 17; AUTO_LAND = 18
    AUTO_FOLLOW_TARGET = 19; AUTO_PRECLAND = 20; ORBIT = 21; AUTO_VTOL_TAKEOFF = 22
    EXTERNAL1 = 23; EXTERNAL2 = 24; EXTERNAL3 = 25; EXTERNAL4 = 26; EXTERNAL5 = 27; EXTERNAL6 = 28
    EXTERNAL7 = 29; EXTERNAL8 = 30

# get_px4_custom_mode(): nav_state -> (main, sub)
_MAIN = dict(MANUAL=1, ALTCTL=2, POSCTL=3, AUTO=4, ACRO=5, OFFBOARD=6, STAB=7, TERMINATION=10,
             ALTITUDE_CRUISE=11, MANUAL_PARKING=12)
_AUTO = dict(READY=1, TAKEOFF=2, LOITER=3, MISSION=4, RTL=5, LAND=6, FOLLOW_TARGET=8, PRECLAND=9,
             VTOL_TAKEOFF=10, EXTERNAL1=11, GUIDED_COURSE=19, DESCEND=20)
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
# MAV_STATE (HEARTBEAT.system_status, HEARTBEAT.hpp L107-125)
MS_UNINIT, MS_STANDBY, MS_ACTIVE, MS_CRITICAL, MS_TERMINATION = 0, 3, 4, 5, 8
# MAV_LANDED_STATE
L_UNDEF, L_GROUND, L_AIR, L_TAKEOFF, L_LANDING = 0, 1, 2, 3, 4


@dataclass
class Intent:                      # Gateway-side knowledge, per vehicle
    cmd: str | None = None         # last accepted nav command: goto|follow_path|orbit|velocity|swarm|hover|rtl|takeoff|land
    arrived: bool = False
    lock: bool = False             # SafetyStop / ABSOLUTE lock held by Gateway
    supervisor: tuple[FS, int] | None = None   # active supervisor overlay, e.g. (CORRECTING, GEOFENCE)
    airborne_since_arm: bool = False
    killed: bool = False
    rtl_phase: int = 7             # OPAQUE unless derived
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
    """returns (fs, sub, failsafe). Pure function of HEARTBEAT + EXTENDED_SYS_STATE + gateway intent."""
    nav = nav_from_custom_mode(r.custom_mode)
    failsafe = r.system_status == MS_CRITICAL
    if r.system_status == MS_TERMINATION:                  # kill / termination / lockdown (HEARTBEAT.hpp L121-125)
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
        if g.supervisor: return g.supervisor[0], g.supervisor[1], True     # our supervisor acts via normal commands
        if g.lock: return FS.HOLD, sub(FS.HOLD, "SAFETY_STOP"), False
        if g.cmd == "land" and not g.arrived: return FS.LANDING, sub(FS.LANDING, "GOTO"), False   # land-there leg
        return FS.FLYING, sub(FS.FLYING, "GOTO" if g.cmd == "goto" and not g.arrived else "HOVER"), False
    if nav == NAV.AUTO_MISSION: return FS.FLYING, sub(FS.FLYING, "PATH"), failsafe
    if nav == NAV.ORBIT: return FS.FLYING, sub(FS.FLYING, "ORBIT"), failsafe
    if nav == NAV.OFFBOARD:
        m = {"follow_path": "PATH", "velocity": "VELOCITY", "swarm": "SWARM", "orbit": "ORBIT", "goto": "GOTO"}
        return FS.FLYING, sub(FS.FLYING, m.get(g.cmd or "", "EXTERNAL")), failsafe
    if nav in MANUAL_NAV: return FS.FLYING, sub(FS.FLYING, "MANUAL"), failsafe
    if nav == NAV.GUIDED_COURSE: return FS.FLYING, sub(FS.FLYING, "GOTO"), failsafe
    return FS.FLYING, sub(FS.FLYING, "EXTERNAL"), failsafe          # FOLLOW_TARGET, EXTERNALn, FREE*


def native_px4(armed: bool, nav: int | None) -> Native:
    if not armed or nav is None: return Native.INIT
    if nav in MANUAL_NAV: return Native.MANUAL
    if nav in LAND_NAV: return Native.LAND
    return Native.COMMAND


# ---------------------------------------------------------------- Prometheus (ROS UAVControlState semantics, NOT Struct.hpp)
P_INIT, P_RC_POS, P_COMMAND, P_LAND = 0, 1, 2, 3
A_INIT_HOVER, A_CUR_HOVER, A_LAND, A_MOVE, A_USER = 1, 2, 3, 4, 5
MOVE_KIND = {0: "GOTO", 1: "VELOCITY", 2: "VELOCITY", 3: "GOTO", 4: "VELOCITY", 5: "VELOCITY",
             6: "PATH", 7: "EXTERNAL", 8: "GOTO"}


@dataclass
class PromRaw:
    connected: bool          # UAVState.connected (companion <-> PX4)
    armed: bool
    control_state: int       # wire value, decoded with ROS enum 0..3
    failsafe: bool
    odom_valid: bool
    in_air: bool             # derived by Gateway with hysteresis (no landed_state on GS protocol)
    agent_cmd: int = A_INIT_HOVER
    move_mode: int = 0
    last_error: str = ""     # latest TextInfo ERROR message
    z_rel: float = 0.0
    takeoff_h: float = 1.5


def derive_prometheus(r: PromRaw, g: Intent) -> tuple[FS, int, bool, Native]:
    if not r.connected: return FS.UNKNOWN, sub(FS.UNKNOWN, "LINK_LOST"), False, Native.INIT
    cs = r.control_state
    if cs not in (0, 1, 2, 3): cs = P_INIT                                   # defensive: Struct.hpp 4 never valid
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
    # COMMAND_CONTROL, in air
    if g.supervisor: return g.supervisor[0], g.supervisor[1], True, native
    if g.lock: return FS.HOLD, sub(FS.HOLD, "SAFETY_STOP"), False, native
    if g.cmd == "rtl": return FS.RTL, g.rtl_phase, False, native            # gateway-emulated RTL
    if g.cmd == "takeoff" and r.z_rel < r.takeoff_h - 0.2: return FS.TAKING_OFF, sub(FS.TAKING_OFF, "CLIMB"), False, native
    if r.agent_cmd in (A_INIT_HOVER, A_CUR_HOVER): return FS.FLYING, sub(FS.FLYING, "HOVER"), False, native
    if r.agent_cmd == A_MOVE:
        k = MOVE_KIND.get(r.move_mode, "EXTERNAL")
        if g.cmd in ("follow_path", "orbit", "swarm") and k in ("GOTO", "PATH"):
            k = {"follow_path": "PATH", "orbit": "ORBIT", "swarm": "SWARM"}[g.cmd]
        if k == "GOTO" and g.arrived: k = "HOVER"
        return FS.FLYING, sub(FS.FLYING, k), False, native
    return FS.FLYING, sub(FS.FLYING, "EXTERNAL"), False, native


# ---------------------------------------------------------------- Mock: canonical FS -> PX4 display emulation (lossy, one-way)
def mock_emulate_px4(fs: FS, s: int, g: Intent) -> Px4Raw:
    armed = fs not in (FS.UNKNOWN, FS.DISARMED, FS.PREFLIGHT, FS.CRASHED)
    # CRITICAL only for autopilot-internal failsafes; CORRECTING is executed through normal reposition commands
    auto_fs = fs in (FS.ELAND, FS.FAILSAFE) or (fs == FS.HOLD and s != sub(FS.HOLD, "SAFETY_STOP"))
    status = MS_TERMINATION if (fs == FS.FAILSAFE and s == 1) else MS_CRITICAL if (armed and auto_fs) \
        else MS_ACTIVE if armed else MS_STANDBY if (fs == FS.DISARMED and s == 0) else MS_UNINIT
    nav = {FS.TAKING_OFF: NAV.AUTO_TAKEOFF, FS.RTL: NAV.AUTO_RTL, FS.LANDING: NAV.AUTO_LAND,
           FS.ELAND: NAV.AUTO_LAND, FS.FAILSAFE: NAV.DESCEND if s == 0 else NAV.TERMINATION,
           FS.LANDED: NAV.AUTO_LAND}.get(fs, NAV.AUTO_LOITER)
    if fs == FS.LANDING and s == 1: nav = NAV.AUTO_LOITER                  # land-there GOTO leg
    if fs == FS.FLYING:
        nav = [NAV.AUTO_LOITER, NAV.AUTO_LOITER, NAV.AUTO_MISSION if g.cmd == "mission" else NAV.OFFBOARD,
               NAV.ORBIT, NAV.OFFBOARD, NAV.OFFBOARD, NAV.POSCTL, NAV.EXTERNAL1][s]
    landed = L_GROUND if fs in (FS.DISARMED, FS.PREFLIGHT, FS.READY, FS.LANDED) else \
        L_TAKEOFF if fs == FS.TAKING_OFF and s == 1 else L_GROUND if fs == FS.TAKING_OFF else \
        L_AIR if (fs == FS.LANDING and s == 1) else L_LANDING if fs in (FS.LANDING, FS.ELAND, FS.FAILSAFE) else L_UNDEF if fs in (FS.UNKNOWN, FS.CRASHED) else L_AIR
    return Px4Raw(armed=armed, custom_mode=custom_mode(nav), system_status=status, landed=landed,
                  maybe_landed=(fs == FS.LANDING and s == 2))


# ---------------------------------------------------------------- lifecycle (r22) and localization (r08) overlays
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


# ---------------------------------------------------------------- admission (command x FlightState)
SAFETY_CLASS = {"land", "rtl", "hover", "safety_stop", "kill"}          # bypass lease (operator role), never agents
NAV_CMDS = {"goto", "follow_path", "orbit", "velocity"}
def admit(cmd: str, fs: FS, s: int, flags: int, has_task: bool = False, agl: float = 10.0) -> str | None:
    """Gateway admission on the canonical state only (lease/geofence/params are checked elsewhere)."""
    auto = bool(flags & F_FAILSAFE)                      # an automatic protective action owns the vehicle
    op_air = fs == FS.FLYING or (fs in (FS.RTL, FS.LANDING) and not auto)   # operator RTL/LAND may be overridden
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
        if fs == FS.HOLD: return None                   # cause-cleared check is done by the supervisor
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
