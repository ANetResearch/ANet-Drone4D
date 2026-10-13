"""识别物运动核、敏感体内外核与视线缓存清空与原实现逐位相同（M18-FR-010 至 FR-013、M18-NFR-001；D2-FX-R2-sim 第 2 次，
`targets/kernels_motion.py`、`kernels_los.clear_used`）。

- `TargetRuntime._motion_batch`（`kernels_motion.step_all`）与 `motion.step_motion` 在随机配置（static、random_walk 含区域、path 含
  驻留与三种循环方式）、随机行为覆盖（hold、look、move 含远离与停止半径）、有无 DSM 世界下逐步比较运动状态的全部字段；
- `_inside_all_r3` 的 numba 路径与 numpy 表达式的内外矩阵与斜距逐位相同；
- `kernels_los.clear_used` 与整表 `clear` 的结果相同（随机插入、淘汰序列之后）。
"""

from __future__ import annotations

import copy
import math
from pathlib import Path

import numpy as np
import pytest
from tlib import Bench, FakeWorld

from awr.sim.targets import kernels_los as KL
from awr.sim.targets import kernels_motion as KM
from awr.sim.targets import motion as M
from awr.sim.targets.motion import MotionState, Override
from awr.sim.targets.runtime import TargetRuntime

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(not KM.HAVE_NUMBA, reason="numba 不可用（AWR_KERNEL=numpy）")


def _fields(ms: MotionState) -> tuple:
    return (tuple(np.asarray(ms.pos, float).tolist()), ms.yaw, ms.speed, ms.vz, ms.omega, ms.pause_until, ms.seg, ms.dirn, ms.dwell_until,
            bool(ms.path_done), ms.odo_m, bool(ms.blocked), ms.detour, ms.flat_c, ms.flat_r, ms.flat_z, ms.flat_fail)


def _close(x: float, y: float) -> bool:
    """numba 内核与 Python 参考逐步积分：浮点按 1e-9 相对与绝对容差比较（不同 CPU 指令集上末位不同，并随步数缓慢累积），
    离散状态在 _same 中仍逐项相等。"""
    return (math.isnan(x) and math.isnan(y)) or math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9)


def _same(a: tuple, b: tuple) -> bool:
    for x, y in zip(a, b, strict=True):
        if isinstance(x, tuple) or isinstance(y, tuple):
            if (x is None) != (y is None) or (x is not None and not all(_close(u, v) for u, v in zip(map(float, x), map(float, y), strict=True))):
                return False
        elif isinstance(x, float) or isinstance(y, float):
            if not _close(float(x), float(y)):
                return False
        elif x != y:
            return False
    return True


class _T:
    pass


class _Cfg:
    pass


def _worlds() -> list:
    out: list = [(None, (0.0, 0.0))]
    p = ROOT / "worlds" / "shenzhen"
    if p.is_dir():
        from awr.world.geometry.query import open_world_query

        w = open_world_query(p)
        if M.dsm_of(w) is not None:
            out.append((w, (-385.0, 10.0)))
    return out


def _cfg(rng: np.random.Generator, cx: float, cy: float) -> tuple[dict, np.ndarray | None]:
    cls = str(rng.choice(["person", "dog", "vehicle", "uav", "machine"]))
    mode = str(rng.choice(["static", "random_walk", "path"]))
    m: dict = {"mode": mode, "speed_mps": float(rng.uniform(0.3, 6)), "speed_max_mps": float(rng.uniform(1, 10)),
               "accel_mps2": float(rng.uniform(0.3, 3)), "turn_rate_max_rad_s": float(rng.uniform(0.2, 3)),
               "speed_sigma_mps": float(rng.uniform(0, 0.5)), "turn_tau_s": float(rng.uniform(1, 6)), "pause_prob": float(rng.uniform(0, 0.3))}
    if rng.random() < 0.2:
        m.pop("speed_max_mps")
    if mode == "path":
        npt = int(rng.integers(1, 6))
        m["path"] = [[float(cx + rng.uniform(-80, 80)), float(cy + rng.uniform(-80, 80)), None if rng.random() < 0.6 else float(rng.uniform(5, 80))]
                     for _ in range(npt)]
        m["loop"] = str(rng.choice(["once", "pingpong", "loop"]))
        if rng.random() < 0.5:
            m["dwell_s"] = [float(rng.choice([0.0, rng.uniform(0.2, 3)])) for _ in range(int(rng.integers(0, npt + 1)))]
    region = None
    if mode == "random_walk" and rng.random() < 0.8:
        h = float(rng.uniform(15, 70))
        region = np.array([[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h]], float)
        if rng.random() < 0.3:
            region = np.array([[cx - h, cy - h], [cx + h, cy - 0.5 * h], [cx + 0.2 * h, cy + h]], float)
    return {"class": cls, "motion": m, "alt_agl_m": float(rng.uniform(10, 80))}, region


def _override(rng: np.random.Generator, cx: float, cy: float) -> Override | None:
    r = rng.random()
    if r < 0.15:
        return Override("hold")
    if r < 0.3:
        return Override("look", (float(cx + rng.uniform(-50, 50)), float(cy + rng.uniform(-50, 50))) if rng.random() < 0.9 else None)
    if r < 0.5:
        pt = (float(cx + rng.uniform(-60, 60)), float(cy + rng.uniform(-60, 60))) if rng.random() < 0.9 else None
        return Override("move", pt, float(rng.uniform(0.5, 6)), away=bool(rng.random() < 0.5), stop_within_m=float(rng.uniform(0, 10)))
    return None


@pytest.mark.parametrize("seed", [1, 2])
def test_motion_kernel_matches_step_motion(seed: int) -> None:
    rng = np.random.default_rng(seed)
    worlds = _worlds()
    rt = TargetRuntime()
    steps = 0
    for _trial in range(40):
        world, (cx, cy) = worlds[int(rng.integers(0, len(worlds)))]
        cfg, region = _cfg(rng, cx, cy)
        z0 = float(world.height_dsm(np.array([[cx, cy]]))[0]) if world is not None else 0.0
        ms_a = MotionState(pos=np.array([cx + rng.uniform(-5, 5), cy + rng.uniform(-5, 5), z0 + (40.0 if cfg["class"] == "uav" else 0.0)]),
                           yaw=float(rng.uniform(-3, 3)), region=region)
        ms_a.speed = float(rng.choice([0.0, rng.uniform(0, 3)]))
        ms_b = copy.deepcopy(ms_a)
        T = _T()
        T.row = 0
        T.cfg = _Cfg()
        T.cfg.raw = cfg
        T.ms = ms_b
        if rt._kparams(T) is None:
            continue
        dt, t = 0.1, float(rng.uniform(0, 100))
        for _ in range(int(rng.integers(20, 160))):
            t += dt
            ov = _override(rng, cx, cy)
            base = bool(rng.random() < 0.85)
            nz, un = rng.normal(size=2), rng.random(2)
            M.step_motion(ms_a, cfg, dt, t, world, nz, un, ov, base_active=base)
            rt._ktab = None
            rt._motion_batch([(0, T, ov, base)], rt._kern_env(world), nz[None, :], un[None, :], dt, t)
            steps += 1
            assert _same(_fields(ms_a), _fields(ms_b)), (cfg, ov, _fields(ms_a), _fields(ms_b))
    assert steps > 1000


def test_inside_all_kernel_matches_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = np.random.default_rng(3)
    with Bench(8, FakeWorld()) as b:
        region = [[-300.0, -300.0], [300.0, -300.0], [300.0, 300.0], [-300.0, 300.0]]
        shapes = ("sphere", "hemisphere", "cylinder", "sector")
        for k in range(12):
            cls = ("person", "dog", "vehicle", "uav")[k % 4]
            bodies = [{"kind": "visual", "shape": shapes[(k + j) % 4], "radius_max_m": float(rng.uniform(20, 150)), "z_min_m": -5.0,
                       "z_max_m": float(rng.uniform(10, 80)), "half_angle_deg": float(rng.uniform(10, 80)),
                       "azimuth_offset_deg": float(rng.uniform(-180, 180))} for j in range(1 + k % 3)]
            extra = {"motion": {"mode": "random_walk", "region": region}} if cls != "vehicle" else {}
            b.add({"class": cls, "pos": [float(rng.uniform(-200, 200)), float(rng.uniform(-200, 200))], "sensitivity": bodies, **extra})
        b.step(2)
        rt = b.rt
        hits = 0
        for _ in range(30):
            for i in range(8):
                b.S.fly(i, [float(rng.uniform(-250, 250)), float(rng.uniform(-250, 250)), float(rng.uniform(1, 120))])
            _ids, P, _m, _s = rt.participants(b.S, b.ctx)
            P[0] = rt.ordered()[0].ms.pos            # 与识别物重合（扇形的 rh² < 1e-12 分支）
            Ts = rt.ordered()
            a_in, a_rows, a_r3 = rt._inside_all_r3(Ts, P)
            monkeypatch.setattr(KM, "HAVE_NUMBA", False)
            b_in, b_rows, b_r3 = rt._inside_all_r3(Ts, P)
            monkeypatch.setattr(KM, "HAVE_NUMBA", True)
            assert a_rows == b_rows
            assert a_in.tobytes() == b_in.tobytes()
            np.testing.assert_allclose(a_r3, b_r3, rtol=1e-12, atol=1e-15)  # numba 与 numpy 在不同 CPU 指令集上末位可能不同：浮点按 1e-12 相对容差比较
            IN, rows = rt._inside_all(Ts, P)               # 既有两元接口
            assert IN.tobytes() == a_in.tobytes() and rows == a_rows
            hits += int(a_in.sum())
            b.step(1)
        assert hits > 0


def test_clear_used_matches_clear() -> None:
    rng = np.random.default_rng(4)
    cap, h = 64, 128
    for _ in range(50):
        tabs = []
        for _k in range(2):
            KEY = np.zeros((cap, 6), np.int64)
            HSH = np.zeros(cap, np.uint64)
            HT = np.full(h, -1, np.int64)
            PREV = np.full(cap, -1, np.int64)
            NEXT = np.zeros(cap, np.int64)
            Mv = np.zeros(5, np.int64)
            KL.clear(HT, PREV, NEXT, Mv)
            tabs.append((KEY, HSH, HT, PREV, NEXT, Mv))
        ops = [(int(rng.integers(0, 3)), tuple(int(x) for x in rng.integers(-50, 50, 6))) for _ in range(int(rng.integers(0, 200)))]
        for KEY, HSH, HT, PREV, NEXT, Mv in tabs:
            for op, key in ops:
                if op < 2 and Mv[KL.M_SIZE] < cap and KL.find(KEY, HSH, HT, *key) < 0:
                    KL._insert(KEY, HSH, HT, PREV, NEXT, Mv, *key)
                elif op == 2:
                    KL.pop_front(KEY, HSH, HT, PREV, NEXT, Mv)
        KL.clear(*tabs[0][2:])
        KL.clear_used(*tabs[1][1:])
        for x, y in zip(tabs[0][2:], tabs[1][2:], strict=True):
            assert x.tobytes() == y.tobytes()
