"""识别物航迹（M19-FR-019；AWR-04 §6.4、§9.1；M18-M19 PRD §6.12）。

每识别物一条三维恒速 Kalman 航迹，融合我方各机的量测（成像 D 级及以上、雷达、MID-360；声阵列只给方位，不入滤波）：

- 状态 x = [p, v]（ENU），连续白噪声加速度模型，过程噪声 q 按类别（person 0.5、dog 2.0、vehicle 2.0、uav 3.0、machine 与
  object 0.05 m/s²）；量测为位置，噪声 sigma 由传感器给出（成像 `√((R·1 mrad)² + GSD² + sigma_hold²)`、雷达由距离与角度噪声换算、
  MID-360 0.05 m）；
- 状态：TENTATIVE（首个量测）→ CONFIRMED（2 s 内 ≥ 3 次更新）→ COASTING（1 s 无更新，协方差增长）→ DROPPED（30 s 无更新，
  删除）；COASTING 中再有更新回到 CONFIRMED；
- 只读接口 `get(target_no, t)` 预测到 t（M20 只读航迹，不读真值）。确定性。

实现（D2-FX-R2-perception；D2 验收第 1 轮 4.1：此前逐次 6 × 6 矩阵与 `np.linalg.inv` 约 60 µs/次，12 × 30 每仿真秒约 850 次）：
量测噪声各向同性（`sigma²·I₃`）、初始协方差 `diag(sigma²·I₃, v0²·I₃)`、F 与 Q 按轴相同，故 6 × 6 协方差恒为分块标量形
`[[a·I₃, b·I₃], [b·I₃, c·I₃]]`，滤波精确分解为三个共用 (a, b, c) 的逐轴二维滤波：

```text
预测  a ← a + 2·dt·b + dt²·c + q²·dt³/3，b ← b + dt·c + q²·dt²/2，c ← c + q²·dt；p ← p + dt·v
更新  s = a + sigma²，k_p = a/s，k_v = b/s；p ← p + k_p·(z − p)，v ← v + k_v·(z − p)；a ← (1 − k_p)·a，b ← (1 − k_p)·b，c ← c − k_v·b
```

与 6 × 6 矩阵形式在数学上相同，数值上只差舍入（`tests/perception/test_kernels.py` 对拍）。状态存于按槽位的数组
`TF = [x(6), a, b, c, t, t_last, q²]`、`TI = [state, n_upd, hist 个数, hist 写位置]`、`HIST`；`TMAP[target_no]` 为槽位（-1 无）。
一次平台处理的全部量测经 numba 核 `kernels.trk_update_row` 逐行完成（单一成像来源时在 `kernels.post_fsm` 内完成；numba 不可用
或 `AWR_KERNEL=numpy` 时逐个 Python 标量更新，运算与次序相同）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .kernels import (
    HAVE_NUMBA,
    T_AB,
    T_C,
    T_Q2,
    T_T,
    T_TL,
    TF_N,
    TI_HN,
    TI_HP,
    TI_N,
    TI_NU,
    TI_ST,
    TRK_HIST,
    njit,
    trk_update_row,
)
from .params import Q_ACCEL, TRACK_COAST_S, TRACK_CONFIRM_N, TRACK_CONFIRM_S, TRACK_DROP_S

__all__ = ["COASTING", "CONFIRMED", "DROPPED", "TENTATIVE", "TRACK_STATES", "Track", "TrackTable"]

TENTATIVE, CONFIRMED, COASTING, DROPPED = 0, 1, 2, 3
TRACK_STATES = ("TENTATIVE", "CONFIRMED", "COASTING", "DROPPED")
CONFIRM_S = TRACK_CONFIRM_S + 1e-9
V0 = 3.0


@dataclass(frozen=True)
class Track:
    target: str
    pos: tuple[float, float, float]
    vel: tuple[float, float, float]
    cov_diag: tuple[float, ...]      # [sigma²_px, sigma²_py, sigma²_pz, sigma²_vx, sigma²_vy, sigma²_vz]
    state: str
    t_last_ns: int
    n_updates: int = 0

    def to_wire(self) -> dict:
        return {"target": self.target, "pos": [round(x, 3) for x in self.pos], "vel": [round(x, 3) for x in self.vel],
                "cov_diag": [round(x, 4) for x in self.cov_diag], "state": self.state, "t_last_ns": int(self.t_last_ns)}


@njit(cache=True, fastmath=False)
def _update_rows(slots, new, t, Z, r2, v0, TF, TI, HIST, confirm_s, confirm_n):
    """slots[k] 的一次位置量测 Z[k]（噪声方差 r2[k]）；new[k] 为新航迹（以量测初始化）。与 `TrackTable._update_py` 逐项相同。"""
    for k in range(slots.shape[0]):
        trk_update_row(slots[k], new[k], t, Z[k, 0], Z[k, 1], Z[k, 2], r2[k], v0, TF, TI, HIST, confirm_s, confirm_n)


class TrackTable:
    def __init__(self) -> None:
        self.tr: dict[int, int] = {}     # target_no -> 槽位（插入次序 = 航迹建立次序）
        self.dropped: list[int] = []     # 最近一次 tick 删除的航迹（target_no）
        self.TF = np.zeros((0, TF_N))
        self.TI = np.zeros((0, TI_N), np.int64)
        self.HIST = np.zeros((0, TRK_HIST))
        self.FREE = np.zeros(0, np.int64)
        self.FM = np.zeros(1, np.int64)              # 空位栈中的个数
        self.TMAP = np.full(64, -1, np.int64)        # target_no -> 槽位
        self._alloc(16)

    def _alloc(self, cap: int) -> None:
        n0 = self.TF.shape[0]
        if cap <= n0:
            return
        TF = np.zeros((cap, TF_N))
        TI = np.zeros((cap, TI_N), np.int64)
        HIST = np.zeros((cap, TRK_HIST))
        TF[:n0], TI[:n0], HIST[:n0] = self.TF, self.TI, self.HIST
        nf = int(self.FM[0])
        FREE = np.empty(cap, np.int64)
        add = np.arange(cap - 1, n0 - 1, -1, dtype=np.int64)
        FREE[:len(add)] = add
        FREE[len(add):len(add) + nf] = self.FREE[:nf]
        self.TF, self.TI, self.HIST, self.FREE = TF, TI, HIST, FREE
        self.FM[0] = len(add) + nf

    def ensure(self, tno_max: int, n_new: int) -> None:
        """核内建立航迹之前：TMAP 覆盖 tno_max、空位 ≥ n_new。"""
        if self.TMAP.shape[0] <= tno_max:
            new = np.full(max(tno_max + 1, 2 * self.TMAP.shape[0]), -1, np.int64)
            new[:self.TMAP.shape[0]] = self.TMAP
            self.TMAP = new
        if int(self.FM[0]) < n_new:
            self._alloc(max(2 * self.TF.shape[0], self.TF.shape[0] + n_new))

    def __len__(self) -> int:
        return len(self.tr)

    def clear(self) -> None:
        self.tr.clear()
        self.TMAP[:] = -1
        cap = self.TF.shape[0]
        self.FREE[:cap] = np.arange(cap - 1, -1, -1, dtype=np.int64)
        self.FM[0] = cap

    def remove(self, target_no: int) -> None:
        tn = int(target_no)
        s = self.tr.pop(tn, None)
        if s is not None:
            if tn < self.TMAP.shape[0]:
                self.TMAP[tn] = -1
            k = int(self.FM[0])
            self.FREE[k] = s
            self.FM[0] = k + 1

    def register(self, target_no: int, s: int) -> None:
        """核内建立的航迹（槽位已从空位栈取出、TMAP 已写）登记到 `tr`。"""
        self.tr[int(target_no)] = int(s)

    def _slot(self, tn: int, cls: str) -> tuple[int, bool]:
        s = self.tr.get(tn)
        if s is not None:
            return s, False
        self.ensure(tn, 1)
        k = int(self.FM[0]) - 1
        s = int(self.FREE[k])
        self.FM[0] = k
        self.tr[tn] = s
        self.TMAP[tn] = s
        q = Q_ACCEL.get(cls, 1.0)
        self.TF[s, T_Q2] = q * q
        return s, True

    def _update_py(self, s: int, new: bool, t: float, z0: float, z1: float, z2: float, r2: float, v0: float) -> None:
        TF, TI = self.TF, self.TI
        if new:
            TF[s, 0:9] = (z0, z1, z2, 0.0, 0.0, 0.0, r2, 0.0, v0 * v0)
            TF[s, T_T] = TF[s, T_TL] = t
            TI[s, TI_ST] = TI[s, TI_NU] = TI[s, TI_HN] = TI[s, TI_HP] = 0
        else:
            x = TF[s, 0:6].tolist()
            dt = t - float(TF[s, T_T])
            a, b, c = TF[s, T_AB], TF[s, T_AB + 1], TF[s, T_C]
            a, b, c = float(a), float(b), float(c)
            if dt > 0.0:
                q2 = float(TF[s, T_Q2])
                a = a + 2.0 * dt * b + dt * dt * c + q2 * (dt * dt * dt) / 3.0
                b = b + dt * c + q2 * (dt * dt) / 2.0
                c = c + q2 * dt
                x[0] = x[0] + dt * x[3]
                x[1] = x[1] + dt * x[4]
                x[2] = x[2] + dt * x[5]
                TF[s, T_T] = t
            sg = a + r2
            kp = a / sg
            kv = b / sg
            y0, y1, y2 = z0 - x[0], z1 - x[1], z2 - x[2]
            TF[s, 0:9] = (x[0] + kp * y0, x[1] + kp * y1, x[2] + kp * y2, x[3] + kv * y0, x[4] + kv * y1, x[5] + kv * y2, (1.0 - kp) * a,
                          (1.0 - kp) * b, c - kv * b)
        TF[s, T_TL] = t
        TI[s, TI_NU] += 1
        hp = int(TI[s, TI_HP])
        self.HIST[s, hp] = t
        TI[s, TI_HP] = (hp + 1) % TRK_HIST
        if TI[s, TI_HN] < TRK_HIST:
            TI[s, TI_HN] += 1
        st = int(TI[s, TI_ST])
        if st == COASTING:
            TI[s, TI_ST] = CONFIRMED
        elif st == TENTATIVE:
            h = self.HIST[s, :int(TI[s, TI_HN])].tolist()
            if sum(1 for x_ in h if t - x_ <= CONFIRM_S) >= TRACK_CONFIRM_N:
                TI[s, TI_ST] = CONFIRMED

    def update(self, target_no: int, cls: str, t: float, z, sigma_m: float, *, v0_mps: float = V0) -> None:
        """位置量测（ENU，m）与各向同性噪声 sigma（m）。"""
        sg = max(float(sigma_m), 1e-3)
        s, new = self._slot(int(target_no), cls)
        self._update_py(s, new, float(t), float(z[0]), float(z[1]), float(z[2]), sg * sg, float(v0_mps))

    def update_batch(self, tnos: list[int], clss: list[str], t: float, Z, sigmas: list[float]) -> None:
        """同一时刻的一批量测（target_no 互不相同；与按序逐个 `update` 相同）。"""
        n = len(tnos)
        if n == 0:
            return
        if not HAVE_NUMBA or n == 1:
            for k in range(n):
                self.update(tnos[k], clss[k], t, Z[k], sigmas[k])
            return
        sl = [self._slot(int(tnos[k]), clss[k]) for k in range(n)]
        slots = np.array([x[0] for x in sl], np.int64)
        new = np.array([x[1] for x in sl], np.bool_)
        sg = np.maximum(np.array(sigmas, float), 1e-3)
        Zc = Z if (isinstance(Z, np.ndarray) and Z.dtype == np.float64 and Z.flags.c_contiguous and Z.shape == (n, 3)) else \
            np.ascontiguousarray(np.asarray(Z, float).reshape(n, 3))
        _update_rows(slots, new, float(t), Zc, sg * sg, V0, self.TF, self.TI, self.HIST, CONFIRM_S, TRACK_CONFIRM_N)

    def tick(self, t: float) -> None:
        """状态推进（5 Hz）：1 s 无更新 COASTING，30 s 无更新 DROPPED（删除）。"""
        self.dropped = []
        TF, TI = self.TF, self.TI
        for tn, s in list(self.tr.items()):
            age = t - float(TF[s, T_TL])
            if age >= TRACK_DROP_S - 1e-9:
                self.dropped.append(tn)
                self.remove(tn)
            elif age >= TRACK_COAST_S - 1e-9 and TI[s, TI_ST] == CONFIRMED:
                TI[s, TI_ST] = COASTING

    def get(self, target_no: int, t: float, target_id: str = "") -> Track | None:
        s = self.tr.get(int(target_no))
        if s is None:
            return None
        row = self.TF[s].tolist()
        x = row[0:6]
        a, b, c = row[T_AB], row[T_AB + 1], row[T_C]
        dt = t - row[T_T]
        if dt > 0.0:
            q2 = row[T_Q2]
            a = a + 2.0 * dt * b + dt * dt * c + q2 * (dt * dt * dt) / 3.0
            c = c + q2 * dt
            pos = (x[0] + dt * x[3], x[1] + dt * x[4], x[2] + dt * x[5])
        else:
            pos = (x[0], x[1], x[2])
        return Track(target_id or str(int(target_no)), pos, (x[3], x[4], x[5]), (a, a, a, c, c, c), TRACK_STATES[int(self.TI[s, TI_ST])],
                     round(row[T_TL] * 1e9), int(self.TI[s, TI_NU]))

    def state_of(self, target_no: int) -> int | None:
        s = self.tr.get(int(target_no))
        return None if s is None else int(self.TI[s, TI_ST])


def warmup() -> None:
    """航迹核的 numba 预热（运行期签名）。"""
    if not HAVE_NUMBA:
        return
    tt = TrackTable()
    tt.update_batch([1, 2], ["person", "uav"], 0.0, [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]], [0.5, 0.5])
    tt.update_batch([1, 2], ["person", "uav"], 0.2, [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]], [0.5, 0.5])
