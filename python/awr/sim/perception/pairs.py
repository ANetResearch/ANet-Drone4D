"""对状态表（D2-FX-R2-perception；M19-FR-013、FR-014；D2-AC-13）。

（机体，成像传感器，识别物）对的等级迟滞与明确捕获状态（`state.py` 的 `PairState` 字段）存于数组，numba 核 `fsm_row` 按
`state.step` 逐项相同地推进（None 以 NaN 或 -1 表示）；`PairView` 以属性读写同一行，供查询接口（capture_state、best_of、汇总、
帧）与 Python 参考路径（`state.step(view, …)`，numba 不可用或 `AWR_KERNEL=numpy` 时）使用。每对另存最近一次测量（`last`）：
N_eff、GSD、p、limiting、传感器类别、量测与 sigma、N_req（`_SlotRec`，键与此前的 dict 相同）。

此前每对一个 dataclass，逐对 Python 调用 `step` 与构造测量 dict：12 × 30 每仿真秒约 1600 次，约占 stage 成本的四分之一。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .kernels import (
    F_DOWN0,
    F_GSD,
    F_NEFF,
    F_NREQ,
    F_P,
    F_SIG,
    F_SINCE,
    F_TBAD,
    F_TEVAL,
    F_TLASTOK,
    F_TOK,
    F_UP0,
    F_Z0,
    F_Z2,
    I_AGENT,
    I_ALIVE,
    I_CAP,
    I_DOWNMAX,
    I_HASLAST,
    I_HASMEAS,
    I_KIND,
    I_LEVEL,
    I_LIM,
    I_LREQ,
    I_REASON,
    I_SEQ,
    I_TNO,
    I_UPMIN,
    KIND_NAMES,
    LREQ_NAMES,
    PF_N,
    PI_N,
    fov_rows,
    fsm_row,
    init_row,
)
from .params import LIMITING
from .state import ARMING, CAPTURED, HOLDING, IDLE

_LREQ_IDX = {"D": 0, "R": 1, "I": 2}


def lreq_code(n_req: float) -> int:
    """`johnson.level_req_of(n_req)` 的码（D 0、R 1、I 2；不足 D 时为缺省 R）。"""
    from .johnson import level_req_of

    return _LREQ_IDX[level_req_of(float(n_req))]

__all__ = ["KIND_NAMES", "LREQ_NAMES", "PairTable", "PairView", "fov_rows", "fsm_row", "last_dict", "lreq_code"]

class PairTable:
    """对状态的数组表（容量按需倍增；空位栈 FREE[:PM[0]]，栈顶为下一个取用的行）。"""

    def __init__(self, cap: int = 64) -> None:
        self.PI = np.zeros((cap, PI_N), np.int64)
        self.PF = np.zeros((cap, PF_N))
        self.FREE = np.arange(cap - 1, -1, -1, dtype=np.int64)
        self.PM = np.array([cap, 0], np.int64)          # [空位个数, 下一个建立序号]

    @property
    def n_free(self) -> int:
        return int(self.PM[0])

    def grow(self, need_free: int) -> None:
        if int(self.PM[0]) >= need_free:
            return
        cap = self.PI.shape[0]
        new = max(2 * cap, cap + need_free)
        PI = np.zeros((new, PI_N), np.int64)
        PF = np.zeros((new, PF_N))
        PI[:cap], PF[:cap] = self.PI, self.PF
        nf = int(self.PM[0])
        FREE = np.empty(new, np.int64)
        add = np.arange(new - 1, cap - 1, -1, dtype=np.int64)
        FREE[:len(add)] = add
        FREE[len(add):len(add) + nf] = self.FREE[:nf]
        self.PI, self.PF, self.FREE = PI, PF, FREE
        self.PM[0] = len(add) + nf

    def alloc(self, tno: int = -1, agent: int = -1) -> int:
        if int(self.PM[0]) == 0:
            self.grow(1)
        k = int(self.PM[0]) - 1
        s = int(self.FREE[k])
        self.PM[0] = k
        init_row(self.PI, self.PF, s)
        self.PI[s, I_TNO] = tno
        self.PI[s, I_AGENT] = agent
        self.PI[s, I_SEQ] = self.PM[1]
        self.PM[1] += 1
        return s

    def release(self, s: int) -> None:
        self.PI[s, I_ALIVE] = 0
        k = int(self.PM[0])
        self.FREE[k] = s
        self.PM[0] = k + 1

    def clear(self) -> None:
        cap = self.PI.shape[0]
        self.PI[:, I_ALIVE] = 0
        self.FREE[:] = np.arange(cap - 1, -1, -1, dtype=np.int64)
        self.PM[0] = cap


def _f(v: float) -> float | None:
    return None if v != v else float(v)


class _SlotRec:
    """对的最近一次测量（`PairState.last`；与此前的测量 dict 同键：n_eff、gsd、p、lim、kind、meas、sigma、n_req）。"""

    __slots__ = ("_s", "_t")

    def __init__(self, tab: PairTable, s: int) -> None:
        self._t, self._s = tab, s

    def get(self, k: str, default: Any = None) -> Any:
        t, s = self._t, self._s
        if k == "n_eff":
            return float(t.PF[s, F_NEFF])
        if k == "gsd":
            return float(t.PF[s, F_GSD])
        if k == "p":
            return float(t.PF[s, F_P])
        if k == "lim":
            c = int(t.PI[s, I_LIM])
            return None if c < 0 else LIMITING[c]
        if k == "kind":
            c = int(t.PI[s, I_KIND])
            return None if c < 0 else KIND_NAMES[c]
        if k == "meas":
            return t.PF[s, F_Z0:F_Z2 + 1].copy() if t.PI[s, I_HASMEAS] else None
        if k == "sigma":
            return float(t.PF[s, F_SIG]) if t.PI[s, I_HASMEAS] else None
        if k == "n_req":
            return float(t.PF[s, F_NREQ])
        return default

    def __getitem__(self, k: str) -> Any:
        v = self.get(k, KeyError)
        if v is KeyError:
            raise KeyError(k)
        return v

    def __bool__(self) -> bool:
        return True

    @property
    def kind(self) -> str | None:
        c = int(self._t.PI[self._s, I_KIND])
        return None if c < 0 else KIND_NAMES[c]


class PairView:
    """`PairState` 的同名属性视图（读写 PairTable 的一行）。"""

    __slots__ = ("s", "tab")

    def __init__(self, tab: PairTable, s: int) -> None:
        self.tab, self.s = tab, s

    # ---- 等级与捕获
    @property
    def level(self) -> int:
        return int(self.tab.PI[self.s, I_LEVEL])

    @level.setter
    def level(self, v: int) -> None:
        self.tab.PI[self.s, I_LEVEL] = int(v)

    @property
    def up_t0(self) -> float | None:
        return _f(self.tab.PF[self.s, F_UP0])

    @up_t0.setter
    def up_t0(self, v: float | None) -> None:
        self.tab.PF[self.s, F_UP0] = math.nan if v is None else float(v)

    @property
    def up_min(self) -> int:
        return int(self.tab.PI[self.s, I_UPMIN])

    @up_min.setter
    def up_min(self, v: int) -> None:
        self.tab.PI[self.s, I_UPMIN] = int(v)

    @property
    def down_t0(self) -> float | None:
        return _f(self.tab.PF[self.s, F_DOWN0])

    @down_t0.setter
    def down_t0(self, v: float | None) -> None:
        self.tab.PF[self.s, F_DOWN0] = math.nan if v is None else float(v)

    @property
    def down_max(self) -> int:
        return int(self.tab.PI[self.s, I_DOWNMAX])

    @down_max.setter
    def down_max(self, v: int) -> None:
        self.tab.PI[self.s, I_DOWNMAX] = int(v)

    @property
    def cap(self) -> int:
        return int(self.tab.PI[self.s, I_CAP])

    @cap.setter
    def cap(self, v: int) -> None:
        self.tab.PI[self.s, I_CAP] = int(v)

    @property
    def t_ok(self) -> float:
        return float(self.tab.PF[self.s, F_TOK])

    @t_ok.setter
    def t_ok(self, v: float) -> None:
        self.tab.PF[self.s, F_TOK] = float(v)

    @property
    def t_bad(self) -> float:
        return float(self.tab.PF[self.s, F_TBAD])

    @t_bad.setter
    def t_bad(self, v: float) -> None:
        self.tab.PF[self.s, F_TBAD] = float(v)

    @property
    def since(self) -> float:
        return float(self.tab.PF[self.s, F_SINCE])

    @since.setter
    def since(self, v: float) -> None:
        self.tab.PF[self.s, F_SINCE] = float(v)

    @property
    def t_last_ok(self) -> float | None:
        return _f(self.tab.PF[self.s, F_TLASTOK])

    @t_last_ok.setter
    def t_last_ok(self, v: float | None) -> None:
        self.tab.PF[self.s, F_TLASTOK] = math.nan if v is None else float(v)

    @property
    def reason(self) -> str | None:
        c = int(self.tab.PI[self.s, I_REASON])
        return None if c < 0 else LIMITING[c]

    @reason.setter
    def reason(self, v: str | None) -> None:
        self.tab.PI[self.s, I_REASON] = -1 if v is None else LIMITING.index(v)

    @property
    def t_eval(self) -> float | None:
        return _f(self.tab.PF[self.s, F_TEVAL])

    @t_eval.setter
    def t_eval(self, v: float | None) -> None:
        self.tab.PF[self.s, F_TEVAL] = math.nan if v is None else float(v)

    @property
    def n_req(self) -> float:
        return float(self.tab.PF[self.s, F_NREQ])

    @n_req.setter
    def n_req(self, v: float) -> None:
        self.tab.PF[self.s, F_NREQ] = float(v)

    @property
    def level_req(self) -> str:
        return LREQ_NAMES[int(self.tab.PI[self.s, I_LREQ])]

    @level_req.setter
    def level_req(self, v: str) -> None:
        self.tab.PI[self.s, I_LREQ] = _LREQ_IDX[v]

    @property
    def last(self) -> Any:
        return _SlotRec(self.tab, self.s) if self.tab.PI[self.s, I_HASLAST] else {}

    def set_last(self, ev: Any, j: int) -> None:
        """Python 路径：从一次评估（runtime._Eval）的第 j 对写入最近测量。"""
        PI, PF, s = self.tab.PI, self.tab.PF, self.s
        PF[s, F_NEFF] = ev.res[j, 1]
        PF[s, F_GSD] = ev.core[j, 3]
        PF[s, F_P] = ev.res[j, 2]
        PI[s, I_LIM] = ev.codes[j, 0]
        PI[s, I_KIND] = KIND_NAMES.index(ev.kind)
        h = int(ev.codes[j, 3])
        PI[s, I_HASMEAS] = h
        PF[s, F_SIG] = ev.res[j, 3]
        if h:
            PF[s, F_Z0:F_Z2 + 1] = ev.z[j]
        PI[s, I_HASLAST] = 1

    @property
    def active(self) -> bool:
        return self.level > 0 or self.cap != IDLE or self.up_t0 is not None

    def hold_s(self, t: float) -> float:
        c = self.cap
        if c == ARMING:
            return max(0.0, t - self.t_ok)
        if c == CAPTURED:
            return max(0.0, t - self.since)
        if c == HOLDING:
            return max(0.0, t - self.t_bad)
        return 0.0

    def __repr__(self) -> str:
        return (f"PairView(s={self.s}, level={self.level}, cap={self.cap}, since={self.since}, n_req={self.n_req}, "
                f"t_last_ok={self.t_last_ok})")


def last_dict(ps: PairView) -> dict:
    """最近测量的 dict（键同 `_SlotRec`；事件与检测环一次读出）。无测量时为空 dict。"""
    tab, s = ps.tab, ps.s
    pi = tab.PI[s].tolist()
    if not pi[I_HASLAST]:
        return {}
    pf = tab.PF[s].tolist()
    lim, kind, h = pi[I_LIM], pi[I_KIND], pi[I_HASMEAS]
    return {"n_eff": pf[F_NEFF], "gsd": pf[F_GSD], "p": pf[F_P], "lim": None if lim < 0 else LIMITING[lim],
            "kind": None if kind < 0 else KIND_NAMES[kind], "meas": pf[F_Z0:F_Z2 + 1] if h else None, "sigma": pf[F_SIG] if h else None,
            "n_req": pf[F_NREQ]}
