"""`slam_eval` 作业（tools/bench/d3/slam_eval；M27-FR-101–110、M27-AC-024/026/027；D3-AC-14、D3-AC-15、D3-AC-47 的 r_map 项）。

- 轨迹指标：刚体变换下零误差、等比尺度误差下 RPE100 等于尺度偏差、与 r1 `eval_sim.py` RPE 循环（逐样本起点、除以 100 m）的
  相对差 ≤ 1%（M27-AC-024）、Umeyama 还原、真机回归的位置 ATE；
- 地图指标：到网格距离（PLY 网格，局部原点 float32）、完整度与 F-score、真值位姿 LiDAR 参考集（B1）落在网格上、L0 离线地图；
- 内存模型：`1.5 × (2 GB + 430 B × r_map × T)`、MID-360 4 核与 4.5 万点/s（ADR-215 冻结）、长量程 ≥ 20 GB 与 4 核、由 RSS 采样
  还原 r_map；
- 锚定（ADR-215 (1)）：事件零点锚定消除初始化瞬态的整体旋转，RPE100 与对齐 ATE 不随锚定点变化；`evaluate_run` 缺省事件零点、
  算例无事件零点时回落到首个估计并注明；
- 容器执行：参数 yaml 补丁、容器参数（`--network none`、`--memory` = `--memory-swap`、`--cpus`）、OOM / 非零退出 / 日志停滞记 684、
  保留日志并重试 1 次、单个运行失败不影响其他运行（假 docker）；
- 判定：D3-AC-14 按 ADR-215 冻结表（realistic 区间 [0.07%, 0.18%]、clean 地图以事件零点锚定）、样本不全时不给通过；旧运行的事件
  零点数值取自 `reanchor.json`，两者都缺时地图条款未定；
- 作业：合同违反（删除无回波点的 bag）返回 642 且不启动容器；合成算例经假 docker 与本地评测完成全流程并给出分布。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "bench" / "d3"))

from slam_eval import SCHEMA  # noqa: E402
from slam_eval.case import load_case, rebag  # noqa: E402
from slam_eval.cli import evaluate_run, resolve_anchor  # noqa: E402
from slam_eval.cli import main as cli_main  # noqa: E402
from slam_eval.livo import (  # noqa: E402
    LivoRun,
    classify_exit,
    docker_argv,
    extrinsic_T_I_L,
    parse_log,
    patch_yaml,
    read_resources,
    run_parallel,
    run_with_retry,
    yaml_get,
    yaml_vector,
)
from slam_eval.memmodel import MEM, calibrate_r_map, job_resources, mem_limit_gb  # noqa: E402
from slam_eval.report import AC14, MAP_CLAUSE, distribution, judge_ac14, summarize_runs  # noqa: E402
from slam_eval.traj import (  # noqa: E402
    R_to_quat,
    TrajInterp,
    Tum,
    load_tum,
    positions_ate,
    quat_to_R,
    save_tum,
    segment_starts,
    traj_metrics,
    umeyama,
)

from awr.sensorsvc.slam.bag import CUSTOM_POINT, G, SlamBagWriter, offset_time_ns, pack_custom_msg  # noqa: E402

NS = 1_000_000_000
T0 = 1_760_000_000.0


# ---------------------------------------------------------------------------------------------------------------- 轨迹样例
def rotz(a: float) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def loop_traj(n_s: float = 180.0, hz: float = 50.0, v: float = 6.0, r: float = 120.0) -> Tum:
    """6 m/s 圆形环线（真值 IMU 位姿，航向沿切向），30 m 高。"""
    t = T0 + np.arange(0, n_s, 1 / hz)
    th = v * (t - T0) / r
    p = np.stack([r * np.sin(th), r * (1 - np.cos(th)), np.full_like(th, 30.0)], 1)
    q = R_to_quat(np.stack([rotz(a) for a in th]))
    return Tum(t, p, q)


def to_frame(tr: Tum, T: np.ndarray) -> Tum:
    """把世界系轨迹表达到 F 系：T_F_I = T · T_W_I。"""
    TT = T @ tr.T()
    return Tum(tr.t, TT[:, :3, 3], R_to_quat(TT[:, :3, :3]))


def T_of(R: np.ndarray, p) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, p
    return T


def r1_rpe(est: Tum, gt: Tum) -> float:
    """r1 `eval_sim.py` 的 RPE 循环原样移植（逐估计样本起点、除以 100 m）。"""
    g = TrajInterp(gt)
    Rg, pgi = g.at(est.t)
    T_W_F = T_of(Rg[0], pgi[0]) @ np.linalg.inv(est.T()[0])
    Tw = T_W_F @ est.T()
    Rw, pw = Tw[:, :3, :3], Tw[:, :3, 3]
    seglen = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(pgi, axis=0), axis=1))])
    rpe, j = [], 0
    for i in range(len(est.t)):
        while j < len(est.t) and seglen[j] - seglen[i] < 100:
            j += 1
        if j >= len(est.t):
            break
        rpe.append(np.linalg.norm(Rg[i].T @ (pgi[j] - pgi[i]) - Rw[i].T @ (pw[j] - pw[i])) / 100 * 100)
    return float(np.mean(rpe))


# ---------------------------------------------------------------------------------------------------------------- traj
def test_quaternion_round_trip_and_umeyama():
    rng = np.random.default_rng(1)
    q = rng.normal(size=(50, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    q[q[:, 3] < 0] *= -1
    assert np.allclose(R_to_quat(quat_to_R(q)), q, atol=1e-12)
    src = rng.normal(size=(200, 3)) * 50
    R = quat_to_R(q[:1])[0]
    t = np.array([3.0, -7.0, 2.5])
    R2, t2, s = umeyama(src, src @ R.T + t)
    assert np.allclose(R2, R, atol=1e-10) and np.allclose(t2, t, atol=1e-8) and s == 1.0
    R3, _t3, s3 = umeyama(src, 1.7 * src @ R.T + t, with_scale=True)
    assert abs(s3 - 1.7) < 1e-10 and np.allclose(R3, R, atol=1e-10)


def test_load_tum_sorts_and_drops_duplicate_stamps(tmp_path):
    p = tmp_path / "a.tum"
    p.write_text("# t x y z qx qy qz qw\n2 1 0 0 0 0 0 1\n1 0 0 0 0 0 0 1\n2 9 9 9 0 0 0 1\n3 2 0 0 0 0 0 1\n")
    tr = load_tum(p)
    assert tr.t.tolist() == [1.0, 2.0, 3.0] and tr.p[1, 0] == 1.0


def test_segment_starts():
    s = np.array([0.0, 0.4, 1.0, 1.5, 2.2, 3.0])
    assert segment_starts(s, 0.0).tolist() == [0, 1, 2, 3, 4, 5]
    assert segment_starts(s, 1.0).tolist() == [0, 2, 4, 5]


def test_rigid_frame_change_gives_zero_error():
    gt = loop_traj()
    est = to_frame(gt, T_of(quat_to_R([[0.1, -0.2, 0.3, 0.9]])[0], [5.0, -3.0, 12.0]))
    m = traj_metrics(est, gt)
    assert m["drift_max_m"] < 1e-6 and m["ate_aligned_rmse_m"] < 1e-6 and m["rpe100_pct"] < 1e-6
    assert m["rot_err_final_deg"] < 1e-5 and m["n_segments"] > 900
    assert 1070 < m["length_m"] < 1090                                     # 180 s × 6 m/s


def test_rpe100_equals_uniform_scale_error():
    """估计位置相对起点等比放大 (1 + ε)、姿态正确：每段相对平移误差 = ε·弦长，除以弧长后 RPE100 = 100ε · sin(x)/x（x = L/2r）；
    直线航段上即 100ε %。"""
    gt = loop_traj()
    eps = 0.001
    p = gt.p[0] + (gt.p - gt.p[0]) * (1 + eps)
    est = to_frame(Tum(gt.t, p, gt.q), T_of(rotz(0.7), [1, 2, 3]))
    m = traj_metrics(est, gt)
    x = 100.0 / (2 * 120.0)
    assert m["rpe100_pct"] == pytest.approx(100 * eps * np.sin(x) / x, rel=2e-4)
    assert m["rpe100_max_pct"] == pytest.approx(m["rpe100_median_pct"], rel=1e-3)
    line = line_traj(60.0)
    pl = line.p[0] + (line.p - line.p[0]) * (1 + eps)
    ml = traj_metrics(to_frame(Tum(line.t, pl, line.q), T_of(rotz(-0.4), [3, 0, 1])), line)
    assert ml["rpe100_pct"] == pytest.approx(100 * eps, rel=1e-9) and ml["rpe100_max_pct"] == pytest.approx(100 * eps, rel=1e-9)
    assert m["drift_final_m"] == pytest.approx(eps * np.linalg.norm(gt.p[-1] - gt.p[0]), rel=1e-6)


def test_rpe100_matches_r1_loop_within_1pct():
    """M27-AC-024：同一轨迹上 RPE100 段均值与 r1 `eval_sim.py` 相对差 ≤ 1%（估计取 10 Hz，含随机游走漂移与姿态噪声）。"""
    gt = loop_traj(hz=200.0)
    rng = np.random.default_rng(7)
    sel = slice(0, None, 20)
    t, p, q = gt.t[sel], gt.p[sel].copy(), gt.q[sel]
    p += np.cumsum(rng.normal(scale=0.004, size=p.shape), axis=0)
    yaw = np.cumsum(rng.normal(scale=2e-5, size=len(t)))
    R = quat_to_R(q) @ np.stack([rotz(a) for a in yaw])
    est = to_frame(Tum(t, p, R_to_quat(R)), T_of(rotz(-1.2), [0.3, 0.1, -0.2]))
    ref = r1_rpe(est, gt)
    for stride in (0.0, 1.0):
        m = traj_metrics(est, gt, stride_m=stride)
        assert abs(m["rpe100_pct"] - ref) / ref <= 0.01, (stride, m["rpe100_pct"], ref)
    assert ref > 0.01


def rotx(a: float) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[1.0, 0, 0], [0, c, -s], [0, s, c]])


def transient_est(gt: Tum, T_F_W: np.ndarray, rad: float, t_until: float) -> Tum:
    """估计 = 真值表达到 F 系；`t_until` 之前的样本带机体系 x 轴姿态误差 `rad`（FAST-LIVO2 预滚窗口内的初始化瞬态）。"""
    T = T_F_W @ gt.T()
    m = gt.t < t_until
    T[m, :3, :3] = T[m, :3, :3] @ rotx(rad)
    return Tum(gt.t, T[:, :3, 3], R_to_quat(T[:, :3, :3]))


def test_traj_metrics_anchor_time_removes_init_transient():
    """ADR-215 (1)：以事件零点（首个不早于锚定时刻的估计）锚定，初始化瞬态不再经环线放大；RPE100 与对齐 ATE 不受锚定点影响。"""
    gt = loop_traj()
    est = transient_est(gt, T_of(rotz(0.4), [3.0, -2.0, 1.0]), np.radians(0.2), T0 + 2.0)
    first = traj_metrics(est, gt)
    ev0 = traj_metrics(est, gt, anchor_t_s=T0 + 2.0 - 1e-4)
    assert first["anchor"]["index"] == 0 and first["anchor"]["requested_t_s"] is None and first["drift_max_m"] > 0.5
    assert ev0["anchor"]["t_s"] == pytest.approx(T0 + 2.0) and ev0["anchor"]["skipped_s"] == pytest.approx(2.0)
    assert ev0["drift_max_m"] < 1e-6 and ev0["drift_final_m"] < 1e-6 and ev0["rot_err_final_deg"] < 1e-5
    assert ev0["rpe100_pct"] == pytest.approx(first["rpe100_pct"], rel=1e-9)                # 全部样本，与锚定无关
    assert ev0["ate_aligned_rmse_m"] == pytest.approx(first["ate_aligned_rmse_m"], rel=1e-6)
    assert traj_metrics(est, gt, anchor_t_s=T0 - 50.0)["anchor"]["index"] == 0              # 早于首个估计：取首个
    with pytest.raises(ValueError):
        traj_metrics(est, gt, anchor_t_s=T0 + 1e4)


def test_evaluate_run_anchors_at_event_zero_by_default(tmp_path):
    gt = loop_traj(n_s=60.0)
    d = tmp_path / "case"
    d.mkdir()
    save_tum(d / "gt_imu.tum", gt)
    (d / "case.json").write_text(json.dumps({"schema": "drone4d.bench.slam_case.v1", "name": "loop", "gt_imu": "gt_imu.tum",
                                             "t_event0_bag_ns": round((T0 + 2.0) * NS)}))
    rd = tmp_path / "run"
    (rd / "Log" / "result").mkdir(parents=True)
    save_tum(rd / "Log" / "result" / "est.txt", transient_est(gt, T_of(rotz(-0.3), [1.0, 2.0, 0.5]), np.radians(0.2), T0 + 2.0))
    case = load_case(d)
    assert case.meta["t_event0_bag_ns"] == round((T0 + 2.0) * NS)
    doc = evaluate_run(case, rd)
    a = doc["traj"]["anchor"]
    assert a["mode"] == "event0" and a["requested_t_s"] == pytest.approx(T0 + 2.0) and a["t_s"] >= T0 + 2.0 - 1e-6
    assert doc["traj"]["drift_max_m"] < 1e-4 and doc["map"] is None
    first = evaluate_run(case, rd, anchor="first")
    assert first["traj"]["anchor"]["mode"] == "first_estimate" and first["traj"]["drift_max_m"] > 0.1
    assert evaluate_run(case, rd, anchor=T0 + 30.0)["traj"]["anchor"]["mode"] == "time"
    # 无事件零点的算例（r1 rig.json、外部 bag）：回落到首个估计并注明
    (d / "case.json").write_text(json.dumps({"schema": "drone4d.bench.slam_case.v1", "name": "loop", "gt_imu": "gt_imu.tum"}))
    r = resolve_anchor(load_case(d))
    assert r["mode"] == "first_estimate" and "event zero" in r["fallback"]
    out = tmp_path / "eval.json"
    assert cli_main(["eval", "--case", str(d), "--run", str(rd), "--out", str(out), "--anchor", str(T0 + 2.0)]) == 0
    assert json.loads(out.read_text())["traj"]["anchor"]["mode"] == "time"


def test_positions_ate_against_rtk():
    rng = np.random.default_rng(3)
    t = T0 + np.arange(0, 300, 0.2)
    p = np.stack([np.cos(t / 30) * 400, np.sin(t / 30) * 400, 90 + np.sin(t / 7)], 1)
    est_p = (p - [10, 20, 0]) @ rotz(0.4).T + rng.normal(scale=0.5, size=p.shape)
    est = Tum(t[::2], est_p[::2], np.tile([0, 0, 0, 1.0], (len(t[::2]), 1)))
    a = positions_ate(est, t, p)
    assert 0.3 < a["ate_rmse_m"] < 1.2 and a["n_assoc"] >= len(t) - 2


# ---------------------------------------------------------------------------------------------------------------- memmodel
def test_memory_model_and_long_range_floor():
    # ADR-215 (5)：MID-360 4.5 万点/s、4 核；360 s 作业约 1.5 × (2 GB + 430 B × 45,000 × 360) ≈ 13.4 GB
    assert mem_limit_gb("mid360", 360.0) == pytest.approx(1.5 * (2.0 + 430 * 45_000 * 360 / 1e9))
    assert mem_limit_gb("mid360", 360.0) == pytest.approx(13.449, abs=1e-3) and job_resources("mid360", 360.0)["mem_gb"] == 14
    assert mem_limit_gb("avia", 60.0) == 20.0                                                       # 长量程下限（暂定）
    r = job_resources("avia", 318.0)
    assert r["mem_gb"] >= 20 and r["cpus"] == 4.0
    assert job_resources("mid360", 160.0)["cpus"] == 4.0
    assert MEM.status == {"mid360": "frozen", "avia": "provisional"} and MEM.adr == "ADR-215"
    with pytest.raises(KeyError):
        mem_limit_gb("unknown", 10.0)


def test_calibrate_r_map_recovers_rate():
    rate, r_map, t0 = 4.0, 48_000.0, 100.0
    tw = t0 + np.arange(0, 40, 2.0)
    rss = 0.6e9 + 430 * r_map * (tw - t0) * rate
    cal = calibrate_r_map(tw, rss, rate=rate, play_t0_s=t0, play_t1_s=t0 + 40, flight_s=160.0)
    assert cal["r_map_slope"] == pytest.approx(r_map, rel=1e-9)
    assert cal["r_map_recommended"] >= cal["r_map_slope"]


# ---------------------------------------------------------------------------------------------------------------- yaml 与容器
LIVO_YAML = textwrap.dedent("""\
    common:
      img_topic: "/left_camera/image"
      img_en: 1
      lidar_en: 1
    extrin_calib:
      extrinsic_T: [-0.01100000, -0.02329000, 0.04412000]
      extrinsic_R: [1, 0, 0, 0, 1, 0, 0, 0, 1]
    evo:
      seq_name: "sim"
      pose_output_en: false
    pcd_save:
      pcd_save_en: true  # 退出时存图
      filter_size_pcd: 0.15
    """)
CAM_YAML = "cam_model: Pinhole\ncam_width: 640\ncam_height: 512\ncam_fx: 381.36\ncam_cx: 319.5\n"


def test_yaml_patch_and_read():
    s = patch_yaml(LIVO_YAML, {"common/img_en": 0, "evo/seq_name": "est", "evo/pose_output_en": True, "pcd_save/pcd_save_en": False})
    assert yaml_get(s, "common/img_en") == "0" and yaml_get(s, "evo/seq_name") == "est"
    assert yaml_get(s, "evo/pose_output_en") == "true" and yaml_get(s, "pcd_save/pcd_save_en") == "false"
    assert yaml_get(s, "pcd_save/filter_size_pcd") == "0.15" and yaml_get(s, "common/lidar_en") == "1"
    assert yaml_get(CAM_YAML, "cam_fx") == "381.36"
    assert yaml_get(patch_yaml(CAM_YAML, {"cam_cx": 319.0}), "cam_cx") == "319.0"
    with pytest.raises(KeyError):
        patch_yaml(LIVO_YAML, {"common/no_such_key": 1})
    with pytest.raises(KeyError):
        patch_yaml(LIVO_YAML, {"evo/img_en": 1})                        # 键属于其他段
    assert np.allclose(yaml_vector(LIVO_YAML, "extrin_calib/extrinsic_T"), [-0.011, -0.02329, 0.04412])
    T = extrinsic_T_I_L(LIVO_YAML)
    assert np.allclose(T[:3, :3], np.eye(3)) and T[2, 3] == pytest.approx(0.04412)


def test_docker_argv_isolation_and_limits(tmp_path):
    bag = tmp_path / "case" / "sim.bag"
    bag.parent.mkdir()
    bag.write_bytes(b"x")
    r = LivoRun("d3-slam-a-livo-r1", "livo", 1, tmp_path / "run", bag, "d3-livo:dev", mem_gb=8.1, cpus=2.0, rate=4.0, start_s=72.0)
    a = docker_argv(r)
    assert a[:2] == ["docker", "run"] and "--rm" not in a
    assert a[a.index("--network") + 1] == "none"
    assert a[a.index("--memory") + 1] == a[a.index("--memory-swap") + 1] == f"{int(np.ceil(8.1 * 1024))}m"
    assert a[a.index("--cpus") + 1] == "2"
    assert f"{bag.parent}:/bag:ro" in a and "BAG=/bag/sim.bag" in a and "RATE=4" in a and "START=72" in a
    assert a[-4:] == ["--entrypoint", "bash", "d3-livo:dev", "/opt/d3-livo/run_livo.sh"]


def test_classify_exit():
    ok = {"result_ok": True}
    assert classify_exit({"ExitCode": 0, "OOMKilled": False}, ok, False)["ok"]
    assert classify_exit({"ExitCode": 137, "OOMKilled": True}, None, False) == {"ok": False, "code": 684, "why": "oom_kill", "exit_code": 137}
    assert classify_exit({"ExitCode": 137, "OOMKilled": False}, None, False)["why"] == "oom_kill"
    assert classify_exit({"ExitCode": 4}, {"why": "node_exited_during_play"}, False)["why"] == "node_exited_during_play"
    assert classify_exit({"ExitCode": 4}, {"why": "node_exited_during_play", "oom_kill_events": 1}, False)["why"] == "oom_kill"
    assert classify_exit({"ExitCode": 0}, {"result_ok": False}, False)["why"] == "no_trajectory"
    assert classify_exit({"ExitCode": 137}, None, True)["why"] == "log_stalled"
    assert classify_exit(None, None, False)["code"] == 684


def test_parse_log_and_resources(tmp_path):
    (tmp_path / "livo.log").write_text(textwrap.dedent("""\
        [ Preprocess ] Input point number: 20000
        [ Preprocess ] Output point number: 5700
        LIO Mapping Time
        | Current Total Time    |   0.0331 |
        VIO Time
        | Current Total Time    |   0.0090 |
        LIO Mapping Time
        | Current Total Time    |   0.0350 |
        """))
    pl = parse_log(tmp_path / "livo.log")
    assert pl["lio"]["n"] == 2 and pl["lio"]["mean_ms"] == pytest.approx(34.05) and pl["vio"]["n"] == 1
    assert pl["pts_in_mean"] == 20000 and pl["pts_out_mean"] == 5700
    (tmp_path / "res.txt").write_text("t_wall rss_kb cpu_pct\n10 1000000 150\n12 2000000 170\n14 2441406 160\n")
    (tmp_path / "status.json").write_text(json.dumps({"play_s": 40.0, "drain_s": 10.0, "save_s": 2.0, "cpu_ticks": 12300, "clk_tck": 100}))
    rs = read_resources(tmp_path)
    assert rs["rss_peak_gb"] == pytest.approx(2.5, rel=1e-6) and rs["cpu_s_total"] == 123.0 and rs["cpu_cores_equiv"] == pytest.approx(2.46)


FAKE_DOCKER = r'''#!/usr/bin/env python3
"""假 docker：run 按容器名中的场景写出运行目录文件；inspect 返回记录的状态；rm/kill/image 为空操作。"""
import json, os, sys, time
from pathlib import Path
st = Path(os.environ["FAKE_DOCKER_STATE"])
st.mkdir(exist_ok=True)
a = sys.argv[1:]
if a[0] == "run":
    name = a[a.index("--name") + 1]
    rd = Path([v.split(":")[0] for v in (a[i + 1] for i, x in enumerate(a) if x == "-v") if v.endswith(":/run")][0])
    n = len(list(st.glob(name + ".*.json")))
    with open(st / "log.txt", "a") as f:
        f.write(" ".join(a) + "\n")
    if "oom" in name:
        (rd / "livo.log").write_text("starting\n")
        (st / f"{name}.{n}.json").write_text(json.dumps({"ExitCode": 137, "OOMKilled": True}))
        sys.exit(137)
    if "stall" in name:
        (rd / "livo.log").write_text("starting\n")
        (st / f"{name}.{n}.json").write_text(json.dumps({"ExitCode": 137, "OOMKilled": False}))
        time.sleep(30)
        sys.exit(137)
    (rd / "Log" / "result").mkdir(parents=True, exist_ok=True)
    src = os.environ.get("FAKE_EST")
    (rd / "Log" / "result" / "est.txt").write_text(Path(src).read_text() if src else "1 0 0 0 0 0 0 1\n2 1 0 0 0 0 0 1\n")
    pcd = os.environ.get("FAKE_PCD")
    if pcd:
        (rd / "Log" / "pcd").mkdir(parents=True, exist_ok=True)
        (rd / "Log" / "pcd" / "all_raw_points.pcd").write_bytes(Path(pcd).read_bytes())
    (rd / "livo.log").write_text("LIO Mapping Time\n| Current Total Time    |   0.0300 |\n")
    (rd / "res.txt").write_text("t_wall rss_kb cpu_pct\n100 500000 120\n102 600000 130\n104 700000 125\n106 800000 140\n")
    (rd / "status.json").write_text(json.dumps({"exit_code": 0, "result_ok": True, "play_t0": 100.0, "play_t1": 106.0, "play_s": 6.0,
                                                "drain_s": 1.0, "save_s": 0.5, "rate": 4.0, "cpu_ticks": 1000, "clk_tck": 100}))
    (st / f"{name}.{n}.json").write_text(json.dumps({"ExitCode": 0, "OOMKilled": False}))
    sys.exit(0)
if a[0] == "inspect":
    name = a[-1]
    fs = sorted(st.glob(name + ".*.json"))
    if not fs:
        sys.exit(1)
    print(fs[-1].read_text())
    sys.exit(0)
if a[0] == "image":
    print("sha256:fake")
    sys.exit(0)
sys.exit(0)
'''


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    d = bin_dir / "docker"
    d.write_text(FAKE_DOCKER.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    d.chmod(d.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "dockerstate"))
    return tmp_path / "dockerstate"


def _run(tmp_path, name, bag) -> LivoRun:
    return LivoRun(name, "livo", 1, tmp_path / name, bag, "d3-livo:dev", mem_gb=2.0, cpus=1.0)


def test_oom_is_684_with_logs_kept_and_one_retry(tmp_path, fake_docker):
    """M27-AC-026 / FR-105：OOM 记 684、失败尝试的日志保留、同一重复只重试 1 次，其他运行不受影响。"""
    bag = tmp_path / "sim.bag"
    bag.write_bytes(b"x")
    runs = [_run(tmp_path, "d3-t-oom", bag), _run(tmp_path, "d3-t-ok1", bag), _run(tmp_path, "d3-t-ok2", bag)]
    res = run_parallel(runs, 2, retries=1, watchdog_s=60, poll_s=0.05)
    assert [r["ok"] for r in res] == [False, True, True]
    bad = res[0]
    assert bad["code"] == 684 and bad["why"] == "oom_kill" and len(bad["attempts"]) == 2
    assert (tmp_path / "d3-t-oom" / "attempt-1" / "livo.log").exists() and (tmp_path / "d3-t-oom" / "livo.log").exists()
    assert all(len(r["attempts"]) == 1 for r in res[1:])
    log = (fake_docker / "log.txt").read_text()
    assert log.count("--name d3-t-oom") == 2


def test_stalled_log_is_684(tmp_path, fake_docker):
    bag = tmp_path / "sim.bag"
    bag.write_bytes(b"x")
    res = run_with_retry(_run(tmp_path, "d3-t-stall", bag), retries=0, watchdog_s=0.5, poll_s=0.1)
    assert not res["ok"] and res["code"] == 684 and res["why"] == "log_stalled" and res["wall_s"] < 20


# ---------------------------------------------------------------------------------------------------------------- 判定
def _doc(name, seed, noise, rpes, ates=None, accs=None, mode="livo", anchor="event0"):
    """metrics.json 样例；`anchor = None` 模拟 ADR-215 之前的运行（以首个估计锚定，`traj` 无 `anchor`）。"""
    runs = [{"run": f"{mode}-r{k + 1}", "mode": mode, "ok": True, "traj": {"rpe100_pct": v, "rpe100_median_pct": v,
                                                                            "ate_aligned_rmse_m": (ates or [0.1] * len(rpes))[k],
                                                                            **({"anchor": {"mode": anchor}} if anchor else {})},
             "map": {"acc_mean_m": (accs or [0.09] * len(rpes))[k], "fscore@0.2": 0.6}} for k, v in enumerate(rpes)]
    return {"case": {"name": name, "seed": seed, "noise_profile": noise}, "runs": runs}


def test_distribution_and_summary():
    d = distribution([0.1, 0.3, None, 0.2, float("nan")])
    assert d["n"] == 3 and d["median"] == pytest.approx(0.2) and d["max"] == 0.3
    s = summarize_runs([{"mode": "livo", "ok": True, "traj": {"rpe100_pct": 0.1}}, {"mode": "livo", "ok": False, "run": "x", "code": 684}])
    assert s["livo"]["ok"] == 1 and s["livo"]["failed"] == 1 and s["livo"]["failures"][0]["code"] == 684


def test_judge_ac14_pass_fail_and_incomplete():
    clean = [_doc("c", 1, "clean", [0.08, 0.09, 0.085])]
    real = [_doc(f"s{s}", s, "realistic", [0.10, 0.12, 0.11]) for s in range(1, 6)]
    j = judge_ac14(clean, real, modes=("livo",))
    assert j["pass"] is True and j["modes"]["livo"]["realistic"]["rpe100_pct"]["n"] == 15 and j["adr"] == "ADR-215"
    assert any(c["clause"] == "realistic RPE100 median in [0.07%, 0.18%]" for c in j["modes"]["livo"]["checks"])
    bad = [_doc(f"s{s}", s, "realistic", [0.10, 0.12, 0.55]) for s in range(1, 6)]                 # 单次 > 0.50%
    assert judge_ac14(clean, bad, modes=("livo",))["pass"] is False
    old_band = [_doc(f"s{s}", s, "realistic", [0.24, 0.26, 0.25]) for s in range(1, 6)]           # r1 区间内、冻结区间外
    assert judge_ac14(clean, old_band, modes=("livo",))["pass"] is False
    low = [_doc(f"s{s}", s, "realistic", [0.069, 0.069, 0.069]) for s in range(1, 6)]
    assert judge_ac14([_doc("c", 1, "clean", [0.05] * 3)], low, modes=("livo",))["pass"] is False
    hi_clean = [_doc("c", 1, "clean", [0.12, 0.11, 0.13])]
    assert judge_ac14(hi_clean, real, modes=("livo",))["pass"] is False
    few = real[:2]
    jf = judge_ac14(clean, few, modes=("livo",))
    assert jf["pass"] is False and any("seeds" in c["clause"] and c["pass"] is False for c in jf["modes"]["livo"]["checks"])
    assert judge_ac14(clean, [], modes=("livo",))["pass"] is None                                # 无 realistic 样本：不判定
    assert AC14["status"] == "frozen" and AC14["main_mode"] == "livo" and AC14["realistic"]["rpe100_median_range_pct"] == [0.07, 0.18]
    assert AC14["hkisland03_resources"] == {"mem_gb_min": 32.0, "cpus_min": 8.0}


def test_judge_ac14_map_clause_uses_event_zero_anchor():
    """ADR-215 (1)：clean 地图到网格按事件零点锚定判定；旧运行（首个估计锚定）取 `reanchor.json`，缺失时未定，r1 口径另报不计入。"""
    real = [_doc(f"s{s}", s, "realistic", [0.10, 0.12, 0.11]) for s in range(1, 6)]
    legacy = [_doc("c", 1, "clean", [0.08] * 3, accs=[0.32] * 3, anchor=None)]
    j = judge_ac14(legacy, real, modes=("livo",))["modes"]["livo"]
    chk = {c["clause"]: c for c in j["checks"]}
    assert chk[MAP_CLAUSE]["pass"] is None and j["pass"] is None                                   # 无事件零点数据：不判定
    assert j["first_estimate_reference"]["map_to_mesh_mean_m"] == pytest.approx(0.32) and j["first_estimate_reference"]["pass"] is False
    for r in legacy[0]["runs"]:
        r["reanchor"] = {"map": {"acc_mean_m": 0.085}, "event0": {"drift_max_m": 0.2}}
    j = judge_ac14(legacy, real, modes=("livo",))["modes"]["livo"]
    chk = {c["clause"]: c for c in j["checks"]}
    assert chk[MAP_CLAUSE]["value"] == pytest.approx(0.085) and chk[MAP_CLAUSE]["pass"] is True and j["pass"] is True
    assert j["clean"]["anchors"] == ["first_estimate"]
    legacy[0]["runs"][2].pop("reanchor")                                                           # 部分运行缺数据：仍不判定
    assert {c["clause"]: c for c in judge_ac14(legacy, real, modes=("livo",))["modes"]["livo"]["checks"]}[MAP_CLAUSE]["pass"] is None
    # 以事件零点评测的运行：直接取 map；不重算首个估计口径
    ev = judge_ac14([_doc("c", 1, "clean", [0.08] * 3, accs=[0.16] * 3)], real, modes=("livo",))["modes"]["livo"]
    assert {c["clause"]: c for c in ev["checks"]}[MAP_CLAUSE]["pass"] is False and ev["first_estimate_reference"]["pass"] is None


# ---------------------------------------------------------------------------------------------------------------- 算例、bag 与地图
H = 30.0
T_IL = np.array([-0.011, -0.02329, 0.04412])


def line_traj(n_s: float, v: float = 6.0, hz: float = 200.0) -> Tum:
    t = T0 + np.arange(0, n_s + 1e-9, 1 / hz)
    p = np.stack([v * (t - T0), np.zeros_like(t), np.full_like(t, H)], 1)
    return Tum(t, p, np.tile([0, 0, 0, 1.0], (len(t), 1)))


def write_plane_bag(path: Path, gt: Tum, frames: int, *, drop_no_return: bool = False, slots: int = 20_000) -> None:
    """平面 z = 0 上的回波（机体水平、R_IL = I）：每帧每 50 个槽位一个回波，其余为零点占位；IMU 200 Hz 静止读数。"""
    rng = np.random.default_rng(0)
    ot = offset_time_ns(slots)
    with SlamBagWriter(path, slots=slots, camera=False) as w:
        for f in range(frames):
            tb = round(T0 * NS) + f * 100_000_000
            i = np.arange(f % 50, slots, 50)
            pts = np.zeros(len(i), [("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("offset_time", "<u4"), ("reflectivity", "u1"),
                                    ("tag", "u1"), ("line", "u1")])
            pts["offset_time"] = ot[i]
            pts["x"], pts["y"] = rng.uniform(-25, 25, len(i)), rng.uniform(-25, 25, len(i))
            pts["z"] = -(H + T_IL[2])
            pts["reflectivity"], pts["line"] = 40, i % 4
            if drop_no_return:
                w.bag.write(w._c_lidar, tb, pack_custom_msg(f, tb, _custom(pts)))      # 只写回波点（违反全槽位合同）
            else:
                w.lidar_frame(tb, pts)
        for k in range(frames * 20 + 1):
            w.imu(round(T0 * NS) + k * 5_000_000, [0.0, 0.0, 0.0], [0.0, 0.0, G])


def _custom(pts):
    out = np.zeros(len(pts), CUSTOM_POINT)
    for k in ("x", "y", "z", "offset_time", "reflectivity", "tag", "line"):
        out[k] = pts[k]
    return out


def plane_mesh(path: Path, half: float = 400.0) -> Path:
    o3d = pytest.importorskip("open3d")
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(np.array([[-half, -half, 0], [half, -half, 0], [half, half, 0],
                                                                        [-half, half, 0.0]])),
                                  o3d.utility.Vector3iVector(np.array([[0, 1, 2], [0, 2, 3]])))
    o3d.io.write_triangle_mesh(str(path), m)
    return path


def make_case(d: Path, *, n_s: float = 20.0, drop_no_return: bool = False) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    gt = line_traj(n_s)
    save_tum(d / "gt_imu.tum", gt)
    write_plane_bag(d / "sim.bag", gt, int(n_s * 10) - 1, drop_no_return=drop_no_return)
    (d / "livo.yaml").write_text(LIVO_YAML)
    (d / "camera.yaml").write_text(CAM_YAML)
    (d / "case.json").write_text(json.dumps({"schema": "drone4d.bench.slam_case.v1", "name": "plane", "uav": "uav0", "lidar_model": "mid360",
                                             "flight_s": n_s, "noise_profile": "clean", "seed": 1, "trajectory_source": "synthetic",
                                             "reflectance_model": "sem_table_v1", "noise_table_version": "test", "mesh": "plane.ply"}))
    return d


def test_load_case_from_case_json_and_r1_rig(tmp_path):
    pytest.importorskip("open3d")
    d = make_case(tmp_path / "c")
    plane_mesh(d / "plane.ply")
    c = load_case(d)
    assert c.name == "plane" and c.mesh == str((d / "plane.ply").resolve()) and c.lidar_model == "mid360"
    assert np.allclose(c.T_I_L[:3, 3], T_IL) and c.meta["reflectance_model"] == "sem_table_v1"
    r = tmp_path / "r1"
    r.mkdir()
    for n in ("sim.bag", "livo.yaml", "camera.yaml", "gt_imu.tum"):
        (r / n).write_bytes((d / n).read_bytes())
    (r / "rig.json").write_text(json.dumps({"noise_profile": "realistic", "seed": 3, "traj": {"duration_s": 159.2, "speed": 6.0, "agl": 30.0},
                                            "camera": {"phase": 0.05}, "world": {"glb": "town.glb"}}))
    c1 = load_case(r, mesh="m.ply")
    assert c1.meta["seed"] == 3 and c1.meta["trajectory_source"] == "r1_spline" and c1.flight_s == 159.2 and c1.mesh == "m.ply"


def test_rebag_round_trip_is_byte_identical(tmp_path):
    d = make_case(tmp_path / "c", n_s=3.0)
    rep = rebag(d / "sim.bag", tmp_path / "re.bag")
    assert rep["messages"]["/livox/lidar"] == 29 and rep["bytes_identical"] == rep["messages"] == rep["body_identical"]
    assert rep["verify"]["lidar_frames"] == 29 and rep["verify"]["returns_mean"] == 400


def test_mesh_distance_map_metrics_reference_and_l0(tmp_path):
    pytest.importorskip("open3d")
    from slam_eval.mapeval import TriMeshDistance, gt_lidar_reference, l0_from_bag, map_metrics, voxel_down

    d = make_case(tmp_path / "c", n_s=5.0)
    mesh = TriMeshDistance(plane_mesh(d / "plane.ply"))
    P = np.array([[0, 0, 0.05], [10, -3, -0.2], [100, 50, 1.0]])
    assert np.allclose(mesh.distance(P), [0.05, 0.2, 1.0], atol=1e-5)
    assert np.allclose(mesh.closest_batch(P, 0.5), [0.05, 0.2, 0.5], atol=1e-5)
    gt = TrajInterp(load_tum(d / "gt_imu.tum"))
    ref, n_raw = gt_lidar_reference(d / "sim.bag", gt, extrinsic_T_I_L(LIVO_YAML))
    assert n_raw == 49 * 400 and len(ref) > 1000
    assert np.abs(mesh.distance(ref)).max() < 1e-3                                       # 真值逐点位姿下回波落在网格上（float32 求距 < 1 mm）
    rng = np.random.default_rng(2)
    Q, _ = voxel_down(ref + np.array([0, 0, 0.08]) + rng.normal(scale=0.01, size=ref.shape), None, 0.1)
    mm = map_metrics(Q, mesh, ref)
    assert mm["acc_mean_m"] == pytest.approx(0.08, abs=0.01) and mm["precision@0.1"] > 0.9 and mm["precision@0.5"] == 1.0
    assert mm["completeness@0.5"] > 0.99 and 0 < mm["fscore@0.2"] <= 1.0
    m = l0_from_bag(d / "sim.bag", gt, extrinsic_T_I_L(LIVO_YAML), uav="uav0")
    st = m.stats()
    assert st["used"] == 49 * 400 and st["occupied_voxels"] > 100
    assert np.abs(mesh.distance(m.occupied_centers())).max() <= np.sqrt(3) / 2 * 0.25 + 1e-6


# ---------------------------------------------------------------------------------------------------------------- 作业
def test_run_rejects_bag_without_no_return_slots_642(tmp_path, fake_docker):
    d = make_case(tmp_path / "c", n_s=3.0, drop_no_return=True)
    rc = cli_main(["run", "--case", str(d), "--out", str(tmp_path / "out"), "--eval-runner", "local", "--repeats", "1"])
    doc = json.loads((tmp_path / "out" / "metrics.json").read_text())
    assert rc == 3 and doc["code"] == 642 and doc["contract"]["ok"] is False
    assert not (fake_docker / "log.txt").exists()                                       # 不启动 FAST-LIVO2 容器


def test_run_end_to_end_with_fake_container(tmp_path, fake_docker, monkeypatch):
    """合成算例 → 合同校验 → 资源（内存模型）→ 2 模式 × 2 重复（假容器写出估计轨迹与地图）→ 本地 eval_sim → metrics.json 与分布。"""
    o3d = pytest.importorskip("open3d")
    d = make_case(tmp_path / "c", n_s=20.0)
    plane_mesh(d / "plane.ply")
    gt = load_tum(d / "gt_imu.tum")
    eps = 0.0005
    p = gt.p[0] + (gt.p - gt.p[0]) * (1 + eps)
    T_F_W = T_of(rotz(0.3), [2.0, -1.0, 0.5])
    est = to_frame(Tum(gt.t[::20], p[::20], gt.q[::20]), T_F_W)
    save_tum(tmp_path / "est.txt", est)
    rng = np.random.default_rng(5)
    Pw = np.stack([rng.uniform(-20, 140, 50_000), rng.uniform(-25, 25, 50_000), np.full(50_000, 0.06)], 1)
    Pf = Pw @ T_F_W[:3, :3].T + T_F_W[:3, 3]
    o3d.io.write_point_cloud(str(tmp_path / "map.pcd"), o3d.geometry.PointCloud(o3d.utility.Vector3dVector(Pf)))
    monkeypatch.setenv("FAKE_EST", str(tmp_path / "est.txt"))
    monkeypatch.setenv("FAKE_PCD", str(tmp_path / "map.pcd"))
    out = tmp_path / "out"
    rc = cli_main(["run", "--case", str(d), "--out", str(out), "--eval-runner", "local", "--repeats", "2", "--name-prefix", "d3-test",
                   "--l0"])
    doc = json.loads((out / "metrics.json").read_text())
    assert rc == 0 and doc["schema"] == SCHEMA and doc["status"] == "completed" and doc["contract"]["ok"]
    assert doc["resources"]["mem_gb_applied"] == job_resources("mid360", 20.0)["mem_gb"] and doc["resources"]["cpus_applied"] == 4.0
    assert sorted(r["run"] for r in doc["runs"]) == ["lio-r1", "lio-r2", "livo-r1", "livo-r2"]
    for r in doc["runs"]:
        assert r["traj"]["rpe100_pct"] == pytest.approx(100 * eps, rel=1e-3)
        assert r["map"]["acc_mean_m"] == pytest.approx(0.06, abs=1e-3) and r["map"]["ref_points"] > 1000
        assert r["r_map"]["r_map_slope"] > 0 and r["per_frame"]["lio"]["n"] == 1
        assert r["traj"]["anchor"]["mode"] == "first_estimate" and "event zero" in r["traj"]["anchor"]["fallback"]   # 算例无事件零点
    lio_yaml = (out / "runs" / "lio-r1" / "livo.yaml").read_text()
    assert yaml_get(lio_yaml, "common/img_en") == "0" and yaml_get(lio_yaml, "evo/pose_output_en") == "true"
    assert yaml_get((out / "runs" / "livo-r2" / "livo.yaml").read_text(), "common/img_en") == "1"
    s = doc["summary"]["livo"]
    assert s["ok"] == 2 and s["rpe100_pct"]["median"] == pytest.approx(100 * eps, rel=1e-3)
    l0 = [r for r in doc["runs"] if r.get("l0")]
    assert len(l0) == 1 and l0[0]["l0"]["mesh_distance"]["max_m"] <= 0.25 * np.sqrt(3) / 2 + 1e-6
    assert (out / "runs" / "livo-r1" / "est_world.tum").exists() and (out / "gt_lidar_ref.ply").exists()
    j = tmp_path / "judge.json"
    assert cli_main(["judge", "--clean", str(out / "metrics.json"), "--out", str(j)]) == 1                 # 无 realistic 样本，不判通过
    assert json.loads(j.read_text())["modes"]["livo"]["checks"][0]["pass"] is True                       # clean RPE100 ≤ 0.10%
    rg = tmp_path / "regress.json"
    rg.write_text(json.dumps({"ok": True, "image": {"id": "sha256:other"}, "ate": {"ate_rmse_m": 1.0}}))
    assert cli_main(["run", "--case", str(d), "--out", str(tmp_path / "out2"), "--require-regress", str(rg)]) == 2
