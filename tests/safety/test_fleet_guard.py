"""M09-AC-022：FleetGuard——numba 与 numpy oracle、与 O(N²) 暴力的一致性；4 片合并与单次全机群扫描一致、每对恰好处理一次；
相向各 5 m/s：低优先级方 HOLD/SEPARATION、最小距离 ≥ 3 m；间距恢复 2 s 后回到 FLYING/HOVER；同一对 60 s 内第 3 次让行发
OSCILLATION。M09-AC-023：相对 24 m/s 穿越（两端点距离均 > 1 m、中间最小 0.5 m）判为碰撞，双方 CRASHED/COLLISION_UAV。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from safelib import Harness, UnitRig

from awr.contracts.enums import FlightState
from awr.sim.safety import kernels as KN
from awr.sim.safety.fleet_guard import priority_keys
from awr.world.georef.frames import enu_to_ned

FS = FlightState
ARGS = dict(cell_m=10.0, T=3.0, min_sep=3.0, warn=10.0, zband=10.0, sweep_s=0.1)


def _run(fn, p, v, idx, shards, rcol):
    n = p.shape[0]
    o = [np.full(n, KN.BIG), np.full(n, -1, np.int64), np.full(n, KN.BIG), np.full(n, -1, np.int64)]
    pairs_all = []
    ncand = 0
    for k in shards:
        pairs = np.zeros((4096, KN.PAIR_COLS))
        npairs, nc = fn(p, v, idx, k, 4, ARGS["cell_m"], ARGS["T"], ARGS["min_sep"], ARGS["warn"], ARGS["zband"], rcol,
                        ARGS["sweep_s"], o[0], o[1], o[2], o[3], pairs, 4096)
        ncand += nc
        pairs_all += [tuple(r) for r in pairs[:npairs]]
    return o, sorted(pairs_all), ncand


def _brute(p, v, idx):
    T = ARGS["T"]
    cnt = 0
    for a in range(len(idx)):
        for b in range(len(idx)):
            i, j = int(idx[a]), int(idx[b])
            if j <= i:
                continue
            ri = np.linalg.norm(v[i]) * T
            rj = np.linalg.norm(v[j]) * T
            lim = ARGS["min_sep"] + ri + rj
            d = p[j] - p[i]
            if math.hypot(d[0], d[1]) < max(lim, ARGS["warn"]) and abs(d[2]) < max(lim, ARGS["zband"]):
                cnt += 1
    return cnt


@pytest.mark.parametrize("layout", ["random", "grid15", "grid15_fast"])
def test_kernel_oracle_bruteforce(layout: str) -> None:
    rng = np.random.default_rng({"random": 1, "grid15": 2, "grid15_fast": 3}[layout])
    n = 240
    if layout == "random":
        p = np.c_[rng.uniform(-150, 150, (n, 2)), rng.uniform(30, 60, n)]
        v = np.c_[rng.normal(0, 3, (n, 2)), rng.normal(0, 0.5, n)]
    else:
        g = np.stack(np.meshgrid(np.arange(16), np.arange(16)), -1).reshape(-1, 2)[:n] * 15.0
        p = np.c_[g, np.full(n, 60.0)]
        ang = rng.uniform(0, 2 * np.pi, n)
        sp = 12.0 if layout == "grid15_fast" else 3.0
        v = np.c_[sp * np.cos(ang), sp * np.sin(ang), np.zeros(n)]
    idx = np.sort(rng.choice(n, n - 5, replace=False)).astype(np.int64)  # 含非活动槽
    rcol = np.full(n, 0.49)
    o_np, pairs_np, nc_np = _run(KN.fleet_scan_np, p, v, idx, range(4), rcol)
    assert nc_np == _brute(p, v, idx)  # 4 片合起来每对恰好处理一次
    if KN.HAVE_NUMBA:
        o_nb, pairs_nb, nc_nb = _run(KN.fleet_scan, p, v, idx, range(4), rcol)
        assert nc_nb == nc_np
        for a, b in zip(o_nb, o_np, strict=True):
            assert np.array_equal(a, b)  # 布尔、下标与 CPA 数值逐位一致（差 ≤ 1e-9 m）
        assert len(pairs_nb) == len(pairs_np)
        assert np.max(np.abs(np.asarray(pairs_nb) - np.asarray(pairs_np)), initial=0.0) <= 1e-9
    # 分片合并 = 单次全机群扫描（以 n_shards = 1 的暴力扫描为参照）
    o1, _pairs1, nc1 = _run(lambda *a: KN.fleet_scan_np(*a[:3], 0, 1, *a[5:]), p, v, idx, [0], rcol)
    assert nc1 == nc_np
    for a, b in zip(o1, o_np, strict=True):
        assert np.array_equal(a, b)


def test_head_on_semantics() -> None:
    p = np.array([[0.0, 0.0, 60.0], [25.0, 0.0, 60.0]])
    v = np.array([[5.0, 0.0, 0.0], [-5.0, 0.0, 0.0]])
    _o, pairs, _ = _run(KN.fleet_scan_np, p, v, np.array([0, 1]), range(4), np.full(2, 0.49))
    assert len(pairs) == 1 and pairs[0][3] < 1e-9  # 25 m 时判冲突（CPA 0）
    p[1, 0] = 40.0
    _o, pairs, _ = _run(KN.fleet_scan_np, p, v, np.array([0, 1]), range(4), np.full(2, 0.49))
    assert pairs == []  # 40 m 时 CPA 在 4 s，超出 3 s 视界


def test_priority_keys() -> None:
    k = priority_keys(np.array([FS.FLYING, FS.ELAND, FS.RTL, FS.FLYING]), np.array([False, True, True, False]),
                      np.zeros(4), np.array([0.8, 0.8, 0.8, 0.2]), np.array([1, 1, 1, 2]), np.array([5, 6, 7, 8]))
    assert list(k[:, 0]) == [0, 2, 1, 0]
    assert k[3, 2] > k[0, 2]  # 电量低者 K3 大（优先）
    assert k[0, 3] == 4 and k[3, 3] == 2  # OPERATOR 4、MISSION 2


def test_swept_collision_24mps() -> None:
    r = UnitRig(n=2)
    for s in (0, 1):
        r.set(s, int(FS.FLYING), 1)
    # 两机相对 24 m/s 横穿：当前与 0.1 s 前的相对位置距离均 > 1 m，中间最小 0.5 m
    r.S.p[0] = enu_to_ned(np.array([0.0, 0.0, 50.0]))
    r.S.p[1] = enu_to_ned(np.array([-1.2, 0.5, 50.0]))  # 0.1 s 前在 (+1.2, 0.5)，其间最近 0.5 m
    r.S.v[0] = enu_to_ned(np.array([12.0, 0.0, 0.0]))
    r.S.v[1] = enu_to_ned(np.array([-12.0, 0.0, 0.0]))
    r.S.touch()
    d_now = math.hypot(-1.2, 0.5)
    d_prev = math.hypot(-1.2 + 24.0 * 0.1, 0.5)
    assert d_now > 1.0 and d_prev > 1.0
    for k in range(4):
        r.ctx.shard = (k, 4)
        r.rt.run("fleet_guard", r.ctx)
    r.tick(1)
    assert r.fs(0) == (int(FS.CRASHED), 2) and r.fs(1) == (int(FS.CRASHED), 2)
    assert [e["code"] for e in r.events()].count("SAF.SEP.COLLISION") == 2


def test_head_on_yield_and_restore() -> None:
    """两机相距 60 m 相向（各 5 m/s 巡航后进入 3 s 视界）：低优先级方（agent_no 大，K5）HOLD/SEPARATION；高优先级方随后同样
    让行（低优先级方已悬停而冲突仍在）；最小距离 ≥ 3 m；间距恢复（> 5 m 且 CPA > 3 m）持续 2 s 后回到 FLYING/HOVER。"""
    h = Harness(n=2, spacing=60.0, limits="px4_default")
    try:
        h.ready()
        h.gcs_age_ms = 0
        a, b = h.takeoff_all(10.0)
        pa, pb = h.pos(a), h.pos(b)
        h.goto([pb[0], pb[1], 10.0], uav=a, wait=False, speed_mps=5.0)
        h.goto([pa[0], pa[1], 10.0], uav=b, wait=False, speed_mps=5.0)
        dmin = np.inf
        t_hold = None
        t_end = h.core.clock.t_ns + int(25e9)
        while h.core.clock.t_ns < t_end:
            h.advance(0.1)
            dmin = min(dmin, float(np.linalg.norm(h.pos(a) - h.pos(b))))
            if t_hold is None and h.fs(b) == ("HOLD", "SEPARATION"):
                t_hold = h.core.clock.t_ns
                assert h.fs(a)[0] == "FLYING"  # 先由低优先级方让行
            if t_hold is not None and h.fs(b) == ("FLYING", "HOVER"):
                break
        assert t_hold is not None
        assert h.fs(b) == ("FLYING", "HOVER") and h.fs(a)[0] == "FLYING", (h.state(a), h.state(b))
        assert dmin >= 3.0, dmin
        cb = h.codes(b)
        assert "SAF.SEP.AVOIDING" in cb and "SAF.SEP.RESTORED" in cb and "SAF.SEP.CONFLICT" not in cb[:1]
        assert h.svc.min_separation_m() >= 3.0
    finally:
        h.close()


def test_oscillation_third_yield() -> None:
    r = UnitRig(n=2)
    fg = r.rt.fg
    sb = r.sb
    for k, t in enumerate((1.0, 20.0, 45.0)):
        fg._record_yield(1, 0, t)
        assert int(sb["yield_state"][1]) == (2 if k == 2 else 1)
    assert [e.code for e in r.rt.sink.pending].count(r.rt.code("SAF.SEP.OSCILLATION")) == 1
    # 60 s 窗口外不计
    r2 = UnitRig(n=2)
    for t in (1.0, 40.0, 90.0):
        r2.rt.fg._record_yield(1, 0, t)
    assert int(r2.sb["yield_state"][1]) == 1
    # 振荡状态下不自动恢复
    r.set(1, int(FS.HOLD), 3, auto=True)
    sb["sep_m"][1] = 50.0
    sb["cpa_min_m"][1] = 50.0
    for _ in range(40):
        r.ctx.shard = (3, 4)
        r.ctx.tick += 25
        r.ctx.t_ns = r.ctx.tick * 4_000_000
        r.rt.run("fleet_guard", r.ctx)
        r.tick(1)
    assert r.fs(1) == (int(FS.HOLD), 3)
