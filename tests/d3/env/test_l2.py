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
        return RenderOut(rgb, np.zeros((h, w), np.float32), z16, z8, 0.0, 0.0, 0, instance_unocc=z16)


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


def _l2(env: BenchEnv, lidar_proc: bool = True) -> L2Sensors:
    return L2Sensors(world_dir=Path(env.spec.world_dir), tape=env.tape, uavs=env.uavs, seed=env.spec.seed, noise=env.spec.noise,
                     obs_level=env.spec.obs_level, nav=env.nav, mount_t_bs_m=env.cam.mount_t_bs_m, hfov_deg=env.cam.hfov_deg,
                     cfg=L2Config(camera_profile="rgb720-gs", cache_dir=Path(env.spec.out_dir) / "wq", lidar_proc=lidar_proc), renderer=FakeRaster())


def _episode(synth_event, out: Path, obs_level: str = "L2", lidar_proc: bool = True) -> tuple[list[tuple[str, dict]], dict]:
    w, ev = synth_event
    spec = EnvSpec("t-l2", 3, w, ev, FLEET, obs_level=obs_level, physics="kinematic", duration_s=3.0, out_dir=out)
    env = BenchEnv(spec, l2=lambda e: _l2(e, lidar_proc))
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
