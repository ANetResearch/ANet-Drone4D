"""泄漏红队：结构化检索（R1）与高熵字节检索（R2）（M26 §8.3、FR-101、FR-102；D3-AC-16）。

在排名配置（`ranked = True`、63 位 CSPRNG 种子、M27 种子承诺值、realistic 噪声档）上运行 L1 与 L2 回合，经真实线协议（Unix
套接字，宿主 `SocketAgentHost`）录下 Agent 收到的全部消息（CONFIG、OBS 含 inbox、ERROR、CLOSE），对照运行真值做 R1 与 R2 检索：
本机真值位置、真值外参、字符串、整数、令牌与数值窗口命中 0，演员位置、云台角与演员序号不显著高于基线，JPEG 段白名单违规 0。
正向对照：向允许的字段注入真值（本机位置、演员位置、种子、外参、云台角、演员序号、inbox 字节）后，各类检索必须检出。

已知缺陷（登记于 KNOWN_FINDINGS，严格 xfail 用例跟踪）：导航解姿态 `q_wb_xyzw` 取真值（`env.nav.NavModel`，M26-FR-040 的姿态误差
未落地，AWR-20 §15.6.5 realistic 横滚俯仰 0.2°、航向 1.0° GM），R1 与 R2 的 `own_attitude` 每帧命中。渲染以 CPU 替身代替 EGL 光栅
相机，物理取运动学开发档；正式口径（realistic gz 物理、真实渲染、Agent 容器）在 dmax 上以 `record_container_episode` 执行。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from awr.agentsdk.env.core import TICK_NS, BenchEnv
from awr.agentsdk.env.l1 import L1Model
from awr.agentsdk.env.obs_audit import ObsSchemaViolation
from awr.agentsdk.env.spec import EnvSpec, FleetEntry
from awr.agentsdk.wire import PROTOCOL, T, WireSocket
from awr.bench.redteam import (
    CaptureAudit,
    CapturedFrame,
    ProbedEnv,
    Secrets,
    TruthSet,
    audit_frames,
    baseline_launcher,
    leak_report,
    read_capture,
    record_socket_episode,
    write_report,
)
from awr.bench.redteam.search import binom_sf, jpeg_markers, pair_hits, poisson_sf
from awr.bench.redteam.truth import derived_keys
from awr.sim.sensors import cbrng

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SDK = ROOT / "tools/sdk/drone4d/src"
sys.path.insert(0, str(HERE.parents[0] / "env"))

_p = HERE.parents[0] / "env" / "conftest.py"
_spec = importlib.util.spec_from_file_location("d3_env_conftest_redteam", _p)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)


@pytest.fixture(scope="session")
def synth_event(tmp_path_factory):
    """合成交接事件，舞台寻路固定为栅格后端：本模块的统计判据按该磁带标定（Detour 与栅格的背景演员轨迹不同，见 env conftest
    `synth_event_at`），结果不随本机是否装有 navsvc 依赖或 `AWR_EVENT_NAV` 取值而变。"""
    yield from _m.synth_event_at(tmp_path_factory, nav="grid")

# 已知缺陷：判据名 → 原因（修复后对应的严格 xfail 用例转为 XPASS 而失败，届时从此处移除）
KNOWN_FINDINGS = {
    "r1.own_attitude": "NavSolution.q_wb_xyzw 取真值姿态（M26-FR-040 姿态误差未落地）",
    "r2.own_attitude": "同上：q_wb_xyzw 的 float64 字节等于真值四元数",
}
L1_FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0)), FleetEntry("uav-03", (0.0, 40.0, 0.0)))
L2_FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 30.0), 0.0, airborne=True), FleetEntry("uav-02", (10.0, 50.0, 30.0), 0.0, airborne=True))


def _ranked_spec(synth_event, out: Path, case: str, *, obs_level: str, fleet, dur: float, l2: dict | None = None) -> EnvSpec:
    """排名配置：63 位高熵种子、M27 形式的运行标识与种子承诺值。种子按用例名由带密钥 BLAKE2b 派生（与 CSPRNG 输出同分布，
    用例可复现：1 mm 容差下导航解与真值的偶然成对吻合概率约 1e-6 / 帧，随机种子会使用例以约 1% 的概率偶发失败）。"""
    w, ev = synth_event
    d = hashlib.blake2b(case.encode(), key=b"d3-redteam-a06", digest_size=32).digest()
    seed = int.from_bytes(d[:8], "little") >> 1
    run_id = f"run-{d[8:16].hex()}"
    commit = "hmac-sha256:" + hashlib.sha256(d[16:] + seed.to_bytes(8, "little")).hexdigest()
    return EnvSpec(run_id, seed, w, ev, fleet, obs_level=obs_level, physics="kinematic", duration_s=dur, out_dir=out, ranked=True,
                   seed_commit=commit, l2=dict(l2 or {}))


def _l2_factory(lidar: bool = True):
    from test_l2 import FakeRaster

    from awr.agentsdk.env.l2 import L2Config, L2Sensors

    def make(env: BenchEnv) -> L2Sensors:
        return L2Sensors(world_dir=Path(env.spec.world_dir), tape=env.tape, uavs=env.uavs, seed=env.spec.seed, noise=env.spec.noise,
                         obs_level=env.spec.obs_level, nav=env.nav, mount_t_bs_m=env.cam.mount_t_bs_m, hfov_deg=env.cam.hfov_deg,
                         cfg=L2Config(camera_profile="rgb720-gs", cache_dir=Path(env.spec.out_dir) / "wq", lidar=lidar), renderer=FakeRaster())

    return make


class _WireAgent(threading.Thread):
    """线协议上的脚本 Agent：三段循环（goto、带偏航角速度的 velocity、goto），云台按正弦扫动（覆盖姿态与云台转动期间的真值）。"""

    def __init__(self, path: Path, leg: int = 20) -> None:
        super().__init__(daemon=True)
        self.path, self.leg, self.n_obs, self.error = path, leg, 0, None

    def run(self) -> None:
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            for _ in range(200):
                try:
                    c.connect(str(self.path))
                    break
                except OSError:
                    time.sleep(0.05)
            ws = WireSocket(c)
            ws.send(T.HELLO, {"protocol": PROTOCOL, "sdk_version": "redteam", "prefs": {}})
            _t, cfg = ws.recv(time.monotonic() + 30)
            cam = next(s["sensor_id"] for s in cfg["sensors"] if s["kind"] == "camera")
            p0 = cfg["start"]["nav_pos_enu_m"]
            ws.send(T.READY, {})
            while True:
                t, body = ws.recv(time.monotonic() + 60)
                if t != T.OBS:
                    break
                k = body["obs"]["k"]
                self.n_obs += 1
                leg = (k // self.leg) % 3
                if leg == 1:
                    nav = {"mode": "velocity", "vel_mps": [2.0, 1.0, 0.0], "yaw_rate_rps": 0.3, "frame": "world"}
                else:
                    nav = {"mode": "goto", "pos_enu_m": [p0[0] + (15.0 if leg else -5.0), p0[1] + (10.0 if leg else 0.0), 32.0], "speed_mps": 5.0}
                act = {"schema": "drone4d.bench.act.v1", "k": k, "nav": nav,
                       "gimbal": {"sensor_id": cam, "mode": "angles", "pitch_deg": -40.0 + 15.0 * float(np.sin(k / 9.0)),
                                  "yaw_deg": 25.0 * float(np.cos(k / 13.0))}}
                if k % 5 == 0 and cfg["peers"]:                                  # 共享本机导航解（他机 inbox 载荷进入检索）
                    nav_p = body["obs"]["nav"]["pos_enu_m"]
                    act["msgs"] = [{"dst": q, "topic": "nav", "payload": struct.pack("<3d3f", *nav_p, *nav_p)} for q in cfg["peers"]]
                ws.send(T.ACT, act)
            c.close()
        except Exception as e:                                                  # 由用例断言
            self.error = e


def _wire_launch(hosts) -> list[_WireAgent]:
    ags = [_WireAgent(h.path) for h in hosts.values()]
    for a in ags:
        a.start()
    return ags


def _accepting(verdict: dict, known=KNOWN_FINDINGS) -> list[str]:
    return sorted(k for k, ok in verdict.items() if not ok and k not in known)


# ---------------------------------------------------------------------------------------------------------------- 单元


def test_pair_hits_needs_two_distinct_values_on_two_components() -> None:
    Q = np.array([[12.345, -7.5, 3.25], [1.0, 2.0, np.nan]])
    assert pair_hits(np.array([12.3452, -7.5004]), Q, 1e-3).tolist() == [True, False]           # 两个取值 → 两个分量
    assert pair_hits(np.array([-7.5004, 99.0, 12.3452]), Q, 1e-3).tolist() == [True, False]     # 与次序无关
    assert not pair_hits(np.array([12.3452, 50.0]), Q, 1e-3).any()                              # 只有一个分量吻合
    assert not pair_hits(np.array([12.345, 12.345 + 2e-3]), Q, 1e-3).any()                      # 超出容差
    assert pair_hits(np.array([1.0, 2.0]), Q, 1e-3).tolist() == [False, True]                   # NaN 分量不参与
    assert not pair_hits(np.array([5.0, 5.0]), np.array([[5.0, 6.0]]), 1e-3).any()               # 同一分量吻合两次不算
    assert pair_hits(np.array([5.0, 5.0]), np.array([[5.0, 5.0]]), 1e-3).all()                   # 两个取值、两个分量


def test_statistics() -> None:
    assert binom_sf(0, 10, 0.5) == 1.0 and abs(binom_sf(10, 10, 0.5) - 2**-10) < 1e-15
    assert abs(binom_sf(3, 5, 0.5) - 0.5) < 1e-12 and binom_sf(6, 5, 0.5) == 0.0
    assert abs(poisson_sf(1, 2.0) - (1 - np.exp(-2.0))) < 1e-12 and poisson_sf(0, 0.0) == 1.0 and poisson_sf(1, 0.0) == 0.0


def test_secrets_tokens_and_derived_keys() -> None:
    seed = 0x1234_5678_9ABC_DEF0
    dk = derived_keys(seed)
    k1 = cbrng.sm(cbrng.u64(seed))
    assert dk["k1"] == int(k1) and dk["stream22"] == int(cbrng.sm(k1 ^ cbrng.u64(22))) and len(dk) == 65
    s = Secrets(run_seed=seed, run_id="run-abcdef012345", event_id="ev", actor_ids=["person_a", "bg_1"], hashes={"x": "ab" * 32}, derived=dk)
    tok, short = s.tokens()
    assert tok[seed.to_bytes(8, "little")] == "seed" and tok[seed.to_bytes(8, "big")] == "seed" and tok[str(seed).encode()] == "seed"
    assert tok[f"{dk['stream21']:016x}".encode()] == "derived_key" and tok[bytes.fromhex("ab" * 32)] == "hash"
    assert tok[b"person_a"] == "actor_id" and b"bg_1" not in tok and b"ev" not in tok
    assert short == {"actor_id": 1, "event_id": 1, "split": 2}
    st = s.strings()
    assert st["bg_1"] == "actor_id" and st["dev"] == "split" and st[f"{seed:x}"] == "seed" and st["AB" * 32] == "hash"
    ints = s.integers()
    assert ints[seed] == "seed" and ints[dk["k1"] - (1 << 64) if dk["k1"] >= 1 << 63 else dk["k1"]] == "seed"


def test_jpeg_markers_and_capture_roundtrip(tmp_path: Path) -> None:
    from test_obs_audit import _body, _jpeg, _obs

    assert jpeg_markers(_jpeg())[:2] == [0xE0, 0xDB] and 0xFE in jpeg_markers(_jpeg(b"\xff\xfe\x00\x04hi"))
    cap = CaptureAudit(out_dir=tmp_path / "cap")
    good = _obs()
    cap.check_body(T.OBS, _body(T.OBS, good), uav="uav-01")
    cap.check_body(T.ERROR, _body(T.ERROR, {"code": 666, "why": "k_mismatch"}), uav="uav-01")
    cap.check_body(T.CLOSE, _body(T.CLOSE, {"reason": "run_ended"}), uav="uav-01")
    bad = _obs()
    bad["obs"]["nav"]["true_pos_enu_m"] = [1.0, 2.0, 3.0]
    with pytest.raises(ObsSchemaViolation):
        cap.check_body(T.OBS, _body(T.OBS, bad), uav="uav-02")                  # 违规帧不发送，也不录制
    cap.close()
    back = list(read_capture(tmp_path / "cap"))
    assert [(f.uav, f.kind) for f in back] == [(f.uav, f.kind) for f in cap.frames] == [("uav-01", k) for k in ("obs", "error", "close")]
    assert [f.body for f in back] == [f.body for f in cap.frames] and cap.summary()["schema_checked"] == 4
    assert cap.capture_summary() == {"frames": 3, "bytes": sum(len(f.body) for f in back), "by_kind": {"close": 1, "error": 1, "obs": 1}}
    assert not (tmp_path / "cap" / "uav-02.wire").exists()


def test_probe_does_not_change_observations(synth_event, tmp_path: Path) -> None:
    """ProbedEnv 与 BenchEnv 在同一规格与同一动作序列下交付逐字节相同的观测（obs.sha256 一致）；探针记录每 tick 与 250 Hz 历史。"""
    w, ev = synth_event
    fleet = (FleetEntry("uav-01", (-10.0, 50.0, 30.0), 0.0, airborne=True),)
    digests, envs = [], []
    for cls, name in ((BenchEnv, "plain"), (ProbedEnv, "probed")):
        env = cls(EnvSpec("t-probe", 7, w, ev, fleet, physics="kinematic", duration_s=5.0, out_dir=tmp_path / name))
        obs, _ = env.reset()
        while env.agents:
            obs, *_ = env.step({u: {"schema": "drone4d.bench.act.v1", "k": o["obs"]["k"],
                                    "nav": {"mode": "goto", "pos_enu_m": [0.0, 60.0, 30.0], "speed_mps": 4.0},
                                    "gimbal": {"sensor_id": env.cam.sensor_id, "mode": "angles", "pitch_deg": -30.0 - o["obs"]["k"] % 20,
                                               "yaw_deg": 5.0}} for u, o in obs.items()})
        m = env.close()
        digests.append((tmp_path / name / "agents/uav-01/obs.sha256").read_text())
        envs.append((env, m))
    assert digests[0] == digests[1] and digests[0]
    env, m = envs[1]
    assert len(env.probe_t) == len(env.probe_rows["uav-01"]) == len(env.probe_gim["uav-01"]) == m["n_ticks"] + 1
    assert len(env.probe_hist["uav-01"]) > 10 * m["n_ticks"]                                      # 250 Hz 历史
    assert len(set(env.probe_gim["uav-01"])) > 5                                          # 云台转动被记录


# ---------------------------------------------------------------------------------------------------------------- 排名运行：0 命中


@pytest.fixture(scope="module")
def l1_ranked(synth_event, tmp_path_factory):
    """L1 排名运行：3 机 × 30 s，真实 SDK 基线 track_share（子进程，经 Unix 套接字）。"""
    out = tmp_path_factory.mktemp("rt-l1")
    spec = _ranked_spec(synth_event, out / "run", "l1-ranked", obs_level="L1", fleet=L1_FLEET, dur=30.0)
    run = record_socket_episode(ProbedEnv(spec), baseline_launcher("track_share", SDK), capture_dir=out / "capture")
    rep = leak_report(run, config={"case": "L1 ranked track_share"})
    write_report(rep, out / "run")
    return run, rep, out


@pytest.fixture(scope="module")
def l2_ranked(synth_event, tmp_path_factory):
    """L2 排名运行：2 机 × 6 s，相机 JPEG（CPU 替身）+ LiDAR + IMU + GNSS，线协议脚本 Agent（goto 与云台扫动）。"""
    pytest.importorskip("PIL")
    out = tmp_path_factory.mktemp("rt-l2")
    spec = _ranked_spec(synth_event, out / "run", "l2-ranked", obs_level="L2", fleet=L2_FLEET, dur=6.0)
    run = record_socket_episode(ProbedEnv(spec, l2=_l2_factory()), _wire_launch)
    rep = leak_report(run, config={"case": "L2 ranked scripted"})
    write_report(rep, out / "run")
    return run, rep, out


def test_l1_ranked_run_has_zero_hits(l1_ranked) -> None:
    run, rep, out = l1_ranked
    cap = run.capture
    a = cap.summary()
    assert all(s["protocol_errors"] == 0 for s in run.stats.values())
    assert cap.n_frames == a["schema_checked"] == sum(rep["config"]["frames"].values()) and a["passed"]       # 录到全部下发帧
    assert rep["config"]["frames"]["config"] == 3 and rep["config"]["frames"]["close"] == 3 and rep["config"]["frames"]["obs"] >= 3 * 290
    back = list(read_capture(out / "capture"))                                          # 落盘录制与内存录制逐帧一致
    assert len(back) == cap.n_frames and sorted(f.body for f in back) == sorted(f.body for f in cap.frames)
    assert _accepting(rep["verdict"]) == [], json.dumps({k: rep["r1"][k] for k in ("vector", "actors", "strings", "integers")})[:3000]
    r1, r2 = rep["r1"], rep["r2"]
    assert r1["vector"]["by_class"].get("own_position:own_pose/position", 0) == 0 and r1["strings"]["hits"] == 0 and r1["integers"]["hits"] == 0
    assert r2["tokens"]["hits"] == 0 and r2["tokens"]["n_tokens"] > 300
    assert all(r2["numeric"]["by_class"].get(c, 0) == 0 for c in r2["numeric"]["by_class"] if not c.startswith("own_attitude"))
    assert r1["actors"]["windows"] == rep["config"]["frames"]["obs"] + 6 and r1["actors"]["baseline_windows"] > 0
    assert r1["actor_no"]["trials"] > 50 and r1["actors"]["p_value"] >= 0.01 and r1["actor_no"]["p_value"] >= 0.01
    assert rep["config"]["numeric_bytes_searched"] == rep["config"]["bytes"]            # L1 无图像载荷
    # 报告正文不含真值与标识
    txt = (out / "run" / "leak_audit_report.json").read_text()
    spec = run.env.spec
    assert str(spec.seed) not in txt and f"{spec.seed:x}" not in txt and spec.run_id not in txt
    assert all(a_["id"] not in txt for a_ in run.env.tape.actors if len(a_["id"]) >= 6)


def test_l2_ranked_run_has_zero_hits(l2_ranked) -> None:
    run, rep, _out = l2_ranked
    assert all(s["protocol_errors"] == 0 and s["acts"] > 50 for s in run.stats.values())
    n_inbox = sum(len(f.doc["inbox"]) for f in run.capture.frames if f.kind == "obs")
    assert n_inbox >= 15                                                                # 他机消息（inbox 载荷）在检索范围内
    assert _accepting(rep["verdict"]) == [], json.dumps({k: rep["r1"][k] for k in ("vector", "actors", "gimbal")})[:3000]
    r2 = rep["r2"]
    assert r2["jpeg"]["frames"] == rep["config"]["frames"]["obs"] and r2["jpeg"]["violations"] == 0
    assert set(r2["jpeg"]["markers"]) == {"0xE0"}                                       # 只有 APP0（JFIF），无其他 APPn 与 COM
    assert r2["lidar_tag"]["frames"] > 0 and r2["lidar_tag"]["violations"] == 0
    assert rep["config"]["image_bytes"] > 0 and rep["config"]["numeric_bytes_searched"] < rep["config"]["bytes"]
    g = rep["r1"]["gimbal"]
    assert g["encoder_sigma_deg"] == pytest.approx(0.05) and g["trials"] >= rep["config"]["frames"]["obs"] - 2 and g["p_value"] >= 0.01
    assert r2["tokens"]["hits"] == 0 and rep["r1"]["strings"]["hits"] == 0


def test_l2_depth_ranked_run_has_zero_hits(synth_event, tmp_path: Path) -> None:
    """L2-depth：深度图（f32le）按图像载荷不做数值窗口检索，其余同 L2。"""
    pytest.importorskip("PIL")
    spec = _ranked_spec(synth_event, tmp_path / "run", "l2-depth-ranked", obs_level="L2-depth", fleet=L2_FLEET[:1], dur=3.0)
    run = record_socket_episode(ProbedEnv(spec, l2=_l2_factory(lidar=False)), _wire_launch)
    rep = leak_report(run)
    n_obs = rep["config"]["frames"]["obs"]
    depth = sum(1 for f in run.capture.frames if f.kind == "obs" for x in f.doc["obs"]["sensors"].get("eo", []) if "depth" in x)
    assert depth == n_obs >= 25 and rep["config"]["image_bytes"] > depth * 1280 * 720 * 4
    assert _accepting(rep["verdict"]) == [] and rep["r2"]["jpeg"]["violations"] == 0


def test_container_path_records_every_frame(synth_event, tmp_path: Path, monkeypatch, request) -> None:
    """容器路径（`run_container_episode`，docker 替身按子进程启动 `drone4d.serve`）同样经白名单审计录制全部下发帧，检索 0 命中。"""
    import shutil
    import tempfile

    from awr.bench.redteam import record_container_episode

    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "docker-state"))
    monkeypatch.setenv("PYTHONPATH", f"{SDK}:{ROOT / 'python'}")
    sock = Path(tempfile.mkdtemp(prefix="d3rc", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(sock, ignore_errors=True))
    monkeypatch.setenv("AWR_AGENT_SOCK_ROOT", str(sock))
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE.parents[0] / 'env' / 'fake_docker.py'} \"$@\"\n")
    docker.chmod(0o755)
    spec = _ranked_spec(synth_event, tmp_path / "run", "l1-container", obs_level="L1", fleet=L1_FLEET[:2], dur=8.0)
    run = record_container_episode(ProbedEnv(spec), tmp_path / "run", "d3-agent-py:dev", capture_dir=tmp_path / "capture",
                                   cmd=["python", "-m", "drone4d.serve", "--baseline", "static_guard"], docker=str(docker), step_budget_s=30.0)
    rep = leak_report(run)
    assert all(st["exit_code"] == 0 and st["protocol_errors"] == 0 for st in run.stats.values())
    assert run.capture.n_frames == run.capture.summary()["schema_checked"] == sum(1 for _ in read_capture(tmp_path / "capture")) >= 2 * 80
    assert _accepting(rep["verdict"]) == [] and rep["r2"]["tokens"]["hits"] == 0


@pytest.mark.xfail(strict=True, reason="已知缺陷：NavSolution.q_wb_xyzw 取真值姿态（M26-FR-040 姿态误差未落地）")
def test_nav_attitude_is_not_truth(l2_ranked) -> None:
    _run, rep, _out = l2_ranked
    assert rep["verdict"]["r1.own_attitude"] and rep["verdict"]["r2.own_attitude"]


def test_known_attitude_finding_is_detected(l2_ranked) -> None:
    """已知缺陷的检出形态：偏航后姿态非平凡，命中只在 `obs/nav/q_wb_xyzw`（R1）与其 float64 字节（R2）。"""
    _run, rep, _out = l2_ranked
    vec = rep["r1"]["vector"]
    paths = {p for c, ps in vec["paths"].items() if c.startswith("own_attitude:") for p in ps}
    assert paths == {"obs/nav/q_wb_xyzw"} and vec["by_class"]["own_attitude:own_pose/attitude_q"] >= 20
    assert rep["r2"]["numeric"]["paths"]["own_attitude:own_pose/attitude_q"].get("f64be", 0) >= 20
    assert rep["failed"] == ["r1.own_attitude", "r2.own_attitude"]


# ---------------------------------------------------------------------------------------------------------------- 正向对照：注入真值必须检出


def _leaky_run(synth_event, tmp_path: Path, monkeypatch, *, obs_level: str = "L1", dur: float = 8.0, fleet=None):
    spec = _ranked_spec(synth_event, tmp_path / "run", f"leak-{tmp_path.name}", obs_level=obs_level, fleet=fleet or L2_FLEET[:1], dur=dur)
    env = ProbedEnv(spec, l2=_l2_factory(lidar=False) if obs_level != "L1" else None)
    run = record_socket_episode(env, _wire_launch)
    return run, leak_report(run)


def test_injected_own_pose_and_extrinsics_are_detected(synth_event, tmp_path: Path, monkeypatch) -> None:
    orig = BenchEnv._observe

    def leaky(self, final=False):
        out = orig(self, final)
        for u, msg in out.items():
            row = self._row(u)
            if msg["obs"]["k"] % 10 == 3:
                msg["obs"]["ground_hint"] = {"dsm_m": float(row[0]), "dtm_m": float(row[1])}           # 本机真值 x、y
            if msg["obs"]["k"] % 10 == 6:
                mt = self.cam.mount_t_bs_m
                yaw = np.arctan2(2 * (row[6] * row[5] + row[3] * row[4]), 1 - 2 * (row[4] ** 2 + row[5] ** 2))
                o = row[0:3] + np.array([np.cos(yaw) * mt[0] - np.sin(yaw) * mt[1], np.sin(yaw) * mt[0] + np.cos(yaw) * mt[1], mt[2]])
                msg["obs"]["nav"]["vel_enu_mps"] = [float(o[0]), float(o[1]), float(o[2])]           # 相机真值光心
        return out

    monkeypatch.setattr(BenchEnv, "_observe", leaky)
    _run, rep = _leaky_run(synth_event, tmp_path, monkeypatch)
    vec = rep["r1"]["vector"]["by_class"]
    n = rep["config"]["frames"]["obs"]
    assert vec["own_position:own_pose/position"] >= n // 10 - 1
    assert sum(v for c, v in vec.items() if c.startswith("extrinsics:")) >= n // 10 - 1
    assert rep["r1"]["vector"]["paths"]["own_position:own_pose/position"].get("obs/ground_hint", 0) >= n // 10 - 1
    num = rep["r2"]["numeric"]["by_class"]
    assert num["own_position:own_pose/position"] >= n // 10 - 1 and not rep["verdict"]["r1.own_position"] and not rep["verdict"]["r2.own_position"]
    assert not rep["verdict"]["r1.extrinsics"] and not rep["verdict"]["r2.extrinsics"]


def test_injected_actor_positions_are_detected(synth_event, tmp_path: Path, monkeypatch) -> None:
    """L1 检测的地面位置换成演员真值 → R1 演员命中显著高于 ±60 s 基线，R2 float64 窗口检出。"""
    orig = L1Model.frame

    def leaky(self, uav_no, seq, t_ns, o, axes, e_nav, env):
        dets, truth = orig(self, uav_no, seq, t_ns, o, axes, e_nav, env)
        st = self.tape.actor_states(t_ns)
        for d, tr in zip(dets, truth, strict=True):
            i = self.ids.index(tr["actor"])
            d["ground_xy_noisy"] = [float(st.pos_enu_m[i, 0]), float(st.pos_enu_m[i, 1])]
        return dets, truth

    monkeypatch.setattr(L1Model, "frame", leaky)
    fleet = (FleetEntry("uav-01", (0.0, 45.0, 40.0), 0.0, airborne=True),)
    _run, rep = _leaky_run(synth_event, tmp_path, monkeypatch, fleet=fleet, dur=20.0)
    act, n_det = rep["r1"]["actors"], rep["r1"]["actor_no"]["trials"]
    # 坐标恰为 0 的分量按平凡取值不参与（合成世界的 x = 0 锚点上的演员只剩一个分量，不计命中），故命中数略少于检测数
    assert n_det >= 20 and act["hits"] >= 15 and act["baseline_hits"] <= 1
    assert act["p_value"] < 1e-4 and not rep["verdict"]["r1.actors"]
    assert rep["r1"]["vector"]["paths"]["actors"].get("obs/sensors/detections/*/ground_xy_noisy", 0) >= 15
    assert rep["r2"]["numeric"]["by_class"].get("actors", 0) > 0 and not rep["verdict"]["r2.actors"]


def test_injected_identifiers_and_bytes_are_detected(synth_event, tmp_path: Path, monkeypatch) -> None:
    """种子十进制文本进字符串字段、种子 u64 与磁带哈希原始字节进 inbox 载荷、演员 id 进话题、本机真值 float32 对进载荷。"""
    orig = BenchEnv._observe

    def leaky(self, final=False):
        out = orig(self, final)
        h = bytes.fromhex(self.tape.hashes()["content_sha256"])
        for u, msg in out.items():
            k = msg["obs"]["k"]
            if k == 4:
                msg["obs"]["status"]["locked_sensors"] = [str(self.spec.seed)]
            if k == 5:
                row = self._row(u)
                pay = b"\x00" + self.spec.seed.to_bytes(8, "little") + h + struct.pack("<ff", row[0], row[1])
                msg["inbox"].append({"src": "uav-09", "seq": 0, "topic": self.tape.actors[0]["id"], "t_sent_ns": k * TICK_NS,
                                     "t_recv_ns": k * TICK_NS, "payload": pay})
        return out

    monkeypatch.setattr(BenchEnv, "_observe", leaky)
    _run, rep = _leaky_run(synth_event, tmp_path, monkeypatch)
    s, tk, num = rep["r1"]["strings"]["by_class"], rep["r2"]["tokens"]["by_class"], rep["r2"]["numeric"]["by_class"]
    assert s.get("seed", 0) >= 1 and s.get("actor_id", 0) >= 1
    assert rep["r1"]["strings"]["paths"]["seed"] == {"obs/status/locked_sensors/*": 1}
    assert tk.get("seed", 0) >= 2 and tk.get("hash", 0) >= 1 and tk.get("actor_id", 0) >= 1          # 文本与 u64 两次（两帧）
    assert any(c.startswith("own_position") for c in num) and rep["r2"]["numeric"]["paths"]["own_position:own_pose/position"].get("f32le_pair")
    for k in ("r1.strings", "r2.tokens", "r2.own_position"):
        assert not rep["verdict"][k]


def test_injected_gimbal_truth_and_actor_numbers_are_detected(synth_event, tmp_path: Path, monkeypatch) -> None:
    """L2 云台读数换成真值角 → 命中显著高于编码器噪声期望；L1 外观分量写入演员序号 → 显著高于相邻序号基线。"""
    pytest.importorskip("PIL")
    from awr.agentsdk.env import l2 as l2mod

    orig_fc = l2mod.L2Sensors._finish_camera

    def leaky_cam(self, uav, k, out, meta):
        fr, tr = orig_fc(self, uav, k, out, meta)
        az, el = self.track[uav].gimbal_at(fr["t_mid_ns"])
        fr["gimbal_reading"] = {"roll_deg": 0.0, "pitch_deg": float(np.degrees(el)), "yaw_deg": float(np.degrees(az))}
        return fr, tr

    monkeypatch.setattr(l2mod.L2Sensors, "_finish_camera", leaky_cam)
    _run, rep = _leaky_run(synth_event, tmp_path / "g", monkeypatch, obs_level="L2", dur=5.0)
    g = rep["r1"]["gimbal"]
    assert g["hits"] >= 30 and g["p_value"] < 1e-6 and not rep["verdict"]["r1.gimbal"]
    monkeypatch.undo()

    orig = L1Model.frame

    def leaky(self, uav_no, seq, t_ns, o, axes, e_nav, env):
        dets, truth = orig(self, uav_no, seq, t_ns, o, axes, e_nav, env)
        for d, tr in zip(dets, truth, strict=True):
            d["appearance"] = {"top_rgb": [self.ids.index(tr["actor"]), 128, 128]}
        return dets, truth

    monkeypatch.setattr(L1Model, "frame", leaky)
    fleet = (FleetEntry("uav-01", (0.0, 45.0, 40.0), 0.0, airborne=True),)
    _run, rep = _leaky_run(synth_event, tmp_path / "n", monkeypatch, fleet=fleet, dur=20.0)
    an = rep["r1"]["actor_no"]
    assert an["hits"] == an["trials"] >= 20 and an["p_value"] < 1e-4 and not rep["verdict"]["r1.actor_no"]


def test_jpeg_comment_segment_is_reported() -> None:
    """图像载荷只查段白名单：COM 段与 APP1 计为违规（在线审计以 644 拦截，离线报告同样计数）。"""
    from test_obs_audit import _body, _jpeg, _obs

    o = _obs()
    o["obs"]["sensors"]["cam0"][0]["rgb"]["data"] = _jpeg(b"\xff\xfe\x00\x06seed")
    fr = CapturedFrame("uav-01", T.OBS, _body(T.OBS, o), 0)
    truth = TruthSet.__new__(TruthSet)
    truth.secrets = Secrets(run_seed=3, run_id="run-x", event_id="ev", actor_ids=[], hashes={}, derived={})
    truth.own, truth.det_actor, truth.gimbal_sigma_deg = {}, {}, None
    truth.n_actors, truth.tape_t_max_ns, truth.duration_s, truth.obs_level, truth.noise = 0, -1, 0.0, "L2", "realistic"
    rep = audit_frames([fr], truth)
    assert rep["r2"]["jpeg"] == {"frames": 1, "violations": 1, "markers": {"0xE0": 1, "0xFE": 1}}
    assert not rep["verdict"]["r2.jpeg_segments"] and rep["verdict"]["r2.lidar_tag"]
