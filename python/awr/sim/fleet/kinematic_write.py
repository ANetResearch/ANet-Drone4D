"""固定翼与 VTOL 运动学的宿主接口（M08-FR-210 至 FR-212；M08 §15.3.5；ADR-096）。

- `write_kinematic(S, slots, p_enu, v_enu, q_xyzw, omega_flu, acc_enu, pos_ref_enu)`：M21 的 `fw_kin` stage（order 086，50 Hz）
  经此写入机体状态（ENU/FLU → NED/FRD 在 fleet 包内完成，其他包不接触 NED）。只接受带 `FW` 或 `L0` 位的 slot，否则 ValueError；
  同一 tick 一个 slot 只允许写一次（一字段一写者）。`pos_ref` 为固定翼的导引参考点（FastGuard 的 pos_err 语义为横向偏差）。
- `FidelityOps`：VTOL 保真度切换 `set_fidelity(slots, add=FW | remove=FW)` 的待生效队列，由 ingest（步边界）执行并写输入日志。
  切入 FW 保留位置、速度与姿态并清除 TRAJ 跟踪；切回 L1 以当前速度与姿态初始化 L1 状态（推力取悬停值、速度积分器清零、
  位置与速度设定取当前值），交回 HOLD 参考，由后续命令接续。
- `frame_kind_view(S)`：构型列只读视图。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from . import actions as ACT
from . import kernels_l1 as K
from .kernels_kin import kin_write, kin_write_numpy
from .stages.registry import Fidelity

if TYPE_CHECKING:
    from .state import FleetState

__all__ = ["FidelityOps", "frame_kind_view", "write_kinematic"]

_FW = int(Fidelity.FW)
_L0 = int(Fidelity.L0)
_L1 = int(Fidelity.L1)


def _rows(a: Any, n: int, width: int, name: str) -> np.ndarray:
    x = np.ascontiguousarray(np.asarray(a, np.float64).reshape(n, width))
    if not np.all(np.isfinite(x)):
        raise ValueError(f"write_kinematic: {name} 含非有限值")
    return x


def write_kinematic(S: FleetState, slots: Any, p_enu: Any, v_enu: Any, q_xyzw: Any, omega_flu: Any, acc_enu: Any,
                    pos_ref_enu: Any) -> None:
    """写入 k 个 slot 的运动学状态（单位：m、m/s、WORLD←FLU 四元数 [x,y,z,w]、rad/s、m/s²、m）。"""
    sl = np.ascontiguousarray(np.atleast_1d(np.asarray(slots, np.int64)))
    n = sl.size
    if n == 0:
        return
    if sl.min() < 0 or sl.max() >= S.capacity:
        raise ValueError("write_kinematic: slot 越界")
    f = S.fidelity[sl]
    if not np.all(S.active[sl]) or np.any((f & (_FW | _L0)) == 0):
        raise ValueError("write_kinematic: 只接受带 FW 或 L0 位的活动 slot（M08-FR-211）")
    if np.unique(sl).size != n:
        raise ValueError("write_kinematic: 同一调用中 slot 重复")
    last = getattr(S, "kin_write_tick", None)
    if last is None:
        last = S.kin_write_tick = np.full(S.capacity, -1, np.int64)
    if np.any(last[sl] == S.tick):
        raise ValueError("write_kinematic: 同一 tick 一个 slot 只允许写一次（一字段一写者）")
    args = (_rows(p_enu, n, 3, "p_enu"), _rows(v_enu, n, 3, "v_enu"), _rows(q_xyzw, n, 4, "q_xyzw"),
            _rows(omega_flu, n, 3, "omega_flu"), _rows(acc_enu, n, 3, "acc_enu"), _rows(pos_ref_enu, n, 3, "pos_ref_enu"))
    fn = kin_write if K.HAVE_NUMBA and n >= 2 else kin_write_numpy  # 单行走 numpy（同 tap：避免 'C' 布局另编签名）
    fn(sl, *args, S.p, S.p_prev, S.v, S.q, S.omega, S.a_meas, S.pos_ref)
    last[sl] = S.tick
    S.touch()


def frame_kind_view(S: FleetState) -> np.ndarray:
    v = S.frame_kind.view()
    v.flags.writeable = False
    return v


class FidelityOps:
    """VTOL 保真度切换的待生效队列（M08-FR-212）：`set_fidelity` 只入队，ingest 在步边界按 slot 升序执行。"""

    def __init__(self) -> None:
        self.pending: list[tuple[int, int, int]] = []  # (slot, add, remove)
        self.inputlog: Any = None
        self.on_applied: Callable[[int, int, int], None] | None = None

    def set_fidelity(self, slots: Any, *, add: int = 0, remove: int = 0) -> None:
        add, remove = int(add), int(remove)
        if add & ~int(Fidelity.ALL) or remove & ~int(Fidelity.ALL) or (add & remove):
            raise ValueError(f"set_fidelity: 非法位掩码 add={add} remove={remove}")
        for s in np.atleast_1d(np.asarray(slots, np.int64)).tolist():
            self.pending.append((int(s), add, remove))

    def apply(self, S: FleetState, T: Any, tick: int, t_s: float) -> int:
        """ingest 内执行（步边界）；返回执行的条数。"""
        if not self.pending:
            return 0
        ops, self.pending = sorted(self.pending), []
        n = 0
        for s, add, remove in ops:
            if not S.active[s]:
                continue
            f0 = int(S.fidelity[s])
            f1 = (f0 | add) & ~remove
            if f1 == f0:
                continue
            sl = np.array([s], np.int64)
            if add & _FW:
                if int(S.ctrl_mode[s]) == K.M_TRAJ:  # 清除 TRAJ 跟踪（运动交给 fw_kin）
                    S.ctrl_mode[s] = K.M_HOLD
                S.vel_sess[s] = False
            S.fidelity[s] = f1
            if remove & _FW and f1 & _L1:
                # 以当前速度与姿态接续 L1：推力取悬停值、积分器清零、位置与速度设定取当前值，交回 HOLD 参考
                S.thrust[s] = float(T.PT[int(S.profile_id[s]), K.P_HOVER])
                S.vel_int[s] = 0.0
                S.pos_sp[s] = S.p[s]
                S.vel_cmd[s] = S.v[s]
                ACT.begin_hold(S, sl, t_s)
            n += 1
            if self.inputlog is not None:
                self.inputlog.append("fidelity", tick, {"slot": s, "add": add, "remove": remove})
            if self.on_applied is not None:
                self.on_applied(s, add, remove)
        if n:
            S.touch()
        return n
