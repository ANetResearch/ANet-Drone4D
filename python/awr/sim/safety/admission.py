"""准入第④步（M09 部分）、预检、resume"原因已解除"判定与 apply 时复核（M09 §6.6.1、§6.6.2、§6.6.4；FR-030 至 FR-036、
FR-070、FR-071）。

M08 CommandEngine 在第④步已执行：生命周期 108、时钟 117、准入矩阵（`commands.json`，101/105/106，加锁时 101 → 114）、104、
113；本模块登记的 `safety.state` 检查在其后对仍通过的机体执行：
- 115：principal 为 agent 时对 safety_stop、kill、escalate 的防御性拒绝（入口第①步已拒，C35）；
- 114：SafetyStop 加锁期间的非安全类命令（M08 只把矩阵结论为 101 的转为 114）；
- 101：resume 的原因未解除（LINK_NOT_RESTORED、SEPARATION_NOT_RESTORED、LOC_NOT_RESTORED、BATTERY）；
- 103：arm 与带 `auto_arm` 的 takeoff 预检（生命周期、LOC_OK、soc ≥ 0.30、border 内且不在 nofly 内、速度 < 0.3 m/s、
  出生点净空 `|z − dsm| ≤ 0.5 m`），detail 列出全部失败项。
每个拒绝附 detail 与中文 remedy（FR-034）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from awr.contracts.enums import Lifecycle
from awr.contracts.reasons import Reason
from awr.sim.core.admission import AdmitResult

from . import codes as C
from .flight_fsm import S_DIS_READY, S_HOLD_ESC, S_HOLD_LINK, S_HOLD_LOC, S_HOLD_SEP, S_HOLD_STOP
from .state import FS

if TYPE_CHECKING:
    from .service import SafetyRuntime

__all__ = ["SAFETY_CMDS", "SafetyAdmission"]

SAFETY_CMDS = frozenset({"land", "hover", "rtl", "safety_stop", "kill", "escalate", "resume", "cancel"})
AGENT_FORBIDDEN = {"safety_stop": "AGENT_SAFETY_STOP", "kill": "AGENT_KILL", "escalate": "AGENT_ESCALATE"}
_BAT = frozenset(C.idx(c) for c in ("SAF.BAT.CRIT", "SAF.BAT.ENERGY_RTL", "SAF.BAT.EMERG"))
_LINK = frozenset(C.idx(c) for c in ("SAF.LINK.LOST_RTL", "SAF.LINK.LOST_HOLD", "SAF.LINK.AGENT_LOST", "SAF.LINK.WATCHDOG"))
_GEO = frozenset(C.idx(c) for c in ("SAF.GEOFENCE.CORRECT_TIMEOUT", "SAF.GEOFENCE.FAR_OUT"))
REMEDY = {
    "LINK_NOT_RESTORED": "链路尚未恢复，等待链路恢复后再恢复任务",
    "SEPARATION_NOT_RESTORED": "与邻机间距尚未恢复，等待间距恢复",
    "LOC_NOT_RESTORED": "定位尚未恢复，等待定位就绪",
    "BATTERY": "电量原因的自动返航不可恢复，请等待降落后更换电池",
    "GEOFENCE": "仍在围栏外或禁飞区内，等待回到合法区域",
    "LOCKED": "该机已安全停止，请先恢复",
    "PREFLIGHT": "预检未通过：{items}",
    "ROLE": "智能体无权执行该安全命令",
}


class SafetyAdmission:
    def __init__(self, rt: SafetyRuntime) -> None:
        self.rt = rt
        self.last_escalate_wall = np.zeros(rt.S.capacity, np.int64)

    # ---------------------------------------------------------------- escalate（ext，12 F30–F32）
    def escalate_target(self, s: int) -> tuple[int, int, str] | None:
        """(目标状态, 子模式, 动作)；TKO/FLY/COR → HOLD/ESCALATE；HOLD/RTL/LANDING → ELAND；ELAND → FAILSAFE（只在
        `escalation_levels` 含 FAILSAFE 时）。"""
        fs = int(self.rt.sb["fs"][s])
        if fs in (FS.TAKING_OFF, FS.FLYING, FS.CORRECTING):
            return int(FS.HOLD), S_HOLD_ESC, "hold"
        if fs in (FS.HOLD, FS.RTL, FS.LANDING):
            return int(FS.ELAND), 0, "eland"
        if fs == FS.ELAND and "FAILSAFE" in self.rt.params.escalation_levels:
            return int(FS.FAILSAFE), 0, "failsafe"
        return None

    def _admit_escalate(self, s: int) -> AdmitResult | None:
        rt = self.rt
        if self.escalate_target(s) is None:
            return AdmitResult(int(Reason.STATE), {"why": "ESCALATION_LEVEL", "remedy": "已到最高允许的升级级别"})
        last = int(self.last_escalate_wall[s])
        if last and rt.wall_ns() - last < int(rt.params.escalation_min_interval_s * 1e9):
            return AdmitResult(int(Reason.STATE), {"why": "ESCALATE_INTERVAL", "remedy": "两次升级之间至少间隔 2 s"})
        return None

    # ---------------------------------------------------------------- 第④步（登记的检查）
    def admit_state(self, req: Any, ctx: Any) -> AdmitResult | None:
        rt = self.rt
        rt.bind_admit(ctx)
        if rt.S is None:
            return None
        op = req.op
        s = int(req.slot)
        p = req.principal if isinstance(req.principal, dict) else {}
        if p.get("role") == "agent" and op in AGENT_FORBIDDEN:
            return AdmitResult(int(Reason.ROLE_FORBIDDEN), {"why": AGENT_FORBIDDEN[op], "remedy": REMEDY["ROLE"]})
        sb = rt.sb
        if not sb["inited"][s]:
            return None
        if sb["locked"][s] and op not in SAFETY_CMDS:
            return AdmitResult(int(Reason.LOCKED), {"why": "SAFETY_STOP", "remedy": REMEDY["LOCKED"]})
        if op == "resume":
            ok, blocked = self.resume_status(s)
            if not ok:
                code = int(Reason.CONFIRM_REQUIRED) if blocked == "CONFIRM" else int(Reason.SAFETY_ACTIVE)
                return AdmitResult(code, {"why": blocked, "remedy": REMEDY.get(blocked, "")})
        if op == "escalate":
            return self._admit_escalate(s)
        fs, sub = int(sb["fs"][s]), int(sb["sub"][s])
        need_pre = op == "arm" or (op == "takeoff" and req.args.get("auto_arm", True) and fs == FS.DISARMED)
        if need_pre and fs == FS.DISARMED and sub == S_DIS_READY:
            items = self.preflight_items(s)
            if items:
                return AdmitResult(int(Reason.PREFLIGHT_FAILED),
                                   {"why": items[0], "items": items, "remedy": REMEDY["PREFLIGHT"].format(items="、".join(items))})
        return None

    # ---------------------------------------------------------------- 预检（§6.6.2）
    def preflight_items(self, s: int) -> list[str]:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        P = rt.params
        out: list[str] = []
        if int(S.lifecycle[s]) != int(Lifecycle.READY):
            out.append("LIFECYCLE")
        if not sb["flag_loc_ok"][s]:
            out.append("LOC_NOT_OK")
        bb = S.blocks["battery"]
        if bb["has_bat"][s] and float(bb["soc"][s]) < P.battery.takeoff_min:
            out.append("SOC_LOW")
        geo = rt.geo
        pos = S.enu.pos[s]
        if geo is not None and geo.valid:
            inb, innf, _ = geo.point_status(pos[None])
            if not inb[0]:
                out.append("OUT_OF_BORDER")
            if innf[0]:
                out.append("IN_NOFLY")
            dsm = float(geo.dsm(pos[None, :2])[0])
            if abs(float(pos[2]) - dsm) > P.fsm.spawn_z_tol_m:
                out.append("BAD_SPAWN_Z")
        if float(np.linalg.norm(S.enu.vel[s])) >= P.fsm.still_mps:
            out.append("NOT_STILL")
        return out

    # ---------------------------------------------------------------- resume 判定（§6.6.4）
    def resume_status(self, s: int) -> tuple[bool, str | None]:
        rt, sb = self.rt, self.rt.sb
        fs, sub = int(sb["fs"][s]), int(sb["sub"][s])
        if fs == FS.HOLD:
            if sub == S_HOLD_STOP:
                return True, None
            if sub == S_HOLD_LINK:
                return (int(sb["link_state"][s]) == 0), (None if int(sb["link_state"][s]) == 0 else "LINK_NOT_RESTORED")
            if sub == S_HOLD_SEP:
                P = rt.params.sep
                ok = float(sb["sep_m"][s]) > P.recover_m and float(sb["cpa_min_m"][s]) > P.min_sep_m
                return ok, None if ok else "SEPARATION_NOT_RESTORED"
            if sub == S_HOLD_LOC:
                ok = bool(sb["flag_loc_ok"][s])
                return ok, None if ok else "LOC_NOT_RESTORED"
            if sub == S_HOLD_ESC:
                return False, "CONFIRM"
            return True, None
        if fs == FS.RTL and sb["fs_auto"][s]:
            r = int(sb["reason"][s])
            if r in _BAT:
                return False, "BATTERY"
            if r in _LINK and int(sb["link_state"][s]) != 0:
                return False, "LINK_NOT_RESTORED"
            if r in _GEO and float(sb["geo_margin_m"][s]) < rt.params.fence.restore_margin_m:
                return False, "GEOFENCE"
            return True, None
        return True, None

    # ---------------------------------------------------------------- apply 时复核（M08-FR-060）
    def matrix_verdict(self, slots: np.ndarray, op: str) -> np.ndarray:
        rt = self.rt
        s = np.asarray(slots, np.int64)
        out = np.zeros(s.size, np.uint16)
        if rt.S is None or s.size == 0:
            return out
        sb = rt.sb
        if op not in SAFETY_CMDS:
            out[sb["locked"][s]] = int(Reason.LOCKED)
        if op == "resume":
            for k, sl in enumerate(s):
                ok, blocked = self.resume_status(int(sl))
                if not ok:
                    out[k] = int(Reason.CONFIRM_REQUIRED) if blocked == "CONFIRM" else int(Reason.SAFETY_ACTIVE)
        return out
