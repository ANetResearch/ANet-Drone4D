"""`LosCache` 的 numba 表（D2-FX-R2-perception；M18-FR-023、M19-NFR-002；D2-AC-13）。

D2 验收第 1 轮 4.1：M19 每次（机体，成像传感器）评估约 30 对视线查询，此前 `OrderedDict` 上的逐对 Python 逻辑（取键、查表、
新鲜度、令牌、旧值、LRU）约 3.8 µs/对，12 × 30 每仿真秒约 1800 对。本模块把同一逻辑放到数组上：

- 条目：键（a 的 5 m 格、b 的 2 m 格，6 个 int64）、已算位、可见位、t_ns；LRU 双向链表（prev/next，head 最久未用、tail 最近用）；
- 索引：开放寻址（线性探测，表长 2 的幂，≥ 2 × 容量），删除用后移（backward shift），无墓碑；
- 令牌桶：`[tokens, rate, cap, t_ns, has_t]`（float64；t_ns 以浮点存整数纳秒，< 2^53 时精确），补充与扣减的运算与 `_Bucket` 相同。

`query_scan` 对应 `LosCache.query_lists` 的第一遍（命中、令牌、旧值、延期，按给定次序），`apply_jobs` 对应射线结果回写与超额
淘汰。次序、LRU 位置、令牌扣减与统计与 `OrderedDict` 实现逐项相同（`tests/targets/test_los_kernels.py` 以原实现对拍）。
numba 不可用时这些函数按普通 Python 执行（结果相同，只损失性能）。
"""

from __future__ import annotations

import os

import numpy as np

try:
    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f

        return deco(a[0]) if a and callable(a[0]) else deco

__all__ = ["HAVE_NUMBA", "M_FREE", "M_HEAD", "M_HWM", "M_SIZE", "M_TAIL", "apply_jobs", "clear", "clear_used", "find", "peek_rows",
           "pop_front", "query_scan", "warmup"]

M_HEAD, M_TAIL, M_SIZE, M_FREE, M_HWM = 0, 1, 2, 3, 4   # M_HWM（可选第 5 元）：自上次清空以来取用过的最大条目号 + 1
B_TOK, B_RATE, B_CAP, B_T, B_HAS = 0, 1, 2, 3, 4
_POP = np.array([0, 1, 1, 2, 1, 2, 2, 3], np.int64)
_H1 = np.uint64(0x9E3779B97F4A7C15)
_H2 = np.uint64(0xBF58476D1CE4E5B9)
_S31 = np.uint64(31)
_S29 = np.uint64(29)


@njit(cache=True, fastmath=False)
def _hash(k0, k1, k2, k3, k4, k5):
    h = np.uint64(0x84222325CBF29CE4)
    for v in (k0, k1, k2, k3, k4, k5):
        h = (h ^ np.uint64(v)) * _H1
        h = h ^ (h >> _S29)
    h = h * _H2
    return h ^ (h >> _S31)


@njit(cache=True, fastmath=False)
def find(KEY, HSH, HT, k0, k1, k2, k3, k4, k5):
    """条目下标或 -1。"""
    mask = HT.shape[0] - 1
    h = _hash(k0, k1, k2, k3, k4, k5)
    p = np.int64(h & np.uint64(mask))
    while True:
        e = HT[p]
        if e < 0:
            return -1
        if HSH[e] == h and KEY[e, 0] == k0 and KEY[e, 1] == k1 and KEY[e, 2] == k2 and KEY[e, 3] == k3 and KEY[e, 4] == k4 and KEY[e, 5] == k5:
            return e
        p = (p + 1) & mask


@njit(cache=True, fastmath=False)
def _ht_remove(HSH, HT, e):
    mask = HT.shape[0] - 1
    p = np.int64(HSH[e] & np.uint64(mask))
    while HT[p] != e:
        p = (p + 1) & mask
    i = p
    j = p
    while True:
        j = (j + 1) & mask
        x = HT[j]
        if x < 0:
            break
        k = np.int64(HSH[x] & np.uint64(mask))
        # x 的原位 k 在 (i, j] 之内（环形）时不动，否则后移到 i
        if i <= j:
            if i < k <= j:
                continue
        elif i < k or k <= j:
            continue
        HT[i] = x
        i = j
    HT[i] = -1


@njit(cache=True, fastmath=False)
def _unlink(PREV, NEXT, M, e):
    p = PREV[e]
    n = NEXT[e]
    if p >= 0:
        NEXT[p] = n
    else:
        M[M_HEAD] = n
    if n >= 0:
        PREV[n] = p
    else:
        M[M_TAIL] = p


@njit(cache=True, fastmath=False)
def _append(PREV, NEXT, M, e):
    t = M[M_TAIL]
    PREV[e] = t
    NEXT[e] = -1
    if t >= 0:
        NEXT[t] = e
    else:
        M[M_HEAD] = e
    M[M_TAIL] = e


@njit(cache=True, fastmath=False)
def _move_to_end(PREV, NEXT, M, e):
    if M[M_TAIL] == e:
        return
    _unlink(PREV, NEXT, M, e)
    _append(PREV, NEXT, M, e)


@njit(cache=True, fastmath=False)
def _insert(KEY, HSH, HT, PREV, NEXT, M, k0, k1, k2, k3, k4, k5):
    """新条目（调用方保证不存在且有空位），放在 LRU 末尾。"""
    e = M[M_FREE]
    M[M_FREE] = NEXT[e]
    if M.shape[0] > M_HWM and e + 1 > M[M_HWM]:
        M[M_HWM] = e + 1
    KEY[e, 0] = k0
    KEY[e, 1] = k1
    KEY[e, 2] = k2
    KEY[e, 3] = k3
    KEY[e, 4] = k4
    KEY[e, 5] = k5
    h = _hash(k0, k1, k2, k3, k4, k5)
    HSH[e] = h
    mask = HT.shape[0] - 1
    p = np.int64(h & np.uint64(mask))
    while HT[p] >= 0:
        p = (p + 1) & mask
    HT[p] = e
    _append(PREV, NEXT, M, e)
    M[M_SIZE] += 1
    return e


@njit(cache=True, fastmath=False)
def pop_front(KEY, HSH, HT, PREV, NEXT, M):
    """删除最久未用的条目（`OrderedDict.popitem(last=False)`）。"""
    e = M[M_HEAD]
    if e < 0:
        return
    _unlink(PREV, NEXT, M, e)
    _ht_remove(HSH, HT, e)
    NEXT[e] = M[M_FREE]
    M[M_FREE] = e
    M[M_SIZE] -= 1


@njit(cache=True, fastmath=False)
def clear(HT, PREV, NEXT, M):
    HT[:] = -1
    n = NEXT.shape[0]
    for e in range(n):
        PREV[e] = -1
        NEXT[e] = e + 1 if e + 1 < n else -1
    M[M_HEAD] = -1
    M[M_TAIL] = -1
    M[M_SIZE] = 0
    M[M_FREE] = 0
    if M.shape[0] > M_HWM:
        M[M_HWM] = 0


@njit(cache=True, fastmath=False)
def clear_used(HSH, HT, PREV, NEXT, M):
    """与 `clear` 结果相同的清空，只触及用过的部分（D2-FX-R2-sim 第 2 次：感知 full 口径每次调用清空一次，整表重置约 2.5 万次
    写入）：沿 LRU 链把各活动条目的索引槽置 −1（索引表只含活动条目），条目 [0, hwm) 的链表指针恢复为初始空位链（hwm 之后的
    条目从未取用，指针本就是初始值）。M 没有 hwm 元时整表重置。"""
    if M.shape[0] <= M_HWM:
        clear(HT, PREV, NEXT, M)
        return
    mask = HT.shape[0] - 1
    e = M[M_HEAD]
    while e >= 0:
        p = np.int64(HSH[e] & np.uint64(mask))
        while HT[p] != e:
            p = (p + 1) & mask
        HT[p] = -1
        e = NEXT[e]
    n = NEXT.shape[0]
    hw = M[M_HWM]
    for q in range(hw):
        PREV[q] = -1
        NEXT[q] = q + 1 if q + 1 < n else -1
    M[M_HEAD] = -1
    M[M_TAIL] = -1
    M[M_SIZE] = 0
    M[M_FREE] = 0
    M[M_HWM] = 0


@njit(cache=True, fastmath=False)
def _refill(b, t):
    if b[B_HAS] == 0.0:
        b[B_T] = float(t)
        b[B_HAS] = 1.0
        return
    dt = (t - np.int64(b[B_T])) * 1e-9
    if dt > 0.0:
        x = b[B_TOK] + dt * b[B_RATE]
        b[B_TOK] = x if x < b[B_CAP] else b[B_CAP]
        b[B_T] = float(t)
    elif dt < 0.0:
        b[B_TOK] = b[B_CAP]
        b[B_T] = float(t)


@njit(cache=True, fastmath=False)
def _tokens(tot, sub, has_sub, n, t):
    _refill(tot, t)
    if has_sub:
        _refill(sub, t)
    if tot[B_TOK] + 1e-9 < n or (has_sub and sub[B_TOK] + 1e-9 < n):
        return False
    tot[B_TOK] -= n
    if has_sub:
        sub[B_TOK] -= n
    return True


@njit(cache=True, fastmath=False)
def query_scan(KEY, HSH, HT, COMP, VIS, T, PREV, NEXT, M, keys, n, need, t, ttl, stale_ns, tot, sub, has_sub, A, B, H, out, ra, rb, cnt, st):
    """按次序处理 n 对（keys[i]）：命中（新鲜且所需位已算）→ LRU 移到末尾；否则需补算的位数有令牌 → 作业（射线端点按位 0、1、2
    写入 ra、rb：a → b 上方 frac·h）；否则 ≤ stale_ns 的旧值 → stale；否则 deferred。

    out[i] = (可见位, stale, deferred, 作业, 待算位)；cnt = [hits, stale, deferred, rays, jobs, 射线条数]；累计统计
    st = [rays, hits, stale, deferred, requests] 原地加上本次的值。A 为 [n, 3] 或 [1, 3]（同一观察点）。
    同一时刻的令牌请求失败后，不少于该数的请求不再查桶（令牌只按仿真时间补充，结果相同）。"""
    hits = 0
    n_stale = 0
    n_def = 0
    rays = 0
    jobs = 0
    nr = 0
    fail_k = 4
    single = A.shape[0] == 1
    for i in range(n):
        out[i, 0] = 0
        out[i, 1] = 0
        out[i, 2] = 0
        out[i, 3] = 0
        out[i, 4] = 0
        e = find(KEY, HSH, HT, keys[i, 0], keys[i, 1], keys[i, 2], keys[i, 3], keys[i, 4], keys[i, 5])
        fresh = e >= 0 and t - T[e] <= ttl and t >= T[e]
        if fresh and (COMP[e] & need) == need:
            hits += 1
            _move_to_end(PREV, NEXT, M, e)
            out[i, 0] = VIS[e] & need
            continue
        todo = need & ~(COMP[e] if fresh else 0)
        k = _POP[todo]
        if k < fail_k and _tokens(tot, sub, has_sub, float(k), t):
            out[i, 3] = 1
            out[i, 4] = todo
            rays += k
            jobs += 1
            ia = 0 if single else i
            for bit in range(3):
                if todo & (1 << bit):
                    frac = 0.95 if bit == 0 else (0.5 if bit == 1 else 0.05)
                    ra[nr, 0] = A[ia, 0]
                    ra[nr, 1] = A[ia, 1]
                    ra[nr, 2] = A[ia, 2]
                    rb[nr, 0] = B[i, 0]
                    rb[nr, 1] = B[i, 1]
                    rb[nr, 2] = B[i, 2] + frac * H[i]
                    nr += 1
            continue
        if k < fail_k:
            fail_k = k
        if e >= 0 and 0 <= t - T[e] <= stale_ns and (COMP[e] & need) == need:
            n_stale += 1
            out[i, 1] = 1
            out[i, 0] = VIS[e] & need
        else:
            n_def += 1
            out[i, 2] = 1
    cnt[0] = hits
    cnt[1] = n_stale
    cnt[2] = n_def
    cnt[3] = rays
    cnt[4] = jobs
    cnt[5] = nr
    st[0] += rays
    st[1] += hits
    st[2] += n_stale
    st[3] += n_def
    st[4] += n
    return jobs


@njit(cache=True, fastmath=False)
def apply_jobs(KEY, HSH, HT, COMP, VIS, T, PREV, NEXT, M, keys, n, need, t, ttl, max_entries, out, vis_rays):
    """作业（out[i, 3] = 1，按 i 升序）的射线结果回写：vis_rays 按作业次序、每作业按位 0、1、2 依次排列。条目不存在或已过期时从
    (0, 0, t) 起；时刻在全部所需位都新算或原时刻即 t 时取 t，否则保留原时刻；移到 LRU 末尾；最后淘汰到 ≤ max_entries。
    写 out[i, 0]（可见位）。"""
    p = 0
    for i in range(n):
        if out[i, 3] == 0:
            continue
        td = out[i, 4]
        k0 = keys[i, 0]
        k1 = keys[i, 1]
        k2 = keys[i, 2]
        k3 = keys[i, 3]
        k4 = keys[i, 4]
        k5 = keys[i, 5]
        e = find(KEY, HSH, HT, k0, k1, k2, k3, k4, k5)
        if e >= 0 and 0 <= t - T[e] <= ttl:
            computed = COMP[e]
            visible = VIS[e]
            t0 = T[e]
        else:
            computed = 0
            visible = 0
            t0 = t
        for bit in range(3):
            if td & (1 << bit):
                computed |= 1 << bit
                visible = visible | (1 << bit) if vis_rays[p] else visible & ~(1 << bit)
                p += 1
        if e < 0:
            e = _insert(KEY, HSH, HT, PREV, NEXT, M, k0, k1, k2, k3, k4, k5)
        else:
            _move_to_end(PREV, NEXT, M, e)
        COMP[e] = computed
        VIS[e] = visible
        T[e] = t if (td == computed or t0 == t) else t0
        out[i, 0] = visible & need
    while M[M_SIZE] > max_entries:
        pop_front(KEY, HSH, HT, PREV, NEXT, M)


@njit(cache=True, fastmath=False)
def peek_rows(KEY, HSH, HT, COMP, VIS, T, keys, n, t, stale_ns, bits, out):
    """只读批量查询（不移动 LRU、不计统计）：out[i] = 1 当条目存在、0 ≤ t − t_e ≤ stale_ns 且 bits 已算而全部不可见（中心射线被遮挡，
    M19 声阵列屏障）；否则 0。"""
    for i in range(n):
        out[i] = 0
        e = find(KEY, HSH, HT, keys[i, 0], keys[i, 1], keys[i, 2], keys[i, 3], keys[i, 4], keys[i, 5])
        if e < 0:
            continue
        age = t - T[e]
        if age < 0 or age > stale_ns:
            continue
        if (COMP[e] & bits) == bits and (VIS[e] & bits) == 0:
            out[i] = 1


def warmup() -> None:
    """按运行期签名编译或读缓存。"""
    if not HAVE_NUMBA:
        return
    from .los_cache import LosCache

    c = LosCache(None)
    A = np.zeros((2, 3))
    B = np.array([[10.0, 0.0, 0.0], [20.0, 0.0, 0.0]])
    c.get_batch(A, B, np.ones(2), "M19", 1, "all")
    c.get_batch(A, B, np.ones(2), "M18", 2, "top")
    c.get_batch(A, B, np.ones(2), "M19", 3, "all")
    c.peek(A[0], B[0], "top", 3)
    c.query_raw(A[0], B, np.ones(2), "M19", 4, "all", keys=c._keys(A[0], B))
    c.blocked_batch(A[0], B, 4, "center")
    c.clear()
