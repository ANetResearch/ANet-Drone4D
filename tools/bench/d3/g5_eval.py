#!/usr/bin/env python3
"""tools/bench/d3/g5_eval.py：G-3DGS 决策门 G5（下游影响）补测与幻觉目标审计（D3-AC-64；docs/05 §6.4、§6.4.2、§6.4.4；r11 §4.5）。

协议（C3 合成，3DGS 只替换静态像素颜色）：在 r11 的 150 m 实验街区（世界 ENU 中心 (−150, 140) m）内按 30、60、120 m 三个 AGL 档取视角，
每个视角在可见的开阔地面上放置演员（事件磁带的人员代理与车辆代理），以 M25 光栅相机（与 `ref_det_v1` 训练数据同一渲染器）渲染
"网格 + 演员"与"只网格"两遍；几何、深度、遮挡、实例与标签恒取网格。背景外观按变体替换：

- `mesh`：网格渲染本身（对照）；
- `base`：r11 产物 A（网格多高度渲染直接训练的 3DGS，`mesh_distilled`，`gs_base.pt`）；
- `lock`：r11 b4（频带分离回灌加几何锁定，生成式外观的候选，`gs_idu_lock.pt`）；
- `cn`：r11 b1（不加约束的课程式细化，`gs_idu_cn.pt`），只作灵敏度对照（已知不可用），不参与判定。

合成：街区掩膜（网格深度反投影的世界 xy 落在街区内）内的静态像素取 3DGS 颜色（gsplat 以黑色背景渲染，按 `c_gs + (1 − alpha)·c_mesh`
以网格颜色回退空洞），演员像素（可见实例 > 0）恒取网格遍；随后在线性域施加 realistic 档效果链（自动曝光收敛、渐晕、Poisson–Gaussian
噪声、sRGB 量化，`camera.effects` 参考实现）与 JPEG q90，即 Env 交付给 Agent 的同一字节路径。同一视角的噪声键与曝光误差相位在各变体
之间相同，变体之间只有背景外观不同。检测器为随包定稿权重 `ref_det_v1`（`RefDet()` 缺省路径载入，校验 `WEIGHTS_SHA256`）。

指标：
- AP：每类 COCO 101 点插值 AP，IoU 0.5 与 0.3（检测器训练评估口径）两档；mAP 为人员、车辆两类均值；标注取可见度归约的可见框，可见像素
  < 12 或可见比例 < 0.2 的演员为忽略区域（与训练数据 gen_data.py 相同）；ΔAP = AP(mesh) − AP(变体)（正值为下降），以视角为单位的
  成对自助法（1000 次）给出 95% 区间。判定：`base` 与 `lock` 的 mAP 下降 ≤ 2 个百分点（两档 IoU 均须满足）。
- 幻觉目标审计（G-3DGS v2 增补，§6.4.4）：只网格背景（无演员）上 ref_det_v1 在工作阈值 0.3 的人员与车辆检测；"每帧新增检测数"
  = 变体帧中与同一视角网格帧任一检测（同类、IoU ≥ 0.3）都不匹配的检测数的每帧均值；另给出每帧检测数之差、有演员帧中的误检之差、
  生成式变体相对产物 A 的新增数，以及 `--reseed`（另一效果链种子下的网格对照）给出的传感器噪声本底。门限 ≤ 0.1（暂定，首次复测后
  冻结），只对生成式候选（`lock`）判定；产物 A 只由网格渲染训练、不能产生新目标，其审计值作为重渲染抖动的参照。ref_det_v1 在 crop
  航拍渲染上训练，作为航拍域检测器。

阶段（dmax；工作目录 W 下 python/、contracts/、vehicles/ 取 git HEAD，tools/ 取工作树）：
  views   d3-sensorsvc 镜像，EGL（GPU）：取视角、放置演员、渲染网格两遍与标注 → <out>/views/
  gs      d3-sky:v1 镜像（torch、gsplat 1.5.3），GPU：按视角渲染各 checkpoint → <out>/gs/<label>/
  eval    CPU（d3-sensorsvc 镜像）：合成、效果链、JPEG、ref_det_v1 推理 → <out>/dets.jsonl
  summarize  纯计算：AP、自助法区间、审计与判定 → <out>/g5.json（`--reseed <dir>`：同一 views 与 gs 下以
          `eval --variants mesh --seed 65` 另跑一次的输出目录）

用法（dmax，D=/data/anet-drone4d-d3/d3dev，W=$D/g64，S=/data/anet-drone4d-d3/sky/work/cache）：
  docker run --rm --name d3-G64-views --gpus device=2 -e NVIDIA_DRIVER_CAPABILITIES=all --cpus 8 --memory 32g --network none \\
    -e PYTHONPATH=/repo/python -e AWR_CONTRACTS_DIR=/repo/contracts -v $W:/repo -v $D/worlds:/repo/worlds:ro \\
    -v $D/d3data:/repo/d3data:ro -w /repo d3-sensorsvc:dev python3 tools/bench/d3/g5_eval.py views \\
    --world worlds/oryahovo-crop1 --event d3data/events/crop-handoff-001/v1 --out out --per-band 120
  docker run --rm --name d3-G64-gs --gpus device=2 -e NVIDIA_DRIVER_CAPABILITIES=all --cpus 8 --memory 32g --network none \\
    -v $W:/repo -v $S:/ckpt:ro -v $W/cache_root:/root/.cache -w /repo d3-sky:v1 python3 tools/bench/d3/g5_eval.py gs --out out \\
    --ckpt base=/ckpt/gs_base.pt --ckpt lock=/ckpt/gs_idu_lock.pt --ckpt cn=/ckpt/gs_idu_cn.pt
  docker run --rm --name d3-G64-eval --cpus 16 --memory 32g --network none -e PYTHONPATH=/repo/python -v $W:/repo -w /repo \\
    d3-sensorsvc:dev python3 tools/bench/d3/g5_eval.py eval --out out --variants mesh,base,lock,cn --workers 16
  python3 tools/bench/d3/g5_eval.py summarize --out out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "python", ROOT / "tools/sdk/anet4d/src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCHEMA = "drone4d.bench.g5_eval.v1"
BLOCK_C = (-150.0, 140.0)                 # r11 实验街区中心（sky_world.BLOCK_C）
BLOCK_HALF = 75.0
# r11 的世界变换（sky_world.T_WORLD_SOURCE，r2 coordinate_v2_example_crop.json）；产品世界 coordinate.json 的 T_world_source 旋转相同、
# 平移 z 不同（r2 之后的高程定稿），3DGS 均值按两者平移差移入产品世界
R11_T_WORLD_SOURCE = (
    (-20.497999, 0.014031, 4.254961, -397.861914),
    (4.254251, 0.456221, 20.493074, -1457.121676),
    (-0.07899, 20.929991, -0.44955, 178.733353),
    (0.0, 0.0, 0.0, 1.0))
ACTOR_HALF = 70.0                         # 演员只放在街区内缩 5 m 的范围（3DGS 训练掩膜外扩 15 m，见 r11 §2.4）
W_PX, H_PX, HFOV_DEG = 1920, 1080, 60.0   # rgb1080-rs，与 gen_data.py 相同
JPEG_Q = 90
BANDS = (                                 # (名称, AGL 范围 m, 俯角范围 °)；§6.4 协议的 30、60、120 m 三档，落在检测器训练包络 25–125 m 内
    ("agl30", (25.0, 40.0), (25.0, 60.0)),
    ("agl60", (50.0, 75.0), (30.0, 89.0)),
    ("agl120", (100.0, 125.0), (45.0, 89.0)),
)
N_PERSON, N_VEHICLE, VEHICLE_COPIES = 10, 2, 5
MIN_PX, MIN_VIS = 12, 0.2                 # 忽略区域规则（gen_data.MIN_PX、MIN_VIS）
MIN_BLOCK_FRAC, MAX_SKY_FRAC = 0.5, 0.05
DET_THR_AP, DET_K_AP = 0.05, 100          # AP 用低阈值取完整 PR 曲线
DET_THR_OP = 0.3                          # 工作阈值（RefDet.detect 缺省、L2Perception）
IOU_AP = (0.5, 0.3)
IOU_AUDIT = 0.3
AE_ITERS = 8
GATE_AP_PP = 2.0                          # G5：AP 下降 ≤ 2 个百分点
GATE_HALLU_PER_FRAME = 0.1                # G-3DGS v2：每帧新增人与车辆检测数 ≤ 0.1（暂定）
CANDIDATES = ("base", "lock")             # 参与 AP 判定的变体；cn 为灵敏度对照
GENERATIVE = ("lock",)                    # 参与幻觉审计判定的生成式候选；产物 A（base）只由网格渲染训练，其审计值作为重渲染抖动的参照
CLASS_NAMES = ("person", "vehicle")
SEM_CLS = {32: 0, 33: 1}


# ------------------------------------------------------------------------------------------------------------- 几何与取景
def unproject(depth: np.ndarray, K: tuple[float, float, float, float], R_ws: np.ndarray, p_w: np.ndarray,
              rows: np.ndarray | None = None, cols: np.ndarray | None = None) -> np.ndarray:
    """平面深度 → 世界点 (h, w, 3)；像素中心 u = i + 0.5（M25 §6.2.1）。rows、cols 给出时只取这些行列。"""
    from awr.sensorsvc.camera.proj import R_FLU_OPT

    fx, fy, cx, cy = K
    rr = np.arange(depth.shape[0]) if rows is None else np.asarray(rows)
    cc = np.arange(depth.shape[1]) if cols is None else np.asarray(cols)
    z = depth[np.ix_(rr, cc)].astype(np.float64)
    xo = (cc[None, :] + 0.5 - cx) / fx * z
    yo = (rr[:, None] + 0.5 - cy) / fy * z
    Xo = np.stack([xo, yo, z], -1)
    Rwo = np.asarray(R_ws, np.float64) @ R_FLU_OPT
    return Xo @ Rwo.T + np.asarray(p_w, np.float64)


def block_mask(depth: np.ndarray, K, R_ws, p_w, center=BLOCK_C, half: float = BLOCK_HALF) -> np.ndarray:
    """静态表面落在街区内的像素（天空与几何外为 False）：3DGS 外观只在此范围内替换。"""
    X = unproject(depth, K, R_ws, p_w)
    return (depth > 0) & (np.abs(X[..., 0] - center[0]) <= half) & (np.abs(X[..., 1] - center[1]) <= half)


def sample_pose(rng: np.random.Generator, q, band: tuple, center=BLOCK_C) -> tuple[np.ndarray, np.ndarray, dict] | None:
    """注视点取街区内缩区域的地面点，AGL 以相机正下方 DTM 计；机体 3 m 半径内净空不足 3 m 时返回 None。→ (R_ws, p_w, meta)。"""
    from awr.sensorsvc.camera.proj import look_at_R

    _name, (a0, a1), (e0, e1) = band
    tx, ty = center[0] + rng.uniform(-50.0, 50.0), center[1] + rng.uniform(-50.0, 50.0)
    tz = float(q.ground_dtm(np.array([[tx, ty]]))[0])
    agl, pitch, az = rng.uniform(a0, a1), rng.uniform(e0, e1), rng.uniform(-math.pi, math.pi)
    px, py, z = tx, ty, tz + agl
    for _ in range(4):                                       # 机下地面高度随水平距离变化，迭代 4 次（r11 sky_world.view_cam）
        d = (z - tz) / math.tan(math.radians(pitch))
        px, py = tx - d * math.cos(az), ty - d * math.sin(az)
        z = float(q.ground_dtm(np.array([[px, py]]))[0]) + agl
    if z < float(q.column_max_within(np.array([[px, py]]), 3.0)[0]) + 3.0:
        return None
    p = np.array([px, py, z])
    R = look_at_R(p, np.array([tx, ty, tz]))
    meta = {"target": [round(tx, 3), round(ty, 3), round(tz, 3)], "agl_m": round(agl, 3), "pitch_deg": round(pitch, 3),
            "az_deg": round(math.degrees(az), 3)}
    return R, p, meta


def actor_spots(depth: np.ndarray, K, R_ws, p_w, q, *, stride: int = 12, center=BLOCK_C, half: float = ACTOR_HALF) -> dict[str, np.ndarray]:
    """可见的开阔地面点（世界 xyz）：落在街区内、可见表面即 DSM（原始）顶面、DSM 与 DTM 差 < 0.5 m（非屋顶、非树冠），且邻域平整
    （人员 1.5 m、车辆 3 m 半径内的柱顶高出 < 0.25 m，即附近没有墙体）。像素取画面中部 90%。"""
    H, W = depth.shape
    rows = np.arange(int(0.08 * H), int(0.92 * H), stride)
    cols = np.arange(int(0.05 * W), int(0.95 * W), stride)
    X = unproject(depth, K, R_ws, p_w, rows, cols).reshape(-1, 3)
    ok = (depth[np.ix_(rows, cols)].ravel() > 0) & (np.abs(X[:, 0] - center[0]) <= half) & (np.abs(X[:, 1] - center[1]) <= half)
    X = X[ok]
    if not len(X):
        return {"person": np.zeros((0, 3)), "vehicle": np.zeros((0, 3))}
    xy = X[:, :2]
    raw = np.asarray(q.height_dsm(xy, raw=True), np.float64)
    eff = np.asarray(q.height_dsm(xy), np.float64)
    dtm = np.asarray(q.ground_dtm(xy), np.float64)
    flat = (np.abs(X[:, 2] - raw) < 0.3) & (raw - dtm < 0.5)
    p_ok = flat & (np.asarray(q.column_max_within(xy, 1.5), np.float64) - eff < 0.25)
    v_ok = flat & (np.asarray(q.column_max_within(xy, 3.0), np.float64) - eff < 0.25)
    return {"person": X[p_ok], "vehicle": X[v_ok]}             # 脚底取可见网格表面点本身


def place_actors(rng: np.random.Generator, spots: dict[str, np.ndarray], roster: list[dict], *, n_person: int = N_PERSON,
                 n_vehicle: int = N_VEHICLE, gap_person_m: float = 2.5, gap_vehicle_m: float = 6.0) -> list[tuple[int, list[float], float]]:
    """→ [(演员序号, 脚底世界坐标, 偏航 rad)]；先放车辆（彼此与人员间距 ≥ 6 m），再放人员（彼此 ≥ 2.5 m）。演员序号互不相同。"""
    persons = [i for i, a in enumerate(roster) if a["class"] == "person"]
    vehicles = [i for i, a in enumerate(roster) if a["class"] == "vehicle"]
    placed: list[tuple[int, np.ndarray, float, str]] = []

    def pick(cands: np.ndarray, n: int, pool: list[int], cls: str, gap: float) -> None:
        if not len(cands) or not pool:
            return
        ids = rng.choice(pool, size=min(n, len(pool)), replace=False)
        order = rng.permutation(len(cands))
        k = 0
        for j in order:
            if k >= len(ids):
                break
            c = cands[j]
            if all(np.hypot(*(c[:2] - p[:2])) >= (gap_vehicle_m if "vehicle" in (cls, pc) else gap) for _i, p, _y, pc in placed):
                placed.append((int(ids[k]), c, float(rng.uniform(-math.pi, math.pi)), cls))
                k += 1

    pick(spots["vehicle"], n_vehicle, vehicles, "vehicle", gap_vehicle_m)
    pick(spots["person"], n_person, persons, "person", gap_person_m)
    return [(i, [round(float(v), 4) for v in p], round(y, 6)) for i, p, y, _c in placed]


def gs_world_offset(coordinate: dict) -> list[float]:
    """r11 世界 → 产品世界的平移（m）。两者旋转与尺度部分须逐元素一致（≤ 1e-6），否则拒绝（3DGS 不能只靠平移对齐）。"""
    T = np.asarray(coordinate["source"]["T_world_source"], np.float64)
    T0 = np.asarray(R11_T_WORLD_SOURCE, np.float64)
    if np.abs(T[:3, :3] - T0[:3, :3]).max() > 1e-6:
        raise ValueError("world T_world_source rotation/scale differs from the r11 frame; a translation cannot align the 3DGS")
    return [round(float(x), 6) for x in T[:3, 3] - T0[:3, 3]]


def actor_roster(tape_actors: list[dict], copies: int = VEHICLE_COPIES) -> list[dict]:
    """事件磁带演员表 + 车辆副本（同一外观，便于每个视角放置多辆车，外观仍在检测器训练分布内）。"""
    out = [dict(a) for a in tape_actors]
    for a in tape_actors:
        if a["class"] == "vehicle":
            out.extend({**a, "id": f"{a['id']}#{k + 1}"} for k in range(copies))
    for i, a in enumerate(out):
        a["idx"] = i
    return out


def gt_boxes(objs: list[dict]) -> list[list]:
    """可见度归约 → [[类别号, x, y, w, h, ignore]]（交付分辨率像素）；规则同 gen_data.py。"""
    boxes = []
    for o in objs:
        c = SEM_CLS.get(o["class"])
        if c is None or o["px_visible"] == 0:
            continue
        boxes.append([c, *o["bbox_xywh"], int(o["px_visible"] < MIN_PX or o["visible_ratio"] < MIN_VIS)])
    return boxes


def camera_model():
    from awr.sensorsvc.camera.effects import profile, with_profile
    from awr.sensorsvc.camera.model import CAMERA_PROFILES, IspConfig
    from awr.sensorsvc.camera.model import CameraModel as SvcCamera

    p = CAMERA_PROFILES["rgb1080-rs"]
    return with_profile(SvcCamera.from_hfov(W_PX, H_PX, HFOV_DEG, fps="10", exposure_s=p["exposure_s"], readout_s=p["readout_s"],
                                            isp=IspConfig(jpeg_quality=JPEG_Q)), profile("realistic"))


def _png(path: Path, a: np.ndarray) -> None:
    from PIL import Image

    im = Image.fromarray(a.astype(np.uint8) * 255 if a.dtype == bool else np.ascontiguousarray(a))   # uint16 → I;16，uint8 → L 或 RGB
    im.save(path, compress_level=1)


def _read_png(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as im:
        if im.mode in ("I;16", "I;16B", "I"):
            return np.asarray(im, np.uint16) if im.mode != "I" else np.asarray(im).astype(np.uint16)
        return np.asarray(im)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main_views(a: argparse.Namespace) -> int:
    from awr.sensorsvc.camera.actors import visibility_objects
    from awr.sensorsvc.camera.proj import world_to_opt
    from awr.sensorsvc.camera.raster import RENDERER_VERSION, ActorFrame, RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance
    from awr.sim.events.tape import open_tape
    from awr.world.geometry.query import open_dsm_world_query

    out = a.out / "views"
    out.mkdir(parents=True, exist_ok=True)
    tape = open_tape(a.event)
    world = json.loads((a.world / "world.json").read_text(encoding="utf-8"))
    roster = actor_roster(list(tape.actors))
    offset = gs_world_offset(json.loads((a.world / "coordinate.json").read_text(encoding="utf-8")))
    q = open_dsm_world_query(a.world)
    cm = camera_model()
    K = (cm.fx, cm.fy, cm.cx, cm.cy)
    t0 = time.perf_counter()
    cam = RasterCamera(load_appearance(a.world))
    cam.set_actors(roster)
    sun = tuple((world.get("sensorHints") or {}).get("bakedSunAzElDeg") or ()) or None
    cam.set_sun(sun)
    load_s = time.perf_counter() - t0
    views, rejected = [], {"pose": 0, "coverage": 0, "spots": 0}
    for bi, band in enumerate(BANDS):
        for k in range(a.per_band):
            rng = np.random.default_rng([a.seed, bi, k])
            for _attempt in range(60):
                pose = sample_pose(rng, q, band)
                if pose is None:
                    rejected["pose"] += 1
                    continue
                R, p, meta = pose
                bg = cam.render(cm, R, p)
                blk = block_mask(bg.depth_m, K, R, p)
                frac, sky = float(blk.mean()), float((bg.depth_m <= 0).mean())
                if frac < MIN_BLOCK_FRAC or sky > MAX_SKY_FRAC:
                    rejected["coverage"] += 1
                    continue
                placed = place_actors(rng, actor_spots(bg.depth_m, K, R, p, q), roster)
                if sum(roster[i]["class"] == "person" for i, _p, _y in placed) < 4:
                    rejected["spots"] += 1
                    continue
                af = ActorFrame(np.array([i for i, _p, _y in placed], np.int64), np.array([p_ for _i, p_, _y in placed], np.float64),
                                np.array([y for _i, _p, y in placed], np.float64))
                act = cam.render(cm, R, p, actors=af)
                boxes = gt_boxes(visibility_objects(act.instance, act.instance_unocc, act.semantic, None, roster))
                vid = f"v{len(views):04d}"
                _png(out / f"{vid}_bg.png", bg.rgb8)
                _png(out / f"{vid}_act.png", act.rgb8)
                _png(out / f"{vid}_inst.png", act.instance)
                _png(out / f"{vid}_block.png", blk)
                v = {"id": vid, "band": band[0], **meta, "R_ws": np.round(R, 12).tolist(), "p_w": [round(float(x), 6) for x in p],
                     "viewmat": world_to_opt(R, p).tolist(), "block_frac": round(frac, 4), "sky_frac": round(sky, 5),
                     "actors": [{"idx": i, "pos": p_, "yaw": y} for i, p_, y in placed], "boxes": boxes,
                     "seed": [a.seed, bi, k]}
                views.append(v)
                break
            else:
                raise RuntimeError(f"band {band[0]} view {k}: no acceptable pose after 60 attempts")
            if len(views) % 30 == 0:
                print(f"[g5] views {len(views)} ({time.perf_counter() - t0:.0f} s)", flush=True)
    cam.release()
    doc = {"schema": SCHEMA, "stage": "views", "renderer": RENDERER_VERSION, "world": str(a.world), "world_content_version": world.get("contentVersion"),
           "event": str(a.event), "seed": a.seed, "per_band": a.per_band, "bands": [list(b) for b in BANDS], "block_center": list(BLOCK_C),
           "block_half_m": BLOCK_HALF, "camera": {"width": cm.width, "height": cm.height, "fx": cm.fx, "fy": cm.fy, "cx": cm.cx, "cy": cm.cy,
                                                  "profile": "rgb1080-rs", "noise": "realistic", "jpeg_quality": JPEG_Q},
           "gs_world_offset_m": offset, "sun_az_el_deg": list(sun) if sun else None, "roster": roster, "rejected": rejected, "load_s": round(load_s, 1),
           "render_s": round(time.perf_counter() - t0 - load_s, 1), "gl": cam.info, "views": views}
    (out / "views.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[g5] views done: {len(views)} views, {sum(len(v['boxes']) for v in views)} boxes, rejected {rejected}", flush=True)
    return 0


# ------------------------------------------------------------------------------------------------------------- 3DGS 渲染
def main_gs(a: argparse.Namespace) -> int:
    import torch
    import torch.nn.functional as F
    from gsplat import __version__ as gsplat_version
    from gsplat import rasterization

    doc = json.loads((a.out / "views" / "views.json").read_text(encoding="utf-8"))
    c = doc["camera"]
    K = torch.tensor([[c["fx"], 0.0, c["cx"]], [0.0, c["fy"], c["cy"]], [0.0, 0.0, 1.0]], dtype=torch.float32, device="cuda")[None]
    res = {"schema": SCHEMA, "stage": "gs", "gs_world_offset_m": doc["gs_world_offset_m"], "gsplat": gsplat_version, "torch": torch.__version__, "gpu": torch.cuda.get_device_name(0),
           "ckpts": {}}
    for spec in a.ckpt:
        label, path = spec.split("=", 1)
        out = a.out / "gs" / label
        out.mkdir(parents=True, exist_ok=True)
        d = torch.load(path, map_location="cuda")
        means = d["means"].float() + torch.tensor(doc["gs_world_offset_m"], dtype=torch.float32, device="cuda")
        quats = F.normalize(d["quats"].float(), dim=-1)
        scales = torch.exp(d["scales"].float())
        opac = torch.sigmoid(d["opacities"].float())
        cols = torch.sigmoid(d["colors"].float())
        ms = []
        for v in doc["views"]:
            vm = torch.tensor(v["viewmat"], dtype=torch.float32, device="cuda")[None]
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            with torch.no_grad():                                 # 与 r11 sky_gs.gs_render 同参数（黑色背景、near 0.3 m）
                img, alpha, _ = rasterization(means, quats, scales, opac, cols, vm, K, c["width"], c["height"], near_plane=0.3,
                                              far_plane=3000.0, render_mode="RGB", packed=False,
                                              backgrounds=torch.zeros((1, 3), device="cuda"))
            torch.cuda.synchronize()
            ms.append((time.perf_counter() - t0) * 1e3)
            rgb8 = (img[0].clamp(0, 1) * 255.0 + 0.5).to(torch.uint8).cpu().numpy()
            a8 = (alpha[0, ..., 0].clamp(0, 1) * 255.0 + 0.5).to(torch.uint8).cpu().numpy()
            _png(out / f"{v['id']}_rgb.png", rgb8)
            _png(out / f"{v['id']}_a.png", a8)
        res["ckpts"][label] = {"path": path, "sha256": _sha256(Path(path)), "n_gauss": int(means.shape[0]),
                               "render_ms_median": round(float(np.median(ms)), 2), "views": len(ms)}
        print(f"[g5] gs {label}: {len(ms)} views, {means.shape[0]} gaussians, median {np.median(ms):.1f} ms", flush=True)
        del d, means, quats, scales, opac, cols
        torch.cuda.empty_cache()
    (a.out / "gs" / "gs.json").write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    return 0


# ------------------------------------------------------------------------------------------------------------- 合成与推理
def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, np.float32)
    return np.where(x <= np.float32(0.04045), x / np.float32(12.92), ((x + np.float32(0.055)) / np.float32(1.055)) ** np.float32(2.4))


def composite(mesh_bg: np.ndarray, block: np.ndarray, *, mesh_act: np.ndarray | None = None, inst: np.ndarray | None = None,
              gs_rgb: np.ndarray | None = None, gs_a: np.ndarray | None = None) -> np.ndarray:
    """C3 合成 → sRGB float32 [0, 1] (H, W, 3)。

    静态像素：街区掩膜内取 3DGS（黑色背景渲染，即预乘颜色）并以网格颜色回退 (1 − alpha)，掩膜外取网格；`mesh_act` 给出时演员像素
    （`inst` > 0）取网格演员遍。`gs_rgb` 为 None 即网格对照（与有演员遍逐像素相同）。"""
    m = np.asarray(mesh_bg, np.float32) / np.float32(255.0)
    if gs_rgb is not None:
        al = np.asarray(gs_a, np.float32)[..., None] / np.float32(255.0)
        g = np.asarray(gs_rgb, np.float32) / np.float32(255.0) + (np.float32(1.0) - al) * m
        m = np.where(np.asarray(block, bool)[..., None], np.clip(g, 0.0, 1.0), m)
    if mesh_act is not None:
        fg = np.asarray(inst)[..., None] > 0
        m = np.where(fg, np.asarray(mesh_act, np.float32) / np.float32(255.0), m)
    return m.astype(np.float32)


def deliver_jpeg(srgb: np.ndarray, cm, *, seed: int, frame_index: int, t_mid_s: float) -> tuple[bytes, float]:
    """线性化 → realistic 效果链（自动曝光在本帧上迭代 AE_ITERS 次收敛）→ JPEG q90。→ (JPEG 字节, 含误差增益)。"""
    from awr.sensorsvc.camera.effects import AeState, apply_fx_reference, finish_frame, frame_params
    from awr.sensorsvc.frame import encode_jpeg

    lin = srgb_to_linear(srgb)
    st = AeState()
    sub = lin[::8, ::8]
    for _ in range(AE_ITERS):
        p = frame_params(cm, st, seed=seed, uav_no=0, sensor_no=0, frame_index=frame_index, t_mid_s=t_mid_s)
        finish_frame(st, p, sub)
    rgb8, _ = apply_fx_reference(lin, lin[..., 0], cm, p)
    return encode_jpeg(rgb8, JPEG_Q), float(p.gain)


def detect_jpeg(det, jpg: bytes) -> list[list[float]]:
    """ref_det_v1：→ [[类别号, 分数, x, y, w, h]]（交付分辨率像素，阈值 DET_THR_AP）。"""
    from anet4d.perception.ref_det import W_IN, decode_small

    small, (fw, fh) = decode_small(jpg)
    sx, sy = fw / W_IN, fh / small.shape[0]
    return [[c, round(s, 5), round(x * sx, 2), round(y * sy, 2), round(w * sx, 2), round(h * sy, 2)]
            for c, s, x, y, w, h in det.detect(small, thr=DET_THR_AP, k=DET_K_AP)]


def psnr_masked(a8: np.ndarray, b8: np.ndarray, mask: np.ndarray) -> float | None:
    m = np.asarray(mask, bool)
    if not m.any():
        return None
    d = (np.asarray(a8, np.float64)[m] - np.asarray(b8, np.float64)[m]) / 255.0
    mse = float((d * d).mean())
    return round(-10.0 * math.log10(max(mse, 1e-10)), 3)


_W: dict = {}


def _worker_init(views_dir: str, gs_dir: str, cam: dict) -> None:
    import os

    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[k] = "1"
    from anet4d.perception.ref_det import RefDet

    _W.update(det=RefDet(), views=Path(views_dir), gs=Path(gs_dir), cm=camera_model(), cam=cam)


def eval_view(job: tuple[int, dict, list[str], int]) -> list[dict]:
    """一个视角、全部变体、有演员与只背景两种帧 → 检测记录。"""
    vi, v, variants, seed = job
    vd, gd, cm = _W["views"], _W["gs"], _W["cm"]
    bg = _read_png(vd / f"{v['id']}_bg.png")
    act = _read_png(vd / f"{v['id']}_act.png")
    inst = _read_png(vd / f"{v['id']}_inst.png")
    blk = _read_png(vd / f"{v['id']}_block.png") > 0
    recs = []
    for var in variants:
        g = a8 = None
        psnr = None
        if var != "mesh":
            g = _read_png(gd / var / f"{v['id']}_rgb.png")
            a8 = _read_png(gd / var / f"{v['id']}_a.png")
            psnr = psnr_masked(np.clip(np.asarray(g, np.float64) + (1.0 - a8[..., None] / 255.0) * bg, 0, 255), bg, blk)
        for mode in ("act", "bg"):
            srgb = composite(bg, blk, mesh_act=act if mode == "act" else None, inst=inst, gs_rgb=g, gs_a=a8)
            jpg, gain = deliver_jpeg(srgb, cm, seed=seed, frame_index=vi, t_mid_s=0.1 * vi)
            recs.append({"view": v["id"], "variant": var, "mode": mode, "gain": round(gain, 5), "jpeg_bytes": len(jpg),
                         "psnr_vs_mesh_block": psnr, "dets": detect_jpeg(_W["det"], jpg)})
    return recs


def main_eval(a: argparse.Namespace) -> int:
    from multiprocessing import get_context

    from anet4d.perception.ref_det import VERSION, WEIGHTS, WEIGHTS_SHA256, RefDet

    RefDet()                                                          # 缺省路径载入即校验定稿 sha256
    doc = json.loads((a.out / "views" / "views.json").read_text(encoding="utf-8"))
    variants = a.variants.split(",")
    views = doc["views"][: a.limit] if a.limit else doc["views"]
    jobs = [(i, v, variants, a.seed) for i, v in enumerate(views)]
    t0 = time.perf_counter()
    n = 0
    with (a.out / "dets.jsonl").open("w", encoding="utf-8") as f, \
            get_context("spawn").Pool(a.workers, _worker_init, (str(a.out / "views"), str(a.out / "gs"), doc["camera"])) as pool:
        for recs in pool.imap(eval_view, jobs):
            for r in recs:
                f.write(json.dumps(r) + "\n")
            n += 1
            if n % 40 == 0:
                print(f"[g5] eval {n}/{len(jobs)} views ({time.perf_counter() - t0:.0f} s)", flush=True)
    meta = {"schema": SCHEMA, "stage": "eval", "variants": variants, "views": len(jobs), "seed": a.seed, "workers": a.workers,
            "detector": {"model": "ref_det_v1", "version": VERSION, "weights_sha256": WEIGHTS_SHA256, "weights_file": WEIGHTS.name,
                         "verified": _sha256(WEIGHTS) == WEIGHTS_SHA256, "thr_ap": DET_THR_AP, "k": DET_K_AP, "thr_operating": DET_THR_OP},
            "effects": {"profile": "realistic", "ae_iters": AE_ITERS, "jpeg_quality": JPEG_Q}, "eval_s": round(time.perf_counter() - t0, 1)}
    (a.out / "eval.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    print(f"[g5] eval done: {len(jobs)} views x {len(variants)} variants in {meta['eval_s']} s", flush=True)
    return 0


# ------------------------------------------------------------------------------------------------------------- AP 与审计
def iou_xywh(a, b) -> float:
    iw = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    u = a[2] * a[3] + b[2] * b[3] - iw * ih
    return iw * ih / u if u > 0 else 0.0


def match_frame(dets: list, boxes: list, cls: int, iou_thr: float) -> tuple[list[tuple[float, int]], int]:
    """单帧单类贪心匹配（分数降序，同分按原序）：→ ([(分数, 1 = TP / 0 = FP)], 正样本数)。与未匹配正样本 IoU ≥ 阈值者为 TP；
    否则与任一忽略区域 IoU > 阈值者不计（与 train.evaluate 同规则）；其余（含重复检测）为 FP。"""
    gts = [b[1:5] for b in boxes if b[0] == cls and not b[5]]
    igs = [b[1:5] for b in boxes if b[5]]
    ds = sorted((d for d in dets if d[0] == cls), key=lambda d: -d[1])
    used: set[int] = set()
    out = []
    for d in ds:
        best, bi = iou_thr, None
        for g, gt in enumerate(gts):
            if g in used:
                continue
            v = iou_xywh(d[2:6], gt)
            if v >= best:
                best, bi = v, g
        if bi is not None:
            used.add(bi)
            out.append((d[1], 1))
        elif not any(iou_xywh(d[2:6], ig) > iou_thr for ig in igs):
            out.append((d[1], 0))
    return out, len(gts)


def average_precision(scored: list[tuple[float, int]], npos: int) -> float:
    """COCO 101 点插值 AP（精度取右侧包络）；无正样本时为 NaN。"""
    if npos == 0:
        return float("nan")
    if not scored:
        return 0.0
    s = np.array([x[0] for x in scored], np.float64)
    t = np.array([x[1] for x in scored], np.float64)
    o = np.argsort(-s, kind="stable")
    tp = np.cumsum(t[o])
    fp = np.cumsum(1.0 - t[o])
    rec = tp / npos
    prec = tp / np.maximum(tp + fp, 1e-12)
    prec = np.maximum.accumulate(prec[::-1])[::-1]
    rs = np.linspace(0.0, 1.0, 101)
    idx = np.searchsorted(rec, rs, side="left")
    return float(np.where(idx < len(prec), prec[np.minimum(idx, len(prec) - 1)], 0.0).mean())


def _per_view(recs: dict, views: list[dict], var: str, iou: float) -> list[list[tuple[list, int]]]:
    """→ per_view[view][cls] = (scored, npos)。"""
    out = []
    for v in views:
        dets = recs[(v["id"], var, "act")]["dets"]
        out.append([match_frame(dets, v["boxes"], c, iou) for c in range(len(CLASS_NAMES))])
    return out


def _ap_from(pv: list, sel) -> dict:
    res = {}
    for c, name in enumerate(CLASS_NAMES):
        scored = [x for i in sel for x in pv[i][c][0]]
        npos = sum(pv[i][c][1] for i in sel)
        res[name] = average_precision(scored, npos)
    vals = [x for x in res.values() if not math.isnan(x)]
    res["mAP"] = float(np.mean(vals)) if vals else float("nan")
    return res


def audit_frame(dets: list, ref: list, thr: float = DET_THR_OP, iou_thr: float = IOU_AUDIT) -> dict:
    """幻觉审计（单帧）：工作阈值以上的检测数，以及与参照帧（同视角网格帧）任一同类检测 IoU < 阈值的"新增"检测数。"""
    d = [x for x in dets if x[1] >= thr]
    r = [x for x in ref if x[1] >= thr]
    new = sum(1 for x in d if not any(y[0] == x[0] and iou_xywh(x[2:6], y[2:6]) >= iou_thr for y in r))
    return {"n": len(d), "new": new}


def false_positives(dets: list, boxes: list, thr: float = DET_THR_OP, iou_thr: float = IOU_AUDIT) -> int:
    """有演员帧中工作阈值以上的误检数（不与任何正样本或忽略区域 IoU ≥ 阈值）。"""
    return sum(1 for x in dets if x[1] >= thr and not any(iou_xywh(x[2:6], b[1:5]) >= iou_thr for b in boxes))


def _r(x: float, nd: int = 4):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), nd)


def summarize(views: list[dict], recs: dict, variants: list[str], *, n_boot: int = 1000, seed: int = 0,
              reseed: dict | None = None) -> dict:
    """AP（总体、分档、分类）、ΔAP 与成对自助法 95% 区间、幻觉审计、逐条判定。

    `reseed` 为另一效果链种子下网格对照的检测记录（只含 mesh），给出审计指标的传感器噪声本底（同一网格画面、只换噪声与曝光误差
    相位时的"每帧新增检测数"）。"""
    n = len(views)
    bands = [b[0] for b in BANDS]
    rng = np.random.default_rng(seed)
    boot = [rng.integers(0, n, n) for _ in range(n_boot)]
    ap: dict = {}
    delta: dict = {}
    for iou in IOU_AP:
        key = f"iou{iou:g}"
        pvs = {var: _per_view(recs, views, var, iou) for var in variants}
        ap[key] = {}
        for var in variants:
            ap[key][var] = {"all": {k: _r(v) for k, v in _ap_from(pvs[var], range(n)).items()}}
            for b in bands:
                sel = [i for i, v in enumerate(views) if v["band"] == b]
                if sel:
                    ap[key][var][b] = {k: _r(v) for k, v in _ap_from(pvs[var], sel).items()}
        if "mesh" not in variants:
            continue
        delta[key] = {}
        mesh_boot = [_ap_from(pvs["mesh"], s) for s in boot]
        for var in variants:
            if var == "mesh":
                continue
            d0 = {k: ap[key]["mesh"]["all"][k] - ap[key][var]["all"][k] for k in ("person", "vehicle", "mAP")
                  if ap[key]["mesh"]["all"][k] is not None and ap[key][var]["all"][k] is not None}
            bs = [_ap_from(pvs[var], s) for s in boot]
            ci = {}
            for k in d0:
                ds = np.array([mb[k] - vb[k] for mb, vb in zip(mesh_boot, bs, strict=True) if not (math.isnan(mb[k]) or math.isnan(vb[k]))])
                ci[k] = [_r(100 * float(np.percentile(ds, 2.5)), 2), _r(100 * float(np.percentile(ds, 97.5)), 2)] if len(ds) else None
            delta[key][var] = {"drop_pp": {k: _r(100 * v, 2) for k, v in d0.items()}, "ci95_pp": ci,
                               "by_band_mAP_drop_pp": {b: _r(100 * (ap[key]["mesh"][b]["mAP"] - ap[key][var][b]["mAP"]), 2)
                                                       for b in bands if b in ap[key][var] and ap[key][var][b]["mAP"] is not None}}
    audit: dict = {}
    for var in variants:
        rows = [audit_frame(recs[(v["id"], var, "bg")]["dets"], recs[(v["id"], "mesh", "bg")]["dets"]) for v in views] if "mesh" in variants else []
        fp = [false_positives(recs[(v["id"], var, "act")]["dets"], v["boxes"]) for v in views]
        audit[var] = {"bg_det_per_frame": _r(np.mean([r["n"] for r in rows]) if rows else float("nan")),
                      "bg_new_per_frame": _r(np.mean([r["new"] for r in rows]) if rows else float("nan")),
                      "act_fp_per_frame": _r(float(np.mean(fp)))}
        for c, name in enumerate(CLASS_NAMES):
            audit[var][f"bg_new_per_frame_{name}"] = _r(np.mean([audit_frame([d for d in recs[(v["id"], var, "bg")]["dets"] if d[0] == c],
                                                                              recs[(v["id"], "mesh", "bg")]["dets"])["new"] for v in views])
                                                        if "mesh" in variants else float("nan"))
        if var != "mesh" and "mesh" in variants:
            audit[var]["bg_det_delta_per_frame"] = _r(audit[var]["bg_det_per_frame"] - audit["mesh"]["bg_det_per_frame"])
            audit[var]["act_fp_delta_per_frame"] = _r(audit[var]["act_fp_per_frame"] - audit["mesh"]["act_fp_per_frame"])
        if var not in ("mesh", "base") and "base" in variants:          # 相对同瓦片产物 A（G-3DGS v2 以产物 A 为生成式的参照）
            audit[var]["bg_new_vs_base_per_frame"] = _r(np.mean([audit_frame(recs[(v["id"], var, "bg")]["dets"],
                                                                             recs[(v["id"], "base", "bg")]["dets"])["new"] for v in views]))
    if reseed is not None and "mesh" in variants:
        rows = [audit_frame(reseed[(v["id"], "mesh", "bg")]["dets"], recs[(v["id"], "mesh", "bg")]["dets"]) for v in views]
        audit["mesh_reseed"] = {"bg_det_per_frame": _r(float(np.mean([r["n"] for r in rows]))),
                                "bg_new_per_frame": _r(float(np.mean([r["new"] for r in rows])))}
    psnr = {var: _r(float(np.median([recs[(v["id"], var, "act")]["psnr_vs_mesh_block"] for v in views])), 2)
            for var in variants if var != "mesh"}
    gate = {}
    for var in (x for x in CANDIDATES if x in variants and "mesh" in variants):
        drops = {k: delta[k][var]["drop_pp"].get("mAP") for k in delta}
        ap_ok = all(x is not None and x <= GATE_AP_PP for x in drops.values())
        h = audit[var]["bg_new_per_frame"]
        gated = var in GENERATIVE
        h_ok = (h is not None and h <= GATE_HALLU_PER_FRAME) if gated else None
        gate[var] = {"ap_drop_pp": drops, "ap_pass": ap_ok, "hallucination_gated": gated, "hallucination_new_per_frame": h,
                     "hallucination_delta_per_frame": audit[var]["bg_det_delta_per_frame"], "hallucination_pass": h_ok,
                     "pass": ap_ok and h_ok is not False}
    n_pos = {name: sum(1 for v in views for b in v["boxes"] if b[0] == c and not b[5]) for c, name in enumerate(CLASS_NAMES)}
    n_ign = sum(1 for v in views for b in v["boxes"] if b[5])
    return {"schema": SCHEMA, "stage": "summary", "views": n, "views_by_band": {b: sum(v["band"] == b for v in views) for b in bands},
            "positives": n_pos, "ignored": n_ign, "variants": variants, "ap": ap, "ap_delta": delta, "audit": audit,
            "psnr_vs_mesh_block_median_db": psnr, "bootstrap": {"n": n_boot, "seed": seed, "unit": "view"},
            "thresholds": {"ap_drop_pp": GATE_AP_PP, "hallucination_new_per_frame": GATE_HALLU_PER_FRAME, "hallucination_variants": list(GENERATIVE),
                           "iou_ap": list(IOU_AP),
                           "iou_audit": IOU_AUDIT, "det_thr_operating": DET_THR_OP},
            "gate": gate, "g5_pass": bool(gate) and all(g["pass"] for g in gate.values())}


def load_dets(path: Path) -> tuple[dict, list[str]]:
    recs, variants = {}, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        recs[(r["view"], r["variant"], r["mode"])] = r
        if r["variant"] not in variants:
            variants.append(r["variant"])
    return recs, variants


def main_summarize(a: argparse.Namespace) -> int:
    doc = json.loads((a.out / "views" / "views.json").read_text(encoding="utf-8"))
    recs, variants = load_dets(a.out / "dets.jsonl")
    ids = {k[0] for k in recs}
    views = [v for v in doc["views"] if v["id"] in ids]
    reseed = load_dets(a.reseed / "dets.jsonl")[0] if a.reseed else None
    res = summarize(views, recs, variants, n_boot=a.boot, seed=a.seed, reseed=reseed)
    if a.reseed:
        res["reseed_eval"] = json.loads((a.reseed / "eval.json").read_text(encoding="utf-8"))
    prov = {"views": {k: doc[k] for k in ("renderer", "world", "world_content_version", "event", "seed", "per_band", "camera", "rejected",
                                          "load_s", "render_s", "gl", "gs_world_offset_m") if k in doc}}
    for name in ("gs/gs.json", "eval.json"):
        p = a.out / name
        if p.exists():
            prov[name.split("/")[0].replace(".json", "")] = json.loads(p.read_text(encoding="utf-8"))
    res["provenance"] = prov
    res["generated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (a.out / "g5.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"ap_delta": res["ap_delta"], "audit": res["audit"], "gate": res["gate"], "g5_pass": res["g5_pass"]}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="g5_eval.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("views")
    v.add_argument("--world", type=Path, required=True)
    v.add_argument("--event", type=Path, required=True)
    v.add_argument("--out", type=Path, required=True)
    v.add_argument("--per-band", type=int, default=120)
    v.add_argument("--seed", type=int, default=64)
    g = sub.add_parser("gs")
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--ckpt", action="append", required=True, help="label=path（gs_*.pt，r11 sky_gs.save_params 格式）")
    e = sub.add_parser("eval")
    e.add_argument("--out", type=Path, required=True)
    e.add_argument("--variants", default="mesh,base,lock,cn")
    e.add_argument("--workers", type=int, default=8)
    e.add_argument("--seed", type=int, default=64, help="效果链噪声与曝光误差的运行种子")
    e.add_argument("--limit", type=int, default=0)
    s = sub.add_parser("summarize")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--boot", type=int, default=1000)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--reseed", type=Path, default=None, help="另一效果链种子下只含 mesh 的 eval 输出目录（审计噪声本底）")
    a = ap.parse_args(argv)
    return {"views": main_views, "gs": main_gs, "eval": main_eval, "summarize": main_summarize}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
