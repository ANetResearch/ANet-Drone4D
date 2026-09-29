"""SafetyParams：M09 全部阈值与参数的冻结 dataclass（M09 §6.15；M09-FR-022、FR-112、FR-120）。

控制器档位 `mock_l1`（D1）与 `px4_mirror`（V0.2，pos_err 3.5 / 4.5 m）两套 FastGuard 取值，只加载一套；剧本覆盖只允许
白名单字段（预检档、`link.auto_resume`、`max_z_m`），FastGuard 阈值不可覆盖。`selfcheck()` 在 install 时执行
（ELAND 阈值 < FAILSAFE 阈值、LOW > CRIT > EMERG 等），失败时 sim-core 拒绝启动（M09-AC-031）。
时钟域：除 `LinkParams`（墙钟，暂停冻结）与 `escalation_min_interval_s`（墙钟）外均为仿真时间（M09 §7.5）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace
from typing import Any

__all__ = [
    "GUARD_PROFILES",
    "BatteryParams",
    "FenceParams",
    "FsmParams",
    "GuardParams",
    "LinkParams",
    "RtlParams",
    "SafetyParams",
    "SelfCheckError",
    "SepParams",
    "apply_overrides",
    "selfcheck",
]


class SelfCheckError(RuntimeError):
    """启动自检失败（M09-FR-112）：sim-core 以非 0 退出码拒绝启动，消息给出原因。"""


@dataclass(frozen=True, slots=True)
class GuardParams:
    tilt_kill_deg: float = 90.0
    tilt_eland_deg: float = 75.0
    tilt_err_deg: float = 20.0
    tilt_err_s: float = 0.5
    pe_eland_m: float = 3.0
    pe_s: float = 0.5
    pe_fail_m: float = 5.0
    thr_frac: float = 0.95
    sink_m: float = 0.5
    thr_s: float = 1.0
    yaw_err_deg: float = 90.0
    state_age_s: float = 0.1
    grace_s: float = 1.0
    track_dv_mps: float = 3.0
    track_s: float = 2.0

    @property
    def tilt_kill_rad(self) -> float:
        return math.radians(self.tilt_kill_deg)

    @property
    def tilt_eland_rad(self) -> float:
        return math.radians(self.tilt_eland_deg)

    @property
    def tilt_err_rad(self) -> float:
        return math.radians(self.tilt_err_deg)

    @property
    def yaw_err_rad(self) -> float:
        return math.radians(self.yaw_err_deg)


GUARD_PROFILES: dict[str, GuardParams] = {"mock_l1": GuardParams(), "px4_mirror": GuardParams(pe_eland_m=3.5, pe_fail_m=4.5)}


@dataclass(frozen=True, slots=True)
class FsmParams:
    preflight_mode: str = "demo"  # demo | realistic（realistic 为 V0.2）
    preflight_demo_s: float = 1.0
    preflight_realistic_s: float = 5.0
    ready_autodisarm_s: float = 10.0
    spoolup_s: float = 1.0
    landed_disarm_s: float = 2.0
    loc_lost_land_s: float = 30.0  # ext
    rejected_rate_s: float = 1.0  # SAF.FSM.REJECTED 每 slot 每秒至多 1 条【墙钟】
    takeoff_min_soc: float = 0.30
    still_mps: float = 0.3
    spawn_z_tol_m: float = 0.5

    @property
    def preflight_s(self) -> float:
        return self.preflight_realistic_s if self.preflight_mode == "realistic" else self.preflight_demo_s


@dataclass(frozen=True, slots=True)
class FenceParams:
    warn_margin_m: float = 5.0
    inset_m: float = 2.0
    correct_timeout_s: float = 15.0
    hard_out_m: float = 10.0
    restore_margin_m: float = 1.5
    near_rearm_m: float = 7.0
    near_rearm_s: float = 2.0
    max_z_offset_m: float = 0.25
    min_clear_m: float = 0.5
    min_clear_target_m: float = 0.75
    goal_clear_m: float = 2.0
    path_buffer_m: float = 1.0
    orbit_poly_segments: int = 16
    max_z_m: float | None = None  # 剧本覆盖（ext 覆盖块）；None 取 border
    target_clear_m: float = 2.0  # 回拉目标的最小净空（z ≥ dsm + 2 m，§6.7.4 第 3 条）


@dataclass(frozen=True, slots=True)
class BatteryParams:
    low: float = 0.15
    crit: float = 0.07
    emerg: float = 0.05
    rtl_margin: float = 1.3
    takeoff_min: float = 0.30
    p_avg_alpha: float = 0.02
    r_int_per_cell_ohm: float = 0.005
    feasible_reserve: float = 0.20  # ADR-036：返航落地后 ≥ 20%


@dataclass(frozen=True, slots=True)
class RtlParams:
    alt_m: float = 30.0
    top_margin_m: float = 5.0
    v_cruise_cap_mps: float = 5.0
    v_up_est: float = 2.0
    v_dn_est: float = 1.5
    v_land_est: float = 0.7
    v_final: float = 1.0
    descend_alt_m: float = 10.0  # FINAL 段起点（home 上方 10 m，12 §4.4.6）
    cruise_done_m: float = 2.0  # 水平距 home < 2 m 进入 DESCEND
    climb_tol_m: float = 0.5
    refresh_hz: float = 1.0
    refresh_move_m: float = 50.0
    # H_top 走廊采样容差（M04 heightmap_top_along 的 tol_m）。M04 缺省 100 m 是粗校验口径：金字塔格 ≥ 100 m 时，
    # 紧贴超高层的短返航线也会取到塔顶（S1 入场点 z_rtl 389 m、t_rtl 326 s，扫描中段误触 ENERGY_RTL）。20 m 仍取膨胀后
    # 金字塔的上包络（保守），对 3 km 返航线约 150 个采样。INT-1 修订。
    h_top_tol_m: float = 20.0


@dataclass(frozen=True, slots=True)
class LinkParams:
    warn_s: float = 1.5
    hold_s: float = 3.0
    rtl_s: float = 13.0
    auto_resume: bool = True
    watchdog_ms: int = 250


@dataclass(frozen=True, slots=True)
class SepParams:
    warn_m: float = 10.0
    rearm_m: float = 12.0
    rearm_s: float = 2.0
    cpa_horizon_s: float = 3.0
    min_sep_m: float = 3.0
    recover_m: float = 5.0
    recover_s: float = 2.0
    osc_window_s: float = 60.0
    osc_count: int = 3
    cell_m: float = 10.0
    n_shards: int = 4
    zband_m: float = 10.0
    sweep_s: float = 0.1  # 扫掠区间 = 一个 10 Hz 周期


@dataclass(frozen=True, slots=True)
class SafetyParams:
    controller: str = "mock_l1"
    guard: GuardParams = field(default_factory=GuardParams)
    fsm: FsmParams = field(default_factory=FsmParams)
    fence: FenceParams = field(default_factory=FenceParams)
    battery: BatteryParams = field(default_factory=BatteryParams)
    rtl: RtlParams = field(default_factory=RtlParams)
    link: LinkParams = field(default_factory=LinkParams)
    sep: SepParams = field(default_factory=SepParams)
    escalation_min_interval_s: float = 2.0  # ext【墙钟】
    escalation_levels: tuple[str, ...] = ("HOLD", "ELAND")  # ext；含 FAILSAFE 时开放第 3 级
    omega_fail_rad_s: float = 4.0  # ext：motor_fail 的不可控滚转角速度

    @classmethod
    def for_controller(cls, controller: str = "mock_l1") -> SafetyParams:
        if controller not in GUARD_PROFILES:
            raise SelfCheckError(f"unknown controller profile {controller!r} (expected one of {sorted(GUARD_PROFILES)})")
        return cls(controller=controller, guard=GUARD_PROFILES[controller])


# 剧本 `safety` 覆盖块的白名单（M09-FR-120；§14 第 8 条）：键 -> (组, 字段)
OVERRIDE_KEYS: dict[str, tuple[str, str]] = {
    "preflight_mode": ("fsm", "preflight_mode"),
    "link.auto_resume": ("link", "auto_resume"),
    "max_z_m": ("fence", "max_z_m"),
}


def apply_overrides(p: SafetyParams, over: dict[str, Any] | None) -> SafetyParams:
    """按白名单应用剧本覆盖；白名单以外的键（包括 FastGuard 阈值）一律拒绝（SelfCheckError）。"""
    if not over:
        return p
    flat: dict[str, Any] = {}
    for k, v in over.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                flat[f"{k}.{k2}"] = v2
        else:
            flat[k] = v
    out = p
    for k, v in flat.items():
        if k not in OVERRIDE_KEYS:
            raise SelfCheckError(f"safety override {k!r} is not allowed (allowed: {sorted(OVERRIDE_KEYS)})")
        grp, name = OVERRIDE_KEYS[k]
        out = replace(out, **{grp: replace(getattr(out, grp), **{name: v})})
    selfcheck(out)
    return out


def selfcheck(p: SafetyParams) -> None:
    """阈值自洽检查（M09-FR-112）；失败抛 SelfCheckError（消息列出全部问题）。"""
    errs: list[str] = []
    g = p.guard
    if p.controller not in GUARD_PROFILES:
        errs.append(f"controller {p.controller!r} unknown")
    if not g.pe_eland_m < g.pe_fail_m:
        errs.append(f"pe_eland_m {g.pe_eland_m} must be < pe_fail_m {g.pe_fail_m}")
    if not g.tilt_eland_deg < g.tilt_kill_deg:
        errs.append(f"tilt_eland_deg {g.tilt_eland_deg} must be < tilt_kill_deg {g.tilt_kill_deg}")
    if not 0 < g.thr_frac <= 1:
        errs.append(f"thr_frac {g.thr_frac} must be in (0, 1]")
    b = p.battery
    if not b.low > b.crit > b.emerg >= 0:
        errs.append(f"battery thresholds must satisfy low > crit > emerg >= 0 (got {b.low}, {b.crit}, {b.emerg})")
    if not b.rtl_margin >= 1.0:
        errs.append(f"rtl_margin {b.rtl_margin} must be >= 1")
    if not b.takeoff_min > b.low:
        errs.append(f"takeoff_min {b.takeoff_min} must be > low {b.low}")
    lk = p.link
    if not 0 < lk.warn_s < lk.hold_s < lk.rtl_s:
        errs.append(f"link thresholds must satisfy 0 < warn < hold < rtl (got {lk.warn_s}, {lk.hold_s}, {lk.rtl_s})")
    f = p.fence
    if not 0 < f.restore_margin_m <= f.inset_m < f.warn_margin_m < f.near_rearm_m:
        errs.append("fence margins must satisfy 0 < restore_margin <= inset < warn_margin < near_rearm")
    if not f.min_clear_m < f.min_clear_target_m:
        errs.append("min_clear_m must be < min_clear_target_m")
    s = p.sep
    if not 0 < s.min_sep_m < s.recover_m < s.warn_m < s.rearm_m:
        errs.append("separation thresholds must satisfy 0 < min_sep < recover < warn < rearm")
    if s.n_shards != 4:
        errs.append("fleet_guard n_shards must be 4 (M08 shard convention, phases 4/9/14/19)")
    if p.fsm.preflight_mode not in ("demo", "realistic"):
        errs.append(f"preflight_mode {p.fsm.preflight_mode!r} must be demo or realistic")
    for grp in (g, p.fsm, p.fence, p.battery, p.rtl, p.link, p.sep):
        for fl in fields(grp):
            v = getattr(grp, fl.name)
            if isinstance(v, float) and not math.isfinite(v):
                errs.append(f"{type(grp).__name__}.{fl.name} is not finite")
    if errs:
        raise SelfCheckError("M09 selfcheck failed: " + "; ".join(errs))
