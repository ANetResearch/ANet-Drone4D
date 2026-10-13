"""共享视线缓存与射线预算 `LosCache`（M18-FR-023；M18-M19 PRD §6.9；AWR-04 §6.3）。每会话（每个 sim-core）一个实例，
M18（physical 视觉顶部射线）自用，M19（感知三条射线、雷达与 MID-360 中心射线）与 M20（站位复核）经 `shared_cache()` 使用。

- 射线端点：观察点 a → 识别物脚点 b 上方 `{0.95, 0.5, 0.05}·h`（顶部、中心、底部，位 0、1、2）；`los_batch(step 1 m, eps 0.5 m)`，
  每批 ≤ 64 对（M04）。未装配世界几何时一律可见（D1 水平地面退化）。
- 缓存键：（a 的 5 m 三维格，b 的 2 m 三维格）；值为已算位掩码、可见位掩码与 t_ns；有效期 1 s【仿真】；旧值可用 ≤ 2 s；≤ 4096 条，LRU。
  同一条目的已算位可被其他请求复用（M18 只请求顶部位，M19 的中心射线请求可复用其结果）。
- 令牌桶（按仿真时间惰性补充）：总桶容量 8、补充速率 `(250 − 8)` 条/s，使任意 1 s【仿真】窗口内射线 ≤ 250（M19-NFR-002；PRD 写
  "每 tick 补 250/tick_hz"，按该写法窗口上界为 258，取下调速率满足 NFR，见实现报告偏差）；子配额 M18 25 条/s、M20 25 条/s
  （各自容量 3），所有者请求须同时有总桶与子桶令牌；其余归 M19。
- 一次请求的射线必须全部可得：不足时用 ≤ 2 s 的旧值并标 `stale`，否则 `deferred`（调用方冻结计时，PRD §6.9）。
- 确定性：结果只取决于调用序列（stage 顺序固定、按（机体序号，识别物序号）升序），不进 checkpoint（恢复后为空缓存）。
- `peek(a, b, which, t_ns)`：只读查询缓存（不消耗令牌、不移动 LRU、不计入统计），返回 `(LosResult, age_s)` 或 None（d2-WP-06-to-M18
  第 2 条；M19 逐对读数的视线与声阵列屏障用，WP-19b）。
- 存储（D2-FX-R2-perception）：条目、索引、LRU 与令牌桶在 `kernels_los.py` 的数组上由 numba 处理（此前 `OrderedDict` 上逐对 Python
  约 3.8 µs/对）；语义与次序不变（`tests/perception/test_kernels.py` 以原实现 `tests/perception/ref_los.py` 对拍）。`query_lists`
  可直接接收调用方算好的格键（M19 在成像核内算出）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from . import kernels_los as KL

__all__ = ["RAY_FRAC", "LosCache", "LosResult", "shared_cache"]

RAY_FRAC = (0.95, 0.5, 0.05)  # 顶部、中心、底部
WHICH_BITS = {"top": 0b001, "center": 0b010, "bottom": 0b100, "all": 0b111}
_POP = (0, 1, 1, 2, 1, 2, 2, 3)
Owner = Literal["M18", "M19", "M20"]


@dataclass(frozen=True)
class LosResult:
    k_los: float      # 所请求射线中可见的比例（3 条：0、1/3、2/3、1；1 条：0 或 1）；deferred 时为 0
    mask: int         # 所请求位中可见的位
    stale: bool       # 用了 ≤ 2 s 的旧值
    deferred: bool    # 本周期无结果（预算不足且无可用旧值）


# 各请求位数下按可见位掩码的 k_los 与共享的不可变结果（`LosResult` 冻结，可复用）
_KLOS = {nn: np.array([_POP[m] / nn for m in range(8)]) for nn in (1, 2, 3)}
_RESULTS = {nn: {(m, s, d): LosResult(0.0, 0, False, True) if d else LosResult(_POP[m] / nn, m, s, False)
                 for m in range(8) for s in (False, True) for d in (False, True)} for nn in (1, 2, 3)}


class _Bucket:
    """令牌桶：状态存于 `b = [tokens, rate, cap, t_ns, has_t]`（numba 核原地读写）；`tokens`、`t_ns` 为属性（t_ns 未初始化时为 None）。"""

    __slots__ = ("b",)

    def __init__(self, rate_per_s: float, cap: float) -> None:
        self.b = np.array([float(cap), float(rate_per_s), float(cap), 0.0, 0.0])

    @property
    def rate(self) -> float:
        return float(self.b[KL.B_RATE])

    @property
    def cap(self) -> float:
        return float(self.b[KL.B_CAP])

    @property
    def tokens(self) -> float:
        return float(self.b[KL.B_TOK])

    @tokens.setter
    def tokens(self, v: float) -> None:
        self.b[KL.B_TOK] = float(v)

    @property
    def t_ns(self) -> int | None:
        return int(self.b[KL.B_T]) if self.b[KL.B_HAS] else None

    @t_ns.setter
    def t_ns(self, v: int | None) -> None:
        if v is None:
            self.b[KL.B_HAS] = 0.0
        else:
            self.b[KL.B_T] = float(int(v))
            self.b[KL.B_HAS] = 1.0

    def refill(self, t_ns: int) -> None:
        if self.t_ns is None:
            self.t_ns = int(t_ns)
            return
        dt = (int(t_ns) - self.t_ns) * 1e-9
        if dt > 0.0:
            self.tokens = min(self.cap, self.tokens + dt * self.rate)
            self.t_ns = int(t_ns)
        elif dt < 0.0:  # 时钟回退（剧本重置）：重新开始
            self.tokens, self.t_ns = self.cap, int(t_ns)


_NO_SUB = np.zeros(5)


class _DView:
    """`d` 的只读视图（与此前 OrderedDict 的 `get`/`len`/`in` 相同的值：`[computed, visible, t_ns]`）。"""

    def __init__(self, c: LosCache) -> None:
        self._c = c

    def get(self, key: tuple, default: Any = None) -> list | None:
        e = self._c._find(key)
        return default if e < 0 else [int(self._c.COMP[e]), int(self._c.VIS[e]), int(self._c.T[e])]

    def __contains__(self, key: tuple) -> bool:
        return self._c._find(key) >= 0

    def __len__(self) -> int:
        return int(self._c.M[KL.M_SIZE])

    def keys(self) -> list[tuple]:
        """LRU 次序（最久未用在前）。"""
        c = self._c
        out = []
        e = int(c.M[KL.M_HEAD])
        while e >= 0:
            out.append(tuple(int(x) for x in c.KEY[e]))
            e = int(c.NEXT[e])
        return out

    def clear(self) -> None:
        self._c.clear()


class LosCache:
    def __init__(self, world: Any = None, *, limit_per_s: float = 250.0, burst: int = 8, quotas: dict[str, float] | None = None,
                 sub_burst: float = 3.0, ttl_ns: int = 1_000_000_000, stale_ns: int = 2_000_000_000, max_entries: int = 4096,
                 cell_a_m: float = 5.0, cell_b_m: float = 2.0, step_m: float = 1.0, eps_m: float = 0.5) -> None:
        self.world = world
        self.total = _Bucket(limit_per_s - burst, burst)
        self.subs = {k: _Bucket(v, sub_burst) for k, v in (quotas if quotas is not None else {"M18": 25.0, "M20": 25.0}).items()}
        self.ttl_ns, self.stale_ns = int(ttl_ns), int(stale_ns)
        self.max_entries = int(max_entries)
        self.cell_a, self.cell_b = float(cell_a_m), float(cell_b_m)
        self.step_m, self.eps_m = float(step_m), float(eps_m)
        self._st = np.zeros(5, np.int64)            # rays、hits、stale、deferred、requests（核内累加；`stats` 为其 dict 视图）
        self.rays_by_owner: dict[str, int] = {"M18": 0, "M19": 0, "M20": 0}
        self._alloc(min(self.max_entries, 4096) + 256)
        self.d = _DView(self)                      # key -> [computed, visible, t_ns]（只读视图）
        self._cnt = np.zeros(6, np.int64)

    @property
    def stats(self) -> dict[str, int]:
        """累计统计（只读快照）：rays、hits、stale、deferred、requests。"""
        r, h, s, d, q = self._st.tolist()
        return {"rays": r, "hits": h, "stale": s, "deferred": d, "requests": q}

    @property
    def deferred_total(self) -> int:
        return int(self._st[3])

    # ------------------------------------------------------------ 表（kernels_los）
    def _alloc(self, cap: int) -> None:
        """容量为 cap 的空表（索引表长取 ≥ 2·cap 的 2 的幂）。"""
        h = 1
        while h < 2 * cap:
            h *= 2
        self.KEY = np.zeros((cap, 6), np.int64)
        self.HSH = np.zeros(cap, np.uint64)
        self.HT = np.full(h, -1, np.int64)
        self.COMP = np.zeros(cap, np.int64)
        self.VIS = np.zeros(cap, np.int64)
        self.T = np.zeros(cap, np.int64)
        self.PREV = np.full(cap, -1, np.int64)
        self.NEXT = np.zeros(cap, np.int64)
        self.M = np.zeros(5, np.int64)          # head、tail、size、free、hwm（kernels_los.M_*）
        KL.clear(self.HT, self.PREV, self.NEXT, self.M)
        self._ver = getattr(self, "_ver", 0) + 1
        self._kt: dict[str, tuple] = {}

    def _grow(self, need_free: int) -> None:
        """空位不足 need_free 时扩容（按 LRU 次序重建，次序与值不变）。"""
        cap = self.KEY.shape[0]
        size = int(self.M[KL.M_SIZE])
        if cap - size >= need_free:
            return
        old = [(tuple(int(x) for x in self.KEY[e]), int(self.COMP[e]), int(self.VIS[e]), int(self.T[e])) for e in self._lru()]
        self._alloc(max(2 * cap, size + need_free + 256))
        for key, comp, vis, t in old:
            e = KL._insert(self.KEY, self.HSH, self.HT, self.PREV, self.NEXT, self.M, *key)
            self.COMP[e], self.VIS[e], self.T[e] = comp, vis, t

    def kernel_tables(self, owner: str, n: int) -> tuple:
        """供调用方的 numba 核直接查表（M19 成像评估，`perception.kernels.eval_a/eval_b`）：保证至少 n 个空位后返回
        (KEY, HSH, HT, COMP, VIS, T, PREV, NEXT, M, ttl_ns, stale_ns, max_entries, 总桶, 子桶, 有子桶, 累计统计)。数组扩容后重建。"""
        if self.KEY.shape[0] - int(self.M[KL.M_SIZE]) < n:
            self._grow(n)
        kt = self._kt.get(owner)
        if kt is None:
            sub = self.subs.get(owner)
            kt = self._kt[owner] = (self.KEY, self.HSH, self.HT, self.COMP, self.VIS, self.T, self.PREV, self.NEXT, self.M, self.ttl_ns,
                                    self.stale_ns, self.max_entries, self.total.b, sub.b if sub is not None else _NO_SUB, sub is not None, self._st)
        return kt

    def cast_jobs(self, owner: str, ra: NDArray, rb: NDArray, nr: int, rays: int) -> NDArray:
        """`kernel_tables` 路径的射线投射（与 `query_raw` 相同：`los_batch` 每批 ≤ 64 对，计入所有者的射线数）。"""
        self.rays_by_owner[owner] = self.rays_by_owner.get(owner, 0) + int(rays)
        return np.ascontiguousarray(self._cast(ra[:nr], rb[:nr]), dtype=np.bool_)

    def _lru(self) -> list[int]:
        out = []
        e = int(self.M[KL.M_HEAD])
        while e >= 0:
            out.append(e)
            e = int(self.NEXT[e])
        return out

    def _find(self, key: Any) -> int:
        k = [int(x) for x in key]
        return int(KL.find(self.KEY, self.HSH, self.HT, k[0], k[1], k[2], k[3], k[4], k[5]))

    def set_world(self, world: Any) -> None:
        if world is not self.world:
            self.world = world
            self.clear()

    def clear(self) -> None:
        KL.clear_used(self.HSH, self.HT, self.PREV, self.NEXT, self.M)

    # ------------------------------------------------------------ 内部
    def _key(self, a: NDArray, b: NDArray) -> tuple:
        """（a 的 5 m 格，b 的 2 m 格）：标量 `math.floor`（与逐元素 `np.floor` 同一 IEEE 除法与取整，结果相同，开销小一个量级）。"""
        ca, cb = self.cell_a, self.cell_b
        return (math.floor(float(a[0]) / ca), math.floor(float(a[1]) / ca), math.floor(float(a[2]) / ca),
                math.floor(float(b[0]) / cb), math.floor(float(b[1]) / cb), math.floor(float(b[2]) / cb))

    def _tokens(self, owner: str, n: int, t_ns: int) -> bool:
        self.total.refill(t_ns)
        sub = self.subs.get(owner)
        if sub is not None:
            sub.refill(t_ns)
        if self.total.tokens + 1e-9 < n or (sub is not None and sub.tokens + 1e-9 < n):
            return False
        self.total.tokens -= n
        if sub is not None:
            sub.tokens -= n
        return True

    def _cast(self, A: NDArray, B: NDArray) -> NDArray[np.bool_]:
        if self.world is None or len(A) == 0:
            return np.ones(len(A), bool)
        if len(A) <= 64:                                   # 一批（与按 64 分批相同）
            return np.asarray(self.world.los_batch(A, B, step_m=self.step_m, eps_m=self.eps_m), bool)
        out = np.empty(len(A), bool)
        for i in range(0, len(A), 64):
            out[i:i + 64] = np.asarray(self.world.los_batch(A[i:i + 64], B[i:i + 64], step_m=self.step_m, eps_m=self.eps_m), bool)
        return out

    # ------------------------------------------------------------ 接口
    def get(self, a: NDArray, b: NDArray, rays: Literal[1, 3], owner: Owner, t_ns: int, which: Literal["top", "center", "all"] = "all",
            *, h_m: float = 1.0) -> LosResult:
        """一对（观察点 a，识别物脚点 b）的视线；`rays` 与 `which` 一致（3 对应 all；1 对应 top 或 center）。"""
        return self.get_batch(np.asarray(a, float).reshape(1, 3), np.asarray(b, float).reshape(1, 3), np.array([h_m]), owner, t_ns,
                              which if rays == 1 else "all")[0]

    def get_batch(self, A: NDArray, B: NDArray, H: Sequence[float] | NDArray, owner: Owner, t_ns: int,
                  which: Literal["top", "center", "all"] = "all") -> list[LosResult]:
        """按给定顺序（调用方保证（机体序号，识别物序号）升序）处理多对；射线一次 `los_batch`（每 64 对一批）。"""
        out = self.query_raw(A, B, H, owner, t_ns, which)
        res = _RESULTS[_POP[WHICH_BITS[which]]]
        return [res[(r[0], r[1] != 0, r[2] != 0)] for r in out.tolist()]

    def _keys(self, A: NDArray, B: NDArray) -> NDArray:
        """一批（a，b）的缓存键 [n, 6]（int64）：与逐对 `_key` 同一 IEEE 除法与取整（`np.floor(x / cell)` 与
        `math.floor(float(x) / cell)` 逐位相同）；含非有限坐标时退回逐对 `_key`（与此前一样抛出）。A 为 (3,) 时为同一观察点。"""
        n = len(B)
        G = np.empty((n, 6))
        np.divide(A, self.cell_a, out=G[:, :3])
        np.divide(B, self.cell_b, out=G[:, 3:])
        if not np.isfinite(G).all():
            A2 = np.broadcast_to(A, (n, 3))
            return np.array([self._key(A2[i], B[i]) for i in range(n)], np.int64).reshape(n, 6)
        return np.floor(G).astype(np.int64)

    def query(self, A: NDArray, B: NDArray, H: Sequence[float] | NDArray, owner: Owner, t_ns: int,
              which: Literal["top", "center", "all"] = "all", *, keys: Any = None) -> tuple[NDArray, NDArray, NDArray, NDArray]:
        """`get_batch` 的数组形式：返回 (k_los float64, mask int64, stale bool, deferred bool)，语义与次序与 `get_batch` 相同。"""
        out = self.query_raw(A, B, H, owner, t_ns, which, keys=keys)
        mask = out[:, 0].copy()
        return _KLOS[_POP[WHICH_BITS[which]]][mask], mask, out[:, 1] != 0, out[:, 2] != 0

    def query_lists(self, A: NDArray, B: NDArray, H: Sequence[float] | NDArray, owner: Owner, t_ns: int,
                    which: Literal["top", "center", "all"] = "all", *, keys: Any = None) -> tuple[list[int], list[bool], list[bool]]:
        """同 `query_raw`，返回 (可见位, stale, deferred) 三个列表。"""
        out = self.query_raw(A, B, H, owner, t_ns, which, keys=keys)
        return out[:, 0].tolist(), (out[:, 1] != 0).tolist(), (out[:, 2] != 0).tolist()

    def query_raw(self, A: NDArray, B: NDArray, H: Sequence[float] | NDArray, owner: Owner, t_ns: int,
                  which: Literal["top", "center", "all"] = "all", *, keys: Any = None) -> NDArray:
        """核心（D2-FX-R2-perception）：返回 out[n, 5] int64 =（所请求位中可见的位, stale, deferred, 作业, 待算位）。

        A 为 (n, 3) 或 (3,)（同一观察点）；`keys` 可由调用方给出（[n, 6] int64 或 6 元组序列，与 `_key(a, b)` 相同；M19 在成像核内
        算出）。逐对的命中、令牌、旧值与延期及射线端点在 `kernels_los.query_scan` 中完成，射线结果在 `apply_jobs` 中回写。"""
        need = WHICH_BITS[which]
        if not (A.__class__ is np.ndarray and A.dtype == np.float64 and A.flags.c_contiguous):
            A = np.ascontiguousarray(A, dtype=np.float64)
        A2 = A.reshape(1, 3) if A.ndim == 1 else A.reshape(-1, 3)
        if not (B.__class__ is np.ndarray and B.dtype == np.float64 and B.ndim == 2 and B.flags.c_contiguous):
            B = np.ascontiguousarray(np.asarray(B, float).reshape(-1, 3))
        n = B.shape[0]
        out = np.zeros((n, 5), np.int64)
        if n == 0:
            return out
        if keys is None:
            K = self._keys(A2[0] if A2.shape[0] == 1 else A2, B)
        elif keys.__class__ is np.ndarray and keys.dtype == np.int64 and keys.flags.c_contiguous:
            K = keys if keys.ndim == 2 else keys.reshape(n, 6)
        else:
            K = np.array(keys, np.int64).reshape(n, 6)
        if not (H.__class__ is np.ndarray and H.dtype == np.float64 and H.flags.c_contiguous):
            H = np.ascontiguousarray(np.asarray(H, float).reshape(-1))
        if self.KEY.shape[0] - int(self.M[KL.M_SIZE]) < n:
            self._grow(n)
        t = int(t_ns)
        sub = self.subs.get(owner)
        ra = np.empty((3 * n, 3))
        rb = np.empty((3 * n, 3))
        cnt = self._cnt
        n_jobs = KL.query_scan(self.KEY, self.HSH, self.HT, self.COMP, self.VIS, self.T, self.PREV, self.NEXT, self.M, K, n, need, t,
                               self.ttl_ns, self.stale_ns, self.total.b, sub.b if sub is not None else _NO_SUB, sub is not None, A2, B, H, out,
                               ra, rb, cnt, self._st)
        if n_jobs:
            self.rays_by_owner[owner] = self.rays_by_owner.get(owner, 0) + int(cnt[3])
            nr = int(cnt[5])
            vis_all = np.ascontiguousarray(self._cast(ra[:nr], rb[:nr]), dtype=np.bool_)
            KL.apply_jobs(self.KEY, self.HSH, self.HT, self.COMP, self.VIS, self.T, self.PREV, self.NEXT, self.M, K, n, need, t, self.ttl_ns,
                          self.max_entries, out, vis_all)
        return out

    def blocked_batch(self, a: NDArray, B: NDArray, t_ns: int, which: Literal["top", "center", "all"] = "center") -> NDArray:
        """只读批量：同一观察点 a 到各脚点 B 的缓存射线（≤ 2 s 的旧值也算）在所请求位上已算且全部被遮挡（M19 声阵列屏障；不消耗令牌、
        不移动 LRU、不计统计；与逐对 `peek` 的判定相同）。返回 bool[n]。"""
        B = np.ascontiguousarray(np.asarray(B, float).reshape(-1, 3))
        n = len(B)
        out = np.zeros(n, np.int64)
        if n:
            K = self._keys(np.asarray(a, float).reshape(3), B)
            KL.peek_rows(self.KEY, self.HSH, self.HT, self.COMP, self.VIS, self.T, K, n, int(t_ns), self.stale_ns, WHICH_BITS[which], out)
        return out != 0

    def peek(self, a: NDArray, b: NDArray, which: Literal["top", "center", "all"] = "top", t_ns: int = 0, *, h_m: float = 1.0,
             ) -> tuple[LosResult, float] | None:
        """只读：缓存中（观察点 a，识别物脚点 b）的视线（≤ 2 s 的旧值也返回，`stale` 标记超过 1 s 的值）与年龄（s）。`h_m` 不参与
        键（键只取 a、b 的格），保留以与 `get` 对称。未命中或所需位未算过时 None。"""
        need = WHICH_BITS[which]
        e = self._find(self._key(np.asarray(a, float).reshape(3), np.asarray(b, float).reshape(3)))
        if e < 0 or (int(self.COMP[e]) & need) != need:
            return None
        age = int(t_ns) - int(self.T[e])
        if not (0 <= age <= self.stale_ns):
            return None
        vis = int(self.VIS[e]) & need
        return LosResult(_POP[vis] / _POP[need], vis, age > self.ttl_ns, False), age * 1e-9


_FALLBACK: list[LosCache] = []


def shared_cache() -> LosCache:
    """当前 sim-core 的共享缓存（M18 插件已装配时取其运行时实例；否则模块级单例，供单测与 M19、M20 独立使用）。"""
    import sys

    plug = sys.modules.get("awr.sim.targets.plugin")
    fn = getattr(plug, "runtime", None)
    if callable(fn):
        rt = fn(create=False)
        if rt is not None:
            return rt.los
    if not _FALLBACK:
        _FALLBACK.append(LosCache())
    return _FALLBACK[0]
