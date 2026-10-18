#!/usr/bin/env python3
"""tools/bench/d3/refdet/gen_data.py：参考检测器 `ref_det_v1` 的训练数据（M26-FR-121；P0a-3）。在 dmax 的 d3-sensorsvc 镜像（EGL）中运行。

按 L2 相机的同一渲染链生成帧（M25 光栅相机 + 效果链 realistic 档 + JPEG q90，与 Env 交付给 Agent 的字节同一路径），标注取可见度
归约（M25 §6.6.1）的可见框：人员（语义 32）与车辆（33）；可见像素 ≥ 12 且可见比例 ≥ 0.2 的为正样本，其余可见演员标为忽略区域。

采样：每段 8 帧（10 Hz，相机以 6 m/s 沿直线移动并持续指向目标，自动曝光随段推进，前 2 帧丢弃）。目标以 80% 概率取该时刻在场的演员
（脚本与背景行人、车辆），其余取任务区（简报区域多边形的外接矩形）内随机点；相机离地 25–125 m（任务高度包络 30–120 m）、水平偏移 0–1.6 倍高度，偏航与俯仰
加随机扰动。段号决定随机数（`--seed` + 段号），同参数两次生成逐字节相同。

输出 `<out>/shard-<k>.bin`（JPEG 字节顺序拼接）与 `<out>/shard-<k>.jsonl`（每帧一行：off、len、t_ns、alt_agl_m、boxes
[[cls, x, y, w, h, ignore], …]，全分辨率像素），`<out>/meta.json`（相机内参、类别表、参数）。

用法：python tools/bench/d3/refdet/gen_data.py --world worlds/oryahovo-crop1 --event d3data/events/crop-handoff-001/v1 \\
          --out out/refdet/data --segments 4000 [--seed 7] [--shard 2000]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "python"))

CLASSES = {32: 0, 33: 1}                     # 语义码 → 类别号（person、vehicle）
NAMES = ["person", "vehicle"]
SEG_LEN, SEG_SKIP = 8, 2
TICK_NS = 100_000_000
MIN_PX, MIN_VIS = 12, 0.2


def _look(cam: np.ndarray, tgt: np.ndarray) -> tuple[float, float]:
    d = tgt - cam
    return math.atan2(d[1], d[0]), math.atan2(d[2], math.hypot(d[0], d[1]))


def main(argv: list[str] | None = None) -> int:
    from awr.agentsdk.task import ALT_ENVELOPE_AGL_M, task_from_event
    from awr.sensorsvc.camera.actors import SEM_CODE, visibility_objects
    from awr.sensorsvc.camera.effects import AeState, finish_frame, frame_params, profile, with_profile
    from awr.sensorsvc.camera.model import CAMERA_PROFILES, IspConfig
    from awr.sensorsvc.camera.model import CameraModel as SvcCamera
    from awr.sensorsvc.camera.raster import ActorFrame, RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance
    from awr.sensorsvc.frame import encode_jpeg
    from awr.sim.events.tape import open_tape
    from awr.sim.sensors.frames import gimbal_R
    from awr.world.geometry.query import open_dsm_world_query

    ap = argparse.ArgumentParser(prog="gen_data.py")
    ap.add_argument("--world", type=Path, required=True)
    ap.add_argument("--event", type=Path, required=True, help="compiled event version directory (tape.bin, plan.json)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--segments", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--shard", type=int, default=2000, help="frames per shard")
    ap.add_argument("--hfov-deg", type=float, default=60.0)
    a = ap.parse_args(argv)

    a.out.mkdir(parents=True, exist_ok=True)
    tape = open_tape(a.event)
    world = json.loads((a.world / "world.json").read_text(encoding="utf-8"))
    plan = json.loads((a.event / "plan.json").read_text(encoding="utf-8"))
    q = open_dsm_world_query(a.world)
    prof = profile("realistic")
    p = CAMERA_PROFILES["rgb1080-rs"]
    cm = with_profile(SvcCamera.from_hfov(*p["size"], a.hfov_deg, fps="10", exposure_s=p["exposure_s"], readout_s=p["readout_s"],
                                          isp=IspConfig(jpeg_quality=90)), prof)
    cam = RasterCamera(load_appearance(a.world))
    cam.set_actors(tape.actors)
    cam.set_sun(tuple((world.get("sensorHints") or {}).get("bakedSunAzElDeg") or ()) or None)
    cls_of = {i + 1: CLASSES.get(SEM_CODE.get(x["class"], 0)) for i, x in enumerate(tape.actors)}
    poly = task_from_event(a.event)["brief"]["region_polygon_enu_m"]
    xs, ys = [float(v[0]) for v in poly], [float(v[1]) for v in poly]
    dur_ns = int(plan["duration_ms"]) * 1_000_000
    lo_alt, hi_alt = ALT_ENVELOPE_AGL_M[0] - 5.0, ALT_ENVELOPE_AGL_M[1] + 5.0
    meta = {"schema": "drone4d.refdet.data.v1", "camera": {"width": cm.width, "height": cm.height, "fx": cm.fx, "fy": cm.fy, "cx": cm.cx,
                                                            "cy": cm.cy, "profile": "rgb1080-rs", "noise": prof.name, "jpeg_quality": 90},
            "classes": NAMES, "min_px": MIN_PX, "min_vis": MIN_VIS, "segments": a.segments, "seg_len": SEG_LEN, "seg_skip": SEG_SKIP,
            "seed": a.seed, "world_content_version": world["contentVersion"], "event": plan["event_id"], "alt_agl_m": [lo_alt, hi_alt]}
    (a.out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    shard, n_in_shard, fbin, fidx = -1, a.shard, None, None
    t0 = time.perf_counter()
    n_frames = n_pos = 0
    for si in range(a.segments):
        rng = np.random.default_rng([a.seed, si])
        t = int(rng.integers(5_000_000_000, dur_ns - SEG_LEN * TICK_NS - 5_000_000_000))
        st = tape.actor_states(t)
        live = np.flatnonzero(np.isin(st.state, (1, 2)))
        if len(live) and rng.random() < 0.8:
            j = int(live[rng.integers(0, len(live))])
            tgt = np.asarray(st.pos_enu_m[j], np.float64)
        else:
            tgt = np.array([rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys)), 0.0])
        gz = float(q.ground_dtm(np.array([tgt[:2]]))[0])
        tgt[2] = gz + 0.9
        h = rng.uniform(lo_alt, hi_alt)
        r = rng.uniform(0.0, 1.6 * h)
        az = rng.uniform(-math.pi, math.pi)
        heading = rng.uniform(-math.pi, math.pi)
        v = 6.0 * np.array([math.cos(heading), math.sin(heading), 0.0])
        c0 = np.array([tgt[0] + r * math.cos(az), tgt[1] + r * math.sin(az), 0.0])
        c0[2] = max(gz + h, float(q.height_dsm(np.array([c0[:2]]))[0]) + 10.0)       # 不进入建筑体
        dyaw, dpit = rng.normal(0.0, math.radians(8.0)), rng.normal(0.0, math.radians(4.0))
        ae = AeState()
        noise_seed = int(rng.integers(0, 2**31 - 1))
        for f in range(SEG_LEN):
            tf = t + f * TICK_NS
            pos = c0 + v * (f * 0.1)
            yaw, pit = _look(pos, tgt)
            pit = min(max(pit + dpit, math.radians(-90.0)), math.radians(-5.0))
            R = np.array([[math.cos(yaw + dyaw), -math.sin(yaw + dyaw), 0.0], [math.sin(yaw + dyaw), math.cos(yaw + dyaw), 0.0], [0.0, 0.0, 1.0]]) \
                @ gimbal_R(0.0, pit)
            fxp = frame_params(cm, ae, t_mid_s=tf / 1e9, seed=noise_seed, uav_no=0, sensor_no=0, frame_index=f)
            stf = tape.actor_states(tf)
            m = np.isin(stf.state, (1, 2))
            out = cam.render(cm, R, pos, fx=fxp, actors=ActorFrame(stf.ids[m], stf.pos_enu_m[m], stf.yaw_rad[m]))
            finish_frame(ae, fxp, out.meter_sub)
            if f < SEG_SKIP:
                continue
            jpg = encode_jpeg(out.rgb8, 90)
            boxes = []
            for o in visibility_objects(out.instance, out.instance_unocc, out.semantic, None, tape.actors):
                c = cls_of.get(o["inst"])
                if c is None or o["px_visible"] == 0:
                    continue
                ign = int(o["px_visible"] < MIN_PX or o["visible_ratio"] < MIN_VIS)
                boxes.append([c, *o["bbox_xywh"], ign])
                n_pos += 1 - ign
            if n_in_shard >= a.shard:
                if fbin is not None:
                    fbin.close()
                    fidx.close()
                shard += 1
                n_in_shard = 0
                fbin = (a.out / f"shard-{shard:03d}.bin").open("wb")
                fidx = (a.out / f"shard-{shard:03d}.jsonl").open("w", encoding="utf-8")
            off = fbin.tell()
            fbin.write(jpg)
            fidx.write(json.dumps({"off": off, "len": len(jpg), "seg": si, "t_ns": tf, "alt_agl_m": round(float(pos[2] - gz), 2),
                                   "boxes": boxes}) + "\n")
            n_in_shard += 1
            n_frames += 1
        if (si + 1) % 100 == 0:
            el = time.perf_counter() - t0
            print(f"[gen] {si + 1}/{a.segments} segments, {n_frames} frames, {n_pos} positive boxes, {n_frames / el:.1f} fps", flush=True)
    if fbin is not None:
        fbin.close()
        fidx.close()
    cam.release()
    print(f"[gen] done: {n_frames} frames, {n_pos} positive boxes in {time.perf_counter() - t0:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
