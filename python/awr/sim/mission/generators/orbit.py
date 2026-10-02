"""`orbit` 生成器（M10-FR-021；M10 §6.5.9、§6.5.10；AWR-12 §7.4 ladder）。

环绕原语：中心 `center_enu_m`（缺省或 "home" 取该机 home，ladder 的 `vehicle_sets[].mission.center = "home"`）、
半径、高度（`z_m` 或 `agl_m`）、速度、圈数（0 为持续）、方向与航向（center、tangent、fixed）。云台俯角
`θ = atan2(h − h_target/2, r)`。约束 `v²/R ≤ 3 m/s²`（M08 第 ⑥ 步也会检查）。环绕段由运动提供者 `m10.orbit` 以
解析原语执行；入圆段能被粗校验证明无障碍时直线切入，否则 safe_transit（§6.5.9）。
"""

from __future__ import annotations

import math

import numpy as np

from .common import GenContext, GenError, GenOutput, ItemDraft, z_from

__all__ = ["YAW_RATE_FRAC", "run"]

YAW_RATE_FRAC = 0.9  # 航向朝心环绕的偏航角速度需求上限（相对机体自动模式上限）


def run(params: dict, ctx: GenContext) -> GenOutput:
    try:
        R = float(params["radius_m"])
    except (KeyError, TypeError, ValueError):
        raise GenError(110, "/params/radius_m") from None
    if not 1.0 <= R <= 1000.0:
        raise GenError(110, "/params/radius_m")
    turns = float(params.get("turns", 0) or 0)
    cw = bool(params.get("cw", True))
    yaw_b = str(params.get("yaw", "center"))
    out = GenOutput(items={})
    for v in ctx.vehicles:
        cxy = params.get("center_enu_m")
        c2 = np.asarray(v.home_enu_m, np.float64)[:2] if cxy in (None, "home") else np.asarray(cxy, np.float64)[:2]
        z = z_from(params, ctx.world, c2, default_agl=60.0)
        if params.get("z_m") is None and params.get("agl_m") is not None and ctx.world is None:
            z = float(v.home_enu_m[2]) + float(params["agl_m"])
        if ctx.world is not None:   # 圆周逐段抬升到 Height_map 以上（FR-027）
            th = np.linspace(0.0, 2.0 * math.pi, 25)
            ring = np.c_[c2[0] + R * np.cos(th), c2[1] + R * np.sin(th)]
            try:
                top = float(np.max(ctx.world.heightmap_top_along(ring[:-1], ring[1:], exact=True)))
            except Exception:
                top = -math.inf
            if top > z:
                out.warnings.append("ALT_RAISED")
                z = top
        speed = min(float(params.get("speed_mps") or v.cruise_mps), v.v_limit_mps, math.sqrt(3.0 * R))
        if params.get("speed_mps") and float(params["speed_mps"]) ** 2 / R > 3.0 + 1e-9:
            raise GenError(110, "/params/speed_mps", "降低速度或增大半径（v²/R ≤ 3 m/s²）")
        if yaw_b == "center" and speed / R > YAW_RATE_FRAC * v.yawrate_max_rad_s + 1e-9:
            # 航向朝心时偏航角速度需求 v/R 超过机体上限会累积航向误差（ADR-062：ladder 的 3 m、2 m/s 需要 38°/s，P600 为 30°/s）
            raise GenError(110, "/params/speed_mps", f"航向朝心的环绕需要偏航角速度 {math.degrees(speed / R):.0f}°/s，超过机体上限 "
                           f"{math.degrees(v.yawrate_max_rad_s):.0f}°/s 的 {YAW_RATE_FRAC:.0%}；降低速度、增大半径或改用 yaw = tangent")
        center = [float(c2[0]), float(c2[1]), float(z)]
        th_h = params.get("target_h_m")
        pitch = None
        if th_h is not None and ctx.world is not None:
            g = float(ctx.world.ground_dtm(c2.reshape(1, 2))[0])
            pitch = -math.atan2((z - g) - float(th_h) / 2.0, R)
        p = np.asarray(v.pos_enu_m, np.float64)
        d = p[:2] - c2
        th0 = math.atan2(d[1], d[0]) if float(np.hypot(*d)) > 0.5 else 0.0
        gimbal = {"mode": "look_at", "p_enu_m": center if th_h is None else [center[0], center[1], center[2] - float(th_h) / 2],
                  "pitch_rad": pitch}
        item = ItemDraft("orbit", "orbit", orbit={"center_enu_m": center, "radius_m": R, "turns": turns, "cw": cw,
                                                  "speed_mps": speed, "yaw_behavior": yaw_b, "theta0_rad": th0},
                         speed_mps=speed, yaw={"mode": "center" if yaw_b == "center" else yaw_b}, gimbal=gimbal,
                         meta={"center_enu_m": center, "radius_m": R})
        out.items[v.vehicle_id] = [item]
        out.stats[v.vehicle_id] = {"len_m": round(2 * math.pi * R * turns, 1), "speed_mps": round(speed, 3)}
    return out
