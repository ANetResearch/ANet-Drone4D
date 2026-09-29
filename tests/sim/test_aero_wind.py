"""气动与风（M08-AC-011、AC-012；M08-FR-032 至 FR-035；ADR-024）。

- 风致倾角：悬停中逆风阶跃（来自北方，机头朝北），稳态俯仰：x500 composite 4/8/12/14 m/s 为 3.38/7.85/13.31/16.36° ± 0.3°；
  P600 8 m/s 为 7.85° ± 0.3°；
- 密度：rho 降低 10% 时悬停推力比为 1/0.9（± 0.5%）；
- 风只经相对空速：同一状态下"机体 v = 5 m/s、无风"与"机体静止、风 −5 m/s"的气动合力差 ≤ 1e-12 N（oracle 与融合核）；
- `set_wind_from_enu()` 写入的 NED 风与 M02 golden（`packages/contracts/golden/frames/enu_ned.json`）满足轴置换。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from simlib import Rig, quat_to_rpy

from awr.sim.fleet import actions as ACT
from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.aero import aero
from awr.sim.fleet.profiles import ProfileTable
from awr.sim.fleet.state import FleetState

ROOT = Path(__file__).resolve().parents[2]


def _steady_pitch(profile: str, winds: list[float]) -> list[float]:
    n = len(winds)
    rig = Rig(profile, n, spacing=300.0)
    try:
        rig.air(np.arange(n), 20.0, 3.0)
        S = rig.S
        ACT.begin_goto(S, np.arange(n), S.p[:n].copy(), np.nan, None, rig.t)
        S.set_wind_from_enu(np.array([[0.0, -w, 0.0] for w in winds]), np.arange(n))
        rig.step_s(12.0)
        acc = np.zeros(n)
        m = 0
        for _ in range(4 * 25):
            rig.step(10)
            acc += quat_to_rpy(S.q[:n])[:, 1]
            m += 1
        return [abs(math.degrees(x / m)) for x in acc]
    finally:
        rig.close()


def test_wind_tilt_x500_composite() -> None:
    got = _steady_pitch("x500", [4.0, 8.0, 12.0, 14.0])
    for g, want in zip(got, [3.38, 7.85, 13.31, 16.36], strict=True):
        assert abs(g - want) <= 0.3, (got, want)


def test_wind_tilt_p600() -> None:
    (g,) = _steady_pitch("p600_mid360", [8.0])
    assert abs(g - 7.85) <= 0.3, g


def test_rho_hover_thrust_ratio() -> None:
    rig = Rig("x500", 2, spacing=300.0)
    try:
        S = rig.S
        S.set_rho(np.array([K.RHO0, 0.9 * K.RHO0]), np.array([0, 1]))
        rig.air([0, 1], 20.0, 8.0)
        thr = np.zeros(2)
        for _ in range(100):
            rig.step(5)
            thr += S.thrust[:2]
        assert abs((thr[1] / thr[0]) / (1 / 0.9) - 1) <= 0.005
    finally:
        rig.close()


def _state_pair() -> tuple[FleetState, ProfileTable]:
    T = ProfileTable()
    S = FleetState(8)
    rng = np.random.default_rng(5)
    for i in range(2):
        S.active[i] = True
        S.profile_id[i] = T.index("x500")
    q = rng.normal(size=4)
    S.q[:2] = q / np.linalg.norm(q)
    S.thrust[:2] = 0.61
    S.v[0] = (5.0, 0.0, 0.0)
    S.wind[1] = (-5.0, 0.0, 0.0)
    return S, T


def test_relative_airspeed_oracle() -> None:
    S, T = _state_pair()
    aero(S, T.PT, np.array([0, 1]))
    assert np.abs(S.force[0] - S.force[1]).max() <= 1e-12
    S.wind[1] = 0.0  # 反例：静止无风时气动力不同
    aero(S, T.PT, np.array([0, 1]))
    assert np.abs(S.force[0] - S.force[1]).max() > 1e-3


def test_relative_airspeed_kernel() -> None:
    """融合核：两机仅 v 与风不同（开环 SPOOLUP 推力相同），一步后加速度（合力 / 质量）一致。"""
    if not K.HAVE_NUMBA:
        pytest.skip("numba 不可用")
    rig = Rig("x500", 2, spacing=300.0)
    try:
        S = rig.S
        rig.air([0, 1], 20.0, 1.0)
        S.v[:2] = 0.0
        S.v[0] = (5.0, 0.0, 0.0)
        S.wind[:2] = 0.0
        S.wind[1] = (-5.0, 0.0, 0.0)
        S.q[1] = S.q[0]
        S.omega[1] = S.omega[0]
        S.thrust[1] = S.thrust[0]
        S.ctrl_mode[:2] = K.M_SPOOLUP
        S.mode_t[:2] = rig.t
        rig.step(2)
        m = rig.T.PT[S.profile_id[0], K.P_MASS]
        assert np.abs(S.a_meas[0] - S.a_meas[1]).max() * m <= 1e-12
    finally:
        rig.close()


def test_set_wind_from_enu_matches_m02_golden() -> None:
    g = json.loads((ROOT / "packages/contracts/golden/frames/enu_ned.json").read_text(encoding="utf-8"))
    cases = [c for c in g["cases"] if c["fn"] == "enu_to_ned"]
    S = FleetState(len(cases))
    w = np.array([c["args"]["v"] for c in cases])
    S.set_wind_from_enu(w)
    exp = np.array([c["out"]["v"] for c in cases])
    tol = g["tolerance"]
    assert (np.abs(S.wind[:len(cases)] - exp) <= tol["atol"]["velocity_mps"] + tol["rtol"] * np.abs(exp)).all()
    S2 = FleetState(4)
    S2.set_wind_from_enu(np.array([[3.0, 4.0, 1.0]]), np.array([2]))
    assert S2.wind[2].tolist() == [4.0, 3.0, -1.0]
