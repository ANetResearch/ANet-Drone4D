"""相机档位吞吐基准 `tools/bench/d3/camera_throughput.py`（D3-AC-11、D3-AC-47；M25-AC-014、047）。

前半部分不需要 GPU：测量矩阵（含 `therm512` 单档与标准轨 online 负载）、天气档代理遍、帧时刻、航迹、统计分箱、`nvidia-smi` 采样
解析、ADR-195 独占级别判定、汇总与逐条判定（含标准轨负载缺口标注与 therm512 条款）。
后半部分需要 EGL、nvJPEG 与 Warp CUDA（dmax d3-sensorsvc 容器内运行，本机跳过）：代价代理遍不改变光栅相机的状态与交付像素；在合成
世界与双演员磁带上以真实 Worker 子进程跑一次缩短的 sustained（720p 最坏档、therm512）与 online 测量，核对报告结构、在线守卫零 CPU
编码与落盘；进程内跑一次缩短的标准轨 online（1080p 卷帘 + therm512 + MID-360，同一 Worker），核对三类传感器逐 tick 交付、录制通道
与真值、热成像帧记录与 LiDAR 帧头。
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
def test_plan_matrix_therm512_and_standard_track() -> None:
    m = ct.plan_matrix(["sustained", "online"], ["rgb720-gs", "rgb1080-rs", "therm512"], ["clear", "worst"], [1, 3])
    names = [ct.measurement_name(x) for x in m]
    assert names[:5] == ["sustained-rgb720-gs-clear-x1", "sustained-rgb720-gs-worst-x1", "sustained-rgb1080-rs-clear-x1",
                         "sustained-rgb1080-rs-worst-x1", "sustained-therm512-single-x1"]                  # 热成像与天气无关的单档
    assert names[5:] == ["online-rgb720-gs-worst-x1", "online-rgb720-gs-worst-x3", "online-rgb1080-rs+therm512+mid360-worst-x1",
                         "online-rgb1080-rs+therm512+mid360-worst-x3"]                                     # 标准轨：同一 Worker 内的完整负载
    assert m[-1]["extras"] == ["therm512", "mid360"] and "extras" not in m[5]
    only_cam = ct.plan_matrix(["online"], ["rgb1080-rs"], ["worst"], [1], extras=())
    assert [ct.measurement_name(x) for x in only_cam] == ["online-rgb1080-rs-worst-x1"]
    assert ct.plan_matrix(["sustained"], ["therm512"], ["clear"], [1]) == [{"mode": "sustained", "profile": "therm512", "tier": "single", "runs": 1}]
    assert ct.plan_matrix(["sustained"], ["rgb720-gs"], ["worst", "single"], [1]) == [{"mode": "sustained", "profile": "rgb720-gs",
                                                                                         "tier": "worst", "runs": 1}]
    with pytest.raises(ValueError, match="modes"):
        ct.plan_matrix(["burst"], ["rgb720-gs"], ["clear"], [1])
    with pytest.raises(ValueError, match="extras"):
        ct.plan_matrix(["online"], ["rgb1080-rs"], ["worst"], [1], extras=("radar",))
    with pytest.raises(ValueError, match="profile"):
        ct.plan_matrix(["sustained"], ["therm640"], ["clear"], [1])


def test_thresholds_match_ac11_text() -> None:
    """D3-AC-11 原文：720p 晴天 ≥ 120、最坏 ≥ 80；1080p 晴天 ≥ 60、最坏 ≥ 40；p99 ≤ 50 / 80 ms；RTF ≥ 1.0、3 并发各 ≥ 0.35；显存 ≤ 1.2 GB。"""
    p = ct.PROFILES
    assert p["rgb720-gs"]["fps"] == {"clear": 120.0, "worst": 80.0} and p["rgb720-gs"]["target_fps"] == {"clear": 200.0, "worst": 120.0}
    assert p["rgb1080-rs"]["fps"] == {"clear": 60.0, "worst": 40.0} and p["rgb1080-rs"]["target_fps"] == {"clear": 90.0, "worst": 60.0}
    assert p["rgb720-gs"]["latency_p99_ms"] == 50.0 and p["rgb1080-rs"]["latency_p99_ms"] == 80.0
    assert p["rgb1080-rs"]["rtf_min"] == {1: 1.0, 3: 0.35} and p["rgb720-gs"]["rtf_min"] == {1: 1.0}
    assert p["therm512"]["fps"] == {"single": 300.0} and p["therm512"]["target_fps"] == {"single": 500.0}      # therm512 ≥ 300，应交付 500
    assert ct.STD_PROFILE == "rgb1080-rs" and ct.STD_EXTRAS == ("therm512", "mid360")                          # 3 × 1080 + 3 × 热 + 3 × MID-360
    assert ct.VRAM_MAX_B == 1.2e9


def test_proxy_passes_per_tier() -> None:
    assert ct.proxy_passes("clear") == () and ct.proxy_passes("worst") == ()                   # raster-gl@0.5.0：效果链全部实现
    assert ct.proxy_passes("worst", fx_proxy=False) == ()
    assert ct.proxy_passes("single") == ()
    assert set(ct.TIERS["worst"]) | set(ct.TIERS["clear"]) <= ct.IMPLEMENTED_FX
    with pytest.raises(ValueError):
        ct.proxy_passes("storm")


def test_camera_weather_tiers() -> None:
    from awr.sensorsvc.camera.effects import visible_sigma_per_m

    wc, sc = ct.camera_weather("clear")
    ww, sw = ct.camera_weather("worst")
    assert wc.visibility_m == ct.CLEAR_VIS_M and wc.rain_mmh == 0.0 and sc == "contact"
    assert ww.visibility_m == ct.FOG_VIS_M and ww.rain_mmh == ct.RAIN_MM_H and sw == "map"
    assert visible_sigma_per_m(ww) > visible_sigma_per_m(wc) > 0.0


def test_job_roundtrip() -> None:
    j = ct.JobCfg(mode="online", profile="rgb1080-rs", tier="worst", world="/w", event="/e", out="/o.json", scratch="/s", outputs=("rgb",),
                  extras=("therm512", "mid360"))
    k = ct.JobCfg.from_dict(json.loads(json.dumps(j.as_dict())))
    assert k == j and k.name == "online-rgb1080-rs+therm512+mid360-worst"
    old = {kk: v for kk, v in j.as_dict().items() if kk not in ("extras", "wq_cache", "t_air_k")}       # S09 的作业文件
    assert ct.JobCfg.from_dict(old).extras == ()


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
    p, yaw = f.body(1, 250_000_000)
    p1, _ = f.body(1, 260_000_000)
    v = (p1 - p) / 0.01
    assert np.allclose(v[:2] / np.linalg.norm(v[:2]), [math.cos(yaw), math.sin(yaw)], atol=1e-3)            # 航向沿切线
    assert np.allclose(ct.rot_z(yaw) @ [1.0, 0.0, 0.0], [math.cos(yaw), math.sin(yaw), 0.0])
    az, el, zoom = ct.gimbal_of(f.pose(0, 0)[1])
    assert -math.pi <= az <= math.pi
    assert el < 0 and zoom == 0.0


def test_distortion_tier_and_light_projection() -> None:
    from awr.sensorsvc.camera import CameraModel
    from awr.sensorsvc.camera.proj import build_remap

    cm = CameraModel.from_hfov(64, 48, 60.0, dist=ct.DIST_TIER)
    rt = build_remap(cm)
    rc = rt.render
    c = np.array([rt.map_u[24, 32] - rc.cx, rt.map_v[24, 32] - rc.cy])
    assert c == pytest.approx([32.5 - cm.cx, 24.5 - cm.cy], abs=0.05)                         # 主点附近近似恒等
    r_out = np.hypot(rt.map_u[0, 0] - rc.cx, rt.map_v[0, 0] - rc.cy)
    assert r_out > np.hypot(0.5 - cm.cx, 0.5 - cm.cy)                                         # k1 < 0（桶形）：角点取更外的源像素
    assert rc.width > cm.width and rt.pixel_ratio <= 1.35
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
def _worker(fps: float, *, rtf: float | None = None, p99: float = 30.0, proxy: tuple[str, ...] = (), pid: int = 1,
            sensors: dict[str, float] | None = None) -> dict:
    d = ct.dist([10.0, 20.0])
    return {"pid": pid, "fps": fps, "fps_bins_60s": [fps, fps * 0.98], "rtf": rtf, "latency_ms": {"p99": p99},
            "fx": {"proxy": list(proxy), "proxy_gpu_ms": {p: d for p in proxy}}, "jpeg_kB": {"mean": 300.0, "max": 360.0}, "write_MBps": 40.0,
            "sink": {"raw": {"block_frac": 0.0}, "truth": {"block_frac": 0.001}}, "cpu_encode_calls": 0,
            "stages_ms": {"draw": d, "readback": d}, "sensors": {k: {"latency_ms": {"p99": v}} for k, v in (sensors or {"camera": p99}).items()}}


def _run(mode: str, profile: str, tier: str, n: int, workers: list[dict], level: str = "A", rep: int = 0, vram: float = 900.0,
         extras: tuple[str, ...] = ()) -> dict:
    m = {"mode": mode, "profile": profile, "tier": tier, "runs": n, **({"extras": list(extras)} if extras else {})}
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
    assert a["passed"] and not a["counted"] and a["why"] == ["partial_workload:missing_therm512+mid360"]   # 只测了相机部分
    a = acc["AC11.rtf.rgb1080-rs.x3"]
    assert a["value"] == 0.33 and not a["passed"] and "exclusive_B_pending_user_confirmation" in a["why"]   # 取 3 个并发 Run 中最小者
    a = acc["AC11.latency_p99.rgb720-gs"]
    assert not a["passed"] and a["why"] == ["shared_gpu"]
    assert acc["AC11.vram.rgb1080-rs"]["value"] == round(1100 * 2 ** 20 / 1e9, 3) and acc["AC11.vram.rgb1080-rs"]["passed"]
    assert acc["AC11.vram.rgb1080-rs"]["why"] == ["partial_workload:missing_therm512+mid360"]
    assert acc["AC11.fps.rgb720-gs.worst"]["why"] == ["not_measured"] and acc["AC11.fps.therm512.single"]["why"] == ["not_measured"]
    assert not any("deferred" in w for a in s["acceptance"] for w in a["why"])


def test_summary_standard_track_and_therm512() -> None:
    """标准轨完整负载的测量优先于只含相机部分的测量；时延条款取标准轨中可见光帧的 p99；therm512 持续吞吐对照 300（应交付 500）。"""
    std = ("therm512", "mid360")
    runs = [_run("online", "rgb1080-rs", "worst", 1, [_worker(30.0, rtf=0.5, p99=60.0)], vram=1000.0),
            _run("online", "rgb1080-rs", "worst", 1, [_worker(30.0, rtf=1.1, p99=200.0, sensors={"camera": 75.0, "thermal": 90.0, "lidar": 20.0})],
                 vram=1100.0, extras=std),
            _run("online", "rgb1080-rs", "worst", 3, [_worker(10.0, rtf=r, pid=k, sensors={"camera": 1.0, "thermal": 2.0, "lidar": 3.0})
                                                      for k, r in enumerate((0.4, 0.36, 0.5))], extras=std),
            _run("sustained", "therm512", "single", 1, [_worker(380.0)])]
    s = ct.summarize_runs(runs)
    it = s["items"]["online-rgb1080-rs+therm512+mid360-worst-x1"]
    assert it["workload"] == ["camera", "lidar", "thermal"] and it["latency_p99_ms_by_sensor"] == {"camera": 75.0, "lidar": 20.0, "thermal": 90.0}
    acc = {a["id"]: a for a in s["acceptance"]}
    a = acc["AC11.rtf.rgb1080-rs.x1"]
    assert a["value"] == 1.1 and a["passed"] and a["counted"] and a["why"] == []                # 完整标准轨负载，不再标注 partial
    assert acc["AC11.rtf.rgb1080-rs.x3"]["value"] == 0.36 and acc["AC11.rtf.rgb1080-rs.x3"]["passed"]
    assert acc["AC11.latency_p99.rgb1080-rs"]["value"] == 75.0 and "standard track" in acc["AC11.latency_p99.rgb1080-rs"]["clause"]
    v = acc["AC11.vram.rgb1080-rs"]
    assert v["value"] == round(1100 * 2 ** 20 / 1e9, 3) and v["counted"] and "therm512" in v["clause"]
    t = acc["AC11.fps.therm512.single"]
    assert t["value"] == 380.0 and t["threshold"] == 300.0 and t["passed"] and t["counted"] and t["target"] == 500.0 and not t["target_met"]
    assert acc["AC11.fps_min_bin.therm512.single"]["value"] == round(380.0 * 0.98, 4)
    assert "AC11.latency_p99.therm512" not in acc and not any(k.startswith("AC11.rtf.therm512") for k in acc)
    it, why = ct.online_item(s["items"], "rgb720-gs", 1)
    assert it is None and why is None


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


def test_async_submit_matches_sync_render(fixture_world) -> None:
    """吞吐 Worker 的读回路径：最坏档（畸变、雾、雨、投影阴影图）下连续提交多帧、按序等待的结果与同步 `render` 逐字节相同，
    且每帧有整帧 GPU 计时；读回槽位在 `release` 后归还。"""
    ctx = _gl_ctx()
    from awr.sensorsvc.camera import CameraModel, look_at_R
    from awr.sensorsvc.camera.effects import AeState, frame_params
    from awr.sensorsvc.camera.raster import ActorFrame, RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance
    from awr.sim.events.tape import open_tape

    root, ev = fixture_world
    tape = open_tape(ev)
    cam = RasterCamera(load_appearance(root, tile_m=64.0), ctx=ctx, readback_slots=3)
    try:
        cam.set_actors(tape.actors)
        weather, shadow = ct.camera_weather("worst")
        cm = CameraModel.from_hfov(320, 180, 60.0, dist=ct.DIST_TIER, shadow=shadow)
        p = np.array([-30.0, -20.0, 40.0])
        R = look_at_R(p, np.zeros(3))
        st = tape.actor_states(10 * ct.NS)
        af = ActorFrame(st.ids, st.pos_enu_m, st.yaw_rad)
        fx = frame_params(cm, AeState(), seed=7, uav_no=0, sensor_no=0, frame_index=3, t_mid_s=10.0, weather=weather)
        with cam.ctx:
            ref = cam.render(cm, R, p, fx=fx, actors=af)
            outs = []
            for _ in range(2):
                jobs = [cam.submit(cm, R, p, fx=fx, actors=af, timer=True) for _ in range(3)]
                for j in jobs:
                    o = j.result()
                    outs.append((o, {k: np.array(getattr(o, k)) for k in ("rgb8", "depth_m", "instance", "semantic", "instance_unocc")}))
                    j.release()
        assert fx.shadow_map and fx.rain_mmh == ct.RAIN_MM_H and fx.fog_sigma_m > 0
        assert ref.rgb8.shape == (180, 320, 3) and (ref.instance_unocc > 0).any()
        for o, arrs in outs:
            assert o.gpu_ms is not None and o.gpu_ms > 0.0
            for k, v in arrs.items():
                assert np.array_equal(getattr(ref, k), v), k
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
                  "--profiles", "rgb720-gs,rgb1080-rs,therm512", "--tiers", "worst", "--runs", "1", "--duration-s", "4", "--online-sim-s", "1",
                  "--warmup", "6", "--segment-s", "2", "--online-extras", "none"])
    assert rc in (ct.EXIT_OK, ct.EXIT_FAIL)
    doc = json.loads((out / "camera_throughput.json").read_text())
    assert doc["schema"] == ct.SCHEMA and len(doc["runs"]) == 5
    runs = {r["name"]: r for r in ct.load_runs(out)}
    s = runs["sustained-rgb720-gs-worst-x1-r0"]
    assert s["ok"] and s["exit_codes"] == [0]
    (w,) = s["workers"]
    assert w["frames"] > 10 and w["fps"] > 0 and w["cpu_encode_calls"] == 0 and w["encoder_kind"] == "gpu"
    assert w["encoder"].startswith("nvimgcodec-") and w["camera"]["width"] == 1280
    assert w["fx"]["proxy"] == [] and w["fx"]["implemented"] == sorted(ct.TIERS["worst"]) and w["stages_ms"]["gpu"]["n"] >= w["frames"]
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
    t = runs["sustained-therm512-single-x1-r0"]
    (w,) = t["workers"]
    assert t["ok"] and w["kind"] == "thermal" and w["camera"]["width"] == 640 and w["camera"]["height"] == 512 and w["fx"]["proxy"] == []
    assert w["frames"] > 10 and w["fps"] > 0 and w["cpu_encode_calls"] == 0 and w["encoder"] == "zstd-1/u16le" and w["jpeg_kB"] == {"n": 0}
    assert w["payload_kB"]["mean"] > 0 and w["objects_per_frame"]["max"] >= 1 and w["workload"] == ["thermal"]
    assert w["thermal"]["manifest"]["thermal_table"]["version"] == "thermal_table_v1" and w["thermal"]["env"]["rain_mmh"] == ct.RAIN_MM_H
    assert w["sink"]["raw"]["messages"] >= w["frames"] and w["sink"]["truth"]["messages"] >= w["frames"] and w["sink"]["raw"]["dropped"] == 0
    acc = {a["id"]: a for a in doc["summary"]["acceptance"]}
    assert {"AC11.fps.rgb720-gs.worst", "AC11.latency_p99.rgb1080-rs", "AC11.rtf.rgb1080-rs.x1", "AC11.vram.rgb1080-rs",
            "AC11.fps.therm512.single"} <= set(acc)
    assert acc["AC11.fps.therm512.single"]["value"] == w["fps"] and acc["AC11.fps.therm512.single"]["threshold"] == 300.0
    assert "partial_workload:missing_therm512+mid360" in acc["AC11.rtf.rgb1080-rs.x1"]["why"]


def _fixture_query():
    """LiDAR 世界查询的最小替身（`WarpMeshLidar` 只用 `tiles` 与 `semantic`）：外观夹具的地面与盒子作为单个瓦片。"""
    from types import SimpleNamespace

    sys.path.insert(0, str(REPO / "tests/d3/sensorsvc"))
    from appfix import scene_enu

    V, F, _UV = scene_enu()
    org = np.zeros(3)
    tile = SimpleNamespace(id=0, origin=org, lo=V.min(0), hi=V.max(0), t0=0, nt=len(F), scene=None, V=(V - org).astype(np.float32),
                           F=F.astype(np.uint32))
    return SimpleNamespace(tiles=[tile], semantic=None)


def test_standard_track_online_in_process(fixture_world, tmp_path: Path) -> None:
    """标准轨在线闭环（同一 Worker：3 × rgb1080-rs + 3 × therm512 + 3 × MID-360，最坏档）：每 tick 三类传感器各交付 3 帧；热成像与
    可见光共用 EGL 上下文；LiDAR 经 Warp CUDA；观测与真值写入录制；在线守卫内零 CPU JPEG 编码；报告含分传感器时延与负载构成。"""
    _gl_ctx().release()
    pytest.importorskip("mcap")
    wm = pytest.importorskip("awr.sensorsvc.lidar.warp_mesh")
    from awr.sensorsvc.camera.encode import cpu_encode_calls, gpu_encoder_available, online_path

    if not gpu_encoder_available():
        pytest.skip("[gpu] nvJPEG (nvImageCodec / CUDA) unavailable")
    pytest.importorskip("warp")
    if not wm.warp_cuda_available():
        pytest.skip("[gpu] no CUDA device for warp")
    root, ev = fixture_world
    job = ct.JobCfg(mode="online", profile="rgb1080-rs", tier="worst", world=str(root), event=str(ev), out=str(tmp_path / "w.json"),
                    scratch=str(tmp_path / "rec"), sim_s=0.5, warmup=3, segment_s=0.3, keep_output=True, extras=ct.STD_EXTRAS)
    import resource

    with online_path():
        c0 = cpu_encode_calls()
        w = ct.Worker(job, lidar_query=_fixture_query())
        try:
            assert w.th.cam.ctx is w.cam.ctx                                                      # 热成像与可见光共用上下文
            w.warmup()
            ru = resource.getrusage(resource.RUSAGE_SELF)
            res = w.online()
            doc = ct.worker_report(w, res, cpu_calls=cpu_encode_calls() - c0, ru=(ru, ru, ru), wall=res["wall_s"])
        finally:
            sink = w.close()
    assert res["ticks"] == 5 and res["frames_by_sensor"] == {"camera": 15, "thermal": 15, "lidar": 15} and res["rtf"] > 0
    assert doc["cpu_encode_calls"] == 0 and doc["workload"] == ["camera", "lidar", "thermal"] and doc["extras"] == ["therm512", "mid360"]
    sen = doc["sensors"]
    assert sen["thermal"]["latency_ms"]["n"] == 15 and sen["lidar"]["points_per_frame"]["mean"] > 100 and sen["camera"]["latency_ms"]["n"] == 15
    assert sen["thermal"]["objects_per_frame"]["max"] >= 1                                         # 热成像可见度归约有演员
    assert doc["lidar"]["backend"] and doc["lidar"]["beta_905_per_m"] > 0 and doc["thermal"]["env"]["fog_visibility_km"] == ct.FOG_VIS_M / 1000
    assert ct.gpu_time({**doc, "sim_s": res["sim_s"]}) > ct.gpu_time({**doc, "sim_s": res["sim_s"], "sensors": {}})   # 计入热成像 GPU 时间
    assert sink["raw"]["dropped"] == 0 and sink["truth"]["dropped"] == 0
    from mcap.reader import make_reader

    from awr.bench.recorder.channels import decode_thermal_message
    from awr.bench.recorder.lidar import unpack_lidar_frame
    from awr.bench.recorder.truth import unpack_camera_gt

    topics: dict[str, int] = {}
    therm, lid, tgt = [], [], []
    for f in sorted(Path(sink["root"]).rglob("*.mcap")):
        with f.open("rb") as fh:
            for _sch, ch, msg in make_reader(fh).iter_messages():
                topics[ch.topic] = topics.get(ch.topic, 0) + 1
                if "/thermal/" in ch.topic and len(therm) < 2:
                    therm.append(decode_thermal_message(msg.data, decode=True))
                elif ch.topic.startswith("/uav/") and "/lidar/" in ch.topic and len(lid) < 2:
                    lid.append(unpack_lidar_frame(msg.data))
                elif ch.topic.startswith("/eval/hidden/camera/") and ch.topic.endswith("/therm0") and len(tgt) < 2:
                    tgt.append(unpack_camera_gt(msg.data))
    n = 3 + 2 + 5 * 3                                                                              # 预热 3 帧 + 2 次 LiDAR 预热批 + 5 tick
    assert topics["/uav/uav-01/thermal/therm0"] >= 5 and topics["/uav/uav-03/lidar/lidar0"] >= 5 and topics["/eval/hidden/lidar/uav-02/lidar0"] >= 5
    assert topics["/uav/uav-01/camera/eo/image"] >= 5 and sum(v for k, v in topics.items() if "/thermal/" in k) <= n
    assert therm[0]["temp_ck"].shape == (512, 640) and therm[0]["temp_ck"].dtype == np.uint16
    hdr, pts = lid[0]
    assert hdr["slots_per_frame"] == 20000 and hdr["point_count"] == len(pts) > 0 and hdr["mount_nominal"]["rpy_deg"] == [180.0, 0.0, 0.0]
    rec, blocks = tgt[0]
    assert rec["kind"] == "thermal" and "temp_ck.u16.zst" not in blocks and {"instance.u16.zst", "semantic.u8.zst"} <= set(blocks)
