"""D3-AC-01 零影响（封板任务 K01）：决策可见性开关的缺省值复现 D2（d2@928c57b）的决策层取风口径。

论文合并（705a1be/0958627）把返航逆风（`SafetyRuntime.wind_head`）、`P600EnergyModel` 的 `estimate`/`path_wh` 的风改为按
仿真时刻读取 M07 平均风，D2 仿真因此不再逐字节一致（relay case b 1 h seed 11 首个分歧为 p600-03 SAF.BAT.ENERGY_RTL）。
修复后该行为由 `DecisionAwareness.env_wind` 显式开启（论文实验经 harness 的 `paper_awareness` 开启），缺省沿用 D2 的
查询口径 `env.query(P, None, fields=1)`：真实 M07 服务在 `int(None)` 处抛 TypeError，决策层按无风计；测试替身照常返回。
端到端证据为回归门 `zero-impact` 步骤（relay_batch 摘要与 baseline.json 的 D2 基线相同）。
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from awr.sim.core import awareness as AW
from awr.sim.core.envwind import d2_query_wind, decision_wind
from awr.sim.safety.battery import P600EnergyModel
from awr.sim.safety.service import SafetyRuntime

PROFILE = "p600_mid360"


class _RealLikeEnv:
    """与 M07 `EnvironmentServiceImpl.query` 同一签名约束：仿真时刻为必填整数（None 时抛 TypeError），结果取 `wind_mean_mps`。"""

    def __init__(self, wind=(4.0, -2.0, 0.0)) -> None:
        self.wind = np.asarray(wind, np.float64)
        self._s_t = 5_000_000_000
        self.calls: list[tuple[int, int]] = []

    def query(self, P, t_sim_ns, fields=0):
        t = int(t_sim_ns)
        self.calls.append((t, int(fields)))
        n = len(np.asarray(P).reshape(-1, 3))
        return SimpleNamespace(wind_mean_mps=np.tile(self.wind, (n, 1)), wind_mps=np.tile(self.wind * 2.0, (n, 1)))


class _D2Double:
    """D2 测试替身口径：接受 t_sim_ns = None，返回带 `wind_enu` 的结果。"""

    def __init__(self, wind=(3.0, -1.0, 0.5)) -> None:
        self.wind = np.asarray(wind, np.float64)
        self.calls: list[tuple[object, int]] = []

    def query(self, P, t_sim_ns, fields=0):
        self.calls.append((t_sim_ns, int(fields)))
        n = len(np.asarray(P).reshape(-1, 3))
        return SimpleNamespace(wind_enu=np.tile(self.wind, (n, 1)))


@pytest.fixture(autouse=True)
def _default_awareness():
    with AW.awareness(None):
        yield


def _samples(n: int = 40) -> np.ndarray:
    """k×7 轨迹（t、ENU 位置、ENU 速度）：爬升、平飞转弯、下降，含一个重复时刻（dt = 0 的段跳过）。"""
    t = np.arange(n, dtype=np.float64)
    t[7] = t[6]
    ang = 0.05 * t
    v = np.c_[8.0 * np.cos(ang), 8.0 * np.sin(ang), np.where(t < 10, 2.0, np.where(t > 30, -1.5, 0.0))]
    p = np.cumsum(v, axis=0)
    return np.c_[t, p, v]


def _d2_path_wh(model: P600EnergyModel, samples: np.ndarray, wind_of) -> float:
    """d2@928c57b `P600EnergyModel.path_wh` 有 env 分支的原文（逐段标量 `_power`，按段序 +=）。"""
    from awr.sim.core.estimate import vehicle_profile
    from awr.sim.fleet.profiles import ProfileTable
    from awr.sim.safety.battery import _bat_param

    prof = vehicle_profile(ProfileTable().get(PROFILE))
    ph = _bat_param(prof.extras.get("battery"), "p_hover_w")
    X = np.asarray(samples, np.float64).reshape(-1, 7)
    wh = 0.0
    for k in range(len(X) - 1):
        dt = float(X[k + 1, 0] - X[k, 0])
        if dt <= 0:
            continue
        vel = 0.5 * (X[k, 4:7] + X[k + 1, 4:7])
        w = wind_of(0.5 * (X[k, 1:4] + X[k + 1, 1:4]))
        v_air = float(np.linalg.norm(vel[:2] - w[:2]))
        wh += model._power(prof, ph, v_air, float(vel[2])) * dt / 3600.0
    return wh


# ---------------------------------------------------------------- 缺省开关
def test_defaults_are_d2_behaviour() -> None:
    a = AW.DecisionAwareness()
    assert not a.env_wind and not a.shared_energy and not a.precheck_env and not a.los and a.rtl_policy == "awr"
    assert AW.DecisionAwareness.from_dict({"env_wind": True}).env_wind
    assert AW.get_awareness() == a


# ---------------------------------------------------------------- 取风口径
def test_decision_wind_default_is_d2_query() -> None:
    e = _D2Double()
    w = decision_wind(e, np.zeros((2, 3)), 123)
    assert e.calls == [(None, 1)] and w.shape == (2, 3) and np.array_equal(w[0], e.wind)
    assert np.array_equal(d2_query_wind(e, np.zeros((1, 3))), e.wind[None])
    with pytest.raises(TypeError):
        decision_wind(_RealLikeEnv(), np.zeros((1, 3)), 123)


def test_decision_wind_env_wind_reads_mean_at_sim_time() -> None:
    e = _RealLikeEnv()
    with AW.awareness({"env_wind": True}):
        w = decision_wind(e, np.zeros((2, 3)), 123)
        decision_wind(e, np.zeros((1, 3)))
    assert e.calls == [(123, 3), (e._s_t, 3)]
    assert np.array_equal(w, np.tile(e.wind, (2, 1)))


# ---------------------------------------------------------------- M09 返航逆风
def _safety_stub(env) -> SimpleNamespace:
    pos = np.array([[100.0, 0.0, 50.0], [0.0, 0.0, 40.0], [-30.0, 40.0, 60.0]])
    home = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    return SimpleNamespace(ctx=SimpleNamespace(env=env), S=SimpleNamespace(enu=SimpleNamespace(pos=pos, home=home)),
                           t_ns=7_000_000_000)


def test_wind_head_default_is_zero_on_real_env() -> None:
    """D2：真实 M07 服务上逆风恒为 0（查询失败）；`env_wind` 开启后按 t_ns 读取平均风。"""
    env = _RealLikeEnv(wind=(5.0, 0.0, 0.0))      # 风向东 5 m/s：自 x = 100 向西返航为逆风 5 m/s
    rt = _safety_stub(env)
    slots, z = np.arange(3), np.array([60.0, 60.0, 60.0])
    assert np.array_equal(SafetyRuntime.wind_head(rt, slots, z), np.zeros(3)) and env.calls == []
    with AW.awareness({"env_wind": True}):
        h = SafetyRuntime.wind_head(rt, slots, z)
    assert env.calls == [(7_000_000_000, 3)]
    assert h[0] == pytest.approx(5.0) and h[1] == 0.0 and h[2] == 0.0   # 第 2 架在 home 正上方；第 3 架为顺风


def test_wind_head_default_keeps_d2_double_values() -> None:
    env = _D2Double(wind=(5.0, 0.0, 0.0))
    h = SafetyRuntime.wind_head(_safety_stub(env), np.arange(3), np.array([60.0, 60.0, 60.0]))
    assert env.calls == [(None, 1)] and h[0] == pytest.approx(5.0)


# ---------------------------------------------------------------- M09 能量模型
@pytest.mark.parametrize("env_cls", [_RealLikeEnv, _D2Double])
def test_path_wh_default_is_d2_loop_bitwise(env_cls) -> None:
    m = P600EnergyModel(None)
    env = env_cls()
    X = _samples()
    got = m.path_wh(PROFILE, X, env)
    ref = _d2_path_wh(m, X, lambda pos: m._wind(env, pos))
    assert got == ref and math.isfinite(got) and got > 0.0
    if env_cls is _RealLikeEnv:   # D2：查询失败按无风计
        assert ref == _d2_path_wh(m, X, lambda pos: np.zeros(3))


def test_path_wh_env_wind_uses_mean_wind() -> None:
    m = P600EnergyModel(None)
    X = _samples()
    calm = m.path_wh(PROFILE, X, _RealLikeEnv(wind=(0.0, 0.0, 0.0)))
    with AW.awareness({"env_wind": True}):
        windy = m.path_wh(PROFILE, X, _RealLikeEnv(wind=(9.0, 0.0, 0.0)))
        still = m.path_wh(PROFILE, X, _RealLikeEnv(wind=(0.0, 0.0, 0.0)))
    assert windy != calm and still == pytest.approx(calm, rel=1e-12)


def test_estimate_wind_default_matches_no_env_on_real_env() -> None:
    """`estimate` 的逐段风：缺省口径下真实 M07 服务按无风计（与 env = None 逐位相同）；开启后计入平均风。"""
    from awr.sim.core.estimate import vehicle_profile
    from awr.sim.core.interfaces import EstimatePath
    from awr.sim.fleet.profiles import ProfileTable

    m = P600EnergyModel(None)
    prof = vehicle_profile(ProfileTable().get(PROFILE))
    path = EstimatePath(points_enu_m=[(0.0, 0.0, 50.0), (400.0, 0.0, 50.0), (400.0, 300.0, 60.0)], speeds_mps=[8.0, 8.0],
                        dwell_s=30.0)
    env = _RealLikeEnv(wind=(-8.0, 0.0, 0.0))
    base = m.estimate(prof, 0.9, path, None)
    assert m.estimate(prof, 0.9, path, env) == base and env.calls == []
    with AW.awareness({"env_wind": True}):
        aware = m.estimate(prof, 0.9, path, env)
    assert aware.energy_wh > base.energy_wh and env.calls


# ---------------------------------------------------------------- 论文实验显式开启
def test_paper_harness_enables_env_wind_explicitly() -> None:
    """research/ccf-tpci 的每次运行经 `paper_awareness` 显式开启 `env_wind`，策略开关覆盖其余项（论文数值口径不变）。"""
    import importlib.util
    from pathlib import Path

    f = Path(__file__).resolve().parents[2] / "research" / "ccf-tpci" / "experiments" / "harness.py"
    spec = importlib.util.spec_from_file_location("_ccf_tpci_harness", f)
    assert spec is not None and spec.loader is not None
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    assert h.PAPER_AWARENESS == {"env_wind": True}
    a = h.paper_awareness(precheck_env=True, shared_energy=True, wind=False)
    assert a.env_wind and a.precheck_env and a.shared_energy and not a.use_wind
    assert h.paper_awareness(env_wind=False).env_wind is False
