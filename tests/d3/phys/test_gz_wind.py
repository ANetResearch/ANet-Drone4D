"""gz `field` 风模式（M24-FR-046、M24 §6.5.8；M24-AC-017 field 部分；D3-AC-35 对照运行的风输入前提）。

- 离线（本机）：
  1. `drag_force` 与 FleetSim `aero_force`（M08 composite / linear oracle）逐位或 1e-12 内一致；
  2. C++ 力律（native/gz/plugins/wind/WindLaw.hh，经 law_probe.cc 编译）与 numpy oracle 一致；
  3. FieldWind 下发风矢量与 M07 env stage（Fast 档同一代码路径）在同一位置、同一网格时刻的查询逐位一致（雷暴预设 + Dryden 湍流 +
     运行中 env/set）；压缩后的 WindSp 序列按零阶保持还原后与逐网格点风相同；
  4. 插件 SDF 段、力律校验（linear 未标定 k → 630、力律混用 → 630、与 WindEffects 同用 → 630）；
  5. GzSimBackend 把 WindSp 放入 Control.wind；SupervisedGzBackend 在 kill 后从回合起点重放，FieldWind 返回缓存，控制消息与
     样本块逐位一致；输入状态不一致时 638 平台失败。
- dmax（d3-gz 容器内 `launcher = local`）：
  6. 8 m/s 恒定风悬停：倾角与 composite 力律稳态解之差 ≤ 0.3°；无风 5 m/s 巡航：倾角与 composite（地速）稳态解之差 ≤ 0.3°
     （验证 MulticopterMotorModel 已按地速施加旋翼阻力、插件只补风增量）；
  7. 时空变化的 M07 风（雷暴 + Dryden）两次运行 PoseHistory 逐位一致，且与无风运行不同。

运行：本机 `PYTHONPATH=python .venv-d3/bin/python -m pytest -q tests/d3/phys/test_gz_wind.py`；dmax
`tools/deploy/dmax/run_gz_tests.sh /repo/tests/d3/phys/test_gz_wind.py`。`AWR_D3_WIND_OUT=<dir>` 时把 dmax 实测写成 JSON。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

# D2 环境（.venv）无 D3 依赖：awr.phys.gz.protocol 导入 google.protobuf（gz 协议消息）
pytest.importorskip("google.protobuf")

from awr.phys import PhysError
from awr.phys.adapter import TierCommand
from awr.phys.gz import protocol as P
from awr.phys.gz.wind import DragSpec, FieldWind, drag_force, field_plugin_xml, load_drag_spec, with_field_wind

ROOT = Path(__file__).resolve().parents[3]
STEP_NS = 4_000_000
G = 9.80665
K_F = 8.54858e-06                                   # x500_d4d motorConstant
M_GZ = 2.0 + 4 * 0.016076923076923075               # x500_d4d 总质量（base_link + 4 旋翼）
X500 = DragSpec(law="composite", mass_kg=2.064, cda_m2=0.02065, c_rd=8.06428e-5, n_rot=4, omega_max_rad_s=1000.0, t_max_n=4 * 8.55,
                profile="x500")
HAS_GZ = shutil.which("gz") is not None and any(
    (Path(p) / "libDrone4DGz.so").exists() for p in os.environ.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "").split(":") if p)
needs_gz = pytest.mark.skipif(not HAS_GZ, reason="needs gz-sim with libDrone4DGz.so (d3-gz image)")


def _rand(n: int, seed: int = 1):
    rng = np.random.default_rng(seed)
    v = rng.normal(0, 6, (n, 3))
    w = rng.normal(0, 5, (n, 3))
    b3 = rng.normal(0, 0.3, (n, 3)) + np.array([0.0, 0.0, 1.0])
    b3 /= np.linalg.norm(b3, axis=1, keepdims=True)
    th = rng.uniform(0.0, 1.2, n)
    return v, w, b3, th


# ---------------------------------------------------------------------------------------------------------------- 1. 力律
def test_drag_force_matches_fleetsim_aero() -> None:
    from awr.sim.fleet import kernels_l1 as K
    from awr.sim.fleet.aero import aero_force

    spec = load_drag_spec("x500")
    assert spec.law == "composite" and spec.cda_m2 == pytest.approx(0.02065) and spec.c_rd == pytest.approx(8.06428e-5)
    v, w, b3, th = _rand(400)
    rho = np.full(400, 1.21)
    PT = np.zeros((1, 10))
    PT[0, [K.P_MASS, K.P_TMAX, K.P_NROT, K.P_WMAX, K.P_AERO, K.P_CDA, K.P_CRD]] = (spec.mass_kg, spec.t_max_n, spec.n_rot,
                                                                                   spec.omega_max_rad_s, 1.0, spec.cda_m2, spec.c_rd)
    pid = np.zeros(400, np.int64)
    so = spec.n_rot * spec.omega_max_rad_s * np.sqrt(np.minimum(np.maximum(th, 0.0), 1.0))
    ref = aero_force(v - w, b3, th, rho, PT, pid)
    full = drag_force(spec, v, w, b3, so, rho=1.21, motor_rotor_drag=False)
    assert np.array_equal(full, ref)                                             # 同式同序：逐位一致
    inc = drag_force(spec, v, w, b3, so, rho=1.21, motor_rotor_drag=True)
    vp = v - (np.sum(v * b3, axis=1, keepdims=True)) * b3
    motor = -(so * spec.c_rd)[:, None] * vp                                      # MulticopterMotorModel：地速旋翼阻力
    assert np.max(np.abs(inc + motor - ref)) < 1e-12
    # linear：FleetSim F = −K_dv·v_r 与 F = m·k·(w − v)，k = K_dv / m
    prof = SimpleNamespace(mass_kg=2.0, aero="linear", k_dv=0.35, cda_m2=0.0, c_rd=0.0, n_rot=4, omega_max_rad_s=1000.0, t_max_n=34.2,
                           profile_id="lin")
    lin = DragSpec.from_profile(prof)
    assert lin.law == "linear" and lin.k_per_s == pytest.approx(0.175)
    PT[0, [K.P_AERO, K.P_KDV]] = (0.0, 0.35)
    assert np.max(np.abs(drag_force(lin, v, w, b3, so) - aero_force(v - w, b3, th, rho, PT, pid))) < 1e-12
    assert X500.hover_sum_omega == pytest.approx(4 * 1000.0 * math.sqrt(2.064 * G / 34.2))


def test_cpp_law_matches_oracle(tmp_path: Path) -> None:
    cxx = shutil.which("g++") or shutil.which("c++")
    if cxx is None:
        pytest.skip("needs a C++ compiler")
    src = ROOT / "native/gz/plugins/wind/law_probe.cc"
    exe = tmp_path / "law_probe"
    subprocess.run([cxx, "-std=c++17", "-O2", f"-I{src.parent}", str(src), "-o", str(exe)], check=True, capture_output=True)
    v, w, b3, th = _rand(300, seed=5)
    so = 4000.0 * np.sqrt(np.clip(th, 0, 1))
    lin = DragSpec(law="linear", mass_kg=2.064, k_per_s=0.31)
    lines, ref = [], []
    for law, spec in ((1, X500), (0, lin)):
        for motor in (0, 1):
            for i in range(len(v)):
                row = [law, motor, 1.2, spec.cda_m2, spec.c_rd, spec.mass_kg, spec.k_per_s, *v[i], *w[i], *b3[i], so[i]]
                lines.append(" ".join(repr(float(x)) if j > 1 else str(x) for j, x in enumerate(row)))
            ref.append(drag_force(spec, v, w, b3, so, rho=1.2, motor_rotor_drag=bool(motor)))
    out = subprocess.run([str(exe)], input="\n".join(lines) + "\n", check=True, capture_output=True, text=True).stdout
    got = np.array([[float(x) for x in ln.split()] for ln in out.strip().splitlines()])
    ref = np.concatenate(ref)
    assert got.shape == ref.shape
    assert np.max(np.abs(got - ref)) <= 1e-12 * max(1.0, float(np.max(np.abs(ref))))


# ---------------------------------------------------------------------------------------------------------------- 3. M07 采样
class _S:
    """env stage 用的 FleetState 替身：只记录 `set_wind_from_enu` 的 ENU 输入。"""

    def __init__(self, n: int) -> None:
        self.n, self.capacity = n, 16
        self.enu = SimpleNamespace(pos=np.zeros((16, 3)), vel=np.zeros((16, 3)))
        self.env_gust = np.zeros(16)
        self.rho = np.zeros(16)
        self.env_flags = np.zeros(16, np.uint16)
        self.got: np.ndarray | None = None

    def active_idx(self) -> np.ndarray:
        return np.arange(self.n)

    def set_wind_from_enu(self, w, act) -> None:
        self.got = np.array(w, np.float64)


def _env(seed: int = 3):
    from awr.environment.field import EnvironmentServiceImpl
    from awr.environment.keyframe import EnvOp

    e = EnvironmentServiceImpl(world_id="t", world_seed=seed, bounds=((-900.0, -1000.0, 0.0), (900.0, 1000.0, 300.0)),
                               initial_preset="thunderstorm", load_assets=False, capacity=16)
    e.apply(EnvOp("set", patch={"config": {"wind": {"turbulence": {"model": "dryden"}}}}), 1)
    return e


def test_field_wind_matches_env_stage_bitwise() -> None:
    """M24-AC-017：field 模式下发风矢量与 M07 查询逐位一致（Fast 档 env stage 同一代码路径、同一 slot 与 Dryden 推进顺序）。"""
    from awr.environment.keyframe import EnvOp
    from awr.environment.stage import env_stage, reset_plugin_state

    uavs = ("uav-01", "uav-02", "uav-03")
    fw = FieldWind(_env(), uavs, record=True)
    ref_env = _env()
    S = _S(len(uavs))
    rng = np.random.default_rng(11)
    pos = rng.uniform([-50, -50, 20], [50, 50, 80], (3, 3))
    vel = rng.normal(0, 4, (3, 3))
    held = np.zeros((3, 3))
    ref_rows = []
    try:
        for k in range(40):
            t_k = k * 100_000_000
            if k == 17:                                                          # 运行中 env/set（磁带环境关键帧），两侧同时生效
                for e in (fw.env, ref_env):
                    e.apply(EnvOp("set", patch={"wind": {"speed_ref_mps": 12.0, "dir_from_deg": 200.0}}, duration_s=0), t_k // STEP_NS)
            msgs = fw.for_tick(k, t_k, 25, {u: (pos[i], vel[i]) for i, u in enumerate(uavs)})
            assert [m.apply_step for m in msgs] == sorted(m.apply_step for m in msgs)
            by_step: dict[int, list] = {}
            for m in msgs:
                by_step.setdefault(m.apply_step, []).append(m)
            for j in range(0, 25, 5):                                            # 参考：env stage 在网格 tick 以同一位置求风
                tick = (t_k + j * STEP_NS) // STEP_NS
                p = pos + vel * (j * STEP_NS * 1e-9)
                p[:, 2] -= 0.227
                S.enu.pos[:3], S.enu.vel[:3] = p, vel
                env_stage(S, SimpleNamespace(tick=tick, t_ns=tick * STEP_NS, dt_tick=0.004, env=ref_env, events=None))
                ref_rows.append(S.got.copy())
                for m in by_step.get(j, ()):
                    held[m.uav_index] = (m.wx, m.wy, m.wz)
                assert np.array_equal(held, S.got)                               # 零阶保持还原后逐位一致
            pos = pos + vel * 0.1 + rng.normal(0, 0.05, (3, 3))
            vel = vel + rng.normal(0, 0.3, (3, 3))
    finally:
        reset_plugin_state()
    got = np.stack([w for _k, _j, _t, _p, w in fw.log])
    assert np.array_equal(got, np.stack(ref_rows))
    assert len(fw.log) == 40 * 5 and fw.stats["grid_points"] == 200
    assert float(np.max(np.abs(np.diff(got, axis=0)))) > 0.1                    # 湍流与阵风确在变化
    assert fw.log[1][1] == 5 and fw.log[1][2] == 20_000_000
    d = fw.describe()
    assert d["wind_mode"] == "field" and d["sample"] == "grid_start_predicted" and d["messages"] <= 200 * 3


def test_grid_steps_and_step_guard() -> None:
    fw = FieldWind(SimpleNamespace(), ["a"])
    assert fw.grid_steps(0, 25) == [0, 5, 10, 15, 20]
    assert fw.grid_steps(8_000_000, 2) == []                                    # 8 ms tick：网格点不在本 tick 内
    assert fw.grid_steps(16_000_000, 2) == [1]
    with pytest.raises(PhysError) as e:
        FieldWind(SimpleNamespace(), ["a"], step_ns=3_000_000)
    assert e.value.code == 637


# ---------------------------------------------------------------------------------------------------------------- 4. SDF 与校验
def test_plugin_xml_and_checks() -> None:
    from awr.phys.gz.compose import UavSpawn, compose_run

    sp = [UavSpawn("uav-01", (0.0, 0.0, 0.3)), UavSpawn("uav-02", (5.0, 0.0, 0.3))]
    xml = field_plugin_xml([s.uav for s in sp], X500, rho=1.2)
    sdf = with_field_wind(compose_run(sp, ground_plane=True), xml)
    assert sdf.count("drone4d::Drone4DWindDrag") == 1 and sdf.index("Drone4DWindDrag") > sdf.index('<model name="uav-02">')
    assert '<uav model="uav-01" link="base_link" mass="2.064" cda="0.02065" c_rd="8.06428e-05"' in sdf
    assert sdf.count('<rotor joint="rotor_') == 8 and "<law>composite</law>" in sdf and "<rho>1.2</rho>" in sdf
    import xml.etree.ElementTree as ET

    ET.fromstring(sdf)                                                           # 合法 XML
    with pytest.raises(PhysError) as e:
        with_field_wind(compose_run(sp, ground_plane=True, wind=True), xml)
    assert e.value.code == 630
    with pytest.raises(PhysError) as e:
        field_plugin_xml(["uav-01"], DragSpec(law="linear", mass_kg=2.0))       # 未标定 k
    assert e.value.code == 630
    with pytest.raises(PhysError) as e:
        field_plugin_xml(["a", "b"], {"a": X500, "b": DragSpec(law="linear", mass_kg=2.0, k_per_s=0.3)})
    assert e.value.code == 630
    assert "<law>linear</law>" in field_plugin_xml(["a"], DragSpec(law="linear", mass_kg=2.0, k_per_s=0.3))


# ---------------------------------------------------------------------------------------------------------------- 5. 宿主接线与重放
zmq = pytest.importorskip("zmq")


class _FakeGz:
    """最小 Drone4DClock 替身：机体按速度设定积分，并按当前保持的风矢量附加 0.2·w 漂移（风输入经 Control.wind 进入状态）；
    `kill_at = (life, tick)` 时该次启动的实例在收到该 tick 的 Control 时退出。记录每次启动收到的 (tick, wind) 序列。"""

    def __init__(self, tmp: Path, n: int, kill_at: tuple[int, int] | None = None) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.endpoint = f"ipc://{tmp / 'wind-ctl.sock'}"
        self.n, self.kill_at = n, kill_at
        self.restarts = self.lives = 0
        self.wind_log: list[list] = []
        self._dead, self._stop = threading.Event(), threading.Event()
        self.start()

    def start(self) -> None:
        self._dead.clear()
        self._stop.clear()
        ready = threading.Event()
        self.wind_log.append([])
        self._th = threading.Thread(target=self._serve, args=(self.lives, ready), daemon=True)
        self.lives += 1
        self._th.start()
        ready.wait(5)

    def alive(self) -> bool:
        return not self._dead.is_set()

    def stop(self, force: bool = False) -> None:
        self._stop.set()
        self._th.join(5)
        self._dead.set()

    def restart(self, ready_timeout_s: float = 60.0) -> None:
        self.stop(True)
        self.restarts += 1
        self.start()

    def _serve(self, life: int, ready: threading.Event) -> None:
        sock = zmq.Context.instance().socket(zmq.REP)
        sock.setsockopt(zmq.LINGER, 0)
        sock.bind(self.endpoint)
        ready.set()
        pos = np.tile([0.0, 0.0, 0.2], (self.n, 1))
        vel, wind = np.zeros((self.n, 3)), np.zeros((self.n, 3))
        seq = t = 0
        try:
            while not self._stop.is_set():
                if not sock.poll(20):
                    continue
                body, m = P.decode(sock.recv())
                if body == "control" and self.kill_at == (life, int(m.tick)):
                    self._dead.set()
                    break
                if body == "hello":
                    st = np.zeros((self.n, P.STATE_N))
                    st[:, 0:3], st[:, 6], st[:, 15] = pos, 1.0, G
                    sock.send(P.encode("hello_ack", P.msg("HelloAck", n_uav=self.n, t_ns=t, initial_state=st.tobytes())))
                    continue
                if body != "control":
                    sock.send(P.encode("bye", P.msg("Bye", iterations=t // STEP_NS)))
                    continue
                seq = int(m.seq)
                for sp in m.setpoints:
                    vel[sp.uav_index] = (sp.vx, sp.vy, sp.vz) if sp.enabled else (0.0, 0.0, 0.0)
                due = sorted(m.wind, key=lambda x: x.apply_step)
                self.wind_log[life].append((int(m.tick), [(x.uav_index, x.apply_step, x.wx, x.wy, x.wz) for x in due]))
                ts, rows = [], []
                for j in range(m.grant_steps):
                    for x in due:
                        if x.apply_step == j:
                            wind[x.uav_index] = (x.wx, x.wy, x.wz)
                    v = vel + 0.2 * wind
                    pos = pos + v * (STEP_NS * 1e-9)
                    t += STEP_NS
                    st = np.zeros((self.n, P.STATE_N))
                    st[:, 0:3], st[:, 6], st[:, 7:10], st[:, 15] = pos, 1.0, v, G
                    ts.append(t)
                    rows.append(st)
                sock.send(P.encode("step_done", P.msg("StepDone", seq=seq, tick=m.tick, n_steps=m.grant_steps, n_uav=self.n, t_ns=ts,
                                                       samples=np.stack(rows).astype("<f8").tobytes())))
        finally:
            sock.close(0)


PLAN = {2: [TierCommand("uav-01", "takeoff", 2, alt_m=30.0), TierCommand("uav-02", "takeoff", 2, alt_m=40.0)],
        30: [TierCommand("uav-01", "goto", 30, target_enu_m=(20.0, 5.0, 30.0), speed_mps=4.0)]}


def _run(fake: _FakeGz, n_ticks: int = 60):
    from awr.phys.gz.backend import SupervisedGzBackend

    fw = FieldWind(_env(), ["uav-01", "uav-02"])
    be = SupervisedGzBackend(fake, ["uav-01", "uav-02"], grounded=["uav-01", "uav-02"], poll_s=0.05, step_timeout_s=0.5, timeout_s=1.0,
                             wind=fw)
    be.open()
    for k in range(n_ticks):
        be.submit(PLAN.get(k, []))
        be.advance(k)
    return be.close(), fw


def test_backend_sends_field_wind_and_replays_from_cache(tmp_path: Path) -> None:
    ref_fake = _FakeGz(tmp_path / "ref", 2)
    info_ref, fw_ref = _run(ref_fake)
    ref_fake.stop()
    sent = ref_fake.wind_log[0]
    assert [k for k, _ in sent] == list(range(60))
    assert sum(len(w) for _k, w in sent) == fw_ref.stats["messages"] > 60      # 每 tick 都有逐机风（湍流），零阶保持压缩后
    assert {a for _k, w in sent for _i, a, *_ in w} <= {0, 5, 10, 15, 20}
    assert info_ref["wind"]["ticks"] == 60 and info_ref["wind"]["replayed"] == 0
    fake = _FakeGz(tmp_path / "f", 2, kill_at=(0, 33))
    info, fw = _run(fake)
    fake.stop()
    assert [(e["tick"], e["code"], e["completed"]) for e in info["recoveries"]] == [(33, 638, True)]
    assert info["pose_sha256"] == info_ref["pose_sha256"]                        # 重放与原运行逐位一致，且与无故障运行相同
    assert fw.stats["replayed"] == 33 + 1 and fw.stats["ticks"] == 60          # 重放 0–32，再重试故障 tick 33（均取缓存）
    assert fake.wind_log[1] == sent                                              # 重启后的实例收到与无故障运行相同的风序列
    with pytest.raises(PhysError) as e:                                          # 重放输入状态不同：平台失败
        fw.for_tick(5, 500_000_000, 25, {"uav-01": (np.ones(3), np.zeros(3)), "uav-02": (np.zeros(3), np.zeros(3))})
    assert e.value.code == 638 and e.value.detail["platform_failure"]


# ---------------------------------------------------------------------------------------------------------------- 6–7. dmax 实测
class _ConstEnv:
    """恒定风的 M07 替身（只实现 FieldWind 用到的三个调用）。"""

    def __init__(self, w: tuple[float, float, float], t_on_ns: int = 0) -> None:
        self.w, self.t_on = np.asarray(w, np.float64), int(t_on_ns)

    def on_env_tick_all(self, tick: int) -> list:
        return []

    def step_dryden(self, *a) -> None:
        return None

    def query(self, pos, t, *, fields, vel=None, agent_idx=None, out=None):
        n = len(pos)
        return SimpleNamespace(wind_mps=np.tile(self.w if t >= self.t_on else np.zeros(3), (n, 1)))


def _expected_tilt(v: np.ndarray, w: np.ndarray, spec: DragSpec = X500, rho: float = 1.225) -> float:
    """稳态（a = 0）倾角：T·b3 = m·g·ẑ − F，F = 插件 composite 增量 + 电机模型地速旋翼阻力；ΣΩ = 4·√(T / (4 k_f))。"""
    v, w = np.ravel(v), np.ravel(w)
    b3, so = np.array([0.0, 0.0, 1.0]), 4.0 * math.sqrt(M_GZ * G / (4 * K_F))
    for _ in range(60):
        f = drag_force(spec, v, w, b3, so, rho=rho)[0] - so * spec.c_rd * (v - np.dot(v, b3) * b3)
        tv = np.array([0.0, 0.0, M_GZ * G]) - f
        T = float(np.linalg.norm(tv))
        b3, so = tv / T, 4.0 * math.sqrt(T / (4 * K_F))
    return math.degrees(math.acos(b3[2]))


def _gz_fly(tmp: Path, env, plan: dict, n_ticks: int, *, field: bool = True, record: bool = False):
    from awr.phys.gz.backend import GzSimBackend
    from awr.phys.gz.compose import UavSpawn, compose_run
    from awr.phys.gz.instance import GzInstance

    inst = GzInstance(tmp, "local", launcher="local")
    sdf = compose_run([UavSpawn("uav-01", (0.0, 0.0, 0.3))], ground_plane=True, endpoint=inst.gz_endpoint)
    if field:
        sdf = with_field_wind(sdf, field_plugin_xml(["uav-01"], X500, rho=1.225))
    inst.start(sdf)
    fw = FieldWind(env, ["uav-01"], record=record) if field and env is not None else None
    rows = []
    try:
        inst.wait_socket(60)
        be = GzSimBackend(["uav-01"], inst.endpoint, wind=fw)
        be.open()
        for k in range(n_ticks):
            be.submit(plan.get(k, []))
            r = be.advance(k)
            sl = be.history.slice("uav-01", r.t_start_ns + 1, r.t_end_ns)
            rows.append(sl.state[1:])
        info = be.close()
    finally:
        inst.stop()
    return np.concatenate(rows), info, fw


def _tilt_deg(state: np.ndarray) -> np.ndarray:
    qx, qy = state[:, 3], state[:, 4]
    return np.degrees(np.arccos(np.clip(1.0 - 2.0 * (qx * qx + qy * qy), -1.0, 1.0)))


def _dump(name: str, doc: dict) -> None:
    out = os.environ.get("AWR_D3_WIND_OUT")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{name}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


@needs_gz
def test_gz_field_hover_tilt_and_calm_cruise(tmp_path: Path) -> None:
    takeoff = {5: [TierCommand("uav-01", "takeoff", 5, alt_m=10.0)]}
    res = {}
    # 8 m/s 东风（去向 +x），12 s 后开始；第 25–30 s 稳态
    s, info, _fw = _gz_fly(tmp_path / "hover", _ConstEnv((8.0, 0.0, 0.0), t_on_ns=12_000_000_000), takeoff, 300)
    ss = s[-1250:]
    v = ss[:, 7:10].mean(axis=0)
    exp = _expected_tilt(v[None], np.array([[8.0, 0.0, 0.0]]))
    got = float(_tilt_deg(ss).mean())
    res["hover_8mps"] = {"tilt_deg": got, "expected_deg": exp, "mean_v": v.tolist(), "wind_info": info["wind"]}
    assert info["wind"]["messages"] == 2                                         # 0 → 8 m/s 只下发两次（零阶保持）
    assert float(np.max(np.abs(v))) < 0.05 and abs(got - exp) <= 0.3
    assert float(ss[:, 0].mean()) > 0.0                                          # 顺风方向的位置偏移
    # 无风 5 m/s 巡航（地速）：倾角 = composite(v) 稳态解
    plan = {**takeoff, 100: [TierCommand("uav-01", "velocity", 100, vel_enu_mps=(5.0, 0.0, 0.0))]}
    s2, _info2, _ = _gz_fly(tmp_path / "cruise", _ConstEnv((0.0, 0.0, 0.0)), plan, 250)
    c = s2[-1000:]
    v2 = c[:, 7:10].mean(axis=0)
    exp2 = _expected_tilt(v2[None], np.zeros((1, 3)))
    got2 = float(_tilt_deg(c).mean())
    res["cruise_5mps_calm"] = {"tilt_deg": got2, "expected_deg": exp2, "mean_v": v2.tolist()}
    _dump("field_tilt", res)
    assert v2[0] > 4.0 and abs(got2 - exp2) <= 0.3


@needs_gz
def test_gz_field_m07_wind_deterministic(tmp_path: Path) -> None:
    try:
        env_a, env_b = _env(), _env()
    except Exception as e:                                                       # 容器内缺 M07 依赖时跳过
        pytest.skip(f"M07 environment service unavailable: {e}")
    plan = {5: [TierCommand("uav-01", "takeoff", 5, alt_m=15.0)],
            100: [TierCommand("uav-01", "goto", 100, target_enu_m=(25.0, 10.0, 15.0), speed_mps=4.0)]}
    a, ia, fa = _gz_fly(tmp_path / "a", env_a, plan, 220, record=True)
    b, ib, _ = _gz_fly(tmp_path / "b", env_b, plan, 220)
    calm, ic, _ = _gz_fly(tmp_path / "c", None, plan, 220, field=False)
    _dump("field_m07", {"pose_sha256": [ia["pose_sha256"], ib["pose_sha256"], ic["pose_sha256"]], "wind": ia["wind"],
                        "max_dev_from_calm_m": float(np.max(np.linalg.norm(a[:, :3] - calm[:, :3], axis=1)))})
    assert ia["pose_sha256"] == ib["pose_sha256"] and np.array_equal(a, b)      # 同一输入：逐位一致
    assert ia["pose_sha256"] != ic["pose_sha256"]
    assert float(np.max(np.linalg.norm(a[:, :3] - calm[:, :3], axis=1))) > 0.05
    assert ia["wind"]["grid_points"] == 220 * 5 and ia["wind"]["max_speed_mps"] > 1.0
    assert len(fa.log) == 220 * 5
