"""禁入守卫与撤离（M20-FR-044、FR-056；M20 §6.9.6；AWR-04 §9.4）。

5 Hz：对每架 M20 驱动的空中机体，按当前速度预测 5 s 内（0.5 s 步长）是否进入"禁入圆 + v_t·τ_pred"（运动识别物按整圆，τ_pred
5 s）；预测进入即撤离：撤离点 `p_e = p_t + û·(R_keep + v_t·τ_pred + 10 m)`（û 为识别物指向机体的水平单位向量，该点在其他禁入圆内
或 DSM 净空不足时依次试 ±45°、±90°），以机型最大速度 `goto{route: direct}`；撤离完成后恢复原动作（转场与作业从当前位置重新规划）。
撤离期间若机体承担接力，间隙原因记 `keepout_priority`。另计"进入配置体"（不含外扩的敏感体）的次数与时长，供接力统计。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from awr.tasking.keepout import inside

__all__ = ["KeepoutGuard"]

TAU_PRED_S = 5.0
STEP_S = 0.5
EXTRA_M = 10.0
GUARD_STATES = ("TRANSIT", "WORKING", "STANDBY", "RETURNING")
FW_EVADE_EXTRA_M = 20.0   # 固定翼撤离点：外推 R_turn + 该值（撤离点为到点后的盘旋中心）
EVADE_DSM_CLEAR_M = 15.0  # 撤离走廊的 DSM 净空（与转场同一口径）
EVADE_AGL_MAX_M = 140.0   # 撤离走廊所需高度超过该 AGL 时换方向
EVADE_HOLD_S = 10.0       # 多旋翼撤离点未变（< EVADE_SAME_M）时不重发的时长
EVADE_SAME_M = 15.0
FW_REROUTE_GAP_S = 5.0    # 固定翼预测入侵（非临近）触发重规划的最小间隔
PATH_TOL_M = 3.0          # 多旋翼按转场 / 返航航线预测时的入侵容差（航线按当前禁入圆的外切多边形规划，此容差只吸收数值与跟踪偏差）。
                          # 作业航线（巡线、巡查、巡边）仍按速度直线外推：其绕行在分解时规划，与守卫圆有数米偏差，按航线预测会在
                          # 同一点反复撤离（D2-FX-R2-tasking 第 2 次：巡线三机 1800 s 未完成）
FW_EVADE_REJECT_S = 5.0   # 固定翼撤离航线被拒（越界、穿楼）后不再撤离的冷却时长（其间按禁入圆外航线重规划转场）


class KeepoutGuard:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.stats = {"evades": 0, "checks": 0}
        self.inside_cfg: dict[str, bool] = {}

    def _circles(self) -> tuple[np.ndarray, list[Any]]:
        rt = self.rt
        kos = rt.keepouts(planning=False)
        if not kos:
            return np.zeros((0, 3)), []
        trt = rt.targets_rt()
        C = []
        for k in kos:
            r = k.r
            if k.moving and trt is not None:
                st = trt.state1(k.target) if k.target else None
                r += float(getattr(st, "speed_mps", 0.0) or 0.0) * TAU_PRED_S
            C.append([k.x, k.y, r])
        return np.asarray(C, float), kos

    def step(self) -> None:
        rt = self.rt
        if not rt.sorties:
            return
        C, kos = self._circles()
        self._config_entries()
        if len(C) == 0:
            return
        ts0 = np.arange(0.0, TAU_PRED_S + 1e-9, STEP_S)
        for uav in sorted(rt.sorties):
            so = rt.sorties[uav]
            if so.state not in GUARD_STATES or so.force_home:
                continue
            p = rt.pos(uav)
            v = rt.vel(uav)
            r_t = self._r_turn(so)
            Q = p[None, :2] + ts0[:, None] * v[None, :2]
            tol = 0.0
            if r_t <= 0.0 and so.call_op == "goto" and so.goto_goal is not None:
                # 多旋翼 goto：到目标点即停，预测轨迹不越过目标点（D2-FX-R2：贴近禁入圆的站位在到站减速时被直线外推判为入侵，
                # 到站—撤离循环）；目标点本身在圆内时仍按外推判定
                g = np.asarray(so.goto_goal, float)[:2]
                L = float(np.hypot(*(g - p[:2])))
                vh = float(np.hypot(v[0], v[1]))
                if L > 1e-6:
                    s_q = np.minimum(ts0 * max(vh, 0.5), L)
                    Q = p[None, :2] + s_q[:, None] * ((g - p[:2]) / L)[None, :]
                else:
                    Q = np.repeat(p[None, :2], len(ts0), axis=0)
            if r_t <= 0.0 and so.call_op == "follow_path" and so.state in ("TRANSIT", "RETURNING") and so.path_cur is not None \
                    and len(so.path_cur) >= 2:
                # 多旋翼沿航线飞行：按剩余航线预测 τ_pred 内的位置（D2-FX-R2-tasking 第 2 次：航线经可见图贴着规划口径的圆转弯，直线外推 5 s（约 40 m）
                # 在转角处越过守卫口径的圆而误判撤离，撤离后重规划又回到同一转角，转场—撤离循环 1000 s 以上）
                vh = max(float(np.hypot(v[0], v[1])), 0.5)
                Qp = self._fw_track(so, p, vh, TAU_PRED_S)
                if Qp is not None and len(Qp):
                    Q = Qp
                    tol = PATH_TOL_M                    # 绕圆航线的弦在圆内的拱高（规划口径即守卫口径的静止识别物）不算入侵
            if r_t > 0.0:
                # 固定翼与 FW 模式的垂起机不能悬停撤离：沿计划航线（或盘旋圈）预测 τ_pred + 2·R_turn / v 内的位置（直线外推会在
                # 绕禁入圆的圆弧上误判入侵），没有航线时取直线外推加掉头前冲时间
                vh = max(float(np.hypot(v[0], v[1])), 10.0)
                T_h = TAU_PRED_S + 2.0 * r_t / vh
                Qp = self._fw_track(so, p, vh, T_h)
                Q = Qp if Qp is not None else p[None, :2] + np.arange(0.0, T_h + 1e-9, STEP_S)[:, None] * v[None, :2]
            self.stats["checks"] += 1
            d = np.hypot(Q[:, None, 0] - C[None, :, 0], Q[:, None, 1] - C[None, :, 1]) - C[None, :, 2]
            hit = d < -tol
            if not hit.any():
                continue
            i0, j = (int(x) for x in np.argwhere(hit)[0])
            if r_t > 0.0 and rt.t - so.evade_rej_t < FW_EVADE_REJECT_S:
                continue                            # 撤离航线刚被细校验拒绝：冷却期内不在 5 Hz 上反复重发（D2-FX-R2-tasking 第 2 次：第 1 轮 4174 次）
            if r_t > 0.0 and so.state in ("TRANSIT", "RETURNING") and float(d[0].min()) > 0.0:
                # 固定翼：预测入侵点在航线前方较远处（τ_pred 内飞不到）时先按当前禁入圆重规划转场（航线顶点外扩转弯半径），不做
                # 撤离机动（D2-FX-R2：S7 中垂起机刚转固定翼即因 340 m 外车辆禁入圆的预测入侵撤离到西南角，随后越界 RTL）
                s_hit = float(np.hypot(*(Q[i0] - p[:2])))
                if s_hit > vh * TAU_PRED_S + r_t and rt.t - so.reroute_t >= FW_REROUTE_GAP_S:
                    so.reroute_t = rt.t
                    rt.ex.stats["reroutes"] += 1
                    if so.state == "TRANSIT":
                        rt.ex.start_transit(so)
                    else:
                        rt.ex.return_home(so)
                    continue
                if s_hit > vh * TAU_PRED_S + r_t:
                    continue                        # 刚重规划过：新航线生效前不撤离，入侵临近时再撤离
            self.evade(so, C, j, p, kos[j] if j < len(kos) else None)

    def _r_turn(self, so: Any) -> float:
        """固定翼与 FW 模式垂起机的转弯半径（按当前风，见 Executor.fw_loiter_r）；多旋翼与 MC 模式的垂起机为 0（可悬停撤离）。"""
        fm = self.rt.fw_mode(so.uav)
        if fm is None or fm in ("MC", "TRANSITION_MC"):
            return 0.0
        return float(self.rt.ex.fw_loiter_r(so))

    def _fw_track(self, so: Any, p: np.ndarray, v: float, T_h: float) -> np.ndarray | None:
        """固定翼在 T_h 内的计划水平轨迹点（每 STEP_S·v 一个）：follow_path 取剩余航线，盘旋取当前盘旋圈；其他 None。"""
        L = v * T_h
        step = max(v * STEP_S, 2.0)
        if so.call_op == "follow_path" and so.path_cur is not None and len(so.path_cur) >= 2:
            P = np.asarray(so.path_cur, float)[:, :2]
            k0 = int(np.argmin(np.hypot(P[:, 0] - p[0], P[:, 1] - p[1])))
            rest = np.vstack([p[None, :2], P[min(k0 + 1, len(P) - 1):]])
            seg = np.hypot(*np.diff(rest, axis=0).T)
            cum = np.r_[0.0, np.cumsum(seg)]
            if cum[-1] < 1e-6:
                return rest[:1]
            s_q = np.arange(0.0, min(L, cum[-1]) + 1e-9, step)
            return np.c_[np.interp(s_q, cum, rest[:, 0]), np.interp(s_q, cum, rest[:, 1])]
        if so.call_op == "loiter" and so.action is not None and so.action.get("center") is not None:
            c = np.asarray(so.action["center"], float)[:2]
            r = float(so.action.get("r") or so.loiter_r or self._r_turn(so))
            th0 = math.atan2(p[1] - c[1], p[0] - c[0])
            n = max(2, int(L / step))
            th = th0 - np.linspace(0.0, L / max(r, 1.0), n)          # 顺时针盘旋
            return np.c_[c[0] + r * np.cos(th), c[1] + r * np.sin(th)]
        return None

    def _border_m(self, q: np.ndarray) -> float:
        zones = getattr(self.rt.world, "zones", None) if self.rt.world is not None else None
        if zones is None:
            return math.inf
        try:
            return float(np.asarray(zones.border_signed_distance(np.asarray(q, float).reshape(1, 2)), float)[0])
        except Exception:
            return math.inf

    def evade(self, so: Any, C: np.ndarray, j: int, p: np.ndarray, ko: Any) -> None:
        rt = self.rt
        m = rt.model(rt.model_id(so.uav))
        c = C[j, :2]
        u = p[:2] - c
        n = float(np.hypot(*u))
        u = u / n if n > 1e-6 else np.array([1.0, 0.0])
        r_t = self._r_turn(so)
        # 固定翼撤离点是到点后的盘旋中心：离禁入圆再留一个盘旋半径与余量（此前 2·R_turn + 20 m，撤离点离车辆约 450 m，
        # S7 机巢一带无处可去）
        R = float(C[j, 2]) + EXTRA_M + (r_t + FW_EVADE_EXTRA_M if r_t > 0.0 else 0.0)
        # 运动识别物：撤离到规划口径（再外扩 v_nom·τ_route）之外，避免识别物继续逼近时反复撤离（撤离后恢复的转场目标同一口径）
        Cp = rt.circles(planning=True)
        if len(Cp):
            jp = int(np.argmin(np.hypot(Cp[:, 0] - c[0], Cp[:, 1] - c[1])))
            if float(np.hypot(*(Cp[jp, :2] - c))) < 1.0:
                R = max(R, float(Cp[jp, 2]) + EXTRA_M)
        target = None
        best = (-math.inf, None)
        # 撤离点离世界边界的内缩：固定翼到点后以撤离点为心盘旋（半径 r_t），再加转弯前冲，内缩 30 + 2·r_t（D2-FX-R2：S7 诊断中
        # 内缩 30 + r_t 的撤离点使盘旋圈越过边界，M09 CORRECTING/GEOFENCE → RTL）
        inset = 30.0 + (2.0 * r_t if r_t > 0.0 else 0.0)
        for ang in (0.0, math.pi / 4, -math.pi / 4, math.pi / 2, -math.pi / 2, 3 * math.pi / 4, -3 * math.pi / 4):
            uu = np.array([u[0] * math.cos(ang) - u[1] * math.sin(ang), u[0] * math.sin(ang) + u[1] * math.cos(ang)])
            q = c + uu * R
            if len(C) > 1 and bool(inside(q[None], np.delete(C, j, axis=0))[0]):
                continue
            zq = self._clear_z(p, q)
            if zq - float(rt.dtm(q[None])[0]) > EVADE_AGL_MAX_M:
                continue                            # 撤离走廊要爬得过高（高楼群）：换方向
            if r_t > 0.0 and zq > float(p[2]) + 3.0:
                continue                            # 固定翼爬升率有限：只取当前高度上净空足够的撤离方向
            bm = self._border_m(q)                  # 撤离点在世界边界内（D2-FX-R2：S7 中撤离点越界，follow_path 102 每 0.2 s 一次）
            if bm >= inset:
                target = q
                break
            if bm > best[0]:
                best = (bm, q)
        if target is None:
            target = best[1] if best[1] is not None else c + u * R
            if r_t > 0.0 and best[0] < 30.0 and so.state in ("TRANSIT", "WORKING", "RETURNING"):
                # 固定翼：没有在世界边界内的撤离点（强风下转弯半径很大）时不下发必被拒的撤离航线，改为按禁入圆外航线重规划转场或
                # 返航，冷却 FW_EVADE_REJECT_S
                so.evade_rej_t = rt.t
                so.reroute_t = rt.t
                rt.ex.stats["reroutes"] += 1
                if so.state == "RETURNING":
                    rt.ex.return_home(so)
                else:
                    rt.ex.start_transit(so)
                return
        if so.state == "EVADING" and r_t > 0.0 and rt.t - so.t_state < 3.0:
            return                                  # 固定翼撤离航线刚下发：等转弯生效，不在 5 Hz 上反复重发
        if (so.state == "EVADING" and r_t <= 0.0 and so.evade_goal is not None and rt.t - so.t_state < EVADE_HOLD_S
                and float(np.hypot(*(np.asarray(so.evade_goal, float)[:2] - target))) < EVADE_SAME_M):
            return                                  # 多旋翼：撤离点未变，不在 5 Hz 上反复取代在途 goto（第 2 轮诊断 542 次 206 superseded）
        if so.state != "EVADING":
            so.evade_ts = [*[x for x in so.evade_ts if rt.t - x <= 600.0][-9:], rt.t]
        so.evade_from = so.state if so.state != "EVADING" else so.evade_from
        so.state, so.t_state = "EVADING", rt.t
        so.actions, so.action, so.hover_until = [], None, None
        so.evade_goal, so.evade_climb = (float(target[0]), float(target[1])), None
        if r_t > 0.0:
            # 固定翼与 FW 模式的垂起机：两点航线（长航段，不触发垂起机的悬停类 goto 转多旋翼，D2-INT S7 的 180 s 悬停上限），
            # 高度取撤离走廊的 DSM 净空高度（D2-FX-R2：此前保持当前高度不复核走廊，30 m AGL 的撤离撞楼）
            z = self._clear_z(p, target)
            wp = [[float(p[0]), float(p[1]), z], [float(target[0]), float(target[1]), z]]
            so.path_cur = np.asarray(wp, float)       # 守卫的航迹预测与航线复核按撤离航线（此前仍是被取代的转场航线）
            rt.ex.submit(so, "follow_path", {"waypoints": wp})
        else:
            # 多旋翼：撤离走廊（±5 m）在当前高度 DSM 净空不足时先原地爬升再水平撤离（D2-FX-R2：刚起飞 10 m 高度向楼群直飞撤离，
            # 细校验 102 每 0.2 s 一次、持续 30 s）
            z = self._clear_z(p, target)
            v = float(min(m.v_max_mps if m else 12.0, 12.0))
            if z > float(p[2]) + 2.0:
                so.evade_climb = (float(target[0]), float(target[1]), z, v)
                rt.ex.submit(so, "goto", {"pos": [float(p[0]), float(p[1]), z], "route": "direct"})
            else:
                rt.ex.submit(so, "goto", {"pos": [float(target[0]), float(target[1]), float(p[2])], "route": "direct", "speed_mps": v})
        rt.ex.stats["evades"] += 1
        self.stats["evades"] += 1
        rt.relays.on_evade(so)
        rt.emit("uav.keepout_guard", 1, uav=so.uav, target=None if ko is None else ko.target, d_m=round(float(n - C[j, 2]), 2),
                clamped=False, action="evade")

    def _clear_z(self, p: np.ndarray, q: np.ndarray) -> float:
        """p → q 水平撤离走廊（±5 m，每 5 m）所需高度：max(当前高度, DSM 最大值 + 15 m)。"""
        a2, b2 = np.asarray(p, float)[:2], np.asarray(q, float)[:2]
        L = float(np.hypot(*(b2 - a2)))
        if L < 1.0:
            return float(p[2])
        n = max(2, int(L / 5.0) + 1)
        u = (b2 - a2) / L
        nrm = np.array([-u[1], u[0]])
        pts = np.vstack([a2 + (b2 - a2) * t + nrm * o for t in np.linspace(0.0, 1.0, n) for o in (-5.0, 0.0, 5.0)])
        return max(float(p[2]), float(self.rt.dsm(pts).max()) + EVADE_DSM_CLEAR_M)

    def evaded(self, so: Any, ok: bool) -> None:
        """撤离完成：恢复原动作（转场与作业从当前位置重新规划；返航继续返航；待命继续待命）。多旋翼的先爬升撤离在爬升完成后
        再下发水平段。撤离航线被拒时记下时刻（固定翼冷却 FW_EVADE_REJECT_S 内不再撤离）。"""
        rt = self.rt
        if not ok:
            so.evade_rej_t = rt.t
        if ok and so.evade_climb is not None:
            x, y, z, v = so.evade_climb
            so.evade_climb = None
            rt.ex.submit(so, "goto", {"pos": [x, y, z], "route": "direct", "speed_mps": v})
            return
        so.evade_goal, so.evade_climb = None, None
        prev = so.evade_from or "STANDBY"
        so.evade_from = None
        if prev == "RETURNING":
            rt.ex.return_home(so)
        elif prev == "STANDBY" or so.released or not so.wid:
            rt.ex.standby(so)
            so.released = True if prev == "STANDBY" and so.released else so.released
        else:
            _T, it = rt.ex.item_of(so)
            if it is not None and it.w.kind == "relay_station":
                rt.relays.after_retreat(so, it)
            rt.ex.start_transit(so)
        del ok

    def _config_entries(self) -> None:
        """进入配置体（不含外扩）的次数与时长（接力统计 `config_entries`）。配置体取守卫口径的禁入圆减去外扩 m（同一 tick 的
        缓存，不再逐识别物查询 M18）；运动识别物的扇形按整圆计（保守：只会多计，不会漏计）。"""
        rt = self.rt
        if not any(so.airborne for so in rt.sorties.values()):
            return
        m = rt.margin_m()
        A = np.asarray([[k.x, k.y, max(k.r - m, 0.0), k.z_min, k.z_max] for k in rt.keepouts(planning=False)], float).reshape(-1, 5)
        for uav in sorted(rt.sorties):
            so = rt.sorties[uav]
            if not so.airborne:
                continue
            p = rt.pos(uav)
            ins = False
            if len(A):
                d = np.hypot(A[:, 0] - p[0], A[:, 1] - p[1])
                ins = bool(((d < A[:, 2]) & (p[2] >= A[:, 3]) & (p[2] <= A[:, 4])).any())
            prev = self.inside_cfg.get(uav, False)
            if ins:
                rt.relays.config_inside(so, entered=not prev)
            self.inside_cfg[uav] = ins
