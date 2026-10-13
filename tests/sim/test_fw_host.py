"""固定翼与 VTOL 运动学宿主（M08-AC-204、AC-205；M08-FR-210 至 FR-213；ADR-096）。

- `write_kinematic`：ENU/FLU → NED/FRD 写入后经 ENU 只读视图读回，往返误差 ≤ 1e-9；只接受带 FW 或 L0 位的 slot（多旋翼 slot
  ValueError），同一 tick 同一 slot 只允许写一次；
- 带 FW 位的 slot 在 100 个 tick 内不被 L1（refgen 至 integrate、融合核）写入（字段哈希不变），numba 与 oracle 两种内核；
- VTOL 保真度切换（`set_fidelity`，ingest 步边界执行）：切入 FW 位置与速度不变；切回 L1 推力取悬停值、交回 HOLD，2 s 内
  pos_err ≤ 1 m、切换前后速度跳变 ≤ 0.1 m/s；
- SupervisorQueue 动作按构型分派：固定翼 slot 交构型运动提供者的 `safety_action`（HOLD、RTL、LAND、ELAND），多旋翼原生执行；
- 伞降触地：带 FW 位的 slot 以 5 m/s 触地为正常触地（LANDED、touchdown 事件），8 m/s 判硬着陆（IMPACT、KILLED）。
"""

from __future__ import annotations

import hashlib
import math

import numpy as np
import pytest
from simlib import Rig

from awr.sim.core import command as CMD
from awr.sim.core.supervisor_queue import SupervisorQueue
from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.kinematic_write import write_kinematic
from awr.sim.fleet.stages.ingest import apply_supervisor
from awr.sim.fleet.stages.registry import Fidelity

FW = int(Fidelity.L1 | Fidelity.FW)


def _hash(S, s: int) -> str:
    h = hashlib.sha256()
    for k in ("p", "v", "q", "omega", "thrust", "thr_sp", "q_sp", "tr_x", "tr_v", "pos_sp", "vel_int"):
        h.update(np.ascontiguousarray(getattr(S, k)[s]).tobytes())
    return h.hexdigest()


def _quat_xyzw(yaw: float, pitch: float, roll: float) -> np.ndarray:
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    w = cr * cp * cy + sr * sp * sy
    x = sr * cp * cy - cr * sp * sy
    y = cr * sp * cy + sr * cp * sy
    z = cr * cp * sy - sr * sp * cy
    return np.array([x, y, z, w])


def test_write_kinematic_roundtrip_and_guards() -> None:
    rig = Rig("x500", 3)
    try:
        S = rig.S
        S.fidelity[1] = FW
        S.fidelity[2] = int(Fidelity.L0)
        rng = np.random.default_rng(3)
        p = rng.normal(size=(2, 3)) * 100
        v = rng.normal(size=(2, 3)) * 10
        q = np.stack([_quat_xyzw(0.3, 0.1, -0.2), _quat_xyzw(-2.0, 0.05, 0.4)])
        w = rng.normal(size=(2, 3))
        a = rng.normal(size=(2, 3))
        ref = rng.normal(size=(2, 3)) * 50
        rig.step(1)
        write_kinematic(S, [1, 2], p, v, q, w, a, ref)
        E = S.enu
        assert np.abs(E.pos[[1, 2]] - p).max() <= 1e-9 and np.abs(E.vel[[1, 2]] - v).max() <= 1e-9
        assert np.abs(E.acc[[1, 2]] - a).max() <= 1e-9 and np.abs(E.pos_ref[[1, 2]] - ref).max() <= 1e-9
        assert np.abs(E.omega_flu[[1, 2]] - w).max() <= 1e-9
        qq = E.q_xyzw[[1, 2]]
        assert np.minimum(np.abs(qq - q).max(axis=1), np.abs(qq + q).max(axis=1)).max() <= 1e-9
        with pytest.raises(ValueError, match="FW 或 L0"):
            write_kinematic(S, [0], p[:1], v[:1], q[:1], w[:1], a[:1], ref[:1])
        with pytest.raises(ValueError, match="只允许写一次"):
            write_kinematic(S, [1], p[:1], v[:1], q[:1], w[:1], a[:1], ref[:1])
        rig.step(1)
        write_kinematic(S, [1], p[:1] + 1.0, v[:1], q[:1], w[:1], a[:1], ref[:1])  # 下一 tick 可再写（单行 numpy 路径）
        assert np.abs(E.pos[1] - (p[0] + 1.0)).max() <= 1e-9
    finally:
        rig.close()


@pytest.mark.parametrize("kernel", ["numba", "numpy"])
def test_fw_slot_untouched_by_l1(kernel: str) -> None:
    if kernel == "numba" and not K.HAVE_NUMBA:
        pytest.skip("numba 不可用")
    rig = Rig("x500", 2, kernel=kernel)
    try:
        rig.air([0, 1], 20.0, 1.0)
        S = rig.S
        S.fidelity[1] = FW
        h0 = _hash(S, 1)
        p0 = S.p[0].copy()
        rig.step(100)
        assert _hash(S, 1) == h0
        assert np.linalg.norm(S.p[0] - p0) < 0.5  # 多旋翼仍由 L1 保持
    finally:
        rig.close()


def test_vtol_fidelity_switch_continuity() -> None:
    rig = Rig("x500", 1)
    try:
        rig.air([0], 20.0, 3.0)
        S = rig.S
        f = rig.fleet
        p0, v0 = S.p[0].copy(), S.v[0].copy()
        f.set_fidelity([0], add=int(Fidelity.FW))
        assert int(S.fidelity[0]) == int(Fidelity.L1)  # 只入队
        rig.step(1)
        assert int(S.fidelity[0]) == FW
        assert np.abs(S.p[0] - p0).max() < 0.05 and np.abs(S.v[0] - v0).max() < 0.1
        S.v[0] = (2.0, 0.0, 0.0)  # 固定翼段以 2 m/s 前飞（运动学不由 L1 推进）
        v_fw = S.v[0].copy()
        f.set_fidelity([0], remove=int(Fidelity.FW))
        rig.step(1)
        assert int(S.fidelity[0]) == int(Fidelity.L1)
        assert np.abs(S.v[0] - v_fw).max() <= 0.1  # 切换前后速度跳变 ≤ 0.1 m/s
        assert int(S.ctrl_mode[0]) == K.M_HOLD
        rig.step_s(2.0)
        assert float(np.linalg.norm(S.pos_ref[0] - S.p[0])) <= 1.0
        assert len(f.fidelity_ops.pending) == 0
    finally:
        rig.close()


class FakeFW:
    name = "fake_fw"
    ops = ()
    frames = frozenset({"fixed_wing", "vtol"})

    def __init__(self) -> None:
        self.started: list = []
        self.safety: list = []
        self.canceled: list = []

    def start(self, call, slots, args, apply_tick):
        self.started.append((call.op, [int(s) for s in slots], dict(args)))
        return "ok"

    def cancel(self, cid, slots):
        self.canceled.append(cid)

    def safety_action(self, action, slots, t_s, item):
        self.safety.append((action, [int(s) for s in slots]))


def test_supervisor_dispatch_by_frame() -> None:
    fw = FakeFW()
    CMD.register_motion_provider(fw)
    try:
        rig = Rig("x500", 2)
        try:
            rig.air([0, 1], 20.0, 0.5)
            S = rig.S
            S.frame_kind[1] = 1
            S.fidelity[1] = FW
            q = SupervisorQueue()
            for act in ("hold", "rtl", "land", "eland"):
                getattr(q, act)(np.array([0, 1], np.int32))
                apply_supervisor(S, q.take(), rig.t, rig.fleet.PB)
            assert [a for a, _ in fw.safety] == ["HOLD", "RTL", "LAND", "ELAND"]
            assert all(sl == [1] for _, sl in fw.safety)
            assert int(S.ctrl_mode[0]) == K.M_ELAND  # 多旋翼原生执行
        finally:
            rig.close()
        with pytest.raises(ValueError):
            CMD.register_motion_provider(FakeFW())  # 同一构型只允许一个
    finally:
        CMD.reset_motion_providers()


@pytest.mark.parametrize(("vz", "hard"), [(5.0, False), (8.0, True)])
def test_parachute_touchdown(vz: float, hard: bool) -> None:
    rig = Rig("x500", 1)
    try:
        S = rig.S
        rig.step(1)
        S.fidelity[0] = FW
        S.frame_kind[0] = 1
        S.landed[0] = False
        S.in_air[0] = True
        S.in_contact[0] = False
        S.p[0, 2] = 0.05  # 已穿过地表 5 cm（NED 向下为正）
        S.v[0] = (0.0, 0.0, vz)
        rig.events.items.clear()
        rig.step(2)
        kinds = rig.events.kinds()
        assert bool(S.landed[0]) and not bool(S.in_air[0]) and abs(float(S.p[0, 2])) < 1e-9
        if hard:
            assert "sim.contact.collision" in kinds and int(S.ctrl_mode[0]) == K.M_KILLED
        else:
            assert "sim.contact.touchdown" in kinds and "sim.contact.collision" not in kinds
            assert int(S.ctrl_mode[0]) == K.M_IDLE
    finally:
        rig.close()
