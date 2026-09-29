"""numba 融合核与 numpy oracle 两项对拍（M08-AC-007；D1-AC-07；ADR-021；M08-FR-038 至 FR-041）。

① 单步等价：50 组随机但物理合理的状态（覆盖全部运动模式与相位、三种机型、三种限速配置、风、路径与绕飞参数）各走一步，
   逐字段相对误差 ≤ 1e-12；contact 核与其 numpy oracle 同样单步对拍（DSM/DTM 栅格）。
② 自由运行：100 架随机 GOTO、PATH、ORBIT 混合，自由运行 10 s（2500 tick），位置差 ≤ 1e-6 m、速度差 ≤ 1e-6 m/s，测试用
   FastGuard 事件序列（tick、slot、code）完全一致。
"""

from __future__ import annotations

import copy

import numpy as np
import pytest
from simlib import FastGuard

from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.fleet import FleetConfig, FleetSim
from awr.sim.fleet.path import PathBuffer, topp_lite
from awr.sim.fleet.pipeline import StageCtx
from awr.sim.fleet.profiles import ProfileTable
from awr.sim.fleet.stages import registry as R
from awr.sim.fleet.stages.contact import ContactCfg, contact_numpy
from awr.sim.fleet.stages.l1 import L1Oracle, prepare_inputs, rtl_phase_of
from awr.sim.fleet.state import FleetState

pytestmark = pytest.mark.skipif(not K.HAVE_NUMBA, reason="numba 不可用")

T = ProfileTable()
FIELDS_F = ["p", "v", "a_meas", "p_prev", "q", "omega", "thrust", "thr_sp", "q_sp", "vel_int", "tr_x", "tr_v", "tr_a",
            "pos_ref", "yaw_sp", "orb", "axis_anchor", "path_tau", "target", "land_xy", "thr_cap", "td_t", "force"]
FIELDS_I = ["ctrl_mode", "ctrl_phase", "mode_evt", "stopping", "axis_lock", "path_seg"]
MODES = np.array([3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 13, 0, 15])


def random_state(rng: np.random.Generator, n: int = 120) -> tuple[FleetState, PathBuffer]:
    S = FleetState(256)
    PB = PathBuffer(8192)
    S.active[:n] = True
    S.fidelity[:n] = 1
    S.profile_id[:n] = rng.integers(0, len(T.ids), n)
    S.limits_id[:n] = rng.integers(0, len(T.limit_ids), n)
    S.p[:n] = rng.uniform(-50, 50, (n, 3))
    S.p[:n, 2] = -rng.uniform(5, 60, n)
    S.v[:n] = rng.normal(0, 2, (n, 3))
    S.a_meas[:n] = rng.normal(0, 1, (n, 3))
    q = rng.normal(0, 0.1, (n, 4))
    q[:, 0] = 1.0
    q /= np.linalg.norm(q, axis=1)[:, None]
    S.q[:n] = q
    S.q_sp[:n] = q
    S.thrust[:n] = rng.uniform(0.3, 0.7, n)
    S.tr_x[:n] = S.p[:n] + rng.normal(0, 1, (n, 3))
    S.tr_v[:n] = rng.normal(0, 1, (n, 3))
    S.tr_a[:n] = rng.normal(0, 0.5, (n, 3))
    S.target[:n] = S.p[:n] + rng.uniform(-80, 80, (n, 3))
    S.vel_int[:n] = rng.normal(0, 0.3, (n, 3))
    S.wind[:n] = rng.normal(0, 4, (n, 3))
    S.rho[:n] = rng.uniform(1.0, 1.25, n).astype(np.float32)
    S.yaw_sp[:n] = rng.uniform(-3, 3, n)
    S.ctrl_mode[:n] = MODES[rng.integers(0, len(MODES), n)]
    S.ctrl_phase[:n] = rng.integers(0, 3, n)
    S.landed[:n] = rng.random(n) < 0.05
    S.in_contact[:n] = rng.random(n) < 0.1
    S.agl[:n] = -S.p[:n, 2]
    S.ground_z[:n] = 0.0
    S.land_xy[:n] = S.p[:n, :2]
    S.z_rtl[:n] = 60.0
    S.stopping[:n] = rng.random(n) < 0.3
    S.speed_cmd[:n] = np.where(rng.random(n) < 0.5, np.nan, 4.0)
    S.mode_t[:n] = rng.uniform(0, 5, n)
    S.thr_cap[:n] = rng.uniform(0.2, 1.0, n).astype(np.float32)
    S.td_t[:n] = np.where(rng.random(n) < 0.5, np.nan, rng.uniform(0, 5, n))
    S.thrust_scale[:n] = np.where(rng.random(n) < 0.1, 0.55, 1.0).astype(np.float32)
    S.motor_ok[:n] = np.where(rng.random(n) < 0.05, 0x0E, 0xFF).astype(np.uint8)
    S.orb[:n, 0:3] = S.p[:n] + rng.uniform(-20, 20, (n, 3))
    S.orb[:n, 3] = rng.uniform(5, 30, n)
    S.orb[:n, 4] = rng.uniform(-0.2, 0.2, n)
    S.orb[:n, 5] = rng.uniform(-3, 3, n)
    S.orb[:n, 7] = np.where(rng.random(n) < 0.5, 0.0, 1.0)
    S.orb[:n, 6] = rng.uniform(0, 1.2, n)
    S.orb[:n, 8] = 4.0
    S.orb[:n, 9] = np.where(rng.random(n) < 0.5, 1.0, -1.0)
    S.orb[:n, 10] = rng.integers(0, 3, n)
    S.vel_cmd[:n] = rng.normal(0, 2, (n, 3))
    S.vel_cmd[:n] *= (rng.random((n, 3)) > 0.3)
    S.vel_frame[:n] = rng.integers(0, 2, n)
    S.hold_alt[:n] = rng.random(n) < 0.7
    S.axis_lock[:n] = rng.integers(0, 8, n)
    S.axis_anchor[:n] = S.p[:n] + rng.normal(0, 0.1, (n, 3))
    S.d_free[:n] = rng.uniform(1, 100, n)
    S.vel_vmax[:n] = np.where(rng.random(n) < 0.5, np.inf, 2.0)
    S.vel_yawrate[:n] = rng.normal(0, 0.2, n)
    S.desc_v[:n] = rng.uniform(0.5, 1.0, n)
    for i in range(n):
        if S.ctrl_mode[i] == 4:
            w = np.vstack([S.p[i], S.p[i] + rng.uniform(-40, 40, (int(rng.integers(2, 6)), 3))])
            seg, vw = topp_lite(w, 1.0, 5.0, 3.0)
            off = PB.alloc(PB.rows_for(len(w)))
            yw = np.where(rng.random(len(w)) < 0.3, rng.uniform(-3, 3, len(w)), np.nan)
            PB.write(off, w, yw, seg, vw)
            S.path_off[i], S.path_len[i] = off, len(seg)
            S.path_seg[i] = off + int(rng.integers(0, len(seg)))  # 含圆弧段
            S.path_tau[i] = PB.seg[S.path_seg[i], K.SG_T0] + rng.uniform(0, 3)
    return S, PB


class FakeWorld:
    """M04 `free_distance` 的替身：按方向给出确定的自由距离（覆盖 Velocity 方向限速分支）。"""

    def free_distance(self, origins, dirs, max_m):
        return np.abs(np.asarray(dirs)[:, 0]) * 6.0 + 1.5


def _rel(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    both_nan = np.isnan(a) & np.isnan(b)
    d = np.where(both_nan, 0.0, np.abs(a - b) / np.maximum(np.abs(a), 1.0))
    return float(np.nan_to_num(d, nan=1.0).max()) if d.size else 0.0


@pytest.mark.parametrize("seed", range(50))
def test_single_step_equivalence(seed: int) -> None:
    rng = np.random.default_rng(1000 + seed)
    S1, PB = random_state(rng)
    S2 = copy.deepcopy(S1)
    PB2 = copy.deepcopy(PB)
    ctx = StageCtx(tick=100 + seed, t_ns=(100 + seed) * 4_000_000)
    idx = np.flatnonzero(S1.active).astype(np.int32)
    prepare_inputs(S1, idx, FakeWorld())  # L1Stage 在调用融合核前执行（Velocity 方向自由距离）
    K.run_l1(S1, T.PT, T.LT, PB, idx, 0.008, ctx.t_ns * 1e-9, 1.0, 4.0, 1.0, rtl_phase_of(S1))
    L1Oracle(T, PB2, every=2, w_fail=4.0, world=FakeWorld()).run_all(S2, ctx)
    n = int(S1.active.sum())
    worst = {f: _rel(getattr(S1, f)[:n], getattr(S2, f)[:n]) for f in FIELDS_F if f != "force"}
    bad = {f: v for f, v in worst.items() if v > 1e-12}
    assert not bad, bad
    for f in FIELDS_I:
        assert np.array_equal(getattr(S1, f)[:n], getattr(S2, f)[:n]), f


@pytest.mark.parametrize("seed", range(10))
def test_contact_equivalence(seed: int) -> None:
    from awr.sim.fleet.kernels_contact import contact

    rng = np.random.default_rng(seed)
    S1, _ = random_state(rng, 100)
    S1.p[:100, 2] = -rng.uniform(-0.5, 3.0, 100)  # 地表附近：夹持、撞墙、硬着陆、触地
    S1.p_prev[:100] = S1.p[:100] + rng.normal(0, 0.5, (100, 3))
    S1.v[:100, 2] = rng.uniform(-1, 5, 100)
    S1.contact_t[:100] = np.where(rng.random(100) < 0.5, np.nan, rng.uniform(0, 2, 100))
    S2 = copy.deepcopy(S1)
    dsm = (rng.random((80, 80)) * 4.0).astype(np.float32)
    dsm[20:40, 20:40] += 30.0
    dtm = (rng.random((20, 20)) * 1.0).astype(np.float32)
    aff = np.array([-80.0, -80.0, 2.0])
    aff2 = np.array([-100.0, -100.0, 10.0])
    prm = ContactCfg().prm()
    rs = np.where(rng.random(S1.capacity) < 0.5, 3, 0).astype(np.uint8)
    idx = np.flatnonzero(S1.active).astype(np.int32)
    e1 = np.zeros((256, 2), np.int32)
    e2 = np.zeros((256, 2), np.int32)
    n1 = contact(idx, 3.0, S1.p, S1.v, S1.p_prev, S1.q, S1.omega, S1.thrust, S1.thr_sp, S1.thr_cap, S1.home, dsm, aff, dtm,
                 aff2, 0.0, S1.ctrl_mode, S1.ctrl_phase, S1.mode_t, S1.td_t, S1.profile_id, T.PT, S1.in_contact, S1.landed,
                 S1.in_air, S1.contact_t, S1.crash_sub, S1.ground_z, S1.agl, S1.mode_evt, rs, prm, e1)
    n2 = contact_numpy(S2, idx, 3.0, dsm, aff, dtm, aff2, 0.0, T.PT, rs, prm, e2)
    assert n1 == n2 and np.array_equal(e1[:n1], e2[:n2])
    for f in ("p", "v", "q", "omega", "ground_z", "agl", "contact_t", "thrust", "thr_sp", "thr_cap", "td_t"):
        assert _rel(getattr(S1, f)[:100], getattr(S2, f)[:100]) <= 1e-12, f
    for f in ("in_contact", "landed", "in_air", "crash_sub", "ctrl_mode", "ctrl_phase", "mode_evt"):
        assert np.array_equal(getattr(S1, f)[:100], getattr(S2, f)[:100]), f


def _mixed_fleet(kernel: str) -> tuple[FleetSim, StageCtx, R.Registry]:
    from awr.sim.backends.base import EntitySpec, Kind
    from awr.sim.fleet import actions as ACT

    cm = R.isolated_registry()
    reg = cm.__enter__()
    f = FleetSim(FleetConfig(kernel=kernel, path_capacity=65536), reg=reg)
    f.build_pipeline()
    rng = np.random.default_rng(42)
    S = f.S
    for k in range(100):
        f.add(EntitySpec(f"u{k}", Kind.UAV, T.ids[k % len(T.ids)], None,
                         (float(k % 10) * 40.0, float(k // 10) * 40.0, 0.0), 0.0), slot=k, agent_no=k, entity_id=f"u{k}")
        S.lifecycle[k] = 4
    S.p[:100, 2] = -30.0
    S.landed[:100] = False
    S.in_air[:100] = True
    S.in_contact[:100] = False
    S.thrust[:100] = T.PT[S.profile_id[:100], K.P_HOVER]
    for k in range(100):
        goal = S.p[k] + np.r_[rng.uniform(-30, 30, 2), rng.uniform(-5, 5)]
        if k % 3 == 0:
            ACT.begin_goto(S, [k], goal, np.nan, None, 0.0)
        elif k % 3 == 1:
            w = np.vstack([S.p[k] + rng.uniform(-25, 25, (4, 3))])
            w[:, 2] = -30.0 + rng.uniform(-3, 3, 4)
            ACT.begin_path(S, f.PB, T, k, w, float("nan"), None, 0.0)
        else:
            ACT.begin_orbit(S, [k], S.p[k] + np.array([12.0, 0.0, 0.0]), 12.0, 3.0, bool(k % 2), 0.0, "center", None, 0.0)
    S.set_wind_from_enu(rng.normal(0, 3, (100, 3)), np.arange(100))
    ctx = StageCtx(profiles=f.T, paths=f.PB)
    f._cm = cm  # 保持隔离登记表上下文
    return f, ctx, reg


def test_free_run_100_drones_10s() -> None:
    fa, ca, _ = _mixed_fleet("numba")
    fb, cb, _ = _mixed_fleet("numpy")
    ga, gb = FastGuard(100), FastGuard(100)
    try:
        for tick in range(2500):
            fa.step(ca, 1)
            fb.step(cb, 1)
            if tick % 5 == 1:
                ga(fa.S, ca.t_ns * 1e-9)
                gb(fb.S, cb.t_ns * 1e-9)
        dp = float(np.abs(fa.S.p[:100] - fb.S.p[:100]).max())
        dv = float(np.abs(fa.S.v[:100] - fb.S.v[:100]).max())
        assert dp <= 1e-6, dp
        assert dv <= 1e-6, dv
        assert ga.events == gb.events
        assert np.array_equal(fa.S.ctrl_mode[:100], fb.S.ctrl_mode[:100])
    finally:
        fa._cm.__exit__(None, None, None)
        fb._cm.__exit__(None, None, None)
