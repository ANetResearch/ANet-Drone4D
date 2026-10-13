"""EnvironmentService 的 D2 部分（WP-09；D2-AC-15；ADR-090、ADR-102；AWR-17 §17.6.9；M18-M19 PRD §6.11）。

- 未配置日历与声学背景时关键帧逐字节同 D1（不带任何 D2 字段）；
- `env/calendar` 在网格点生效、jump 语义、EnvKeyframe 的 `calendar`、`sun.mode = clock`、湿度、地表、声学背景字段且通过
  `env_state.schema.json`；1 Hz 复核：太阳方向变化 ≥ 0.5° 或照度档变化时下发新版本，照度档变化另发 `env.changed`；
- `env/set{patch: {acoustic_bg, humidity_pct, surface}}`、会话配置、配置快照、checkpoint 往返；
- EnvPerceptionView：按波长光学厚度、照度与照度档、热背景（含热交叉时段）、声学带级、雨强；昼夜预报与下一次照度档变化；
- 时钟档：sandbox100（tick 10 ms）下命令生效时刻、env stage 全量求值相位（10 Hz）与 d1_250 一致。
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import numpy as np
import pytest

from awr.environment.field import EnvironmentServiceImpl
from awr.environment.keyframe import EnvOp, decode
from awr.environment.sky.solar import solar_position
from awr.environment.weather.presets import EnvError

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "packages/contracts/env/env_state.schema.json").read_text(encoding="utf-8"))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
SEC = 1_000_000_000
TICK = 4_000_000
COORD_SYN = {"anchor": {"latDeg": 30.0, "lonDeg": 120.0, "hMslM": 10.0}, "ground": {"zM": 0.0}, "trueNorth": {"yawOffsetDeg": 0.0}}
D2_KEYS = {"calendar", "humidity_pct", "surface", "acoustic_bg"}


def _svc(**kw) -> EnvironmentServiceImpl:
    return EnvironmentServiceImpl(world_id="synthcity", world_seed=7, load_assets=False, coordinate=COORD_SYN, **kw)


def _ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


def _valid(w: dict) -> None:
    errs = sorted(VALIDATOR.iter_errors(json.loads(json.dumps(w))), key=str)
    assert not errs, errs[:3]


def _run(env: EnvironmentServiceImpl, t0_s: float, t1_s: float, step_s: float) -> list:
    frames = []
    t = t0_s
    while t <= t1_s + 1e-9:
        frames += env.on_env_tick_all(round(t * SEC) // env.tick_ns)
        t += step_s
    return frames


def test_d1_frames_unchanged_without_d2():
    env = _svc()
    env.on_env_tick_all(0)
    w = env.keyframe().to_wire()
    assert not (D2_KEYS & set(w)) and w["config"]["sun"] == {"azimuth_deg": 150, "elevation_deg": 50}
    frames = _run(env, 0, 30, 1.0)
    assert frames == []  # 无 D2 时 1 Hz 复核不发版本
    # 感知视图在固定太阳下也可用（日间、乡村背景、热交叉 1）
    smp = env.sky_sample(10 * SEC)
    assert smp.mode == "fixed" and smp.band == "day" and smp.k_cross == 1.0 and smp.l_bg_dba == 40.0
    assert smp.illum_lx == pytest.approx(1.2e5 * math.sin(math.radians(50)) * (1 - 0.7 * 0.05), rel=1e-9)


def test_calendar_command_jump_and_keyframe():
    env = _svc()
    _run(env, 0, 10, 1.0)
    op = env.op_from_msg("env/calendar", {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"}, by="p-op")
    r = env.apply(op, (10 * SEC) // TICK + 1)
    assert r.t_apply_ns == 10 * SEC + 20_000_000
    frames = env.on_env_tick_all(r.t_apply_ns // TICK)
    assert len(frames) == 1
    kf = frames[0]
    w = kf.to_wire()
    _valid(w)
    assert w["calendar"] == {"start_utc": "2026-10-05T01:59:49.980Z", "tz": "Asia/Shanghai",
                             "t_cal_unix_ns": str(_ms(2026, 10, 5, 2) * 1_000_000)}
    sun = w["config"]["sun"]
    p = solar_position(_ms(2026, 10, 5, 2), 30.0, 120.0)  # 10:00 北京时间
    assert sun["mode"] == "clock" and sun["band"] == "day"
    assert abs(sun["elevation_deg"] - p.el_deg) < 0.01 and abs(sun["azimuth_deg"] - p.az_deg) < 0.01
    assert w["acoustic_bg"]["profile"] == "rural" and w["acoustic_bg"]["l_bg_dba"] == 40.0
    assert w["humidity_pct"] == 50.0 and set(w["surface"]) == {"t_c", "emissivity", "k_cross"}
    assert ("env.changed", {"version": kf.version, "by": "p-op", "reason": "calendar"}) in env.outbox
    # 心跳：t_cal 随 t_ns 更新；解码往返保留日历
    hb = env.keyframe()
    w2 = hb.to_wire()
    assert int(w2["calendar"]["t_cal_unix_ns"]) - int(w["calendar"]["t_cal_unix_ns"]) == w2["t_ns"] - w["t_ns"]
    back = decode(hb.encode())
    assert back.cal == hb.cal and back.d2 == hb.d2 and back.to_wire() == w2
    assert len(hb.encode()) <= 2048
    # 非法输入在 apply 前拒绝（110）
    for bad in ({"start_utc": "2026-13-01T00:00:00Z", "tz": "Asia/Shanghai"}, {"start_utc": "2026-10-05T00:00:00Z", "tz": "Nowhere/X"}):
        with pytest.raises(EnvError) as ei:
            env.op_from_msg("env/calendar", bad)
        assert ei.value.code == 110
    with pytest.raises(EnvError) as ei:
        env.op_from_msg("env/calendar", {"start_utc": 5, "tz": "UTC"})
    assert ei.value.code == 300


def test_dusk_versions_band_changes_and_sun_threshold():
    """日落前后 3 h（北京时间 16:30–19:30，synthcity 10 月 5 日）：太阳每变化 ≥ 0.5° 或照度档变化发新版本。"""
    env = _svc()
    env.configure_session({"calendar": {"start_utc": "2026-10-05T08:30:00Z", "tz": "Asia/Shanghai"}})
    env.on_env_tick_all(0)
    v0 = env.kf.version
    env.outbox.clear()
    frames = _run(env, 0, 3 * 3600, 5.0)
    assert len(frames) > 40
    bands = [f.config["sun"]["band"] for f in frames]
    assert bands[0] == "day" and bands[-1] == "night" and "low" in bands
    # 照度档变化的版本在太阳越过 ±6° 后 ≤ 5 s（步长）内发出
    for f, prev in zip(frames[1:], frames, strict=False):
        if f.config["sun"]["band"] != prev.config["sun"]["band"]:
            t_cal = int(f.to_wire()["calendar"]["t_cal_unix_ns"]) / 1e6
            el_now = solar_position(t_cal, 30.0, 120.0).el_deg
            el_before = solar_position(t_cal - 6000.0, 30.0, 120.0).el_deg
            thr = 6.0 if f.config["sun"]["band"] == "low" else -6.0
            assert el_now < thr <= el_before + 1e-9
    # 相邻两个版本的太阳方向差 ≤ 0.5° + 一个步长内的运动（约 0.02°）
    for f, prev in zip(frames[1:], frames, strict=False):
        a, b = f.config["sun"], prev.config["sun"]
        assert abs(a["elevation_deg"] - b["elevation_deg"]) <= 0.6
    changed = [d for k, d in env.outbox if k == "env.changed" and d.get("reason") == "illum_band"]
    assert [d["band"] for d in changed] == ["low", "night"]
    assert frames[0].version > v0 and all(f.to_wire().get("calendar") for f in frames)
    for f in frames[:: max(1, len(frames) // 8)]:
        _valid(f.to_wire())
    # k_cross：日落前 1 h 内为 0.5
    kc = [(f.to_wire()["config"]["sun"]["elevation_deg"], f.d2["surface"]["k_cross"]) for f in frames]
    assert any(k == 0.5 for _el, k in kc) and kc[-1][1] == 1.0


def test_env_set_d2_patch_and_snapshot():
    env = _svc()
    env.on_env_tick_all(0)
    op = env.op_from_msg("env/set", {"patch": {"acoustic_bg": "urban"}})
    assert op.kind == "d2"
    env.apply(op, 10)
    frames = env.on_env_tick_all(10)
    w = frames[-1].to_wire()
    _valid(w)
    assert w["acoustic_bg"] == {"profile": "urban", "l_bg_dba": 60.0} and w["config"]["sun"]["mode"] == "fixed"
    op2 = env.op_from_msg("env/set", {"patch": {"humidity_pct": 85, "wind": {"speed_ref_mps": 4.0}}, "duration_s": 0})
    assert op2.kind == "set" and op2.patch["atmosphere"]["rh"] == 0.85
    env.apply(op2, 20)
    frames = env.on_env_tick_all(20)
    assert frames[-1].to_wire()["humidity_pct"] == 85.0
    for bad in ({"acoustic_bg": "forest"}, {"humidity_pct": 120}, {"surface": {"class": "lava"}}):
        with pytest.raises(EnvError) as ei:
            env.op_from_msg("env/set", {"patch": bad})
        assert ei.value.code == 110
    snap = env.session_snapshot()
    assert snap["acoustic_bg"] == "urban" and "calendar" not in snap
    assert snap["env"]["overrides"]["atmosphere"]["rh"] == 0.85 and snap["env"]["overrides"]["wind"]["speed_ref_mps"] == 4.0


def test_session_config_and_checkpoint_roundtrip():
    env = _svc()
    env.configure_session({"calendar": {"start_utc": "2026-10-05T14:00:00Z", "tz": "Asia/Shanghai"}, "acoustic_bg": "suburban",
                           "surface_class": "vegetation", "env": {"preset": "haze"}})
    kf = env.keyframe()
    assert kf.version == 1 and kf.to_preset == "haze"
    w = kf.to_wire()
    _valid(w)
    assert w["config"]["sun"]["band"] == "night" and w["acoustic_bg"]["l_bg_dba"] == 40.0 and w["surface"]["emissivity"] == 0.97
    _run(env, 0, 120, 1.0)
    snap = env.session_snapshot(60 * SEC)
    assert snap["calendar"] == {"start_utc": "2026-10-05T14:01:00Z", "tz": "Asia/Shanghai"} and snap["env"]["preset"] == "haze"
    blob = env.checkpoint()
    env2 = _svc()
    env2.restore(blob)
    assert env2.sky.calendar == env.sky.calendar and env2.sky.acoustic == "suburban" and env2.sky.surface == "vegetation"
    assert env2.keyframe().to_wire()["calendar"] == env.keyframe().to_wire()["calendar"]
    f1 = _run(env, 121, 400, 1.0)
    f2 = _run(env2, 121, 400, 1.0)
    strip = lambda f: {k: v for k, v in f.to_wire().items() if k != "epoch"}  # noqa: E731  恢复后生产者纪元 + 1（K08）
    assert f1 and [strip(f) for f in f1] == [strip(f) for f in f2]


def test_perception_view():
    env = _svc()
    env.configure_session({"calendar": {"start_utc": "2026-07-01T05:00:00Z", "tz": "Asia/Shanghai"}})  # 13:00 北京时间
    env.apply_now(EnvOp("set", patch={"atmosphere": {"rh": 0.8}}, duration_s=0.0, mode="step"))
    env.on_env_tick_all(5)
    t = 10 * SEC
    pos = np.array([[0.0, 0.0, 50.0], [100.0, 20.0, 80.0]])
    E, band = env.illuminance(pos, t)
    assert E.shape == (2,) and np.all(E > 5e4) and np.all(band == 0)
    tb = env.thermal_bg(pos[:, :2], t)
    assert tb.t_b_c.shape == (2,) and np.all(tb.t_b_c > tb.t_sky_c) and np.all(tb.k_cross == 1.0) and np.all(tb.eps_b == 0.93)
    ab = env.acoustic_bg_bands(pos[:, :2], t)
    assert ab.shape == (2, 24) and abs(10 * math.log10(np.sum(10 ** (ab[0] / 10))) - 40.0) < 1e-9
    assert env.rain_rate_mmph(t) == 0.0
    od550 = env.optical_depth(pos[:1], pos[1:], t)
    od905 = env.optical_depth(pos[:1], pos[1:], t, wavelength_nm=905.0)
    odlw = env.optical_depth(pos[:1], pos[1:], t, wavelength_nm=10000.0)
    assert od905[0] < od550[0] and odlw[0] > 0
    L = float(np.linalg.norm(pos[1] - pos[0]))
    assert odlw[0] > 0.15 / 1000 * L  # rh 0.8、日均 14.9 °C：rho_v ≈ 10.2 g/m³，水汽项约 0.15 /km
    smp = env.sky_sample(t)
    assert smp.rho_v_gm3 > 10 and smp.humidity_pct == 80.0 and smp.band == "day"
    assert env.sigma_lambda_ground(t, 550.0) == pytest.approx(math.log(20) / smp.mor_m, rel=1e-9)
    # 同一仿真秒内同一值（1 s 量化，确定）
    assert env.sky_sample(t + 400_000_000) is env.sky_sample(t)
    # 热交叉：日出后 1 h 内为 0.5（北京时间 6 月日出约 05:00）
    env2 = _svc()
    env2.configure_session({"calendar": {"start_utc": "2026-07-01T21:30:00Z", "tz": "Asia/Shanghai"}})  # 05:30
    assert env2.sky_sample(0).k_cross == 0.5 and env2.sky_sample(3 * 3600 * SEC).k_cross == 1.0


def test_forecast_and_next_band_change():
    env = _svc()
    env.configure_session({"calendar": {"start_utc": "2026-10-05T08:00:00Z", "tz": "Asia/Shanghai"}})  # 16:00
    f = env.forecast(0, 24 * 3600, 3600)
    assert f["t_ns"].shape == (25,) and set(np.unique(f["band"])) == {0, 1, 2}
    nb = env.next_band_change(0)
    assert nb is not None
    t_change, b0, b1 = nb
    assert (b0, b1) == ("day", "low")
    el = solar_position(_ms(2026, 10, 5, 8) + t_change / 1e6, 30.0, 120.0).el_deg
    assert -0.01 < el - 6.0 <= 0.0
    assert _svc().next_band_change(0) is None


def test_clock_profile_sandbox100():
    """ADR-090：tick 长度经时钟档读取；sandbox100（10 ms）下命令生效于同一 20 ms 网格，env stage 全量求值仍为 10 Hz。"""
    import awr.environment.stage as st

    env = _svc()
    env.tick_ns = 10_000_000
    op = env.op_from_msg("env/preset", {"name": "rain", "duration_s": 0})
    r = env.apply(op, 101)  # t_cmd = 1.01 s -> 网格 1.02 s
    assert r.t_apply_ns == 1_020_000_000
    assert st.tick_ns_of(SimpleNamespace(dt_tick=0.01)) == 10_000_000
    assert st.tick_ns_of(SimpleNamespace(dt_tick=0.004)) == 4_000_000
    assert st.tick_ns_of(SimpleNamespace(clock=SimpleNamespace(tick_ns=10_000_000))) == 10_000_000
    assert st.tick_ns_of(SimpleNamespace()) == 4_000_000
    # 全量求值相位：d1_250（every 5）与 sandbox100（every 2）在同一组 20 ms 网格点上
    full_d1 = [t for t in range(0, 500, 5) if ((t * 4_000_000) // st.H_GRID_NS) % 10 in st.FULL_GRID]
    full_sb = [t for t in range(0, 200, 2) if ((t * 10_000_000) // st.H_GRID_NS) % 10 in st.FULL_GRID]
    assert [t % 50 for t in full_d1] == [5, 35] * 10  # 与 D1 的 tick % 50 ∈ {5, 35} 逐 tick 相同
    assert [t * 4 for t in full_d1] == [t * 10 for t in full_sb]  # 同一组仿真时刻（ms），10 Hz


def test_handle_command_calendar_registered(monkeypatch):
    import awr.environment.stage as st
    from awr.sim.fleet.stages import registry as R

    assert "env/calendar" in R._REG.commands or not hasattr(R, "register_command_handler")
    env = _svc()
    st.reset_plugin_state()
    monkeypatch.setitem(st._st, "env", env)
    ctx = SimpleNamespace(env=env, dt_tick=0.004)
    rep = st.handle_command({"op": "env/calendar", "args": {"start_utc": "2026-10-05T12:00:00Z", "tz": "UTC"},
                             "principal": {"principal_id": "p-x"}}, 3, ctx)
    assert rep["status"] == "accepted" and rep["result"]["t_apply_ns"] == 20_000_000
    rep = st.handle_command({"op": "env/calendar", "args": {"start_utc": "x", "tz": "UTC"}}, 3, ctx)
    assert rep["status"] == "rejected" and rep["code"] == 110
    st.reset_plugin_state()


def test_session_config_after_first_frame_makes_a_new_version():
    """初始帧已对外给出（心跳或首个 env.keyframe）后再装入会话配置：在下一个网格点以新版本生效，客户端不会错过 D2 字段。"""
    env = _svc()
    v1 = env.keyframe().version
    env.configure_session({"calendar": {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"}, "acoustic_bg": "urban"})
    assert env.kf.version == v1 and "calendar" not in env.kf.to_wire()
    frames = env.on_env_tick_all(5)
    assert frames and frames[-1].version > v1
    w = frames[-1].to_wire()
    _valid(w)
    assert w["calendar"]["start_utc"] == "2026-10-05T02:00:00Z" and w["acoustic_bg"]["profile"] == "urban"
