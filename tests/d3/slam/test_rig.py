"""rig.json、FAST-LIVO2 / FAST-LIO 配置生成与预滚窗口（M25 §6.11.3、§6.11.4 C4、C5、C8–C11；M26-FR-004、M26-AC-022；
AWR-05 §5.4.6 SLAM 导出合同与初始化条款；D3-AC-13）。

1. rig.json 由 `p600_mid360_inv` 生成：`extrinsic_T/R`、`Rcl/Pcl` 与 r1 闭环配置一致；内置 IMU 在机体系的位姿与 L2 交付的
   `lidar0_imu` 挂载一致；slam_cam 全局快门、无云台、固定内参与曝光；时间约定为 0；噪声参数与参数表逐项一致（C11）。
2. livo.yaml、camera.yaml、fastlio.yaml 只由 rig.json 推出：外参一致、`exposure_time_init = 0`、`img_time_offset = 0`、
   `cam_cx = cx − 0.5`、`cam_cy = cy − 0.5`、`gravity_align_en` 随出生方式；结构与 r1 / 上游配置相同，slam_eval 按行解析可读写。
3. 负向用例：yaml 篡改（642）、rig 不满足 SLAM 条件（649）、窗口不足或不静止（642）。
4. 预滚：`[−t_pre, 0)` 位于事件零点之前（缺省 2 s，下限 0.5 s）；地面起飞为静止窗口、空中出生为悬停保持窗口（末速度 ≤ 0.1 m/s）；
   预滚期间不调用 Agent、不交付观测，k = 0 观测时刻为 0；运动学档预滚不改变 Agent 侧观测与动作流；Fast 档窗口满足 C4、C5、C8。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

from awr.phys.adapter import TierCommand, VehicleStatus
from awr.phys.kinematic import GEAR_Z_M, KinematicTier
from awr.sensorsvc.errors import SensorSvcError
from awr.sensorsvc.slam import rig as RIG
from awr.sensorsvc.slam.rig import (
    G,
    cam_lidar_extrinsics,
    camera_yaml,
    check_init_window,
    check_slam_configs,
    check_slam_rig,
    fastlio_yaml,
    imu_lidar_extrinsics,
    livo_yaml,
    load_rig,
    require_init_window,
    rig_for_vehicle,
    write_slam_configs,
)
from awr.sim.catalog.loader import Catalog, imu_noise_tier

ROOT = Path(__file__).resolve().parents[3]
VID = "p600_mid360_inv"
R1_SIM = ROOT / ".cache/research/d3/r1-fast-livo2/results/sims/seed1_realistic"
FASTLIO_MID360 = ROOT / "refs/lidar/FAST_LIO/config/mid360.yaml"

# r1 闭环（sim_livo_bag.py 生成的 livo.yaml、camera.yaml；与 tests/d3/catalog/test_overlay.py 同源）
R1_EXTRINSIC_T = [-0.011, -0.02329, 0.04412]
R1_EXTRINSIC_R = [1, 0, 0, 0, 1, 0, 0, 0, 1]
R1_RCL = [0.0, 1.0, 0.0, -0.70710678, 0.0, 0.70710678, 0.70710678, 0.0, 0.70710678]
R1_PCL = [0.0, 0.08485281, -0.08485281]
R1_CAM = {"cam_width": 640, "cam_height": 512, "cam_cx": 319.5, "cam_cy": 255.5}
R1_FIXED = {("preprocess", "lidar_type"): 1, ("preprocess", "scan_line"): 4, ("preprocess", "blind"): 0.5,
            ("preprocess", "filter_size_surf"): 0.3, ("lio", "voxel_size"): 1.0, ("imu", "imu_int_frame"): 30, ("imu", "acc_cov"): 0.5,
            ("imu", "gyr_cov"): 0.3}

# Env 夹具：合成世界上的合成事件（tests/d3/env/conftest.py 的 synth_event）
_spec = importlib.util.spec_from_file_location("d3_env_fixtures", ROOT / "tests/d3/env/conftest.py")
_env_fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_env_fix)
synth_event = _env_fix.synth_event
sys.path.insert(0, str(ROOT / "tests" / "d3" / "env"))
sys.path.insert(0, str(ROOT / "tools" / "bench" / "d3"))


@pytest.fixture(scope="module")
def overlay():
    return Catalog.load(ROOT / "vehicles", d3=True).overlay


@pytest.fixture(scope="module")
def rig_ground() -> dict:
    return rig_for_vehicle(VID, vehicles_dir=ROOT / "vehicles", uav="uav-01", start="ground")


@pytest.fixture(scope="module")
def rig_pose() -> dict:
    return rig_for_vehicle(VID, vehicles_dir=ROOT / "vehicles", uav="uav-02", start="pose")


def _flat(a) -> list[float]:
    return [float(x) for x in np.asarray(a, np.float64).reshape(-1)]


# ---------------------------------------------------------------------------------------------------------------- rig.json
def test_rig_from_variant_matches_catalog_and_r1(overlay, rig_ground: dict) -> None:
    r = rig_ground
    check_slam_rig(r)
    assert r["schema"] == "drone4d.rig.v1" and r["vehicle_ref"] == VID and r["livo_params_version"] == "r1-2026-10"
    v = overlay.variant(VID)
    # LiDAR：倒装挂载、4 线、20,000 槽位、offset_time = i · 5000 ns
    lid = v.sensor("lidar0")
    assert r["lidar"]["T_base_lidar"]["xyz_m"] == list(lid.xyz_m) == [0.0, 0.0, -0.08]
    assert r["lidar"]["T_base_lidar"]["rpy_deg"] == [180.0, 0.0, 0.0] and r["lidar"]["mount_preset"] == "inverted_mapping"
    assert (r["lidar"]["scan_line"], r["lidar"]["slots_per_frame"], r["lidar"]["point_dt_ns"]) == (4, 20000, 5000)
    assert r["lidar"]["livo_blind_m"] == 0.5 and r["lidar"]["sensor_blind_m"] == 0.1
    # LiDAR → IMU（FAST-LIVO2 extrinsic_T/R）与 r1 一致；倒装不改变
    R_IL, t_IL = imu_lidar_extrinsics(r)
    assert _flat(t_IL) == R1_EXTRINSIC_T and _flat(R_IL) == [float(x) for x in R1_EXTRINSIC_R]
    # LiDAR → 相机光学帧（Rcl、Pcl）复现 r1
    R_CL, P_CL = cam_lidar_extrinsics(r)
    assert np.allclose(_flat(R_CL), R1_RCL, atol=1e-8) and np.allclose(P_CL, R1_PCL, atol=1e-8)
    # 内置 IMU 在机体系的位姿：与目录 rig（lidar0_imu）及 L2 交付挂载（MID360_IMU_IN_LIDAR 杆臂）一致
    from awr.sensorsvc.imu import MID360_IMU_IN_LIDAR

    imu = v.sensor("lidar0_imu")
    assert np.allclose(r["imu"]["T_base_imu"]["t_m"], imu.xyz_m, atol=1e-12) and np.allclose(r["imu"]["T_base_imu"]["R"], imu.R, atol=1e-12)
    R_BL = np.asarray(lid.R)
    assert np.allclose(np.asarray(lid.xyz_m) + R_BL @ np.asarray(MID360_IMU_IN_LIDAR), r["imu"]["T_base_imu"]["t_m"], atol=1e-12)
    T_BL = np.eye(4)
    T_BL[:3, :3], T_BL[:3, 3] = R_BL, lid.xyz_m
    T_BI, T_IL = np.eye(4), np.eye(4)
    T_BI[:3, :3], T_BI[:3, 3] = r["imu"]["T_base_imu"]["R"], r["imu"]["T_base_imu"]["t_m"]
    T_IL[:3, :3], T_IL[:3, 3] = R_IL, t_IL
    assert np.allclose(T_BI @ T_IL, T_BL, atol=1e-12)                                       # T_B_L = T_B_I · T_I_L
    assert r["imu"]["acc_unit"] == "g" and r["imu"]["rate_hz"] == 200 and r["imu"]["model"] == "imu_icm40609_livox@1.0.0"
    # slam_cam：全局快门、无云台、固定内参与曝光（C10 静态部分）
    c = r["camera"]
    assert (c["shutter"], c["readout_s"], c["gimbal"], c["exposure_mode"]) == ("global", 0.0, False, "fixed")
    assert (c["width"], c["height"], c["fx"], c["fy"], c["cx"], c["cy"]) == (640, 512, 381.36, 381.36, 320.0, 256.0)
    assert c["exposure_ns"] == 8_000_000 and c["fps"] == 10.0 and c["pixel_convention"] == "colmap_center_0.5"
    assert c["T_base_cam"]["xyz_m"] == [0.12, 0.0, -0.08] and c["T_base_cam"]["rpy_deg"] == [0.0, 45.0, 0.0]
    # 时间约定（C9）与初始化
    assert r["time"] == {"stamp": "exposure_mid", "img_time_offset": 0.0, "exposure_time_init": 0.0, "imu_time_offset": 0.0}
    assert r["init"] == {"start": "ground", "mode": "static", "t_pre_s": 2.0, "min_s": 0.5} and r["imu"]["gravity_align_en"] is False


def test_rig_noise_parameters_match_the_table(overlay) -> None:
    """C11：rig.json 的 IMU 噪声参数与 noise_table_version 对应的参数表（AWR-20 §15.6.5，imu_noise_tier）逐项一致。"""
    imu_doc = overlay.sensor_model("imu_icm40609_livox@1.0.0")[0]
    for tier in ("clean", "realistic", "harsh"):
        r = rig_for_vehicle(VID, vehicles_dir=ROOT / "vehicles", noise_tier=tier)
        assert r["noise"]["tier"] == tier and r["noise"]["noise_table_version"] == "d3-noise-1"
        assert r["noise"]["imu"] == imu_noise_tier(imu_doc, tier, bpf_hz=150.0 if tier == "harsh" else None)
    clean = rig_for_vehicle(VID, vehicles_dir=ROOT / "vehicles", noise_tier="clean")["noise"]["imu"]
    assert all(clean[k] == 0.0 for k in ("acc_sigma_sample_g", "gyro_sigma_sample_rps", "acc_bias_rw_g_rts", "gyro_bias_rw_rps_rts",
                                         "turn_on_acc_mps2", "turn_on_gyro_dps"))
    harsh = rig_for_vehicle(VID, vehicles_dir=ROOT / "vehicles", noise_tier="harsh")["noise"]["imu"]
    assert harsh["vib_hz"] == 150.0 and harsh["vib_amp_mps2"] == 1.5 and harsh["acc_sigma_sample_g"] == 1e-3


def test_airborne_spawn_enables_gravity_align(rig_pose: dict) -> None:
    assert rig_pose["init"]["mode"] == "hover_hold" and rig_pose["imu"]["gravity_align_en"] is True
    assert yaml.safe_load(livo_yaml(rig_pose))["uav"]["gravity_align_en"] is True
    check_slam_rig(rig_pose)


# ---------------------------------------------------------------------------------------------------------------- yaml
def test_livo_and_camera_yaml_follow_rig(rig_ground: dict, rig_pose: dict) -> None:
    for r in (rig_ground, rig_pose):
        L, C = yaml.safe_load(livo_yaml(r)), yaml.safe_load(camera_yaml(r))
        assert L["extrin_calib"]["extrinsic_T"] == R1_EXTRINSIC_T and L["extrin_calib"]["extrinsic_R"] == R1_EXTRINSIC_R
        assert np.allclose(L["extrin_calib"]["Rcl"], R1_RCL, atol=1e-8) and np.allclose(L["extrin_calib"]["Pcl"], R1_PCL, atol=1e-8)
        assert L["time_offset"] == {"imu_time_offset": 0.0, "img_time_offset": 0.0, "exposure_time_init": 0.0}
        for (sec, key), want in R1_FIXED.items():
            assert L[sec][key] == want, (sec, key)
        assert L["common"]["img_en"] == 1 and L["common"]["lid_topic"] == "/livox/lidar" and L["common"]["imu_topic"] == "/livox/imu"
        assert L["uav"]["gravity_align_en"] is r["imu"]["gravity_align_en"]
        for k, want in R1_CAM.items():
            assert C[k] == want
        assert C["cam_cx"] == r["camera"]["cx"] - 0.5 and C["cam_cy"] == r["camera"]["cy"] - 0.5
        assert C["cam_fx"] == C["cam_fy"] == 381.36 and C["cam_model"] == "Pinhole"
        assert [C[f"cam_d{i}"] for i in range(4)] == [0.0] * 4
        assert check_slam_configs(r, livo_yaml(r), camera_yaml(r), fastlio_yaml(r)) == []
    assert yaml.safe_load(livo_yaml(rig_ground, mode="lio"))["common"]["img_en"] == 0
    with pytest.raises(ValueError):
        livo_yaml(rig_ground, mode="vio")


@pytest.mark.skipif(not (R1_SIM / "livo.yaml").is_file(), reason="needs r1 results (.cache/research/d3/r1-fast-livo2)")
def test_livo_yaml_has_the_r1_structure(rig_ground: dict) -> None:
    """生成的 livo.yaml 与 r1 闭环配置节、键完全相同，除头注释外只在浮点书写精度上不同；camera.yaml 只有 fx、fy 取目录值。"""
    ours, r1 = yaml.safe_load(livo_yaml(rig_ground)), yaml.safe_load((R1_SIM / "livo.yaml").read_text(encoding="utf-8"))
    assert list(ours) == list(r1)
    for sec in r1:
        assert list(ours[sec]) == list(r1[sec]), sec
        for k, v in r1[sec].items():
            if isinstance(v, list):
                assert np.allclose(ours[sec][k], v, atol=1e-8), (sec, k)
            else:
                assert ours[sec][k] == v, (sec, k)
    cam, cam_r1 = yaml.safe_load(camera_yaml(rig_ground)), yaml.safe_load((R1_SIM / "camera.yaml").read_text(encoding="utf-8"))
    assert list(cam) == list(cam_r1)
    assert {k: v for k, v in cam.items() if k not in ("cam_fx", "cam_fy")} == {k: v for k, v in cam_r1.items() if k not in ("cam_fx", "cam_fy")}
    assert abs(cam["cam_fx"] - cam_r1["cam_fx"]) < 0.01                                       # 目录 381.36，r1 由 HFOV 80° 推出 381.361


def test_fastlio_yaml_matches_rig_and_upstream_structure(rig_ground: dict) -> None:
    F = yaml.safe_load(fastlio_yaml(rig_ground))
    m = F["mapping"]
    assert m["extrinsic_T"] == R1_EXTRINSIC_T and m["extrinsic_R"] == R1_EXTRINSIC_R and m["extrinsic_est_en"] is False
    assert F["preprocess"] == {"lidar_type": 1, "scan_line": 4, "blind": 0.5, "scan_rate": 10}
    assert (m["acc_cov"], m["gyr_cov"], m["det_range"], m["fov_degree"]) == (0.5, 0.3, 100.0, 360)
    assert F["common"]["time_sync_en"] is False and F["common"]["time_offset_lidar_to_imu"] == 0.0
    assert F["filter_size_surf"] == 0.3 and F["point_filter_num"] == 1 and F["feature_extract_enable"] is False
    if FASTLIO_MID360.is_file():                                                              # 上游 mid360.yaml 的节与键全部出现
        up = yaml.safe_load(FASTLIO_MID360.read_text(encoding="utf-8"))
        for sec, kv in up.items():
            assert set(kv) <= set(F[sec]), sec


def test_slam_eval_reads_and_patches_generated_yaml(rig_ground: dict) -> None:
    """slam_eval 的按行解析（yaml_get、patch_yaml、extrinsic_T_I_L）可直接读写生成的 livo.yaml。"""
    from slam_eval.livo import MODES, extrinsic_T_I_L, patch_yaml, yaml_get

    text = livo_yaml(rig_ground)
    T = extrinsic_T_I_L(text)
    R_IL, t_IL = imu_lidar_extrinsics(rig_ground)
    assert np.allclose(T[:3, :3], R_IL) and np.allclose(T[:3, 3], t_IL)
    assert yaml_get(text, "time_offset/exposure_time_init") == "0.0" and yaml_get(text, "uav/gravity_align_en") == "false"
    for mode, kv in MODES.items():
        patched = patch_yaml(text, {**kv, "evo/pose_output_en": True, "evo/seq_name": f"r-{mode}", "pcd_save/pcd_save_en": False})
        doc = yaml.safe_load(patched)
        assert doc["common"]["img_en"] == kv["common/img_en"] and doc["evo"]["seq_name"] == f"r-{mode}"
        assert check_slam_configs(rig_ground, patched, camera_yaml(rig_ground)) == []


def test_write_slam_configs_and_cli(tmp_path: Path, rig_pose: dict) -> None:
    paths = write_slam_configs(rig_pose, tmp_path / "case")
    assert {p.name for p in paths.values()} == {"rig.json", "livo.yaml", "camera.yaml", "fastlio.yaml"}
    assert load_rig(paths["rig"]) == rig_pose
    assert RIG.main(["--vehicle", VID, "--uav", "uav-02", "--start", "pose", "--vehicles-dir", str(ROOT / "vehicles"), "--out",
                     str(tmp_path / "cli")]) == 0
    for name in ("rig.json", "livo.yaml", "camera.yaml", "fastlio.yaml"):                      # 命令行与库输出逐字节一致
        assert (tmp_path / "cli" / name).read_bytes() == (tmp_path / "case" / name).read_bytes()
    a = (tmp_path / "case" / "rig.json").read_bytes()
    write_slam_configs(rig_for_vehicle(VID, vehicles_dir=ROOT / "vehicles", uav="uav-02", start="pose"), tmp_path / "again")
    assert (tmp_path / "again" / "rig.json").read_bytes() == a                                # 确定性


# ---------------------------------------------------------------------------------------------------------------- 负向
def _edit(text: str, key: str, value: str) -> str:
    from slam_eval.livo import patch_yaml

    return patch_yaml(text, {key: value})


def test_tampered_configs_are_rejected_642(rig_ground: dict) -> None:
    L, C, F = livo_yaml(rig_ground), camera_yaml(rig_ground), fastlio_yaml(rig_ground)
    cases = {
        "exposure_time_init": (_edit(L, "time_offset/exposure_time_init", "0.004"), C, F),
        "img_time_offset": (_edit(L, "time_offset/img_time_offset", "0.05"), C, F),
        "Rcl": (_edit(L, "extrin_calib/Rcl", "[1, 0, 0, 0, 1, 0, 0, 0, 1]"), C, F),
        "extrinsic_T": (_edit(L, "extrin_calib/extrinsic_T", "[0.011, 0.02329, -0.04412]"), C, F),
        "gravity_align_en": (_edit(L, "uav/gravity_align_en", "true"), C, F),
        "cam_cx": (L, _edit(C, "cam_cx", "320.0"), F),                                       # 未减 0.5
        "cam_cy": (L, _edit(C, "cam_cy", "256.0"), F),
        "fastlio": (L, C, _edit(F, "mapping/extrinsic_est_en", "true")),
        "blind": (_edit(L, "preprocess/blind", "0.1"), C, F),
    }
    for what, (l_, c_, f_) in cases.items():
        bad = check_slam_configs(rig_ground, l_, c_, f_, raise_on_error=False)
        assert bad and any(what.split("_")[0] in b or what in b for b in bad), (what, bad)
        with pytest.raises(SensorSvcError) as e:
            check_slam_configs(rig_ground, l_, c_, f_)
        assert e.value.code == 642


def test_invalid_rigs_are_rejected_649(rig_ground: dict) -> None:
    def bad(edit) -> list[str]:
        r = json.loads(json.dumps(rig_ground))
        edit(r)
        with pytest.raises(SensorSvcError) as e:
            check_slam_rig(r)
        assert e.value.code == 649
        return e.value.extra["violations"]

    assert any("global shutter" in v for v in bad(lambda r: r["camera"].update(shutter="rolling", readout_s=0.012)))
    assert any("gimbal" in v for v in bad(lambda r: r["camera"].update(gimbal=True)))
    assert any("exposure_time_init" in v for v in bad(lambda r: r["time"].update(exposure_time_init=0.004)))
    assert any("img_time_offset" in v for v in bad(lambda r: r["time"].update(img_time_offset=-0.05)))
    assert any("gravity_align_en" in v for v in bad(lambda r: r["imu"].update(gravity_align_en=True)))
    assert any("in g" in v for v in bad(lambda r: r["imu"].update(acc_unit="mps2")))
    assert any("slot grid" in v for v in bad(lambda r: r["lidar"].update(point_dt_ns=4999)))
    assert any("rotation" in v for v in bad(lambda r: r["imu"]["T_imu_lidar"].update(R=[[1, 0, 0], [0, 1, 0], [0, 0, 2]])))
    assert any("0.5 s" in v for v in bad(lambda r: r["init"].update(t_pre_s=0.3)))
    with pytest.raises(SensorSvcError) as e:
        rig_for_vehicle(VID, vehicles_dir=ROOT / "vehicles", t_pre_s=0.3)
    assert e.value.code == 649
    with pytest.raises(SensorSvcError) as e:
        rig_for_vehicle("x500", vehicles_dir=ROOT / "vehicles")                               # 不是 D3 SLAM 变体
    assert e.value.code == 649


# ---------------------------------------------------------------------------------------------------------------- 初始化窗口
def _window(n_s: float = 2.0, hz: int = 250, *, v_end: float = 0.0, move_tail_s: float = 0.0, f_g: float = 1.0, spike: float = 0.0):
    t = np.arange(-round(n_s * hz), 1, dtype=np.int64) * (1_000_000_000 // hz)
    w = np.zeros((len(t), 3))
    f = np.tile([0.0, 0.0, f_g * G], (len(t), 1))
    v = np.zeros((len(t), 3))
    v[-1, 0] = v_end
    if move_tail_s:
        v[t > -move_tail_s * 1e9, 0] = 0.5
    if spike:
        w[len(t) // 2, 2] = spike
    return t, w, f, v


def test_init_window_checks() -> None:
    t, w, f, v = _window()
    win = check_init_window(t, w, f, vel=v, mode="static")
    assert win.ok and win.window_s == pytest.approx(2.0) and win.still_s == pytest.approx(2.0) and win.mean_f_g == pytest.approx(1.0)
    assert win.max_omega_rps == 0.0 and win.end_speed_mps == 0.0 and win.to_dict()["ok"] is True
    # IMU 读数（g、200 Hz，无速度列）同样可判定
    ti = np.arange(-400, 1, dtype=np.int64) * 5_000_000
    assert check_init_window(ti, np.zeros((401, 3)), np.tile([0.0, 0.0, 1.0], (401, 1)), mode="hover_hold", acc_unit="g").ok
    # 负向
    assert not check_init_window(*_window(0.4)[:3], vel=_window(0.4)[3], mode="static").ok                           # 窗口 < 0.5 s
    t, w, f, v = _window(move_tail_s=1.7)
    win = check_init_window(t, w, f, vel=v, mode="static")
    assert not win.ok and win.still_s < 0.5 and any("static segment" in x for x in win.violations)                 # 事件零点前在移动
    t, w, f, v = _window(v_end=0.2)
    assert check_init_window(t, w, f, vel=v, mode="static").ok is False                                            # 末样本运动
    win = check_init_window(t, w, f, vel=v, mode="hover_hold")
    assert any("end speed" in x for x in win.violations)                                                           # M26-AC-022
    t, w, f, v = _window(f_g=1.02)
    assert any("specific force" in x for x in check_init_window(t, w, f, vel=v, mode="static").violations)         # C4
    t, w, f, v = _window(spike=5.0)
    assert any("|omega|" in x for x in check_init_window(t, w, f, vel=v, mode="hover_hold").violations)            # C8
    t, w, f, v = _window()
    f[10, 0] = 30.0
    assert any("|a|" in x for x in check_init_window(t, w, f, vel=v, mode="hover_hold").violations)
    assert not check_init_window(t[:-50], w[:-50], f[:-50], vel=v[:-50], mode="static", t_event0_ns=0).ok          # 未到事件零点
    with pytest.raises(SensorSvcError) as e:
        require_init_window(check_init_window(*_window(0.4)[:3], mode="static"))
    assert e.value.code == 642 and "window" in e.value.extra


def test_require_init_window_matches_rig(rig_ground: dict, rig_pose: dict) -> None:
    t, w, f, v = _window()
    require_init_window(check_init_window(t, w, f, vel=v, mode="static"), rig_ground)
    require_init_window(check_init_window(t, w, f, vel=v, mode="hover_hold"), rig_pose)
    with pytest.raises(SensorSvcError) as e:                                                   # 空中出生的窗口配了地面 rig
        require_init_window(check_init_window(t, w, f, vel=v, mode="hover_hold"), rig_ground)
    assert e.value.code == 642


# ---------------------------------------------------------------------------------------------------------------- 预滚
def test_plan_preroll() -> None:
    from awr.agentsdk.env.preroll import PREROLL_DEFAULT_S, plan_preroll

    p = plan_preroll([("uav-01", False), ("uav-02", True)])
    assert PREROLL_DEFAULT_S == 2.0 and (p.t_pre_s, p.n_ticks, p.t_pre_ns) == (2.0, 20, 2_000_000_000)
    assert p.modes == {"uav-01": "static", "uav-02": "hover_hold"} and p.gravity_align_en
    assert (p.start_of("uav-01"), p.start_of("uav-02")) == ("ground", "pose")
    assert not plan_preroll([("uav-01", False)], 0.5).gravity_align_en and plan_preroll([("uav-01", False)], 0.5).n_ticks == 5
    assert plan_preroll([("uav-01", False)], 0).n_ticks == 0
    for bad in (0.3, 0.25, 2.05, -1.0):
        with pytest.raises(ValueError):
            plan_preroll([("uav-01", False)], bad)


def test_event_time_tier_on_the_kinematic_tier() -> None:
    from awr.agentsdk.env.preroll import plan_preroll, run_preroll

    spawn = {"uav-01": ((0.0, 0.0, 0.0), 0.0), "uav-02": ((20.0, 0.0, 30.0), 0.5)}
    inner = KinematicTier(list(spawn), spawn, lambda xy: np.zeros(len(xy)), grounded=["uav-01"])
    inner.open()
    plan = plan_preroll([("uav-01", False), ("uav-02", True)])
    tier = run_preroll(inner, plan)
    assert tier.name == "kinematic" and tier.n_steps == 25 and inner.t_k_ns == 2_000_000_000
    rep = tier.report
    assert rep["t_pre_s"] == 2.0 and rep["n_ticks"] == 20 and rep["gravity_align_en"] and rep["ok"] and rep["contacts"] == []
    u1, u2 = rep["uavs"]["uav-01"], rep["uavs"]["uav-02"]
    assert (u1["mode"], u1["start"], u2["mode"], u2["start"]) == ("static", "ground", "hover_hold", "pose")
    for u in (u1, u2):                                                                         # 窗口 [−2 s, 0]，全程静止或悬停
        assert u["t_first_ns"] == -2_000_000_000 and u["t_last_ns"] == 0 and u["window_s"] == 2.0 and u["still_s"] == 2.0
        assert u["mean_f_g"] == pytest.approx(1.0, abs=1e-9) and u["end_speed_mps"] == 0.0
    # 事件时间视图：PoseHistory 负时刻、tick 0 推进 (0, 100 ms]、指令 apply_tick 平移
    s = tier.history.last_sample("uav-02")
    assert s.t_ns == 0 and np.allclose(s.pos_enu_m, (20.0, 0.0, 30.0)) and tier.history.last_sample("uav-01").pos_enu_m[2] == GEAR_Z_M
    t, st = tier.preroll_samples("uav-01")
    assert t[0] == -2_000_000_000 and t[-1] == 0 and len(t) == 501 and np.all(np.diff(t) == 4_000_000)
    assert np.allclose(st[:, 13:16], [0.0, 0.0, G])
    sl = tier.history.slice("uav-01", -1_000_000_000, 0)
    assert sl.t_ns[0] == -1_000_000_000 and sl.t_ns[-1] == 0
    assert tier.submit([TierCommand("uav-01", "takeoff", 0, alt_m=10.0)])[0].ok
    r = tier.advance(0)
    assert (r.k, r.t_start_ns, r.t_end_ns) == (0, 0, 100_000_000) and inner.t_k_ns == 2_100_000_000
    contacts = list(r.contacts)
    for k in range(1, 10):
        contacts += tier.advance(k).contacts
    lift = [c for c in contacts if c.kind == "LIFTOFF"]
    assert lift and 0 < lift[0].t_ns <= 1_000_000_000                                         # 接触事件为事件时间
    assert tier.history.last_sample("uav-01").pos_enu_m[2] > GEAR_Z_M and tier.status()["uav-01"] == VehicleStatus.AIRBORNE
    info = tier.close()
    assert info["t_end_ns"] == 1_000_000_000 and info["ticks"] == 30 and info["preroll"]["ok"] is True


def test_preroll_crash_is_a_physics_failure() -> None:
    from awr.agentsdk.env.preroll import plan_preroll, run_preroll
    from awr.phys import PhysError

    class Crashing(KinematicTier):                                                            # 预滚第 5 个 tick 起报告坠毁
        def status(self):
            return {u: VehicleStatus.CRASHED if self._ticks >= 5 else st for u, st in super().status().items()}

    spawn = {"uav-01": ((0.0, 0.0, 30.0), 0.0)}
    inner = Crashing(["uav-01"], spawn, lambda xy: np.zeros(len(xy)))
    inner.open()
    with pytest.raises(PhysError) as e:
        run_preroll(inner, plan_preroll([("uav-01", True)], 2.0))
    assert e.value.code == 630 and e.value.detail["status"] == {"uav-01": str(VehicleStatus.CRASHED)}


class _Counter:
    """记录每次调用的观测（k、t_ns、传感器帧时刻）；动作只用悬停与起飞。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def reset(self, cfg) -> None:
        self.cfg = cfg

    def step(self, obs, inbox):
        self.calls.append(obs)
        kind = "takeoff" if obs["k"] == 0 and obs["status"]["mode"] == "landed" else "hover"
        nav = {"mode": kind, **({"alt_m": 10.0} if kind == "takeoff" else {})}
        return {"schema": "drone4d.bench.act.v1", "k": obs["k"], "nav": nav}


def _run_env(spec, l2=None, **kw):
    from awr.agentsdk.env.core import BenchEnv

    env = BenchEnv(spec, l2=l2, **kw)
    obs, infos = env.reset()
    ags = {u: _Counter() for u in env.possible_agents}
    for u, a in ags.items():
        a.reset(infos[u]["config"])
    first = {u: o["obs"] for u, o in obs.items()}
    while env.agents:
        obs, *_ = env.step({u: ags[u].step(o["obs"], o["inbox"]) for u, o in obs.items()})
    return env, env.close(), ags, first


def _fleet_mix():
    from awr.agentsdk.env.spec import FleetEntry

    return (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 30.0), 0.0, airborne=True))


def test_env_preroll_is_invisible_to_agents_kinematic(synth_event, tmp_path: Path) -> None:
    """M26-AC-022：窗口位于 t < 0，预滚期间 Agent 被请求动作 0 次、收到帧 0；k = 0 观测时刻为 0；运动学档预滚不改变观测与动作流。"""
    from awr.agentsdk.env.spec import EnvSpec

    w, ev = synth_event
    runs = {}
    for name, pre in (("pre", 2.0), ("none", 0.0)):
        spec = EnvSpec(f"t-pre-{name}", 5, w, ev, _fleet_mix(), physics="kinematic", duration_s=3.0, out_dir=tmp_path / name)
        runs[name] = _run_env(spec, preroll_s=pre)
    _env, m, ags, first = runs["pre"]
    assert all(o["k"] == 0 and o["t_ns"] == 0 for o in first.values())
    for a in ags.values():
        ks = [o["k"] for o in a.calls]
        assert ks == sorted(ks) and ks[0] == 0 and min(o["t_ns"] for o in a.calls) == 0
        dets = [d for o in a.calls for d in o["sensors"].get("detections", [])]
        assert all(int(d.get("t_ns", 0)) >= 0 for d in dets)
    pr = m["tier_info"]["preroll"]
    assert pr["ok"] and pr["t_pre_s"] == 2.0 and pr["gravity_align_en"] is True
    assert pr["uavs"]["uav-01"]["mode"] == "static" and pr["uavs"]["uav-02"]["mode"] == "hover_hold"
    assert all(u["t_first_ns"] == -2_000_000_000 and u["t_last_ns"] == 0 for u in pr["uavs"].values())
    assert m["n_ticks"] == 30 and m["tier_info"]["ticks"] == 50 and m["tier_info"]["t_end_ns"] == 3_000_000_000
    fl = np.load(tmp_path / "pre/truth/fleet.npz")
    assert int(fl["t_ns"][0]) == 0
    # 运动学档的静止与悬停保持与无预滚时状态相同：Agent 侧观测与动作流逐字节一致
    for u in ("uav-01", "uav-02"):
        for f in ("obs.sha256", "acts.jsonl"):
            assert (tmp_path / "pre/agents" / u / f).read_bytes() == (tmp_path / "none/agents" / u / f).read_bytes(), (u, f)
    assert runs["none"][1]["tier_info"]["preroll"]["ok"] is None and runs["none"][1]["tier_info"]["ticks"] == 30


def test_env_preroll_fast_windows_and_rig(synth_event, tmp_path: Path) -> None:
    """Fast 档（d1_250）：地面起飞静止窗口、空中出生悬停保持窗口满足 C4、C5、C8 与末速度 ≤ 0.1 m/s；窗口与 rig.json 的出生方式相容。"""
    from awr.agentsdk.env.spec import EnvSpec

    w, ev = synth_event
    spec = EnvSpec("t-pre-fast", 5, w, ev, _fleet_mix(), physics="fast", duration_s=1.0, out_dir=tmp_path / "f")
    env, m, _ags, first = _run_env(spec)
    pr = m["tier_info"]["preroll"]
    assert m["tier"] == "fast" and pr["ok"], pr
    for u, mode, start in (("uav-01", "static", "ground"), ("uav-02", "hover_hold", "pose")):
        x = pr["uavs"][u]
        assert (x["mode"], x["start"]) == (mode, start)
        assert x["window_s"] == 2.0 and x["still_s"] >= 0.5 and abs(x["mean_f_g"] - 1.0) <= 0.01
        assert x["max_omega_rps"] < 5.0 and x["max_f_mps2"] < 30.0 and x["end_speed_mps"] <= 0.1
        t, st = env.tier.preroll_samples(u)
        assert t[0] == -2_000_000_000 and t[-1] == 0 and np.all(np.diff(t) == 4_000_000)       # 250 Hz、事件零点之前
        win = check_init_window(t, st[:, 10:13], st[:, 13:16], vel=st[:, 7:10], mode=mode)
        rig = rig_for_vehicle(VID, vehicles_dir=ROOT / "vehicles", uav=u, start=env.preroll.start_of(u))
        require_init_window(win, rig)
        assert rig["imu"]["gravity_align_en"] is (start == "pose")
    assert pr["uavs"]["uav-01"]["max_omega_rps"] == 0.0 and pr["uavs"]["uav-01"]["end_speed_mps"] == 0.0
    assert all(o["t_ns"] == 0 for o in first.values())


def test_env_preroll_l2_frames_start_at_zero(synth_event, tmp_path: Path) -> None:
    """L2：预滚样本只进入 IMU 预热，交付的相机、LiDAR、IMU 帧时刻全部 ≥ 0（预滚帧不进入 OBS）。"""
    from test_l2 import _l2

    from awr.agentsdk.env.l2 import CAM_SENSOR, IMU_SENSOR
    from awr.agentsdk.env.spec import EnvSpec

    w, ev = synth_event
    spec = EnvSpec("t-pre-l2", 3, w, ev, _fleet_mix()[1:], obs_level="L2", physics="kinematic", duration_s=1.0, out_dir=tmp_path / "l2")
    _env, m, ags, first = _run_env(spec, l2=lambda e: _l2(e, False))
    assert m["tier_info"]["preroll"]["ok"]
    s0 = first["uav-02"]["sensors"]
    assert IMU_SENSOR not in s0 and [f["seq"] for f in s0[CAM_SENSOR]] == [0]
    imu = [f for o in ags["uav-02"].calls for f in o["sensors"].get(IMU_SENSOR, [])]
    assert imu and min(int(f["t0_ns"]) for f in imu) == 0 and [f["seq"] for f in imu] == list(range(len(imu)))
    for o in ags["uav-02"].calls:
        for sid, fs in o["sensors"].items():
            for f in fs:
                assert int(f.get("t_ns", f.get("t0_ns", f.get("timebase_ns", 0)))) >= 0, sid
