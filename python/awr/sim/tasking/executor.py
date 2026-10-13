"""架次执行（M20-FR-040 至 FR-047；M20 §6.8、§6.11.2）。

站位类与航段类工作项都以内部调用驱动（principal `tasking:<tid>`、role tasking → MISSION 租约，ADR-016 准入 ④–⑩）：

```text
ASSIGNED（起降位排队、电池与风限）→ takeoff（多旋翼 10 m AGL）→ TRANSIT（follow_path：禁入圆外可见图绕行，转场高度 = max(目标高度,
DTM + 60 m, 走廊 DSM + 15 m)，≤ DTM + 120 m）→ WORKING：航段类 follow_path（持续性按 loop 或 pingpong 重复；停留航点 hover 计时），
站位类 hover（多旋翼）或 loiter（固定翼、垂起），云台经 M13 `set_tasking_pointing` 指向 → 工作项完成或被释放 → STANDBY（悬停待命，
120 s 未分配或低电返航）→ RETURNING（禁入圆外到机巢槽位上方）→ RECOVERING（land）→ 着陆后交机巢检查与换电 → ENDED
```

返航一律由 M20 规划禁入圆外航线（不用 `rtl`，M20-FR-042）；M09 能量 RTL、ELAND、FAILSAFE、坠毁使架次 DROPPED、工作项立即重新分配
（FR-047）；租约被 OPERATOR 或 AGENT 抢占时工作项在下一个 tasking stage（≤ 1 s【仿真】）重新分配、记 `manual`，租约交还后空中
机体进入 STANDBY（FR-046）。转场或作业的 follow_path 被细校验拒绝时退回 `goto{route: auto}`（越障爬升剖面）。M10 的
`MissionConstraints.keepouts` 与 astar25 转场（M10-FR-206、FR-207）尚未交付，按 M20 PRD §11 的过渡方案由 M20 以内部调用执行全部转场。
"""

from __future__ import annotations

import contextlib
import math
from typing import Any

import numpy as np

from awr.tasking.energy import MIN_SHIFT_S
from awr.tasking.keepout import inside, plan_path, plan_route, seg_blocked

from .book import ItemRT, SortieRT, TaskRT

__all__ = ["H_TAKEOFF_M", "STANDBY_TIMEOUT_S", "Executor"]

H_TAKEOFF_M = 10.0
H_TRANSIT_AGL = 60.0
H_CAP_AGL = 120.0
DSM_CLEAR_M = 15.0
STANDBY_TIMEOUT_S = 120.0
T_MARGIN_S = 60.0
RESERVE = 0.2
FAIL_STATES = ("ELAND", "FAILSAFE", "CRASHED")
SAFETY_ACTIVE_CODES = (101, 114, 204)   # 安全动作进行中（101、114）或在途调用被安全动作抢占（204）：稍后重试，不计失败
CODE_FW_PARAM = 110                     # M21 准入：参数越界（盘旋半径低于风下下限等）
FW_LOITER_R_MARGIN = 1.05               # 固定翼盘旋半径相对风下下限的余量
CLIMB_STEP_M = 5.0                      # 转场首段原地爬升的分档（斜升走廊净空的最低爬升高度）
YIELD_R_M = 12.0                        # 间隔让行后续飞：水平距离小于该值、高差小于 YIELD_DZ_M 的他机存在时，编号大者先避让
YIELD_DZ_M = 6.0
YIELD_STEP_M = 8.0                      # 避让：原地升高（或降低）该高度，让对方从下（上）方通过
YIELD_MAX_S = 60.0
YIELD_AHEAD_M = 120.0                   # 续飞前比较双方前方航线是否相交的长度
YIELD_STEP_H_M = 15.0                   # 垂直避让不可行时离开对方前方航线的水平距离
YIELD_PASS_WAIT_S = 20.0                # 先行方等避让方让开的最长时间
EVADE_WIN_S = 180.0                     # 反复撤离：该时长内第 2 次起每次撤离后，转场与返航的规划圆外扩 EVADE_PAD_M（上限 EVADE_PAD_MAX_M）
EVADE_PAD_M = 25.0
EVADE_PAD_MAX_M = 75.0
VERT_CLEAR_M = 8.0                      # 原地垂直升降的竖直走廊半径（其内有他机时先水平让开）
VERT_SIDESTEP_M = 15.0
YAW_STEP_MAX_DEG = 60.0                 # 带机头朝向的 goto 每次相对当前航向的最大转角（M09 快速守卫：悬停中航向误差 > 90° 即 ELAND）
YAW_DONE_DEG = 10.0                     # 分步转向的完成容差
FW_HOLD_PAD_M = 40.0                    # 固定翼就地盘旋圈外扩（入圈外甩与风中跟踪偏差）
FW_HOLD_BORDER_M = 30.0
FW_HOLD_CLEAR_M = 10.0
GROUND_STATES = ("LANDED", "DISARMED", "READY")
FLIGHT_OPS = ("follow_path", "goto", "hover", "orbit", "loiter")
ARRIVE_M = 2.0          # 视为已在作业起点的距离
MAX_SPEED = 12.0
ROUTE_RECHECK_S = 10.0
REROUTE_GAP_S = 30.0
PREDICT_S = 60.0
# D2-FX-R2（验收第 1 轮 D2-AC-35：两架 P600 航线交叉 → M09 FleetGuard 双方让行悬停 → 执行器在转场与返航指令被 101 拒绝后原地
# land → 降落中位置误差 ELAND/FAILSAFE，24 h 38 次）
LAYER_BASE_M = 4.0       # 转场与返航高度层：在走廊高度上按机体 slot 错层（4、9、14、19 m），与巡查航段（h_m）及他机转场错开
LAYER_STEP_M = 5.0
RETRY_S = 3.0            # 安全动作进行中（101）或返航航线被拒：3 s 后重试，不原地降落
DIRECT_MAX_M = 250.0     # 直线安全时直飞（不爬升到转场层）的最大水平距离
RELAY_PREDICT_S = 20.0   # 接力站位转场的航线复核外推上限（与规划口径 v·τ_route 一致；站位本身由跟随维持相对位置）
HOME_NEAR_M = 15.0       # 返航失败时离起降点水平距离小于该值才直接降落
HOME_WAIT_S = 5.0          # 返航航线止于机巢外（机巢在运动识别物禁入圆内）时的等待重试间隔
HOME_WAIT_MAX_S = 120.0    # 机巢被禁入圆占据时的最长等待，之后（或能量临界时）直飞降落
NEST_BLOCKED_ETA_S = 30.0  # 接替 ETA：机巢在禁入圆内时的撤离耗时
ETA_SCALE = 1.2            # 接替 ETA 的放大系数
ETA_PAD_S = 10.0
OFF_NEST_M = 30.0          # 落地点离起降点水平距离超过该值：机巢外落地（不按回收入巢）
TALL_GRID_M = 10.0         # 固定翼高楼障碍的网格
TALL_RANGE_M = 1500.0      # 高楼障碍的计算范围（机巢外扩）
TALL_TURN_M = 40.0         # 高楼障碍圆的转弯余量
TALL_TILE_M = 40.0         # 高楼障碍圆的分块边长
TALL_MR_M = 10.0           # 多旋翼绕高楼的余量（D2-INT：多旋翼转场高度封顶 H_CAP_AGL + 40，更高的楼此前直线穿越，细校验 102）
FW_CEIL_AGL_M = 150.0      # 固定翼转场高度上限（低于 M09 高度上限 160 m 留 10 m 的高度保持余量；S7 诊断中 160.4 m 触发 ALT_MAX → RTL）
UAV_SEP_M = 15.0           # 直飞捷径与其他空中机体的最小间隔
MR_BORDER_INSET_M = 15.0   # 多旋翼绕行顶点离世界边界的最小距离
VZ_DN_MAX_MPS = 3.0        # 多旋翼下降速率上限（P600 p600_std MPC_Z_VEL_MAX_DN）：斜降直飞按该值限制水平速度（实际更慢时只会偏高）
# D2-FX-R2-tasking 第 2 次（验收第 1 轮 D2-AC-16：区域巡查的接替机 B 直飞到在岗机 A 正在往返的航线起点，两机在 2 m 处互相让行死锁约 290 s）
RELIEF_DZ_M = 15.0         # 持续性航线工作项的接替会合点：航线起点上方
RELIEF_CLEAR_M = 25.0      # 接替机到达会合点后，等在岗机水平离开该距离再接手（在岗机随即返航、接替机下降入航线）
RELIEF_WAIT_MAX_S = 90.0   # 等待上限：之后只要求水平离开 RELIEF_MIN_M
RELIEF_MIN_M = 8.0


class Executor:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.by_cid: dict[str, str] = {}
        self.stats = {"sorties": 0, "launches": 0, "recoveries": 0, "dropped": 0, "preempted": 0, "evades": 0, "rejected": 0,
                      "flight_s": 0.0, "reroutes": 0}
        self._recheck_t = -math.inf
        self._tall: np.ndarray | None = None
        self._corridor_top = -math.inf
        self.route_blocked = False          # 最近一次 route()：转场高度封顶后走廊仍有高于航线的建筑（多旋翼改由 M10 自动航线）

    # ------------------------------------------------------------ 调用
    def principal(self, tid: str | None) -> dict:
        return {"principal_id": f"tasking:{tid or 'm20'}", "role": "tasking", "entry": "tasking"}

    def submit(self, so: SortieRT, op: str, args: dict) -> dict:
        rt = self.rt
        if (op in FLIGHT_OPS and so.airborne and so.state not in ("RECOVERING", "DROPPED") and rt.fs(so.uav) in GROUND_STATES
                and rt.t - (so.t_launched if so.t_launched is not None else rt.t) > 10.0):
            # D2-INT：机体已在机巢外落地（S7 中 AWR-V1 的多旋翼悬停用满 180 s 后迫降在楼顶），飞行指令必被 105 拒绝；此前
            # follow_path 与 goto 互为退路，每个 tasking 周期一对被拒，2400 s 内 2 万余条 cmd.rejected。改为丢弃该架次一次
            self._grounded(so)
            return {"status": "rejected", "code": 105}
        if op != "goto" or args.get("yaw_rad") is None:
            so.yaw_goal = None                         # 其他指令取代分步转向
        so.n_calls += 1
        cid = f"m20:{so.uav}:{so.sortie_id}:{so.n_calls}"
        so.cid, so.call_op = cid, op
        so.goto_goal = tuple(float(x) for x in args["pos"][:3]) if op == "goto" and args.get("pos") is not None \
            and all(x is not None for x in args["pos"][:3]) else None
        self.by_cid[cid] = so.uav
        if rt.cmd is None:
            return {"status": "rejected", "code": 213}
        adm = rt.cmd.submit_internal({"cid": cid, "op": op, "uav": so.uav, "args": args}, self.principal(so.tid))
        if adm.get("status") == "rejected":
            self.stats["rejected"] += 1
            rt.results.append((cid, "rejected", int(adm.get("code") or 0), adm.get("detail")))
        return adm

    def yaw_goto(self, so: SortieRT, pos: Any, yaw: float, **extra: Any) -> dict:
        """goto(pos) 带机头朝向 yaw（ENU）：相对当前航向的转角每次不超过 YAW_STEP_MAX_DEG，余量记在 `so.yaw_goal`，goto 完成后由
        `_yaw_continue` 续转。D2-FX-R2-tasking 第 2 次：M09 快速守卫在悬停中航向误差 > 90° 即 ELAND（SAF.CTRL.YAW_ERR），区域巡查看洞点
        原地转向 128° 时 6 架 P600 先后迫降。"""
        cur = self.rt.yaw(so.uav)
        y = float(yaw)
        if cur is not None:
            d = math.atan2(math.sin(y - cur), math.cos(y - cur))
            lim = math.radians(YAW_STEP_MAX_DEG)
            if abs(d) > lim:
                y = cur + math.copysign(lim, d)
        adm = self.submit(so, "goto", {"pos": [float(x) for x in list(pos)[:3]], "route": "direct", "yaw_rad": y, **extra})
        so.yaw_goal = float(yaw)
        return adm

    def _yaw_continue(self, so: SortieRT) -> bool:
        """分步转向：离目标朝向仍 > YAW_DONE_DEG 时在当前位置再转一步（True）；否则结束（False）。"""
        g = so.yaw_goal
        if g is None:
            return False
        cur = self.rt.yaw(so.uav)
        if cur is None or abs(math.atan2(math.sin(g - cur), math.cos(g - cur))) <= math.radians(YAW_DONE_DEG):
            so.yaw_goal = None
            return False
        self.yaw_goto(so, self.rt.pos(so.uav), g)
        return True

    # ------------------------------------------------------------ 几何
    def transit_z(self, P2: np.ndarray, z_goal: float, layer_m: float = 0.0, cap_agl: float = H_CAP_AGL + 40.0) -> float:
        rt = self.rt
        n = max(2, int(float(np.linalg.norm(np.diff(P2, axis=0), axis=1).sum()) / 10.0) + 1)
        s = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(P2, axis=0), axis=1))]
        L = s[-1] if s[-1] > 0 else 1.0
        q = np.linspace(0.0, L, n)
        pts = np.c_[np.interp(q, s, P2[:, 0]), np.interp(q, s, P2[:, 1])]
        dsm = rt.dsm(pts)
        dtm = rt.dtm(pts)
        self._corridor_top = float(dsm.max())
        # 高度层加在走廊基准高度上（不叠加到当前高度：改道时不产生原地爬升航点，D2-FX-R2）
        z = max(float(z_goal), float(dtm.max()) + H_TRANSIT_AGL + float(layer_m), float(dsm.max()) + DSM_CLEAR_M + float(layer_m))
        return min(z, float(dtm.min()) + float(cap_agl))

    def direct_ok(self, a: np.ndarray, b: np.ndarray, C: np.ndarray, *, profile: bool = False, max_m: float = DIRECT_MAX_M) -> bool:
        """a → b（N×3 端点）可直飞：水平距离 ≤ DIRECT_MAX_M、线段不穿越禁入圆、起点不在圆内、走廊（±5 m）DSM + 10 m ≤ 两端较低高度。
        `profile`（多旋翼）：改为按直线斜升（降）剖面逐点复核走廊 DSM 净空（`_fly` 按爬升率限制水平速度，使 goto 沿直线；起点水平
        15 m 内为本机起飞或悬停的空域，净空要求降为 3 m）。D2-FX-R2-tasking 第 2 次（D2-AC-34）：此前从 10 m 起飞高度出发的转场一律先原地爬升到转场层
        （synthcity T5 首个看洞点约 25 s 爬升），会话开始后 ≤ 60 s 的首次捕获做不到。"""
        a2, b2 = a[:2], b[:2]
        L = float(np.hypot(*(b2 - a2)))
        if max_m < L:
            return False
        if len(C) and (len(seg_blocked(a2, b2, C)) or bool(inside(a2[None], C)[0])):
            return False
        if L < 1.0:
            return True
        n = max(2, int(L / 5.0) + 1)
        u = (b2 - a2) / L
        nrm = np.array([-u[1], u[0]])
        ts = np.linspace(0.0, 1.0, n)
        pts = np.vstack([a2 + (b2 - a2) * t + nrm * o for t in ts for o in (-5.0, 0.0, 5.0)])
        dsm = self.rt.dsm(pts).reshape(n, 3).max(axis=1)
        if not profile:
            return float(dsm.max()) + DSM_CLEAR_M - 5.0 <= min(float(a[2]), float(b[2]))
        z_line = float(a[2]) + (float(b[2]) - float(a[2])) * ts
        need = np.where(ts * L <= 15.0, 3.0, DSM_CLEAR_M - 5.0)
        return bool(np.all(dsm + need <= z_line))

    def fw_margin_m(self, so: SortieRT) -> float:
        m = self.rt.model(self.rt.model_id(so.uav))
        return 0.0 if m is None or m.frame == "multirotor" else self.fw_loiter_r(so)

    def fw_loiter_r(self, so: SortieRT) -> float:
        """固定翼与垂起机的盘旋与转弯半径：设计风 10 m/s 的下限按当前风换算 `R ∝ (V_a + w)²`（V_a ≈ 15 m/s）并留 15% 余量，
        不小于 80 m（r_loiter_default）且不小于当前风下的准入下限（M21 第 ⑥ 步 110）。"""
        m = self.rt.model(self.rt.model_id(so.uav))
        if m is None or m.frame == "multirotor":
            return 0.0
        r = self.rt.fw_r_min(so.uav)
        if r is None:
            w = self.rt.wind_mps(self.rt.pos(so.uav))
            r = float(m.r_loiter_min_m or 80.0) * ((15.0 + w) / 25.0) ** 2
        return max(80.0, 1.15 * float(r))

    def fw_hold_center(self, so: SortieRT, p: Any, r: float, *, home: bool = True) -> np.ndarray:
        """固定翼就地盘旋（待命、停留航点）的圆心：盘旋圈外扩 FW_HOLD_PAD_M 的环上离世界边界 ≥ FW_HOLD_BORDER_M、DSM 净空
        ≥ FW_HOLD_CLEAR_M 时取当前位置，否则依次改取本架次的作业站位、机巢上方（同高度）。D2-FX-R2-tasking 第 2 次（D2-AC-16）：
        19 m/s 风中定点盯防完成后在边界内 60 m 处就地待命盘旋，折线等待越出世界边界 → CORRECTING → RTL。"""
        rt = self.rt
        p = np.asarray(p, float)
        cands = [p]
        if so.station is not None:
            q = np.asarray(so.station, float)
            cands.append(np.array([q[0], q[1], max(float(p[2]), float(q[2]))]))
        if home:
            h = self._home_xy(so)
            cands.append(np.array([float(h[0]), float(h[1]), float(p[2])]))
        th = np.linspace(0.0, 2.0 * math.pi, 24, endpoint=False)
        zones = getattr(rt.world, "zones", None) if rt.world is not None else None
        for c in cands:
            P = np.c_[c[0] + (r + FW_HOLD_PAD_M) * np.cos(th), c[1] + (r + FW_HOLD_PAD_M) * np.sin(th)]
            ok = bool(np.all(rt.dsm(P) + FW_HOLD_CLEAR_M <= float(c[2])))
            if ok and zones is not None:
                with contextlib.suppress(Exception):
                    ok = bool(np.all(np.asarray(zones.border_signed_distance(P), float) >= FW_HOLD_BORDER_M))
            if ok:
                return c
        return p

    def layer_m(self, so: SortieRT) -> float:
        """转场与返航的高度层（按 slot 错层；固定翼不错层，其盘旋与航线由 M21 保持高度）。"""
        m = self.rt.model(self.rt.model_id(so.uav))
        if m is not None and m.frame != "multirotor":
            return 0.0
        return LAYER_BASE_M + LAYER_STEP_M * (int(so.slot) % 4 if so.slot is not None and so.slot >= 0 else 0)

    def safe_goal(self, b: np.ndarray, *, extra_m: float = 10.0, C: np.ndarray | None = None) -> np.ndarray:
        """目标点在禁入圆内（识别物逼近）时，沿圆心 → 目标方向投到圆外 extra_m 处（宁可偏离站位也不进入，M20 §1.4 第 3 条）。
        `C` 缺省为规划口径；接力站位传入站位口径（`RelayController.station_circles`）。"""
        rt = self.rt
        C = rt.circles(planning=True) if C is None else np.asarray(C, float).reshape(-1, 3)
        q = np.asarray(b, float).copy()
        for _ in range(4):
            if len(C) == 0 or not bool(inside(q[None, :2], C)[0]):
                return q
            j = int(np.argmin(np.hypot(C[:, 0] - q[0], C[:, 1] - q[1]) - C[:, 2]))
            u = q[:2] - C[j, :2]
            n = float(np.hypot(*u))
            u = u / n if n > 1e-6 else np.array([1.0, 0.0])
            q[:2] = C[j, :2] + u * (C[j, 2] + extra_m)
        return q

    def route(self, a: np.ndarray, b: np.ndarray, *, z_end: float | None = None, layer_m: float = 0.0, fw_margin_m: float = 0.0,
              descend: bool = False, uav: str | None = None, goal_c: np.ndarray | None = None) -> np.ndarray:
        """a → b 的禁入圆外航线（N×3，不含 a 本身）：转场高度巡航，末点为 b（z_end 为 None 时 b 的高度；b 在禁入圆内时投到圆外）。"""
        rt = self.rt
        self.route_blocked = False
        Cb = rt.circles(planning=True).reshape(-1, 3)
        # 高楼障碍圆：固定翼（D2-FX-R2）与多旋翼（D2-INT：转场高度封顶 DTM + 160 m，chicago 等世界的高楼高于封顶，多旋翼的
        # 转场航线此前直线穿楼，细校验 102 后退回 goto，例 chicago T3）都水平绕行；多旋翼余量 TALL_MR_M
        Tc = self.tall_circles()
        tall_m = TALL_TURN_M if fw_margin_m > 0.0 else TALL_MR_M
        b = self.safe_goal(np.asarray(b, float), C=goal_c)
        a2, b2 = np.asarray(a, float)[:2], np.asarray(b, float)[:2]
        zb0 = float(b[2] if z_end is None else z_end)
        if len(Cb):
            # 终点在规划口径的圆内而在站位口径外（运动识别物的接力站位）：包含终点的圆收缩到终点以内 1 m（转场仍绕开其余部分）
            db = np.hypot(Cb[:, 0] - b2[0], Cb[:, 1] - b2[1])
            Cb = Cb.copy()
            Cb[:, 2] = np.where(db < Cb[:, 2], np.maximum(db - 1.0, 0.0), Cb[:, 2])
            pad = self.evade_pad(uav)
            if pad > 0.0:
                # 反复撤离（随机游走识别物的禁入圆贴着航线、每次重规划都沿圆边切线走）：规划圆外扩，端点所在处只外扩到端点以内 1 m
                # （D2-FX-R2-tasking 第 2 次，D2-AC-16：巡边机在区域东南角返航—撤离循环 640 s，能量 RTL 直线返航被发现）
                r_inf = Cb[:, 2] + pad
                for e in (a2, b2):
                    d = np.hypot(Cb[:, 0] - e[0], Cb[:, 1] - e[1])
                    r_inf = np.where(d < r_inf, np.maximum(Cb[:, 2], d - 1.0), r_inf)
                Cb[:, 2] = r_inf
        C = self._route_circles(Cb, Tc, fw_margin_m, (a2, b2), tall_m=tall_m)
        if self.direct_ok(np.asarray(a, float), np.array([b2[0], b2[1], zb0]), C, profile=fw_margin_m <= 0.0) \
                and self._seg_clear_of_uavs(np.asarray(a, float), np.array([b2[0], b2[1], zb0]), uav):
            # 短距离且直线安全（禁入圆外、走廊 DSM 净空 ≥ 10 m）：直接飞到目标点，不爬升到转场层（D2-FX-R2：跟随与换站时
            # 爬回转场层又下降，接近站位时被改道）
            return np.asarray([[float(b2[0]), float(b2[1]), zb0]], float)
        start = a2
        pre: list[np.ndarray] = []
        if len(Cb) and bool(inside(a2[None], Cb)[0]):
            # 起点在禁入体内（识别物逼近）：先沿离开方向退出
            j = int(np.argmin(np.hypot(Cb[:, 0] - a2[0], Cb[:, 1] - a2[1]) - Cb[:, 2]))
            u = a2 - Cb[j, :2]
            n = float(np.hypot(*u))
            u = u / n if n > 1e-6 else np.array([1.0, 0.0])
            start = Cb[j, :2] + u * (Cb[j, 2] + 10.0)
            pre = [start]
        # 绕行顶点在世界边界内（多旋翼内缩 MR_BORDER_INSET_M；D2-FX-R2-tasking 第 2 次：多旋翼的可见图绕行此前不限边界，区域巡查 + 巡边中转场航线
        # 绕到边界外，准入 102 OUT_OF_BORDER，转场—撤离循环 1000 s 以上）
        nk = self._border_node_ok(30.0 + float(fw_margin_m)) if fw_margin_m > 0.0 else self._border_node_ok(MR_BORDER_INSET_M)
        # 逐级放宽（D2-FX-R2：此前可见图一次无解即退回直线，固定翼的直线穿过高楼与禁入圆，S7 中撞楼）：
        # 外扩 + 高楼 + 边界内缩 30 + R → 内缩 30 m → 不外扩 + 高楼 → 只有禁入圆 → 直线
        P2 = None
        stages = [(self._route_circles(Cb, Tc, fw_margin_m, (a2, start, b2), tall_m=tall_m), nk)]
        if fw_margin_m > 0.0:
            nk0 = self._border_node_ok(30.0)          # 固定翼的绕行顶点始终在世界边界内（只放宽内缩量）
            stages += [(stages[0][0], nk0), (self._route_circles(Cb, Tc, 0.0, (a2, start, b2)), nk0), (Cb, nk0)]
        elif len(Tc):
            stages.append((Cb, nk))                   # 多旋翼：绕高楼无解时只绕禁入圆（与 D2-INT 之前相同）
        for Cs, ok in stages:
            if not len(Cs):
                P2 = np.vstack([start, b2])
                break
            P2 = plan_path(start, b2, Cs, node_ok=ok)
            if P2 is not None:
                break
        if P2 is None:
            P2 = np.vstack([start, b2])
        P2 = np.vstack([*pre, P2]) if pre else P2
        zb = float(b[2]) if z_end is None else float(z_end)
        # 返航（descend）按走廊高度下降，不保持当前高度（垂起机从 160 m 高空到机巢上方再以多旋翼慢降，悬停时间不够）
        z_tr = self.transit_z(np.vstack([a2, P2]), zb if descend else max(zb, float(a[2])), layer_m,
                              FW_CEIL_AGL_M if fw_margin_m > 0.0 else H_CAP_AGL + 40.0)
        # 封顶后的转场高度仍低于走廊 DSM（+ 细校验的碰撞半径与缓冲 2 m）：这条航线会被 M08 细校验以 102 拒绝
        self.route_blocked = self._corridor_top + 2.0 > z_tr
        pts = [np.array([a2[0], a2[1], z_tr])] if abs(float(a[2]) - z_tr) > 2.0 else []
        pts += [np.array([p[0], p[1], z_tr]) for p in P2]
        pts.append(np.array([b2[0], b2[1], zb]))
        out = [pts[0]]
        for p in pts[1:]:
            if float(np.linalg.norm(p - out[-1])) > 0.5:
                out.append(p)
        P = np.asarray(out, float)
        if len(P) and float(np.linalg.norm(P[0] - np.asarray(a, float))) < 1.0:
            P = P[1:]
        return P

    # ------------------------------------------------------------ 分配生效
    def assign(self, uav: str, T: TaskRT, it: ItemRT, a: dict) -> bool:
        rt = self.rt
        so = rt.sorties.get(uav)
        t = rt.t
        if so is None:
            nid = rt.nest_of(uav)
            n = rt.nests.get(nid) if nid else None
            if n is None or uav not in n.air:
                return False
            ac = n.air[uav]
            if ac.state not in ("ready", "turnaround", "swapping", "waiting_pack", "charging"):
                return False
            so = SortieRT(uav, rt.slot_of(uav), nid, f"s{next(rt._sortie_seq)}", T.tid, it.w.wid, "ASSIGNED", t)
            t_ready = n.t_ready(uav, t)
            if not math.isfinite(t_ready):
                return False
            so.t_launch = n.pad.book(max(t, t_ready), "launch", uav)
            so.principal = f"tasking:{T.tid}"
            n.launch_queue.append({"uav": uav, "tid": T.tid, "t_planned_ns": int(so.t_launch * 1e9),
                                   "delay_reason": None if so.t_launch <= t + 1e-6 else ("battery" if t_ready > t else "launch_interval")})
            if so.t_launch > t + 1.0:
                rt.emit("nest.dispatch_delayed", 2, uav=uav, nest=nid, tid=T.tid, wid=it.w.wid,
                        reason="battery" if t_ready > t else "launch_interval", delay_s=round(so.t_launch - t, 1))
                rt.relays.note_delay(T, "battery" if t_ready > t else "launch_interval")
            rt.sorties[uav] = so
            self.stats["sorties"] += 1
        else:
            if so.tid != T.tid:
                rt.release_lease(uav, so.principal)
            so.tid, so.wid, so.released, so.standby_since = T.tid, it.w.wid, False, None
            so.principal = f"tasking:{T.tid}"
            self.start_transit(so)
        prev = it.uav
        if it.relief_for is None and prev and prev != uav:
            it.relief_for = prev
        it.state, it.uav, it.t_assigned = "ASSIGNED", uav, t
        it.eta_s = float(a.get("eta_s")) if a.get("eta_s") is not None else None
        prt = rt.perception_rt()
        if prt is not None and T.type in ("area_patrol", "area_scan_gsd", "area_guard", "perimeter_patrol", "polyline_patrol"):
            with contextlib.suppress(Exception):
                prt.set_cue(uav, True)
        return True

    # ------------------------------------------------------------ 状态推进
    def item_of(self, so: SortieRT) -> tuple[TaskRT | None, ItemRT | None]:
        T = self.rt.tasks.get(so.tid or "")
        it = T.items.get(so.wid or "") if T is not None else None
        return T, it

    def _launch(self, so: SortieRT) -> None:
        rt = self.rt
        n = rt.nests.get(so.nest_id or "")
        m = rt.model(rt.model_id(so.uav))
        so.e0_wh = rt.soc(so.uav) * (m.e_use_wh if m else 200.0)
        adm = self.submit(so, "takeoff", {"alt_m": H_TAKEOFF_M if (m is None or m.hovers) else 30.0})
        if adm.get("status") == "rejected":
            return
        so.state, so.t_state, so.t_launched = "LAUNCHING", rt.t, rt.t
        self.stats["launches"] += 1
        if n is not None:
            a = n.air.get(so.uav)
            if a is not None:
                a.state = "airborne"
            n.launch_queue = [q for q in n.launch_queue if q.get("uav") != so.uav]
        rt.emit("nest.launch", 0, uav=so.uav, nest=so.nest_id, tid=so.tid, wid=so.wid)
        rt.relays.on_launch(so)

    def _fw_wind_adjust(self, so: SortieRT, it: ItemRT) -> None:
        """固定翼定点盘旋的执行期风下复核（分解时 M07 的风可能尚未就绪，且风随时间变化；口径同 `decompose._fw_wind_station`）：
        站位高度的风超过机型抗风限时，把站位降到风 ≤ 抗风限、盘旋圈（半径 ≥ 该风速下的盘旋半径下限 × 1.02）上 DSM 净空 ≥ 10 m、
        离世界边界 ≥ 30 m、在禁入圆外的最高 AGL（每 5 m，最低 30 m），盘旋半径相应放大，告警 `fw_wind_alt`。"""
        rt = self.rt
        m = rt.model(rt.model_id(so.uav))
        w_it = it.w
        if m is None or m.frame != "fixed_wing" or w_it.kind != "station" or w_it.pos is None or (w_it.needs or {}).get("fw_wind_done"):
            return
        w_it.needs = dict(w_it.needs or {})
        w_it.needs["fw_wind_done"] = True
        xy = np.asarray(w_it.pos[:2], float)
        g = float(rt.dtm(xy[None])[0])
        agl0 = float(w_it.pos[2]) - g
        rating = float(m.wind_max_mps)
        if rt.wind_mps(np.r_[xy, g], agl0) <= rating:
            return
        th = np.linspace(0.0, 2.0 * math.pi, 36, endpoint=False)
        C = rt.circles(planning=True)
        zones = getattr(rt.world, "zones", None) if rt.world is not None else None
        agl = math.floor(agl0 / 5.0) * 5.0
        while agl >= 30.0:
            wv = rt.wind_mps(np.r_[xy, g], agl)
            if wv <= rating:
                r = max(float(w_it.loiter_radius_m or 0.0), 1.02 * m.r_loiter_at(wv))
                # 盘旋圈两侧环带（入圈与风中跟踪偏差：内 30 m、外 40 m）的 DSM 净空（D2-FX-R2-tasking 第 2 次：只核对圈上时，入圈外甩 25 m 撞上 60 m 的楼）
                P = np.vstack([np.c_[xy[0] + rr * np.cos(th), xy[1] + rr * np.sin(th)] for rr in (r - 30.0, r, r + 40.0)])
                ok = bool(np.all(rt.dtm(P) + agl >= rt.dsm(P) + 10.0))
                if ok and zones is not None:
                    with contextlib.suppress(Exception):
                        ok &= bool(np.all(np.asarray(zones.border_signed_distance(P), float) >= 30.0))
                if ok and len(C):
                    ok &= bool(np.all(np.hypot(P[:, None, 0] - C[None, :, 0], P[:, None, 1] - C[None, :, 1]) >= C[None, :, 2]))
                if ok:
                    w_it.pos = (float(xy[0]), float(xy[1]), g + agl)
                    w_it.loiter_radius_m = r
                    T = rt.tasks.get(w_it.tid)
                    if T is not None and "fw_wind_alt" not in T.warnings:
                        T.warnings.append("fw_wind_alt")
                    rt.emit("task.warning", 1, tid=w_it.tid, uav=so.uav, code="fw_wind_alt", alt_m=round(agl, 1), radius_m=round(r, 1),
                            wind_mps=round(float(wv), 1))
                    return
            agl -= 5.0

    def start_transit(self, so: SortieRT) -> None:
        rt = self.rt
        _T, it = self.item_of(so)
        if it is None:
            self.release(so, "item_gone")
            return
        self._fw_wind_adjust(so, it)
        goal = self.work_start(so, it)
        p0 = np.asarray(rt.pos(so.uav), float)
        P = self.route(p0, goal, layer_m=self.layer_m(so), fw_margin_m=self.fw_margin_m(so), uav=so.uav,
                       goal_c=rt.relays.station_circles() if it.w.kind == "relay_station" else None)
        so.state, so.t_state = "TRANSIT", rt.t
        so.retry_t = None
        so.climb_first = False
        if not (it.relief_for == so.uav and it.state in ("PENDING", "ASSIGNED")):
            it.state = "ACTIVE"
        # 已在作业起点（撤离或改道后常见）：直接开始作业。D2-INT：此前仍下发 [当前点, 起点] 两点航线，M10 的刹停点与当前点
        # 几乎重合，折线规划给出 0.94 m、944 s 的轨迹，定点盯防因此长期停在转场（T1 不再捕获）
        if len(P) == 0 or (len(P) == 1 and float(np.linalg.norm(np.asarray(P[0], float) - p0)) < ARRIVE_M):
            self.start_work(so)
            return
        if self.route_blocked and self._is_mr(so):
            # D2-INT：禁入圆与高楼之间没有封顶高度以下的水平航线（chicago T3：三个识别物的禁入圆把区域起点围在高楼区边上），
            # 不再先下发必被细校验 102 拒绝的 follow_path，直接交给 M10 自动航线（与 follow_path 失败后的退路相同）
            self.stats["transit_auto"] = self.stats.get("transit_auto", 0) + 1
            self.submit(so, "goto", {"pos": [float(x) for x in goal], "route": "auto"})
            return
        if self._is_mr(so) and len(P) >= 2 and float(np.hypot(P[0][0] - p0[0], P[0][1] - p0[1])) < 0.5 and float(P[0][2] - p0[2]) > 5.0:
            # 首段为原地爬升：到下一航点的斜升直线走廊净空足够时斜升直飞（按爬升率限制水平速度，沿直线），不先原地爬升；否则只原地
            # 爬升到斜升走廊恰好净空的高度（每 CLIMB_STEP_M 一档），其余斜升（D2-FX-R2-tasking 第 2 次，D2-AC-34：suzhou T5 首个看洞点
            # 原地爬升 100 m 用 25 s 再平飞 24 s，斜线在 90 m 处被 72 m 的楼挡住）
            nxt = np.asarray(P[1], float)
            if self.direct_ok(p0, nxt, np.zeros((0, 3)), profile=True, max_m=600.0):
                P = P[1:]
            else:
                z_top = float(P[0][2])
                for z_c in np.arange(float(p0[2]) + CLIMB_STEP_M, z_top - CLIMB_STEP_M + 1e-6, CLIMB_STEP_M):
                    if self.direct_ok(np.array([p0[0], p0[1], z_c]), nxt, np.zeros((0, 3)), profile=True, max_m=600.0):
                        P = np.vstack([np.array([P[0][0], P[0][1], z_c])[None, :], np.asarray(P[1:], float)])
                        break
        if self._climb_first(so, P, p0):
            return
        self._fly(so, P)

    def _climb_first(self, so: SortieRT, P: np.ndarray, p0: np.ndarray) -> bool:
        """航线首点是原地爬升（> 5 m）时先单独爬升：多旋翼避免跟踪器把垂直段并入斜线；垂起机以多旋翼爬到转场高度再转固定翼
        （D2-FX-R2：此前在 30 m AGL 转固定翼后边飞边爬，城区低空撞楼并被车辆发现）。爬升完成后重新转场。"""
        m = self.rt.model(self.rt.model_id(so.uav))
        if m is not None and m.frame == "fixed_wing":
            return False
        if m is not None and m.frame == "vtol" and self.rt.fw_mode(so.uav) not in (None, "MC", "TRANSITION_MC"):
            return False                            # 固定翼模式的垂起机不下发悬停类 goto（会转多旋翼；S7 诊断中随后 CORRECTING → RTL → 开伞）
        if len(P) < 2 or float(np.hypot(P[0][0] - p0[0], P[0][1] - p0[1])) >= 0.5 or float(P[0][2] - p0[2]) <= 5.0:
            return False
        so.climb_first = True
        side = self._sidestep(so, float(P[0][2]))
        if side is not None:
            self.submit(so, "goto", {"pos": [float(x) for x in side], "route": "direct"})
            return True
        self.submit(so, "goto", {"pos": [float(p0[0]), float(p0[1]), float(P[0][2])], "route": "direct",
                                 "speed_mps": float(min(m.v_cruise_mps if m is not None and m.frame == "multirotor" else 5.0, MAX_SPEED))})
        return True

    def _sidestep(self, so: SortieRT, z_to: float) -> np.ndarray | None:
        """原地垂直升降的竖直走廊（水平 < VERT_CLEAR_M、高度在两端之间 ±3 m）内有其他空中架次时，先在当前高度水平离开它
        VERT_SIDESTEP_M（DSM 净空与世界边界复核）；没有阻挡或无处可去时 None。完成后由调用方重新规划（爬升前再核一次）。
        D2-FX-R2-tasking 第 2 次（D2-AC-16）：巡线 once 与 pingpong 的两机都停在终点上方（70 m 与 80 m），低者返航原地爬升穿过高者相撞。"""
        rt = self.rt
        p = np.asarray(rt.pos(so.uav), float)
        lo, hi = min(float(p[2]), z_to) - 3.0, max(float(p[2]), z_to) + 3.0
        for o in sorted(rt.sorties.values(), key=lambda x: x.uav):
            if o is so or not o.airborne:
                continue
            q = np.asarray(rt.pos(o.uav), float)
            if float(np.hypot(q[0] - p[0], q[1] - p[1])) >= VERT_CLEAR_M or not lo <= float(q[2]) <= hi:
                continue
            u = p[:2] - q[:2]
            n = float(np.hypot(*u))
            u = u / n if n > 0.5 else np.array([1.0, 0.0])
            zones = getattr(rt.world, "zones", None) if rt.world is not None else None
            for off in (0.0, 45.0, -45.0, 90.0, -90.0, 135.0, -135.0, 180.0):
                ang = math.atan2(u[1], u[0]) + math.radians(off)
                xy = p[:2] + VERT_SIDESTEP_M * np.array([math.cos(ang), math.sin(ang)])
                if float(rt.dsm(xy[None])[0]) + DSM_CLEAR_M > float(p[2]):
                    continue
                if zones is not None:
                    with contextlib.suppress(Exception):
                        if float(np.asarray(zones.border_signed_distance(xy[None]), float)[0]) < MR_BORDER_INSET_M:
                            continue
                return np.array([xy[0], xy[1], p[2]])
            return None
        return None

    def _path(self, so: SortieRT, P: np.ndarray, *, speed: float | None = None) -> dict:
        rt = self.rt
        m = rt.model(rt.model_id(so.uav))
        args: dict[str, Any] = {"waypoints": [[round(float(x), 3) for x in p] for p in np.asarray(P, float)[:1000]]}
        if m is None or m.frame == "multirotor":
            args["speed_mps"] = float(min(speed or (m.v_cruise_mps if m else 8.0), MAX_SPEED))
        if len(args["waypoints"]) < 2:
            args["waypoints"] = [args["waypoints"][0], args["waypoints"][0]] if args["waypoints"] else []
            if args["waypoints"]:
                p0 = rt.pos(so.uav)
                args["waypoints"][0] = [round(float(x), 3) for x in p0]
        so.path_cur = np.asarray(args["waypoints"], float).reshape(-1, 3) if args["waypoints"] else None
        so.path_speed = speed
        return self.submit(so, "follow_path", args)

    # ------------------------------------------------------------ 航线复核（M20-FR-045）
    def recheck_routes(self) -> int:
        """每 10 s【仿真】：对执行中的 follow_path 航线（自当前位置起的剩余部分）按识别物 60 s 内的预测位置做距离检查（M20-FR-045）。

        时间对齐：剩余航线按速度换算每段的到达时刻 [t_i, t_i+1]，运动识别物（沿当前速度外推）取这两个时刻（截到 60 s）的禁入圆，
        静止识别物取当前禁入圆；只有机体到达该段时识别物会在那里，才算预测违规（识别物在机体飞过之后才到达的段不算）。违规即
        重规划：转场与返航从当前位置重新绕行，作业航段把剩余部分经可见图绕开预测禁入圆后重发；同一架次 30 s 内至多重规划一次
        （重发航线会带来跟踪瞬态），结果生效前由 5 Hz 禁入守卫兜底。返回重规划次数。"""
        rt = self.rt
        kos = rt.keepouts(planning=False)
        if not kos:
            return 0
        trt = rt.targets_rt()
        rows = []                                   # (x, y, r, vx, vy)
        hull_rows = []                              # 作业航线：活动范围已知的运动识别物按其静态包络（不外推）
        for k in kos:
            vx = vy = 0.0
            if k.moving and trt is not None and k.target:
                st = trt.state1(k.target)
                if st is not None:
                    vx, vy = float(st.vel[0]), float(st.vel[1])
            rows.append((k.x, k.y, k.r, vx, vy))
            hull = rt.motion_hull(k.target, k) if k.moving else None
            if hull:
                hull_rows.extend((x, y, r - 10.0, 0.0, 0.0) for x, y, r in hull)
            else:
                hull_rows.append((k.x, k.y, k.r, vx, vy))
        K = np.asarray(rows, float).reshape(-1, 5)
        KH = np.asarray(hull_rows, float).reshape(-1, 5)
        n = 0
        for uav in sorted(rt.sorties):
            so = rt.sorties[uav]
            P = so.path_cur
            if P is None or len(P) < 2 or so.call_op != "follow_path" or so.state not in ("TRANSIT", "WORKING", "RETURNING"):
                continue
            if rt.t - so.reroute_t < REROUTE_GAP_S or (so.action or {}).get("search"):
                continue                                 # 搜索航线每圈按规划禁入圆重新生成，不复核
            p = rt.pos(uav)
            k0 = int(np.argmin(np.hypot(P[:, 0] - p[0], P[:, 1] - p[1])))
            rest = np.vstack([p[None, :], P[k0 + 1:]]) if k0 + 1 < len(P) else np.vstack([p[None, :], P[-1:]])
            v = max(float(so.path_speed or 0.0) or 8.0, 1.0)
            seg_t = np.r_[0.0, np.cumsum(np.hypot(*np.diff(rest[:, :2], axis=0).T))] / v
            C_hit = []
            KK = KH if so.state == "WORKING" else K
            _T, it_r = self.item_of(so)
            pmax = RELAY_PREDICT_S if (so.state == "TRANSIT" and it_r is not None and it_r.w.kind == "relay_station") else PREDICT_S
            for i in range(len(rest) - 1):
                taus = np.clip(np.array([seg_t[i], seg_t[i + 1]]), 0.0, pmax)
                C = np.vstack([np.c_[KK[:, 0] + KK[:, 3] * tau, KK[:, 1] + KK[:, 4] * tau, KK[:, 2]] for tau in taus])
                if len(seg_blocked(rest[i], rest[i + 1], C)):
                    C_hit.append(C)
            if not C_hit:
                continue
            n += 1
            so.reroute_t = rt.t
            self.stats["reroutes"] = self.stats.get("reroutes", 0) + 1
            rt.emit("uav.keepout_guard", 1, uav=uav, target=None, d_m=None, clamped=False, action="reroute")
            if so.state == "TRANSIT":
                self.start_transit(so)
            elif so.state == "RETURNING":
                self.return_home(so)
            else:
                Cp = np.vstack([rt.circles(planning=True), *C_hit])
                if bool(inside(rest[-1:, :2], Cp)[0]):
                    continue                             # 终点被预测禁入：交给守卫与下一次复核
                R2 = plan_route(rest[:, :2], Cp)
                if R2 is None:
                    continue
                z = np.interp(np.r_[0.0, np.cumsum(np.hypot(*np.diff(R2, axis=0).T))],
                              np.r_[0.0, np.cumsum(np.hypot(*np.diff(rest[:, :2], axis=0).T))], rest[:, 2])
                self._path(so, np.c_[R2, z], speed=so.path_speed)     # 首点为当前位置：参考轨迹从机体处开始，避免位置误差 ELAND
        return n

    def work_start(self, so: SortieRT, it: ItemRT) -> np.ndarray:
        w = it.w
        if w.kind == "relay_station":
            st = self.rt.relays.station_for(so, it)
            if st is not None:
                return self.safe_goal(np.asarray(st, float), extra_m=2.0, C=self.rt.relays.station_circles())
        if w.path is not None and len(w.path):
            p0 = np.asarray(w.path[0], float)
            if self._relieving(so, it) is not None:
                # 接替持续性航线：会合点在航线起点上方 RELIEF_DZ_M（在岗机正沿航线飞行，可能就在起点附近）
                cap = float(self.rt.dtm(p0[None, :2])[0]) + H_CAP_AGL + 40.0
                return np.array([p0[0], p0[1], min(float(p0[2]) + RELIEF_DZ_M, max(cap, float(p0[2])))])
            return p0
        return self.safe_goal(np.asarray(w.pos if w.pos is not None else self.rt.pos(so.uav), float))

    def _relieving(self, so: SortieRT, it: ItemRT) -> SortieRT | None:
        """持续性航线工作项（非接力站位）的接替：被接替且仍在空中、仍持有该工作项的在岗机；否则 None。"""
        if not it.relief_for or it.relief_for == so.uav or it.w.kind == "relay_station" or it.w.path is None:
            return None
        old = self.rt.sorties.get(it.relief_for)
        if old is None or old.uav == so.uav or old.wid != it.w.wid or not old.airborne:
            return None
        return old

    def start_work(self, so: SortieRT) -> None:
        rt = self.rt
        _T, it = self.item_of(so)
        if it is None:
            self.release(so, "item_gone")
            return
        old = self._relieving(so, it)
        if old is not None and old.state in ("WORKING", "TRANSIT", "STANDBY", "EVADING"):
            # 接替会合（D2-FX-R2-tasking 第 2 次）：接替机悬停在航线起点上方，等在岗机水平离开 RELIEF_CLEAR_M（等待超过 RELIEF_WAIT_MAX_S 后只要求
            # RELIEF_MIN_M）再接手；在岗机随即返航（先原地爬升），接替机下降入航线，两机不在同一点相遇
            d = float(np.hypot(*(rt.pos(old.uav)[:2] - rt.pos(so.uav)[:2])))
            if so.relief_wait_t0 is None:
                so.relief_wait_t0 = rt.t
            need = RELIEF_CLEAR_M if rt.t - so.relief_wait_t0 < RELIEF_WAIT_MAX_S else RELIEF_MIN_M
            if d < need:
                if so.call_op != "hover" or so.action is not None:
                    so.action = None
                    if self._is_mr(so):
                        self.submit(so, "hover", {})
                so.retry_t, so.retry_op = rt.t + 1.0, "relief"
                return
        so.relief_wait_t0 = None
        so.state, so.t_state = "WORKING", rt.t
        if not (it.relief_for == so.uav and it.state in ("PENDING", "ASSIGNED")):
            it.state = "ACTIVE"                     # 等待接替的 A 恢复作业时不改工作项状态（接替的分配照常进行）
        if it.relief_for and it.w.kind != "relay_station" and it.relief_for != so.uav:
            # 接力的接替关系保留到交接完成（RelayController._promote 清除）。被接替的 A 自己重新开始作业（撤离后恢复）时不清除
            # relief_for（D2-FX-R2：此前 A 撤离后恢复即清除，接替机 B 到达时不再释放 A，两架同飞一条值守航线，在看洞点相撞）
            old = rt.sorties.get(it.relief_for)
            if old is not None and old.wid == it.w.wid and old.uav != so.uav:
                self.release(old, "relieved")
                rt.emit("task.reallocated", 0, tid=it.w.tid, wid=it.w.wid, from_uav=old.uav, to_uav=so.uav, reason="energy",
                        items=[{"wid": it.w.wid, "uav": so.uav}])
            it.relief_for = None
        if it.w.kind != "relay_station" and it.relief_for is None:
            # 同一工作项只允许一架作业：其余仍持有该工作项的架次（被接替者，或工作项被重新分配前的旧持有者）一律释放（D2-FX-R2：
            # sanfrancisco 中工作项在接替途中被重新分配，两架 P600 同飞一条值守航线，在看洞点相撞）
            for other in list(rt.sorties.values()):
                if other is not so and other.wid == it.w.wid and other.tid == so.tid and other.state in ("TRANSIT", "WORKING", "STANDBY", "EVADING"):
                    self.release(other, "relieved")
        w = it.w
        so.actions = []
        if w.path is not None and len(w.path) >= 2:
            self._queue_path(so, it, first=True)
        elif w.kind == "relay_station":
            rt.relays.on_station(so, it)
            self._station(so, it)
        else:
            self._station(so, it)
        self._next_action(so)

    @staticmethod
    def _ahead(P: Any, p: np.ndarray, length_m: float) -> np.ndarray:
        """航线 P 自离 p 最近的顶点起、前方 length_m 内每 5 m 的采样点（含 p）；P 为空时只有 p。"""
        if P is None or len(P) == 0:
            return p[None, :3]
        P = np.asarray(P, float).reshape(-1, 3)
        k = int(np.argmin(np.hypot(P[:, 0] - p[0], P[:, 1] - p[1])))
        pts, left, a = [p[:3]], float(length_m), p[:3]
        for b in P[k:]:
            seg = float(np.linalg.norm(b - a))
            if seg > 1e-6:
                n = max(1, int(min(seg, left) / 5.0))
                pts += [a + (b - a) * min(1.0, (i + 1) * 5.0 / seg) for i in range(n)]
                left -= seg
            a = b
            if left <= 0.0:
                break
        return np.asarray(pts, float)

    def _yield_mate(self, so: SortieRT, *, dz_m: float = YIELD_DZ_M) -> str | None:
        """编号小于本机、在近处（水平 < YIELD_R_M、高差 < dz_m）或其前方 YIELD_AHEAD_M 航线与本机续飞航线前方相交（采样点水平
        < YIELD_R_M、高差 < YIELD_DZ_M）的空中架次；没有时 None。"""
        rt = self.rt
        p = np.asarray(rt.pos(so.uav), float)
        mine = self._ahead((so.action or {}).get("P") if (so.action or {}).get("op") == "path"
                           else ((so.action or {}).get("resume") or {}).get("P"), p, YIELD_AHEAD_M)
        for o in sorted(rt.sorties.values(), key=lambda x: x.uav):
            if o is so or not o.airborne or o.uav >= so.uav or o.state in ("RECOVERING", "DROPPED"):
                continue
            q = np.asarray(rt.pos(o.uav), float)
            if float(np.hypot(q[0] - p[0], q[1] - p[1])) < YIELD_R_M and abs(float(q[2] - p[2])) < dz_m:
                return o.uav
            if o.path_cur is not None and len(mine) > 1:
                theirs = self._ahead(o.path_cur, q, YIELD_AHEAD_M)
                dh = np.hypot(mine[:, None, 0] - theirs[None, :, 0], mine[:, None, 1] - theirs[None, :, 1])
                dz = np.abs(mine[:, None, 2] - theirs[None, :, 2])
                if bool(((dh < YIELD_R_M) & (dz < YIELD_DZ_M)).any()):
                    return o.uav
        return None

    def _yield(self, so: SortieRT, t: float) -> bool:
        """间隔让行（M09 HOLD/SEPARATION）解除后、续飞作业航线之前：两机仍在近处时编号大者先原地升高（不可行时降低）YIELD_STEP_M
        让对方先走，对方离开后再续飞，最长 YIELD_MAX_S（D2-FX-R2-tasking 第 2 次，D2-AC-16：区域巡查与巡边的作业航线在同一高度交叉，
        让行解除后两机同时续飞、再次相遇，三轮后在 1.5 m 处的让行悬停中相撞）。True 表示继续等待。"""
        if not self._is_mr(so):
            return False
        if self.rt.fs(so.uav) == "HOLD":
            return True                                # M09 让行悬停尚未解除：避让指令会被 101 拒绝，等解除
        a = so.action or {}
        # 避让开始后按水平距离判定对方是否已离开（本机升高后高差已够，但续飞会回到原高度）
        mate = self._yield_mate(so, dz_m=YIELD_DZ_M if so.yield_t0 is None else YIELD_DZ_M + YIELD_STEP_M + 2.0)
        if mate is None and a.get("op") != "yield" and self._path_blocker(so) is not None \
                and (so.yield_t0 is None or t - so.yield_t0 < YIELD_PASS_WAIT_S):
            # 本机有先行权，但对方（避让方）仍停在本机前方航线上：等对方让开（升高或水平离开）再续飞，最长 YIELD_PASS_WAIT_S
            if so.yield_t0 is None:
                so.yield_t0 = t
            return True
        if mate is None or (so.yield_t0 is not None and t - so.yield_t0 >= YIELD_MAX_S):
            so.yield_t0 = None
            if a.get("op") == "yield":
                so.action = a.get("resume")
            return False
        if so.yield_t0 is None:
            so.yield_t0 = t
            goal = self._yield_goal(so, mate)
            if goal is not None:
                so.action = {"op": "yield", "resume": so.action}
                self.submit(so, "goto", {"pos": [float(x) for x in goal], "route": "direct"})
        return True

    def _path_blocker(self, so: SortieRT) -> str | None:
        """本机续飞航线前方 YIELD_AHEAD_M 内（水平 < YIELD_R_M、高差 < YIELD_DZ_M）停着或经过的其他空中架次；没有时 None。"""
        rt = self.rt
        p = np.asarray(rt.pos(so.uav), float)
        mine = self._ahead((so.action or {}).get("P") if (so.action or {}).get("op") == "path" else None, p, YIELD_AHEAD_M)
        for o in sorted(rt.sorties.values(), key=lambda x: x.uav):
            if o is so or not o.airborne or o.state in ("RECOVERING", "DROPPED"):
                continue
            q = np.asarray(rt.pos(o.uav), float)
            if bool(((np.hypot(mine[:, 0] - q[0], mine[:, 1] - q[1]) < YIELD_R_M) & (np.abs(mine[:, 2] - q[2]) < YIELD_DZ_M)).any()):
                return o.uav
        return None

    def _yield_goal(self, so: SortieRT, mate: str) -> np.ndarray | None:
        """避让点：原地升高 YIELD_STEP_M，不可行（高度上限、DSM 净空）时降低，再不可行时离开对方前方航线水平 YIELD_STEP_H_M
        （DSM 净空与世界边界复核）；都不可行时 None（原地等待）。D2-FX-R2-tasking 第 2 次：原地等待的一方正停在对方的航线上，对方续飞
        即再次相遇，第 3 次让行 M09 判 OSCILLATION 保持 HOLD。"""
        rt = self.rt
        p = np.asarray(rt.pos(so.uav), float)
        g, top = float(rt.dtm(p[None, :2])[0]), float(rt.dsm(p[None, :2])[0])
        for dz in (YIELD_STEP_M, -YIELD_STEP_M):
            z = float(p[2]) + dz
            if z - g <= H_CAP_AGL and z >= top + DSM_CLEAR_M:
                return np.array([p[0], p[1], z])
        o = rt.sorties.get(mate)
        q = np.asarray(rt.pos(mate), float)
        theirs = self._ahead(o.path_cur if o is not None else None, q, YIELD_AHEAD_M)
        k = int(np.argmin(np.hypot(theirs[:, 0] - p[0], theirs[:, 1] - p[1])))
        u = p[:2] - theirs[k, :2]
        if float(np.hypot(*u)) < 0.5:
            j = min(k + 1, len(theirs) - 1)
            dvec = theirs[j, :2] - theirs[max(j - 1, 0), :2]
            u = np.array([-dvec[1], dvec[0]]) if float(np.hypot(*dvec)) > 1e-6 else np.array([1.0, 0.0])
        u = u / max(float(np.hypot(*u)), 1e-9)
        zones = getattr(rt.world, "zones", None) if rt.world is not None else None
        for sgn in (1.0, -1.0):
            xy = p[:2] + sgn * u * YIELD_STEP_H_M
            if float(rt.dsm(xy[None])[0]) + DSM_CLEAR_M > float(p[2]):
                continue
            if zones is not None:
                with contextlib.suppress(Exception):
                    if float(np.asarray(zones.border_signed_distance(xy[None]), float)[0]) < MR_BORDER_INSET_M:
                        continue
            return np.array([xy[0], xy[1], p[2]])
        return None

    def _resume_action(self, so: SortieRT) -> None:
        """安全动作结束后续飞当前作业动作：航段从离当前位置最近的后续航点续飞（经接入段），站位重新下发，其余从作业起点重新开始。"""
        if (so.action or {}).get("op") == "yield":
            so.action = so.action.get("resume")
        so.yield_t0 = None
        a = dict(so.action or {})
        op = a.get("op")
        if op == "path" and a.get("P") is not None and len(a["P"]) >= 1:
            P = np.asarray(a["P"], float)
            p = self.rt.pos(so.uav)
            k = int(np.argmin(np.hypot(P[:, 0] - p[0], P[:, 1] - p[1])))
            if k + 1 < len(P) and float(np.hypot(*(P[k, :2] - p[:2]))) < 2.0:
                k += 1
            a["P"] = P[k:] if len(P) - k >= 1 else P[-1:]
            so.actions.insert(0, a)
            so.action = None
            self._next_action(so)
        elif op in ("hold", "loiter"):
            so.actions.insert(0, a)
            so.action = None
            self._next_action(so)
        elif op == "vmove":
            so.action = None                       # 垂直段未完成：其余航线在动作队列首位，重新接入（必要时再次垂直段）
            self._next_action(so)
        else:
            self.start_transit(so)

    def _queue_path(self, so: SortieRT, it: ItemRT, *, first: bool) -> None:
        w = it.w
        P = np.asarray(w.path, float)
        rep = str(w.needs.get("repeat") or ("loop" if w.closed else ""))
        if rep == "pingpong" and it.laps % 2 == 1:
            P = P[::-1]
        if w.closed and len(P) > 2:
            P = np.vstack([P, P[:1]])
        speed = float(w.needs.get("speed_mps") or 0.0) or None
        cuts = sorted(i for i in w.dwell if 0 <= i < len(P)) if (first or not (rep == "pingpong" and it.laps % 2 == 1)) else []
        looks = w.needs.get("looks") or {}
        k0 = 0
        for i in cuts:
            if i > k0:
                so.actions.append({"op": "path", "P": P[k0:i + 1], "speed": speed})
            act: dict[str, Any] = {"op": "hover", "s": float(w.dwell[i])}
            if str(i) in looks:
                act["look"] = {"target": looks[str(i)], "level": str(w.needs.get("level") or "R")}   # 看洞：云台指向与变焦
            so.actions.append(act)
            k0 = i
        if len(P) - 1 > k0:
            so.actions.append({"op": "path", "P": P[k0:], "speed": speed, "lap_end": True})
        elif not cuts:
            so.actions.append({"op": "lap", "lap_end": True})

    def _station(self, so: SortieRT, it: ItemRT) -> None:
        rt = self.rt
        m = rt.model(rt.model_id(so.uav))
        w = it.w
        if m is not None and m.frame != "multirotor":
            st = self.work_start(so, it)
            r = max(float(w.loiter_radius_m or m.r_loiter_min_m or 80.0), m.r_loiter_min_m)
            if m.frame == "fixed_wing" and m.r_loiter_w:
                # 站位高度的风下盘旋半径下限（M21 准入 110 LOITER_RADIUS_MIN 的口径，留 5% 余量；D2-FX-R2-tasking 第 2 次：19 m/s 风中
                # 按 150 m 下发被拒，工作项照常计时却没有盘旋指令在执行）
                g = float(rt.dtm(np.asarray(st[:2], float)[None])[0])
                r = max(r, FW_LOITER_R_MARGIN * m.r_loiter_at(rt.wind_mps(np.r_[st[:2], g], float(st[2]) - g)))
            center = st
            if w.kind == "relay_station":
                tp = rt.relays.target_pos(so)
                if tp is not None:
                    center = np.array([tp[0], tp[1], st[2]])
                    r = max(r, float(np.hypot(st[0] - tp[0], st[1] - tp[1])))
            so.loiter_r = r
            so.actions.append({"op": "loiter", "center": center, "r": r})
        else:
            so.actions.append({"op": "hold"})
        so.station = tuple(float(x) for x in self.work_start(so, it))  # type: ignore[assignment]
        if not w.persistent and w.kind not in ("relay_station",):
            so.actions.append({"op": "timer", "s": float(w.workload_s), "done": True})

    def _next_action(self, so: SortieRT) -> None:
        rt = self.rt
        if not so.actions:
            so.action = None
            T, it = self.item_of(so)
            if it is None:
                self.release(so, "item_gone")
                return
            w = it.w
            if w.path is not None and len(w.path) >= 2:
                it.laps += 1
                if w.persistent and T is not None and T.state == "RUNNING":
                    self._queue_path(so, it, first=False)
                    self._next_action(so)
                    return
                it.state = "DONE"
                self.release(so, "done")
                if T is not None:
                    rt.finish_if_done(T)
            return
        prev = so.action or {}
        a = so.actions.pop(0)
        so.action = a
        op = a["op"]
        if prev.get("look") is not None:
            self.release_pointing(so)                  # 看洞结束：云台交回巡查（M19 cue）
        if op == "path":
            P = self._connect(so, np.asarray(a["P"], float))
            p0 = rt.pos(so.uav)
            if self._is_mr(so) and len(P) >= 2 and float(np.hypot(*(P[0][:2] - p0[:2]))) < 1.0 and abs(float(P[0][2] - p0[2])) > 3.0:
                # 航线首段为原地垂直段（接入段的爬升、作业航线的垂直过渡）：先以 goto 单独完成，再飞其余航线（跟踪器会把首个垂直段
                # 并入斜线，D2-FX-R2 第 2.3 节）
                so.actions.insert(0, {**a, "P": P[1:]})
                so.action = {"op": "vmove"}
                m = rt.model(rt.model_id(so.uav))
                self.submit(so, "goto", {"pos": [float(x) for x in P[0]], "route": "direct",
                                         "speed_mps": float(min(m.v_cruise_mps if m is not None else 5.0, MAX_SPEED))})
                return
            self._path(so, P, speed=a.get("speed"))
        elif op == "hover":
            m = rt.model(rt.model_id(so.uav))
            if m is not None and m.frame != "multirotor":
                # 固定翼与垂起机的停留航点改为盘旋（垂起机的 hover 会转多旋翼并消耗 180 s 悬停上限，D2-INT S7）
                r = self.fw_loiter_r(so)
                p = self.fw_hold_center(so, rt.pos(so.uav), r, home=False)
                a["center"], a["r"] = p.copy(), r      # 守卫按盘旋圈预测轨迹（此前取直线外推 5 + 2·R/v 秒，看洞盘旋起步即误判撤离）
                self.submit(so, "loiter", {"center": [float(p[0]), float(p[1]), float(p[2])], "radius_m": r, "dir": "cw"})
            else:
                yaw = self._yaw_to_target(so, (a.get("look") or {}).get("target"))
                if yaw is not None:
                    # 看洞停留：机头转向洞内识别物（云台方位限位 ±160°；D2-FX-R2-tasking 第 2 次：shenzhen、chicago 的 T5 首个看洞点以起飞时的机头朝向
                    # 悬停，识别物正在机尾方向，15 s 停留中没有任何检测，首次捕获推迟到下一个看洞点 92–100 s）
                    self.yaw_goto(so, rt.pos(so.uav), yaw)
                else:
                    self.submit(so, "hover", {})
            so.hover_until = rt.t + float(a["s"])
            if a.get("look") is not None:
                self._look(so, a["look"])
        elif op in ("hold", "loiter"):
            if op == "hold":
                yaw = self._yaw_to_look(so)
                if yaw is not None:
                    # D2-INT：多旋翼到站后机头转向盯防点（云台方位限位约 ±160°；FX-R2 起短距离转场为直飞 goto，机头保持起飞时的
                    # 朝向，识别物在机身后方时云台够不到，shenzhen 与 chicago 的 T1 定点盯防 180 s 内无捕获）
                    self.yaw_goto(so, rt.pos(so.uav), yaw)
                else:
                    self.submit(so, "hover", {})
            else:
                c = a["center"]
                self.submit(so, "loiter", {"center": [float(c[0]), float(c[1]), float(c[2])], "radius_m": float(a["r"]), "dir": "cw"})
            so.hover_until = None
            if so.actions and so.actions[0]["op"] == "timer":
                tm = so.actions.pop(0)        # 驻留计时与悬停（盘旋）同时进行
                so.hover_until = rt.t + float(tm["s"])
                a["done"] = bool(tm.get("done"))
            self._point(so)
        elif op == "timer":
            so.hover_until = rt.t + float(a["s"])
        elif op == "lap":
            self._next_action(so)

    def _yaw_to_target(self, so: SortieRT, tgt: str | None) -> float | None:
        """多旋翼朝识别物 tgt 当前位置的航向（ENU，弧度）；固定翼、识别物不存在或距离 < 5 m 时 None。"""
        rt = self.rt
        if not tgt or not self._is_mr(so):
            return None
        trt = rt.targets_rt()
        st = trt.state1(tgt) if trt is not None else None
        if st is None:
            return None
        p = rt.pos(so.uav)
        dx, dy = float(st.pos[0]) - float(p[0]), float(st.pos[1]) - float(p[1])
        return math.atan2(dy, dx) if math.hypot(dx, dy) >= 5.0 else None

    def _yaw_to_look(self, so: SortieRT) -> float | None:
        """多旋翼定点（非接力）作业的盯防点方位（ENU 偏航，弧度）；没有盯防点、固定翼或距离 < 5 m 时 None。"""
        rt = self.rt
        m = rt.model(rt.model_id(so.uav))
        if m is not None and m.frame != "multirotor":
            return None
        _T, it = self.item_of(so)
        if it is None:
            return None
        look = None
        if it.w.kind == "relay_station":
            # 接力站位同样转机头朝识别物（D2-FX-R2-tasking 第 2 次：云台方位限位 ±160°，第 1 轮 (c) 接替机到站时机头背对识别物，15 min 无捕获，
            # 同一站位的下一架到站 60 s 内即捕获）
            tp = rt.relays.target_pos(so)
            look = None if tp is None else np.asarray(tp, float)
        elif it.w.look_at is not None:
            look = np.asarray(it.w.look_at, float)
        elif it.w.target:
            trt = rt.targets_rt()
            st = trt.state1(it.w.target) if trt is not None else None
            look = None if st is None else np.asarray(st.pos, float)
        if look is None or not np.all(np.isfinite(look[:2])):
            return None
        p = rt.pos(so.uav)
        dx, dy = float(look[0] - p[0]), float(look[1] - p[1])
        if math.hypot(dx, dy) < 5.0:
            return None
        return math.atan2(dy, dx)

    def _point(self, so: SortieRT) -> None:
        """云台指向：接力为识别物航迹（M19），定点为 look_at 点；M13 运行时直接锁存（tasking 源），否则发 `gimbal` 命令。"""
        rt = self.rt
        _T, it = self.item_of(so)
        look = None
        if it is not None and it.w.kind == "relay_station":
            tp = rt.relays.target_pos(so)
            look = None if tp is None else tuple(float(x) for x in tp)
        elif it is not None and it.w.look_at is not None:
            look = tuple(float(x) for x in it.w.look_at)
        elif it is not None and it.w.target:
            trt = rt.targets_rt()
            st = trt.state1(it.w.target) if trt is not None else None
            look = None if st is None else tuple(float(x) for x in st.pos)
        if look is None:
            return
        so.look = look
        srt = rt.sensor_rt()
        if it is not None and it.w.kind != "relay_station" and it.w.target:
            self._zoom_for_target(so, it)  # D2-INT：定点盯防按所需等级变焦（P600 2.0.0 沙盒实例只有 GX40 吊舱，广角端达不到 R 级）
        if srt is not None and hasattr(srt, "set_tasking_pointing"):
            try:
                srt.set_tasking_pointing(so.slot, None, list(look))
                return
            except Exception:
                pass
        so.n_calls += 1
        if rt.cmd is not None:
            with contextlib.suppress(Exception):
                rt.cmd.submit_internal({"cid": f"m20g:{so.uav}:{so.sortie_id}:{so.n_calls}", "op": "gimbal", "uav": so.uav,
                                        "args": {"mode": "look_at", "look_at_enu_m": list(look)}}, self.principal(so.tid))

    def _look(self, so: SortieRT, look: dict) -> None:
        """看洞停留（M20 §6.4 第 4 条）：云台指向洞内识别物的当前位置（禁入体所属识别物，场景情报），按所需等级变焦（M19
        `feasible_view`，光学变焦）；停留结束由下一动作释放指向（巡查的 M19 cue 照常）。D2-FX-R2：此前看洞点只悬停，云台保持巡查
        姿态与广角，第 1 轮 T5 区域值守全程只到 D 级（limiting pixels）。"""
        rt = self.rt
        tgt = look.get("target")
        trt = rt.targets_rt()
        st = trt.state1(tgt) if trt is not None and tgt else None
        if st is None:
            return
        pt = tuple(float(x) for x in st.pos)
        so.look = pt
        srt = rt.sensor_rt()
        if srt is not None and hasattr(srt, "set_tasking_pointing"):
            with contextlib.suppress(Exception):
                srt.set_tasking_pointing(so.slot, None, list(pt))
        prt = rt.perception_rt()
        if prt is not None:
            with contextlib.suppress(Exception):
                prt.set_requirement(so.uav, str(tgt), str(look.get("level") or "R"), None)
        if srt is None or not hasattr(srt, "set_zoom"):
            return
        try:
            from awr.sim.perception.feasibility import env_for, feasible_view

            from .models import station_specs

            band, mor = rt.light()
            t = trt.config(tgt)
            for name, sn in station_specs(rt.T, rt.model_id(so.uav) or "", band):
                fmin, fmax = float(getattr(sn, "f_min_mm", 0.0) or 0.0), float(getattr(sn, "f_max_mm", 0.0) or 0.0)
                if not (fmax > fmin * 1.01 > 0):
                    continue
                fv = feasible_view(rt.pos(so.uav)[None, :], t, st, sn, env_for(band, mor), str(look.get("level") or "R"))
                f = float(fv.f_mm[0])
                if math.isfinite(f) and f > 0:
                    srt.set_zoom(so.slot, name, f)
                    return
        except Exception:
            return

    def _zoom_for_target(self, so: SortieRT, it: Any) -> None:
        """有光学变焦的成像传感器：按 M19 `feasible_view` 给出满足所需等级的焦距（与接力 `RelayController._zoom` 同一口径）。"""
        rt = self.rt
        srt = rt.sensor_rt()
        if srt is None or not hasattr(srt, "set_zoom"):
            return
        try:
            from awr.sim.perception.feasibility import env_for, feasible_view

            from .models import station_specs

            T = rt.tasks.get(it.w.tid)
            lvl = str((T.params.get("level_req") if T is not None else None) or "R")
            band, mor = rt.light()
            trt = rt.targets_rt()
            t, ts = trt.config(it.w.target), trt.state1(it.w.target)
            if t is None or ts is None:
                return
            for name, sn in station_specs(rt.T, rt.model_id(so.uav) or "", band):
                fmin, fmax = float(getattr(sn, "f_min_mm", 0.0) or 0.0), float(getattr(sn, "f_max_mm", 0.0) or 0.0)
                if not (fmax > fmin * 1.01 > 0):   # 只对光学变焦
                    continue
                fv = feasible_view(rt.pos(so.uav)[None, :], t, ts, sn, env_for(band, mor), lvl)
                f = float(fv.f_mm[0])
                if math.isfinite(f) and f > 0:
                    srt.set_zoom(so.slot, name, f)
                    return
        except Exception:
            return

    def release_pointing(self, so: SortieRT) -> None:
        srt = self.rt.sensor_rt()
        if srt is not None and hasattr(srt, "set_tasking_pointing"):
            with contextlib.suppress(Exception):
                srt.set_tasking_pointing(so.slot, None, None)

    # ------------------------------------------------------------ 释放、待命、返航
    def release(self, so: SortieRT, why: str) -> None:
        """工作项结束或被释放：持续性任务中被接替的机体与取消、低电者返航，其余进入 STANDBY。"""
        rt = self.rt
        so.released = True
        self.release_pointing(so)
        prt = rt.perception_rt()
        if prt is not None:
            with contextlib.suppress(Exception):
                prt.set_cue(so.uav, None)
        if so.state in ("ASSIGNED",):
            n = rt.nests.get(so.nest_id or "")
            if n is not None:
                n.pad.cancel(so.uav, "launch")
                n.launch_queue = [q for q in n.launch_queue if q.get("uav") != so.uav]
            rt.sorties.pop(so.uav, None)
            return
        if not so.airborne:
            return
        if why in ("cancel", "relieved", "energy", "dormant") or self._low_energy(so):
            self.return_home(so)
        else:
            self.standby(so)

    def standby(self, so: SortieRT, *, hold: bool = False) -> None:
        rt = self.rt
        so.state, so.t_state = "STANDBY", rt.t
        so.standby_since = rt.t
        so.actions, so.action, so.hover_until = [], None, None
        if hold:
            so.released = False
        m = rt.model(rt.model_id(so.uav))
        if m is not None and m.frame != "multirotor":
            r = max(self.fw_loiter_r(so), 100.0)
            p = self.fw_hold_center(so, rt.pos(so.uav), r)
            so.action = {"op": "standby", "center": p.copy(), "r": r}
            self.submit(so, "loiter", {"center": [float(p[0]), float(p[1]), None], "radius_m": r, "dir": "cw"})
        else:
            self.submit(so, "hover", {})

    def resume_work(self, so: SortieRT) -> None:
        so.released = False
        so.standby_since = None
        self.start_transit(so)

    def return_home(self, so: SortieRT) -> None:
        rt = self.rt
        n = rt.nests.get(so.nest_id or "")
        e = rt.entry(so.uav)
        home = np.asarray(getattr(e, "home_enu_m", None) if e is not None and getattr(e, "home_enu_m", None) is not None
                          else (n.pos3 if n is not None else rt.pos(so.uav)), float)
        z_above = float(rt.dsm(home[None, :2])[0]) + 30.0 + self.layer_m(so)
        m0 = rt.model(rt.model_id(so.uav))
        if so.force_home:
            so.state, so.t_state = "RETURNING", rt.t
            so.retry_t = None
            self.submit(so, "goto", {"pos": [float(home[0]), float(home[1]), z_above], "route": "direct"})
            return
        # 固定翼与垂起机按走廊高度下降返航；多旋翼保持不低于当前高度（D2-FX-R2：交接后 A 若下降返航，会在比它低 10 m 的接替机
        # 高度上穿过其侧向 15 m 的站位，T5 变体诊断中 COLLISION_UAV）
        P = self.route(rt.pos(so.uav), np.array([home[0], home[1], z_above]), layer_m=self.layer_m(so), fw_margin_m=self.fw_margin_m(so),
                       descend=m0 is not None and m0.frame != "multirotor", uav=so.uav)
        so.state, so.t_state = "RETURNING", rt.t
        so.retry_t = None
        so.actions, so.action, so.hover_until = [], None, None
        self.release_pointing(so)
        if n is not None:
            n.recover_queue = [r for r in n.recover_queue if r.get("uav") != so.uav] + [{"uav": so.uav, "eta_s": None}]
        p0 = rt.pos(so.uav)
        m = rt.model(rt.model_id(so.uav))
        if len(P) == 0:
            if float(np.hypot(p0[0] - home[0], p0[1] - home[1])) > HOME_NEAR_M:
                so.action = {"op": "hold_home"}
                if m is None or m.frame == "multirotor":
                    self.submit(so, "hover", {})
                so.retry_t, so.retry_op = rt.t + HOME_WAIT_S, "return"
                return
            self._land(so)
            return
        if (m is None or m.frame == "multirotor") and float(np.hypot(P[0][0] - p0[0], P[0][1] - p0[1])) < 0.5 and float(P[0][2] - p0[2]) > 2.0:
            # 先单独垂直爬升到转场层再水平返航（D2-FX-R2：follow_path 的首个垂直航段被跟踪器并入斜线，刚交接的 A 平飞穿过
            # 15 m 外同高度的 B，第 1 轮 (b) 与本轮诊断各有一次 COLLISION_UAV）
            so.ret_climb = True
            side = self._sidestep(so, float(P[0][2]))
            if side is not None:
                self.submit(so, "goto", {"pos": [float(x) for x in side], "route": "direct"})
                return
            self.submit(so, "goto", {"pos": [float(p0[0]), float(p0[1]), float(P[0][2])], "route": "direct",
                                     "speed_mps": float(min(m.v_cruise_mps if m else 8.0, MAX_SPEED))})
            return
        so.ret_climb = False
        self._fly(so, P)

    def evade_pad(self, uav: str | None) -> float:
        """该架次最近 EVADE_WIN_S 内撤离 n 次时规划圆的外扩量 min(EVADE_PAD_M·(n − 1), EVADE_PAD_MAX_M)。"""
        so = self.rt.sorties.get(uav or "")
        if so is None:
            return 0.0
        n = sum(1 for x in so.evade_ts if self.rt.t - x <= EVADE_WIN_S)
        return float(min(EVADE_PAD_M * max(n - 1, 0), EVADE_PAD_MAX_M))

    def _route_circles(self, Cb: np.ndarray, Tc: np.ndarray, fw_margin_m: float, ends: tuple, *, tall_m: float = TALL_TURN_M
                       ) -> np.ndarray:
        """规划用圆：禁入圆（固定翼外扩转弯半径；包含端点的圆只外扩到端点以内 1 m）+ 高楼障碍圆（加 tall_m，固定翼缺省
        TALL_TURN_M、多旋翼 TALL_MR_M；包含端点的不参与，端点高度已按其处 DSM 净空给出）。"""
        C = Cb.copy()
        if fw_margin_m > 0.0 and len(C):
            r_inf = C[:, 2] + float(fw_margin_m)
            for e in ends:
                d = np.hypot(C[:, 0] - e[0], C[:, 1] - e[1])
                r_inf = np.where(d < r_inf, np.maximum(C[:, 2], d - 1.0), r_inf)
            C[:, 2] = r_inf
        if len(Tc):
            T = Tc.copy()
            T[:, 2] += float(tall_m)
            for e in ends:
                T = T[np.hypot(T[:, 0] - e[0], T[:, 1] - e[1]) > T[:, 2]]
            C = np.vstack([C, T]) if len(C) else T
        return C

    def _seg_clear_of_uavs(self, a: np.ndarray, b: np.ndarray, uav: str | None) -> bool:
        """a → b 的直飞线段离其他空中 M20 机体当前位置 ≥ UAV_SEP_M（三维）。D2-FX-R2：交接后 A 以直飞捷径斜降返航，穿过侧向 15 m、
        低 10 m 处的接替机 B（T5 变体诊断中 COLLISION_UAV）；不满足时走转场剖面（先爬升，再水平）。"""
        if uav is None:
            return True
        rt = self.rt
        d = b - a
        L2 = float(d @ d)
        for u, so in rt.sorties.items():
            if u == uav or not so.airborne:
                continue
            q = np.asarray(rt.pos(u), float)
            t = 0.0 if L2 < 1e-9 else float(np.clip((q - a) @ d / L2, 0.0, 1.0))
            if float(np.linalg.norm(a + t * d - q)) < UAV_SEP_M:
                return False
        return True

    def _border_node_ok(self, inset_m: float):
        """绕行顶点在世界边界内缩 inset_m 以内（固定翼：顶点处转弯不越界）；没有边界数据时 None。"""
        zones = getattr(self.rt.world, "zones", None) if self.rt.world is not None else None
        if zones is None or not hasattr(zones, "border_signed_distance"):
            return None

        def ok(xy: np.ndarray) -> np.ndarray:
            try:
                return np.asarray(zones.border_signed_distance(np.asarray(xy, float)[:, :2]), float) >= inset_m
            except Exception:
                return np.ones(len(xy), bool)
        return ok

    def _is_mr(self, so: SortieRT) -> bool:
        m = self.rt.model(self.rt.model_id(so.uav))
        return m is None or m.frame == "multirotor"

    def tall_circles(self) -> np.ndarray:
        """固定翼航线的高楼障碍圆（N×3）：顶部 + DSM_CLEAR_M 高于固定翼巡航上限（DTM + FW_CEIL_AGL_M）的建筑，10 m 网格上
        按 TALL_TILE_M 分块取外接圆（再加 5 m）。转场高度只能越过上限以内的楼，更高的楼须水平绕行（D2-FX-R2：S7 中垂起机向北转场
        进入 200–350 m 的高楼区，撞上 207 m 楼顶）。按机巢与任务区域外扩 1500 m 的范围计算一次并缓存（世界静态）。"""
        if self._tall is not None:
            return self._tall
        rt = self.rt
        pts = [n.pos3[:2] for n in rt.nests.values()]
        if rt.world is None or not pts:
            self._tall = np.zeros((0, 3))
            return self._tall
        lo = np.min(np.asarray(pts, float), axis=0) - TALL_RANGE_M
        hi = np.max(np.asarray(pts, float), axis=0) + TALL_RANGE_M
        xs = np.arange(lo[0], hi[0] + TALL_GRID_M, TALL_GRID_M)
        ys = np.arange(lo[1], hi[1] + TALL_GRID_M, TALL_GRID_M)
        X, Y = np.meshgrid(xs, ys)
        Q = np.c_[X.ravel(), Y.ravel()]
        try:
            dsm = rt.dsm(Q).reshape(X.shape)
            dtm = rt.dtm(Q).reshape(X.shape)
        except Exception:
            self._tall = np.zeros((0, 3))
            return self._tall
        mask = dsm + DSM_CLEAR_M > dtm + FW_CEIL_AGL_M
        out = []
        if mask.any():
            # 按 TALL_TILE_M 分块取外接圆（连通域整体的外接圆在高楼区可达数百米并常把起点包含在内，D2-FX-R2 诊断）
            ii = np.flatnonzero(mask.ravel())
            T = np.floor((Q[ii] - lo[None, :]) / TALL_TILE_M).astype(np.int64)
            key = T[:, 0] * 1_000_003 + T[:, 1]
            for kk in np.unique(key):
                sel = ii[key == kk]
                c = Q[sel].mean(axis=0)
                r = float(np.hypot(Q[sel, 0] - c[0], Q[sel, 1] - c[1]).max()) + 0.75 * TALL_GRID_M + 5.0
                out.append([float(c[0]), float(c[1]), r])
        self._tall = np.asarray(out, float).reshape(-1, 3)
        return self._tall

    def _connect(self, so: SortieRT, P: np.ndarray) -> np.ndarray:
        """作业航段的接入段：当前位置到首个航点的直线走廊（±5 m）DSM 净空不足 15 m 时，先在当前位置上方与首点上方以安全高度接入
        （固定翼盘旋或撤离后离计划航线较远，接入段此前不复核，细校验 102 后三次失败即返航）。"""
        p0 = self.rt.pos(so.uav)
        if len(P) == 0:
            return P
        a2, b2 = p0[:2], P[0][:2]
        L = float(np.hypot(*(b2 - a2)))
        if L < 5.0:
            return P
        n = max(2, int(L / 5.0) + 1)
        u = (b2 - a2) / L
        nrm = np.array([-u[1], u[0]])
        pts = np.vstack([a2 + (b2 - a2) * t + nrm * o for t in np.linspace(0.0, 1.0, n) for o in (-5.0, 0.0, 5.0)])
        z_need = float(self.rt.dsm(pts).max()) + DSM_CLEAR_M
        if z_need <= min(float(p0[2]), float(P[0][2])):
            return P
        z_c = max(z_need, float(p0[2]), float(P[0][2]))
        return np.vstack([[float(p0[0]), float(p0[1]), z_c], [float(P[0][0]), float(P[0][1]), z_c], P])

    def _fly(self, so: SortieRT, P: np.ndarray) -> dict:
        """单个目标点（直飞捷径）用 goto（D2-FX-R2：两点 follow_path 的首点为当前位置时，悬停中下发会以位置误差 ELAND），其余 follow_path。"""
        P = np.asarray(P, float).reshape(-1, 3)
        if len(P) == 1:
            m = self.rt.model(self.rt.model_id(so.uav))
            if m is None or m.frame == "multirotor":
                so.path_cur = None
                v = float(min(m.v_cruise_mps if m else 8.0, MAX_SPEED))
                p0 = self.rt.pos(so.uav)
                dxy, dz = float(np.hypot(*(P[0][:2] - p0[:2]))), float(P[0][2] - p0[2])
                if abs(dz) > 3.0 and dxy > 1.0:
                    # 斜升（降）直飞：水平速度按垂直速率限制，使水平与垂直同时到达、沿直线（direct_ok 的剖面复核以直线为准）
                    vz = float(m.climb_mps if m is not None else 3.0) if dz > 0 else VZ_DN_MAX_MPS
                    v = max(1.0, min(v, vz * dxy / abs(dz)))
                return self.submit(so, "goto", {"pos": [float(x) for x in P[0]], "route": "direct", "speed_mps": v})
        return self._path(so, P)

    def _land(self, so: SortieRT) -> None:
        """到达机巢上方：按起降位时间线（M20-FR-003，相邻事件 ≥ launch_interval_s，回收优先）登记回收；轮到时才 land，之前在
        上方悬停（固定翼盘旋）等待（D2-FX-R2：此前登记后立即降落，相邻槽位可能同时下降）。"""
        rt = self.rt
        n = rt.nests.get(so.nest_id or "")
        t0 = rt.t
        if n is not None and so.land_wait_t is None:
            t0 = n.pad.book(rt.t, "recover", so.uav, recover=True)
            if t0 > rt.t + 0.5:
                so.land_wait_t = t0
                so.state, so.t_state = "RETURNING", rt.t
                so.action = {"op": "land_wait"}
                m = rt.model(rt.model_id(so.uav))
                if m is not None and m.frame != "multirotor":
                    p = rt.pos(so.uav)
                    r = self.fw_loiter_r(so)
                    so.action.update(center=p.copy(), r=r)
                    self.submit(so, "loiter", {"center": [float(p[0]), float(p[1]), float(p[2])], "radius_m": r, "dir": "cw"})
                else:
                    self.submit(so, "hover", {})
                return
        so.land_wait_t = None
        so.state, so.t_state = "RECOVERING", rt.t
        self.submit(so, "land", {})

    def _low_energy(self, so: SortieRT) -> bool:
        rt = self.rt
        m = rt.model(rt.model_id(so.uav))
        if m is None:
            return False
        return self.t_end(so, m) - rt.t < 120.0

    def e_back_wh(self, so: SortieRT, m: Any) -> float:
        rt = self.rt
        n = rt.nests.get(so.nest_id or "")
        p = rt.pos(so.uav)
        home = np.asarray(n.pos3 if n is not None else p, float)
        from awr.tasking.keepout import detour_length

        L = detour_length(p[:2], home[:2], rt.circles(planning=True))
        h = max(float(p[2] - home[2]), 0.0)
        return (m.p_role("cruise") * L / max(m.v_cruise_mps, 0.1) + m.p_role("hover") * h / 1.5) / 3600.0

    def t_end(self, so: SortieRT, m: Any) -> float:
        """预测服务结束时刻 `now + (E_avail − E_back − 0.2·E_use)/P_role`（M20-FR-043）。"""
        rt = self.rt
        _T, it = self.item_of(so)
        role = it.w.role if it is not None else "hover"
        if role == "hover" and not m.hovers:
            role = "loiter"
        e_av = rt.soc(so.uav) * m.e_use_wh
        p = max(m.p_role(role), 1.0)
        return rt.t + (e_av - self.e_back_wh(so, m) - RESERVE * m.e_use_wh) * 3600.0 / p

    def eta_relief(self, so: SortieRT, it: ItemRT) -> float:
        """接替候选的最小 ETA（同巢就绪时刻 + 爬升 + 转场）。"""
        rt = self.rt
        best = math.inf
        target = self.work_start(so, it)
        for n in rt.nests.values():
            if n.cfg.get("crewed") and it.w.persistent:
                continue
            for uav in n.air:
                if uav == so.uav or uav in rt.sorties:
                    continue
                m = rt.model(rt.model_id(uav))
                if m is None:
                    continue
                tr = n.t_ready(uav, rt.t)
                if not math.isfinite(tr):
                    continue
                d = float(np.hypot(target[0] - n.pos3[0], target[1] - n.pos3[1]))
                Cp = rt.circles(planning=True)
                if len(Cp):
                    # 绕行长度（禁入圆外的切线-圆弧路径），不是直线距离（D2-FX-R2：S7 中接替机绕开车辆与人的禁入圆，实际到站比 ETA
                    # 晚约 1 min，在岗机到 t_end 时只同时捕获了 6 s）
                    from awr.tasking.keepout import detour_length

                    with contextlib.suppress(Exception):
                        d = max(d, float(detour_length(np.asarray(n.pos3[:2], float), np.asarray(target[:2], float), Cp)))
                    if bool(inside(np.asarray(n.pos3[:2], float)[None], Cp)[0]):
                        d += NEST_BLOCKED_ETA_S * max(m.v_cruise_mps, 0.1)   # 机巢在运动识别物禁入圆内：起飞即撤离的耗时
                # 转场按执行器的航线剖面：爬升到转场层（DTM + 60 m + 高度层）→ 平飞 → 在站位上方下降（多旋翼 1.5 m/s）
                z_tr = max(float(target[2]), float(n.pos3[2]) + H_TRANSIT_AGL + LAYER_BASE_M + LAYER_STEP_M * 3)
                desc = max(z_tr - float(target[2]), 0.0) / 1.5 if m.frame == "multirotor" else 0.0
                eta = max(tr - rt.t, 0.0) + n.cfg["launch_interval_s"] + max(z_tr - float(n.pos3[2]), 0.0) / max(m.climb_mps, 0.5) \
                    + d / max(m.v_cruise_mps, 0.1) + desc
                # 剖面估算之外的加减速、转弯、到站后指向与变焦：按 ETA_SCALE 放大并加 ETA_PAD_S（D2-FX-R2：S7 4 h 中两次接替预测 112 s、
                # 实到 175 s，在岗机到 t_end 时同时捕获只有 10 s）
                best = min(best, ETA_SCALE * eta + ETA_PAD_S)
        return best

    # ------------------------------------------------------------ 结果
    def pump(self) -> None:
        rt = self.rt
        if not rt.results:
            return
        res, rt.results = rt.results, []
        for cid, status, code, detail in res:
            uav = self.by_cid.pop(cid, None)
            so = rt.sorties.get(uav or "")
            if so is None or so.cid != cid:
                continue
            if status in ("accepted", "running"):
                continue
            self.on_result(so, status, code, detail)

    def on_result(self, so: SortieRT, status: str, code: int, detail: Any) -> None:
        rt = self.rt
        ok = status == "succeeded"
        op = so.call_op
        if so.state == "LAUNCHING" and op == "takeoff":
            if ok:
                self.start_transit(so)
            else:
                so.fail_n += 1
                if so.fail_n >= 3:
                    self._drop(so, f"takeoff:{code}")
                else:
                    so.state, so.t_launch = "ASSIGNED", rt.t + 5.0
            return
        if so.state == "TRANSIT" and op in ("follow_path", "goto"):
            if ok and op == "goto" and so.climb_first:
                so.climb_first = False
                self.start_transit(so)                # 爬升完成：水平转场
                return
            if ok:
                self.start_work(so)
            elif status != "canceled" and int(code or 0) in SAFETY_ACTIVE_CODES:
                so.retry_t, so.retry_op = rt.t + RETRY_S, "transit"     # 让行悬停等安全动作：稍后重试，不改走越障剖面
            elif op == "follow_path" and status in ("failed", "rejected", "timeout") and self._is_mr(so):
                _T, it = self.item_of(so)
                goal = self.work_start(so, it) if it is not None else rt.pos(so.uav)
                self.submit(so, "goto", {"pos": [float(x) for x in goal], "route": "auto"})
            elif op == "follow_path" and status in ("failed", "rejected", "timeout") and so.fail_n < 3:
                # 固定翼与垂起机：不退回 M10 的多旋翼 goto 自动航线（垂起机会转多旋翼），稍后从当前位置重新规划转场
                so.fail_n += 1
                so.retry_t, so.retry_op = rt.t + RETRY_S, "transit"
            elif status != "canceled":
                self._fail_work(so, f"transit:{code}")
            return
        if so.state == "WORKING":
            if ok and op == "goto" and so.yaw_goal is not None and self._yaw_continue(so):
                return                                  # 分步转向未完成：续转
            a = so.action or {}
            if a.get("op") == "yield" and op == "goto" and not ok and status in ("failed", "rejected", "timeout"):
                so.action, so.yield_t0 = a.get("resume"), None      # 避让指令被拒（仍在让行悬停等）：下次重试重新避让
                so.retry_t, so.retry_op = rt.t + RETRY_S, "work"
                return
            if a.get("op") == "path" and a.get("search") and not ok and status in ("failed", "rejected", "timeout"):
                # 接力搜索航段被拒（越界、禁飞区或穿越建筑）：就地悬停，改为重新选站，不计作业失败（D2-FX-R2：第 1 轮三次被拒即返航，
                # 新机到站后立即重复，形成"派遣—返航"空转）
                so.action = {"op": "hold"}
                self.submit(so, "hover", {})
                rt.relays.search_failed(so)
                return
            if not ok and status in ("failed", "rejected", "timeout") and int(code or 0) in SAFETY_ACTIVE_CODES \
                    and a.get("op") in ("path", "hold", "loiter", "vmove"):
                # 安全动作（间隔让行、地理围栏修正）抢占或拒绝：稍后从当前位置续飞当前动作，不计作业失败（D2-FX-R2-tasking 第 2 次：此前三次 101
                # 即放弃工作项返航，让行解除后两机都已在返航）
                so.retry_t, so.retry_op = rt.t + RETRY_S, "work"
                return
            if a.get("op") == "loiter" and status == "rejected" and int(code or 0) == CODE_FW_PARAM and isinstance(detail, dict) \
                    and detail.get("min_m") is not None and so.fail_n < 3:
                # 盘旋半径低于当前风下的准入下限（110 LOITER_RADIUS_MIN）：按回复的下限放大后重发
                so.fail_n += 1
                a["r"] = max(float(a.get("r") or 0.0), float(detail["min_m"]) * FW_LOITER_R_MARGIN)
                so.loiter_r = float(a["r"])
                c = a["center"]
                self.submit(so, "loiter", {"center": [float(c[0]), float(c[1]), float(c[2])], "radius_m": float(a["r"]), "dir": "cw"})
                return
            if a.get("op") == "vmove":
                if ok:
                    self._next_action(so)
                elif status in ("failed", "rejected", "timeout"):
                    so.fail_n += 1
                    if so.fail_n >= 3:
                        self._fail_work(so, f"work:{code}")
                    else:
                        so.action = None
                        self._next_action(so)
            elif a.get("op") == "path":
                if ok:
                    self._next_action(so)
                elif status in ("failed", "rejected", "timeout"):
                    so.fail_n += 1
                    if so.fail_n >= 3:
                        self._fail_work(so, f"work:{code}")
                    else:
                        self._next_action(so)
            elif a.get("op") in ("hover",) and not ok and status in ("failed", "rejected"):
                so.hover_until = rt.t
            return
        if so.state == "EVADING" and op in ("goto", "follow_path"):
            self.rt.guard.evaded(so, ok)
            return
        if so.state == "RETURNING" and op in ("follow_path", "goto"):
            if ok and op == "goto" and so.ret_climb:
                so.ret_climb = False
                self.return_home(so)                  # 爬升完成：水平返航
                return
            if ok:
                home = self._home_xy(so)
                p = rt.pos(so.uav)
                m = rt.model(rt.model_id(so.uav))
                if m is not None and m.frame == "vtol" and op == "follow_path" and float(np.hypot(p[0] - home[0], p[1] - home[1])) > 3.0:
                    # 垂起机：固定翼航段在接受半径（约 40 m）外即完成，转多旋翼后再 goto 到起降位正上方，之后才降落（此前直接 land，
                    # 过渡减速段落在机巢外 60 m）
                    z = float(rt.dsm(np.asarray(home)[None, :2])[0]) + 30.0
                    self.submit(so, "goto", {"pos": [float(home[0]), float(home[1]), max(min(float(p[2]), z + 20.0), z)], "route": "direct"})
                    return
                if float(np.hypot(p[0] - home[0], p[1] - home[1])) > HOME_NEAR_M:
                    # 航线止于机巢外（机巢落在运动识别物的规划禁入圆内时，绕行规划把终点移到圆外）：原地等待后重试返航，不就地降落
                    # （D2-FX-R2：S7 中车辆驶近机巢箱 A，P600 降落在机巢外 55 m 处，此后无法换电）
                    if so.home_wait_t0 is None:
                        so.home_wait_t0 = rt.t
                    if (m is None or m.frame == "multirotor") and not so.force_home \
                            and (rt.t - so.home_wait_t0 >= HOME_WAIT_MAX_S or self._low_energy(so)):
                        # 等待超时或能量临界（识别物停在机巢附近）：保持当前高度直飞机巢上方再降落，守卫对该架次让步（此前一直等到
                        # M09 能量 RTL 以 SOC 0.08 直线返航）
                        so.force_home = True
                        rt.emit("task.warning", 1, tid=so.tid, uav=so.uav, code="home_blocked_forced", wid=so.wid)
                        z_l = float(rt.dsm(np.asarray(home)[None, :2])[0]) + 30.0 + self.layer_m(so)   # 按架次错层，机巢上方不叠在同一高度
                        self.submit(so, "goto", {"pos": [float(home[0]), float(home[1]), z_l], "route": "direct"})
                        return
                    if m is not None and m.frame != "multirotor":
                        r = self.fw_loiter_r(so)
                        so.action = {"op": "hold_home", "center": p.copy(), "r": r}
                        self.submit(so, "loiter", {"center": [float(p[0]), float(p[1]), float(p[2])], "radius_m": r, "dir": "cw"})
                    else:
                        so.action = {"op": "hold_home"}
                        self.submit(so, "hover", {})
                    so.retry_t, so.retry_op = rt.t + HOME_WAIT_S, "return"
                    return
                self._land(so)
                return
            if status == "canceled":
                return
            home = self._home_xy(so)
            p = rt.pos(so.uav)
            if float(np.hypot(p[0] - home[0], p[1] - home[1])) < HOME_NEAR_M:
                self._land(so)
            elif int(code or 0) in SAFETY_ACTIVE_CODES or op == "goto" or not self._is_mr(so):
                # 固定翼与垂起机不退回 M10 的多旋翼自动航线（其终点高度为机巢上方 30 m，固定翼据此在城区低空返航，D2-FX-R2-tasking 第 2 次：第 1 轮
                # 风下固定翼以 goto 降到 45 m 撞上楼顶）
                # D2-FX-R2：此前 goto 也被拒时原地 land（离机巢数百米、与让行的邻机并排下降 → POS_ERR_ELAND）；改为稍后重试返航
                so.retry_t, so.retry_op = rt.t + RETRY_S, "return"
            else:
                self.submit(so, "goto", {"pos": [float(home[0]), float(home[1]), float(rt.dsm(np.asarray(home)[None, :2])[0]) + 30.0
                                                 + self.layer_m(so)], "route": "auto"})
            return
        if so.state == "RETURNING" and op in ("hover", "loiter") and (so.action or {}).get("op") in ("land_wait", "hold_home"):
            return
        if so.state == "RECOVERING" and op == "land":
            if ok:
                self._recovered(so)
            elif status != "canceled":
                so.t_state = rt.t
            return

    def _home_xy(self, so: SortieRT) -> np.ndarray:
        rt = self.rt
        n = rt.nests.get(so.nest_id or "")
        e = rt.entry(so.uav)
        h = getattr(e, "home_enu_m", None) if e is not None else None
        return np.asarray(h if h is not None else (n.pos3 if n is not None else rt.pos(so.uav)), float)

    def _recovered(self, so: SortieRT) -> None:
        rt = self.rt
        n = rt.nests.get(so.nest_id or "")
        soc = rt.soc(so.uav)
        if n is not None:
            n.recover(so.uav, rt.t, soc)
            n.recover_queue = [r for r in n.recover_queue if r.get("uav") != so.uav]
        m = rt.model(rt.model_id(so.uav))
        used = max(so.e0_wh - soc * (m.e_use_wh if m else 200.0), 0.0)
        rt.relays.account_sortie(so, used, rt.t - (so.t_launched or rt.t))
        self.stats["recoveries"] += 1
        self.stats["flight_s"] += rt.t - (so.t_launched or rt.t)
        rt.emit("nest.recover", 0, uav=so.uav, nest=so.nest_id, tid=so.tid, wid=so.wid)
        rt.release_lease(so.uav, so.principal)
        rt.sorties.pop(so.uav, None)
        so.state = "ENDED"
        rt.request_allocation()

    def _off_nest(self, so: SortieRT) -> bool:
        """落地点离所属机巢超过 OFF_NEST_M（按机巢位置判定：M08 的 home 在迫降后可能被改到落地点）。"""
        n = self.rt.nests.get(so.nest_id or "")
        if n is None:
            return False
        p = self.rt.pos(so.uav)
        return float(np.hypot(p[0] - n.pos3[0], p[1] - n.pos3[1])) > OFF_NEST_M

    def _lost(self, so: SortieRT) -> None:
        """机体落在机巢外（迫降、撞楼后着地）：不按回收入巢（此前记 nest.recover，机巢随即换电并把它当作就绪，S7 中撞上楼顶的
        AWR-V1 被再次分配并在楼顶反复 takeoff），记入 `rt.lost` 并从机巢的在巢机体中移除（`sync_nests` 不再登记，`vehicle_views`
        随之为 unavailable，不可分配；机巢状态载荷不再列出它）；回到机巢附近着地后恢复。租约释放给操作员处置。"""
        rt = self.rt
        n = rt.nests.get(so.nest_id or "")
        rt.lost.add(so.uav)
        if n is not None:
            n.detach(so.uav)
            n.recover_queue = [r for r in n.recover_queue if r.get("uav") != so.uav]
        m = rt.model(rt.model_id(so.uav))
        soc = rt.soc(so.uav)
        rt.relays.account_sortie(so, max(so.e0_wh - soc * (m.e_use_wh if m else 200.0), 0.0), rt.t - (so.t_launched or rt.t))
        rt.emit("task.warning", 1, tid=so.tid, uav=so.uav, code="landed_off_nest", wid=so.wid)
        rt.release_lease(so.uav, so.principal)
        rt.sorties.pop(so.uav, None)
        so.state = "ENDED"
        rt.request_allocation()

    def _fail_work(self, so: SortieRT, why: str) -> None:
        _T, it = self.item_of(so)
        if it is not None and it.state not in ("DONE", "CANCELED"):
            it.state, it.uav, it.reason = "PENDING", None, why
            if it.w.kind == "relay_station" and why.startswith("transit:"):
                self.rt.relays.on_transit_failed(so, it)     # 不可达站位：回避并重选，不把同一站位派给下一架
            self.rt.request_allocation()
        self.return_home(so)

    def _grounded(self, so: SortieRT) -> None:
        rt = self.rt
        rt.emit("task.warning", 1, tid=so.tid, uav=so.uav, code="grounded", wid=so.wid)
        self._drop(so, "grounded")

    def _drop(self, so: SortieRT, why: str) -> None:
        rt = self.rt
        _T, it = self.item_of(so)
        if it is not None and it.state not in ("DONE", "CANCELED"):
            it.state, it.uav, it.reason = "PENDING", None, why
            rt.emit("task.reallocated", 0, tid=it.w.tid, wid=it.w.wid, from_uav=so.uav, to_uav=None, reason="lost", items=[])
        self.stats["dropped"] += 1
        so.state, so.t_state = "DROPPED", rt.t
        so.released = True
        rt.relays.on_dropped(so, why)
        rt.request_allocation(urgent=True)          # M20-FR-047：2 s【仿真】内重新分配

    # ------------------------------------------------------------ 1 Hz
    def step(self) -> None:
        rt = self.rt
        t = rt.t
        if t - self._recheck_t >= ROUTE_RECHECK_S:
            self._recheck_t = t
            self.recheck_routes()
        for uav in sorted(rt.sorties):
            so = rt.sorties.get(uav)
            if so is None:
                continue
            if so.state == "ASSIGNED":
                self._step_assigned(so)
                continue
            fs = rt.fs(uav)
            if so.state == "DROPPED":
                if rt.slot_of(uav) < 0:
                    rt.sorties.pop(uav, None)
                elif fs in ("LANDED", "DISARMED", "READY"):
                    if self._off_nest(so):
                        self._lost(so)
                    else:
                        self._recovered(so)
                continue
            if so.airborne and so.state != "RECOVERING":
                if fs in FAIL_STATES or (fs == "RTL" and so.state != "RETURNING"):
                    self._drop(so, fs.lower())
                    continue
                if rt.energy_rtl_n(uav) > so.rtl0:
                    self._drop(so, "energy_rtl")
                    rt.relays.count_energy_rtl(so)
                    continue
                L = rt.lease(uav)
                if so.state != "PREEMPTED" and L.get("owner") in ("OPERATOR", "AGENT"):
                    self._preempt(so, str(L.get("owner")))
                    continue
            if so.state == "PREEMPTED":
                L = rt.lease(uav)
                if L.get("owner") in ("NONE", None) or (L.get("owner") == "MISSION" and L.get("holder") == so.principal):
                    if fs in ("LANDED", "DISARMED", "READY"):
                        self._recovered(so)
                    else:
                        self.standby(so)
                        so.released = True
                continue
            if so.retry_t is not None and t >= so.retry_t and so.state in ("TRANSIT", "RETURNING", "WORKING"):
                op_r, so.retry_t = so.retry_op, None
                if op_r == "return" and so.state == "RETURNING":
                    self.return_home(so)
                elif op_r == "transit" and so.state == "TRANSIT":
                    self.start_transit(so)
                elif op_r == "relief" and so.state == "TRANSIT":
                    self.start_work(so)
                elif op_r == "work" and so.state == "WORKING":
                    if self._yield(so, t):
                        so.retry_t, so.retry_op = t + RETRY_S, "work"
                    else:
                        self._resume_action(so)
                continue
            if so.state == "RETURNING" and so.land_wait_t is not None and t >= so.land_wait_t:
                self._land(so)
                continue
            if so.state == "WORKING":
                self._step_working(so)
            elif so.state == "STANDBY":
                if (so.standby_since is not None and t - so.standby_since > STANDBY_TIMEOUT_S and so.released) or self._low_energy(so):
                    self.return_home(so)
            elif so.state == "RECOVERING" and fs in ("LANDED", "DISARMED", "READY") and t - so.t_state > 3.0:
                self._recovered(so)
            elif so.state in ("TRANSIT", "RETURNING") and self._low_energy(so) and so.state == "TRANSIT":
                self._fail_work(so, "energy")

    def _step_assigned(self, so: SortieRT) -> None:
        rt = self.rt
        t = rt.t
        n = rt.nests.get(so.nest_id or "")
        if n is None:
            rt.sorties.pop(so.uav, None)
            return
        a = n.air.get(so.uav)
        T, it = self.item_of(so)
        if it is None or T is None or T.state not in ("RUNNING",):
            if T is not None and T.state == "PAUSED":
                return
            self.release(so, "cancel")
            return
        if a is None or a.state != "ready" or not n.wind_ok:
            if t >= so.t_launch:
                reason = "wind" if not n.wind_ok else "battery"
                if not so.delay_noted:
                    so.delay_noted = True
                    rt.emit("nest.dispatch_delayed", 2, uav=so.uav, nest=n.id, tid=so.tid, wid=so.wid, reason=reason, delay_s=0.0)
                    rt.relays.note_delay(T, reason)
                tr = n.t_ready(so.uav, t)
                so.t_launch = n.pad.book(max(t + 1.0, tr if math.isfinite(tr) else t + 10.0), "launch", so.uav)
            return
        if t + 1e-6 >= so.t_launch:
            iv = float(n.cfg["launch_interval_s"])
            busy = [e[0] for e in n.pad.events if e[1] == "recover" and e[2] != so.uav and abs(e[0] - t) < iv - 1e-9]
            if busy:                                 # 回收优先（M20-FR-003）：与正在降落的邻机错开 launch_interval_s
                so.t_launch = max(busy) + iv
                return
            so.rtl0 = rt.energy_rtl_n(so.uav)
            self._launch(so)

    def _step_working(self, so: SortieRT) -> None:
        rt = self.rt
        t = rt.t
        T, it = self.item_of(so)
        if it is None or T is None:
            self.release(so, "item_gone")
            return
        a = so.action or {}
        if so.hover_until is not None and t >= so.hover_until:
            so.hover_until = None
            if a.get("done"):
                it.state = "DONE"
                self.release(so, "done")
                rt.finish_if_done(T)
                return
            self._next_action(so)
            return
        if a.get("op") in ("hold", "loiter"):
            if it.w.kind == "station" or it.w.kind == "relay_station":
                it.dwell_s += 1.0
            self._point(so)
        elif a.get("look") is not None:
            self._look(so, a["look"])                  # 看洞停留中每秒刷新指向与变焦（运动识别物）
        if it.w.kind == "relay_station":
            return                                  # 接力的接替由 RelayController 判定
        if it.w.persistent and it.relief_for is None and it.state == "ACTIVE":
            m = rt.model(rt.model_id(so.uav))
            if m is None:
                return
            t_end = self.t_end(so, m)
            eta = self.eta_relief(so, it)
            if t_end - t <= (eta if math.isfinite(eta) else 0.0) + T_MARGIN_S + rt.jobs.t_plan_s():
                # 预测性接替：同一工作项交给下一架（到达后本机返航）；无候选时到 t_end 返航
                if math.isfinite(eta):
                    it.state, it.relief_for = "PENDING", so.uav
                    rt.request_allocation()
                    rt.emit("task.reallocated", 0, tid=it.w.tid, wid=it.w.wid, from_uav=so.uav, to_uav=None, reason="energy", items=[])
                elif t_end - t <= T_MARGIN_S:
                    it.state, it.uav = "PENDING", None
                    self.release(so, "energy")
                    rt.request_allocation()
        elif it.w.persistent and it.relief_for == so.uav:
            # 接替已派而未到：本机到自身 t_end 仍须返航（D2-FX-R2：此前置 relief_for 后不再核对 t_end，T5 变体中值守 P600 等接替
            # 等到 SOC 0.17 被 M09 能量 RTL 接管，直线返航穿过识别物敏感体而被发现）；接替到达后从作业起点继续
            m = rt.model(rt.model_id(so.uav))
            if m is not None and self.t_end(so, m) - t <= 0.0:
                it.relief_for = None
                if it.uav == so.uav:
                    it.state, it.uav = "PENDING", None
                self.release(so, "energy")
                rt.request_allocation()

    def _preempt(self, so: SortieRT, owner: str) -> None:
        rt = self.rt
        T, it = self.item_of(so)
        self.stats["preempted"] += 1
        so.state, so.t_state = "PREEMPTED", rt.t
        so.actions, so.action, so.hover_until = [], None, None
        self.release_pointing(so)
        if it is not None and it.state not in ("DONE", "CANCELED"):
            it.state, it.uav, it.reason = "PENDING", None, "manual"
            rt.emit("task.reallocated", 0, tid=it.w.tid, wid=it.w.wid, from_uav=so.uav, to_uav=None, reason="manual_takeover", items=[])
            rt.request_allocation(urgent=True)      # M20-FR-046：2 s【仿真】内重新分配
        rt.relays.on_preempt(so)
        del T, owner

    def min_shift_s(self) -> float:
        return MIN_SHIFT_S
