"""M19 与真实 M13（挂载、云台、变焦、成像模式、引导）联调（M19-FR-011、FR-017、FR-020、FR-023；d2-WP-10-to-M19 第 1–3 条）。

隔离登记表中装配 M13（沙盒缺省挂载）、M18、M19；FleetState 与 ProfileTable（仓库 `vehicles/`，AWR-Q9：GX40、LWIR 100 mm、
mmw77、mic8）按 d1_250 的 tick 推进（M13 sensors stage 50 Hz、targets 10 Hz、perception 每 2 tick，25 片轮转，每机 5 Hz）：

- 成像状态来自 M13 `payload_view`（eo、lwir 两个成像传感器；焦距、视场、成像模式）；
- 雷达 D 级检测在 cue 打开后 ≤ 2 s 内把 GX40 云台转向识别物（M13 指向源 cue）；
- 变焦到 48 mm 后明确捕获；夜间 auto 模式切 nir 并开启照明器后帧的 kind 为 nir；cue = off（`sensor/mode{cue: off}`）时不引导。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from plib import FakeEnv

from awr.contracts.rng_streams import Stream, rng
from awr.sim.fleet.pipeline import StageCtx
from awr.sim.fleet.stages import registry as R
from awr.sim.fleet.state import FleetState
from awr.world.georef.frames import enu_to_ned, q_nedfrd_from_enuflu

TICK_NS = 4_000_000


class _Events:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.epoch = 1
        self.bus = None

    @property
    def last_seq(self) -> int:
        return len(self.items)

    def emit(self, kind, *, t_sim_ns, severity=0, uav=None, cid=None, batch_id=None, fields=None, **data):
        self.items.append({"kind": kind, "t_sim_ns": int(t_sim_ns), "severity": severity, "uav": uav, "data": {**(fields or {}), **data}})
        return len(self.items)

    def of(self, kind):
        return [e for e in self.items if e["kind"] == kind]


@pytest.fixture(scope="module")
def profiles():
    from awr.sim.fleet.profiles import ProfileTable

    return ProfileTable()


class Rig:
    def __init__(self, profiles, env) -> None:
        self.cm = R.isolated_registry()
        self.reg = self.cm.__enter__()
        from awr.sim.perception import plugin as pp
        from awr.sim.sensors import plugin as sp
        from awr.sim.targets import plugin as tp

        self.sp, self.tp, self.pp = sp, tp, pp
        self.srt = sp.install()
        self.srt.sandbox = True
        self.trt = tp.install()
        self.prt = pp.install()
        from awr.sim.core.entities import EntityTables

        self.T = profiles
        self.S = FleetState(16, blocks=dict(self.reg.blocks))
        self.ents = EntityTables(self.reg.entity_blocks, limits={"target": 30})
        self.ev = _Events()
        c = self.ctx = StageCtx()
        c.events, c.profiles, c.entities, c.env, c.world = self.ev, profiles, self.ents, env, None
        c.rng = {st.name.lower(): rng(7, int(st)) for st in Stream}
        c.streams = {}
        self.stage = next(s for s in self.reg.stages if s.name == "sensors")
        self.tick = 0

    def close(self) -> None:
        self.pp.uninstall()
        self.tp.uninstall()
        self.sp.uninstall()
        self.cm.__exit__(None, None, None)

    def spawn(self, slot, profile, pos, vid, yaw_deg=0.0) -> None:
        S = self.S
        S.active[slot] = True
        S.agent_no[slot] = slot + 1
        S.profile_id[slot] = self.T.ids.index(profile)
        S.ids[slot] = vid
        S.in_air[slot] = True
        S.p[slot] = enu_to_ned(np.asarray(pos, np.float64))
        h = math.radians(yaw_deg) / 2.0
        S.q[slot] = q_nedfrd_from_enuflu(np.array([0.0, 0.0, math.sin(h), math.cos(h)]))
        S.touch()

    def add(self, target) -> str:
        r = self.reg.commands["target/add"].fn({"op": "target/add", "args": {"target": target}}, self.tick + 1, self.ctx)
        assert r["status"] == "accepted", r
        return r["result"]["id"]

    def cmd(self, op, vid, **args):
        from awr.sim.sensors.commands import vehicle_command

        return vehicle_command(self.srt, op, vid, args, self.tick + 1, self.ctx)

    def run(self, seconds: float) -> None:
        for _ in range(round(seconds * 250)):
            t = self.tick
            c = self.ctx
            c.tick, c.t_ns, c.dt_tick = t, t * TICK_NS, 0.004
            if (t - self.stage.phase) % self.stage.every == 0:
                c.stage_dt = 0.02
                self.stage.fn(self.S, c)
            if t % 25 == 19:
                c.stage_dt = 0.1
                self.trt.stage(self.S, c)
            if t % 2 == 1:
                c.stage_dt = 0.008
                self.prt.stage(self.S, c)
            self.tick += 1


@pytest.fixture
def rig(profiles):
    r = Rig(profiles, FakeEnv())
    yield r
    r.close()


def test_frames_from_m13_payload(rig):
    rig.spawn(0, "awr_q9", (0.0, 0.0, 40.0), "q9")
    tid = rig.add({"class": "vehicle", "pos": [200.0, 0.0, 0.0], "motion": {"mode": "static"}})
    rig.run(1.5)
    f = rig.prt.frame_for("q9")
    assert f is not None and {s["sensor"] for s in f["sensors"]} == {"eo", "lwir"}
    eo = next(s for s in f["sensors"] if s["sensor"] == "eo")
    assert eo["f_mm"] == pytest.approx(4.8) and eo["kind"] == "eo"
    assert any(t["target"] == tid for t in eo["targets"])          # 缺省俯仰 −15°、广角：车在视锥内
    assert rig.prt.radar_last["q9"]["sensor"] == "radar"


def test_radar_cue_turns_gimbal_then_zoom_captures(rig):
    # Q9 的雷达挂载 rpy [0, −15, 0] 在 M13 的 FLU 约定下视轴上仰 15°（俯仰窗 0°–30°，见实现报告与 d2-WP-06-to-M21）：用空中的
    # 他方无人机（uav 类，RCS 0.01 m²，检测距离 57 m）验证引导链路
    rig.spawn(0, "awr_q9", (0.0, 0.0, 40.0), "q9")
    tid = rig.add({"class": "uav", "pos": [45.0, 0.0], "alt_agl_m": 60.0, "motion": {"mode": "static"}, "rcs_fluct": "swerling0"})
    rig.run(1.0)
    eo = next(s for s in rig.prt.frame_for("q9")["sensors"] if s["sensor"] == "eo")
    assert all(t["target"] != tid for t in eo["targets"])                # 缺省俯仰 −15°：上仰 24° 的目标不在视锥内
    assert rig.prt.radar_last["q9"]["tracks"], rig.prt.radar_last["q9"]
    rig.prt.set_cue("q9", True)
    rig.run(2.0)                                                          # ≤ 2 s 内云台转向（M13 指向源 cue）
    assert rig.prt.stats["cue"] >= 1
    st = rig.srt.imaging_specs(0)[0]
    assert st.src == "cue" and math.degrees(st.el_rad) > 10.0
    rig.run(0.5)
    eo = next(s for s in rig.prt.frame_for("q9")["sensors"] if s["sensor"] == "eo")
    assert any(t["target"] == tid for t in eo["targets"])
    assert rig.cmd("zoom", "q9", sensor="eo", f_mm=24.0)["status"] == "accepted"
    rig.run(5.0)
    caps = [e for e in rig.ev.of("perception.capture") if e["data"]["target"] == tid]
    assert caps and caps[0]["data"]["sensor"] in ("eo", "lwir")


def test_cue_off_by_sensor_mode(rig):
    rig.spawn(0, "awr_q9", (0.0, 0.0, 40.0), "q9")
    rig.add({"class": "uav", "pos": [45.0, 0.0], "alt_agl_m": 60.0, "motion": {"mode": "static"}, "rcs_fluct": "swerling0"})
    rig.prt.set_cue("q9", True)
    rig.run(0.1)
    r = rig.cmd("sensor/mode", "q9", sensor="eo", cue="off")
    assert r["status"] == "accepted", r
    rig.run(2.5)
    assert rig.srt.imaging_specs(0)[0].src != "cue"


def test_night_auto_switches_to_nir(profiles):
    r = Rig(profiles, FakeEnv.night())
    try:
        r.spawn(0, "awr_q9", (0.0, 0.0, 40.0), "q9")
        r.add({"class": "person", "pos": [100.0, 0.0, 0.0], "motion": {"mode": "static"}})
        r.srt.set_light_band("night")
        r.run(1.5)
        eo = next(s for s in r.prt.frame_for("q9")["sensors"] if s["sensor"] == "eo")
        assert eo["kind"] == "nir" and eo["mode"] == "nir" and eo["illum"]["builtin"] == "on"
    finally:
        r.close()
