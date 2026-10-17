"""按机采集作业（docs/05 §10.2 P0a-1；M25 §6.2、§6.4、§6.7；M27 §7.5、§7.6）：脚本位姿驱动光栅相机，产出一个 capture Run。

输入为采集规格（JSON）：世界、时长、机队；每机一条脚本航迹（`orbit` 环绕或 `waypoints` 航点，高度为离地高度，按 DSM 2 m 栅格
贴地）与若干相机（交付分辨率、水平视场、有理数帧率、标称挂载、固定云台角、输出集合）。位姿以 250 Hz 写入 PoseHistory（P0a-1
允许脚本位姿，P0a-3 切换为 PoseHistory 真值通道时相机接口不变，ADR-193）。

输出（`<data_root>/dataset/<event_id>/`）：`runs/<run_id>/{raw.mcap, frames.idx, run_manifest.json}` 与
`truth/run_<run_id>.{mcap, frames.idx}`。用法：`python -m awr.sensorsvc.capture spec.json [--data-root /data/d3]`。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from awr.sensorsvc.camera.model import CameraModel, IspConfig
from awr.sensorsvc.camera.snap import ground_z
from awr.sensorsvc.errors import SensorSvcError
from awr.sensorsvc.frame import FrameContext, camera_frame
from awr.sensorsvc.poses import Mount, PoseHistory, sensor_pose
from awr.sensorsvc.timing import check_exposure, parse_rate, t_end_ns, t_mid_ns

__all__ = ["CameraSpec", "UavSpec", "build_history", "load_spec", "run_capture"]

POSE_HZ = 250
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
class UavSpec:
    uav: str
    path: dict
    cameras: tuple[CameraSpec, ...]


def load_spec(doc: dict) -> tuple[dict, list[UavSpec]]:
    fleet = []
    for u in doc["fleet"]:
        cams = []
        for k, c in enumerate(u["cameras"]):
            w, h = c.get("size", [1280, 720])
            cm = CameraModel.from_hfov(int(w), int(h), float(c.get("hfov_deg", 84.0)), fps=str(c.get("fps", "10")),
                                       exposure_s=float(c.get("exposure_s", 0.008)), isp=IspConfig(jpeg_quality=int(c.get("jpeg_quality", 90))))
            check_exposure(parse_rate(cm.fps), round(cm.exposure_s * NS), 0)
            g = c.get("gimbal", {})
            cams.append(CameraSpec(c.get("name", f"cam{k}"), k, cm, cm.fps, Mount(tuple(c.get("mount_xyz_m", (0.1, 0.0, -0.05))),
                                                                                 tuple(c.get("mount_rpy_deg", (0.0, 0.0, 0.0)))),
                                   float(g.get("az_deg", 0.0)), float(g.get("el_deg", -30.0)), frozenset(c.get("outputs", ["rgb", "depth"]))))
        fleet.append(UavSpec(u["uav"], u["path"], tuple(cams)))
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
    from awr.sensorsvc.camera.raster import RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance

    doc, fleet = load_spec(spec)
    run_id, event_id = doc["run_id"], doc.get("event_id", "capture")
    wdir = Path(worlds_dir or data_root / "worlds") / doc["world_id"]
    world = json.loads((wdir / "world.json").read_text(encoding="utf-8"))
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
    ctx = FrameContext(run_id, run_id, world["contentVersion"], str(cam.info.get("GL_RENDERER")), str(cam.info.get("GL_VERSION")).split()[-1])
    hist = {u.uav: build_history(u, duration, gz) for u in fleet}
    # 全部 (t_mid, uav, cam, seq) 按时刻排序后渲染：与锁步运行中逐 tick 交付的顺序一致
    jobs = []
    for u in fleet:
        for c in u.cameras:
            rate = parse_rate(c.fps)
            k = 1
            while (te := t_end_ns(0, rate, k)) <= duration * NS:
                jobs.append((t_mid_ns(te, round(c.model.exposure_s * NS)), u.uav, c, k))
                k += 1
    jobs.sort(key=lambda j: (j[0], j[1], j[2].sensor_no))
    if max_frames:
        jobs = jobs[:max_frames]
    ms = []
    uav_of = {u.uav: u for u in fleet}
    with RunWriter(run_dir / "raw.mcap") as raw, RunWriter(truth_dir / f"run_{run_id}.mcap") as tru:
        raw.metadata("run", {"run_id": run_id, "event_id": event_id, "kind": "capture", "world_content_version": world["contentVersion"]})
        for i, (tm, uav, c, seq) in enumerate(jobs):
            pos, R, g = sensor_pose(hist[uav], c.mount, c.sensor_no, tm)
            out = cam.render(c.model, R, pos)
            ms.append(out.draw_ms + out.readback_ms)
            rec, blocks = camera_frame(ctx, uav=uav, sensor_id=c.name, seq=seq, t_mid_ns=tm, exposure_ns=round(c.model.exposure_s * NS),
                                       readout_ns=0, cm=c.model, mount=c.mount, gimbal_reading=g, gimbal_true=g, pos_enu_m=pos, R_ws=R,
                                       out=out, outputs=c.outputs | {"gt_depth"})
            jpeg = blocks[rec["agent_view"]["rgb"]["ref"]]
            raw.write_camera(uav, c.name, seq, tm, jpeg, depth_m=out.depth_m if "depth" in c.outputs else None,
                             period_ns=int(NS / parse_rate(c.fps)))
            tru.write_camera_truth(uav, c.name, seq, tm, rec, {k: v for k, v in blocks.items() if not k.endswith(("rgb.jpg", "depth.f32"))})
            if (i + 1) % 100 == 0:
                log(f"[capture] {i + 1}/{len(jobs)} frames, render p50 {np.median(ms):.1f} ms")
    build_frames_idx(run_dir / "raw.mcap", run_dir / "frames.idx")
    build_frames_idx(truth_dir / f"run_{run_id}.mcap", truth_dir / f"run_{run_id}.frames.idx", parse=parse_truth_topic)
    cam.release()
    manifest = {"schema": "drone4d.capture_manifest.v0", "run_id": run_id, "event_id": event_id, "kind": "capture", "state": "SUCCEEDED",
                "world_id": doc["world_id"], "world_content_version": world["contentVersion"], "duration_s": duration,
                "created_unix_ns": t_start, "ended_unix_ns": time.time_ns(), "renderer": ctx.renderer_version, "gpu_model": ctx.gpu_model,
                "frames": len(jobs), "render_ms_p50": round(float(np.median(ms)), 2) if ms else None,
                "fleet": [{"uav": u.uav, "path": u.path, "cameras": [{"name": c.name, "width": c.model.width, "height": c.model.height,
                                                                       "fps": c.fps, "hfov_deg": round(c.model.hfov_deg, 3),
                                                                       "K": [c.model.fx, 0.0, c.model.cx, 0.0, c.model.fy, c.model.cy, 0.0, 0.0, 1.0],
                                                                       "gimbal": {"az_deg": c.gimbal_az_deg, "el_deg": c.gimbal_el_deg},
                                                                       "outputs": sorted(c.outputs)} for c in uav_of[u.uav].cameras]}
                          for u in fleet]}
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"[capture] {run_id}: {len(jobs)} frames, p50 {manifest['render_ms_p50']} ms, raw {(run_dir / 'raw.mcap').stat().st_size >> 20} MiB")
    return manifest


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
