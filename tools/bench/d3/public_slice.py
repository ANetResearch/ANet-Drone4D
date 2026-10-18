#!/usr/bin/env python3
"""tools/bench/d3/public_slice.py：`public` 档时间窗切片与 `basemap.rrd`（M27 §6.7.8 第 4 步、M27-FR-082；AWR-05 §6.1.5、§6.1.6；
D3-AC-20、D3-AC-21、D3-AC-25）。

对公开样例运行（`public_catalog.yaml` 选中的 dev 运行），围绕每个关键事件的中点取 10 s 窗口，以 `public` 档参数直接从 MCAP 生成
观测层切片；另由 M22 彩色点云按事件舞台裁剪、体素降采样得到 `basemap.rrd`。两者与 Run 的其他层共用 `application_id` 与
`recording_id = run_id`，可在原生 Viewer 或 Web Viewer 中合并加载回放。

切片（`<out>/<key>.rrd`，每个关键事件一个文件）：

- 只含观测层（`obs.*`）：全部机（缺省；`--uav` 可限定）的 `/fleet/<uav>/**`（带噪导航解、遥测、相机 RGB 与深度、LiDAR、L1 检测），
  以及相机（Pinhole 内参、机体 ← 光学帧）与 LiDAR（机体 ← 传感器）的静态标定；不含底图（`/world/basemap`）、不含 `/truth/**`、
  `/eval/**` 与实例 / 语义分割，排名运行不含种子。
- 生成：每机一个观测分片（`rerun_export.export_obs`，时间窗 `[t0, t1)`），加一个段属性头（`layer = obs`、`uav` 为切片内机队、
  切片编号、时间窗与降级轮次），经 `rerun rrd optimize` 合并为单一 recording（视频按 GoP 边界重排块）。
- 关键事件：`<event>/truth/r<rev>/event_truth.json` 的 `key_events[]`（M23-FR-077；`interval_ms` 或以秒计的 `interval`）优先，其次
  `annotation_checks[]` 中已验证条目的首个计算区间 `computed_ms[0]`，再次 `plan.json` 的 `annotations[].window`（秒）；时刻为事件
  时刻，采集运行按清单 `event.offset_s` 换算为运行仿真时刻（Env 运行偏移为 0）。与运行区间不相交的关键事件跳过；窗口越界时平移到
  运行区间内，运行短于窗口时取整段运行。`--at` 可直接给出运行仿真时刻（秒）的窗口中点。
- 体积：单片 ≤ 64 MB（`--slice-cap-mb`，与 `scan_public.py static` 同一口径，1 MB = 2^20 B）。以写出后的实际字节数判定；超出时依次
  把 crf 加 2、RGB 帧率降至 2.5 Hz、LiDAR 点数降至 3,000（逐轮累加），至多 3 轮，仍超出则该切片失败（696）。

底图（`<out>/basemap.rrd`，`--world-dir` 给出时生成）：

- 来源：世界包 `visual/pointcloud/`（ANET_Q16 容器，单根或森林 `r-<k>/`）的全部节点，颜色取容器 `col` 的 sRGB（`col.rgb = srgb`；
  旧世界为烘焙高度色）。
- 裁剪：事件舞台（`awr.sim.events.compile.stage_bbox`：被引用锚点、背景区与通信区的包围盒外扩 60 m），`--margin-m` 另加外扩；
  也可用 `--bbox X0 Y0 X1 Y1` 直接给出（世界系 ENU，米）。
- 降采样：体素网格（位置与颜色取体素内均值，确定），体素从 `--voxel-m`（缺省 0.25 m）起每轮 ×1.25 增大，直至写出的文件
  ≤ 24 MB（`--basemap-cap-mb`），至多 24 轮，仍超出则失败（696）。
- 内容：资产层（`layer = asset`）的 `/world`（RIGHT_HAND_Z_UP）与静态 `/world/basemap`（Points3D：位置、颜色、半径 = 体素半边长）；
  无时间轴数据，任何播放时刻可见。

校验（缺省开启，`--no-verify` 关闭）：每个文件 `rerun rrd verify` 与 `rerun_export.lint_rrd`（切片为 obs 层、底图为 asset 层），另查
切片不含底图实体、只含时间窗内的数据、每路视频首个样本为关键帧（可从切片起点开始解码）；首个切片与底图合并为单一 recording 后
再执行一次 verify，并核对合并后的实体集合与时间窗（"合并后可由原生 Viewer 打开并回放"的文件级判据）。

产物清单 `<out>/slices.json`（`drone4d.bench.public_slice.v1`）：运行身份、档位参数与降级轮次、每片的关键事件、时间窗（运行仿真
时刻与事件时刻）、字节数与 sha256、跳过的关键事件、底图参数，以及可直接并入 `lab_static_index.v1` 的 `index` 片段（`slices[]`
与 `basemap`，路径相对 `<out>`）。

保留期：输出目录为缺省的 `<run>/derived/slices/` 时，完成后以 `awr.bench.retention.register_derived(run, "slices")` 登记派生物
（standard 30 天，来源 Run 清理时同时清理；发布为静态制品后由 `sync_references(root, "static:<ver>", [(run_id, "slices")])` 升为
pinned），由每小时的 `python -m awr.bench.retention gc` 清理（M27-FR-058、FR-059）。

用法：
    python tools/bench/d3/public_slice.py --run <run_dir> --event-dir <events>/<eid>/v<ver> --world-dir <worlds>/<world> \\
        [--out DIR] [--window-s 10] [--slice-cap-mb 64] [--basemap-cap-mb 24] [--key ID ...] [--at T_S ...] [--uav ID ...] \\
        [--max-slices N] [--jobs 3] [--voxel-m 0.25] [--margin-m 0] [--bbox X0 Y0 X1 Y1] [--no-basemap] [--no-verify] [--no-register]
退出码 0 成功、1 有切片或底图失败（清单仍写出，含失败原因）、2 参数错误。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

from awr.bench.recorder import rerun_export as rx  # noqa: E402

__all__ = ["BASEMAP_CAP_MB", "DEGRADE_STEPS", "SCHEMA", "SLICE_CAP_MB", "WINDOW_S", "KeyEvent", "build_basemap", "build_public",
           "build_slice", "check_merged", "degrade", "key_events", "load_world_points", "main", "merged_rows", "slice_window", "stage_bbox_of",
           "voxel_downsample"]

SCHEMA = "drone4d.bench.public_slice.v1"
NS = 1_000_000_000
MB = 1024 * 1024                         # 与 scan_public.py static 的单文件上限同一口径
WINDOW_S = 10.0                          # rerun.public.window_s（M27 §8 配置表）
SLICE_CAP_MB = 64.0                      # rerun.public.slice_cap_mb
BASEMAP_CAP_MB = 24.0                    # rerun.public.basemap_cap_mb
DEGRADE_STEPS = ("crf+2", "rgb_hz=2.5", "lidar_max_points=3000")     # M27 §6.7.8 第 4 步，逐轮累加，至多 3 轮
VOXEL_M = 0.25
VOXEL_GROWTH = 1.25
VOXEL_ROUNDS = 24
BASEMAP_BYTES_PER_POINT = 16             # Points3D：f32 位置 12 B + RGBA 4 B（写出前的体积预估，实际以文件字节为准）
BASEMAP_ENTITY = "/world/basemap"
FAILED = 696


# ---------------------------------------------------------------------- 档位与降级


def degrade(prof: rx.Profile, level: int) -> rx.Profile:
    """`public` 档第 `level` 轮降级（0 = 原档）：1 crf + 2；2 另把 RGB 帧率降至 2.5 Hz；3 另把 LiDAR 点数降至 3,000。"""
    if not 0 <= level <= len(DEGRADE_STEPS):
        raise ValueError(f"degrade level {level} outside 0..{len(DEGRADE_STEPS)}")
    p = prof
    if level >= 1:
        p = dataclasses.replace(p, crf=prof.crf + 2)
    if level >= 2:
        p = dataclasses.replace(p, rgb_hz=min(float(prof.rgb_hz or 2.5), 2.5))
    if level >= 3:
        p = dataclasses.replace(p, lidar_max_points=min(int(prof.lidar_max_points or 3000), 3000))
    return p


def _profile_doc(p: rx.Profile) -> dict:
    return {"name": p.name, "rgb": p.rgb, "crf": p.crf, "gop_s": p.gop_s, "preset": p.preset, "rgb_hz": p.rgb_hz,
            "rgb_max": list(p.rgb_max) if p.rgb_max else None, "depth": p.depth, "depth_hz": p.depth_hz,
            "depth_max": list(p.depth_max) if p.depth_max else None, "lidar_hz": p.lidar_hz, "lidar_max_points": p.lidar_max_points}


# ---------------------------------------------------------------------- 关键事件与时间窗


@dataclass(frozen=True)
class KeyEvent:
    """关键事件（事件时刻，秒）。`source`：key_events | annotation_checks | plan | at。"""

    id: str
    type: str
    a_s: float
    b_s: float
    source: str

    @property
    def mid_s(self) -> float:
        return 0.5 * (self.a_s + self.b_s)


def _event_truth(event_dir: Path) -> dict | None:
    """`truth/r<rev>/event_truth.json`（取最大 rev）或旧布局 `truth/event_truth.json`。"""
    d = Path(event_dir) / "truth"
    cands = sorted(d.glob("r*/event_truth.json"), key=lambda p: int(p.parent.name[1:]) if p.parent.name[1:].isdigit() else -1)
    p = cands[-1] if cands else d / "event_truth.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def key_events(event_dir: Path) -> list[KeyEvent]:
    """关键事件清单（按开始时刻排序）：`key_events[]` > 已验证的 `annotation_checks[]` > `plan.json` 的 `annotations[]`。"""
    event_dir = Path(event_dir)
    tr = _event_truth(event_dir) or {}
    out: list[KeyEvent] = []
    for k in tr.get("key_events") or []:
        if "interval_ms" in k:
            a, b = (float(x) / 1000.0 for x in k["interval_ms"])
        elif "interval" in k:
            a, b = (float(x) for x in k["interval"])
        else:
            continue
        out.append(KeyEvent(str(k["id"]), str(k.get("type", "")), a, b, "key_events"))
    if not out:
        for c in tr.get("annotation_checks") or []:
            spans = c.get("computed_ms") or []
            if c.get("verified") and spans:
                a, b = (float(x) / 1000.0 for x in spans[0])
                out.append(KeyEvent(str(c["id"]), str(c.get("type", "")), a, b, "annotation_checks"))
    if not out and (event_dir / "plan.json").is_file():
        plan = json.loads((event_dir / "plan.json").read_text(encoding="utf-8"))
        out.extend(KeyEvent(str(a["id"]), str(a.get("type", "")), float(a["window"][0]), float(a["window"][1]), "plan")
                   for a in plan.get("annotations") or [])
    return sorted(out, key=lambda k: (k.a_s, k.id))


def event_offset_ns(src: rx.RunSource) -> int:
    """运行仿真时刻 = 事件时刻 − 偏移：采集运行为清单 `event.offset_s`（采集时刻 t 对应事件时刻 offset_s + t），Env 运行为 0。"""
    ev = src.manifest.get("event")
    if isinstance(ev, dict) and ev.get("offset_s") is not None:
        return round(float(ev["offset_s"]) * NS)
    return 0


def slice_window(mid_ns: int, t_range: tuple[int, int], window_ns: int) -> tuple[int, int]:
    """以 `mid_ns` 为中点的 `[t0, t1)`；越界时平移到运行区间内，运行短于窗口时取整段运行（`t1` 为末条消息之后 1 ns）。"""
    lo, hi = int(t_range[0]), int(t_range[1]) + 1
    if hi - lo <= window_ns:
        return lo, hi
    t0 = int(mid_ns) - window_ns // 2
    t0 = min(max(t0, lo), hi - window_ns)
    return t0, t0 + window_ns


# ---------------------------------------------------------------------- rerun 命令行


def _optimize(inputs: Sequence[Path], out: Path) -> None:
    """`rerun rrd optimize <inputs…> -o <out>`：合并为单一 recording 并压实块（视频按 GoP 边界重排）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    p = subprocess.run([rx._rerun_cli(), "rrd", "optimize", *map(str, inputs), "-o", str(tmp)], capture_output=True, text=True,
                       timeout=1800, env=rx._cli_env())
    if p.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise rx.RerunExportError(f"rerun rrd optimize failed: {(p.stdout + p.stderr).strip()[-2000:]}")
    tmp.replace(out)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------- 切片


def _shard_job(args: tuple) -> tuple[dict, list[str]]:
    run_dir, ident, uav, path, prof, window, far_m = args
    src = rx.RunSource.open(Path(run_dir), **ident)
    return rx.export_obs(src, uav, Path(path), prof, window=window, far_m=far_m), src.warnings


def _write_header(src: rx.RunSource, path: Path, *, uavs: list[str], key: str, window: tuple[int, int], level: int,
                  prof: rx.Profile) -> None:
    """切片段属性头：覆盖各分片的 `uav`（改为切片内机队）并记录切片编号、时间窗与降级轮次（静态属性取最新写入）。"""
    props = {**src.properties("obs"), "uav": ",".join(uavs), "profile": "public", "slice": key, "slice_t0_ns": int(window[0]),
             "slice_t1_ns": int(window[1]), "degrade_level": int(level), "crf": int(prof.crf), "rgb_hz": float(prof.rgb_hz or 0.0),
             "lidar_max_points": int(prof.lidar_max_points or 0)}
    w = rx.LayerWriter(path, app_id=src.app_id, recording_id=src.run_id, layer="obs", properties=props, timebase=src.timebase,
                       recording_name=f"{src.run_id} public {key}")
    w.close()


def build_slice(src: rx.RunSource, key: str, window: tuple[int, int], out_path: Path, *, uavs: Sequence[str] | None = None,
                cap_bytes: int = int(SLICE_CAP_MB * MB), jobs: int = 1, far_m: float = rx.DEFAULT_FAR_M,
                ident: dict | None = None) -> dict:
    """一个关键事件的切片：逐轮（0..3）导出各机观测分片、写段属性头并合并；写出体积 ≤ `cap_bytes` 即停止，否则 696。"""
    out_path = Path(out_path)
    sel = sorted(uavs) if uavs else list(src.uavs)
    ident = ident or {"world": src.world, "event": src.event, "run_id": src.run_id}
    base = rx.PROFILES["public"]
    rounds: list[dict] = []
    warnings: list[str] = []
    t_wall = time.perf_counter()
    for level in range(len(DEGRADE_STEPS) + 1):
        prof = degrade(base, level)
        with tempfile.TemporaryDirectory(prefix=f".{out_path.stem}-", dir=out_path.parent) as td:
            tdir = Path(td)
            tasks = [(str(src.run_dir), ident, u, str(tdir / f"obs.{rx.path_part(u)}.rrd"), prof, window, far_m) for u in sel]
            if jobs > 1 and len(tasks) > 1:
                import multiprocessing as mp
                from concurrent.futures import ProcessPoolExecutor

                with ProcessPoolExecutor(max_workers=min(int(jobs), len(tasks)), mp_context=mp.get_context("spawn")) as ex:
                    res = list(ex.map(_shard_job, tasks))
            else:
                res = [_shard_job(t) for t in tasks]
            shards = {}
            for (st, wn), t in zip(res, tasks, strict=True):
                shards[t[2]] = {"bytes": st["bytes"], "nav": st["nav"], "detections": st["detections"],
                                "cameras": {k: {x: v.get(x) for x in ("rgb_frames", "rgb_samples", "keyframes", "depth_frames", "calib_source",
                                                                      "cam_pose", "video")} for k, v in st["cameras"].items()},
                                "lidars": {k: {x: v.get(x) for x in ("frames", "points")} for k, v in st["lidars"].items()}}
                warnings += [x for x in wn if x not in warnings]
            head = tdir / "header.rrd"
            _write_header(src, head, uavs=sel, key=key, window=window, level=level, prof=prof)
            _optimize([*(Path(t[3]) for t in tasks), head], out_path)
        size = out_path.stat().st_size
        rounds.append({"level": level, "steps": list(DEGRADE_STEPS[:level]), "bytes": size, "profile": _profile_doc(prof)})
        if size <= cap_bytes:
            return {"key": key, "path": str(out_path), "bytes": size, "sha256": _sha256(out_path), "level": level, "rounds": rounds,
                    "profile": _profile_doc(prof), "uavs": sel, "window_ns": [int(window[0]), int(window[1])], "shards": shards,
                    "warnings": warnings, "wall_s": round(time.perf_counter() - t_wall, 3)}
    out_path.unlink(missing_ok=True)
    raise rx.RerunExportError(f"{FAILED} public slice {key!r} exceeds {cap_bytes} B after {len(DEGRADE_STEPS)} degrade rounds",
                              {"key": key, "cap_bytes": cap_bytes, "rounds": rounds})


def merged_rows(rows: list[tuple[int | None, dict]]) -> list[tuple[int | None, dict]]:
    """同一时刻的多行合并为一行（`rerun rrd optimize` 后同一次 log 的各分量可能落在不同块中）。"""
    out: dict[int | None, dict] = {}
    for t, v in rows:
        out.setdefault(t, {}).update(v)
    return sorted(out.items(), key=lambda r: -1 if r[0] is None else r[0])


def _video_first_keyframe(path: Path) -> dict[str, bool]:
    """每路 `…/image/rgb` 视频：时间最早的样本是否为关键帧（Viewer 从切片起点开始解码的前提）。"""
    inv = rx.scan_rrd(path)
    out = {}
    for e, archs in sorted(inv.entities.items()):
        if e.endswith("/image/rgb") and "VideoStream" in archs:
            rows = [(t, v) for t, v in merged_rows(rx.read_entity(path, e)) if t is not None and v.get("VideoStream:sample") is not None]
            if rows:
                k = rows[0][1].get("VideoStream:is_keyframe")
                out[e] = bool(k[0] if isinstance(k, list) else k) if k is not None else False
    return out


def _fleet_times(path: Path, inv: rx.RrdInventory, uav: str) -> list[int]:
    """一机在文件中的时间样本：`/fleet/<uav>`（导航解）存在时取它，否则取该机下按路径排序的首个时序实体（采集运行不记录导航解）。"""
    U = f"/fleet/{rx.path_part(uav)}"
    ent = U if U in inv.entities else next((e for e in sorted(inv.entities) if e.startswith(U + "/")), None)
    return [] if ent is None else [t for t, _ in rx.read_entity(path, ent) if t is not None]


def check_slice(path: Path, src: rx.RunSource, window: tuple[int, int], uavs: Sequence[str]) -> list[str]:
    """切片校验：obs 层路径校验 + `rerun rrd verify` + 不含底图 + 每机都有观测实体（运行记录了导航解时 `/fleet/<uav>` 必须存在）
    + 数据在时间窗内 + 视频首样本为关键帧。"""
    errs = rx.lint_rrd(path, "obs", run_id=src.run_id, app_id=src.app_id)
    ok, msg = rx.verify_rrd([path])
    if not ok:
        errs.append(f"rerun rrd verify: {msg.strip()[-1000:]}")
    inv = rx.scan_rrd(path)
    ents = set(inv.entities) | set(inv.static)
    if any(e == BASEMAP_ENTITY or e.startswith(BASEMAP_ENTITY + "/") for e in ents):
        errs.append("public slice must not embed the basemap (/world/basemap)")
    for u in uavs:
        U = f"/fleet/{rx.path_part(u)}"
        if not any(e == U or e.startswith(U + "/") for e in ents):
            errs.append(f"{U}/** missing from the slice")
        elif f"/uav/{u}/obs/nav" in src.channels and U not in inv.entities:
            errs.append(f"{U}: navigation solution missing although the run records /uav/{u}/obs/nav")
        ts = _fleet_times(path, inv, u)
        if not ts:
            errs.append(f"{U}: no samples inside the slice window")
        elif min(ts) < window[0] or max(ts) >= window[1]:
            errs.append(f"{U}: samples outside the slice window [{window[0]}, {window[1]})")
    for e, key in _video_first_keyframe(path).items():
        if not key:
            errs.append(f"{e}: first video sample is not a keyframe")
    return errs


# ---------------------------------------------------------------------- 底图


def _containers(world_dir: Path) -> list[Path]:
    """`visual/pointcloud/`（单根）或 `visual/pointcloud/r-<k>/`（森林）。"""
    d = Path(world_dir) / "visual" / "pointcloud"
    if (d / "metadata.json").is_file():
        return [d]
    return sorted(p.parent for p in d.glob("*/metadata.json"))


def load_world_points(world_dir: Path, bbox: tuple[float, float, float, float] | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """世界彩色点云（ANET_Q16 容器全部节点）→ (xyz float64 (n, 3), rgb uint8 (n, 3), 来源信息)；`bbox` 为 (x0, y0, x1, y1) 裁剪。"""
    from awr.world.pointcloud.reader import iter_nodes, read_container

    roots = _containers(world_dir)
    if not roots:
        raise rx.RerunExportError(f"{world_dir}: no point cloud container under visual/pointcloud/")
    xs, cs, total, modes = [], [], 0, set()
    for r in roots:
        c = read_container(r)
        modes.add(str(((c.metadata.get("anet") or {}).get("col") or {}).get("rgb", "unknown")))
        for v in iter_nodes(c, decode=True):
            if not v.num_points:
                continue
            p = v.positions
            total += len(p)
            if bbox is not None:
                m = (p[:, 0] >= bbox[0]) & (p[:, 0] <= bbox[2]) & (p[:, 1] >= bbox[1]) & (p[:, 1] <= bbox[3])
                if not m.any():
                    continue
                xs.append(p[m])
                cs.append(v.col_u8[m, :3])
            else:
                xs.append(p)
                cs.append(v.col_u8[:, :3])
    xyz = np.concatenate(xs) if xs else np.zeros((0, 3))
    rgb = np.concatenate(cs).astype(np.uint8) if cs else np.zeros((0, 3), np.uint8)
    return xyz, rgb, {"containers": [str(r) for r in roots], "points_total": int(total), "points_cropped": len(xyz),
                      "color": sorted(modes)}


def voxel_downsample(xyz: np.ndarray, rgb: np.ndarray, voxel_m: float) -> tuple[np.ndarray, np.ndarray]:
    """体素网格降采样：每个被占体素一个点，位置与颜色取体素内均值（与输入顺序无关，确定）。"""
    if not len(xyz):
        return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint8)
    q = np.floor(np.asarray(xyz, np.float64) / float(voxel_m)).astype(np.int64)
    _, inv, cnt = np.unique(q, axis=0, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    n = len(cnt)
    pos = np.stack([np.bincount(inv, weights=xyz[:, k], minlength=n) for k in range(3)], 1) / cnt[:, None]
    col = np.stack([np.bincount(inv, weights=rgb[:, k].astype(np.float64), minlength=n) for k in range(3)], 1) / cnt[:, None]
    return pos.astype(np.float32), np.clip(np.rint(col), 0, 255).astype(np.uint8)


def stage_bbox_of(event_dir: Path, margin_m: float = 0.0) -> tuple[float, float, float, float]:
    """事件舞台包围盒（M23：被引用锚点、背景区与通信区外扩 60 m），另加 `margin_m`。"""
    from awr.sim.events.compile import stage_bbox

    event_dir = Path(event_dir)
    plan = json.loads((event_dir / "plan.json").read_text(encoding="utf-8"))
    anchors = json.loads((event_dir / "anchors.json").read_text(encoding="utf-8"))
    a = {x["id"]: x for x in anchors.get("anchors", anchors if isinstance(anchors, list) else [])}
    x0, y0, x1, y1 = stage_bbox(plan, a)
    m = float(margin_m)
    return x0 - m, y0 - m, x1 + m, y1 + m


def build_basemap(src: rx.RunSource, world_dir: Path, out_path: Path, *, bbox: tuple[float, float, float, float] | None,
                  cap_bytes: int = int(BASEMAP_CAP_MB * MB), voxel_m: float = VOXEL_M) -> dict:
    """`basemap.rrd`：裁剪后的世界彩色点云按体素降采样，体素逐轮 ×1.25 直至写出 ≤ `cap_bytes`（至多 24 轮，否则 696）。"""
    t_wall = time.perf_counter()
    out_path = Path(out_path)
    xyz, rgb, info = load_world_points(world_dir, bbox)
    if not len(xyz):
        raise rx.RerunExportError(f"{FAILED} basemap: no world points inside the stage bbox {bbox}", {"bbox": bbox, **info})
    v = float(voxel_m)
    rounds: list[dict] = []
    for _ in range(VOXEL_ROUNDS):
        pos, col = voxel_downsample(xyz, rgb, v)
        est = len(pos) * BASEMAP_BYTES_PER_POINT
        if est > cap_bytes:                                            # 预估已超出：不写文件，直接增大体素
            rounds.append({"voxel_m": round(v, 6), "points": len(pos), "bytes_est": est})
            v *= VOXEL_GROWTH
            continue
        props = {**src.properties("asset"), "asset": "basemap", "world": src.world, "voxel_m": float(v), "points": len(pos),
                 "bbox_enu_m": json.dumps([round(float(x), 3) for x in bbox] if bbox else None)}
        w = rx.LayerWriter(out_path, app_id=src.app_id, recording_id=src.run_id, layer="asset", properties=props, timebase=src.timebase,
                           recording_name=f"{src.run_id} basemap")
        rr = w.rr
        rx._base(w, semantic=False)
        w.static(BASEMAP_ENTITY, rr.Points3D(pos, colors=col, radii=[0.5 * v]))
        w.close()
        size = out_path.stat().st_size
        rounds.append({"voxel_m": round(v, 6), "points": len(pos), "bytes_est": est, "bytes": size})
        if size <= cap_bytes:
            return {"path": str(out_path), "bytes": size, "sha256": _sha256(out_path), "voxel_m": round(v, 6), "points": len(pos),
                    "bbox_enu_m": [float(x) for x in bbox] if bbox else None, "rounds": rounds, **info,
                    "wall_s": round(time.perf_counter() - t_wall, 3)}
        v *= VOXEL_GROWTH
    out_path.unlink(missing_ok=True)
    raise rx.RerunExportError(f"{FAILED} basemap exceeds {cap_bytes} B after {VOXEL_ROUNDS} voxel rounds", {"rounds": rounds})


def check_basemap(path: Path, src: rx.RunSource) -> list[str]:
    errs = rx.lint_rrd(path, "asset", run_id=src.run_id, app_id=src.app_id)
    ok, msg = rx.verify_rrd([path])
    if not ok:
        errs.append(f"rerun rrd verify: {msg.strip()[-1000:]}")
    inv = rx.scan_rrd(path)
    if "Points3D" not in inv.static.get(BASEMAP_ENTITY, set()):
        errs.append(f"{BASEMAP_ENTITY}: static Points3D missing")
    if inv.entities:
        errs.append(f"basemap carries temporal data: {sorted(inv.entities)[:5]}")
    return errs


def check_merged(slice_path: Path, basemap_path: Path, work: Path, *, app_id: str, recording_id: str, uavs: Iterable[str],
                 window: tuple[int, int]) -> dict:
    """切片与底图合并为单一 recording（与 Viewer 依次打开两个文件的合并语义相同），verify 并核对实体集合与时间窗。"""
    merged = Path(work) / f".merged-{Path(slice_path).stem}.rrd"
    errs: list[str] = []
    try:
        _optimize([slice_path, basemap_path], merged)
        ok, msg = rx.verify_rrd([merged])
        if not ok:
            errs.append(f"rerun rrd verify: {msg.strip()[-1000:]}")
        inv = rx.scan_rrd(merged)
        if inv.n_recordings != 1:
            errs.append(f"merged file holds {inv.n_recordings} recordings (application_id / recording_id differ)")
        if (inv.application_id, inv.recording_id) != (app_id, recording_id):
            errs.append(f"merged store {(inv.application_id, inv.recording_id)} != {(app_id, recording_id)}")
        ents = set(inv.entities) | set(inv.static)
        miss = [] if BASEMAP_ENTITY in ents else [BASEMAP_ENTITY]
        miss += [f"/fleet/{rx.path_part(u)}/**" for u in uavs if not any(e.startswith(f"/fleet/{rx.path_part(u)}") for e in ents)]
        if miss:
            errs.append(f"merged recording lacks {miss}")
        ts = []
        for u in uavs:
            ts += _fleet_times(merged, inv, u)
        span = [min(ts), max(ts)] if ts else None
        if span is None or span[0] < window[0] or span[1] >= window[1]:
            errs.append(f"merged sim_time span {span} outside [{window[0]}, {window[1]})")
        return {"ok": not errs, "problems": errs, "bytes": merged.stat().st_size if merged.is_file() else 0, "n_recordings": inv.n_recordings,
                "timelines": sorted(inv.timelines), "sim_time_span_ns": span, "entities": len(ents)}
    except rx.RerunExportError as e:
        return {"ok": False, "problems": [str(e)]}
    finally:
        merged.unlink(missing_ok=True)


# ---------------------------------------------------------------------- 编排


def _find_event_dir(src: rx.RunSource, events_root: Path | None) -> Path | None:
    if events_root is None:
        return None
    ev = src.manifest.get("event") if isinstance(src.manifest.get("event"), dict) else {}
    eid = ev.get("event_id") or ev.get("id") or src.event
    ver = ev.get("version")
    cands = [Path(events_root) / eid / f"v{ver}"] if ver is not None else sorted((Path(events_root) / eid).glob("v*"))
    cands = [c for c in cands if c.is_dir()]
    return cands[-1] if cands else None


def build_public(run_dir: Path, *, out: Path | None = None, event_dir: Path | None = None, events_root: Path | None = None,
                 world_dir: Path | None = None, window_s: float = WINDOW_S, slice_cap_mb: float = SLICE_CAP_MB,
                 basemap_cap_mb: float = BASEMAP_CAP_MB, keys: Sequence[str] | None = None, at_s: Sequence[float] | None = None,
                 uavs: Sequence[str] | None = None, max_slices: int | None = None, jobs: int = 1, voxel_m: float = VOXEL_M,
                 margin_m: float = 0.0, bbox: tuple[float, float, float, float] | None = None, basemap: bool = True,
                 verify: bool = True, register: bool = True, world: str | None = None, event: str | None = None,
                 far_m: float = rx.DEFAULT_FAR_M) -> dict:
    """一个公开样例运行的全部切片与底图；写 `slices.json`。有切片或底图失败时清单的 `ok = false`（调用方据此返回 696）。"""
    t_wall = time.perf_counter()
    sdk = rx.require_sdk()
    run_dir = Path(run_dir)
    ident = {"world": world, "event": event, "run_id": None}
    src = rx.RunSource.open(run_dir, world=world, event=event)
    ident["run_id"] = src.run_id
    default_out = run_dir / "derived" / "slices"
    out = Path(out) if out else default_out
    out.mkdir(parents=True, exist_ok=True)
    sel = sorted(uavs) if uavs else list(src.uavs)
    miss = [u for u in sel if u not in src.uavs]
    if miss:
        raise ValueError(f"unknown uav ids {miss}; run has {src.uavs}")
    nonav = [u for u in sel if f"/uav/{u}/obs/nav" not in src.channels]
    event_dir = Path(event_dir) if event_dir else _find_event_dir(src, events_root)
    off = event_offset_ns(src)
    win_ns = round(float(window_s) * NS)
    t_lo, t_hi = src.t_range
    skipped: list[dict] = []
    if at_s:
        kes = [KeyEvent(f"t{round(float(t) * 1000):08d}ms", "at", float(t) + off / NS, float(t) + off / NS, "at") for t in at_s]
    else:
        if event_dir is None:
            raise ValueError("--event-dir (or --events-root) is required to locate key events, or give --at")
        kes = key_events(event_dir)
        if keys:
            kes = [k for k in kes if k.id in set(keys)]
    plan: list[tuple[KeyEvent, tuple[int, int]]] = []
    for k in kes:
        a, b = round(k.a_s * NS) - off, round(k.b_s * NS) - off
        if b < t_lo or a > t_hi:
            skipped.append({"id": k.id, "reason": "outside_run", "event_interval_s": [k.a_s, k.b_s]})
            continue
        plan.append((k, slice_window(round(k.mid_s * NS) - off, (t_lo, t_hi), win_ns)))
    if max_slices is not None:
        for k, _w in plan[max_slices:]:
            skipped.append({"id": k.id, "reason": "max_slices"})
        plan = plan[:max_slices]
    man: dict = {"schema": SCHEMA, "run_id": src.run_id, "application_id": src.app_id, "recording_id": src.run_id, "world": src.world,
                 "event": src.event, "event_dir": str(event_dir) if event_dir else None, "event_offset_ns": off,
                 "run_t_range_ns": [int(t_lo), int(t_hi)], "rerun_version": sdk, "window_s": float(window_s),
                 "slice_cap_bytes": int(slice_cap_mb * MB), "basemap_cap_bytes": int(basemap_cap_mb * MB), "degrade_steps": list(DEGRADE_STEPS),
                 "profile": _profile_doc(rx.PROFILES["public"]), "uavs": sel, "layers": ["obs"], "slices": [], "skipped": skipped,
                 "failures": [], "basemap": None, "created_unix_ns": time.time_ns(),
                 "warnings": [f"{u}: run records no /uav/{u}/obs/nav; /fleet/{rx.path_part(u)} carries no pose (capture run)" for u in nonav]}
    if not plan:
        man["failures"].append({"what": "slices", "error": "no key event intersects the run interval"})
    for k, w in plan:
        key = rx.path_part(k.id)
        try:
            rec = build_slice(src, key, w, out / f"{key}.rrd", uavs=sel, cap_bytes=int(slice_cap_mb * MB), jobs=jobs, far_m=far_m, ident=ident)
        except rx.RerunExportError as e:
            man["failures"].append({"what": "slice", "key": key, "code": e.code, "error": str(e), "detail": e.detail})
            continue
        rec.update(key_event={"id": k.id, "type": k.type, "source": k.source, "event_interval_s": [k.a_s, k.b_s]},
                   t_from_s=round(w[0] / NS, 6), t_to_s=round(w[1] / NS, 6), event_t_from_s=round((w[0] + off) / NS, 6),
                   event_t_to_s=round((w[1] + off) / NS, 6), rrd=Path(rec["path"]).name)
        man["warnings"] += [x for x in rec.pop("warnings") if x not in man["warnings"]]
        if verify:
            rec["verify"] = {"problems": check_slice(Path(rec["path"]), src, w, sel)}
            rec["verify"]["ok"] = not rec["verify"]["problems"]
            if not rec["verify"]["ok"]:
                man["failures"].append({"what": "slice_verify", "key": key, "problems": rec["verify"]["problems"]})
        man["slices"].append(rec)
    if basemap and world_dir is not None:
        try:
            bb = bbox if bbox is not None else (stage_bbox_of(event_dir, margin_m) if event_dir is not None else None)
            bm = build_basemap(src, Path(world_dir), out / "basemap.rrd", bbox=bb, cap_bytes=int(basemap_cap_mb * MB), voxel_m=voxel_m)
            bm["bbox_source"] = "argument" if bbox is not None else ("event_stage" if bb is not None else "whole_world")
            if verify:
                bm["verify"] = {"problems": check_basemap(Path(bm["path"]), src)}
                bm["verify"]["ok"] = not bm["verify"]["problems"]
                if not bm["verify"]["ok"]:
                    man["failures"].append({"what": "basemap_verify", "problems": bm["verify"]["problems"]})
            man["basemap"] = bm
        except rx.RerunExportError as e:
            man["failures"].append({"what": "basemap", "code": e.code, "error": str(e), "detail": e.detail})
    if verify and man["basemap"] and man["slices"]:
        s0 = man["slices"][0]
        man["merged_check"] = {"slice": s0["key"], **check_merged(Path(s0["path"]), Path(man["basemap"]["path"]), out, app_id=src.app_id,
                                                                 recording_id=src.run_id, uavs=sel, window=tuple(s0["window_ns"]))}
        if not man["merged_check"]["ok"]:
            man["failures"].append({"what": "merged_check", "problems": man["merged_check"]["problems"]})
    man["index"] = {"slices": [{"event_id": s["key_event"]["id"], "t_from_s": s["t_from_s"], "t_to_s": s["t_to_s"], "rrd": s["rrd"],
                                "bytes": s["bytes"], "sha256": s["sha256"]} for s in man["slices"]],
                    "basemap": Path(man["basemap"]["path"]).name if man["basemap"] else None}
    man["warnings"] = [*man["warnings"], *(x for x in src.warnings if x not in man["warnings"])]
    man["ok"] = not man["failures"]
    reg = None
    if register and out.resolve() == default_out.resolve() and (man["slices"] or man["basemap"]):
        from awr.bench.retention import register_derived

        rec = register_derived(run_dir, "slices")
        reg = {"kind": "slices", **rec["derived"]["slices"]}
    man["retention"] = reg
    man["wall_s"] = round(time.perf_counter() - t_wall, 3)
    (out / "slices.json").write_text(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return man


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/bench/d3/public_slice.py", description="public-profile time-window slices and basemap.rrd (M27 §6.7.8)")
    ap.add_argument("--run", dest="run_dir", required=True, type=Path, help="Run directory with raw.mcap")
    ap.add_argument("--out", type=Path, default=None, help="output directory (default <run>/derived/slices, registered for retention)")
    ap.add_argument("--event-dir", type=Path, default=None, help="event version directory (plan.json, anchors.json, truth/r<rev>/)")
    ap.add_argument("--events-root", type=Path, default=None, help="events root; the event directory is <root>/<event>/v<version>")
    ap.add_argument("--world-dir", type=Path, default=None, help="world package directory (visual/pointcloud/) for basemap.rrd")
    ap.add_argument("--world", default=None)
    ap.add_argument("--event", default=None)
    ap.add_argument("--window-s", type=float, default=WINDOW_S)
    ap.add_argument("--slice-cap-mb", type=float, default=SLICE_CAP_MB)
    ap.add_argument("--basemap-cap-mb", type=float, default=BASEMAP_CAP_MB)
    ap.add_argument("--key", action="append", default=None, help="only these key event ids")
    ap.add_argument("--at", action="append", type=float, default=None, help="window midpoint in run sim time (s) instead of key events")
    ap.add_argument("--uav", action="append", default=None)
    ap.add_argument("--max-slices", type=int, default=None)
    ap.add_argument("--jobs", type=int, default=1, help="parallel per-vehicle shard processes")
    ap.add_argument("--voxel-m", type=float, default=VOXEL_M)
    ap.add_argument("--margin-m", type=float, default=0.0)
    ap.add_argument("--bbox", nargs=4, type=float, default=None, metavar=("X0", "Y0", "X1", "Y1"))
    ap.add_argument("--far-m", type=float, default=rx.DEFAULT_FAR_M)
    ap.add_argument("--no-basemap", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--no-register", action="store_true")
    a = ap.parse_args(argv)
    try:
        man = build_public(a.run_dir, out=a.out, event_dir=a.event_dir, events_root=a.events_root, world_dir=a.world_dir, window_s=a.window_s,
                           slice_cap_mb=a.slice_cap_mb, basemap_cap_mb=a.basemap_cap_mb, keys=a.key, at_s=a.at, uavs=a.uav,
                           max_slices=a.max_slices, jobs=a.jobs, voxel_m=a.voxel_m, margin_m=a.margin_m,
                           bbox=tuple(a.bbox) if a.bbox else None, basemap=not a.no_basemap, verify=not a.no_verify,
                           register=not a.no_register, world=a.world, event=a.event, far_m=a.far_m)
    except (ValueError, rx.RerunExportError) as e:
        print(json.dumps({"ok": False, "code": getattr(e, "code", 2), "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": man["ok"], "run_id": man["run_id"], "slices": [{"key": s["key"], "bytes": s["bytes"], "level": s["level"],
                                                                            "t_from_s": s["t_from_s"], "t_to_s": s["t_to_s"]} for s in man["slices"]],
                      "basemap": {k: man["basemap"][k] for k in ("bytes", "voxel_m", "points")} if man["basemap"] else None,
                      "merged_ok": (man.get("merged_check") or {}).get("ok"), "failures": man["failures"], "skipped": man["skipped"],
                      "wall_s": man["wall_s"]}, ensure_ascii=False))
    return 0 if man["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
