"""决策层可见信息开关、风场读取、感知等级与按等级反推的扫描规划。"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from awr.sim.core import awareness as AW
from awr.sim.core.envwind import env_mean_wind, env_t_ns
from awr.sim.perception.levels import (
    EoSensor,
    Target,
    critical_dim,
    max_range_for_level,
    n_required,
    perception_level,
    ttpf,
)
from awr.sim.sensors.detector import pd_johnson
from awr.swarm.coverage.gsd import ScanRequest, evaluate_scan, plan_gsd_scan, sortie_endurance_s

FLAT = lambda xy: np.zeros(len(xy))  # noqa: E731
AOI = np.array([[0.0, 0.0], [600.0, 0.0], [600.0, 400.0], [0.0, 400.0]])
HOMES = np.array([[-100.0, 0.0, 0.0]])
POWER = lambda v, vz: 600.0 + 2.0 * np.asarray(v) ** 2  # noqa: E731


# ---------------------------------------------------------------- awareness
def test_awareness_defaults_and_policy() -> None:
    a = AW.DecisionAwareness()
    assert a.use_geometry and a.use_detour and a.use_wind and a.use_energy_rtl and not a.precheck_env
    b = AW.DecisionAwareness(rtl_policy="fixed_alt")
    assert not (b.use_geometry or b.use_detour or b.use_wind or b.use_energy_rtl)
    with pytest.raises(ValueError):
        AW.DecisionAwareness.from_dict({"nope": 1})
    assert AW.DecisionAwareness.from_dict({"wind": False}).use_wind is False


def test_awareness_context_restores() -> None:
    base = AW.get_awareness()
    with AW.awareness(AW.DecisionAwareness(geometry=False)):
        assert not AW.get_awareness().use_geometry
    assert AW.get_awareness() == base


# ---------------------------------------------------------------- envwind
class _Env:
    def __init__(self) -> None:
        self._s_t = 7
        self.calls: list[int] = []

    def query(self, P, t_ns, fields=0):
        self.calls.append(t_ns)
        n = len(P)
        return SimpleNamespace(wind_mean_mps=np.tile([3.0, -1.0, 0.0], (n, 1)), wind_mps=np.zeros((n, 3)))


def test_env_mean_wind_passes_time_and_reads_mean() -> None:
    e = _Env()
    w = env_mean_wind(e, np.zeros((2, 3)))
    assert w.shape == (2, 3) and w[0, 0] == 3.0 and e.calls == [7]
    env_mean_wind(e, np.zeros((1, 3)), 123)
    assert e.calls[-1] == 123 and env_t_ns(SimpleNamespace(), None) == 0


# ---------------------------------------------------------------- perception
def test_n_required_matches_johnson_ttpf() -> None:
    assert n_required("D") == pytest.approx(3.5, abs=0.15)
    assert n_required("R") == pytest.approx(14.0, abs=0.5)
    assert n_required("I") == pytest.approx(22.4, abs=0.8)
    assert ttpf(n_required("R"), "R") == pytest.approx(0.9, abs=1e-3)


def test_perception_level_limits() -> None:
    s, t = EoSensor(), Target()
    elev = np.full(3, math.pi / 2)
    r = perception_level(t, s, np.array([50.0, 50.0, 50.0]), elev, 20.0, tau=np.array([1.0, 0.1, 1.0]),
                         k_los=np.array([1.0, 1.0, 0.0]))
    assert r["level"][0] == 3 and r["limiting"][1] == 2 and r["level"][2] == 0 and r["limiting"][2] == 3
    far = perception_level(t, s, np.array([2000.0]), np.array([math.pi / 2]), 4.8)
    assert far["level"][0] == 0 and far["limiting"][0] == 1
    assert critical_dim(t, 0.0) > critical_dim(t, math.pi / 2)
    r_clear = max_range_for_level(t, s, "R", 20.0, 1e-5, math.pi / 4)
    r_fog = max_range_for_level(t, s, "R", 20.0, 3.912 / 200, math.pi / 4)
    assert r_fog < r_clear


def test_pd_johnson_monotone_in_range_and_visibility() -> None:
    cam = np.array([[0.0, 0.0, 100.0]] * 3)
    tg = np.array([[0.0, 0.0, 0.0], [0.0, 300.0, 0.0], [0.0, 0.0, 0.0]])
    p = pd_johnson(np.full(3, 2000.0), cam, tg, np.zeros(3, np.int64), np.array([1.0, 1.0, 0.2]))
    assert p[0] > p[1] and p[0] > p[2]
    pr = pd_johnson(np.full(3, 2000.0), cam, tg, np.ones(3, np.int64), np.array([1.0, 1.0, 0.2]))
    assert np.all(pr <= p + 1e-12)


# ---------------------------------------------------------------- GSD scan
def _plan(level: str, sigma: float, hf=FLAT):
    req = ScanRequest(aoi=AOI, level=level)
    ts = sortie_endurance_s(POWER, 400.0, 8.0, (5.0, 0.0))
    return req, plan_gsd_scan(req, EoSensor(), hf, FLAT, sigma_ext_per_m=sigma, t_sortie_s=ts, homes=HOMES), ts


def test_gsd_scan_lowers_height_in_fog_and_captures() -> None:
    req, clear, ts = _plan("R", 1e-4)
    _, fog, _ = _plan("R", 3.912 / 300)
    assert clear.feasible and fog.feasible
    assert fog.h_agl_m < clear.h_agl_m and fog.spacing_m < clear.spacing_m
    rng = np.random.default_rng(0)
    T = np.c_[rng.uniform(0, 600, 300), rng.uniform(0, 400, 300), np.zeros(300)]
    od = lambda sig: (lambda a, b: sig * np.linalg.norm(b - a, axis=1))  # noqa: E731
    aware = evaluate_scan(fog, req, EoSensor(), T, optical_depth=od(3.912 / 300), los=None, t_sortie_true_s=ts)
    blind = evaluate_scan(clear, req, EoSensor(), T, optical_depth=od(3.912 / 300), los=None, t_sortie_true_s=ts)
    assert aware["summary"]["capture_rate"] >= 0.95
    assert blind["summary"]["capture_rate"] < aware["summary"]["capture_rate"]


def test_gsd_scan_avoids_tall_obstacles_and_splits_fleet() -> None:
    tower = lambda p: np.where((p[:, 0] > 200) & (p[:, 0] < 260) & (p[:, 1] > 100) & (p[:, 1] < 160), 90.0, 0.0)  # noqa: E731
    _, p, _ = _plan("I", 1e-4, tower)
    assert p.feasible and p.obstacle_frac <= 0.05 and p.n_drones >= 2
    assert p.makespan_s <= p.t_max_s + 1e-6 and len(p.vehicle_chunk) == p.n_drones
    for a, b in p.seq:
        for q in (a, b):
            assert not (200 < q[0] < 260 and 100 < q[1] < 160)


def test_gsd_scan_unreachable_level() -> None:
    _, p, _ = _plan("I", 3.912 / 15)
    assert not p.feasible and p.why == "height"


def test_lawnmower_perception_hook() -> None:
    from awr.sim.mission.generators import GenContext, VehicleCtx
    from awr.sim.mission.generators.lawnmower import plan

    vs = [VehicleCtx(f"p600-0{i + 1}", np.array([-100.0, 0.0, 0.0]), np.array([-100.0, 0.0, 0.0]), 12.0, 8.0) for i in range(2)]
    lm = plan({"polygon_enu_m": AOI.tolist(), "perception": {"level": "R"}}, GenContext(None, vs))
    assert lm.stats["gsd"]["feasible"] and lm.stats["gsd"]["h_agl_m"] > 0
