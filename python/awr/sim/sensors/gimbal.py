"""GimbalBank：云台 5 模式、限位、限速、动态集（M13-FR-011、FR-012、FR-016；M13 §6.5.2）。

目标角（d 为目标在挂载帧中的方向，`d = R_mountᵀ·R_wbᵀ·(p − p_mount_world)`）：
FIXED(az, el) 常量；LOOK_AT(p) `az = atan2(d_y, d_x)`、`el = atan2(d_z, √(d_x² + d_y²))`；LOOK_AT_AXIS(c_xy) 目标取
`(c_x, c_y, z_sensor)`；NADIR `az = 0`、`el = el_min`；FORWARD `az = 0`、`el = default_el`。先钳制到限位（撞限位置 `g_lim`），
再按 `rate_max·dt` 限速逼近；方位范围 < 360°，不做跨 ±180° 的最短弧。LOOK_AT 类模式目标在正下方
（√(d_x² + d_y²) < 1e-3·|d|）时方位保持上一值。FIXED、NADIR、FORWARD 收敛到 ≤ 0.01° 后退出动态集；LOOK_AT 类常驻。

动态集 n ≤ 8 时走纯 Python 标量路径（RK-1），否则 numpy 路径；两路径同一公式，数值等价（≤ 1e-12 rad，测试保证）；路径只取决于
动态集大小（确定性状态），因此结果可逐位重仿真。
来源：M10 任务项 `gimbal` 动作与生成器缺省（`set_gimbal`、`set_default_for`、`apply_item_gimbal`，sim-core 内部调用，当步生效，
不写日志）；外部来源（V0.2 起 operator 与 agent）经 `external=True` 在步边界按 apply_tick 锁存（D1 无外部来源，
日志回调 `on_external` 留给 V0.2）。
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from . import kernels_gimbal as KG
from .enums import GimbalMode, SensorKind, SensorState
from .frames import gimbal_R, quat_to_R, quat_to_R_s

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["COL_OF_KIND", "SCALAR_MAX", "GimbalBank", "SensorError", "generator_default", "mode_from_item"]

SCALAR_MAX = 8
ENTER_EPS = math.radians(0.1)
EXIT_EPS = math.radians(0.01)
COL_OF_KIND = {SensorKind.CAMERA: 0, SensorKind.THERMAL: 1}
KIND_OF_COL = {0: SensorKind.CAMERA, 1: SensorKind.THERMAL}
N_COLS = len(KIND_OF_COL)
MODE_NAMES = {"fixed": GimbalMode.FIXED, "look_at": GimbalMode.LOOK_AT, "look_at_axis": GimbalMode.LOOK_AT_AXIS,
              "nadir": GimbalMode.NADIR, "forward": GimbalMode.FORWARD}


class SensorError(ValueError):
    """M13 API 拒绝（原因码 + detail 字符串，AWR-17 §8.4）。"""

    def __init__(self, code: int, detail: str, **extra: Any) -> None:
        super().__init__(f"{code} {detail}")
        self.code = int(code)
        self.detail = detail
        self.extra = extra


def _vec3(x: Any) -> tuple[float, float, float]:
    a = [float(v) for v in x]
    if len(a) == 2:
        a.append(float("nan"))
    return a[0], a[1], a[2]


def mode_from_item(g: dict) -> tuple[GimbalMode, dict]:
    """M10 任务项 `gimbal` 字典（生成器 ItemDraft.gimbal、ActionSpec args）-> (模式, 参数)。

    识别：`{mode: nadir}`、`{mode: forward}`、`{mode: fixed, az_rad, el_rad}`、`{mode: look_at, p_enu_m}`、
    `{mode: look_at_axis, center_enu_m}`，以及 ActionSpec 形式 `{pitch_rad}`（FIXED(0, θ)）。"""
    if "pitch_rad" in g and "mode" not in g:
        th = float(g["pitch_rad"])
        return GimbalMode.FIXED, {"az_rad": float(g.get("yaw_rad", 0.0)), "el_rad": th}
    m = MODE_NAMES.get(str(g.get("mode", "fixed")).lower())
    if m is None:
        raise SensorError(110, "GIMBAL_MODE_UNKNOWN", mode=g.get("mode"))
    if m == GimbalMode.FIXED:
        return m, {"az_rad": float(g.get("az_rad", 0.0)), "el_rad": float(g.get("el_rad", g.get("pitch_rad", 0.0)))}
    if m == GimbalMode.LOOK_AT:
        p = g.get("p_enu_m", g.get("target_enu_m", g.get("center_enu_m")))
        if p is None:
            raise SensorError(110, "GIMBAL_TARGET_MISSING")
        return m, {"p_enu_m": _vec3(p)}
    if m == GimbalMode.LOOK_AT_AXIS:
        c = g.get("center_enu_m", g.get("p_enu_m"))
        if c is None:
            raise SensorError(110, "GIMBAL_TARGET_MISSING")
        return m, {"center_enu_m": _vec3(c)}
    return m, {}


def generator_default(generator: str, params: dict | None, ground: Callable[[float, float], float] | None = None) -> tuple[GimbalMode, dict]:
    """M13-FR-012 生成器缺省云台（任务项没有 `gimbal` 动作时）。"""
    p = params or {}
    g = str(generator)
    if g in ("lawnmower", "expanding_square", "terrain_follow"):
        return GimbalMode.NADIR, {}
    if g == "orbit":
        c = p.get("center_enu_m") or p.get("center") or [0.0, 0.0, 0.0]
        cx, cy = float(c[0]), float(c[1])
        cz = ground(cx, cy) if ground is not None else (float(c[2]) if len(c) > 2 else 0.0)
        return GimbalMode.LOOK_AT, {"p_enu_m": (cx, cy, cz)}
    if g == "helix_scan":
        mode = str(p.get("gimbal", "look_at_axis"))
        c = p.get("center_enu_m") or [0.0, 0.0, 0.0]
        if mode == "look_at_axis":
            return GimbalMode.LOOK_AT_AXIS, {"center_enu_m": (float(c[0]), float(c[1]), float("nan"))}
        return mode_from_item({"mode": mode, "p_enu_m": c, "center_enu_m": c})
    if g == "corridor":
        tilt = math.radians(float(p.get("gimbal_tilt_deg", 45.0)))
        side = str(p.get("look", p.get("side", "left")))
        return GimbalMode.FIXED, {"az_rad": math.pi / 2 if side == "left" else -math.pi / 2, "el_rad": -tilt}
    return GimbalMode.FIXED, {"az_rad": 0.0, "el_rad": math.radians(-15.0)}


class GimbalBank:
    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.pending: list[tuple[int, int, str, Any]] = []  # (apply_tick, seq, op, args)，外部来源
        self._seq = 0
        self.on_external: Callable[[dict], None] | None = None
        self.stats = {"scalar": 0, "numpy": 0, "limited": 0}
        self.use_numba = KG.HAVE_NUMBA  # 大机群路径用融合核（kernels_gimbal，与 `_step_vec` 逐位相同；ADR-070）
        self.scan = True  # 融合核内枚举动态集（gimbal_step_scan）；False 时先用 numpy 枚举再调 `_step_nb`（对拍用）
        self._tab_key: tuple | None = None
        self._tab: tuple | None = None

    # ------------------------------------------------------------ 参数
    def _cols(self, slot: int, sensor: str | None) -> list[int]:
        rig = self.rt.rig_of(slot)
        if rig is None:
            raise SensorError(110, "SENSOR_UNKNOWN", slot=slot)
        cols = []
        for k, kind in KIND_OF_COL.items():
            s = rig.by_kind(kind)
            if s is None or s.gimbal is None or not (self.rt.blk["has"][slot] & s.bit):
                continue
            if sensor is None or sensor == s.name or sensor == kind.name.lower():
                cols.append(k)
        if sensor is not None and not cols:
            raise SensorError(110, "SENSOR_UNKNOWN", slot=slot, sensor=sensor)
        return cols

    def spec_of(self, slot: int, k: int):
        rig = self.rt.rig_of(slot)
        return None if rig is None else rig.by_kind(KIND_OF_COL[k])

    # ------------------------------------------------------------ API（sim-core 内部调用，当步生效）
    def set_mode(self, slot: int, sensor: str | None, mode: GimbalMode | int | str, args: dict | None = None, *,
                 apply_tick: int | None = None, external: bool = False) -> None:
        m = MODE_NAMES[mode] if isinstance(mode, str) else GimbalMode(int(mode))
        if external:
            tick = int(apply_tick if apply_tick is not None else self.rt.tick + 1)
            rec = {"op": "sensor.gimbal", "slot": int(slot), "sensor": sensor, "mode": int(m), "args": dict(args or {}),
                   "apply_tick": tick}
            if self.on_external is not None:
                self.on_external(rec)
            self._seq += 1
            bisect.insort(self.pending, (tick, self._seq, "gimbal", rec))
            return
        self._apply_mode(int(slot), sensor, m, dict(args or {}))

    def _apply_mode(self, slot: int, sensor: str | None, m: GimbalMode, args: dict) -> None:
        b = self.rt.blk
        for k in self._cols(slot, sensor):
            b["g_mode"][slot, k] = int(m)
            b["g_tgt"][slot, k] = np.nan
            if m == GimbalMode.FIXED:
                b["g_paz"][slot, k] = float(args.get("az_rad", 0.0))
                b["g_pel"][slot, k] = float(args.get("el_rad", 0.0))
            elif m == GimbalMode.LOOK_AT:
                b["g_tgt"][slot, k] = _vec3(args["p_enu_m"])
            elif m == GimbalMode.LOOK_AT_AXIS:
                c = _vec3(args["center_enu_m"])
                b["g_tgt"][slot, k] = (c[0], c[1], np.nan)
            b["g_dyn"][slot, k] = True

    def set_gimbal(self, slot: int, sensor: str | None = None, *, pitch_rad: float | None = None,
                   look_at: str | None = None, mission_ctx: Any = None, apply_tick: int | None = None) -> None:
        """M10 任务项 `gimbal` 动作映射（M13 §6.5.2）：`pitch_rad = θ` -> FIXED(0, θ)；`look_at: axis` -> LOOK_AT_AXIS(center)；
        `look_at: center` -> LOOK_AT((c_x, c_y, dtm(c)))；`look_at: target` -> LOOK_AT(target_enu_m)，缺省退回 center。"""
        mc = _ctx_dict(mission_ctx)
        if pitch_rad is not None:
            self._apply_mode(int(slot), sensor, GimbalMode.FIXED, {"az_rad": 0.0, "el_rad": float(pitch_rad)})
            return
        if look_at is None:
            raise SensorError(110, "GIMBAL_ARGS_MISSING")
        c = mc.get("center_enu_m")
        if look_at == "axis":
            if c is None:
                raise SensorError(110, "GIMBAL_TARGET_MISSING")
            self._apply_mode(int(slot), sensor, GimbalMode.LOOK_AT_AXIS, {"center_enu_m": c})
            return
        if look_at == "target" and mc.get("target_enu_m") is not None:
            self._apply_mode(int(slot), sensor, GimbalMode.LOOK_AT, {"p_enu_m": mc["target_enu_m"]})
            return
        if c is None:
            raise SensorError(110, "GIMBAL_TARGET_MISSING")
        cx, cy = float(c[0]), float(c[1])
        self._apply_mode(int(slot), sensor, GimbalMode.LOOK_AT, {"p_enu_m": (cx, cy, self.rt.ground_z(cx, cy, c))})

    def set_default_for(self, slot: int, generator: str, params: dict | None = None, *, apply_tick: int | None = None,
                        sensor: str | None = None) -> None:
        m, a = generator_default(generator, params, lambda x, y: self.rt.ground_z(x, y, None))
        self._apply_mode(int(slot), sensor, m, a)

    def apply_item_gimbal(self, slot: int, gimbal: dict, sensor: str | None = None) -> None:
        m, a = mode_from_item(gimbal)
        self._apply_mode(int(slot), sensor, m, a)

    def set_active(self, slot: int, sensor: str, on: bool, *, apply_tick: int | None = None, external: bool = False) -> None:
        """M13-FR-016：关闭 -> STANDBY（`act` 位清零；SensorPose48 ACTIVE = 0；不参与检测；噪声过程照常推进；不发事件）。"""
        if external:
            tick = int(apply_tick if apply_tick is not None else self.rt.tick + 1)
            rec = {"op": "sensor.active", "slot": int(slot), "sensor": sensor, "on": bool(on), "apply_tick": tick}
            if self.on_external is not None:
                self.on_external(rec)
            self._seq += 1
            bisect.insort(self.pending, (tick, self._seq, "active", rec))
            return
        self._apply_active(int(slot), sensor, bool(on))

    def _apply_active(self, slot: int, sensor: str, on: bool) -> None:
        rig = self.rt.rig_of(slot)
        s = None if rig is None else rig.by_name(sensor)
        b = self.rt.blk
        if s is None or not (b["has"][slot] & s.bit):
            raise SensorError(110, "SENSOR_UNKNOWN", slot=slot, sensor=sensor)
        if on:
            b["act"][slot] |= np.uint8(s.bit)
            if b["state"][slot, s.kind] == SensorState.STANDBY:
                b["state"][slot, s.kind] = SensorState.ACTIVE
        else:
            b["act"][slot] &= np.uint8(0xFF & ~s.bit)
            b["state"][slot, s.kind] = SensorState.STANDBY

    def apply_pending(self, tick: int) -> int:
        n = 0
        while self.pending and self.pending[0][0] <= tick:
            _t, _s, op, rec = self.pending.pop(0)
            try:
                if op == "gimbal":
                    self._apply_mode(rec["slot"], rec["sensor"], GimbalMode(rec["mode"]), rec["args"])
                else:
                    self._apply_active(rec["slot"], rec["sensor"], rec["on"])
            except SensorError:
                pass
            n += 1
        return n

    # ------------------------------------------------------------ stage
    def step(self, S: Any, dt_s: float) -> int:
        b = self.rt.blk
        dyn = b["g_dyn"]
        if self.use_numba and self.scan:
            # 扫描版融合核：动态集枚举与逐对推进一次完成（与下面的 np.nonzero 行主序枚举 + `_step_nb` 逐位相同），
            # 对数不超过 SCALAR_MAX 时仍走标量路径（路径只取决于动态集大小，FX2-R3）
            npair = int(KG.count_pairs(dyn, S.active))
            if npair > SCALAR_MAX:
                self.stats["numpy"] += 1
                ok, f, mt, MR, eq = self._param_table()
                n_lim = KG.gimbal_step_scan(dyn, S.active, self.rt.slot_rig, N_COLS, ok, f, mt, MR, eq, b["g_mode"],
                                            b["g_az"], b["g_el"], b["g_paz"], b["g_pel"], b["g_tgt"], b["g_lim"], dyn,
                                            S.enu.pos, S.enu.q_xyzw, float(dt_s), EXIT_EPS)
                self.stats["limited"] += int(n_lim)
                return npair
            if npair == 0:
                return 0
        rows = np.flatnonzero(dyn[:, 0] | dyn[:, 1])
        if rows.size == 0:
            return 0
        rows = rows[S.active[rows]]
        if rows.size == 0:
            return 0
        ri, kk = np.nonzero(dyn[rows])  # 行主序（slot 升序、k 升序），与逐对枚举同序
        npair = ri.size
        if npair > SCALAR_MAX:  # 大机群：向量化路径（FX2-R2：免去逐对的 Python 枚举与 spec 查找）
            self.stats["numpy"] += 1
            if self.use_numba:
                self._step_nb(S, rows[ri], kk.astype(np.int64), dt_s)
            else:
                self._step_vec(S, rows, rows[ri], kk.astype(np.int64), ri, dt_s)
            return int(npair)
        pairs = [(int(rows[i]), int(k)) for i, k in zip(ri.tolist(), kk.tolist(), strict=True)]
        need_pose = any(int(b["g_mode"][s, k]) in (GimbalMode.LOOK_AT, GimbalMode.LOOK_AT_AXIS) for s, k in pairs)
        PQ = S.enu.pose_enu_flu(rows) if need_pose else None
        row_of = {int(s): i for i, s in enumerate(rows)}
        if len(pairs) <= SCALAR_MAX:
            self.stats["scalar"] += 1
            for s, k in pairs:
                self._step_scalar(s, k, None if PQ is None else PQ[row_of[s]], dt_s)
        else:
            self.stats["numpy"] += 1
            self._step_numpy(pairs, PQ, row_of, dt_s)
        return len(pairs)

    def _target_scalar(self, s: int, k: int, spec, pq) -> tuple[float, float]:
        b = self.rt.blk
        g = spec.gimbal
        m = int(b["g_mode"][s, k])
        if m == GimbalMode.FIXED:
            return float(b["g_paz"][s, k]), float(b["g_pel"][s, k])
        if m == GimbalMode.NADIR:
            return 0.0, g.el_min_rad
        if m == GimbalMode.FORWARD:
            return 0.0, g.default_el_rad
        # LOOK_AT 类：d = R_mountᵀ·R_wbᵀ·(p − p_mount)（纯 math 标量运算）
        R = quat_to_R_s(float(pq[3]), float(pq[4]), float(pq[5]), float(pq[6]))
        mt = spec.mount_t
        mx, my, mz = float(mt[0]), float(mt[1]), float(mt[2])
        pmx = float(pq[0]) + R[0] * mx + R[1] * my + R[2] * mz
        pmy = float(pq[1]) + R[3] * mx + R[4] * my + R[5] * mz
        pmz = float(pq[2]) + R[6] * mx + R[7] * my + R[8] * mz
        t = b["g_tgt"][s, k]
        tz = pmz if m == GimbalMode.LOOK_AT_AXIS else float(t[2])
        wx, wy, wz = float(t[0]) - pmx, float(t[1]) - pmy, tz - pmz
        bx = R[0] * wx + R[3] * wy + R[6] * wz
        by = R[1] * wx + R[4] * wy + R[7] * wz
        bz = R[2] * wx + R[5] * wy + R[8] * wz
        M = spec.mount_R
        dx = float(M[0, 0]) * bx + float(M[1, 0]) * by + float(M[2, 0]) * bz
        dy = float(M[0, 1]) * bx + float(M[1, 1]) * by + float(M[2, 1]) * bz
        dz = float(M[0, 2]) * bx + float(M[1, 2]) * by + float(M[2, 2]) * bz
        h = math.sqrt(dx * dx + dy * dy)
        el = math.atan2(dz, h)
        n = math.sqrt(h * h + dz * dz)
        az = float(b["g_az"][s, k]) if h < 1e-3 * n else math.atan2(dy, dx)
        return az, el

    def _step_scalar(self, s: int, k: int, pq, dt: float) -> None:
        spec = self.spec_of(s, k)
        b = self.rt.blk
        if spec is None or spec.gimbal is None:
            b["g_dyn"][s, k] = False
            return
        g = spec.gimbal
        az_t0, el_t0 = self._target_scalar(s, k, spec, pq)
        az_t = min(max(az_t0, g.az_min_rad), g.az_max_rad)
        el_t = min(max(el_t0, g.el_min_rad), g.el_max_rad)
        lim = az_t != az_t0 or el_t != el_t0
        b["g_lim"][s, k] = lim
        if lim:
            self.stats["limited"] += 1
        step = g.rate_max_rad_s * dt
        az = float(b["g_az"][s, k])
        el = float(b["g_el"][s, k])
        az += min(max(az_t - az, -step), step)
        el += min(max(el_t - el, -step), step)
        b["g_az"][s, k] = az
        b["g_el"][s, k] = el
        m = int(b["g_mode"][s, k])
        if m in (GimbalMode.FIXED, GimbalMode.NADIR, GimbalMode.FORWARD) and abs(az_t - az) <= EXIT_EPS and abs(el_t - el) <= EXIT_EPS:
            b["g_az"][s, k] = az_t
            b["g_el"][s, k] = el_t
            b["g_dyn"][s, k] = False

    def _step_numpy(self, pairs: list[tuple[int, int]], PQ, row_of: dict[int, int], dt: float) -> None:
        # 分组：按 (slot, k) 逐个取 spec 参数，目标角向量化计算（LOOK_AT 类批量求 d）
        b = self.rt.blk
        n = len(pairs)
        S_ = np.fromiter((p[0] for p in pairs), np.int64, n)
        K_ = np.fromiter((p[1] for p in pairs), np.int64, n)
        specs = [self.spec_of(s, k) for s, k in pairs]
        ok = np.array([sp is not None and sp.gimbal is not None for sp in specs])
        for i in np.flatnonzero(~ok):
            b["g_dyn"][S_[i], K_[i]] = False
        idx = np.flatnonzero(ok)
        if idx.size == 0:
            return
        S_, K_ = S_[idx], K_[idx]
        specs = [specs[i] for i in idx]
        G = [sp.gimbal for sp in specs]
        az_min = np.array([g.az_min_rad for g in G])
        az_max = np.array([g.az_max_rad for g in G])
        el_min = np.array([g.el_min_rad for g in G])
        el_max = np.array([g.el_max_rad for g in G])
        rate = np.array([g.rate_max_rad_s for g in G])
        mode = b["g_mode"][S_, K_].astype(np.int64)
        az = b["g_az"][S_, K_].copy()
        el = b["g_el"][S_, K_].copy()
        az_t = b["g_paz"][S_, K_].copy()
        el_t = b["g_pel"][S_, K_].copy()
        nad = mode == GimbalMode.NADIR
        az_t[nad] = 0.0
        el_t[nad] = el_min[nad]
        fwd = mode == GimbalMode.FORWARD
        az_t[fwd] = 0.0
        el_t[fwd] = np.array([g.default_el_rad for g in G])[fwd]
        la = (mode == GimbalMode.LOOK_AT) | (mode == GimbalMode.LOOK_AT_AXIS)
        if la.any():
            li = np.flatnonzero(la)
            rows = np.fromiter((row_of[int(s)] for s in S_[li]), np.int64, li.size)
            pq = PQ[rows]
            Rb = quat_to_R(pq[:, 3:7])
            mt = np.stack([specs[i].mount_t for i in li])
            MR = np.stack([specs[i].mount_R for i in li])
            pm = pq[:, :3] + np.einsum("nij,nj->ni", Rb, mt)
            t = b["g_tgt"][S_[li], K_[li]].copy()
            axis = mode[li] == GimbalMode.LOOK_AT_AXIS
            t[axis, 2] = pm[axis, 2]
            dw = t - pm
            d = np.einsum("nji,nj->ni", MR, np.einsum("nji,nj->ni", Rb, dw))
            h = np.sqrt(d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1])
            nn = np.sqrt(h * h + d[:, 2] ** 2)
            e = np.arctan2(d[:, 2], h)
            a = np.arctan2(d[:, 1], d[:, 0])
            sing = h < 1e-3 * nn
            a[sing] = az[li][sing]
            az_t[li] = a
            el_t[li] = e
        az_c = np.minimum(np.maximum(az_t, az_min), az_max)
        el_c = np.minimum(np.maximum(el_t, el_min), el_max)
        lim = (az_c != az_t) | (el_c != el_t)
        self.stats["limited"] += int(lim.sum())
        step = rate * dt
        az = az + np.minimum(np.maximum(az_c - az, -step), step)
        el = el + np.minimum(np.maximum(el_c - el, -step), step)
        b["g_lim"][S_, K_] = lim
        done = ~la & (np.abs(az_c - az) <= EXIT_EPS) & (np.abs(el_c - el) <= EXIT_EPS)
        az[done] = az_c[done]
        el[done] = el_c[done]
        b["g_az"][S_, K_] = az
        b["g_el"][S_, K_] = el
        b["g_dyn"][S_[done], K_[done]] = False

    def _param_table(self) -> tuple:
        """按 (机型 rig, 云台列) 展开的参数表（行号 = rig·N_COLS + 列）：有无云台、限位与限速、挂载平移与旋转。
        rig 列表只追加（新机型装配时），按长度缓存。"""
        rl = self.rt.rig_list
        key = (id(rl), len(rl))
        if self._tab_key == key and self._tab is not None:
            return self._tab
        T = max(1, len(rl) * N_COLS)
        ok = np.zeros(T, np.bool_)
        f = np.zeros((T, KG.TAB_COLS))
        mt = np.zeros((T, 3))
        MR = np.zeros((T, 3, 3))
        for r_, rg in enumerate(rl):
            for k_ in range(N_COLS):
                sp = None if rg is None else rg.by_kind(KIND_OF_COL[k_])
                if sp is None or sp.gimbal is None:
                    continue
                i = r_ * N_COLS + k_
                g = sp.gimbal
                ok[i] = True
                f[i] = (g.az_min_rad, g.az_max_rad, g.el_min_rad, g.el_max_rad, g.rate_max_rad_s, g.default_el_rad)
                mt[i] = sp.mount_t
                MR[i] = sp.mount_R
        # 参数行逐位相同（且都有云台）的行对：同一机体两列输入相同时可沿用前一列的结果（gimbal_step_scan）
        eq = np.zeros((T, T), np.bool_)
        for i in range(T):
            for j in range(T):
                eq[i, j] = bool(ok[i] and ok[j] and f[i].tobytes() == f[j].tobytes() and mt[i].tobytes() == mt[j].tobytes()
                                and MR[i].tobytes() == MR[j].tobytes())
        self._tab_key, self._tab = key, (ok, f, mt, MR, eq)
        return self._tab

    def _step_nb(self, S: Any, S_: np.ndarray, K_: np.ndarray, dt: float) -> None:
        """`_step_vec` 的 numba 融合实现（kernels_gimbal.gimbal_step，逐位相同；N = 1000 时 2.8 ms → 约 0.1 ms）。"""
        b = self.rt.blk
        ok, f, mt, MR, _eq = self._param_table()
        rig = self.rt.slot_rig[S_].astype(np.int64)
        tab_i = np.where(rig >= 0, rig * N_COLS + K_, -1)
        n_lim = KG.gimbal_step(S_, K_, tab_i, ok, f, mt, MR, b["g_mode"], b["g_az"], b["g_el"], b["g_paz"], b["g_pel"],
                               b["g_tgt"], b["g_lim"], b["g_dyn"], S.enu.pos, S.enu.q_xyzw, float(dt), EXIT_EPS)
        self.stats["limited"] += int(n_lim)

    def _step_vec(self, S: Any, rows: np.ndarray, S_: np.ndarray, K_: np.ndarray, ri: np.ndarray, dt: float) -> None:
        """`_step_numpy` 的向量化等价实现：spec 参数按 (机型 rig, 云台列) 分组取（同组共用同一 spec），其余运算与
        `_step_numpy` 逐项相同（同一 numpy 表达式与次序）。"""
        b = self.rt.blk
        n = S_.size
        rig = self.rt.slot_rig[S_].astype(np.int64)
        key = rig * N_COLS + K_
        az_min = np.empty(n)
        az_max = np.empty(n)
        el_min = np.empty(n)
        el_max = np.empty(n)
        rate = np.empty(n)
        dflt_el = np.empty(n)
        mt = np.empty((n, 3))
        MR = np.empty((n, 3, 3))
        ok = np.zeros(n, np.bool_)
        for u in np.unique(key).tolist():
            m = key == u
            r_, k_ = divmod(int(u), N_COLS)
            rg = self.rt.rig_list[r_] if r_ >= 0 else None
            sp = None if rg is None else rg.by_kind(KIND_OF_COL[k_])
            if sp is None or sp.gimbal is None:
                continue
            g = sp.gimbal
            ok[m] = True
            az_min[m], az_max[m], el_min[m], el_max[m] = g.az_min_rad, g.az_max_rad, g.el_min_rad, g.el_max_rad
            rate[m], dflt_el[m] = g.rate_max_rad_s, g.default_el_rad
            mt[m] = sp.mount_t
            MR[m] = sp.mount_R
        if not ok.all():
            bad = ~ok
            b["g_dyn"][S_[bad], K_[bad]] = False
            S_, K_, ri = S_[ok], K_[ok], ri[ok]
            az_min, az_max, el_min, el_max, rate, dflt_el = (a[ok] for a in (az_min, az_max, el_min, el_max, rate, dflt_el))
            mt, MR = mt[ok], MR[ok]
            if S_.size == 0:
                return
        mode = b["g_mode"][S_, K_].astype(np.int64)
        az = b["g_az"][S_, K_].copy()
        el = b["g_el"][S_, K_].copy()
        az_t = b["g_paz"][S_, K_].copy()
        el_t = b["g_pel"][S_, K_].copy()
        nad = mode == GimbalMode.NADIR
        az_t[nad] = 0.0
        el_t[nad] = el_min[nad]
        fwd = mode == GimbalMode.FORWARD
        az_t[fwd] = 0.0
        el_t[fwd] = dflt_el[fwd]
        la = (mode == GimbalMode.LOOK_AT) | (mode == GimbalMode.LOOK_AT_AXIS)
        if la.any():
            li = np.flatnonzero(la)
            pq = S.enu.pose_enu_flu(rows)[ri[li]]
            Rb = quat_to_R(pq[:, 3:7])
            pm = pq[:, :3] + np.einsum("nij,nj->ni", Rb, mt[li])
            t = b["g_tgt"][S_[li], K_[li]].copy()
            axis = mode[li] == GimbalMode.LOOK_AT_AXIS
            t[axis, 2] = pm[axis, 2]
            dw = t - pm
            d = np.einsum("nji,nj->ni", MR[li], np.einsum("nji,nj->ni", Rb, dw))
            h = np.sqrt(d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1])
            nn = np.sqrt(h * h + d[:, 2] ** 2)
            e = np.arctan2(d[:, 2], h)
            a = np.arctan2(d[:, 1], d[:, 0])
            sing = h < 1e-3 * nn
            a[sing] = az[li][sing]
            az_t[li] = a
            el_t[li] = e
        az_c = np.minimum(np.maximum(az_t, az_min), az_max)
        el_c = np.minimum(np.maximum(el_t, el_min), el_max)
        lim = (az_c != az_t) | (el_c != el_t)
        self.stats["limited"] += int(lim.sum())
        step = rate * dt
        az = az + np.minimum(np.maximum(az_c - az, -step), step)
        el = el + np.minimum(np.maximum(el_c - el, -step), step)
        b["g_lim"][S_, K_] = lim
        done = ~la & (np.abs(az_c - az) <= EXIT_EPS) & (np.abs(el_c - el) <= EXIT_EPS)
        az[done] = az_c[done]
        el[done] = el_c[done]
        b["g_az"][S_, K_] = az
        b["g_el"][S_, K_] = el
        b["g_dyn"][S_[done], K_[done]] = False

    # ------------------------------------------------------------ 位姿
    def R_gimbal(self, slots: np.ndarray, k: int) -> np.ndarray:
        b = self.rt.blk
        return gimbal_R(b["g_az"][slots, k], b["g_el"][slots, k])


def _ctx_dict(mc: Any) -> dict:
    if mc is None:
        return {}
    if isinstance(mc, dict):
        return mc
    return {k: getattr(mc, k) for k in ("center_enu_m", "target_enu_m") if hasattr(mc, k)}
