"""contact（M08-AC-018；M08-FR-036、FR-037；M08 §6.5.5）。

世界替身 `GridWorld`：DSM（柱体最近格）与 DTM（格心双线性）只读栅格，地面高 2.345 m（非整数，检验夹持精度），东侧
x_e ∈ [40, 60)、y_n ∈ [−10, 10) 为 30 m 高建筑。numba 与 numpy 两种内核各跑一遍：
- 以 1 m/s 下降到屋顶：`landed` 且无坠毁；
- 以 3 m/s 水平撞向立面：越过立面栅格后 ≤ 1 个 L1 tick 判 COLLISION_WORLD；
- 20 m 高 kill：落地判 IMPACT；
- 地面静止时 z 与 DSM 相差 ≤ 1e-9 m；
- 两机对撞判 COLLISION_UAV。
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from simlib import Rig

from awr.sim.fleet import actions as ACT
from awr.sim.fleet import kernels_contact as KC
from awr.sim.fleet import kernels_l1 as K

GROUND = 2.345
X0, Y0, CELL = -100.0, -100.0, 1.0


class GridWorld:
    def __init__(self) -> None:
        a = np.full((300, 300), GROUND, np.float32)
        c0, c1 = int((40 - X0) / CELL), int((60 - X0) / CELL)
        r0, r1 = int((-10 - Y0) / CELL), int((10 - Y0) / CELL)
        a[r0:r1, c0:c1] = GROUND + 30.0
        self._dsm = SimpleNamespace(a=a, x0_m=X0, y0_m=Y0, cell_m=CELL)
        self._dtm = SimpleNamespace(a=np.full((30, 30), GROUND, np.float32), x0_m=X0, y0_m=Y0, cell_m=10.0)

    def dsm_grid(self):
        return self._dsm

    def dtm_grid(self):
        return self._dtm


KERNELS = ["numba", "numpy"] if K.HAVE_NUMBA else ["numpy"]


def _rig(kernel: str, n: int = 1, spacing: float = 50.0) -> Rig:
    return Rig("x500", n, kernel=kernel, world=GridWorld(), spacing=spacing)


def _collisions(rig: Rig) -> list[tuple[float, str, str]]:
    return [(kw["t_sim_ns"] * 1e-9, kw["uav"], kw["fields"]["kind"]) for k, kw in rig.events.items
            if k == "sim.contact.collision"]


@pytest.mark.parametrize("kernel", KERNELS)
def test_ground_pin_matches_dsm(kernel: str) -> None:
    rig = _rig(kernel)
    try:
        rig.step_s(1.0)
        S = rig.S
        assert S.landed[0] and S.crash_sub[0] == 0
        assert abs(-S.p[0, 2] - float(np.float32(GROUND))) <= 1e-9
    finally:
        rig.close()


@pytest.mark.parametrize("kernel", KERNELS)
def test_roof_landing_1mps(kernel: str) -> None:
    rig = _rig(kernel, 2)  # slot 1 出生在 (50, 0)：屋顶
    try:
        S = rig.S
        rig.air(1, GROUND + 40.0, 3.0)
        ACT.begin_failsafe(S, [1], 1.0, rig.t)  # 冻结参考、垂直前馈 1 m/s
        vmax = 0.0
        for _ in range(30 * 125):
            rig.step(2)
            vmax = max(vmax, float(S.v[1, 2]))
            if S.landed[1]:
                break
        assert S.landed[1] and S.crash_sub[1] == 0, (S.landed[1], S.crash_sub[1])
        assert 0.8 <= vmax <= 1.3
        assert abs(-S.p[1, 2] - (GROUND + 30.0)) < 1e-4
        assert _collisions(rig) == []
    finally:
        rig.close()


@pytest.mark.parametrize("kernel", KERNELS)
def test_facade_collision_within_one_l1_tick(kernel: str) -> None:
    rig = _rig(kernel)
    try:
        S = rig.S
        rig.air(0, GROUND + 10.0, 3.0)
        ACT.begin_goto(S, [0], S.p[0] + np.array([0.0, 80.0, 0.0]), 3.0, None, rig.t)
        dt_l1 = 2 * 0.004
        t_cross = None
        for _ in range(60 * 250):
            x_prev = float(S.p[0, 1])
            rig.step(1)
            if t_cross is None and x_prev + float(S.v[0, 1]) * dt_l1 >= 40.0:
                t_cross = rig.t  # 下一个 L1 积分将越过立面格
            if _collisions(rig):
                break
        ev = _collisions(rig)
        assert ev and ev[0][2] == "COLLISION_WORLD", ev
        assert t_cross is not None and ev[0][0] - t_cross <= dt_l1 + 1e-9
        assert S.crash_sub[0] == KC.CRASH_COLLISION_WORLD and S.ctrl_mode[0] == K.M_KILLED
        assert float(S.p[0, 1]) < 40.0  # 回退到立面外
    finally:
        rig.close()


@pytest.mark.parametrize("kernel", KERNELS)
def test_kill_at_20m_impact(kernel: str) -> None:
    rig = _rig(kernel)
    try:
        S = rig.S
        rig.air(0, GROUND + 20.0, 2.0)
        ACT.begin_kill(S, [0], rig.t)
        rig.step_s(4.0)
        ev = _collisions(rig)
        assert [e[2] for e in ev] == ["IMPACT"]
        assert S.crash_sub[0] == KC.CRASH_IMPACT and S.landed[0]
        assert abs(-S.p[0, 2] - float(np.float32(GROUND))) <= 1e-9
    finally:
        rig.close()


@pytest.mark.parametrize("kernel", KERNELS)
def test_uav_collision(kernel: str) -> None:
    rig = Rig("x500", 2, kernel=kernel, spacing=30.0)  # 平地（无世界）
    try:
        S = rig.S
        rig.air([0, 1], 15.0, 2.0)
        mid = 0.5 * (S.p[0] + S.p[1])
        ACT.begin_goto(S, [0], mid + (S.p[1] - S.p[0]), 4.0, None, rig.t, stop_motion=False)
        ACT.begin_goto(S, [1], mid + (S.p[0] - S.p[1]), 4.0, None, rig.t, stop_motion=False)
        for _ in range(20 * 125):
            rig.step(2)
            if len(_collisions(rig)) >= 2:
                break
        ev = _collisions(rig)
        assert sorted(e[1] for e in ev[:2]) == [S.ids[0], S.ids[1]]
        assert {e[2] for e in ev} == {"COLLISION_UAV"}
        assert S.crash_sub[0] == 3 and S.crash_sub[1] == 3
    finally:
        rig.close()


def test_uav_collider_kernel_matches_grid_pairing() -> None:
    """大机群路径（numba 排序扫描 `kernels_contact.uav_hits`）与 8 套半格平移网格的排序配对给出同一组命中、同一顺序
    （FX2-R2）。随机 600 架密集机群，含成团近碰撞（多机同格）与跨格边界的对。"""
    from awr.sim.fleet.collide import UavCollider
    from awr.sim.fleet.state import FleetState

    if not K.HAVE_NUMBA:
        pytest.skip("numba unavailable")
    rng = np.random.default_rng(11)
    for trial in range(6):
        n = 600
        S1, S2 = FleetState(1024), FleetState(1024)
        idx = np.sort(rng.choice(1024, n, replace=False)).astype(np.int32)
        p0 = rng.uniform(-60.0, 60.0, (1024, 3))
        p0[:, 2] = rng.uniform(-40.0, -20.0, 1024)
        clump = idx[: 40 + 10 * trial]
        p0[clump] = p0[clump[0]] + rng.normal(0.0, 0.8, (clump.size, 3))
        step = rng.normal(0.0, 0.3, (1024, 3))
        rad = np.array([0.49, 0.6])
        for S in (S1, S2):
            S.p[:] = p0
            S.profile_id[:] = rng.integers(0, 2, 1024) if S is S1 else S1.profile_id
            S.landed[:] = False
            S.crash_sub[:] = 0
        c1, c2 = UavCollider(1024), UavCollider(1024)
        c2.use_kernel = False
        assert c1.use_kernel
        g1 = c1.check(S1, idx, rad)
        g2 = c2.check(S2, idx, rad)
        assert g1 == g2, (trial, len(g1), len(g2))
        for S in (S1, S2):
            S.p[:] = p0 + step
            S.crash_sub[:] = 0          # 第二次检查：线性运动段上的 CPA（全体重新参与）
        h1 = c1.check(S1, idx, rad)
        h2 = c2.check(S2, idx, rad)
        assert h1 == h2 and len(g1) > 0 and len(h1) > 0, (trial, len(h1), len(h2))
        assert np.array_equal(S1.crash_sub, S2.crash_sub) and np.array_equal(S1.ctrl_mode, S2.ctrl_mode)
