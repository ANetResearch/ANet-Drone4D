"""tests/sim 共用工具：独立 FleetSim 夹具（不经进程与总线）、测试用 FastGuard、SIH 黄金数据与 17 项指标（g08 §9）。

- `Rig`：隔离登记表下的 FleetSim + pipeline（ring = None，world 可选），`air(slot, z)` 把机体放到空中悬停、`step_s(s)` 推进；
- `FastGuard`：g08 `st_guard` 的测试替身（倾角、倾角误差、pos_err、油门饱和，1 s 模式切换宽限，持续计时），只记事件；
- `load_sih()`、`metrics()`、`wind_metrics()`、`rmse_vs()`：g08 `regress_g08.py` 迁移（48 ms 采样，右移 0.12 s）。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.sim.backends.base import EntitySpec, Kind
from awr.sim.fleet import actions as ACT
from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.fleet import FleetConfig, FleetSim
from awr.sim.fleet.pipeline import TICK_NS, StageCtx
from awr.sim.fleet.px4lite import quat_from_yaw
from awr.sim.fleet.stages import registry as R

ROOT = Path(__file__).resolve().parents[2]
GOLD = ROOT / "tests" / "golden" / "sih_x500_px4-1.18rc1"
SAMPLE_DT = 0.048
SHIFT = 0.12


class Events:
    """EventPublisher 替身：记录 emit。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def emit(self, kind: str, **kw) -> None:
        self.items.append((kind, kw))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.items]

    def flush(self) -> None:
        return None


class Rig:
    def __init__(self, profile_id: str = "x500", n: int = 1, *, l1_every: int = 2, kernel: str = "numba",
                 time_stretch: bool = True, stop_motion: bool = True, world=None, spacing: float = 50.0,
                 home_z: float = 0.0, aero_override: str | None = None, yaw_enu: float = math.pi / 2,
                 env=None) -> None:
        cfg = FleetConfig(l1_every=l1_every, tap_every=l1_every, kernel=kernel, time_stretch=time_stretch,
                          stop_motion=stop_motion, aero_override=aero_override, path_capacity=65536)
        self._reg_ctx = R.isolated_registry()
        self.reg = self._reg_ctx.__enter__()
        if env is not None:  # M07 env stage 的测试替身（order 020、every 5、phase 0，50 Hz）
            R.register_stage("env", 5, 0, 20, owner="M07")(env)
        self.events = Events()
        self.fleet = FleetSim(cfg, None, world, self.events, None, 0, reg=self.reg)
        self.fleet.build_pipeline(ring=None)
        self.S = self.fleet.S
        self.T = self.fleet.T
        self.ctx = StageCtx(events=self.events, profiles=self.T, paths=self.fleet.PB, cfg=cfg)
        for k in range(n):
            spec = EntitySpec(f"u{k}", Kind.UAV, profile_id, None, (k * spacing, 0.0, home_z), yaw_enu)  # 缺省朝北（SIH 同）
            self.fleet.add(spec, slot=k, agent_no=k, entity_id=f"u{k}")
            self.S.lifecycle[k] = 4
        self.n = n

    def close(self) -> None:
        self._reg_ctx.__exit__(None, None, None)

    @property
    def t(self) -> float:
        return self.ctx.t_ns * 1e-9

    def air(self, slots, z_up: float, settle_s: float = 5.0) -> None:
        """放到空中（ENU 高 z_up）并以 OFFBOARD 位置保持 settle_s（g08 `hover_at`）。"""
        S = self.S
        s = np.atleast_1d(np.asarray(slots, np.int64))
        S.p[s, 2] = -z_up
        S.p_prev[s] = S.p[s]
        S.v[s] = 0.0
        S.thrust[s] = self.T.PT[S.profile_id[s], K.P_HOVER]
        S.landed[s] = False
        S.in_air[s] = True
        S.in_contact[s] = False
        S.q[s] = quat_from_yaw(S.yaw_sp[s])
        ACT.set_offboard(S, s, S.p[s].copy(), self.t)
        self.step_s(settle_s)

    def step(self, n: int = 1) -> None:
        self.fleet.step(self.ctx, n)

    def step_s(self, s: float) -> None:
        self.step(round(s * 1e9 / TICK_NS))


# ---------------------------------------------------------------- 测试用 FastGuard（M09 的替身，g08 st_guard）
@dataclass
class GuardParams:
    tilt_kill_deg: float = 90.0
    tilt_eland_deg: float = 75.0
    tilt_err_deg: float = 20.0
    tilt_err_s: float = 0.5
    pos_err_eland: float = 3.0
    pos_err_failsafe: float = 5.0
    pos_err_s: float = 0.5
    thr_sat_frac: float = 0.95
    thr_sat_sink: float = 0.5
    thr_sat_s: float = 1.0
    grace_s: float = 1.0


@dataclass
class FastGuard:
    n: int
    gp: GuardParams = field(default_factory=GuardParams)
    events: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.te = np.full(self.n, np.nan)
        self.pe = np.full(self.n, np.nan)
        self.ts = np.full(self.n, np.nan)
        self.state = np.zeros(self.n, np.int8)
        self.max_pos_err = np.zeros(self.n)
        self.max_tilt_err = np.zeros(self.n)
        self.last_mode = None
        self.mode_t = np.zeros(self.n)

    def __call__(self, S, t: float) -> None:
        gp = self.gp
        n = self.n
        q, qs = S.q[:n], S.q_sp[:n]
        bz = np.stack([2 * (q[:, 1] * q[:, 3] + q[:, 0] * q[:, 2]), 2 * (q[:, 2] * q[:, 3] - q[:, 0] * q[:, 1]),
                       1 - 2 * (q[:, 1] ** 2 + q[:, 2] ** 2)], 1)
        bd = np.stack([2 * (qs[:, 1] * qs[:, 3] + qs[:, 0] * qs[:, 2]), 2 * (qs[:, 2] * qs[:, 3] - qs[:, 0] * qs[:, 1]),
                       1 - 2 * (qs[:, 1] ** 2 + qs[:, 2] ** 2)], 1)
        tilt = np.degrees(np.arccos(np.clip(bz[:, 2], -1, 1)))
        tilt_err = np.degrees(np.arccos(np.clip((bz * bd).sum(1), -1, 1)))
        mode = S.ctrl_mode[:n].copy()
        if self.last_mode is not None:
            self.mode_t = np.where(mode != self.last_mode, t, self.mode_t)
        self.last_mode = mode
        grace = (t - self.mode_t) < gp.grace_s
        nav = np.isin(mode, (3, 4, 5, 6, 9, 15))
        pos_err = np.where(nav, np.linalg.norm(S.pos_ref[:n] - S.p[:n], axis=1), 0.0)
        self.max_pos_err = np.maximum(self.max_pos_err, pos_err)
        self.max_tilt_err = np.maximum(self.max_tilt_err, tilt_err)

        def persist(cond, since, dur):
            since[:] = np.where(cond, np.where(np.isnan(since), t, since), np.nan)
            return cond & (t - since >= dur)

        kill = tilt > gp.tilt_kill_deg
        te = persist((tilt_err > gp.tilt_err_deg) & ~grace, self.te, gp.tilt_err_s)
        el = (tilt > gp.tilt_eland_deg) & ~grace
        pe = persist((pos_err > gp.pos_err_eland) & ~grace, self.pe, gp.pos_err_s)
        fs = (pos_err > gp.pos_err_failsafe) & ~grace
        sink = (S.pos_ref[:n, 2] - S.p[:n, 2]) < -gp.thr_sat_sink
        ts = persist((S.thrust[:n] >= gp.thr_sat_frac * 1.0) & sink, self.ts, gp.thr_sat_s)
        for name, mask, sev in (("TILT_KILL", kill, 3), ("TILT_ERR_KILL", te, 3), ("POS_ERR_FAILSAFE", fs, 2),
                                ("TILT_ELAND", el, 1), ("POS_ERR_ELAND", pe, 1), ("THROTTLE_SAT", ts, 1)):
            new = mask & (self.state < sev)
            for i in np.flatnonzero(new):
                self.events.append((round(t, 3), int(i), name))
            self.state[new] = sev


# ---------------------------------------------------------------- SIH 黄金数据与指标（g08 regress_g08.py）
def load_sih() -> dict:
    with open(GOLD / "marks.csv", encoding="utf-8") as fh:
        marks = {r[0]: float(r[1]) for r in csv.reader(fh)}
    out = {}
    for i, key in ((0, "offboard_step"), (1, "reposition"), (2, "wind")):
        lp, at = [], []
        with open(GOLD / f"inst{i}.csv", encoding="utf-8") as fh:
            for r in csv.reader(fh):
                (lp if r[0] == "lpos" else at).append([float(x) for x in r[1:]])
        lp, at = np.array(lp), np.array(at)
        t0 = marks[key]
        seg = lp[lp[:, 0] >= t0]
        tb0 = seg[:, 1].min()
        x0 = seg[0, 2:5]
        t = (seg[:, 1] - tb0) / 1000.0
        a = at[at[:, 0] >= t0]
        ta = (a[:, 1] - tb0) / 1000.0
        out[key] = {"t": t, "p": seg[:, 2:5] - x0, "v": seg[:, 5:8], "ta": ta, "rpy": a[:, 2:5]}
    return out


def quat_to_rpy(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q.T
    roll = np.arctan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = np.arcsin(np.clip(2 * (w * y - z * x), -1, 1))
    yaw = np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return np.stack([roll, pitch, yaw], 1)


def metrics(t, p, v, rpy_t, rpy, target: float = 50.0) -> dict:
    x = p[:, 0]
    vmax = float(np.abs(v[:, 0]).max())
    acc = np.abs(np.diff(v[:, 0])) / np.maximum(np.diff(t), 1e-6)
    acc98 = float(np.sort(acc)[int(len(acc) * 0.98)]) if len(acc) else 0.0
    tilt = np.degrees(np.arccos(np.clip(np.cos(rpy[:, 0]) * np.cos(rpy[:, 1]), -1, 1)))
    tilt_max = float(tilt[rpy_t <= 25].max())
    t90 = float(t[np.argmax(np.abs(x) >= 0.9 * target)])
    bad = np.nonzero(np.abs(x - target) >= 0.5)[0]
    settle = float(t[bad[-1] + 1]) if len(bad) and bad[-1] + 1 < len(t) else None
    return {"vmax": vmax, "acc98": acc98, "tilt_max": tilt_max, "t90": t90, "settle": settle,
            "overshoot": float(x.max() - target), "z_dev": float(np.abs(p[:, 2]).max())}


def rmse_vs(gold: dict, t, p, v, T: float = 20.0, shift: float = SHIFT) -> tuple[float, float]:
    grid = np.arange(0, T, 0.05)
    xs = np.interp(grid, gold["t"], gold["p"][:, 0])
    xm = np.interp(grid - shift, t, p[:, 0])
    vs = np.interp(grid, gold["t"], gold["v"][:, 0])
    vm = np.interp(grid - shift, t, v[:, 0])
    return float(np.sqrt(np.mean((xs - xm) ** 2))), float(np.sqrt(np.mean((vs - vm) ** 2)))


def wind_metrics(t, p, rpy_t, rpy) -> dict:
    sel = rpy_t >= 8
    return {"pitch": float(math.degrees(rpy[sel, 1].mean())), "roll": float(math.degrees(rpy[sel, 0].mean())),
            "drift": float(np.hypot(p[:, 0], p[:, 1]).max())}


def run_mock(profile_id: str, test: str, *, l1_every: int = 2, kernel: str = "numba", T: float = 25.0, **kw) -> dict:
    """单个剧本（兼容接口）。"""
    return run_mock_all(profile_id, (test,), l1_every=l1_every, kernel=kernel, T=T, **kw)[test]


def run_mock_all(profile_id: str, tests=("offboard_step", "reposition", "wind"), *, l1_every: int = 2,
                 kernel: str = "numba", T: float = 25.0, **kw) -> dict:
    """与 g08 `run_mock` 同一剧本：三架机（互相独立的 slot）同时在 10 m 悬停 5 s 后下发命令，48 ms 采样。"""
    n = len(tests)
    rig = Rig(profile_id, n, l1_every=l1_every, kernel=kernel, spacing=300.0, **kw)
    guard = FastGuard(n)
    try:
        rig.air(np.arange(n), 10.0, 5.0)
        S = rig.S
        t_cmd = rig.t
        for k, test in enumerate(tests):
            base = S.p[k].copy()
            if test == "offboard_step":
                ACT.set_offboard(S, [k], base + np.array([50.0, 0.0, 0.0]), t_cmd)
            elif test == "reposition":
                ACT.begin_goto(S, [k], base + np.array([50.0, 0.0, 0.0]), np.nan, None, t_cmd,
                               stop_motion=kw.get("stop_motion", True))
            elif test == "wind":
                ACT.begin_goto(S, [k], base, np.nan, None, t_cmd)
                S.set_wind_from_enu(np.array([[0.0, -8.0, 0.0]]), np.array([k]))  # SIH_WIND_N = 8：来向为北，空气向南
        base0 = S.p[:n].copy()
        rec_t, rec_p, rec_v, rec_q = [], [], [], []
        nxt = 0.0
        for _ in range(round(T * 1e9 / TICK_NS)):
            rig.step(1)
            tt = rig.t - t_cmd
            if (rig.ctx.tick % 5) == 1:
                guard(S, rig.t)
            if tt >= nxt - 1e-9:
                rec_t.append(tt)
                rec_p.append(S.p[:n].copy())
                rec_v.append(S.v[:n].copy())
                rec_q.append(S.q[:n].copy())
                nxt += SAMPLE_DT
        out = {}
        P_, V_, Q_ = np.array(rec_p), np.array(rec_v), np.array(rec_q)
        for k, test in enumerate(tests):
            p = P_[:, k] - base0[k]
            if test != "wind":
                p = P_[:, k] - P_[0, k]
            p[:, 2] = P_[:, k, 2] - base0[k, 2]
            ev = [e for e in guard.events if e[1] == k]
            g = FastGuard(1)
            g.events = ev
            out[test] = {"t": np.array(rec_t), "p": p, "v": V_[:, k], "q": Q_[:, k], "guard": g}
        return out
    finally:
        rig.close()


TOL = {  # g08 §9.3 容差
    "step": {"vmax": ("rel", 0.03), "acc98": ("rel", 0.10), "tilt_max": ("abs", 2.0), "t90": ("abs", 0.25),
             "overshoot": ("abs", 0.30), "settle": ("abs", 1.0), "rmse_x": ("max", 0.25), "rmse_v": ("max", 0.40)},
    "goto": {"vmax": ("rel", 0.05), "acc98": ("rel", 0.20), "tilt_max": ("abs", 2.0), "t90": ("abs", 0.55),
             "overshoot": ("abs", 0.30), "rmse_x": ("max", 0.50), "rmse_v": ("max", 0.25)},
    "wind": {"pitch": ("abs", 0.30), "drift": ("abs", 0.25)},
}


def evaluate(profile_id: str, *, l1_every: int = 2, kernel: str = "numba", **kw) -> tuple[dict, dict, list[str]]:
    """跑三个剧本并按 17 项容差判定；返回 (SIH 指标, Mock 指标, 不通过项)。"""
    gold = load_sih()
    g = gold["offboard_step"]
    gr = gold["reposition"]
    gw = gold["wind"]
    ref = {"step": metrics(g["t"], g["p"], g["v"], g["ta"], g["rpy"]),
           "goto": metrics(gr["t"], gr["p"], gr["v"], gr["ta"], gr["rpy"]),
           "wind": wind_metrics(gw["t"], gw["p"], gw["ta"], gw["rpy"])}
    row: dict = {}
    events: list = []
    allm = run_mock_all(profile_id, l1_every=l1_every, kernel=kernel, **kw)
    for test, key in (("offboard_step", "step"), ("reposition", "goto"), ("wind", "wind")):
        m = allm[test]
        rpy = quat_to_rpy(m["q"])
        if key == "wind":
            row[key] = wind_metrics(m["t"], m["p"], m["t"], rpy)
        else:
            mm = metrics(m["t"], m["p"], m["v"], m["t"], rpy)
            mm["rmse_x"], mm["rmse_v"] = rmse_vs(gold[test], m["t"], m["p"], m["v"])
            row[key] = mm
            events += m["guard"].events
    fails = []
    for sec, rules in TOL.items():
        for k, (kind, tol) in rules.items():
            val = row[sec].get(k)
            if kind == "max":
                ok = val <= tol
            else:
                gv = ref[sec][k]
                ok = gv is not None and val is not None and abs(val - gv) <= (tol * abs(gv) if kind == "rel" else tol)
            if not ok:
                fails.append(f"{sec}.{k}={val} (SIH {ref[sec].get(k)}, {kind} {tol})")
    if events:
        fails.append(f"guard_events={events[:3]}")
    return ref, row, fails


# ---------------------------------------------------------------- 环境 stage 替身：均匀风 + Dryden（g08 dryden_exact 迁移；M07 实现为准）
class DrydenEnv:
    """MIL-F-8785C 低空 Dryden 精确离散（标准化状态，g06 §5.5.3），平均风 ENU；作为 env stage（50 Hz，零阶保持）。"""

    def __init__(self, n: int, mean_enu=(0.0, 0.0, 0.0), sigma_ref: float = 1.45, seed: int = 7, turb: bool = True) -> None:
        self.n = n
        self.mean = np.asarray(mean_enu, np.float64)
        self.sigma_ref = sigma_ref
        self.turb = turb
        self.rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, 1])))
        self.du = np.zeros(n)
        self.dv = np.zeros((n, 2))
        self.dw = np.zeros((n, 2))
        self.init = False
        self.calls = 0

    def sample(self, h_m: np.ndarray, v_rel_speed: np.ndarray, dt: float) -> np.ndarray:
        """返回 ENU 湍流分量 (n, 3)。"""
        FT = 0.3048
        n = self.n
        hf = np.maximum(np.maximum(h_m, 0.0) / FT, 10.0)
        Lw = hf * FT
        Lu = hf / (0.177 + 0.000823 * hf) ** 1.2 * FT
        sw = 0.5295 * self.sigma_ref
        su = sw / (0.177 + 0.000823 * hf) ** 0.4
        V = np.maximum(v_rel_speed, 0.5)
        rng = self.rng
        a = np.exp(-V * dt / Lu)
        if not self.init:
            self.du = rng.standard_normal(n)
        self.du = a * self.du + np.sqrt(1 - a * a) * rng.standard_normal(n)

        def second(z, L, sig):
            r = dt * V / L
            e = np.exp(-r)
            f11, f12, f21, f22 = e * (1 + r), e * r, -e * r, e * (1 - r)
            e2 = e * e
            q11 = 1 - e2 * (1 + 2 * r + 2 * r * r)
            q12 = 2 * e2 * r * r
            q22 = 1 - e2 * (1 - 2 * r + 2 * r * r)
            l11 = np.sqrt(np.maximum(q11, 0.0))
            l21 = q12 / np.maximum(l11, 1e-12)
            l22 = np.sqrt(np.maximum(q22 - l21 * l21, 0.0))
            if not self.init:
                z = rng.standard_normal((n, 2))
            n1, n2 = rng.standard_normal(n), rng.standard_normal(n)
            z1 = f11 * z[:, 0] + f12 * z[:, 1] + l11 * n1
            z2 = f21 * z[:, 0] + f22 * z[:, 1] + l21 * n1 + l22 * n2
            z = np.stack([z1, z2], 1)
            return z, 0.5 * sig * (z[:, 0] + np.sqrt(3) * z[:, 1])

        self.dv, yv = second(self.dv, Lu, su)
        self.dw, yw = second(self.dw, Lw, sw)
        self.init = True
        m = self.mean[:2]
        nm = float(np.linalg.norm(m))
        ux = m / nm if nm > 1e-3 else np.array([1.0, 0.0])
        lat = np.array([-ux[1], ux[0]])
        uu = su * self.du
        return np.stack([uu * ux[0] + yv * lat[0], uu * ux[1] + yv * lat[1], yw], 1)

    def __call__(self, S, ctx) -> None:
        n = self.n
        self.calls += 1
        w = np.broadcast_to(self.mean, (n, 3)).copy()
        if self.turb:
            pos = S.enu.pos[:n]
            vel = S.enu.vel[:n]
            w += self.sample(pos[:, 2], np.linalg.norm(self.mean[None, :] - vel, axis=1), ctx.dt_tick * 5)
        S.set_wind_from_enu(w, np.arange(n))


# ---------------------------------------------------------------- SimCore 单步驱动夹具（假墙钟，LocalBus + LocalRing）
class CoreHarness:
    """sim-core 进程内单步驱动（不睡眠、确定性）：`cmd()` 经 CommandEngine 准入，`advance(s)` 推进仿真秒。"""

    def __init__(self, *, n: int = 1, reg=None, profile_id: str = "p600_mid360", spawn_xy=(0.0, 0.0), spacing: float = 10.0,
                 world=None, kernel: str = "numba", limits: str | None = None, seat: bool = True, ready: bool = True,
                 fleet_cfg: dict | None = None, persist_dir=None, cfg_over: dict | None = None) -> None:
        import secrets

        from awr.contracts import LAYOUT_ID
        from awr.runtime.bus import LocalBus
        from awr.runtime.principal import derive_key
        from awr.runtime.statering import LocalRing
        from awr.sim.runtime.config import SimConfig
        from awr.sim.runtime.main import SimCore

        self.W = [1_000_000_000]
        self.secret = secrets.token_bytes(32)
        self.run_id = "rm08" + secrets.token_hex(3)
        self.path = f"/m08/{self.run_id}/state.sim-core"
        self.ring, _ = LocalRing.open_or_create(self.path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0,
                                                id_count=1024)
        self.bus = LocalBus.open("sim-core", namespace=f"awr/test/{self.run_id}")
        fc = FleetConfig(kernel=kernel, path_capacity=65536, **(fleet_cfg or {}))
        kw = {"run_id": self.run_id, "n_vehicles": n, "load_world": False, "autoplay": True, "profile_id": profile_id,
              "spawn_xy": spawn_xy, "spawn_spacing_m": spacing, "fleet": fc, "limits_profile": limits} | dict(cfg_over or {})
        cfg = SimConfig(**kw)
        self.core = SimCore(cfg, self.bus, self.ring, secret=self.secret, wall_ns=lambda: self.W[0], reg=reg, world=world,
                            persist_dir=persist_dir)
        self.core.start()
        self.k_entry = derive_key(self.secret, self.run_id, "entry")
        self.n = 0
        if seat:
            self.seat()
        if ready:
            self.until(lambda: all(e.lifecycle == 4 for e in self.core.roster.by_slot.values()), 5.0)

    @property
    def S(self):
        return self.core.fleet.S

    @property
    def t(self) -> float:
        return self.core.clock.t_ns * 1e-9

    def principal(self, cid: str, pid: str = "p-op", role: str = "operator", seat: bool = True) -> dict:
        from awr.runtime.principal import Principal, sign_principal

        p = Principal(pid, role, "api", "c-1", seat)
        d = p.fields()
        d["sig"] = sign_principal(p, cid, self.k_entry)
        return d

    def advance(self, seconds: float, *, wall_step_ticks: int = 8) -> None:
        end = self.core.clock.t_ns + int(seconds * 1e9)
        guard = 0
        while self.core.clock.t_ns < end and guard < 1_000_000:
            self.W[0] += wall_step_ticks * TICK_NS
            self.core.iterate()
            guard += 1

    def until(self, pred, timeout_s: float, dt: float = 0.1) -> bool:
        t_end = self.core.clock.t_ns + int(timeout_s * 1e9)
        while self.core.clock.t_ns < t_end:
            if pred():
                return True
            self.advance(dt)
        return bool(pred())

    def cmd(self, op: str, args: dict | None = None, *, cid: str | None = None, uav="__first__", **pk) -> dict:
        self.n += 1
        cid = cid or f"c-t{self.n:05d}"
        if uav == "__first__":
            uav = self.ids()[0]
        return self.core.engine.handle({"v": 1, "cid": cid, "op": op, "uav": uav, "args": args or {},
                                        "principal": self.principal(cid, **pk), "lease": None, "t_wall_ns": 0,
                                        "epoch_seen": 1, "batch_id": None})

    def call(self, cid: str):
        ent = self.core.engine.idem.get(cid)
        return ent.call if ent is not None else None

    def calls(self, cid: str):
        ent = self.core.engine.idem.get(cid)
        return ent.calls if ent is not None else []

    def seat(self, pid: str = "p-op") -> dict:
        return self.core._lease_op({"v": 1, "cid": f"seat-{pid}", "op": "seat_claim",
                                    "principal": self.principal(f"seat-{pid}", pid)})

    def clock(self, op: str, args: dict | None = None) -> dict:
        self.n += 1
        cid = f"k-{self.n}"
        return self.core._clock_op({"v": 1, "cid": cid, "op": op, "args": args or {}, "principal": self.principal(cid)})

    def ids(self) -> list[str]:
        return [self.core.roster.by_slot[s].id for s in self.core.roster.slots_in_order()]

    def slot(self, vid: str | None = None) -> int:
        return self.core.roster.resolve(vid or self.ids()[0]).slot

    def pos(self, vid: str | None = None) -> np.ndarray:
        return self.S.enu.pos[self.slot(vid)].copy()

    def fs(self, vid: str | None = None) -> tuple[int, int]:
        sb = self.S.blocks["safety"]
        s = self.slot(vid)
        return int(sb["fs"][s]), int(sb["sub"][s])

    def takeoff(self, alt: float = 10.0, vid: str | None = None) -> None:
        cid = f"to-{self.n + 1}"
        adm = self.cmd("takeoff", {"alt_m": alt}, cid=cid, uav=vid or self.ids()[0])
        assert adm["status"] == "accepted", adm
        assert self.until(lambda: self.call(cid).final, 40.0), self.call(cid)
        assert self.call(cid).status == "succeeded", self.call(cid).effect

    def full(self, vid: str | None = None):
        """StateRing 最新一帧的 Full64 行（按 agent_no 对应机体）。"""
        from awr.contracts import LAYOUT_ID
        from awr.contracts.layouts import DRONE_STATE64
        from awr.runtime.statering import LocalRing

        f = LocalRing.attach(self.path, expect_layout_id=LAYOUT_ID).read_latest(0)
        rows = np.frombuffer(f.full, DRONE_STATE64)
        if vid is None:
            return rows
        ag = self.core.roster.resolve(vid).agent_no
        return rows[rows["agent_no"] == ag][0]

    def events(self, kind: str | None = None) -> list[dict]:
        return [e for e in getattr(self, "_events", []) if kind is None or e[0] == kind]

    def close(self) -> None:
        from awr.runtime.statering import LocalRing

        self.core.stop()
        self.bus.close()
        LocalRing.remove(self.path)


class EventTap:
    """订阅 `evt/sim-core/*`（LocalBus），收集 sim-core 事件。"""

    def __init__(self, h: CoreHarness) -> None:
        import msgpack

        from awr.contracts import bus_keys

        self.items: list[dict] = []
        for cat in ("sim", "cmd", "lease", "safety"):
            h.bus.subscribe(bus_keys.evt("sim-core", cat), lambda k, raw: self.items.extend(msgpack.unpackb(raw, raw=False)))

    def kinds(self) -> list[str]:
        return [e.get("kind") for e in self.items]

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.items if e.get("kind") == kind]
