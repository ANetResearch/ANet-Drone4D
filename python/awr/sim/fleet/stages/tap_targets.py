"""stage `tap_targets`（order 141；d1_250 every 25 / phase 21，sandbox100 every 10 / phase 2，均为 10 Hz【仿真】，排在 M18 `targets`
之后）：按 `awr.TargetLite32.v1` 打包全部识别物并发布总线 key `state/sim-core/targets`（M08-FR-217；M08 §15.3.4；17 §17.7.3）。

载荷 msgpack `{v: 1, t_sim_ns, roster_version, rows: bin(32·n)}`，`rows` 按 `target_no` 升序（M11 映射为 topic
`swarm/target/state`）。本 stage 只按布局表把实体表的列打包进 32 B 行（`COLUMNS`：列名 → 布局字段与位段；冻结字段表见
`tools/fake/targets_stub.py`），不解释字段语义；缺少的列（插件未装配）按 0 打包。快进时按墙钟 ≥ 50 ms 节流；`target/remove` 后
下一次发布即不含该行。只在实体层登记了 `target` 块（M18 已装配）时由组合根装配（D1 剧本的调度不变）。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import msgpack
import numpy as np

from awr.contracts.layouts import TARGET_LITE32, TARGET_LITE32_BITS

try:
    from numba import njit

    _NB = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover
    _NB = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f

        return deco(a[0]) if a and callable(a[0]) else deco

if TYPE_CHECKING:
    from ...core.entities import EntityTable
    from ..pipeline import StageCtx
    from ..state import FleetState

__all__ = ["BIT_COLUMNS", "SCALAR_COLUMNS", "TapTargets", "pack_rows", "warmup"]

# 布局字段 ← 实体列（标量）
SCALAR_COLUMNS: dict[str, str] = {"agent_no": "target_no", "class": "tgt_cls", "pos": "tgt_pos", "heading_rad": "tgt_yaw",
                                  "discovered_n": "tgt_disc_n", "n_vol": "tgt_n_vol", "keepout_r_m": "tgt_keepout_r"}
# 位段字段 ← {位段名: 实体列}（位置与宽度取 TARGET_LITE32_BITS）
BIT_COLUMNS: dict[str, dict[str, str]] = {
    "state": {"awareness": "tgt_aware", "criterion": "tgt_crit", "moving": "tgt_moving", "occupied": "tgt_occupied",
              "sound_on": "tgt_sound_on", "enabled": "tgt_enabled"},
    "best": {"level": "tgt_best_level", "captured": "tgt_captured", "relay": "tgt_relay", "sensor": "tgt_best_sensor"},
    "flags": {"hidden": "tgt_hidden", "discovered_recent": "tgt_disc_recent", "cfg_rev": "tgt_cfg_rev"},
}


_BIT_ORDER = tuple((fld, name, col) for fld, parts in BIT_COLUMNS.items() for name, col in parts.items())
_BIT_SPEC = tuple((("state", "best", "flags").index(f), int(TARGET_LITE32_BITS[f][nm][0]), (1 << int(TARGET_LITE32_BITS[f][nm][1])) - 1)
                  for f, nm, _c in _BIT_ORDER)
_ZERO: dict[tuple, np.ndarray] = {}


@njit(cache=True, fastmath=False)
def _bits(acc, c, r, shift, mask):
    return acc | ((np.uint32(c[r]) & np.uint32(mask)) << np.uint32(shift))


@njit(cache=True, fastmath=False)
def _pack_nb(rows, tno, cls, pos, yaw, disc_n, n_vol, keep, speed, alert, s_aw, s_cr, s_mv, s_oc, s_so, s_en, b_lv, b_ca, b_re, b_se,
             f_hi, f_dr, f_cfg, o_agent, o_cls, o_pos, o_yaw, o_speed, o_alert, o_disc, o_nvol, o_keep, o_state, o_best, o_flags):
    """`pack_rows` 的逐行核（同一取整、饱和与位段截断；位段的位置与宽度为 TARGET_LITE32_BITS 的冻结值，`_check_bits` 校验）。"""
    for i in range(rows.shape[0]):
        r = rows[i]
        o_agent[i] = tno[r]
        o_cls[i] = cls[r]
        o_pos[i, 0] = pos[r, 0]
        o_pos[i, 1] = pos[r, 1]
        o_pos[i, 2] = pos[r, 2]
        o_yaw[i] = yaw[r]
        o_disc[i] = disc_n[r]
        o_nvol[i] = n_vol[r]
        o_keep[i] = keep[r]
        v = np.round(np.float64(speed[r]) * 100.0)
        v = -32767.0 if v < -32767.0 else (32767.0 if v > 32767.0 else v)
        o_speed[i] = np.int16(v)
        al = np.round(np.float64(alert[r]) * 255.0)
        al = 0.0 if al < 0.0 else (255.0 if al > 255.0 else al)
        o_alert[i] = np.uint8(al)
        st = _bits(np.uint32(0), s_aw, r, 0, 7)
        st = _bits(st, s_cr, r, 3, 1)
        st = _bits(st, s_mv, r, 4, 1)
        st = _bits(st, s_oc, r, 5, 1)
        st = _bits(st, s_so, r, 6, 1)
        st = _bits(st, s_en, r, 7, 1)
        o_state[i] = np.uint8(st & np.uint32(255))
        be = _bits(np.uint32(0), b_lv, r, 0, 3)
        be = _bits(be, b_ca, r, 2, 1)
        be = _bits(be, b_re, r, 3, 1)
        be = _bits(be, b_se, r, 4, 15)
        o_best[i] = np.uint8(be & np.uint32(255))
        fl = _bits(np.uint32(0), f_hi, r, 0, 1)
        fl = _bits(fl, f_dr, r, 1, 1)
        fl = _bits(fl, f_cfg, r, 2, 63)
        o_flags[i] = np.uint8(fl & np.uint32(255))


# 核内写死的位段（字段次序同 BIT_COLUMNS）：(目标字段, 位置, 掩码)
_NB_BITS = ((0, 0, 7), (0, 3, 1), (0, 4, 1), (0, 5, 1), (0, 6, 1), (0, 7, 1), (1, 0, 3), (1, 2, 1), (1, 3, 1), (1, 4, 15), (2, 0, 1), (2, 1, 1),
            (2, 2, 63))


def _check_bits() -> bool:
    return _BIT_SPEC == _NB_BITS


_BITS_OK = _check_bits()


def _zeros(cap: int, shape: tuple, dt: Any) -> np.ndarray:
    k = (cap, shape, np.dtype(dt).str)
    z = _ZERO.get(k)
    if z is None:
        z = _ZERO[k] = np.zeros((cap, *shape), dt)
    return z


_SIG = {"target_no": "iu", "tgt_cls": "u", "tgt_pos": "f", "tgt_yaw": "f", "tgt_disc_n": "u", "tgt_n_vol": "u", "tgt_keepout_r": "f",
        "tgt_speed": "f", "tgt_alert": "f"}


def _pack_fast(tab: EntityTable, rows: np.ndarray) -> np.ndarray | None:
    """numba 路径（D2-FX-R2-sim 第 2 次：此前约 50 次字段赋值与位运算，30 个识别物约 0.3 ms/次）；列的 dtype 不在核所设范围或
    位段布局与核不一致时返回 None 走 numpy 实现。"""
    if not _BITS_OK:
        return None
    cols = tab.cols
    cap = int(next(iter(cols.values())).shape[0]) if cols else 0
    if cap == 0:
        return None

    def col(name: str, shape: tuple, dt: Any) -> np.ndarray | None:
        c = cols.get(name)
        if c is None:
            return _zeros(cap, shape, dt)
        if c.shape[1:] != shape:
            return None
        k = _SIG.get(name)
        if k is not None and c.dtype.kind not in k:
            return None
        if k is None and c.dtype.kind not in "uib":
            return None
        return c

    args = [col("target_no", (), np.int64), col("tgt_cls", (), np.uint8), col("tgt_pos", (3,), np.float64), col("tgt_yaw", (), np.float64),
            col("tgt_disc_n", (), np.uint16), col("tgt_n_vol", (), np.uint8), col("tgt_keepout_r", (), np.float32),
            col("tgt_speed", (), np.float64), col("tgt_alert", (), np.float32)]
    args += [col(cn, (), np.uint8) for _f, _nm, cn in _BIT_ORDER]
    if any(x is None for x in args):
        return None
    n = rows.size
    a = np.zeros(n, TARGET_LITE32)
    o_pos = np.zeros((n, 3), np.float32)
    o_bits = np.zeros((3, n), np.uint8)
    _pack_nb(rows, *args, a["agent_no"], a["class"], o_pos, a["heading_rad"], a["speed_cms"], a["alertness_u8"], a["discovered_n"],
             a["n_vol"], a["keepout_r_m"], o_bits[0], o_bits[1], o_bits[2])
    a["pos"] = o_pos
    a["state"] = o_bits[0]
    a["best"] = o_bits[1]
    a["flags"] = o_bits[2]
    return a


def pack_rows(tab: EntityTable) -> np.ndarray:
    """实体表 → TargetLite32 行（按 target_no 升序）。速度量化为 cm/s（饱和 ±32767），警觉度 × 255，位段按宽度取模。"""
    rows = tab.rows()
    n = rows.size
    if n and _NB:
        try:
            a = _pack_fast(tab, rows)
        except Exception:
            a = None
        if a is not None:
            return a
    return _pack_np(tab, rows)


def _pack_np(tab: EntityTable, rows: np.ndarray) -> np.ndarray:
    n = rows.size
    a = np.zeros(n, TARGET_LITE32)
    if n == 0:
        return a
    cols = tab.cols
    for fld, col in SCALAR_COLUMNS.items():
        c = cols.get(col)
        if c is not None:
            a[fld] = c[rows]
    sp = cols.get("tgt_speed")
    if sp is not None:
        a["speed_cms"] = np.clip(np.round(sp[rows].astype(np.float64) * 100.0), -32767, 32767).astype(np.int16)
    al = cols.get("tgt_alert")
    if al is not None:
        a["alertness_u8"] = np.clip(np.round(al[rows].astype(np.float64) * 255.0), 0, 255).astype(np.uint8)
    for fld, parts in BIT_COLUMNS.items():
        acc = np.zeros(n, np.uint32)
        bits = TARGET_LITE32_BITS[fld]
        for name, col in parts.items():
            c = cols.get(col)
            if c is None:
                continue
            shift, width = int(bits[name][0]), int(bits[name][1])
            acc |= (c[rows].astype(np.uint32) & np.uint32((1 << width) - 1)) << np.uint32(shift)
        a[fld] = acc.astype(np.uint8)
    return a


class TapTargets:
    """`state/sim-core/targets` 发布 stage（M08-FR-217）。`publish(bytes)` 由组合根注入（总线 publisher 的 put）。"""

    def __init__(self, table: Callable[[], EntityTable | None], publish: Callable[[bytes], Any], *,
                 roster_version: Callable[[], int], wall_ns: Callable[[], int] | None = None,
                 min_wall_ns: int = 50_000_000) -> None:
        self.table = table
        self.publish = publish
        self.roster_version = roster_version
        self.wall_ns = wall_ns
        self.min_wall_ns = int(min_wall_ns)
        self.last_wall = -(1 << 62)
        self.published = 0
        self.skipped = 0
        self.enabled = True
        self.last: dict | None = None  # 最近一次发布的载荷（批处理与测试读取）
        self._last_empty = False

    def __call__(self, S: FleetState, ctx: StageCtx) -> None:
        if not self.enabled:
            return
        tab = self.table()
        if tab is None:
            return
        n = len(tab)
        if n == 0 and self._last_empty:
            return  # 没有识别物：只在由有变无时发一次空帧
        wall = self.wall_ns() if self.wall_ns is not None else 0
        fastfwd = ctx.clock is not None and getattr(ctx.clock, "rate", 1.0) > 1.0
        if fastfwd and wall - self.last_wall < self.min_wall_ns:
            self.skipped += 1
            return
        rows = pack_rows(tab)
        msg = {"v": 1, "t_sim_ns": int(ctx.t_ns), "roster_version": int(self.roster_version()), "rows": rows.tobytes()}
        self.last = msg
        self.publish(msgpack.packb(msg, use_bin_type=True))
        self.last_wall = wall
        self.published += 1
        self._last_empty = n == 0


def warmup() -> None:
    """打包核按运行期签名编译或读缓存（M18、M19、M20 装配后的列 dtype；缺列以 uint8 零列代替）。"""
    if not _NB:
        return

    class _Tab:
        def __init__(self) -> None:
            from awr.sim.core.entities import M08_FIELDS
            from awr.sim.targets.block import FIELDS

            self.cols = {"target_no": np.zeros(4, M08_FIELDS["target_no"][0])}
            for k, (dt, shape) in FIELDS.items():
                self.cols[k] = np.zeros((4, *shape), dt)
            for k in ("tgt_best_level", "tgt_captured", "tgt_best_sensor", "tgt_relay"):
                self.cols[k] = np.zeros(4, np.uint8)

        def rows(self) -> np.ndarray:
            return np.arange(2, dtype=np.int64)

    t = _Tab()
    _pack_fast(t, t.rows())  # type: ignore[arg-type]
    del t.cols["tgt_relay"]
    _pack_fast(t, t.rows())  # type: ignore[arg-type]
