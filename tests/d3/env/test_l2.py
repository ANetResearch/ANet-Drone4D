"""L2 渲染级观测（M26 FR-021、FR-026）：观测通过 obs.v1 校验、JPEG 段白名单、传感器帧随 tick 累积、同种子逐字节一致；
EU 的框证据按 IoU 关联到演员。渲染以 CPU 替身代替 EGL 光栅相机（真实渲染在 dmax 上验证）。"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from awr.agentsdk.env.core import BenchEnv
from awr.agentsdk.env.l2 import CAM_SENSOR, GNSS_SENSOR, IMU_SENSOR, LIDAR_SENSOR, L2Config, L2Sensors
from awr.agentsdk.env.spec import EnvSpec, FleetEntry
from awr.agentsdk.schemas import bench_validator

sys.path.insert(0, str(Path(__file__).parent))

FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 30.0), 0.0, airborne=True), FleetEntry("uav-02", (10.0, 50.0, 30.0), 0.0, airborne=True))
JPEG_OK = {0xD8, 0xE0, 0xDB, 0xC0, 0xC4, 0xDD, 0xDA, 0xD9} | set(range(0xD0, 0xD8))


class FakeRaster:
    """EGL 光栅相机的 CPU 替身：按位姿生成确定性的灰度渐变图（不绘制场景）。"""

    def render(self, cm, R_ws, p_w, *, fx=None, rs_nodes=None, actors=None, linear=False):
        from awr.sensorsvc.camera.raster import RenderOut

        h, w = cm.height, cm.width
        v = int(abs(float(p_w[0]) * 7 + float(p_w[1]) * 3)) % 200
        rgb = np.broadcast_to((np.arange(w, dtype=np.uint16)[None, :, None] * 255 // w + v).astype(np.uint8) % 255, (h, w, 3)).copy()
        z16, z8 = np.zeros((h, w), np.uint16), np.zeros((h, w), np.uint8)
        inst, unocc = z16.copy(), z16.copy()
        if actors is not None:                                     # 每个演员一个按位姿平移的方块（实例号 idx + 1），供真值可见度归约
            for j, i in enumerate(np.asarray(actors.idx)[:4]):
                x0, y0 = (40 + 90 * j + v) % (w - 40), (60 + 7 * j) % (h - 60)
                unocc[y0:y0 + 30, x0:x0 + 12] = int(i) + 1
                inst[y0 + 5:y0 + 30, x0:x0 + 12] = int(i) + 1
        return RenderOut(rgb, np.zeros((h, w), np.float32), inst, z8, 0.0, 0.0, 0, instance_unocc=unocc)


class FakeRasterAsync(FakeRaster):
    """异步接口替身（`submit` → 作业 `result()` / `release()`）：结果为共享槽位上的只读视图，归还后槽位被覆写，用于发现归还后
    仍读取视图的错误。"""

    def __init__(self, slots: int = 6) -> None:
        self.free = list(range(slots))
        self.mem: dict[int, dict] = {}

    def submit(self, cm, R_ws, p_w, *, fx=None, rs_nodes=None, actors=None):
        out = self.render(cm, R_ws, p_w, fx=fx, rs_nodes=rs_nodes, actors=actors)
        k = self.free.pop(0)
        planes = {n: getattr(out, n) for n in ("rgb8", "depth_m", "instance", "semantic", "instance_unocc")}
        for a in planes.values():
            a.setflags(write=False)
        self.mem[k] = planes
        ring = self

        class Job:
            released = False

            def result(self):
                from awr.sensorsvc.camera.raster import RenderOut

                m = ring.mem[k]
                return RenderOut(m["rgb8"], m["depth_m"], m["instance"], m["semantic"], 0.0, 0.0, 0, instance_unocc=m["instance_unocc"])

            def release(self):
                if self.released:
                    return
                self.released = True
                for a in ring.mem[k].values():                   # 槽位复用：归还后内容失效
                    a.setflags(write=True)
                    a[...] = 0
                ring.free.append(k)

        return Job()


class FakeRasterRing(FakeRaster):
    """有界读回环替身（与 `ReadbackRing.acquire` 同义）：`readback_slots` 个槽位，全部占用时 `submit` 等待其他线程归还，`timeout_s`
    内等不到则抛 TimeoutError；结果为槽位上的只读视图，归还后槽位被覆写。`advertise` 为对调用方声明的槽位数（缺省等于实际值；
    取大于实际值时复现分批之前"一次提交全部机体"的超时）。"""

    def __init__(self, slots: int = 3, *, timeout_s: float = 2.0, advertise: int | None = None) -> None:
        import threading

        self.n, self.timeout_s, self.advertise = int(slots), float(timeout_s), advertise
        self.free = list(range(self.n))
        self.mem: dict[int, dict] = {}
        self.cv = threading.Condition()
        self.max_busy = 0
        self.submits = 0

    @property
    def readback_slots(self) -> int:
        return self.n if self.advertise is None else int(self.advertise)

    def submit(self, cm, R_ws, p_w, *, fx=None, rs_nodes=None, actors=None):
        import time

        out = self.render(cm, R_ws, p_w, fx=fx, rs_nodes=rs_nodes, actors=actors)
        deadline = time.monotonic() + self.timeout_s
        with self.cv:
            while not self.free:
                left = deadline - time.monotonic()
                if left <= 0 or not self.cv.wait(left):
                    raise TimeoutError("readback ring: no free slot (frames are not being released)")
            k = self.free.pop(0)
            self.max_busy = max(self.max_busy, self.n - len(self.free))
            self.submits += 1
        planes = {n: getattr(out, n) for n in ("rgb8", "depth_m", "instance", "semantic", "instance_unocc")}
        for a in planes.values():
            a.setflags(write=False)
        self.mem[k] = planes
        ring = self

        class Job:
            released = False

            def result(self):
                from awr.sensorsvc.camera.raster import RenderOut

                m = ring.mem[k]
                return RenderOut(m["rgb8"], m["depth_m"], m["instance"], m["semantic"], 0.0, 0.0, 0, instance_unocc=m["instance_unocc"])

            def release(self):
                if self.released:
                    return
                self.released = True
                for a in ring.mem[k].values():
                    a.setflags(write=True)
                    a[...] = 0
                with ring.cv:
                    ring.free.append(k)
                    ring.cv.notify_all()

        return Job()


def _segments(jpg: bytes) -> set[int]:
    seen, i = set(), 2
    assert jpg[:2] == b"\xff\xd8"
    seen.add(0xD8)
    while i < len(jpg):
        assert jpg[i] == 0xFF
        m = jpg[i + 1]
        seen.add(m)
        if m == 0xD9:
            break
        n = int.from_bytes(jpg[i + 2:i + 4], "big")
        if m == 0xDA:                                              # 扫描数据直到 EOI
            seen.add(0xD9)
            break
        i += 2 + n
    return seen


def _l2(env: BenchEnv, lidar_proc: bool = True, renderer=None) -> L2Sensors:
    return L2Sensors(world_dir=Path(env.spec.world_dir), tape=env.tape, uavs=env.uavs, seed=env.spec.seed, noise=env.spec.noise,
                     obs_level=env.spec.obs_level, nav=env.nav, mount_t_bs_m=env.cam.mount_t_bs_m, hfov_deg=env.cam.hfov_deg,
                     cfg=L2Config(camera_profile="rgb720-gs", cache_dir=Path(env.spec.out_dir) / "wq", lidar_proc=lidar_proc),
                     renderer=renderer or FakeRaster())


def _episode(synth_event, out: Path, obs_level: str = "L2", lidar_proc: bool = True, renderer=None,
             async_rec: bool = False, fleet=FLEET, l2=None) -> tuple[list[tuple[str, dict]], dict]:
    w, ev = synth_event
    spec = EnvSpec("t-l2", 3, w, ev, fleet, obs_level=obs_level, physics="kinematic", duration_s=3.0, out_dir=out)
    env = BenchEnv(spec, l2=l2 or (lambda e: _l2(e, lidar_proc, renderer)))
    env.async_obs_record = async_rec
    obs, infos = env.reset()
    seen = [(u, o) for u, o in obs.items()]
    cfg = infos["uav-01"]["config"]
    while env.agents:
        acts = {u: {"nav": {"kind": "goto", "target_enu": [0.0, 60.0, 30.0], "speed_mps": 4.0}, "gimbal": {"pitch_deg": -45.0, "yaw_deg": 10.0}}
                for u in obs}
        obs, *_ = env.step(acts)
        seen += [(u, o) for u, o in obs.items()]
    env.close()
    return seen, cfg


def test_l2_observations_validate_and_carry_sensor_frames(synth_event, tmp_path: Path) -> None:
    seen, cfg = _episode(synth_event, tmp_path / "a")
    assert not list(bench_validator("agent_config").iter_errors(cfg))
    kinds = {s["sensor_id"]: s["kind"] for s in cfg["sensors"]}
    assert kinds == {CAM_SENSOR: "camera", LIDAR_SENSOR: "lidar", IMU_SENSOR: "imu", GNSS_SENSOR: "gnss"}
    val = bench_validator("obs")
    seqs: dict[str, list[int]] = {}
    for u, msg in seen:
        errs = [e.message for e in val.iter_errors(msg)]
        assert not errs, errs[:3]
        s = msg["obs"]["sensors"]
        assert "detections" not in s
        for f in s[CAM_SENSOR]:
            assert _segments(f["rgb"]["data"]) <= JPEG_OK
            assert f["width"] == 1280 and f["readout_ns"] == 0
            seqs.setdefault(u, []).append(f["seq"])
        if msg["obs"]["k"] > 0:
            imu = s[IMU_SENSOR][0]
            assert imu["n"] == 20 and len(imu["acc_mps2"]) == 4 * 3 * 20
            lid = s[LIDAR_SENSOR][0]
            assert len(lid["xyz_m"]) == 12 * lid["n"] and len(lid["offset_time_ns"]) == 4 * lid["n"]
    assert seqs["uav-01"] == list(range(31))                       # 每 tick 一帧，按 seq 升序、无缺漏
    first = next(m for u, m in seen if u == "uav-01" and m["obs"]["k"] == 10)
    assert first["obs"]["sensors"][CAM_SENSOR][0]["gimbal_reading"]["pitch_deg"] < -30.0          # 云台指令生效（限速趋近）


def test_l2_is_deterministic_and_l2_depth_adds_depth(synth_event, tmp_path: Path) -> None:
    def digest(seen):
        h = hashlib.sha256()
        for u, m in seen:
            for sid, fs in sorted(m["obs"]["sensors"].items()):
                for f in fs:
                    for k in ("rgb", "xyz_m", "acc_mps2", "pos_enu_m"):
                        x = f.get(k)
                        if x is not None:
                            h.update(u.encode() + sid.encode() + (x["data"] if k == "rgb" else x if isinstance(x, bytes) else repr(x).encode()))
        return h.hexdigest()

    a, _ = _episode(synth_event, tmp_path / "a")
    b, _ = _episode(synth_event, tmp_path / "b")
    assert digest(a) == digest(b)
    d, cfg = _episode(synth_event, tmp_path / "d", obs_level="L2-depth")
    f = d[0][1]["obs"]["sensors"][CAM_SENSOR][0]
    assert f["depth"]["encoding"] == "f32le" and len(f["depth"]["data"]) == 4 * 1280 * 720
    assert cfg["obs_level"] == "L2-depth"


def test_lidar_worker_process_matches_in_process(synth_event, tmp_path: Path) -> None:
    a, _ = _episode(synth_event, tmp_path / "p", lidar_proc=True)
    b, _ = _episode(synth_event, tmp_path / "i", lidar_proc=False)
    la = [(u, f["seq"], f["xyz_m"], f["offset_time_ns"]) for u, m in a for f in m["obs"]["sensors"].get(LIDAR_SENSOR, [])]
    lb = [(u, f["seq"], f["xyz_m"], f["offset_time_ns"]) for u, m in b for f in m["obs"]["sensors"].get(LIDAR_SENSOR, [])]
    assert la == lb and len(la) == 60 and all(len(x[2]) > 0 for x in la)
    import json

    for d in ("p", "i"):                                               # 运行清单记录网格 LiDAR 后端（缺省 Open3D CPU 参考）
        assert json.loads((tmp_path / d / "manifest.json").read_text(encoding="utf-8"))["l2"] == {"lidar_backend": "cpu"}


def test_lidar_backend_is_fixed_by_config_or_env_variable(monkeypatch) -> None:
    """后端只由 `L2Config.lidar_backend` 或 Env 专用变量 `AWR_ENV_LIDAR_BACKEND` 确定（缺省 cpu）；sensorsvc 的 `AWR_LIDAR_BACKEND`
    （缺省 auto，随机器是否有 GPU 改变点云）不影响 Env。"""
    from awr.agentsdk.env.l2 import lidar_backend

    monkeypatch.delenv("AWR_ENV_LIDAR_BACKEND", raising=False)
    monkeypatch.setenv("AWR_LIDAR_BACKEND", "warp")
    assert lidar_backend(L2Config()) == "cpu"
    monkeypatch.setenv("AWR_ENV_LIDAR_BACKEND", "warp")
    assert lidar_backend(L2Config()) == "warp"
    assert lidar_backend(L2Config(lidar_backend="cpu")) == "cpu"
    monkeypatch.setenv("AWR_ENV_LIDAR_BACKEND", "auto")
    with pytest.raises(ValueError, match="lidar backend"):
        lidar_backend(L2Config())


def test_scan_jobs_batches_warp_frames_like_per_frame_scans() -> None:
    """Warp 后端（有 `scan_batch`）一次扫描同一 tick 的全部机体，逐机结果与逐帧 `scan` 相同（帧序号 k − 1、窗口起点、机序号、种子、
    噪声档按作业传入）。"""
    from awr.agentsdk.env import l2 as L
    from awr.bench.recorder.lidar import LIDAR_RECORD

    def frame(seq, t_s, uav_no, seed, tier):
        p = np.zeros(3, LIDAR_RECORD)
        p["x"] = [seq, t_s / 1e9, uav_no]
        p["y"] = seed % 1000
        p["z"] = len(tier)
        p["offset_time"] = [1, 2, 3]
        return SimpleNamespace(points=p)

    class PerFrame:
        def scan(self, seq, t_s, nodes, *, actor_states=None, seed=0, uav_no=0, sensor_no=0, tier="realistic"):
            assert sensor_no == 0 and actor_states == "fn"
            return frame(seq, t_s, uav_no, seed, tier)

    class Batched(PerFrame):
        calls = 0

        def scan_batch(self, reqs, *, actor_states=None):
            Batched.calls += 1
            assert actor_states == "fn"
            return [frame(r.frame_seq, r.t_start_ns, r.uav_no, r.seed, r.tier) for r in reqs]

    jobs = [("uav-01", 0, 400_000_000, []), ("uav-02", 1, 400_000_000, []), ("uav-03", 2, 400_000_000, [])]
    a = L._scan_jobs(PerFrame(), 5, jobs, "fn", 1234, "realistic")
    b = L._scan_jobs(Batched(), 5, jobs, "fn", 1234, "realistic")
    assert Batched.calls == 1 and list(a) == list(b) == ["uav-01", "uav-02", "uav-03"]
    assert a == b and a["uav-03"]["seq"] == 4 and a["uav-03"]["n"] == 3


def test_async_render_path_matches_sync_and_obs_hash(synth_event, tmp_path: Path) -> None:
    """异步接口路径（先提交各机、再按机序取回与编码；真值可见度归约推迟到后台线程，槽位在归约后归还；观测记录在后台线程写出，
    `async_obs_record`）与同步路径逐字节相同：
    观测摘要文件（`obs.sha256`，由 `canonical_sha256` 写出）、交付的传感器帧与真值行 truth/l2.jsonl 一致；摘要与整段规范 JSON 的
    sha256 相同。"""
    from awr.agentsdk.env.recorder import canonical_json

    a, _ = _episode(synth_event, tmp_path / "s")
    b, _ = _episode(synth_event, tmp_path / "a", renderer=FakeRasterAsync(), async_rec=True)
    for u in ("uav-01", "uav-02"):
        sa = (tmp_path / "s" / "agents" / u / "obs.sha256").read_text()
        assert sa == (tmp_path / "a" / "agents" / u / "obs.sha256").read_text() and sa.count("\n") == 31
        want = [f"{m['obs']['k']} {hashlib.sha256(canonical_json(m)).hexdigest()}" for uu, m in a if uu == u]
        assert sa.splitlines() == want
    assert [m["obs"]["sensors"] for _u, m in a] == [m["obs"]["sensors"] for _u, m in b]
    from awr.bench.recorder.writer import channel_counts

    assert channel_counts(tmp_path / "s" / "raw.mcap") == channel_counts(tmp_path / "a" / "raw.mcap")   # 后台线程的观测记录（逐通道条数）
    tv = (tmp_path / "s" / "truth" / "l2.jsonl").read_bytes()                # 推迟到后台线程的真值行：内容与顺序不变
    assert tv and tv == (tmp_path / "a" / "truth" / "l2.jsonl").read_bytes() and tv.count(b"\n") >= 2 * 31


def test_l2_frame_evidence_reports_are_accepted(synth_event, tmp_path: Path) -> None:
    """L2 的 report 证据为相机帧框（`sensor`、`frame_seq`、`bbox_xywh`，无 det_id）：引用本机已交付的帧时接受并写入 reports.jsonl，
    引用未交付的帧时 663 `evidence_unknown`（守卫按交付帧登记，`ActionGuard.saw_frames`）。"""
    import json

    w, ev = synth_event
    spec = EnvSpec("t-l2-rep", 3, w, ev, FLEET, obs_level="L2", physics="kinematic", duration_s=1.0, out_dir=tmp_path / "r")
    env = BenchEnv(spec, l2=lambda e: _l2(e))
    obs, _ = env.reset()
    acks = []

    def rep(seq: int) -> dict:
        return {"type": "meet", "participants": [{"track_id": "a", "evidence": [{"uav": "uav-01", "sensor": CAM_SENSOR, "frame_seq": seq,
                                                                                    "bbox_xywh": [10.0, 10.0, 6.0, 12.0]}]}],
                "window": {"t_start_ns": 0, "t_end_ns": 1}}

    while env.agents:
        acts = {}
        for u, o in obs.items():
            k = o["obs"]["k"]
            acks += [(u, a) for a in o["obs"]["acks"]]
            a = {"schema": "drone4d.bench.act.v1", "k": k}
            if u == "uav-01" and k == 3:
                a["report"] = [rep(2), rep(99)]
            acts[u] = a
        obs, *_ = env.step(acts)
        acks += [(u, a) for u, o in obs.items() for a in o["obs"]["acks"]]
    env.close()
    got = {(a["k"], a["index"]): (a["result"], a.get("why")) for u, a in acks if u == "uav-01" and a["item"] == "report"}
    assert got == {(3, 0): ("applied", None), (3, 1): ("rejected", "evidence_unknown")}
    rows = [json.loads(x) for x in (tmp_path / "r" / "reports.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["report"]["participants"][0]["evidence"][0]["frame_seq"] == 2


def test_canonical_sha256_equals_full_encoding() -> None:
    """二进制载荷以占位串代替的分段摘要与整段 `canonical_json` 的 sha256 相同：嵌套、bytearray、numpy 标量、需转义的字符串、空载荷，
    以及数据中出现形同占位串的字符串（退回整段编码）。"""
    import os

    from awr.agentsdk.env.recorder import canonical_json, canonical_sha256

    cases = [{}, {"a": b""}, {"obs": {"k": np.int64(3), "sensors": {"eo": [{"rgb": {"data": os.urandom(5000)}, "K": [1.0, 2.5]}],
                                                                   "lidar0": [{"xyz_m": os.urandom(1200), "tag": bytearray(b"\x00\x01")}]},
                                     "nav": {"note": 'é "q" \\ \u0000 @b64@'}}, "inbox": [{"payload": b"\xff", "s": "/"}]},
             {"x": b"xy", "y": "@b64@0123456789abcdef@0@"}, [b"a", [b"b", {"z": b"c"}]]]
    for c in cases:
        assert canonical_sha256(c) == hashlib.sha256(canonical_json(c)).hexdigest()


def test_bbox_evidence_resolves_by_iou() -> None:
    from awr.bench.eval.eu import resolve

    run = SimpleNamespace(det_actor={}, vis_boxes={("uav-01", 7): [{"actor": "person_a", "bbox_xywh": [100, 100, 20, 50]},
                                                                   {"actor": "person_b", "bbox_xywh": [130, 100, 20, 50]}]})
    ev = lambda box: {"uav": "uav-01", "sensor": "eo", "frame_seq": 7, "bbox_xywh": box}       # noqa: E731
    assert resolve({"evidence": [ev([102, 102, 20, 48])]}, run) == "person_a"
    assert resolve({"evidence": [ev([300, 300, 20, 50])]}, run) is None                       # 无重叠：未解析
    assert resolve({"evidence": [ev([102, 102, 20, 48]), ev([131, 99, 20, 50]), ev([129, 101, 20, 50])]}, run) == "person_b"


@pytest.mark.parametrize("level", ["L3", "L0"])
def test_unranked_levels_are_refused(synth_event, level: str) -> None:
    w, ev = synth_event
    with pytest.raises(ValueError):
        BenchEnv(EnvSpec("t", 1, w, ev, FLEET, obs_level=level, physics="kinematic", duration_s=1.0))


# ---------------------------------------------------------------- 读回环槽位分批（D3-AC-60；F-116）
def _stub_tape():
    st = SimpleNamespace(ids=np.zeros(0, np.int64), state=np.zeros(0, np.uint8), pos_enu_m=np.zeros((0, 3)), yaw_rad=np.zeros(0))
    return SimpleNamespace(actors=[], path=Path("."), actor_states=lambda t_ns, ids=None: st)


def _unit(renderer, n: int) -> L2Sensors:
    """无世界的 L2 相机单元：n 架机静止于不同位置（各机画面不同），云台俯仰 −45°。"""
    uavs = [f"u{i:02d}" for i in range(n)]
    s = L2Sensors(world_dir=Path("."), tape=_stub_tape(), uavs=uavs, seed=3, noise="realistic", obs_level="L2", nav=None,
                  mount_t_bs_m=(0.12, 0.0, -0.08), hfov_deg=60.0, renderer=renderer,
                  cfg=L2Config(camera_profile="rgb720-gs", lidar=False, imu=False, gnss=False))
    for i, u in enumerate(uavs):
        st = np.zeros(16)
        st[0:3], st[6] = (7.0 * i, -3.0 * i, 40.0 + i), 1.0
        s.ingest(u, np.array([0, 10 * 100_000_000], np.int64), np.stack([st, st]))
        s.gimbal(u, 0, 5.0 * i, -45.0)
    return s


def _frames(s: L2Sensors, ticks=(1, 2, 3)) -> list:
    out = []
    for k in ticks:
        res = s.sense_all(k, [(u, i) for i, u in enumerate(s.uavs)])
        for u in s.uavs:
            sens, rows = res[u]
            out.append((u, k, sens, rows.result() if hasattr(rows, "result") else rows))
    return out


@pytest.mark.parametrize("defer", [False, True])
def test_async_path_batches_submissions_by_readback_slots(defer: bool) -> None:
    """一个 tick 的机数大于读回槽位数（8 机、3 槽位）：异步路径按槽位数分批提交，同时未归还的帧数不超过槽位数，每机每 tick 恰好
    提交一次，交付帧与真值行与同步路径逐字节相同（含 `defer_truth`：上一批槽位由真值线程归还）。"""
    pytest.importorskip("PIL")
    ref = _frames(_unit(FakeRaster(), 8))
    ring = FakeRasterRing(3)
    s = _unit(ring, 8)
    s.defer_truth = defer
    try:
        got = _frames(s)
    finally:
        s.close()
    assert ring.max_busy <= 3 and ring.submits == 8 * 3 and len(ring.free) == 3
    assert [(u, k) for u, k, *_ in got] == [(u, k) for u, k, *_ in ref]
    assert [x[2] for x in got] == [x[2] for x in ref] and [x[3] for x in got] == [x[3] for x in ref]


def test_async_path_without_batching_times_out_on_full_ring() -> None:
    """替身复现分批之前的故障：渲染器声明的槽位数大于实际槽位数时，一次提交全部机体的第（槽位数 + 1）次提交等不到归还而超时；
    声明正确时同一负载正常完成（L2 只按声明的 `readback_slots` 分批）。"""
    pytest.importorskip("PIL")
    s = _unit(FakeRasterRing(2, timeout_s=0.2, advertise=64), 5)
    with pytest.raises(TimeoutError):
        s.sense_all(1, [(u, i) for i, u in enumerate(s.uavs)])
    ok = _unit(FakeRasterRing(2, timeout_s=0.2), 5)
    assert len(ok.sense_all(1, [(u, i) for i, u in enumerate(ok.uavs)])) == 5


def test_async_episode_with_more_uavs_than_readback_slots(synth_event, tmp_path: Path) -> None:
    """8 机 L2 运行（BenchEnv：`defer_truth` 与后台观测记录开启），读回环 3 个槽位：每 tick 分 3 批提交，不超时；观测摘要、交付帧与
    truth/l2.jsonl 与同步路径逐字节相同。"""
    fleet = tuple(FleetEntry(f"uav-{i + 1:02d}", (-21.0 + 6.0 * i, 50.0 + (i % 2) * 4.0, 30.0), 0.0, airborne=True) for i in range(8))

    def l2(renderer):
        return lambda e: L2Sensors(world_dir=Path(e.spec.world_dir), tape=e.tape, uavs=e.uavs, seed=e.spec.seed, noise=e.spec.noise,
                                   obs_level=e.spec.obs_level, nav=e.nav, mount_t_bs_m=e.cam.mount_t_bs_m, hfov_deg=e.cam.hfov_deg,
                                   cfg=L2Config(camera_profile="rgb720-gs", lidar=False, imu=False, gnss=False), renderer=renderer)

    ring = FakeRasterRing(3, timeout_s=10.0)
    a, _ = _episode(synth_event, tmp_path / "s", fleet=fleet, l2=l2(FakeRaster()))
    b, _ = _episode(synth_event, tmp_path / "a", fleet=fleet, l2=l2(ring), async_rec=True)
    assert ring.max_busy <= 3 and ring.submits == 8 * 31
    assert [m["obs"]["sensors"] for _u, m in a] == [m["obs"]["sensors"] for _u, m in b]
    for f in fleet:
        sa = (tmp_path / "s" / "agents" / f.uav / "obs.sha256").read_text()
        assert sa.count("\n") == 31 and sa == (tmp_path / "a" / "agents" / f.uav / "obs.sha256").read_text()
    tv = (tmp_path / "s" / "truth" / "l2.jsonl").read_bytes()
    assert tv == (tmp_path / "a" / "truth" / "l2.jsonl").read_bytes()
