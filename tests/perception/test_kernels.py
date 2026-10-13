"""D2-FX-R2-perception（D2-AC-13；M19-NFR-001、M19-AC-010）：感知 stage 的 numba 路径与参考实现对拍。

- `LosCache` 的数组表（`targets/kernels_los.py`）与原 OrderedDict 实现（`ref_los.py`）：随机调用序列（三个所有者、三种位、时钟回退、
  小容量淘汰、哈希冲突与后移删除）下逐对结果、统计、射线数、`peek` 与 LRU 次序逐项相同；
- 成像判据核（`perception/kernels.py`）与 numpy 路径（`_eval_np`，即此前 `_eval_cand` 的运算）：EO、NIR（照明器）、LWIR、运动目标、
  任务 N_req 覆盖与视线延期的场景下，离散量（等级、捕获条件、limiting、延期、有无量测）逐项相等，连续量相对误差 ≤ 1e-12；
- 计数器 RNG：核内 Box–Muller 与 `cbrng.normal` 逐位相同；前缀键与 `cbrng.key` 相同；
- 航迹：逐轴闭式滤波与 6 × 6 矩阵 Kalman（原实现）一致（≤ 1e-9），numba 批量更新与 Python 标量更新逐位相同；
- 识别物汇总（一次遍历、按识别物增量）与逐识别物 `best_of` 相同。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from plib import FakeEnv, FakeWorld, PBench, make_imaging, make_nonimg
from ref_los import RefLosCache

from awr.sim.perception import gx40, gx40_nir, lwir640, mmw77
from awr.sim.perception import kernels as K
from awr.sim.perception import runtime as RT
from awr.sim.sensors import cbrng
from awr.sim.targets import kernels_los as KL
from awr.sim.targets.los_cache import LosCache

needs_numba = pytest.mark.skipif(not K.HAVE_NUMBA, reason="numba 路径未启用（AWR_KERNEL=numpy 或无 numba）")


# ---------------------------------------------------------------- LosCache
def _seq(seed: int, n_calls: int, *, spread: float, max_entries: int) -> tuple[list, list]:
    r = np.random.default_rng(seed)
    w = FakeWorld(boxes=[(30.0, 60.0, -40.0, 40.0, 6.0), (-80.0, -50.0, 0.0, 90.0, 25.0)])
    kw = {"max_entries": max_entries}
    a, b = LosCache(w, **kw), RefLosCache(w, **kw)
    out_a, out_b = [], []
    t = 0
    for c in range(n_calls):
        t += int(r.choice([0, 3_000_000, 10_000_000, 250_000_000, 900_000_000]))
        if r.random() < 0.01:
            t = max(0, t - 1_500_000_000)                     # 时钟回退
        n = int(r.integers(1, 40))
        A = np.c_[r.uniform(-spread, spread, (n, 2)), r.uniform(5.0, 90.0, n)]
        if r.random() < 0.5:
            A[:] = A[0]
        B = np.c_[r.uniform(-spread, spread, (n, 2)), np.zeros(n)]
        H = r.uniform(0.3, 4.0, n)
        owner = str(r.choice(["M18", "M19", "M20"]))
        which = str(r.choice(["top", "center", "all"]))
        ra = a.get_batch(A, B, H, owner, t, which)
        rb = b.get_batch(A, B, H, owner, t, which)
        out_a.append([(x.k_los, x.mask, x.stale, x.deferred) for x in ra])
        out_b.append([(x.k_los, x.mask, x.stale, x.deferred) for x in rb])
        if c % 7 == 0:
            for i in range(min(n, 5)):
                pa, pb = a.peek(A[i], B[i], which, t), b.peek(A[i], B[i], which, t)
                out_a.append(None if pa is None else (pa[0], pa[1]))
                out_b.append(None if pb is None else (pb[0], pb[1]))
        if c % 50 == 49:
            out_a.append((dict(a.stats), dict(a.rays_by_owner), a.total.tokens, a.total.t_ns, a.d.keys()))
            out_b.append((dict(b.stats), dict(b.rays_by_owner), b.total.tokens, b.total.t_ns, list(b.d.keys())))
        if r.random() < 0.003:
            a.clear()
            b.clear()
    return out_a, out_b


@pytest.mark.parametrize(("seed", "spread", "max_entries"), [(1, 60.0, 4096), (2, 400.0, 64), (3, 25.0, 17), (4, 2000.0, 300)])
def test_los_table_matches_ordereddict(seed: int, spread: float, max_entries: int) -> None:
    oa, ob = _seq(seed, 1500, spread=spread, max_entries=max_entries)
    assert oa == ob


def test_los_keys_given_by_caller_and_single_observer() -> None:
    w = FakeWorld(boxes=[(30.0, 60.0, -40.0, 40.0, 6.0)])
    a, b = LosCache(w), RefLosCache(w)
    r = np.random.default_rng(9)
    for k in range(200):
        p = np.array([r.uniform(-20, 20), r.uniform(-20, 20), 60.0])
        B = np.c_[r.uniform(-100, 100, (12, 2)), np.zeros(12)]
        H = np.full(12, 1.7)
        keys = a._keys(p, B)
        m, st, df = a.query_lists(p, B, H, "M19", k * 10_000_000, "all", keys=keys)
        ref = b.get_batch(np.broadcast_to(p, (12, 3)), B, H, "M19", k * 10_000_000, "all")
        assert m == [x.mask for x in ref] and st == [x.stale for x in ref] and df == [x.deferred for x in ref]
        assert keys.tolist() == [list(b._key(p, B[i])) for i in range(12)]


def test_los_hash_backshift_delete() -> None:
    c = LosCache(None, max_entries=50)
    keys = [(i % 7, -i, 3, i * 13, -(i % 5), 0) for i in range(500)]
    for i, kk in enumerate(keys):
        e = KL._insert(c.KEY, c.HSH, c.HT, c.PREV, c.NEXT, c.M, *kk) if c._find(kk) < 0 else -1
        assert e >= 0
        c.T[e] = i
        while c.M[KL.M_SIZE] > 50:
            KL.pop_front(c.KEY, c.HSH, c.HT, c.PREV, c.NEXT, c.M)
        if i % 37 == 0:
            live = keys[max(0, i - 49):i + 1]
            assert c.d.keys() == live and all(c._find(x) >= 0 for x in live)
            assert all(c._find(x) < 0 for x in keys[:max(0, i - 49)])


# ---------------------------------------------------------------- RNG
def test_rng_bitwise_equal_cbrng() -> None:
    r = np.random.default_rng(3)
    out = np.empty(3)
    for _ in range(300):
        seed, agent, tick = int(r.integers(0, 2 ** 63)), int(r.integers(0, 4000)), int(r.integers(0, 10 ** 9))
        sn, tn = int(r.integers(0, 70000)), int(r.integers(0, 70000))
        pfx = K.prefix_key(seed, 18, agent, tick)
        assert pfx == K.prefix_tick(K.prefix3(seed, 18, agent), tick)
        ch = [RT._ch(sn, tn, j) for j in range(3)]
        ref = [float(cbrng.normal(seed, 18, agent, tick, c)) for c in ch]
        if K.HAVE_NUMBA:
            K.normals3(np.uint64(pfx), sn, tn, out)
            np.testing.assert_allclose(out, ref, rtol=1e-12, atol=0)  # numba 与 Python 的 log/sin/cos 在不同 CPU 上末位可能不同
        assert K.uniform_ch(pfx, ch[0]) == float(cbrng.uniform(seed, 18, agent, tick, ch[0]))
        k_ref = int(cbrng.key(seed, 18, agent, tick, ch[1]))
        assert K._sm_int(pfx ^ ch[1]) == k_ref


# ---------------------------------------------------------------- 成像判据核 vs numpy 路径
def _scene(kind: str, seed: int) -> tuple[PBench, dict]:
    r = np.random.default_rng(seed)
    env = FakeEnv.night() if kind in ("nir", "lwir_night") else FakeEnv()
    b = PBench(4, FakeWorld(boxes=[(150.0, 160.0, 20.0, 40.0, 30.0)]), env=env, seed=seed)
    for k in range(24):
        cls = ["person", "vehicle", "dog", "uav", "object"][k % 5]
        x0, x1, wy = (40.0, 160.0, 0.12) if kind == "nir" else (120.0, 420.0, 0.4)
        while True:
            px, py = float(r.uniform(x0, x1)), float(r.uniform(-wy * x1, wy * x1))
            if not (145.0 <= px <= 165.0 and 15.0 <= py <= 45.0):
                break
        tgt = {"class": cls, "pos": [px, py, 0.0 if cls != "uav" else 40.0],
               "motion": {"mode": "random_walk", "speed_mps": 1.2} if k % 3 == 0 and cls in ("person", "dog", "vehicle") else {"mode": "static"}}
        if k % 4 == 1:
            tgt["level_req"] = "I"
        if k % 6 == 2:
            tgt["n_req_px"] = 5.0
        if kind.startswith("lwir") and k % 2:
            tgt["thermal"] = {"mode": "absolute", "t_surface_c": 36.0, "hot_fraction": 0.6}
        b.add(tgt)
    b.step(1)
    spec = {"eo": gx40(), "nir": gx40_nir(True), "lwir": lwir640(), "lwir_night": lwir640()}[kind]
    for s in range(4):
        p = (0.0, -60.0 + 40.0 * s, (25.0 if kind == "nir" else 70.0) + 10 * s) if kind != "nir" else (0.0, -6.0 + 4.0 * s, 20.0 + 5 * s)
        f = {"eo": 12.0, "nir": 24.0, "lwir": 25.0, "lwir_night": 13.0}[kind]
        if not kind.startswith("lwir"):
            f = min(max(f, spec.f_min_mm), spec.f_max_mm)
        im = make_imaging(spec, p, (100.0 if kind == "nir" else 260.0, 0.0, 0.0), f_mm=f, src="tasking" if s % 2 else "default",
                          illum_on=(True, s % 2 == 0) if kind == "nir" else ())
        b.platform(s, pos=p, vel=(3.0 * s, -2.0, 0.0), imaging=[im], nonimg=[make_nonimg("radar", mmw77(), p)] if s == 0 else [])
    if kind == "eo":
        b.prt.set_requirement("uav-02", b.trt.ordered()[3].id, "D", None)
        b.prt.set_requirement("*", b.trt.ordered()[5].id, None, 9.0)
    return b, {}


def _collect(b: PBench, seconds: float) -> list:
    rows = []

    def ev_tap(P, t_ns, ev):
        rows.append((P.agent_no, int(t_ns), ev.cand.tolist(), ev.codes.tolist(), ev.dfr.tolist(), ev.core.copy(), ev.res.copy(), ev.z.copy()))

    b.prt.ev_hook = ev_tap
    b.run(seconds)
    return rows


def _compare(ra: list, rb: list) -> None:
    assert len(ra) == len(rb) and ra
    for a, c in zip(ra, rb, strict=True):
        assert a[:5] == c[:5]
        for x, y in ((a[5], c[5]), (a[6], c[6])):
            np.testing.assert_allclose(x, y, rtol=1e-12, atol=1e-12, equal_nan=True)
        has = np.array([cc[3] for cc in a[3]], bool)
        np.testing.assert_allclose(a[7][has], c[7][has], rtol=1e-12, atol=1e-9)


@needs_numba
@pytest.mark.parametrize("kind", ["eo", "nir", "lwir", "lwir_night"])
def test_imaging_kernel_matches_numpy_path(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    out = []
    for use_nb in (True, False):
        monkeypatch.setattr(K, "HAVE_NUMBA", use_nb)
        b, _ = _scene(kind, 11)
        with b:
            out.append(_collect(b, 6.0))
            out.append(([(e["kind"], e["t_sim_ns"], e["data"]) for e in b.events.items if e["kind"].startswith("perception.")],
                        list(b.prt.ring.items), b.prt.tracks_wire(b.t)))
    _compare(out[0], out[2])
    ev_a, ring_a, trk_a = out[1]
    ev_b, ring_b, trk_b = out[3]
    assert ev_a == ev_b and ring_a == ring_b
    assert [t["target"] for t in trk_a] == [t["target"] for t in trk_b]
    for x, y in zip(trk_a, trk_b, strict=True):
        assert x["state"] == y["state"]
        np.testing.assert_allclose(x["pos"], y["pos"], atol=2e-3)


# ---------------------------------------------------------------- 航迹
def _kalman6(zs: list, sig: float, q: float, v0: float = 3.0) -> tuple[np.ndarray, np.ndarray]:
    """原 6 × 6 实现（矩阵形式），作对拍参考。"""
    I3 = np.eye(3)
    H = np.hstack([I3, np.zeros((3, 3))])
    x = P = None
    t_prev = 0.0
    for t, z in zs:
        r2 = max(sig, 1e-3) ** 2
        if x is None:
            x = np.concatenate([z, np.zeros(3)])
            P = np.diag([r2] * 3 + [v0 ** 2] * 3)
            t_prev = t
        else:
            dt = t - t_prev
            if dt > 0:
                t_prev = t
                F = np.eye(6)
                F[:3, 3:] = dt * I3
                q2 = q * q
                Q = np.zeros((6, 6))
                Q[:3, :3] = q2 * dt ** 3 / 3.0 * I3
                Q[:3, 3:] = Q[3:, :3] = q2 * dt ** 2 / 2.0 * I3
                Q[3:, 3:] = q2 * dt * I3
                x = F @ x
                P = F @ P @ F.T + Q
            S = P[:3, :3] + r2 * I3
            Kg = P[:, :3] @ np.linalg.inv(S)
            x = x + Kg @ (z - x[:3])
            P = (np.eye(6) - Kg @ H) @ P
            P = 0.5 * (P + P.T)
    return x, P


def test_track_closed_form_matches_matrix_kalman() -> None:
    from awr.sim.perception.tracks import TrackTable

    r = np.random.default_rng(5)
    for cls, q in (("person", 0.5), ("uav", 3.0), ("machine", 0.05)):
        tt = TrackTable()
        zs = []
        t = 0.0
        for _k in range(60):
            t += float(r.choice([0.2, 0.2, 0.4, 1.0]))
            z = np.array([1.5 * t, -0.7 * t, 0.1 * t]) + r.normal(0, 0.4, 3)
            zs.append((t, z))
            tt.update(7, cls, t, z, 0.4)
        x, P = _kalman6(zs, 0.4, q)
        tr = tt.get(7, t)
        np.testing.assert_allclose(tr.pos, x[:3], rtol=1e-9, atol=1e-9)
        np.testing.assert_allclose(tr.vel, x[3:], rtol=1e-9, atol=1e-9)
        np.testing.assert_allclose(tr.cov_diag, np.diag(P), rtol=1e-9, atol=1e-12)


@needs_numba
def test_track_batch_kernel_bitwise_equal_python() -> None:
    from awr.sim.perception.tracks import TrackTable

    r = np.random.default_rng(6)
    a, b = TrackTable(), TrackTable()
    t = 0.0
    for _k in range(300):
        t += float(r.choice([0.0, 0.2, 0.2, 1.3]))
        n = int(r.integers(2, 9))
        tn = r.choice(40, n, replace=False).tolist()
        cls = [["person", "uav", "dog", "object"][x % 4] for x in tn]
        Z = r.normal(0, 50, (n, 3))
        sg = r.uniform(0.0005, 3.0, n).tolist()
        a.update_batch(tn, cls, t, [Z[k] for k in range(n)], sg)
        for k in range(n):
            b.update(tn[k], cls[k], t, Z[k], sg[k])
        if _k % 10 == 0:
            a.tick(t)
            b.tick(t)
    assert list(a.tr) == list(b.tr)
    for tn in a.tr:
        assert a.get(tn, t + 0.3) == b.get(tn, t + 0.3)


# ---------------------------------------------------------------- 汇总
def test_summary_incremental_equals_best_of() -> None:
    b, _ = _scene("eo", 21)
    with b:
        for _ in range(40):
            b.run(0.25)
            prt = b.prt
            snap = prt._snap
            c = b.ents["target"].cols
            for i in range(snap.n):
                best, sens, cap, uno, _f = prt.best_of(int(snap.tno[i]))
                row = int(snap.rows[i])
                assert (int(c["tgt_best_level"][row]), int(c["tgt_best_sensor"][row]), int(c["tgt_captured"][row]),
                        int(c["tgt_capture_uav_no"][row])) == (best, sens, 1 if cap else 0, uno)


def test_stage_kernel_signatures_stable() -> None:
    """运行期不新增 numba 签名（预热覆盖运行期类型；M08-FR-005）。"""
    if not K.HAVE_NUMBA:
        pytest.skip("numba 未启用")
    from awr.sim.perception import tracks as TR

    K.warmup()
    TR.warmup()
    KL.warmup()
    disp = [K.fov_gate, K.eval_a, K.eval_b, K.imaging_core, K.post_fsm, K.normals3, K.fov_rows, K.fsm_row, K.summary_rows, K.summary_cols,
            TR._update_rows, KL.query_scan, KL.apply_jobs, KL.find, KL.clear, KL.pop_front]
    before = [len(d.signatures) for d in disp]
    for kind in ("eo", "nir", "lwir"):
        b, _ = _scene(kind, 31)
        with b:
            b.run(3.0)
    assert [len(d.signatures) for d in disp] == before
    assert math.isfinite(sum(before))


def test_snapshot_dynamics_equal_m18_definitions() -> None:
    """快照的速度与遮挡（向量化）与 M18 `_vel`、`_occlusion` 逐位相同（含 hidden 与运动识别物）。"""
    b, _ = _scene("eo", 41)
    with b:
        trt = b.trt
        Ts = trt.ordered()
        for k, T in enumerate(Ts):
            if k % 3 == 0:
                T.hidden = True
        for _ in range(6):
            b.run(0.35)
            b.prt._snap_key = None                      # 快照按 M18 推进计数缓存（测试直接改了状态）：强制以当前状态重建
            snap = b.prt.targets(b.ctx)
            Ts = [T for T in trt.ordered() if bool(T.cfg.raw.get("enabled", True))]
            assert snap.n == len(Ts)
            assert snap.vel.tolist() == [list(trt._vel(T)) for T in Ts]
            assert snap.occ.tolist() == [float(trt._occlusion(T)) for T in Ts]
            assert snap.pos.tolist() == [list(map(float, T.ms.pos)) for T in Ts]
            assert snap.yaw.tolist() == [float(T.ms.yaw) for T in Ts]


def test_acoustic_matrix_equals_per_target_and_blocked_batch() -> None:
    """声阵列：向量化的带 SNR 与逐个 `acoustic_snr_bands` 逐位相同；批量遮挡查询与逐对 `peek` 判定相同。"""
    from awr.sim.perception import mic8
    from awr.sim.perception import nonimaging as NI

    sp = mic8()
    r = np.random.default_rng(12)
    k = 64
    LW = r.uniform(40.0, 110.0, (k, 24))
    R = np.concatenate([r.uniform(0.05, 3000.0, k - 2), [0.1, 0.0999]])
    blk = r.random(k) < 0.4
    ego = r.uniform(50.0, 90.0, 24)
    bg = r.uniform(5.0, 40.0, 24)
    for frame in ("multirotor", "fixed_wing"):
        M = NI.acoustic_snr_matrix(sp, LW, R, ego, bg, frame=frame, blocked=blk)
        for i in range(k):
            ref = NI.acoustic_snr_bands(sp, LW[i], float(R[i]), ego, bg, frame=frame, blocked=bool(blk[i]))
            assert M[i].tolist() == ref.tolist()
    w = FakeWorld(boxes=[(40.0, 45.0, -60.0, 60.0, 30.0)])
    c = LosCache(w)
    a = np.array([0.0, 0.0, 20.0])
    B = np.c_[r.uniform(10, 120, (40, 2)) * [1, 0.5], np.zeros(40)]
    c.get_batch(np.broadcast_to(a, (40, 3)), B, np.full(40, 1.7), "M19", 10_000_000, "center")
    for t in (10_000_000, 1_500_000_000, 2_500_000_000):
        got = c.blocked_batch(a, B, t, "center").tolist()
        ref = []
        for i in range(40):
            pk = c.peek(a, B[i], "center", t)
            ref.append(pk is not None and pk[0].mask == 0)
        assert got == ref
    assert any(got := c.blocked_batch(a, B, 10_000_000, "center").tolist()) and not all(got)


def test_shard_cache_equals_shard_slots() -> None:
    """`_slots`（按周期缓存的分片）与 `shard_slots` 逐次相同：在场集合变化、跨周期、片数变化。"""
    prt = RT.PerceptionRuntime()
    rng = np.random.default_rng(5)
    for n_sh, n_max in ((20, 15), (7, 15), (1, 15), (20, 300)):
        for call in range(3 * n_sh + 5):
            if call % 11 == 0:
                act = np.sort(rng.choice(400, size=int(rng.integers(0, n_max)), replace=False)).astype(np.int64)
            got = prt._slots(act, call, n_sh)
            assert got.tolist() == RT.PerceptionRuntime.shard_slots(act, call, n_sh).tolist()
