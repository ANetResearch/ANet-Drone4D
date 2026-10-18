"""按机采集作业（docs/05 §10.2 P0a-1；M25 §6.2、§6.4、§6.7；M27 §7.5、§7.6）：脚本位姿驱动光栅相机，产出一个 capture Run。

输入为采集规格（JSON）：世界、时长、机队；每机一条脚本航迹（`orbit` 环绕或 `waypoints` 航点，高度为离地高度，按 DSM 2 m 栅格
贴地）与若干相机（档位 `profile`：`rgb720-gs` 缺省或 `rgb1080-rs` 卷帘；交付分辨率、水平视场、有理数帧率、标称挂载、固定云台角、
输出集合）。位姿以 250 Hz 写入 PoseHistory（P0a-1
允许脚本位姿，P0a-3 切换为 PoseHistory 真值通道时相机接口不变，ADR-193）。规格顶层 `noise_profile`（clean、realistic、harsh，
缺省 realistic）与 `seed`（run_seed，缺省 0）选择效果链与噪声档（M25 §6.5；`camera.effects`）：RGB 经曝光、渐晕、噪声与 ISP，
观测深度经 L2-depth 传感器模型（均在 GPU 编码遍内），云台读数加编码器噪声；真值深度与真值云台角不变。规格可选 `event`
（`{path: 事件版本目录, offset_s}`，相对路径按 data_root 解析）：以该磁带为动态源（M25 §6.4.4），采集时刻 t 对应事件时刻
offset_s + t，演员按 t_mid 的磁带状态绘制并写出 `instance_unocc` 与 `objects[]`。每台 LiDAR（`lidars[]`，M25 §6.8）附带其内置
IMU（M25 §6.9，200 Hz，`<name>_imu`）：比力与角速度由脚本位姿差分得到，每个 LiDAR 帧周期写一个 ImuObs 块（`/uav/{id}/imu/<name>_imu`）。

输出（`<data_root>/dataset/<event_id>/`）：`runs/<run_id>/{raw.mcap, frames.idx, run_manifest.json}` 与
`truth/run_<run_id>.{mcap, frames.idx}`。用法：`python -m awr.sensorsvc.capture spec.json [--data-root /data/d3]`。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from awr.sensorsvc.camera.effects import (
    FX_VERSION,
    NOISE_TABLE_VERSION,
    AeState,
    finish_frame,
    frame_params,
    gimbal_reading,
    profile,
    with_profile,
)
from awr.sensorsvc.camera.model import CAMERA_PROFILES, CameraModel, IspConfig
from awr.sensorsvc.camera.snap import ground_z
from awr.sensorsvc.errors import SensorSvcError
from awr.sensorsvc.frame import FrameContext, camera_frame
from awr.sensorsvc.lidar.model import MOUNT_INVERTED_MAPPING
from awr.sensorsvc.poses import Mount, PoseHistory, sensor_pose
from awr.sensorsvc.timing import check_exposure, parse_rate, t_end_ns, t_mid_ns

__all__ = ["CameraSpec", "LidarSpec", "UavSpec", "build_history", "load_spec", "run_capture"]

POSE_HZ = 250
LIDAR_SENSOR_NO0 = 100                    # LiDAR 的传感器号（与相机号分开；无云台样本，即机体固连）
NS = 1_000_000_000


@dataclass(frozen=True)
class CameraSpec:
    name: str
    sensor_no: int
    model: CameraModel
    fps: str
    mount: Mount
    gimbal_az_deg: float
    gimbal_el_deg: float
    outputs: frozenset[str]


@dataclass(frozen=True)
class LidarSpec:
    name: str
    sensor_no: int
    model_id: str
    mount: Mount


@dataclass(frozen=True)
class UavSpec:
    uav: str
    path: dict
    cameras: tuple[CameraSpec, ...]
    lidars: tuple[LidarSpec, ...] = ()


def load_spec(doc: dict) -> tuple[dict, list[UavSpec]]:
    fleet = []
    for u in doc["fleet"]:
        cams = []
        for k, c in enumerate(u["cameras"]):
            prof = CAMERA_PROFILES.get(str(c.get("profile", "rgb720-gs")))
            if prof is None:
                raise SensorSvcError(649, f"unknown camera profile {c.get('profile')!r}")
            w, h = c.get("size", prof["size"])
            cm = CameraModel.from_hfov(int(w), int(h), float(c.get("hfov_deg", 84.0)), fps=str(c.get("fps", "10")),
                                       exposure_s=float(c.get("exposure_s", prof["exposure_s"])),
                                       readout_s=float(c.get("readout_s", prof["readout_s"])),
                                       isp=IspConfig(jpeg_quality=int(c.get("jpeg_quality", 90))))
            check_exposure(parse_rate(cm.fps), round(cm.exposure_s * NS), round(cm.readout_s * NS))
            g = c.get("gimbal", {})
            cams.append(CameraSpec(c.get("name", f"cam{k}"), k, cm, cm.fps, Mount(tuple(c.get("mount_xyz_m", (0.1, 0.0, -0.05))),
                                                                                 tuple(c.get("mount_rpy_deg", (0.0, 0.0, 0.0)))),
                                   float(g.get("az_deg", 0.0)), float(g.get("el_deg", -30.0)), frozenset(c.get("outputs", ["rgb", "depth"]))))
        lids = []
        for k, L in enumerate(u.get("lidars", [])):
            mt = L.get("mount", "inverted_mapping")
            xyz, rpy = MOUNT_INVERTED_MAPPING if mt == "inverted_mapping" else (mt["xyz_m"], mt["rpy_deg"])
            lids.append(LidarSpec(L.get("name", f"lidar{k}"), LIDAR_SENSOR_NO0 + k, str(L.get("model", "mid360")), Mount(tuple(xyz), tuple(rpy))))
        fleet.append(UavSpec(u["uav"], u["path"], tuple(cams), tuple(lids)))
    if len({u.uav for u in fleet}) != len(fleet):
        raise SensorSvcError(649, "duplicate uav ids")
    return doc, fleet


def _yaw_q(yaw: float) -> np.ndarray:
    return np.array([0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)])


def path_state(path: dict, t: float, gz) -> tuple[np.ndarray, float]:
    """脚本航迹在 t 秒的 (位置 ENU, 航向 rad)。高度为离地高度（DSM），水平飞行。"""
    if path["kind"] == "orbit":
        cx, cy = path.get("center", (0.0, 0.0))
        r = float(path["radius_m"])
        w = 2 * math.pi / float(path["period_s"]) * (1 if path.get("ccw", True) else -1)
        a = math.radians(path.get("phase_deg", 0.0)) + w * t
        x, y = cx + r * math.cos(a), cy + r * math.sin(a)
        yaw = a + math.copysign(math.pi / 2, w)
        return np.array([x, y, gz(x, y) + float(path["agl_m"])]), yaw
    if path["kind"] == "waypoints":
        wp = np.asarray(path["points"], np.float64)               # [t_s, x, y, agl_m, yaw_deg]
        t = min(max(t, wp[0, 0]), wp[-1, 0])
        i = min(int(np.searchsorted(wp[:, 0], t, side="right")) - 1, len(wp) - 2)
        u = (t - wp[i, 0]) / max(wp[i + 1, 0] - wp[i, 0], 1e-9)
        x, y, agl = (1 - u) * wp[i, 1:4] + u * wp[i + 1, 1:4]
        dyaw = (wp[i + 1, 4] - wp[i, 4] + 180) % 360 - 180
        return np.array([x, y, gz(x, y) + agl]), math.radians(wp[i, 4] + u * dyaw)
    raise SensorSvcError(649, f"unknown path kind {path['kind']!r}")


def build_history(u: UavSpec, duration_s: float, gz, t0_ns: int = 0) -> PoseHistory:
    H = PoseHistory(u.uav, POSE_HZ)
    n = round(duration_s * POSE_HZ)
    for j in range(n + 1):
        t_ns = t0_ns + j * (NS // POSE_HZ)
        p, yaw = path_state(u.path, (t_ns - t0_ns) / NS, gz)
        H.append(t_ns, p, _yaw_q(yaw))
    for c in u.cameras:
        H.append_gimbal(c.sensor_no, t0_ns, math.radians(c.gimbal_az_deg), math.radians(c.gimbal_el_deg))
    return H


def run_capture(spec: dict, data_root: Path, *, worlds_dir: Path | None = None, max_frames: int | None = None, log=print) -> dict:
    from awr.bench.recorder.channels import parse_truth_topic
    from awr.bench.recorder.frames_idx import build_frames_idx
    from awr.bench.recorder.writer import RunWriter
    from awr.sensorsvc.camera.raster import RS_NODES, RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance

    doc, fleet = load_spec(spec)
    prof = profile(str(doc.get("noise_profile", "realistic")))
    seed = int(doc.get("seed", 0))
    fleet = [replace(u, cameras=tuple(replace(c, model=with_profile(c.model, prof)) for c in u.cameras)) for u in fleet]
    run_id, event_id = doc["run_id"], doc.get("event_id", "capture")
    wdir = Path(worlds_dir or data_root / "worlds") / doc["world_id"]
    world = json.loads((wdir / "world.json").read_text(encoding="utf-8"))
    tape, ev_off_ns, ev_meta = None, 0, None
    if doc.get("event"):
        from awr.sim.events.tape import open_tape

        ep = Path(doc["event"]["path"])
        tape = open_tape(ep if ep.is_absolute() else Path(data_root) / ep)
        tape.check_world(world["contentVersion"])                    # 615：磁带须为当前世界版本编译
        ev_off_ns = round(float(doc["event"].get("offset_s", 0.0)) * NS)
        man = json.loads((tape.path.parent / "tape.json").read_text(encoding="utf-8"))
        ev_meta = {"event_id": tape.header["event_id"], "version": tape.header["event_version"], "content_sha256": man["content_sha256"],
                   "offset_s": ev_off_ns / NS}
    run_dir = Path(data_root) / "dataset" / event_id / "runs" / run_id
    truth_dir = run_dir.parent.parent / "truth"
    if run_dir.exists():
        raise SensorSvcError(640, f"run {run_id} already exists")
    run_dir.mkdir(parents=True)
    truth_dir.mkdir(parents=True, exist_ok=True)
    duration = float(doc["duration_s"])
    t_start = time.time_ns()
    gz = ground_z(wdir)
    t0 = time.perf_counter()
    cam = RasterCamera(load_appearance(wdir))
    log(f"[capture] world {doc['world_id']} loaded in {time.perf_counter() - t0:.1f} s ({cam.info.get('GL_RENDERER')})")
    ctx = FrameContext(run_id, run_id, world["contentVersion"], str(cam.info.get("GL_RENDERER")), str(cam.info.get("GL_VERSION")).split()[-1],
                       fx_version=FX_VERSION, noise_tier=prof.name, noise_table_version=NOISE_TABLE_VERSION)
    hist = {u.uav: build_history(u, duration, gz) for u in fleet}
    if tape is not None:
        cam.set_actors(tape.actors)
        cam.set_sun(tuple((world.get("sensorHints") or {}).get("bakedSunAzElDeg") or ()) or None)
        ctx = replace(ctx, dynamic_source_id=ev_meta["content_sha256"])
        log(f"[capture] event {ev_meta['event_id']}@v{ev_meta['version']}: {tape.n_actors} actors, offset {ev_meta['offset_s']} s")
    # 全部 (t_mid, uav, cam, seq) 按时刻排序后渲染：与锁步运行中逐 tick 交付的顺序一致
    jobs = []
    for u in fleet:
        for c in u.cameras:
            rate = parse_rate(c.fps)
            k = 1
            while (te := t_end_ns(0, rate, k)) <= duration * NS:
                jobs.append((t_mid_ns(te, round(c.model.exposure_s * NS), round(c.model.readout_s * NS)), u.uav, c, k))
                k += 1
    lidar, imus = None, {}
    uav_no_of = {u.uav: i for i, u in enumerate(fleet)}
    if any(u.lidars for u in fleet):                             # 网格 LiDAR（M25 §6.8）：帧末时刻排入同一时序
        from awr.sensorsvc.lidar.mesh import MeshLidar
        from awr.sensorsvc.lidar.model import load_lidar_model
        from awr.world.mesh.query import open_mesh_world_query

        lmodel = load_lidar_model()
        t1 = time.perf_counter()
        lidar = MeshLidar(open_mesh_world_query(wdir, Path(data_root) / "cache" / "worldquery"), lmodel, tape.actors if tape else None)
        log(f"[capture] mesh lidar ready in {time.perf_counter() - t1:.1f} s")
        imus = {(u.uav, L.name): _imu_track(hist[u.uav], L, duration, seed, uav_no_of[u.uav], prof.name) for u in fleet for L in u.lidars}
        for u in fleet:
            for L in u.lidars:
                k = 0
                while (k + 1) * lmodel.frame_ns <= duration * NS:
                    jobs.append(((k + 1) * lmodel.frame_ns, u.uav, L, k))
                    k += 1
    jobs.sort(key=lambda j: (j[0], j[1], j[2].sensor_no))
    if max_frames:
        jobs = jobs[:max_frames]
    ms: list[float] = []
    uav_of = {u.uav: u for u in fleet}
    uav_no = {u.uav: i for i, u in enumerate(fleet)}
    ae = {(u.uav, c.name): AeState() for u in fleet for c in u.cameras}
    with RunWriter(run_dir / "raw.mcap") as raw, RunWriter(truth_dir / f"run_{run_id}.mcap") as tru:
        raw.metadata("run", {"run_id": run_id, "event_id": event_id, "kind": "capture", "world_content_version": world["contentVersion"]})
        for i, (tm, uav, c, seq) in enumerate(jobs):
            if isinstance(c, LidarSpec):
                _lidar_frame(lidar, raw, tru, hist[uav], uav, uav_no[uav], c, seq, tm - lidar.m.frame_ns, seed, prof.name, tape, ev_off_ns,
                             run_id, ctx.dynamic_source_id)
                _imu_block(raw, imus[(uav, c.name)], uav, c, seq, tm - lidar.m.frame_ns, tm)
                continue
            pos, R, g = sensor_pose(hist[uav], c.mount, c.sensor_no, tm)
            kw = {"seed": seed, "uav_no": uav_no[uav], "sensor_no": c.sensor_no, "frame_index": seq}
            st = ae[(uav, c.name)]
            fxp = frame_params(c.model, st, t_mid_s=tm / NS, depth=prof.depth if "depth" in c.outputs else None, **kw)
            ro = round(c.model.readout_s * NS)
            rs = None
            if ro > 0:                                           # 卷帘：读出窗口内 9 个位姿节点（M25 §15.4）
                rs = []
                for k in range(RS_NODES):
                    pk, Rk, _ = sensor_pose(hist[uav], c.mount, c.sensor_no, tm - ro // 2 + (k * ro) // (RS_NODES - 1))
                    rs.append((Rk, pk))
            af = None
            if tape is not None:
                from awr.sensorsvc.camera.raster import ActorFrame

                stt = tape.actor_states(ev_off_ns + tm)
                v = np.isin(stt.state, (1, 2))
                af = ActorFrame(stt.ids[v], stt.pos_enu_m[v], stt.yaw_rad[v])
            out = cam.render(c.model, R, pos, fx=fxp, rs_nodes=rs, actors=af)
            finish_frame(st, fxp, out.meter_sub)
            ms.append(out.draw_ms + out.readback_ms)
            d_obs = out.depth_obs
            g_read = gimbal_reading(g, prof.gimbal_sigma_deg, **kw)
            rec, blocks = camera_frame(ctx, uav=uav, sensor_id=c.name, seq=seq, t_mid_ns=tm, exposure_ns=round(c.model.exposure_s * NS),
                                       readout_ns=ro, cm=c.model, mount=c.mount, gimbal_reading=g_read, gimbal_true=g, pos_enu_m=pos, R_ws=R,
                                       out=out, outputs=c.outputs | {"gt_depth"}, depth_obs=d_obs,
                                       depth_range_m=(prof.depth.d_min_m, prof.depth.d_max_m), actors=tape.actors if tape else None)
            jpeg = blocks[rec["agent_view"]["rgb"]["ref"]]
            raw.write_camera(uav, c.name, seq, tm, jpeg, depth_m=d_obs, period_ns=int(NS / parse_rate(c.fps)))
            agent = {rec["agent_view"]["rgb"]["ref"], rec["agent_view"].get("depth", {}).get("ref")}      # 观测载荷只进 raw.mcap
            tru.write_camera_truth(uav, c.name, seq, tm, rec, {k: v for k, v in blocks.items() if k not in agent})
            if (i + 1) % 100 == 0 and ms:
                log(f"[capture] {i + 1}/{len(jobs)} frames, render p50 {np.median(ms):.1f} ms")
    build_frames_idx(run_dir / "raw.mcap", run_dir / "frames.idx")
    build_frames_idx(truth_dir / f"run_{run_id}.mcap", truth_dir / f"run_{run_id}.frames.idx", parse=parse_truth_topic)
    cam.release()
    manifest = {"schema": "drone4d.capture_manifest.v0", "run_id": run_id, "event_id": event_id, "kind": "capture", "state": "SUCCEEDED",
                "world_id": doc["world_id"], "world_content_version": world["contentVersion"], "duration_s": duration,
                "created_unix_ns": t_start, "ended_unix_ns": time.time_ns(), "renderer": ctx.renderer_version, "gpu_model": ctx.gpu_model,
                "frames": len(jobs), "render_ms_p50": round(float(np.median(ms)), 2) if ms else None,
                "noise_profile": prof.name, "seed": seed, "event": ev_meta,
                "fx_version": FX_VERSION, "noise_table_version": NOISE_TABLE_VERSION,
                "fleet": [{"uav": u.uav, "path": u.path,
                           "lidars": [{"name": L.name, "model": L.model_id, "mount": {"xyz_m": list(L.mount.xyz_m), "rpy_deg": list(L.mount.rpy_deg)},
                                       "imu": {"name": f"{L.name}_imu", "rate_hz": 200, "mount_xyz_m": imus[(u.uav, L.name)][1]}}
                                      for L in uav_of[u.uav].lidars],
                           "cameras": [{"name": c.name, "width": c.model.width, "height": c.model.height,
                                                                       "fps": c.fps, "hfov_deg": round(c.model.hfov_deg, 3),
                                                                       "K": [c.model.fx, 0.0, c.model.cx, 0.0, c.model.fy, c.model.cy, 0.0, 0.0, 1.0],
                                                                       "gimbal": {"az_deg": c.gimbal_az_deg, "el_deg": c.gimbal_el_deg},
                                                                       "outputs": sorted(c.outputs)} for c in uav_of[u.uav].cameras]}
                          for u in fleet]}
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"[capture] {run_id}: {len(jobs)} frames, p50 {manifest['render_ms_p50']} ms, raw {(run_dir / 'raw.mcap').stat().st_size >> 20} MiB")
    return manifest


def _lidar_frame(lidar, raw, tru, H: PoseHistory, uav: str, uav_no: int, L: LidarSpec, seq: int, t_s: int, seed: int, tier: str, tape,
                 ev_off_ns: int, run_id: str, source_id: str) -> None:
    """一帧网格 LiDAR：11 个位姿节点（t_s + 10 ms·m）、演员取磁带状态，观测写 raw.mcap、槽位真值写真值文件。"""
    from awr.sensorsvc.poses import R_to_quat_xyzw

    m = lidar.m
    seg = m.frame_ns // (m.pose_nodes - 1)
    nodes, nd_meta = [], []
    for k in range(m.pose_nodes):
        p, Rk, _ = sensor_pose(H, L.mount, L.sensor_no, t_s + k * seg)
        nodes.append((Rk, p))
        nd_meta.append({"t_ns": int(t_s + k * seg), "pos_enu_m": [float(v) for v in p], "q_ws_xyzw": [float(v) for v in R_to_quat_xyzw(Rk)]})
    st_fn = (lambda t: tape.actor_states(ev_off_ns + t)) if tape is not None else None
    sc = lidar.scan(seq, t_s, nodes, actor_states=st_fn, seed=seed, uav_no=uav_no, sensor_no=L.sensor_no - LIDAR_SENSOR_NO0, tier=tier)
    fid = f"{run_id}/{uav}/{L.name}/{seq}"
    hdr = {"schema": "awr.sensor.lidar_frame.v2", "sensor_id": L.name, "frame_id": fid, "frame_seq": int(seq), "timebase_ns": int(t_s),
           "point_dt_ns": m.point_dt_ns, "slots_per_frame": m.slots_per_frame, "point_count": len(sc.points), "model": L.model_id,
           "pattern_id": "mid360-nonrep@1", "return_mode": "first", "sync_type": "sim",
           "mount_nominal": {"xyz_m": list(L.mount.xyz_m), "rpy_deg": list(L.mount.rpy_deg)}, "noise_tier": tier}
    raw.write_lidar(uav, L.name, seq, t_s, hdr, sc.points, period_ns=m.frame_ns)
    gt = {"schema": "awr.sensor.lidar_gt.v1", "frame_id": fid, "frame_seq": int(seq), "timebase_ns": int(t_s), "slots": m.slots_per_frame,
          "codec": "zstd", "T_world_sensor_nodes": nd_meta, "reflectance_model": "sem_table_v1", "beta_905_per_m": 0.0,
          "dynamic_source_id": source_id, "actor_hits": sc.actor_hits, "actor_rays": sc.actor_rays,
          "seed": {"run_seed": int(seed), "uav_no": int(uav_no), "sensor_no": int(L.sensor_no - LIDAR_SENSOR_NO0)}}
    tru.write_lidar_truth(uav, L.name, seq, t_s, gt, sc.gt_inst, sc.gt_flags)


def _imu_track(H: PoseHistory, L: LidarSpec, duration_s: float, seed: int, uav_no: int, tier: str):
    """LiDAR 内置 IMU 的整段样本（M25 §6.9）：IMU 帧与 LiDAR 帧同向，杆臂取挂载点加 mid360 外参。"""
    from awr.sensorsvc.imu import MID360_IMU_IN_LIDAR, kinematics_from_poses, load_imu_model, synth_imu

    t = np.asarray(H.t, np.int64)
    omega, f_b = kinematics_from_poses(t, np.asarray(H.p), np.asarray(H.q))
    R_bl = L.mount.R
    r = np.asarray(L.mount.xyz_m, np.float64) + R_bl @ np.asarray(MID360_IMU_IN_LIDAR)
    s = synth_imu(t, omega, f_b, R_bi=R_bl, r_imu_b=r, model=load_imu_model(), t_from_ns=int(t[0]), t_to_ns=round(duration_s * NS),
                  tier=tier, seed=seed, uav_no=uav_no, sensor_no=L.sensor_no - LIDAR_SENSOR_NO0)
    return s, [float(v) for v in r]


def _imu_block(raw, track, uav: str, L: LidarSpec, seq: int, t_from: int, t_to: int) -> None:
    s, r = track
    i0, i1 = np.searchsorted(s.t_ns, [t_from, t_to])
    if i1 <= i0:
        return
    name = f"{L.name}_imu"
    obs = {"sensor_id": name, "seq": int(seq), "t0_ns": int(s.t_ns[i0]), "dt_ns": int(NS // 200), "n": int(i1 - i0),
           "acc_mps2": s.acc_mps2[i0:i1].astype("<f4").tobytes(), "gyro_rps": s.gyro_rps[i0:i1].astype("<f4").tobytes(),
           "mount_nominal": {"xyz_m": r, "rpy_deg": list(L.mount.rpy_deg)}}
    raw.write_imu(uav, name, seq, int(s.t_ns[i0]), obs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.sensorsvc.capture")
    ap.add_argument("spec")
    ap.add_argument("--data-root", default="/data/d3")
    ap.add_argument("--worlds-dir")
    ap.add_argument("--max-frames", type=int)
    a = ap.parse_args(argv)
    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    run_capture(spec, Path(a.data_root), worlds_dir=Path(a.worlds_dir) if a.worlds_dir else None, max_frames=a.max_frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
