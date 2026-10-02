"""M09-AC-014：运行期围栏——r24 fence_breach_correction 复现（不经校验的运动推出 border，加 6 m/s 顺风）：越界后 ≤ 0.12 s
进入 CORRECTING，回到内侧 ≥ 1.5 m 后 FLYING；持续推动 15 s 后 RTL；越界 12 m 立即 RTL；Velocity 5 m/s 冲向边界时停在距边界
≥ 1 m 处（`d_free_fence_m` → M08 refgen 方向限速）。"""

from __future__ import annotations

import numpy as np
from safelib import Harness

from awr.sim.fleet import actions as ACT
from awr.world.georef.frames import enu_to_ned

BORDER_X = -180.0  # tiny world：border 为 World 包围盒各边内缩 20 m


def _setup(tiny_world, **kw) -> Harness:
    h = Harness(n=1, world=tiny_world, spawn=(-165.0, -100.0), **kw)
    h.ready()
    h.gcs_age_ms = 0
    h.takeoff(12.0)
    return h


def _push_out(h: Harness, x: float) -> None:
    """不经准入直接下发 GOTO（模拟"不经校验的速度推动"）。"""
    s = h.slot()
    p = h.pos()
    ACT.begin_goto(h.S, np.array([s]), enu_to_ned(np.array([x, p[1], p[2]])), np.nan, None, h.core.clock.t_ns * 1e-9)


def test_fence_breach_correction_with_tailwind(tiny_world) -> None:
    h = _setup(tiny_world)
    try:
        s = h.slot()
        h.S.set_wind_from_enu(np.array([[-6.0, 0.0, 0.0]]), np.array([s]))  # 顺风吹向界外（M07 未装配时直接写）
        _push_out(h, -186.0)
        assert h.until(lambda: h.pos()[0] < BORDER_X, 30.0, step_s=0.004)
        t0 = h.core.clock.t_ns
        assert h.until(lambda: h.fs()[0] == "CORRECTING", 0.3, step_s=0.004)
        assert (h.core.clock.t_ns - t0) * 1e-9 <= 0.12 + 0.004
        assert h.fs() == ("CORRECTING", "GEOFENCE")
        assert "SAF.GEOFENCE.BREACH" in h.codes()
        tgt = h.S.blocks["safety"]["correct_target"][s]
        assert abs(tgt[0] - (BORDER_X + 2.0)) < 1e-6  # 最近边内法向内移 2 m
        assert h.until(lambda: h.fs() == ("FLYING", "HOVER"), 15.0)
        assert h.pos()[0] >= BORDER_X + 1.5 - 0.3
        assert "SAF.GEOFENCE.RESTORED" in h.codes()
        h.advance(3.0)
        assert h.fs() == ("FLYING", "HOVER") and h.pos()[0] > BORDER_X
    finally:
        h.close()


def test_persistent_push_timeout(tiny_world) -> None:
    """持续推动：外吹强风（25 m/s，超过 P600 抗风能力）使回拉无法完成 → 15 s 后 RTL（CORRECT_TIMEOUT）。"""
    h = _setup(tiny_world)
    try:
        s = h.slot()
        _push_out(h, -186.0)
        h.S.set_wind_from_enu(np.array([[-25.0, 0.0, 0.0]]), np.array([s]))
        assert h.until(lambda: h.fs()[0] == "CORRECTING", 30.0)
        t0 = h.core.clock.t_ns
        assert h.until(lambda: h.fs()[0] != "CORRECTING", 20.0)
        dt = (h.core.clock.t_ns - t0) * 1e-9
        assert h.fs()[0] == "RTL" and 15.0 <= dt <= 15.2, (h.state(), dt)
        assert "SAF.GEOFENCE.CORRECT_TIMEOUT" in h.codes() or "SAF.GEOFENCE.FAR_OUT" in h.codes()
        assert bool(h.S.blocks["safety"]["fs_auto"][s])
    finally:
        h.close()


def test_far_out_immediate_rtl(tiny_world) -> None:
    h = _setup(tiny_world)
    try:
        s = h.slot()
        p = h.pos()
        h.S.p[s] = enu_to_ned(np.array([BORDER_X - 12.0, p[1], p[2]]))  # 越界 12 m（瞬移）
        h.S.pos_ref[s] = h.S.p[s]
        h.S.tr_x[s] = h.S.p[s]
        h.S.touch()
        assert h.until(lambda: h.fs()[0] == "RTL", 0.2, step_s=0.004)
        assert "SAF.GEOFENCE.FAR_OUT" in h.codes()
    finally:
        h.close()


def test_velocity_stops_before_border(tiny_world) -> None:
    h = _setup(tiny_world, limits="px4_default")
    try:
        s = h.slot()
        assert h.cmd("velocity", {"frame": "world"}, cid="vel")["status"] == "accepted"
        mb = h.core.fleet.mailbox
        clk = h.core.clock
        vmax = 0.0
        for k in range(1, int(12.0 / 0.02) + 1):
            mb.put(s, np.array([-5.0, 0.0, 0.0]), 0.0, 0, k, clk.wall_mono_ns(), clk.paused_total_ns())
            h.step(5)
            vmax = max(vmax, float(np.linalg.norm(h.S.enu.vel[s])))
        assert vmax > 3.0
        assert h.pos()[0] >= BORDER_X + 1.0, h.pos()
        assert h.fs()[0] == "FLYING"
        assert np.isfinite(h.S.blocks["safety"]["d_free_fence_m"][s])
    finally:
        h.close()


def test_wind_limit_ext() -> None:
    """ext：平均风超过机型 `wind_rating_mps`（P600 13.8 m/s）时 SAF.ENV.WIND_LIMIT（边沿），回落后清除。"""
    from safelib import UnitRig

    class Env:
        w = np.array([15.0, 0.0, 0.0])

        def query(self, pos, t, fields=1):
            return np.tile(self.w, (len(pos), 1))

    r = UnitRig(n=1)
    r.set(0, 5, 0)
    r.ctx.env = Env()
    r.rt.begin(r.ctx)
    r.rt.mg.step(r.ctx)
    r.rt.mg.step(r.ctx)
    assert [e.code for e in r.rt.sink.pending].count(r.rt.code("SAF.ENV.WIND_LIMIT")) == 1
    assert int(r.sb["cond"][0]) & (1 << 18)
    Env.w = np.array([5.0, 0.0, 0.0])
    r.rt.mg.step(r.ctx)
    assert not int(r.sb["cond"][0]) & (1 << 18)


def test_fence_kernel_fast_path_equivalent(tiny_world, monkeypatch) -> None:
    """MissionGuard._fence 的融合核快速路径（kernels.fence_core，FX2-R3，ADR-070）与原 numpy 实现等价：随机机体分布在
    border、禁飞区、限制区、高楼附近与远处（含越界、越限、贴地、FLYING/CORRECTING/Velocity/起降豁免、各条件位与
    near_ok_since 的组合），逐轮比较安全块全部字段与产生的安全事件。核判定"无事件"的轮次只走快速路径。"""
    from safelib import UnitRig

    from awr.sim.safety import kernels as KN
    from awr.sim.safety.state import FS as FSE

    if not KN.HAVE_NUMBA:
        return
    rng = np.random.default_rng(21)
    fs_pool = [int(FSE.FLYING), int(FSE.FLYING), int(FSE.FLYING), int(FSE.CORRECTING), int(FSE.HOLD), int(FSE.RTL),
               int(FSE.TAKING_OFF), int(FSE.LANDING)]
    for rnd in range(212):
        n = 40 if rnd < 12 else 1  # 后 200 轮单机随机状态：核判定"无事件"时必须与原实现逐字段相同（逐机覆盖判据）
        xy = rng.uniform(-230.0, 230.0, (n, 2))
        z = rng.uniform(1.0, 160.0, n)
        fs = rng.choice(fs_pool, n)
        sub = rng.integers(0, 4, n)
        cond = rng.integers(0, 2, (n, 5))
        if rnd % 3 == 0 and rnd < 12:  # 全部在内部开阔处、无事件（快速路径）
            xy = rng.uniform(-60.0, -20.0, (n, 2))
            z = rng.uniform(40.0, 90.0, n)
            fs = rng.choice([int(FSE.FLYING), int(FSE.HOLD), int(FSE.RTL)], n)
            sub = np.where(fs == int(FSE.FLYING), 0, rng.integers(0, 3, n))
            cond[:] = 0
        ns = np.where(rng.random(n) < 0.5, np.nan, rng.uniform(0.0, 5.0, n))
        out = []
        for use_nb in (False, True):
            monkeypatch.setattr(KN, "HAVE_NUMBA", use_nb)
            r = UnitRig(n=n, world=tiny_world)
            r.ctx.tick = 1000
            r.ctx.t_ns = 4_000_000_000
            r.rt.begin(r.ctx)
            for i in range(n):
                r.set(i, int(fs[i]), int(sub[i]))
                r.S.p[i] = enu_to_ned(np.array([xy[i, 0], xy[i, 1], z[i]]))
                bits = 0
                for j, b in enumerate((0, 1, 2, 3, 4)):
                    bits |= int(cond[i, j]) << b
                r.sb["cond"][i] = bits
                r.sb["near_ok_since"][i] = ns[i]
            r.S.touch()
            r.rt.mg.step(r.ctx)
            r.rt.sink.flush(r.ctx, r.S.ids)
            out.append(({k: np.ascontiguousarray(v).tobytes() for k, v in r.sb.items()},
                        [(e["kind"], e.get("uav"), e.get("code")) for e in r.ctx.events.items]))
        monkeypatch.setattr(KN, "HAVE_NUMBA", True)
        for k in out[0][0]:
            assert out[0][0][k] == out[1][0][k], (rnd, k)
        assert out[0][1] == out[1][1], rnd
