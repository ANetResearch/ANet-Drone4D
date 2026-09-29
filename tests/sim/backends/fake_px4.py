"""PX4 风味 MAVLink 自驾仪替身（测试专用；由 `.cache/research/r21/fake_px4.py` 迁入并改为同步脚本模式，M08 §9.1）。

与 r21 原型相同的点质量动力学（一阶速度跟踪、限速限加速度）与命令语义（COMMAND_LONG：ARM_DISARM、DO_SET_MODE、NAV_TAKEOFF、
NAV_LAND、DO_REPOSITION、DO_ORBIT、NAV_RETURN_TO_LAUNCH），不开 socket：`command()` 直接处理，`stream()` 用 pymavlink
（2.4.50，`development` 方言含 CURRENT_MODE）打包 HEARTBEAT、CURRENT_MODE、EXTENDED_SYS_STATE 的字节，测试侧用
`MAVLink.parse_buffer` 解回，喂给 `derive_px4`（与 V0.2 px4-bridge 的解码路径相同）。

补充 PX4 行为：ORBIT 半径超过 `MC_ORBIT_RAD_MAX`（默认 1000 m）时拒绝（MAV_RESULT_DENIED）但 CURRENT_MODE 的
`intended_custom_mode` 已记为 ORBIT；OFFBOARD 在无 setpoint 流时被拒（留在 HOLD）；RTL 到达 home 后降落，触地后
`COM_DISARM_LAND`（2 s）自动上锁；起飞到 `MIS_TAKEOFF_ALT` 后转 AUTO.LOITER。
"""

from __future__ import annotations

import math

from pymavlink.dialects.v20 import development as mav

from awr.sim.core.state_model import NAV, custom_mode

MODE_LOITER = custom_mode(NAV.AUTO_LOITER)
MODE_TAKEOFF = custom_mode(NAV.AUTO_TAKEOFF)
MODE_LAND = custom_mode(NAV.AUTO_LAND)
MODE_RTL = custom_mode(NAV.AUTO_RTL)
MODE_ORBIT = custom_mode(NAV.ORBIT)
MODE_OFFBOARD = custom_mode(NAV.OFFBOARD)
MODE_POSCTL = custom_mode(NAV.POSCTL)
ORBIT_RAD_MAX = 1000.0
TAKEOFF_ALT = 2.5
RTL_ALT = 30.0
DISARM_LAND_S = 2.0


class FakePx4:
    def __init__(self, sysid: int = 1) -> None:
        self.m = mav.MAVLink(None, srcSystem=sysid, srcComponent=1)
        self.rx = mav.MAVLink(None)
        self.rx.robust_parsing = True
        self.t = 0.0
        self.p = [0.0, 0.0, 0.0]  # NED
        self.v = [0.0, 0.0, 0.0]
        self.yaw = 0.0
        self.armed = False
        self.mode = MODE_POSCTL
        self.intended = MODE_POSCTL
        self.target: tuple[float, float, float] | None = None
        self.orbit: tuple[float, float, float, float] | None = None  # (cn, ce, r, v)
        self.home = list(self.p)
        self.landed_since: float | None = None
        self.rtl_leg = 0

    # ---------------- 命令（COMMAND_LONG 语义）
    def command(self, cmd: int, *params: float) -> int:
        p = list(params) + [0.0] * (7 - len(params))
        A, D = mav.MAV_RESULT_ACCEPTED, mav.MAV_RESULT_DENIED
        if cmd == mav.MAV_CMD_COMPONENT_ARM_DISARM:
            self.armed = p[0] > 0.5
            self.landed_since = None
            return A
        if cmd == mav.MAV_CMD_DO_SET_MODE:
            main, sub = int(p[1]), int(p[2])
            want = (main << 16) | ((sub if main == 4 else 0) << 24)
            self.intended = want
            if want == MODE_OFFBOARD:
                return D  # 没有 setpoint 流：PX4 拒绝进入 OFFBOARD，留在原模式
            self.mode = want
            if want == MODE_LOITER:
                self.target = tuple(self.p)
            return A
        if cmd == mav.MAV_CMD_NAV_TAKEOFF:
            self.intended = MODE_TAKEOFF
            if not self.armed:
                return D
            self.mode = MODE_TAKEOFF
            self.target = (self.p[0], self.p[1], -TAKEOFF_ALT)
            return A
        if cmd == mav.MAV_CMD_NAV_LAND:
            self.mode = self.intended = MODE_LAND
            self.target = (self.p[0], self.p[1], 0.0)
            return A
        if cmd == mav.MAV_CMD_DO_REPOSITION:  # 本替身用 NED 局部坐标放在 param5–7
            self.mode = self.intended = MODE_LOITER
            self.target = (p[4], p[5], p[6])
            return A
        if cmd == mav.MAV_CMD_DO_ORBIT:  # param1 半径、param2 速度、param5/6 圆心（NED 局部）
            self.intended = MODE_ORBIT
            r = abs(p[0])
            if r > ORBIT_RAD_MAX or r < 1.0:
                return D
            self.mode = MODE_ORBIT
            self.orbit = (p[4], p[5], r, p[1] or 2.0)
            return A
        if cmd == mav.MAV_CMD_NAV_RETURN_TO_LAUNCH:
            self.mode = self.intended = MODE_RTL
            self.rtl_leg = 0
            return A
        return mav.MAV_RESULT_UNSUPPORTED

    # ---------------- 动力学（r21 Vehicle.step）
    def step(self, dt: float, tau: float = 0.35, vmax: float = 8.0, amax: float = 6.0, kp: float = 1.2) -> None:
        self.t += dt
        tgt = self.target
        if self.mode == MODE_ORBIT and self.orbit is not None:
            cn, ce, r, v = self.orbit
            ang = math.atan2(self.p[1] - ce, self.p[0] - cn) + v / r * dt * 4
            tgt = (cn + r * math.cos(ang), ce + r * math.sin(ang), self.p[2])
        elif self.mode == MODE_RTL:
            h = self.home
            legs = [(self.p[0] if self.rtl_leg == 0 else h[0], self.p[1] if self.rtl_leg == 0 else h[1], -RTL_ALT),
                    (h[0], h[1], -RTL_ALT), (h[0], h[1], 0.0)]
            tgt = legs[min(self.rtl_leg, 2)]
            if self.rtl_leg < 2 and math.dist(self.p, tgt) < 1.0:
                self.rtl_leg += 1
        if tgt is not None and self.armed:
            vd = [kp * (tgt[i] - self.p[i]) for i in range(3)]
            s = math.sqrt(sum(x * x for x in vd))
            if s > vmax:
                vd = [x * vmax / s for x in vd]
        else:
            vd = [0.0, 0.0, 0.0]
        a = [(vd[i] - self.v[i]) / tau for i in range(3)]
        an = math.sqrt(sum(x * x for x in a))
        if an > amax:
            a = [x * amax / an for x in a]
        for i in range(3):
            self.v[i] += a[i] * dt
            self.p[i] += self.v[i] * dt
        if self.p[2] > 0.0:
            self.p[2] = 0.0
            self.v[2] = min(self.v[2], 0.0)
        if self.mode == MODE_TAKEOFF and self.p[2] < -TAKEOFF_ALT + 0.1:
            self.mode = self.intended = MODE_LOITER
            self.target = tuple(self.p)
        on_ground = self.p[2] > -0.05
        if self.armed and on_ground and self.mode in (MODE_LAND, MODE_RTL) and abs(self.v[2]) < 0.3:
            self.landed_since = self.t if self.landed_since is None else self.landed_since
            if self.t - self.landed_since >= DISARM_LAND_S:
                self.armed = False
                self.mode = self.intended = MODE_LOITER
        else:
            self.landed_since = None

    def landed_state(self) -> int:
        if self.p[2] > -0.05 and abs(self.v[2]) < 0.3:
            return mav.MAV_LANDED_STATE_ON_GROUND
        if self.mode == MODE_TAKEOFF:
            return mav.MAV_LANDED_STATE_TAKEOFF
        if self.mode == MODE_LAND or (self.mode == MODE_RTL and self.rtl_leg == 2):
            return mav.MAV_LANDED_STATE_LANDING
        return mav.MAV_LANDED_STATE_IN_AIR

    # ---------------- 输出流
    def stream(self) -> bytes:
        base = mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | (mav.MAV_MODE_FLAG_SAFETY_ARMED if self.armed else 0)
        out = [mav.MAVLink_heartbeat_message(mav.MAV_TYPE_QUADROTOR, mav.MAV_AUTOPILOT_PX4, base, self.mode,
                                             mav.MAV_STATE_ACTIVE if self.armed else mav.MAV_STATE_STANDBY, 3),
               mav.MAVLink_current_mode_message(0, self.mode, self.intended),
               mav.MAVLink_extended_sys_state_message(0, self.landed_state())]
        return b"".join(m.pack(self.m) for m in out)

    def decode(self, buf: bytes) -> dict:
        """测试侧解码（与 px4-bridge 相同的字段读取）。"""
        d: dict = {}
        for msg in self.rx.parse_buffer(buf) or []:
            t = msg.get_type()
            if t == "HEARTBEAT":
                d["armed"] = bool(msg.base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED)
                d["custom_mode"] = int(msg.custom_mode)
                d["system_status"] = int(msg.system_status)
            elif t == "CURRENT_MODE":
                d["intended"] = int(msg.intended_custom_mode)
            elif t == "EXTENDED_SYS_STATE":
                d["landed"] = int(msg.landed_state)
        return d
