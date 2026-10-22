"""3 机 × 1080p @ 10 Hz 在线输出与 1080p 单帧体积（D3-AC-60 的 1080p 条款；docs/05 §5.4.3"1080p 多机输出与编码"、ADR-179；
M25-FR-060、183，M25 §6.7、§15.5；M27 §7.5.1"存档的正是 Agent 收到的字节"）。

验收原文（docs/05 §11.2 D3-AC-60，本文件覆盖的部分）："3 机 × 1080p @ 10 Hz 时 RGB 经 GPU nvJPEG 编码，在线路径无 CPU 编码
调用，Agent 收到的字节与存档字节 sha256 一致；1080p 单帧体积实测写入实现报告"。卷帘几何、`t_mid_ns` 与 `readout_ns` 条款见
test_rolling_shutter.py，本文件只复核交付值。用例对应：

- 3 架 x500 在正式世界包 oryahovo-crop1 上以 `rgb1080-rs` L2 观测（Fast 物理档、参考策略 coverage_scan）运行 BenchEnv，整个运行
  （reset、各步与 close）都在 `online_path()` 作用域内：每 tick 每机恰好一帧（10 Hz：seq 连续、t_mid 间隔 100 ms，1920 × 1080，
  readout_ns = 20,000,000）；L2 记录的编码器身份为 nvImageCodec（nvJPEG）q90 4:2:0，抽样帧用新建的 GPU 编码器重编码后逐字节相同，
  与 CPU 回退的字节不同；`cpu_encode_calls()` 增量为 0；每次观测都经宿主线协议编码（`awr.agentsdk.wire.encode_frame`，
  并按线上字节做下发审计），在 Unix 套接字对的另一端由 SDK 的接收函数（`drone4d.wire._recv`）解码，得到 Agent 收到的 JPEG。
  其 sha256 与 `raw.mcap` 中的存档字节（经 `frames.idx` 由 FrameStore 读出）、帧索引条目逐帧一致：
  `test_1080p_three_uav_online_end_to_end`；
- 1080p 单帧体积：沿用同一产品路径（L2Sensors 渲染、效果链、自动曝光与 nvJPEG），在固定视点网格（离地 40/80/150/250 m ×
  俯仰 −90°/−45°/−30° × 3 个方位）与 clean/realistic/harsh 三个噪声档上统计 JPEG 字节数，连同上一用例的运行分布写入实测记录
  （`AWR_D3_1080P_OUT` 目录下的 JSON；docs/impl/D3-相机实测记录.md）：`test_1080p_frame_size_viewpoints`；
- CPU 部分（本机可跑）：L2 相机按档位选择编码器，`rgb1080-rs` 不回退 CPU（`AWR_JPEG_ENCODER=cpu` 时抛 `CpuEncodeForbidden`，
  无 GPU 时抛 `GpuEncoderUnavailable`）：`test_l2_1080p_never_falls_back_to_cpu`；720p 档 3 机运行（CPU 替身渲染）经线协议与
  raw.mcap 往返后逐帧 sha256 一致：`test_l2_delivered_bytes_round_trip_raw_mcap`。

GPU 用例需要 moderngl（EGL）、nvImageCodec 与 CUDA，在 dmax 的 d3-sensorsvc 镜像内运行，单卡绑定。体积与字节属内容口径（同镜像、
同型号 GPU 下逐字节确定），不属 R-D3-63 的性能类条款；用例打印的墙钟、编码时延与 RTF 只作开发参考，正式吞吐由 D3-AC-11 判定
（S09、S21，独占卡）。
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from awr.sensorsvc.camera import encode as E

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE.parent / "world"))
sys.path.insert(0, str(HERE.parent / "events"))
sys.path.insert(0, str(ROOT / "tools" / "sdk" / "drone4d" / "src"))

PROFILE = "rgb1080-rs"
W, H = 1920, 1080
READOUT_NS = 20_000_000
TICK_NS = 100_000_000
CAM = "eo"
N_UAV = 3
WORLD, EVENT = "oryahovo-crop1", "crop-handoff-001"
PADS = ((76.0, -78.0), (76.0, -82.0), (76.0, -86.0))               # 与 test_multiview 同一组起降点（演员群附近空地）
PAD_YAW = math.pi / 2
SEED = 11
DURATION_S = float(os.environ.get("AWR_D3_1080P_S", "30"))         # 参考策略约 15 s 爬升到巡航高度，此后为扫描段
RECORD_DIR = os.environ.get("AWR_D3_1080P_OUT")
REENCODE_EVERY = 50                                                  # 每 50 tick 抽 1 个 tick（3 帧）做重编码对拍
NVJPEG_ID = re.compile(r"nvimgcodec-[\d.]+/jpeg/q90/420")
AGL_M = (40.0, 80.0, 150.0, 250.0)
PITCH_DEG = (-90.0, -45.0, -30.0)
HEADING_DEG = (0.0, 120.0, 240.0)
TIERS = ("clean", "realistic", "harsh")
VP_FRAMES, VP_SETTLE = 8, 4                                          # 每视点 8 帧：前 4 帧供自动曝光收敛，统计后 4 帧
AGL_BANDS = ((-1e9, 10.0, "ground"), (10.0, 60.0, "10-60m"), (60.0, 120.0, "60-120m"), (120.0, 1e9, ">=120m"))


# ---------------------------------------------------------------- 工具
def size_stats(sizes) -> dict:
    """JPEG 字节数统计（1 kB = 1000 B）。"""
    a = np.asarray(list(sizes), np.int64)
    if not len(a):
        return {"n": 0}
    kb = lambda v: round(float(v) / 1000.0, 1)                       # noqa: E731
    return {"n": len(a), "mean_kB": kb(a.mean()), "p50_kB": kb(np.percentile(a, 50)), "p95_kB": kb(np.percentile(a, 95)),
            "min_kB": kb(a.min()), "max_kB": kb(a.max()), "total_MB": round(float(a.sum()) / 1e6, 2)}


def pct_ms(xs) -> dict:
    a = np.asarray(list(xs), np.float64)
    if not len(a):
        return {"n": 0}
    return {"n": len(a), "p50_ms": round(float(np.percentile(a, 50)), 2), "p99_ms": round(float(np.percentile(a, 99)), 2),
            "max_ms": round(float(a.max()), 2)}


def write_record(name: str, obj: dict) -> Path | None:
    """实测记录 JSON 写入 `AWR_D3_1080P_OUT`（未设置时只打印）。"""
    print(f"[S08] {name}: {json.dumps(obj, ensure_ascii=False)[:4000]}")
    if not RECORD_DIR:
        return None
    p = Path(RECORD_DIR) / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return p


class WireTap:
    """Agent 侧实际收到的观测：宿主按线协议编码 OBS 帧并按线上字节审计（与 `SocketAgentHost._send` 相同），经 Unix 套接字对
    发送，另一端由 SDK 的接收函数 `drone4d.wire._recv` 解码。"""

    def __init__(self) -> None:
        from awr.agentsdk.env.obs_audit import ObsAudit

        self.audit = ObsAudit()
        self.a, self.b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.frames = 0
        self.wire_bytes = 0

    def deliver(self, uav: str, msg: dict) -> dict:
        from drone4d.wire import T as SdkT
        from drone4d.wire import _recv

        from awr.agentsdk.wire import T, encode_frame

        frame = encode_frame(T.OBS, {"obs": msg["obs"], "inbox": msg["inbox"]})
        self.audit.check_body(T.OBS, frame[8:], uav=uav)
        th = threading.Thread(target=self.a.sendall, args=(frame,), daemon=True)   # 帧大于套接字缓冲区，发送与接收并行
        th.start()
        t, body = _recv(self.b)
        th.join(30)
        assert t == SdkT.OBS
        self.frames += 1
        self.wire_bytes += len(frame)
        return body

    def close(self) -> None:
        self.a.close()
        self.b.close()


def archived_frames(run_dir: Path, uavs, sensor: str = CAM) -> dict[tuple[str, int], tuple[str, int, int]]:
    """raw.mcap 的存档帧：(uav, seq) → (sha256, t_ns, 字节数)，经 frames.idx 由 FrameStore 读出；同时核对帧索引分页的 sha256。"""
    from awr.bench.recorder.read import FrameStore

    fs = FrameStore(run_dir)
    out: dict[tuple[str, int], tuple[str, int, int]] = {}
    try:
        for u in uavs:
            info = fs.channel_info(u, sensor)
            assert info is not None and info.get("codec") == "jpeg" and info.get("agent_delivered") == "true", info
            seq = 0
            while seq is not None:
                page = fs.index_page(u, sensor, seq)
                for it in page["items"]:
                    f = fs.frame(u, sensor, it["seq"])
                    assert f is not None and f.codec == "jpeg" and f.sha256 == it["sha256"] and len(f.payload) == it["bytes"]
                    out[(u, f.seq)] = (f.sha256, f.t_ns, len(f.payload))
                seq = page["next_seq"]
    finally:
        fs.close()
    return out


def _stub_tape():
    ids = np.zeros(0, np.int64)
    st = SimpleNamespace(ids=ids, state=np.zeros(0, np.uint8), pos_enu_m=np.zeros((0, 3)), yaw_rad=np.zeros(0))
    return SimpleNamespace(actors=[], path=Path("."), actor_states=lambda t_ns, ids=None: st)


def _static_state(pos, yaw: float = 0.0) -> np.ndarray:
    s = np.zeros(16)
    s[0:3] = pos
    s[3:7] = (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))
    return s


class FakeRaster:
    """EGL 光栅相机的 CPU 替身（与 tests/d3/env/test_l2 同式）：按位姿生成确定性渐变图，附逐帧变化的纹理，使 JPEG 字节随帧不同。"""

    def __init__(self) -> None:
        self.n = 0

    def render(self, cm, R_ws, p_w, *, fx=None, rs_nodes=None, actors=None, linear=False):
        from awr.sensorsvc.camera.raster import RenderOut

        h, w = cm.height, cm.width
        self.n += 1
        v = int(abs(float(p_w[0]) * 7 + float(p_w[1]) * 3)) % 200
        x = np.arange(w, dtype=np.int64)[None, :]
        y = np.arange(h, dtype=np.int64)[:, None]
        g = ((x * 255 // w + v + ((x * 31 + y * 17 + self.n * 13) % 23)) % 255).astype(np.uint8)
        rgb = np.ascontiguousarray(np.broadcast_to(g[:, :, None], (h, w, 3)))
        z16, z8 = np.zeros((h, w), np.uint16), np.zeros((h, w), np.uint8)
        return RenderOut(rgb, np.zeros((h, w), np.float32), z16, z8, 0.0, 0.0, 0, instance_unocc=z16)


# ---------------------------------------------------------------- CPU 部分
def test_size_stats_and_percentiles() -> None:
    s = size_stats([500_000, 600_000, 700_000, 800_000])
    assert s["n"] == 4 and s["mean_kB"] == 650.0 and s["min_kB"] == 500.0 and s["max_kB"] == 800.0 and s["total_MB"] == 2.6
    assert size_stats([]) == {"n": 0}
    p = pct_ms([1.0] * 99 + [10.0])
    assert p["p50_ms"] == 1.0 and p["max_ms"] == 10.0


def _l2_unit(profile: str, renderer, uavs=("u1",)):
    from awr.agentsdk.env.l2 import L2Config, L2Sensors

    s = L2Sensors(world_dir=Path("."), tape=_stub_tape(), uavs=list(uavs), seed=3, noise="realistic", obs_level="L2", nav=None,
                  mount_t_bs_m=(0.12, 0.0, -0.08), hfov_deg=60.0, cfg=L2Config(camera_profile=profile, lidar=False, imu=False, gnss=False),
                  renderer=renderer)
    for i, u in enumerate(uavs):
        st = _static_state((10.0 * i, 0.0, 50.0))
        s.ingest(u, np.array([0, 2 * TICK_NS], np.int64), np.stack([st, st]))
        s.gimbal(u, TICK_NS, 0.0, -45.0)
    return s


def test_l2_1080p_never_falls_back_to_cpu(monkeypatch) -> None:
    """`rgb1080-rs` 的 L2 相机只用 GPU 编码：强制 CPU 时抛 `CpuEncodeForbidden`；无 GPU 时抛 `GpuEncoderUnavailable`，不静默回退。"""
    pytest.importorskip("PIL")
    s = _l2_unit(PROFILE, FakeRaster())
    n0 = E.cpu_encode_calls()
    monkeypatch.setenv(E.ENV_ENCODER, "cpu")
    with pytest.raises(E.CpuEncodeForbidden):
        s.sense_all(1, [("u1", 0)])
    assert E.cpu_encode_calls() == n0                                 # 选择阶段即拒绝，未进入 CPU 编码
    monkeypatch.setenv(E.ENV_ENCODER, "auto")
    if E.gpu_encoder_available():
        res = s.sense_all(1, [("u1", 0)])
        f = res["u1"][0][CAM][0]
        assert NVJPEG_ID.fullmatch(s.encoder) and E.jpeg_subsampling(f["rgb"]["data"]) == "420"
        assert f["width"] == W and f["height"] == H and f["readout_ns"] == READOUT_NS
    else:
        with pytest.raises(E.GpuEncoderUnavailable):
            s.sense_all(1, [("u1", 0)])
    assert E.cpu_encode_calls() == n0
    # 720p 全局快门档在 GPU 不可用时回退 CPU（离线与 CI），在线作用域内同样只允许 GPU
    s7 = _l2_unit("rgb720-gs", FakeRaster())
    f7 = s7.sense_all(1, [("u1", 0)])["u1"][0][CAM][0]
    assert E.jpeg_subsampling(f7["rgb"]["data"]) == "420" and s7.encoder.endswith("/jpeg/q90/420")
    if not E.gpu_encoder_available():
        assert s7.encoder.startswith("pillow-")
        with E.online_path(), pytest.raises(E.GpuEncoderUnavailable):
            s7.sense_all(2, [("u1", 0)])


@pytest.fixture(scope="module")
def synth_event(tmp_path_factory):
    """合成世界上编译的合成交接事件（与 tests/d3/env/conftest 的 `synth_event` 同式：补计分标注与简报，门 3 以 lenient 编译）。"""
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    from synthworld import build_synth_world
    from test_compile import EVENT, OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    tmp = tmp_path_factory.mktemp("s08")
    mp = pytest.MonkeyPatch()
    w = build_synth_world(tmp, mp)
    ev = copy.deepcopy(EVENT)
    ev["annotations"] = [{"id": "meet_ab", "type": "proximity_encounter", "participants": ["person_a", "person_b"], "window": [5, 25]}]
    ev["brief"] = {"public_anchors": ["ev_plaza", "ev_door"],
                   "targets": [{"actor": "person_a", "description_en": "Blue top", "description_zh": "蓝色上衣"}]}
    d = tmp / "ev"
    d.mkdir()
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache", strict=False))
    assert r.status == "compiled", r.as_dict()
    yield w, d / "v1"
    mp.undo()


def test_l2_delivered_bytes_round_trip_raw_mcap(synth_event, tmp_path: Path) -> None:
    """3 机 L2（720p 档，CPU 替身渲染；GPU 可用时同样走 nvJPEG）：Agent 经线协议收到的 JPEG 与 raw.mcap 存档字节逐帧 sha256 一致。"""
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.l2 import L2Config, L2Sensors
    from awr.agentsdk.env.spec import EnvSpec, FleetEntry

    w, ev = synth_event
    fleet = tuple(FleetEntry(f"uav-0{i + 1}", (-10.0 + 10.0 * i, 50.0, 30.0), 0.0, airborne=True) for i in range(N_UAV))
    spec = EnvSpec("t-s08", 3, w, ev, fleet, obs_level="L2", physics="kinematic", duration_s=2.0, out_dir=tmp_path / "run")

    made: list[L2Sensors] = []

    def l2(e):
        made.append(L2Sensors(world_dir=Path(e.spec.world_dir), tape=e.tape, uavs=e.uavs, seed=e.spec.seed, noise=e.spec.noise, obs_level=e.spec.obs_level,
                         nav=e.nav, mount_t_bs_m=e.cam.mount_t_bs_m, hfov_deg=e.cam.hfov_deg, renderer=FakeRaster(),
                         cfg=L2Config(camera_profile="rgb720-gs", lidar=False, imu=False, gnss=False)))
        return made[-1]

    env = BenchEnv(spec, l2=l2)
    tap = WireTap()
    got: dict[tuple[str, int], str] = {}
    try:
        obs, _ = env.reset()
        while True:
            for u, m in obs.items():
                for f in tap.deliver(u, m)["obs"]["sensors"].get(CAM, []):
                    assert (u, f["seq"]) not in got
                    got[(u, f["seq"])] = hashlib.sha256(f["rgb"]["data"]).hexdigest()
            if not env.agents:
                break
            obs, *_ = env.step({u: {"nav": {"kind": "goto", "target_enu": [0.0, 60.0, 30.0], "speed_mps": 4.0},
                                    "gimbal": {"pitch_deg": -45.0, "yaw_deg": 10.0}} for u in obs})
    finally:
        env.close()
        tap.close()
    arch = archived_frames(tmp_path / "run", env.uavs)
    assert len(got) == N_UAV * 21 and set(arch) == set(got)
    assert all(arch[key][0] == h for key, h in got.items())
    assert len(made) == 1 and made[0].encoder.endswith("/jpeg/q90/420")
    assert made[0].encoder.startswith("nvimgcodec-" if E.gpu_encoder_available() else "pillow-")
    assert tap.audit.jpeg_segment_violations == 0


# ---------------------------------------------------------------- GPU 部分：正式世界包上的 3 机在线运行
def _require_gpu_stack() -> None:
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    pytest.importorskip("PIL")
    moderngl = pytest.importorskip("moderngl")
    try:
        from nvidia import nvimgcodec  # noqa: F401
    except ImportError as e:
        pytest.skip(f"[dep] No module named '{getattr(e, 'name', None) or 'nvidia.nvimgcodec'}'")
    if not E.gpu_encoder_available():
        pytest.skip("[gpu] nvImageCodec / CUDA GPU encoder unavailable")
    try:
        ctx = moderngl.create_standalone_context(backend="egl", require=450)
        ctx.release()
    except Exception as e:                                          # 无 EGL 设备
        pytest.skip(f"[gpu] EGL unavailable: {e}")


@pytest.fixture(scope="module")
def crop_event(tmp_path_factory):
    """正式世界包 oryahovo-crop1 与以 lenient 编译的 crop-handoff-001（与 test_multiview 同一事件源；`AWR_D3_1080P_EVENT` 可指定
    已编译的事件版本目录）。"""
    _require_gpu_stack()
    from awr.sim.events.compile import CompileOptions, compile_event

    w = Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds") / WORLD
    if not (w / "visual/mesh/appearance/index.json").is_file():
        pytest.skip(f"[data] needs worlds/{WORLD} (make worlds)")
    tmp = tmp_path_factory.mktemp("s08crop")
    pre = os.environ.get("AWR_D3_1080P_EVENT")
    if pre:
        return w, Path(pre), tmp
    src = ROOT / "configs" / "events.d3" / EVENT / "event.yaml"
    r = compile_event(src, w, opts=CompileOptions(out=tmp / "ev" / "v1", cache_dir=tmp / "cache", strict=False))
    assert r.status == "compiled", r.as_dict()
    return w, tmp / "ev" / "v1", tmp


@dataclass
class _RunProbe:
    sense_s: list[tuple[int, int, float]] = field(default_factory=list)        # (k, 机数, 墙钟 s)
    rgb: dict[tuple[str, int], np.ndarray] = field(default_factory=dict)        # 抽样帧的编码输入
    timing: list[dict] = field(default_factory=list)                            # nvJPEG 票据计时（upload_ms、encode_ms）
    encoders: set[str] = field(default_factory=set)
    agent: dict[tuple[str, int], dict] = field(default_factory=dict)            # Agent 收到的帧：sha256、字节数、t_mid、AGL 等
    wall_s: float = 0.0
    cpu_calls: int = -1
    wire_bytes: int = 0
    run_dir: Path | None = None
    uavs: list[str] = field(default_factory=list)


def _probe_l2(probe: _RunProbe):
    """L2 传感器工厂（与 `BenchEnv._make_l2` 同参数）：记录每 tick 相机路径墙钟、编码器身份与抽样帧的编码输入，不改变输出。"""
    from awr.agentsdk.env.l2 import L2Config, L2Sensors

    class ProbeL2(L2Sensors):
        def sense_all(self, k, items):
            t0 = time.perf_counter()
            r = super().sense_all(k, items)
            probe.sense_s.append((int(k), len(items), time.perf_counter() - t0))
            return r

        def _finish_camera(self, uav, k, out, meta, job=None):
            # BenchEnv 不经 M25 会话时开启 `defer_truth`，产品路径以 `job` 把读回槽位交给真值线程：先复制编码输入，再交产品路径
            if k % REENCODE_EVERY == 0:
                probe.rgb[(uav, int(k))] = np.array(out.rgb8, copy=True)
            obs, rows = super()._finish_camera(uav, k, out, meta, job=job)
            probe.encoders.add(self.encoder)
            return obs, rows

    def factory(env):
        wd = Path(env.spec.world_dir)
        meta = json.loads((wd / "world.json").read_text(encoding="utf-8")) if (wd / "world.json").is_file() else {}
        cfg = L2Config(**{k: v for k, v in (env.spec.l2 or {}).items() if k in ("camera_profile", "jpeg_quality", "lidar", "imu", "gnss")})
        return ProbeL2(world_dir=wd, tape=env.tape, uavs=env.uavs, seed=env.spec.seed, noise=env.spec.noise, obs_level=env.spec.obs_level,
                       nav=env.nav, mount_t_bs_m=env.cam.mount_t_bs_m, hfov_deg=env.cam.hfov_deg, cfg=cfg, world_meta=meta)

    return factory


@pytest.fixture(scope="module")
def crop_run(crop_event) -> _RunProbe:
    """3 机 × rgb1080-rs × 10 Hz 的 BenchEnv 运行（整个运行在 `online_path()` 内），Agent 侧经线协议接收观测。"""
    from drone4d.baselines import BASELINES

    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.spec import EnvSpec, FleetEntry

    w, ev, tmp = crop_event
    fleet = tuple(FleetEntry(f"uav-{i + 1:02d}", (x, y, 0.0), PAD_YAW) for i, (x, y) in enumerate(PADS[:N_UAV]))
    out = tmp / "run"
    spec = EnvSpec("s08-1080p", SEED, w, ev, fleet, obs_level="L2", physics="fast", duration_s=DURATION_S, out_dir=out,
                   l2={"camera_profile": PROFILE, "lidar": False, "imu": False, "gnss": False})
    probe = _RunProbe(run_dir=out)
    tap = WireTap()
    with E.online_path():
        enc = E.shared_encoder(PROFILE, W, H)                       # 与 L2 同一进程常驻实例（同选择结果、分辨率与质量）
        assert enc.kind == "gpu"
        submit = enc.submit

        def timed_submit(rgb8):
            tk = submit(rgb8)
            probe.timing.append(tk.timing)                           # 编码线程原地补全 encode_ms
            return tk

        enc.submit = timed_submit
        cpu0 = E.cpu_encode_calls()
        env = BenchEnv(spec, l2=_probe_l2(probe))
        try:
            t0 = time.perf_counter()
            obs, infos = env.reset()
            ags = {u: BASELINES["coverage_scan"]() for u in env.possible_agents}
            for u, a in ags.items():
                a.reset(infos[u]["config"])
                a.perception = None          # 不跑参考检测器（每帧约 0.18 s CPU）：策略按航线飞行，传感器链路不变
            while True:
                got = {}
                for u, m in obs.items():
                    body = tap.deliver(u, m)
                    o = body["obs"]
                    agl = float(o["nav"]["pos_enu_m"][2]) - float(o["ground_hint"]["dtm_m"])
                    for f in o["sensors"].get(CAM, []):
                        b = f["rgb"]["data"]
                        assert (u, f["seq"]) not in probe.agent
                        probe.agent[(u, int(f["seq"]))] = {
                            "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b), "t_mid_ns": int(f["t_mid_ns"]), "k": int(o["k"]),
                            "readout_ns": int(f["readout_ns"]), "w": int(f["width"]), "h": int(f["height"]), "agl_m": agl,
                            "subsampling": E.jpeg_subsampling(b), "pitch_deg": float(f["gimbal_reading"]["pitch_deg"])}
                    got[u] = body
                if not env.agents:
                    break
                obs, *_ = env.step({u: ags[u].step(b["obs"], b["inbox"]) for u, b in got.items()})
            probe.wall_s = time.perf_counter() - t0
        finally:
            env.close()
            enc.submit = submit
            tap.close()
        probe.cpu_calls = E.cpu_encode_calls() - cpu0
    probe.uavs = list(env.uavs)
    probe.wire_bytes = tap.wire_bytes
    assert tap.audit.jpeg_segment_violations == 0
    return probe


def test_1080p_three_uav_online_end_to_end(crop_run: _RunProbe) -> None:
    p = crop_run
    n_ticks = round(DURATION_S * 10)
    # 10 Hz：每 tick 每机恰好一帧，seq 连续，t_mid 间隔 100 ms；1080p 卷帘交付值
    for u in p.uavs:
        seqs = sorted(s for uu, s in p.agent if uu == u)
        assert seqs == list(range(n_ticks + 1)), (u, seqs[:5], len(seqs))
        tm = [p.agent[(u, s)]["t_mid_ns"] for s in seqs]
        assert all(b - a == TICK_NS for a, b in itertools.pairwise(tm[1:]))   # 第 0 帧 t_mid 钳位到 0（S03 遗留），自第 1 帧起
        assert all(p.agent[(u, s)]["k"] == s for s in seqs)
    assert len(p.uavs) == N_UAV and len(p.agent) == N_UAV * (n_ticks + 1)
    assert all(f["w"] == W and f["h"] == H and f["readout_ns"] == READOUT_NS and f["subsampling"] == "420" for f in p.agent.values())
    assert sum(1 for _k, n, _t in p.sense_s if n == N_UAV) == n_ticks + 1    # 每个相机 tick 同时为 3 机出帧
    # RGB 经 GPU nvJPEG；在线路径零 CPU 编码调用
    assert len(p.encoders) == 1 and NVJPEG_ID.fullmatch(next(iter(p.encoders))), p.encoders
    assert p.cpu_calls == 0
    assert len(p.timing) == len(p.agent)                                  # 每帧一次 GPU 提交
    # 抽样帧：新建 GPU 编码器重编码逐字节相同；CPU 回退（Pillow，同 q90 4:2:0 量化表）字节不同——交付字节来自 nvJPEG
    assert len(p.rgb) >= N_UAV
    with E.GpuJpegEncoder(W, H) as g:
        for key, rgb in p.rgb.items():
            b = g.encode(rgb)
            assert hashlib.sha256(b).hexdigest() == p.agent[key]["sha256"], key
    cpu = E.CpuJpegEncoder()
    key0, rgb0 = next(iter(p.rgb.items()))
    assert hashlib.sha256(cpu.encode(rgb0)).hexdigest() != p.agent[key0]["sha256"]
    # Agent 收到的字节与 raw.mcap 存档字节 sha256 逐帧一致（帧索引同样一致）
    arch = archived_frames(p.run_dir, p.uavs)
    assert set(arch) == set(p.agent)
    bad = [k for k, f in p.agent.items() if arch[k][0] != f["sha256"] or arch[k][1] != f["t_mid_ns"] or arch[k][2] != f["bytes"]]
    assert not bad, bad[:5]
    man = json.loads((p.run_dir / "manifest.json").read_text(encoding="utf-8"))
    rec = man["recorder"]
    assert rec["dropped"] == 0 and all(rec["frames"][f"{u}/{CAM}"] == n_ticks + 1 for u in p.uavs)
    # 实测记录：运行内单帧体积分布（按离地高度分段）与开发参考墙钟
    sizes = [f["bytes"] for f in p.agent.values()]
    bands = {name: size_stats(f["bytes"] for f in p.agent.values() if lo <= f["agl_m"] < hi) for lo, hi, name in AGL_BANDS}
    cam_s = [t for _k, n, t in p.sense_s if n == N_UAV]
    enc_ms = [t["encode_ms"] for t in p.timing if "encode_ms" in t]
    up_ms = [t["upload_ms"] for t in p.timing if "upload_ms" in t]
    record = {
        "schema": "drone4d.d3.s08.run_1080p.v1", "world": WORLD, "event": EVENT, "profile": PROFILE, "uavs": N_UAV, "duration_s": DURATION_S,
        "physics": "fast", "noise": "realistic", "policy": "coverage_scan", "encoder": sorted(p.encoders), "frames": len(p.agent),
        "cpu_encode_calls": p.cpu_calls, "sha256_agent_vs_raw_mcap": {"frames": len(arch), "mismatch": len(bad)},
        "reencode_checked": len(p.rgb), "size": size_stats(sizes), "size_by_agl": bands,
        "raw_mcap_bytes": int(rec["raw"]["bytes"]), "wire_bytes": p.wire_bytes,
        "dev_reference": {"note": "shared-card wall clock, not a formal performance figure (R-D3-63)",
                          "camera_tick_3x1080p": pct_ms(t * 1e3 for t in cam_s), "nvjpeg_encode": pct_ms(enc_ms),
                          "nvjpeg_upload": pct_ms(up_ms), "env_wall_s": round(p.wall_s, 2),
                          "rtf": round(DURATION_S / max(p.wall_s, 1e-9), 3),
                          "camera_fps_p50": round(N_UAV / max(float(np.percentile(cam_s, 50)), 1e-9), 1)}}
    write_record("run_1080p", record)


# ---------------------------------------------------------------- GPU 部分：固定视点网格上的 1080p 单帧体积
def _viewpoints(center: np.ndarray, ground_z: float) -> list[dict]:
    vps = []
    for agl in AGL_M:
        for pitch in PITCH_DEG:
            d = 0.0 if pitch <= -89.9 else agl / math.tan(math.radians(-pitch))
            for hd in HEADING_DEG if pitch > -89.9 else HEADING_DEG[:1]:
                a = math.radians(hd)
                pos = np.array([center[0] - d * math.cos(a), center[1] - d * math.sin(a), ground_z + agl])
                vps.append({"agl_m": agl, "pitch_deg": pitch, "heading_deg": hd, "pos": pos,
                            "target": np.array([center[0], center[1], ground_z]), "slant_m": math.hypot(d, agl)})
    return vps


def test_1080p_frame_size_viewpoints(crop_event) -> None:
    """同一产品路径（L2Sensors：GL 光栅、卷帘、效果链与自动曝光、nvJPEG）在固定视点网格与三个噪声档上的 1080p JPEG 体积。"""
    from awr.agentsdk.env.l2 import L2Config, L2Sensors
    from awr.sensorsvc.camera.proj import look_at_R
    from awr.sim.events.tape import open_tape

    w, ev, _tmp = crop_event
    tape = open_tape(Path(ev))
    st = tape.actor_states(5 * 10**9)
    v = np.isin(st.state, (1, 2))
    assert v.any()
    center = np.median(st.pos_enu_m[v], 0)
    ground_z = float(center[2])
    vps = _viewpoints(center, ground_z)
    uavs = [f"vp-{i:02d}" for i in range(len(vps))]
    meta = json.loads((Path(w) / "world.json").read_text(encoding="utf-8"))

    class FixedView(L2Sensors):
        """视点固定：相机位姿取 look-at（光轴指向演员群中心），其余（渲染、卷帘节点、效果链、编码）为产品路径。"""

        def _cam_pose(self, uav, t_ns):
            vp = vps[uavs.index(uav)]
            el = math.radians(vp["pitch_deg"])
            return vp["pos"], look_at_R(vp["pos"], vp["target"]), (math.radians(vp["heading_deg"]), el, 0.0)

    rows, enc_ms, depth_ok = [], [], []
    renderer = None
    n0 = E.cpu_encode_calls()
    with E.online_path():
        enc = E.shared_encoder(PROFILE, W, H)
        submit = enc.submit

        def timed_submit(rgb8):
            tk = submit(rgb8)
            enc_ms.append(tk.timing)
            return tk

        enc.submit = timed_submit
        try:
            for tier in TIERS:
                s = FixedView(world_dir=Path(w), tape=tape, uavs=uavs, seed=SEED, noise=tier, obs_level="L2", nav=None,
                              mount_t_bs_m=(0.12, 0.0, -0.08), hfov_deg=60.0, renderer=renderer, world_meta=meta,
                              cfg=L2Config(camera_profile=PROFILE, lidar=False, imu=False, gnss=False))
                renderer = s.cam                                     # 三个噪声档共用一个 GL 上下文与世界常驻
                for u in uavs:
                    s.ingest(u, np.array([0, 20 * TICK_NS], np.int64), np.stack([_static_state(np.zeros(3))] * 2))
                for k in range(1, VP_FRAMES + 1):
                    res = s.sense_all(k, [(u, i) for i, u in enumerate(uavs)])
                    if k <= VP_SETTLE:
                        continue
                    for i, u in enumerate(uavs):
                        f = res[u][0][CAM][0]
                        b = f["rgb"]["data"]
                        assert E.jpeg_subsampling(b) == "420" and f["width"] == W and f["readout_ns"] == READOUT_NS
                        vp = vps[i]
                        rows.append({"tier": tier, "agl_m": vp["agl_m"], "pitch_deg": vp["pitch_deg"], "heading_deg": vp["heading_deg"],
                                     "slant_m": round(vp["slant_m"], 1), "k": k, "bytes": len(b)})
                assert NVJPEG_ID.fullmatch(s.encoder), s.encoder
                if tier == "clean":                                  # 几何核对：中心像素深度与视点斜距一致（地表起伏与建筑放宽 35%）
                    out = renderer.render(s.cm, look_at_R(vps[0]["pos"], vps[0]["target"]), vps[0]["pos"])
                    dc = float(np.median(out.depth_m[H // 2 - 16:H // 2 + 16, W // 2 - 16:W // 2 + 16]))
                    depth_ok.append((dc, vps[0]["slant_m"]))
        finally:
            enc.submit = submit
            if renderer is not None:
                renderer.release()
    assert E.cpu_encode_calls() == n0
    assert depth_ok and all(abs(dc - sl) <= 0.35 * sl for dc, sl in depth_ok), depth_ok
    assert len(rows) == len(TIERS) * len(vps) * (VP_FRAMES - VP_SETTLE)
    by = {}
    for tier in TIERS:
        by[tier] = {"all": size_stats(r["bytes"] for r in rows if r["tier"] == tier)}
        for agl in AGL_M:
            for pitch in PITCH_DEG:
                by[tier][f"agl{int(agl)}_pitch{int(pitch)}"] = size_stats(
                    r["bytes"] for r in rows if r["tier"] == tier and r["agl_m"] == agl and r["pitch_deg"] == pitch)
    allr = size_stats(r["bytes"] for r in rows)
    assert allr["min_kB"] > 50.0 and allr["max_kB"] < 3000.0                # 体积量级合理（q90 4:2:0 的 1080p）
    record = {"schema": "drone4d.d3.s08.size_1080p.v1", "world": WORLD, "event": EVENT, "profile": PROFILE, "hfov_deg": 60.0,
              "encoder": s.encoder, "center_enu_m": [round(float(x), 2) for x in center], "viewpoints": len(vps),
              "frames_per_viewpoint": VP_FRAMES - VP_SETTLE, "size": allr, "by_tier": by, "center_depth_check": depth_ok,
              "dev_reference": {"note": "shared-card wall clock, not a formal performance figure (R-D3-63)",
                                "nvjpeg_encode": pct_ms(t["encode_ms"] for t in enc_ms if "encode_ms" in t),
                                "nvjpeg_upload": pct_ms(t["upload_ms"] for t in enc_ms if "upload_ms" in t)},
              "rows": rows}
    write_record("size_1080p", record)


def test_l2_async_batches_match_sync_on_gpu(crop_event) -> None:
    """读回环分批（D3-AC-60；F-116）：EGL 光栅相机只有 2 个读回槽位时，L2 一个 tick 为 5 个视点出帧（异步接口按槽位数分 3 批提交）
    不超时；连续 3 个 tick 的交付 JPEG 与真值行与同一渲染器经同步接口（`render`）逐机渲染的结果逐字节相同。"""
    _require_gpu_stack()
    from awr.agentsdk.env.l2 import L2Config, L2Sensors
    from awr.sensorsvc.camera.proj import look_at_R
    from awr.sensorsvc.camera.raster import RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance
    from awr.sim.events.tape import open_tape

    w, ev, _tmp = crop_event
    tape = open_tape(Path(ev))
    st = tape.actor_states(5 * 10**9)
    v = np.isin(st.state, (1, 2))
    center = np.median(st.pos_enu_m[v], 0)
    vps = [vp for vp in _viewpoints(center, float(center[2])) if vp["agl_m"] in (40.0, 80.0)][:5]
    uavs = [f"vp-{i}" for i in range(len(vps))]
    meta = json.loads((Path(w) / "world.json").read_text(encoding="utf-8"))

    class FixedView(L2Sensors):
        def _cam_pose(self, uav, t_ns):
            vp = vps[uavs.index(uav)]
            return vp["pos"], look_at_R(vp["pos"], vp["target"]), (math.radians(vp["heading_deg"]), math.radians(vp["pitch_deg"]), 0.0)

    class SyncOnly:                                                  # 同一渲染器的同步接口（无 `submit`）：逐机渲染并复制
        def __init__(self, cam) -> None:
            self.cam = cam

        def render(self, *a, **kw):
            return self.cam.render(*a, **kw)

    cam = RasterCamera(load_appearance(Path(w)), readback_slots=2)
    cam.set_actors(tape.actors)
    cam.set_sun(tuple((meta.get("sensorHints") or {}).get("bakedSunAzElDeg") or ()) or None)
    assert cam.readback_slots == 2

    def run(renderer) -> list:
        s = FixedView(world_dir=Path(w), tape=tape, uavs=uavs, seed=SEED, noise="realistic", obs_level="L2", nav=None,
                      mount_t_bs_m=(0.12, 0.0, -0.08), hfov_deg=60.0, renderer=renderer, world_meta=meta,
                      cfg=L2Config(camera_profile=PROFILE, lidar=False, imu=False, gnss=False))
        for u in uavs:
            s.ingest(u, np.array([0, 20 * TICK_NS], np.int64), np.stack([_static_state(np.zeros(3))] * 2))
        out = []
        for k in (1, 2, 3):
            res = s.sense_all(k, [(u, i) for i, u in enumerate(uavs)])
            out += [(u, k, hashlib.sha256(res[u][0][CAM][0]["rgb"]["data"]).hexdigest(), repr(res[u][1])) for u in uavs]
        return out

    try:
        with E.online_path():
            a = run(cam)
            waits = cam.ring.waits
            b = run(SyncOnly(cam))
    finally:
        cam.release()
    assert len(a) == 5 * 3 and a == b
    assert cam.ring.n == 2 and waits == 0                           # 分批后同一线程内从不等待槽位
