"""`LosCache` 的原 OrderedDict 实现（D2-FX-R2-perception 之前的 `awr/sim/targets/los_cache.py`，逐字保留逻辑），只作对拍参考。"""

from __future__ import annotations

import math
from collections import OrderedDict
from typing import Any

import numpy as np

from awr.sim.targets.los_cache import RAY_FRAC, WHICH_BITS, LosResult

_POP = (0, 1, 1, 2, 1, 2, 2, 3)


class _Bucket:
    __slots__ = ("cap", "rate", "t_ns", "tokens")

    def __init__(self, rate_per_s: float, cap: float) -> None:
        self.rate = float(rate_per_s)
        self.cap = float(cap)
        self.tokens = float(cap)
        self.t_ns: int | None = None

    def refill(self, t_ns: int) -> None:
        if self.t_ns is None:
            self.t_ns = int(t_ns)
            return
        dt = (int(t_ns) - self.t_ns) * 1e-9
        if dt > 0.0:
            self.tokens = min(self.cap, self.tokens + dt * self.rate)
            self.t_ns = int(t_ns)
        elif dt < 0.0:
            self.tokens, self.t_ns = self.cap, int(t_ns)


class RefLosCache:
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
        self.d: OrderedDict[tuple, list] = OrderedDict()
        self.stats = {"rays": 0, "hits": 0, "stale": 0, "deferred": 0, "requests": 0}
        self.rays_by_owner: dict[str, int] = {"M18": 0, "M19": 0, "M20": 0}

    def clear(self) -> None:
        self.d.clear()

    def _key(self, a, b) -> tuple:
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

    def _cast(self, A, B):
        if self.world is None or len(A) == 0:
            return np.ones(len(A), bool)
        out = np.empty(len(A), bool)
        for i in range(0, len(A), 64):
            out[i:i + 64] = np.asarray(self.world.los_batch(A[i:i + 64], B[i:i + 64], step_m=self.step_m, eps_m=self.eps_m), bool)
        return out

    def get_batch(self, A, B, H, owner, t_ns, which="all") -> list[LosResult]:
        need = WHICH_BITS[which]
        n_need = _POP[need]
        A = np.asarray(A, float).reshape(-1, 3)
        B = np.asarray(B, float).reshape(-1, 3)
        H = np.asarray(H, float).reshape(-1)
        out: list[LosResult | None] = [None] * len(A)
        cast_a, cast_b, jobs = [], [], []
        t = int(t_ns)
        for i in range(len(A)):
            self.stats["requests"] += 1
            key = self._key(A[i], B[i])
            e = self.d.get(key)
            fresh = e is not None and t - e[2] <= self.ttl_ns and t >= e[2]
            if fresh and (e[0] & need) == need:
                self.stats["hits"] += 1
                self.d.move_to_end(key)
                vis = e[1] & need
                out[i] = LosResult(_POP[vis] / n_need, vis, False, False)
                continue
            todo = need & ~(e[0] if fresh else 0)
            k = _POP[todo]
            if self._tokens(owner, k, t):
                for bit, frac in enumerate(RAY_FRAC):
                    if todo & (1 << bit):
                        cast_a.append(A[i])
                        cast_b.append(np.array([B[i, 0], B[i, 1], B[i, 2] + frac * H[i]]))
                jobs.append((i, key, todo))
                self.stats["rays"] += k
                self.rays_by_owner[owner] = self.rays_by_owner.get(owner, 0) + k
                continue
            if e is not None and 0 <= t - e[2] <= self.stale_ns and (e[0] & need) == need:
                self.stats["stale"] += 1
                vis = e[1] & need
                out[i] = LosResult(_POP[vis] / n_need, vis, True, False)
            else:
                self.stats["deferred"] += 1
                out[i] = LosResult(0.0, 0, False, True)
        if jobs:
            vis_all = self._cast(np.asarray(cast_a), np.asarray(cast_b))
            p = 0
            for i, key, todo in jobs:
                e = self.d.get(key)
                if e is None or not (0 <= t - e[2] <= self.ttl_ns):
                    e = [0, 0, t]
                computed, visible = e[0], e[1]
                for bit in range(3):
                    if todo & (1 << bit):
                        computed |= 1 << bit
                        visible = visible | (1 << bit) if vis_all[p] else visible & ~(1 << bit)
                        p += 1
                self.d[key] = [computed, visible, t if todo == computed or e[2] == t else e[2]]
                self.d.move_to_end(key)
                vis = visible & need
                out[i] = LosResult(_POP[vis] / n_need, vis, False, False)
            while len(self.d) > self.max_entries:
                self.d.popitem(last=False)
        return out  # type: ignore[return-value]

    def peek(self, a, b, which="top", t_ns=0, *, h_m=1.0):
        need = WHICH_BITS[which]
        e = self.d.get(self._key(np.asarray(a, float).reshape(3), np.asarray(b, float).reshape(3)))
        if e is None or (e[0] & need) != need:
            return None
        age = int(t_ns) - int(e[2])
        if not (0 <= age <= self.stale_ns):
            return None
        vis = e[1] & need
        return LosResult(_POP[vis] / _POP[need], vis, age > self.ttl_ns, False), age * 1e-9
