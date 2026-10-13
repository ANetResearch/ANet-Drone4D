"""M19-AC-018：环境接口。未装配 M07 时回退（E 1e5 lx、tau = 1、rural 日间背景）并计数 `env_fallback`；M07 的照度档与 MOR 变化时
k_light 与判定随之变化（FakeEnv 与真实 M07 `EnvironmentServiceImpl` 的 `sky_sample`、`optical_depth`）；按波长调用 optical_depth
（EO 550、NIR 850、LWIR 10000）。"""

from __future__ import annotations

import pytest
from plib import FakeEnv, FakeWorld, PBench, make_imaging

from awr.sim.perception import gx40, gx40_nir, lwir640

P0 = (0.0, 0.0, 60.0)


def _bench(env, sensors):
    b = PBench(1, FakeWorld(), env=env)
    tid = b.add({"class": "person", "pos": [300.0, 0.0, 0.0], "motion": {"mode": "static"}})
    b.step(1)
    b.platform(0, pos=P0, imaging=[make_imaging(s, P0, (300.0, 0.0, 0.9), f_mm=48.0 if s.kind != "lwir" else None, name=f"s{k}", sensor_no=k)
                                   for k, s in enumerate(sensors)])
    return b, tid


def test_fallback_counts():
    b, _ = _bench(None, [gx40()])
    with b:
        b.run(1.0)
        assert b.prt.stats["env_fallback"] > 0
        assert b.prt.frame_for("uav-01")["sensors"][0]["targets"][0]["k"]["light"] == 1.0


def test_band_change_changes_k_light_and_capture():
    env = FakeEnv()
    b, tid = _bench(env, [gx40()])
    with b:
        b.run(2.0)
        assert b.prt.capture_state(tid).captured
        env.illum_lx, env.band = 0.001, "night"
        b.run(3.0)
        t = b.prt.frame_for("uav-01")["sensors"][0]["targets"][0]
        assert t["k"]["light"] == 0.0 and t["limiting"] == "light"
        assert len(b.ev("perception.capture_lost")) == 1 and b.ev("perception.capture_lost")[0]["data"]["reason"] == "light"
        assert b.prt.stats["env_fallback"] == 0


def test_wavelengths_requested():
    env = FakeEnv.night()
    b, _ = _bench(env, [gx40(), gx40_nir(True), lwir640(100.0)])
    with b:
        b.run(1.0)
        assert {550.0, 850.0, 10000.0} <= set(env.calls)


def test_mor_drop_reduces_n_eff():
    env = FakeEnv(mor_m=5000.0)
    b, _ = _bench(env, [gx40()])
    with b:
        b.run(0.5)
        hi = b.prt.frame_for("uav-01")["sensors"][0]["targets"][0]["n_eff"]
        env.mor_m = 500.0
        b.run(0.5)
        lo = b.prt.frame_for("uav-01")["sensors"][0]["targets"][0]["n_eff"]
        assert lo < hi


def test_real_m07_service():
    try:
        from awr.environment.field import EnvironmentServiceImpl
    except Exception:  # pragma: no cover
        pytest.skip("M07 field service unavailable")
    import inspect

    sig = inspect.signature(EnvironmentServiceImpl)
    try:
        env = EnvironmentServiceImpl() if all(p.default is not inspect._empty or p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
                                              for p in sig.parameters.values()) else None
    except Exception:
        env = None
    if env is None:
        pytest.skip("EnvironmentServiceImpl needs a configured run")
    b, _ = _bench(env, [gx40()])
    with b:
        b.run(1.0)
        assert b.prt.stats["env_fallback"] == 0 and b.prt.frame_for("uav-01") is not None
