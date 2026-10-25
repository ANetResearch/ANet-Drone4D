"""Fast 档 `physics = fast`（M24 §6.4；FR-005、FR-009、FR-017；D3-AC-35、D3-AC-58、D3-AC-13、D3-AC-14 的 Fast 档前提）。

1. 时钟档：`{d1_250, 250, 250, 250}` 即 `l1_every = 1`（250 Hz、每 tick 25 步）；`l1_hz = 125` 只用于 8 ms 整数倍的 tick 且不含
   imu、lidar、bag；sandbox100 为 100 Hz；输出含 imu、lidar 或 bag 时 `physics_hz ≥ 200`，违反 637。
2. `pose_history` stage：逐物理步、4 ms 网格、读取接触处理后的状态；静止时比力恒为 (0, 0, +g)，比力与速度差分逐步自洽。
3. 指令与状态：takeoff、goto、velocity、land、kill 经 FleetSim 运动模式执行；VehicleStatus 统一派生；接触事件。
4. `mesh_contact` stage：侧向撞墙由网格判定（detector = mesh，附三角形号），降落到楼顶交给 D2 contact（不误判坠毁），净空门控。
5. 确定性（一级 b）：同一指令序列两次运行 PoseHistory 逐字节一致。
6. Env：`physics = "fast"` 与运动学档运行同一 Agent 代码（L1、L2）；同一种子两次运行观测流与动作流逐字节一致；清单记录档位、
   时钟档与运行哈希；物理频率不足 200 Hz 的 L2 运行在 reset 时被拒（637）。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from awr.phys import PhysError
from awr.phys.adapter import ClockProfile, OrbitSpec, TierCommand, VehicleStatus
from awr.phys.fast import (
    BODY_Z_M,
    MIN_IMU_PHYSICS_HZ,
    FastTier,
    check_physics_hz,
    clock_of,
    fleet_config,
    resolve_profile,
)
from awr.phys.pose_history import G_W

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "world"))
sys.path.insert(0, str(HERE.parents[0] / "events"))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "sdk" / "anet4d" / "src"))

G = 9.80665


def _quat_rot(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """R(q) v，q 为 WORLD←FLU xyzw。"""
    u, w = q[:, 0:3], q[:, 3:4]
    t = 2.0 * np.cross(u, v)
    return v + w * t + np.cross(u, t)


def _run(tier: FastTier, script: dict[int, list[TierCommand]], n: int) -> list:
    tier.open()
    out = []
    for k in range(n):
        if k in script:
            assert all(r.ok for r in tier.submit(script[k]))
        out.append(tier.advance(k))
    return out


# ---------------------------------------------------------------------------------------------------------------- 时钟档
def test_clock_profiles_and_physics_hz_guard() -> None:
    t = FastTier(["a"], {"a": ((0.0, 0.0, 0.0), 0.0)})
    assert (t.l1_every, t.n_steps, t.step_ns, t.cp.physics_hz) == (1, 25, 4_000_000, 250)    # d1_250 缺省：l1_every = 1
    assert t.caps()["pose_history_hz"] == 250 and t.caps()["ranked_allowed"] is False and t.caps()["determinism"] == "1b"
    names = [(s.name, s.every, s.order) for s in t.f.pipeline.stages]
    assert ("mesh_contact", 5, 195) in names and ("pose_history", 1, 198) in names
    writers = [s.order for s in t.f.pipeline.stages if s.name in ("l1", "contact", "mesh_contact")]
    assert max(writers) < 198                                                                  # 采集在全部写状态的 stage 之后
    assert fleet_config(clock_of("d1_250")).l1_every == 1 and fleet_config(clock_of("d1_250_l1_125")).l1_every == 2

    sb = FastTier(["a"], {"a": ((0.0, 0.0, 0.0), 0.0)}, clock_profile="sandbox100")
    assert (sb.n_steps, sb.step_ns, sb.cp.physics_hz) == (10, 10_000_000, 100)
    with pytest.raises(PhysError) as e:                                                       # 100 ms 不是 8 ms 的整数倍
        FastTier(["a"], {"a": ((0.0, 0.0, 0.0), 0.0)}, clock_profile="d1_250_l1_125")
    assert e.value.code == 637
    h = FastTier(["a"], {"a": ((0.0, 0.0, 0.0), 0.0)}, clock_profile="d1_250_l1_125", tick_ms=200)
    assert (h.l1_every, h.n_steps, h.cp.physics_hz) == (2, 25, 125)
    for cp, tick, outs in (("d1_250_l1_125", 200, ("imu",)), ("sandbox100", 100, ("lidar",)),
                           ({"name": "d1_250", "main_hz": 250, "physics_hz": 125, "l1_hz": 125}, 200, ("bag",))):
        with pytest.raises(PhysError) as e:
            FastTier(["a"], {"a": ((0.0, 0.0, 0.0), 0.0)}, clock_profile=cp, tick_ms=tick, outputs=outs)
        assert e.value.code == 637
    with pytest.raises(PhysError) as e:                                                       # 物理频率下限独立于时钟档名
        check_physics_hz(ClockProfile("custom", 250, 125, 125), ("imu", "camera"))
    assert e.value.code == 637 and str(MIN_IMU_PHYSICS_HZ) in str(e.value)
    check_physics_hz(ClockProfile("custom", 250, 125, 125), ("detections",))
    check_physics_hz(clock_of("d1_250"), ("imu", "lidar", "bag"))
    for bad in ({"name": "d1_250", "main_hz": 250, "physics_hz": 250, "l1_hz": 125}, {"name": "nope", "main_hz": 250, "physics_hz": 250,
                                                                                         "l1_hz": 250}, "d9"):
        with pytest.raises(PhysError) as e:
            fleet_config(clock_of(bad))
        assert e.value.code == 637


def test_vehicle_profiles_resolve_through_the_d3_overlay() -> None:
    from awr.sim.fleet.profiles import ProfileTable

    T = ProfileTable()
    assert resolve_profile("x500", T) == "x500" and resolve_profile("p600_mid360_inv", T) == "p600_mid360"
    with pytest.raises(PhysError) as e:
        resolve_profile("no_such_vehicle", T)
    assert e.value.code == 630
    with pytest.raises(PhysError) as e:                                                       # 固定翼不由 Fast 档 L1 驱动
        FastTier(["a"], {"a": ((0.0, 0.0, 0.0), 0.0)}, vehicles={"a": "awr_f1"})
    assert e.value.code == 630


# ---------------------------------------------------------------------------------------------------------------- pose_history
def test_pose_history_grid_and_specific_force() -> None:
    t = FastTier(["g", "h"], {"g": ((0.0, 0.0, 0.0), 0.3), "h": ((10.0, 0.0, 20.0), -0.5)}, grounded=["g"], retain_s=None, history_s=30.0)
    rs = _run(t, {10: [TierCommand("g", "takeoff", 10, alt_m=5.0), TierCommand("h", "velocity", 10, vel_enu_mps=(3.0, -1.0, 0.5),
                                                                                 yaw_rate_rps=0.2)]}, 60)
    assert all(r.n_steps == 25 and r.t_end_ns - r.t_start_ns == 100_000_000 for r in rs)
    g = t.history.slice("g", 0, 6_000_000_000)
    assert np.array_equal(g.t_ns, np.arange(0, 6_000_000_001, 4_000_000))                     # 逐物理步、4 ms 网格、自 0 起
    rest = g.state[: 10 * 25]                                                                  # 起飞前静止：机体原点高度、比力 (0, 0, +g)
    assert np.allclose(rest[:, 2], BODY_Z_M, atol=0, rtol=0) and np.allclose(rest[:, 13:16], [0.0, 0.0, G], atol=1e-9)
    assert np.allclose(np.linalg.norm(g.state[:, 3:7], axis=1), 1.0, atol=1e-12)
    for u in ("g", "h"):                                                                       # 比力与速度差分逐步自洽
        s = t.history.slice(u, 0, 6_000_000_000).state
        a = (s[1:, 7:10] - s[:-1, 7:10]) / 0.004
        assert np.allclose(_quat_rot(s[1:, 3:7], s[1:, 13:16]) + G_W, a, atol=1e-9)
        assert np.max(np.linalg.norm(s[:, 10:13], axis=1)) < 5.0 and np.max(np.linalg.norm(s[:, 13:16], axis=1)) < 30.0
    h_end = t.history.last_sample("h")
    assert h_end.vel_enu_mps[0] > 2.5 and h_end.vel_enu_mps[1] < -0.7 and h_end.vel_enu_mps[2] > 0.3
    assert abs(float(h_end.omega_b_rps[2]) - 0.2) < 0.05                                      # 偏航角速度（FLU）
    assert t.history.last_sample("g").pos_enu_m[2] > BODY_Z_M + 3.0
    lift = [c for r in rs for c in r.contacts if c.kind == "LIFTOFF"]
    assert [c.uav for c in lift] == ["g"] and lift[0].t_ns > 1_000_000_000                     # SPOOLUP 后离地


# ---------------------------------------------------------------------------------------------------------------- 指令与状态
def test_commands_status_and_contacts() -> None:
    t = FastTier(["a", "b", "c"], {"a": ((0.0, 0.0, 0.0), 0.0), "b": ((30.0, 0.0, 15.0), 0.0), "c": ((-30.0, 0.0, 25.0), 0.0)},
                 grounded=["a"])
    t.open()
    assert dict(t.status()) == {"a": VehicleStatus.GROUNDED, "b": VehicleStatus.AIRBORNE, "c": VehicleStatus.AIRBORNE}
    bad = t.submit([TierCommand("x", "hover", 0), TierCommand("a", "follow_path", 0, path_enu_m=np.zeros((0, 3))),
                    TierCommand("a", "orbit", 0, orbit=OrbitSpec((0.0, 0.0, 10.0), 0.5))])
    assert [(r.ok, r.code) for r in bad] == [(False, 630)] * 3
    assert all(r.ok for r in t.submit([TierCommand("a", "takeoff", 0, alt_m=10.0), TierCommand("b", "goto", 0, target_enu_m=(40.0, 10.0, 20.0),
                                                                                              speed_mps=4.0),
                                       TierCommand("c", "kill", 5)]))
    seen: list = []
    for k in range(150):
        r = t.advance(k)
        seen += r.contacts
        if k == 140:
            t.submit([TierCommand("a", "land", 141)])
    assert t.tracker.kind("b") == "goto" and t.tracker.kind("c") == "kill"
    b = t.history.last_sample("b")
    assert np.linalg.norm(b.pos_enu_m - np.array([40.0, 10.0, 20.0])) < 0.3                     # goto 目标为机体原点
    st = t.status()
    assert st["c"] == VehicleStatus.CRASHED and st["b"] == VehicleStatus.AIRBORNE
    imp = [c for c in seen if c.uav == "c"]
    assert [c.kind for c in imp] == ["IMPACT"] and imp[0].detector == "dsm" and imp[0].speed_mps > 3.0
    for k in range(150, 400):
        seen += t.advance(k).contacts
    assert t.status()["a"] == VehicleStatus.GROUNDED and t.history.last_sample("a").pos_enu_m[2] == pytest.approx(BODY_Z_M, abs=1e-9)
    assert [c.kind for c in seen if c.uav == "a"] == ["LIFTOFF", "TOUCHDOWN"]
    assert all(r.ok for r in t.submit([TierCommand("b", "orbit", 400, orbit=OrbitSpec((40.0, 0.0, 20.0), 10.0), speed_mps=3.0)]))
    for k in range(400, 500):
        t.advance(k)
    p = t.history.last_sample("b").pos_enu_m
    assert abs(float(np.hypot(p[0] - 40.0, p[1])) - 10.0) < 0.5 and t.tracker.kind("b") == "orbit"
    info = t.close()
    assert info["tier"] == "fast" and info["ticks"] == 500 and info["physics_hz"] == 250 and info["profiles"]["a"] == "x500"


def test_determinism_pose_history_bytes() -> None:
    def once() -> tuple[str, bytes]:
        t = FastTier(["a", "b"], {"a": ((0.0, 0.0, 0.0), 0.1), "b": ((5.0, 5.0, 12.0), 1.0)}, grounded=["a"], retain_s=None,
                     history_s=12.0)
        _run(t, {0: [TierCommand("a", "takeoff", 0, alt_m=8.0)], 30: [TierCommand("b", "velocity", 30, vel_enu_mps=(1.5, 2.0, -0.3),
                                                                                    yaw_rate_rps=-0.3)],
                 60: [TierCommand("a", "goto", 60, target_enu_m=(12.0, -4.0, 9.0), speed_mps=5.0), TierCommand("b", "hover", 60)]}, 100)
        sl = t.history.slice("a", 0, 10_000_000_000)
        return t.close()["pose_sha256"], sl.t_ns.tobytes() + sl.state.tobytes()

    a, b = once(), once()
    assert a == b


# ---------------------------------------------------------------------------------------------------------------- mesh_contact
@pytest.fixture(scope="module")
def synth_world(tmp_path_factory):
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    from synthworld import build_synth_world

    from awr.world.mesh.query import open_mesh_world_query

    mp = pytest.MonkeyPatch()
    w = build_synth_world(tmp_path_factory.mktemp("fastw"), mp)
    yield w, open_mesh_world_query(w)
    mp.undo()


def test_mesh_contact_side_wall_crash_and_roof_landing(synth_world) -> None:
    _w, q = synth_world                                    # 盒子 20 × 20 × 30 m 位于 world (30..50, −50..−30)
    t = FastTier(["a", "b", "far"], {"a": ((20.0, -40.0, 10.0), 0.0), "b": ((40.0, -40.0, 35.0), 0.0), "far": ((-80.0, 80.0, 20.0), 0.0)},
                 q, grounded=[], retain_s=None, history_s=15.0)
    t.open()
    t.submit([TierCommand("a", "velocity", 0, vel_enu_mps=(4.0, 0.0, 0.0)), TierCommand("b", "land", 0),
              TierCommand("far", "velocity", 0, vel_enu_mps=(0.0, -3.0, 0.0))])
    ev: list = []
    for k in range(120):
        r = t.advance(k)
        ev += r.contacts
    a = [c for c in ev if c.uav == "a"]
    assert a and a[0].kind == "COLLISION_WORLD" and a[0].detector == "mesh"                     # 侧向撞墙：网格先于 DSM 判定
    assert a[0].prim_id is not None and a[0].semantic is not None and a[0].normal_enu is not None and a[0].normal_enu[0] < -0.9
    assert 29.0 < a[0].pos_enu_m[0] <= 30.0 + 1e-6 and a[0].speed_mps > 3.0
    sa = t.history.slice("a", 0, a[0].t_ns + 40_000_000)
    jump = np.linalg.norm(np.diff(sa.state[:, 0:3], axis=0), axis=1)
    assert float(jump.max()) <= 0.4 + 1e-9                                                      # 回到上次安全位置（≤ 一个检查周期）
    st = t.status()
    assert st["a"] == VehicleStatus.CRASHED and t.f.S.blocks["d3phys"]["mesh_crash"][0] == 1
    assert t.history.last_sample("a").pos_enu_m[2] == pytest.approx(BODY_Z_M, abs=1e-6)        # 失能下落，停在地表
    assert t.history.last_sample("a").pos_enu_m[0] < 30.0
    b = [c for c in ev if c.uav == "b"]
    assert [c.kind for c in b] == ["TOUCHDOWN"] and b[0].detector == "dsm"                     # 降落楼顶：交给 contact，不判坠毁
    assert st["b"] == VehicleStatus.GROUNDED and t.history.last_sample("b").pos_enu_m[2] == pytest.approx(30.0 + BODY_Z_M, abs=0.05)
    assert st["far"] == VehicleStatus.AIRBORNE and not [c for c in ev if c.uav == "far"]
    m = t.close()["mesh_contact"]
    assert m["enabled"] and m["crashes"] == 1 and m["calls"] == 120 * 5
    assert m["exact"] < m["checked"]                                                          # 净空门控：远离网格的机体只在 10 Hz 刷新时精查
    clr = float(t.f.S.blocks["d3phys"]["clear_m"][2])
    assert 0.0 < clr <= 50.0


def test_mesh_contact_off_without_mesh_layer() -> None:
    t = FastTier(["a"], {"a": ((0.0, 0.0, 10.0), 0.0)}, grounded=[])
    assert t.caps()["mesh_contact"] is False
    with pytest.raises(PhysError) as e:
        FastTier(["a"], {"a": ((0.0, 0.0, 10.0), 0.0)}, grounded=[], mesh_contact=True)
    assert e.value.code == 630


# ---------------------------------------------------------------------------------------------------------------- Env
@pytest.fixture(scope="module")
def synth_event(synth_world, tmp_path_factory):
    from test_compile import EVENT, OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    w, _q = synth_world
    tmp = tmp_path_factory.mktemp("fastev")
    ev = copy.deepcopy(EVENT)
    ev["annotations"] = [
        {"id": "meet_ab", "type": "proximity_encounter", "participants": ["person_a", "person_b"], "window": [5, 25]},
        {"id": "board_bc", "type": "board_vehicle", "participants": ["person_b", "vehicle_c"], "window": [70, 90]},
        {"id": "enter_c", "type": "enter_building", "participants": ["person_c"], "window": [35, 45], "params": {"anchor": "ev_door"}}]
    ev["brief"] = {"public_anchors": ["ev_plaza", "ev_door"],
                   "targets": [{"actor": "person_a", "description_en": "Blue top", "description_zh": "蓝色上衣"}]}
    d = tmp / "ev"
    d.mkdir()
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    # meet_ab 为示意声明（合成路线中 a、b 不相遇）；夹具不核对语义，门 3 以 lenient 编译（strict 下为 613，E01）
    r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache", strict=False))
    assert r.status == "compiled", r.as_dict()
    return w, d / "v1"


def _episode(spec, make_agent, l2=None) -> dict:
    from awr.agentsdk.env.core import BenchEnv

    env = BenchEnv(spec, l2=l2)
    obs, infos = env.reset()
    ags = {u: make_agent(u) for u in env.possible_agents}
    for u, a in ags.items():
        a.reset(infos[u]["config"])
    while env.agents:
        obs, *_ = env.step({u: ags[u].step(o["obs"], o["inbox"]) for u, o in obs.items()})
    return env.close()


def test_env_fast_l1_same_agent_code_and_bytewise_streams(synth_event, tmp_path: Path) -> None:
    from anet4d.baselines import BASELINES

    from awr.agentsdk.env.spec import EnvSpec, FleetEntry

    w, ev = synth_event
    fleet = (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0)))
    runs = {}
    for name, physics in (("a", "fast"), ("b", "fast"), ("k", "kinematic")):
        spec = EnvSpec(f"t-fast-{name}", 5, w, ev, fleet, physics=physics, duration_s=20.0, out_dir=tmp_path / name)
        runs[name] = _episode(spec, lambda u: BASELINES["coverage_scan"]())
    a, b, k = runs["a"], runs["b"], runs["k"]
    assert a["tier"] == "fast" and k["tier"] == "kinematic" and a["n_ticks"] == k["n_ticks"] == 200
    for u in ("uav-01", "uav-02"):                                                             # 同一种子：观测流与动作流逐字节一致
        assert (tmp_path / "a/agents" / u / "acts.jsonl").read_bytes() == (tmp_path / "b/agents" / u / "acts.jsonl").read_bytes()
        assert (tmp_path / "a/agents" / u / "obs.sha256").read_bytes() == (tmp_path / "b/agents" / u / "obs.sha256").read_bytes()
        assert (tmp_path / "a/agents" / u / "obs.sha256").read_bytes() != (tmp_path / "k/agents" / u / "obs.sha256").read_bytes()
        assert a["agents"][u]["flight_s"] > 10.0 and a["agents"][u]["term"]["reason"] == "time_limit"
    assert a["pose_sha256"] == b["pose_sha256"] and a["pose_sha256"] != k["pose_sha256"]
    ti = a["tier_info"]
    assert ti["physics_hz"] == 250 and ti["steps_per_tick"] == 25 and ti["mesh_contact"]["enabled"] and ti["mesh_contact"]["crashes"] == 0
    assert a["clock_profile"] == {"name": "d1_250", "main_hz": 250, "physics_hz": 250, "l1_hz": 250}
    assert a["run_hash"] == b["run_hash"] and a["config_sha256"] != k["config_sha256"]        # physics 进入运行哈希
    fl = np.load(tmp_path / "a/truth/fleet.npz")
    assert fl["pos"].shape[:2] == (201, 2) and float(fl["pos"][-1, :, 2].min()) > 10.0


def test_env_fast_l2_runs_and_low_physics_rate_is_refused(synth_event, tmp_path: Path) -> None:
    sys.path.insert(0, str(HERE.parents[0] / "env"))
    from test_l2 import FakeRaster

    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.l2 import CAM_SENSOR, IMU_SENSOR, LIDAR_SENSOR, L2Config, L2Sensors
    from awr.agentsdk.env.spec import EnvSpec, FleetEntry

    w, ev = synth_event
    fleet = (FleetEntry("uav-01", (-10.0, 50.0, 30.0), 0.0, airborne=True),)

    def l2(env):
        return L2Sensors(world_dir=Path(env.spec.world_dir), tape=env.tape, uavs=env.uavs, seed=env.spec.seed, noise=env.spec.noise,
                         obs_level=env.spec.obs_level, nav=env.nav, mount_t_bs_m=env.cam.mount_t_bs_m, hfov_deg=env.cam.hfov_deg,
                         cfg=L2Config(camera_profile="rgb720-gs", cache_dir=Path(env.spec.out_dir) / "wq"), renderer=FakeRaster())

    class Goto:
        def reset(self, cfg):
            self.cfg = cfg

        def step(self, obs, inbox):
            self.last = obs
            return {"schema": "drone4d.bench.act.v1", "k": obs["k"], "nav": {"mode": "goto", "pos_enu_m": [0.0, 60.0, 30.0], "speed_mps": 4.0}}

    agent = Goto()
    spec = EnvSpec("t-fast-l2", 3, w, ev, fleet, obs_level="L2", physics="fast", duration_s=2.0, out_dir=tmp_path / "l2")
    m = _episode(spec, lambda u: agent, l2=l2)
    assert m["tier"] == "fast" and m["n_ticks"] == 20 and m["agents"]["uav-01"]["term"]["reason"] == "time_limit"
    s = agent.last["sensors"]
    assert {CAM_SENSOR, LIDAR_SENSOR, IMU_SENSOR} <= set(s)
    assert all(a["result"] in ("applied", "clamped") for a in agent.last["acks"] if a["item"] == "nav")
    fl = np.load(tmp_path / "l2/truth/fleet.npz")                                              # 真值：朝目标飞行、保持高度
    assert float(fl["pos"][-1, 0, 1]) > 51.0 and abs(float(fl["pos"][-1, 0, 2]) - 30.0) < 3.0                    # 目标经导航误差换算
    with pytest.raises(PhysError) as e:                                                       # L2 含 imu、lidar：100 Hz 物理被拒
        env = BenchEnv(EnvSpec("t-fast-sb", 3, w, ev, fleet, obs_level="L2", physics="fast", duration_s=1.0, out_dir=tmp_path / "sb",
                               clock_profile="sandbox100"), l2=l2)
        env.reset()
    assert e.value.code == 637
    env = BenchEnv(EnvSpec("t-fast-sb1", 3, w, ev, fleet, physics="fast", duration_s=1.0, clock_profile="sandbox100"))
    env.reset()                                                                                # L1 不含 imu、lidar：sandbox100 可运行
    assert env.tier.cp.physics_hz == 100 and env.tier.n_steps == 10
    env.close()
