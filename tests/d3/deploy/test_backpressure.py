"""M25 背压（D3-AC-17 背压一致性、D3-AC-22 背压三项；M25 FR-123、FR-124；AWR-05 ADR-162、ADR-173）。

1. Benchmark 锁步：在途上限 1、每帧人为延迟 50 ms 时 641 只阻塞仿真时钟（墙钟变长），不降频、不丢帧；同步执行与工作线程执行
   两种方式下 Agent 收到的观测流、`(uav, sensor, seq)` 集合与帧字节逐一一致，且与不经 M25 会话的运行一致。
2. 交互预览：队列超限返回 641 并使预览帧率减半（下限 0.5 Hz），负载回落、连续 5 s 无 641 后每 5 s 恢复一级至 2 Hz；
   预览级优先于在线级取批；预览会话不能走锁步等待。
3. 观测墙预览：Benchmark 运行的 2 Hz、480 × 270 预览由写入器旁路从交付帧降采样，M25 预览队列的提交数为 0，观测流不变。

渲染以 CPU 替身代替 EGL 光栅相机（与 tests/d3/env/test_l2.py 相同口径）；GPU 帧的同项对拍在 dmax 上由正式配置用例执行。
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "world"))
sys.path.insert(0, str(HERE.parents[0] / "events"))

pytest.importorskip("open3d")
pytest.importorskip("zstandard")

from awr.agentsdk.env.core import BenchEnv  # noqa: E402
from awr.agentsdk.env.l2 import CAM_SENSOR, L2Config, L2Sensors  # noqa: E402
from awr.agentsdk.env.spec import EnvSpec, FleetEntry  # noqa: E402
from awr.sensorsvc.errors import SensorSvcError  # noqa: E402
from awr.sensorsvc.scheduler import FrameKey, PreviewRate, PreviewStream, Request, Scheduler, wait_all  # noqa: E402

FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 30.0), 0.0, airborne=True), FleetEntry("uav-02", (10.0, 50.0, 30.0), 0.0, airborne=True))
DURATION_S = 1.0
DELAY_S = 0.05
S = 1_000_000_000


@pytest.fixture(scope="module")
def synth_event(tmp_path_factory):
    from synthworld import build_synth_world
    from test_compile import EVENT, OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    tmp = tmp_path_factory.mktemp("bp")
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
    # meet_ab 为示意声明（合成路线中 a、b 不相遇）；夹具不核对语义，门 3 以 lenient 编译（strict 下为 613，E01）
    r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache", strict=False))
    assert r.status == "compiled", r.as_dict()
    yield w, d / "v1"
    mp.undo()


class FakeRaster:
    """EGL 光栅相机的 CPU 替身：按位姿生成确定性的灰度渐变图（与 tests/d3/env/test_l2.py 相同）。"""

    def render(self, cm, R_ws, p_w, *, fx=None, rs_nodes=None, actors=None, linear=False):
        from awr.sensorsvc.camera.raster import RenderOut

        h, w = cm.height, cm.width
        v = int(abs(float(p_w[0]) * 7 + float(p_w[1]) * 3)) % 200
        rgb = np.broadcast_to((np.arange(w, dtype=np.uint16)[None, :, None] * 255 // w + v).astype(np.uint8) % 255, (h, w, 3)).copy()
        z16, z8 = np.zeros((h, w), np.uint16), np.zeros((h, w), np.uint8)
        return RenderOut(rgb, np.zeros((h, w), np.float32), z16, z8, 0.0, 0.0, 0, instance_unocc=z16)


def _l2(env: BenchEnv) -> L2Sensors:
    return L2Sensors(world_dir=Path(env.spec.world_dir), tape=env.tape, uavs=env.uavs, seed=env.spec.seed, noise=env.spec.noise,
                     obs_level=env.spec.obs_level, nav=env.nav, mount_t_bs_m=env.cam.mount_t_bs_m, hfov_deg=env.cam.hfov_deg,
                     cfg=L2Config(camera_profile="rgb720-gs", cache_dir=Path(env.spec.world_dir).parent / "wq"), renderer=FakeRaster())


def _run(synth_event, out: Path, **kw) -> dict:
    """一回合基线策略（goto + 云台指令）→ 交付观测、帧键与摘要、运行清单。"""
    w, ev = synth_event
    spec = EnvSpec("t-bp", 3, w, ev, FLEET, obs_level="L2", physics="kinematic", duration_s=DURATION_S, out_dir=out)
    env = BenchEnv(spec, l2=_l2, **kw)
    obs, _ = env.reset()
    seen = list(obs.items())
    while env.agents:
        acts = {u: {"nav": {"kind": "goto", "target_enu": [0.0, 60.0, 30.0], "speed_mps": 4.0}, "gimbal": {"pitch_deg": -45.0, "yaw_deg": 10.0}}
                for u in obs}
        obs, *_ = env.step(acts)
        seen += list(obs.items())
    man = env.close()
    frames = {}
    for u, m in seen:
        for sid, fs in m["obs"]["sensors"].items():
            for f in fs:
                h = hashlib.sha256()
                for k in ("rgb", "xyz_m", "acc_mps2", "gyro_rps", "pos_enu_m"):
                    x = f.get(k)
                    if x is not None:
                        h.update(x["data"] if k == "rgb" else x if isinstance(x, bytes) else repr(x).encode())
                frames[(u, sid, int(f["seq"]))] = h.hexdigest()
    streams = {u: (out / "agents" / u / "obs.sha256").read_text() for u in env.uavs}
    acts = {u: (out / "agents" / u / "acts.jsonl").read_text() for u in env.uavs}
    return {"frames": frames, "obs": streams, "acts": acts, "truth_l2": _text(out / "truth" / "l2.jsonl"), "manifest": man,
            "t_ns": [m["obs"]["t_ns"] for _, m in seen]}


def _text(p: Path) -> str:
    return p.read_text() if p.is_file() else ""


@pytest.fixture(scope="module")
def base_run(synth_event, tmp_path_factory):
    return _run(synth_event, tmp_path_factory.mktemp("base") / "run")


def test_lockstep_backpressure_blocks_sim_clock_without_rate_change(synth_event, base_run, tmp_path: Path) -> None:
    n_ticks = round(DURATION_S * 10)
    assert "sensorsvc" not in base_run["manifest"]
    runs = {}
    for name, threaded in (("sync", False), ("worker", True)):
        sched = Scheduler(delay_s=DELAY_S, threaded=threaded)
        try:
            runs[name] = _run(synth_event, tmp_path / name, sensorsvc=sched, inflight_cap=1)
        finally:
            sched.close()
        st = sched.stats()["classes"]
        assert st["preview"]["submitted"] == 0 and st["offline"]["submitted"] == 0

    n_frames = len(base_run["frames"])
    cams = sorted(k for k in base_run["frames"] if k[1] == CAM_SENSOR)
    assert cams == [(u, CAM_SENSOR, s) for u in ("uav-01", "uav-02") for s in range(n_ticks + 1)]     # 每 tick 一帧：未降频、无缺漏
    for name, r in runs.items():
        sv = r["manifest"]["sensorsvc"]
        # 背压确实发生：在途上限 1、每个 tick 的到期帧 ≥ 2，因此每个 tick 都收到 641 并按原键重提交
        assert sv["mode"] == "benchmark" and sv["inflight_cap"] == 1 and sv["max_inflight"] == 1, name
        assert sv["ticks"] == sv["blocked_ticks"] == n_ticks + 1, name
        assert sv["rejected_641"] > 0 and sv["resubmits"] == sv["completed"] - (n_ticks + 1), name
        assert sv["completed"] == sv["accepted"] == n_frames == 2 * (2 + 4 * n_ticks), name           # k = 0：相机、GNSS；k ≥ 1 另有 LiDAR、IMU
        # 仿真时钟不受墙钟影响：观测时刻、tick 数一致；墙钟至少增加每帧 50 ms
        assert r["t_ns"] == base_run["t_ns"] and r["manifest"]["n_ticks"] == base_run["manifest"]["n_ticks"] == n_ticks, name
        assert sv["wait_s"] >= DELAY_S * sv["completed"] * 0.95 and sv["backpressure_s"] > 0, name
        assert r["manifest"]["wall_s"] >= sv["wait_s"] - 0.01, name                                   # 等待计入运行墙钟
        # 观测集合与字节只由 Run 规格决定：与无背压运行逐一一致
        assert r["frames"] == base_run["frames"], name
        assert r["obs"] == base_run["obs"] and r["acts"] == base_run["acts"] and r["truth_l2"] == base_run["truth_l2"], name


def test_preview_queue_641_halves_rate_and_recovers() -> None:
    sched = Scheduler(threaded=False)
    made: list[FrameKey] = []

    def produce(batch):
        made.extend(r.key for r in batch)
        return [(r, b"preview") for r in batch]

    ps = PreviewStream(sched, "lab/preview-1", [("uav-01", CAM_SENSOR), ("uav-02", CAM_SENSOR)], produce=produce)
    assert ps.sess.cap == 8 and ps.rate.hz() == 2.0                                  # 在途上限：2 × 预览帧率（每源）
    # 负载高峰：worker 未取批，预览帧堆积至在途上限
    t, res = 0, []
    while len(res) < 7:
        r = ps.pump(t)
        if r is not None:
            res.append((t, r.backpressure))
        t += S // 10
    assert res == [(0, False), (S // 2, False), (S, False), (3 * S // 2, False), (2 * S, True), (3 * S, True), (5 * S, True)]
    assert ps.rate.hz() == 0.5 and ps.dropped == 6                                    # 2 → 1 → 0.5 Hz，下限 0.5 Hz
    assert ps.sess.rejected_641 == 6 and ps.sess.inflight == 8
    # 负载回落：worker 排空队列，此后每轮提交都被接受
    assert sched.drain() == 1 and len(ps.frames()) == 8
    hz = {}
    while t <= 16 * S:
        r = ps.pump(t)
        assert r is None or not r.backpressure
        sched.drain()
        ps.frames()
        hz[t] = ps.rate.hz()
        t += S // 10
    assert hz[round(9.9 * S)] == 0.5 and hz[10 * S] == 1.0 and hz[round(14.9 * S)] == 1.0 and hz[15 * S] == 2.0 and hz[16 * S] == 2.0
    summ = ps.close()
    assert summ["rate_events"] == [(2 * S, "down", 1.0), (3 * S, "down", 0.5), (10 * S, "up", 1.0), (15 * S, "up", 2.0)]
    assert summ["delivered"] == summ["accepted"] == len(made)

    # 恢复期间再遇 641：计时重新开始
    pr = PreviewRate()
    pr.on_backpressure(0)
    pr.on_backpressure(S)
    assert pr.update(6 * S) == 1.0
    pr.on_backpressure(8 * S)
    assert pr.update(12 * S) == 0.5 and pr.update(13 * S) == 1.0 and pr.update(18 * S) == 2.0

    sched.close()

    # 优先级（各级已用 GPU 时间为 0 时）：预览级先于在线级取批；Benchmark 会话超限只返回 641，不改变会话参数；预览会话不能走锁步等待
    sched = Scheduler(threaded=False)
    order: list[str] = []
    bench = sched.open("run-1/0", "benchmark", produce=lambda b: order.extend(r.quality for r in b) or [(r, 1) for r in b], inflight_cap=2)
    prev = sched.open("lab/preview-2", "preview", produce=lambda b: order.extend(r.quality for r in b) or [(r, 1) for r in b], inflight_cap=4)
    r = sched.submit(bench, [Request(FrameKey("uav-01", CAM_SENSOR, s), "online") for s in range(3)])
    assert [q.key.seq for q in r.accepted] == [0, 1] and [(q.key.seq, c) for q, c in r.rejected] == [(2, 641)] and bench.cap == 2
    sched.submit(prev, [Request(FrameKey("uav-01", CAM_SENSOR, 0), "preview")])
    sched.step()
    assert order == ["preview"]
    with pytest.raises(SensorSvcError) as ei:
        wait_all(sched, prev, [])
    assert ei.value.code == 640
    with pytest.raises(SensorSvcError) as ei:
        sched.submit(bench, [Request(FrameKey("uav-01", CAM_SENSOR, 9), "preview")])
    assert ei.value.code == 642
    sched.close()


def test_observation_wall_preview_bypasses_m25_queue(synth_event, base_run, tmp_path: Path) -> None:
    from PIL import Image

    sched = Scheduler(threaded=False)
    got: list[tuple[str, int, int, bytes]] = []
    try:
        r = _run(synth_event, tmp_path / "wall", sensorsvc=sched, wall_preview=True)
        cb = _run(synth_event, tmp_path / "cb", wall_preview=lambda u, seq, t, b: got.append((u, seq, t, b)))
    finally:
        sched.close()
    st = sched.stats()["classes"]
    assert st["preview"]["submitted"] == 0 and st["online"]["submitted"] == r["manifest"]["sensorsvc"]["submitted"] > 0
    assert r["manifest"]["sensorsvc"]["rejected_641"] == 0                         # 缺省在途上限（2 个 tick 的到期帧）下无背压
    seqs = [0, 5, 10]                                                                # 仿真时间 2 Hz
    for u in ("uav-01", "uav-02"):
        files = sorted((tmp_path / "wall" / "preview" / u).glob("*.jpg"))
        assert [int(p.stem) for p in files] == seqs
        for p in files:
            im = Image.open(p)
            assert im.format == "JPEG" and im.size == (480, 270)
    assert r["manifest"]["wall_preview"]["frames"] == {"uav-01": 3, "uav-02": 3}
    assert sorted((u, s) for u, s, _, _ in got) == [(u, s) for u in ("uav-01", "uav-02") for s in seqs]
    assert all(Image.open(io.BytesIO(b)).size == (480, 270) for *_, b in got)
    assert not (tmp_path / "cb" / "preview").exists()
    for x in (r, cb):                                                                # 预览旁路不改变观测流
        assert x["obs"] == base_run["obs"] and x["frames"] == base_run["frames"]
