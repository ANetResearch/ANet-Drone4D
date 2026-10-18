"""相机档位吞吐基准 `tools/bench/d3/camera_throughput.py`（D3-AC-11、D3-AC-47；M25-AC-014、047）。

前半部分不需要 GPU：测量矩阵、天气档代理遍、帧时刻、统计分箱、`nvidia-smi` 采样解析、ADR-195 独占级别判定、汇总与逐条判定。
后半部分需要 EGL 与 nvJPEG（dmax d3-sensorsvc 容器内运行，本机跳过）：代价代理遍不改变光栅相机的状态与交付像素；在合成世界与
单演员磁带上以真实 Worker 子进程跑一次缩短的 sustained 与 online 测量，核对报告结构、在线守卫零 CPU 编码与落盘。
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("camera_throughput", REPO / "tools/bench/d3/camera_throughput.py")
ct = importlib.util.module_from_spec(_spec)
sys.modules["camera_throughput"] = ct
_spec.loader.exec_module(ct)

PMON = """# gpu         pid   type     sm    mem    enc    dec    jpg    ofa     fb   ccpm    command
# Idx           #    C/G      %      %      %      %      %      %     MB     MB    name
    0    2262127     C      -      -      -      -      -      -  30114      0    sglang::schedul
    0    3353453   C+G     41     12      -      -      -      -   2165      0    python3
    0          -     -      -      -      -      -      -      -      -      -    -
"""


# ---------------------------------------------------------------------------------------------------------------- 配置与矩阵
def test_plan_matrix_and_deferred_profile() -> None:
    m = ct.plan_matrix(["sustained", "online"], ["rgb720-gs", "rgb1080-rs"], ["clear", "worst"], [1, 3])
    names = [ct.measurement_name(x) for x in m]
    assert names[:4] == ["sustained-rgb720-gs-clear-x1", "sustained-rgb720-gs-worst-x1", "sustained-rgb1080-rs-clear-x1",
                         "sustained-rgb1080-rs-worst-x1"]
    assert names[4:] == ["online-rgb720-gs-worst-x1", "online-rgb720-gs-worst-x3", "online-rgb1080-rs-worst-x1", "online-rgb1080-rs-worst-x3"]
    with pytest.raises(ValueError, match="S20"):
        ct.plan_matrix(["sustained"], ["therm512"], ["clear"], [1])
    with pytest.raises(ValueError, match="modes"):
        ct.plan_matrix(["burst"], ["rgb720-gs"], ["clear"], [1])


def test_thresholds_match_ac11_text() -> None:
    """D3-AC-11 原文：720p 晴天 ≥ 120、最坏 ≥ 80；1080p 晴天 ≥ 60、最坏 ≥ 40；p99 ≤ 50 / 80 ms；RTF ≥ 1.0、3 并发各 ≥ 0.35；显存 ≤ 1.2 GB。"""
    p = ct.PROFILES
    assert p["rgb720-gs"]["fps"] == {"clear": 120.0, "worst": 80.0} and p["rgb720-gs"]["target_fps"] == {"clear": 200.0, "worst": 120.0}
    assert p["rgb1080-rs"]["fps"] == {"clear": 60.0, "worst": 40.0} and p["rgb1080-rs"]["target_fps"] == {"clear": 90.0, "worst": 60.0}
    assert p["rgb720-gs"]["latency_p99_ms"] == 50.0 and p["rgb1080-rs"]["latency_p99_ms"] == 80.0
    assert p["rgb1080-rs"]["rtf_min"] == {1: 1.0, 3: 0.35} and p["rgb720-gs"]["rtf_min"] == {1: 1.0}
    assert ct.VRAM_MAX_B == 1.2e9


def test_proxy_passes_per_tier() -> None:
    assert ct.proxy_passes("clear") == ("fog", "contact_shadow", "distortion")
    assert ct.proxy_passes("worst") == ("rain", "fog", "shadow_map", "contact_shadow", "distortion")
    assert ct.proxy_passes("worst", fx_proxy=False) == ()
    assert set(ct.TIERS["worst"]) - set(ct.TIERS["clear"]) == {"rain", "shadow_map"}
    with pytest.raises(ValueError):
        ct.proxy_passes("storm")


def test_job_roundtrip() -> None:
    j = ct.JobCfg(mode="online", profile="rgb1080-rs", tier="worst", world="/w", event="/e", out="/o.json", scratch="/s", outputs=("rgb",))
    k = ct.JobCfg.from_dict(json.loads(json.dumps(j.as_dict())))
    assert k == j and k.name == "online-rgb1080-rs-worst"


# ---------------------------------------------------------------------------------------------------------------- 时刻与航迹
def test_frame_time_grid() -> None:
    """采集窗口结束于第 seq + 1 个 tick 边界；曝光中点 = 窗口终点 − (曝光 + 读出) / 2。"""
    for seq in (0, 1, 7, 12345):
        t = ct.frame_t_mid(seq, 1_000_000, 20_000_000)
        assert t + (1_000_000 + 20_000_000) // 2 == (seq + 1) * ct.TICK_NS
    assert ct.frame_t_mid(0, 8_000_000, 0) == 96_000_000
    span = 3_000_000_000
    assert ct.event_time(500, 30 * ct.NS, span) == 30 * ct.NS + 500
    assert ct.event_time(span + 7, 30 * ct.NS, span) == 30 * ct.NS + 7


def test_fleet_orbits_and_look_at() -> None:
    f = ct.Fleet(np.array([10.0, -5.0, 0.0]), lambda x, y: 2.0, 3)
    for i in range(3):
        p0, R = f.pose(i, 0)
        p1, _ = f.pose(i, 100_000_000)
        assert np.linalg.norm(p1 - p0) / 0.1 == pytest.approx(8.0, rel=1e-3)                  # 8 m/s
        assert p0[2] == pytest.approx(2.0 + 35.0 + 10.0 * i)
        assert math.hypot(p0[0] - 10.0, p0[1] + 5.0) == pytest.approx(40.0 + 15.0 * i)
        d = f.target - p0
        assert R[:, 0] @ (d / np.linalg.norm(d)) == pytest.approx(1.0, abs=1e-9)             # 光轴注视环绕中心
    az, el, zoom = ct.gimbal_of(f.pose(0, 0)[1])
    assert -math.pi <= az <= math.pi
    assert el < 0 and zoom == 0.0


def test_distortion_lut_and_light_projection() -> None:
    from awr.sensorsvc.camera import CameraModel

    cm = CameraModel.from_hfov(64, 48, 60.0)
    lut = ct.distortion_lut(cm)
    assert lut.shape == (48, 64, 2) and lut.dtype == np.float32
    c = lut[24, 32]
    assert c == pytest.approx([32.5, 24.5], abs=0.05)                                         # 主点附近近似恒等
    r_out = np.hypot(*(lut[0, 0] - [cm.cx, cm.cy]))
    assert r_out > np.hypot(0.5 - cm.cx, 0.5 - cm.cy)                                         # k1 < 0（桶形）：角点取更外的源像素
    P = ct.ortho_reversed_z(300.0, 1.0, 1000.0)
    near, far = P @ [0, 0, 1.0, 1], P @ [0, 0, 1000.0, 1]
    assert near[2] == pytest.approx(1.0) and far[2] == pytest.approx(0.0) and (P @ [150.0, 0, 5, 1])[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------------------------------------------- 统计
def test_dist_pct_and_bins() -> None:
    d = ct.dist([1.0, 2.0, 3.0, 4.0, 100.0])
    assert d["n"] == 5 and d["p50"] == 3.0 and d["max"] == 100.0 and ct.dist([]) == {"n": 0}
    t = [0.05 + 0.1 * k for k in range(1200)]                                                # 120 s 内 10 帧/s
    assert ct.fps_bins(t, 0.0, 120.0) == [10.0, 10.0]
    assert ct.fps_bins(t, 0.0, 20.0) == []                                                    # 不足半箱的残箱不计
    assert ct.fps_bins(t, 0.0, 90.0) == [10.0, 10.0]


# ---------------------------------------------------------------------------------------------------------------- 同卡负载与独占级别（ADR-195）
def test_parse_pmon_and_apps() -> None:
    rows = ct.parse_pmon(PMON.splitlines())
    assert [r["pid"] for r in rows] == [2262127, 3353453]
    assert rows[0]["sm"] is None and rows[0]["fb"] == 30114 and rows[0]["command"] == "sglang::schedul"
    assert rows[1]["type"] == "C+G" and rows[1]["sm"] == 41 and rows[1]["fb"] == 2165
    csv = "GPU-aaa, 11, 300\nGPU-bbb, 12, 400\nGPU-aaa, 13, [N/A]\n"
    assert ct.parse_apps(csv, "GPU-aaa") == [{"pid": 11, "used_mib": 300}, {"pid": 13, "used_mib": None}]


def test_exclusivity_levels() -> None:
    rows = ct.parse_pmon(PMON.splitlines())
    own = {3353453}
    mine = [r for r in rows if r["pid"] in own]
    assert ct.exclusivity(mine, [], own, free_mib_min=40000, need_mib=2200, pmon_ok=True)["level"] == "A"
    b = ct.exclusivity(rows, [{"pid": 2262127, "used_mib": 30114}], own, free_mib_min=16000, need_mib=2200, pmon_ok=True)
    assert b["level"] == "B" and b["foreign_pids"] == [2262127] and b["foreign_table"][0]["fb_mib_max"] == 30114
    assert ct.exclusivity(rows, [], own, free_mib_min=5000, need_mib=2200, pmon_ok=True)["level"] == "shared"   # 余量 < 需求 + 4 GB
    busy = [*rows, {"pid": 2262127, "sm": 3, "mem": 0, "fb": 30114}]
    s = ct.exclusivity(busy, [], own, free_mib_min=16000, need_mib=2200, pmon_ok=True)
    assert s["level"] == "shared" and s["foreign_active_samples"] == 1
    assert ct.exclusivity(rows, [], {999}, free_mib_min=16000, need_mib=0, pmon_ok=True)["level"] == "unknown"  # PID 命名空间不一致
    assert ct.exclusivity([], [], own, free_mib_min=None, need_mib=0, pmon_ok=False)["level"] == "unknown"


# ---------------------------------------------------------------------------------------------------------------- 汇总与判定
def _worker(fps: float, *, rtf: float | None = None, p99: float = 30.0, proxy: tuple[str, ...] = (), pid: int = 1) -> dict:
    d = ct.dist([10.0, 20.0])
    return {"pid": pid, "fps": fps, "fps_bins_60s": [fps, fps * 0.98], "rtf": rtf, "latency_ms": {"p99": p99},
            "fx": {"proxy": list(proxy), "proxy_gpu_ms": {p: d for p in proxy}}, "jpeg_kB": {"mean": 300.0, "max": 360.0}, "write_MBps": 40.0,
            "sink": {"raw": {"block_frac": 0.0}, "truth": {"block_frac": 0.001}}, "cpu_encode_calls": 0,
            "stages_ms": {"draw": d, "readback": d}}


def _run(mode: str, profile: str, tier: str, n: int, workers: list[dict], level: str = "A", rep: int = 0, vram: float = 900.0) -> dict:
    m = {"mode": mode, "profile": profile, "tier": tier, "runs": n}
    return {"name": f"{ct.measurement_name(m)}-r{rep}", "measurement": m, "repeat": rep, "ok": True, "workers": workers,
            "vram": [{"pid": w["pid"], "mib": vram, "method": "pmon_fb"} for w in workers], "gpu_load": {"level": level}}


def test_summary_acceptance_counted_and_median() -> None:
    runs = [_run("sustained", "rgb720-gs", "clear", 1, [_worker(f)], rep=k) for k, f in enumerate((130.0, 118.0, 125.0))]
    runs += [_run("sustained", "rgb1080-rs", "worst", 1, [_worker(45.0, proxy=("rain", "shadow_map"))]),
             _run("online", "rgb1080-rs", "worst", 1, [_worker(30.0, rtf=1.2, p99=70.0)], vram=1100.0),
             _run("online", "rgb1080-rs", "worst", 3, [_worker(10.0, rtf=r, p99=200.0, pid=k) for k, r in enumerate((0.4, 0.33, 0.5))],
                  level="B"),
             _run("online", "rgb720-gs", "worst", 1, [_worker(30.0, rtf=0.9, p99=60.0)], level="shared")]
    s = ct.summarize_runs(runs)
    it = s["items"]["sustained-rgb720-gs-clear-x1"]
    assert it["fps"] == 125.0 and it["repeats"] == 3 and it["levels"] == ["A"]                 # 3 次取中位（ADR-033）
    acc = {a["id"]: a for a in s["acceptance"]}
    a = acc["AC11.fps.rgb720-gs.clear"]
    assert a["passed"] and a["counted"] and a["target"] == 200.0 and not a["target_met"]
    a = acc["AC11.fps.rgb1080-rs.worst"]
    assert a["passed"] and not a["counted"] and a["why"] == ["fx_proxy:rain,shadow_map"]
    a = acc["AC11.latency_p99.rgb1080-rs"]
    assert a["value"] == 70.0 and a["passed"] and a["counted"]
    a = acc["AC11.rtf.rgb1080-rs.x1"]
    assert a["passed"] and not a["counted"] and "partial_workload:therm512+mid360_by_S20" in a["why"]
    a = acc["AC11.rtf.rgb1080-rs.x3"]
    assert a["value"] == 0.33 and not a["passed"] and "exclusive_B_pending_user_confirmation" in a["why"]   # 取 3 个并发 Run 中最小者
    a = acc["AC11.latency_p99.rgb720-gs"]
    assert not a["passed"] and a["why"] == ["shared_gpu"]
    assert acc["AC11.vram.rgb1080-rs"]["value"] == round(1100 * 2 ** 20 / 1e9, 3) and acc["AC11.vram.rgb1080-rs"]["passed"]
    assert acc["AC11.fps.rgb720-gs.worst"]["why"] == ["not_measured"] and acc["AC11.fps.therm512"]["why"] == ["deferred:S20"]


def test_gpu_time_per_sim_second() -> None:
    w = {"sim_s": 10.0, "stages_ms": {"draw": {"n": 300, "mean": 8.0}, "proxy_gpu": {"n": 300, "mean": 2.0}}}
    assert ct.gpu_time(w) == 0.3                                                              # 300 帧 × 10 ms / 10 s
    assert ct.gpu_time({"stages_ms": {"draw": {"n": 3, "mean": 1.0}}}) is None                 # sustained 无仿真时长


def test_write_summary_exit_semantics(tmp_path: Path) -> None:
    runs = [_run("sustained", "rgb720-gs", "clear", 1, [_worker(130.0)])]
    doc = ct.write_summary(tmp_path, runs, {"protocol": {"repeats": 1}})
    assert doc["schema"] == ct.SCHEMA and doc["summary"]["passed"] and not doc["summary"]["seal_ready"]
    assert json.loads((tmp_path / "camera_throughput.json").read_text())["runs"] == [runs[0]["name"]]
    bad = [_run("sustained", "rgb720-gs", "clear", 1, [_worker(100.0)])]
    assert not ct.write_summary(tmp_path, bad, {})["summary"]["passed"]


# ---------------------------------------------------------------------------------------------------------------- GPU（dmax）
def _gl_ctx():
    moderngl = pytest.importorskip("moderngl")
    pytest.importorskip("PIL")
    try:
        return moderngl.create_standalone_context(backend="egl", require=450)
    except Exception as e:                                   # 无 EGL 设备
        pytest.skip(f"[gpu] EGL unavailable: {e}")


def _tape(d: Path, content_version: str, n_t: int = 600) -> None:
    """2 名演员（行人、车辆）在原点附近往返行走，60 s。"""
    from awr.sim.events import tape as tp

    f = np.zeros((n_t, 2), tp.RECORD)
    f["container"] = tp.NO_CONTAINER
    f["state"][:, :] = 1
    k = np.arange(n_t)
    f["x_mm"][:, 0] = (5000 + 3000 * np.sin(k / 50.0)).astype(np.int64)
    f["x_mm"][:, 1] = -8000
    f["y_mm"][:, 1] = (2000 * np.cos(k / 80.0)).astype(np.int64)
    h64 = "0" * 64
    hd = {"schema": "drone4d.tape.v1", "event_id": "ev-tput", "event_version": 1, "event_sha256": h64, "mode": "exogenous", "seed": 0,
          "tick_hz": 10, "n_ticks": n_t, "duration_ms": n_t * 100, "record": tp.record_spec(),
          "codec": {"name": "col-delta-zstd@1", "block_ticks": 100},
          "actors": [{"idx": 0, "id": "person_A", "class": "person", "scripted": True, "profile": "person", "size_mm": [500, 300, 1750],
                      "appearance": {"asset": "person-proxy@1"}},
                     {"idx": 1, "id": "car_B", "class": "vehicle", "scripted": True, "profile": "vehicle", "size_mm": [4500, 1800, 1500],
                      "appearance": {"asset": "car-proxy@1"}}],
          "world": {"id": "app-fixture", "content_version": content_version, "raycast_sha256": h64, "navmesh_sha256": {"person": h64},
                    "anchors_sha256": h64}, "environment": {}, "env_track": [], "comms": {}, "anim": None, "realized": None}
    h = tp.write_tape(d / "tape.bin", hd, f, n_scripted=2)
    (d / "tape.json").write_text(json.dumps({"content_sha256": h["content_sha256"]}))


@pytest.fixture(scope="module")
def fixture_world(tmp_path_factory) -> tuple[Path, Path]:
    pytest.importorskip("zstandard")
    sys.path.insert(0, str(REPO / "tests/d3/sensorsvc"))
    from appfix import write_world

    root = write_world(tmp_path_factory.mktemp("w") / "app-fixture")
    (root / "world.json").write_text(json.dumps({"contentVersion": "abc123def456"}))
    ev = tmp_path_factory.mktemp("ev") / "v1"
    ev.mkdir()
    _tape(ev, "abc123def456")
    return root, ev


def test_proxy_passes_leave_renderer_state_intact(fixture_world) -> None:
    """代理遍在同一上下文执行后，下一次渲染的全部输出与未执行代理遍时逐字节相同；各遍均有 GPU 计时。"""
    ctx = _gl_ctx()
    from awr.sensorsvc.camera import CameraModel, look_at_R
    from awr.sensorsvc.camera.raster import ActorFrame, RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance
    from awr.sim.events.tape import open_tape

    root, ev = fixture_world
    tape = open_tape(ev)
    cam = RasterCamera(load_appearance(root, tile_m=64.0), ctx=ctx)
    try:
        cam.set_actors(tape.actors)
        cm = CameraModel.from_hfov(320, 180, 60.0)
        p = np.array([-30.0, -20.0, 40.0])
        R = look_at_R(p, np.zeros(3))
        st = tape.actor_states(10 * ct.NS)
        af = ActorFrame(st.ids, st.pos_enu_m, st.yaw_rad)
        with cam.ctx:
            ref = cam.render(cm, R, p, actors=af)
            px = ct.FxProxy(cam, cm, ct.proxy_passes("worst"))
            ms = px.run(R, p, af, 3, np.zeros(3))
            again = cam.render(cm, R, p, actors=af)
            px.release()
        assert set(ms) == set(ct.proxy_passes("worst")) and all(v >= 0.0 for v in ms.values()) and sum(ms.values()) > 0.0
        assert px.caster_counts == [2]
        for k in ("rgb8", "depth_m", "instance", "semantic", "instance_unocc"):
            assert np.array_equal(getattr(ref, k), getattr(again, k)), k
        assert (ref.instance_unocc > 0).any()
    finally:
        cam.release()


def test_end_to_end_short_run(fixture_world, tmp_path: Path) -> None:
    """缩短的 sustained（720p 最坏档）与 online（1080p 卷帘，3 机）测量：真实 Worker 子进程、nvJPEG、录制落盘与汇总。"""
    _gl_ctx().release()
    pytest.importorskip("mcap")
    from awr.sensorsvc.camera.encode import gpu_encoder_available

    if not gpu_encoder_available():
        pytest.skip("[gpu] nvJPEG (nvImageCodec / CUDA) unavailable")
    root, ev = fixture_world
    out = tmp_path / "out"
    rc = ct.main(["run", "--world", str(root), "--event", str(ev), "--out", str(out), "--no-lock", "--modes", "sustained,online",
                  "--profiles", "rgb720-gs,rgb1080-rs", "--tiers", "worst", "--runs", "1", "--duration-s", "4", "--online-sim-s", "1",
                  "--warmup", "6", "--segment-s", "2"])
    assert rc in (ct.EXIT_OK, ct.EXIT_FAIL)
    doc = json.loads((out / "camera_throughput.json").read_text())
    assert doc["schema"] == ct.SCHEMA and len(doc["runs"]) == 4
    runs = {r["name"]: r for r in ct.load_runs(out)}
    s = runs["sustained-rgb720-gs-worst-x1-r0"]
    assert s["ok"] and s["exit_codes"] == [0]
    (w,) = s["workers"]
    assert w["frames"] > 10 and w["fps"] > 0 and w["cpu_encode_calls"] == 0 and w["encoder_kind"] == "gpu"
    assert w["encoder"].startswith("nvimgcodec-") and w["camera"]["width"] == 1280
    assert w["fx"]["proxy"] == list(ct.proxy_passes("worst")) and w["fx"]["proxy_gpu_ms"]["shadow_map"]["n"] >= w["frames"]
    assert w["objects_per_frame"]["max"] >= 1                                                 # 可见度归约有演员
    assert w["sink"]["raw"]["messages"] >= 2 * w["frames"] and w["sink"]["truth"]["messages"] >= w["frames"]   # 图像 + 深度、真值
    assert w["sink"]["raw"]["dropped"] == 0 and w["sink"]["raw"]["segments"] >= 2 and w["sink"]["bytes"] > 0
    assert not any((out / "rec").rglob("*.mcap"))                                            # 轮转段关闭后删除
    o = runs["online-rgb1080-rs-worst-x1-r0"]
    (w,) = o["workers"]
    assert o["ok"] and w["ticks"] == 10 and w["frames"] == 30 and w["rtf"] > 0 and w["camera"]["readout_ms"] == 20.0
    assert w["latency_ms"]["n"] == 30 and w["latency_ms"]["p99"] >= w["latency_ms"]["p50"] > 0
    assert o["vram"][0]["mib"] and o["vram"][0]["method"] in ("pmon_fb", "compute_apps", "device_delta")
    assert o["gpu_load"]["level"] in ("A", "B", "shared", "unknown")
    ids = {a["id"] for a in doc["summary"]["acceptance"]}
    assert {"AC11.fps.rgb720-gs.worst", "AC11.latency_p99.rgb1080-rs", "AC11.rtf.rgb1080-rs.x1", "AC11.vram.rgb1080-rs"} <= ids
