"""跨 Run 位姿查询的规模测量（D3-AC-20"跨 3 个 Run 的位姿查询 ≤ 1 s"；M27-FR-083；r8 §9.5）。

按导出器的实体规范（`LayerWriter`，AWR-05 §6.1.3）合成 N 个 Run：每机一个观测分片（`/fleet/<uav>` 50 Hz 列式位姿、遥测
50 Hz、`cam0` 静态位姿与 Pinhole、`cam0/image/rgb` JPEG、`lidar0` Points3D）、真值层（`/truth/fleet/<uav>` 50 Hz）与评测层
（`/eval/visibility/<uav>/<actor>`），写出 `export.json` 后按评测与分发两个 catalog 注册，再在进程内 catalog 服务上计时：

- `poses(runs, hz)` 冷启动（新客户端首次查询，含 dataset 解析）与热查询（重复 `reps` 次）的 p50 与最大值；
- 分发 catalog 上 `/truth/**` 查询的行数（应为 0）；
- 注册耗时与服务重启后清单重放耗时。

命令行：`python -m awr.bench.catalog perf --work DIR [--runs 3] [--uavs 3] [--duration 360] [--cam-hz 10] [--lidar-points 5000]
[--out result.json]`；`--keep` 保留合成数据（缺省删除），`--no-optimize` 跳过注册前的 `rerun rrd optimize`。
"""

from __future__ import annotations

import io
import json
import shutil
import statistics
import time
from pathlib import Path

import numpy as np

from awr.bench.catalog.query import D3Catalog
from awr.bench.catalog.registry import dataset_name, register_run
from awr.bench.catalog.service import CatalogService
from awr.bench.recorder.rerun_export import (
    EXPORT_SCHEMA,
    RERUN_LOCKED,
    LayerWriter,
    Timebase,
    application_id,
    sdk_version,
)

__all__ = ["measure", "synth_run"]

WORLD, EVENT = "perf-world", "perf-event"
POSE_HZ = 50


def _jpeg(w: int, h: int, seed: int) -> bytes:
    from PIL import Image

    rng = np.random.default_rng(seed)
    a = (rng.integers(0, 64, (h, w, 3), np.uint8) + np.linspace(0, 160, w, dtype=np.uint8)[None, :, None]).astype(np.uint8)
    b = io.BytesIO()
    Image.fromarray(a).save(b, format="JPEG", quality=85)
    return b.getvalue()


def synth_run(out: Path, run_id: str, *, n_uav: int = 3, duration_s: float = 360.0, cam_hz: float = 10.0, lidar_hz: float = 10.0,
              lidar_points: int = 5000, image: tuple[int, int] = (640, 360), seed: int = 0) -> Path:
    """合成一个 Run 的导出目录（obs.<uav>.rrd、truth.rrd、eval.rrd、export.json）→ export.json 路径。"""
    import rerun as rr

    out = Path(out).absolute()
    out.mkdir(parents=True, exist_ok=True)
    app, tb = application_id(WORLD, EVENT), Timebase(250)
    uavs = [f"uav-{i + 1:02d}" for i in range(n_uav)]
    base = {"run_id": run_id, "policy": "perf", "n_uav": n_uav, "physics": "kinematic", "clock_profile": "d1_250", "obs_level": "L2",
            "appearance": "none", "noise": "realistic", "rerun_version": sdk_version(), "ranked": False}
    n_pose = int(duration_s * POSE_HZ)
    t_pose = np.arange(n_pose, dtype=np.int64) * (1_000_000_000 // POSE_HZ)
    jpg = _jpeg(*image, seed)
    rng = np.random.default_rng(seed)
    pts = rng.uniform(-40, 40, (lidar_points, 3)).astype(np.float32)
    files: dict = {}
    truth = LayerWriter(out / "truth.rrd", app_id=app, recording_id=run_id, layer="truth", properties={**base, "layer": "truth", "seed": 7},
                        timebase=tb)
    ev = LayerWriter(out / "eval.rrd", app_id=app, recording_id=run_id, layer="eval", properties={**base, "layer": "eval", "seed": 7},
                     timebase=tb)
    for w in (truth, ev):
        w.static("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP)
    for j, u in enumerate(uavs):
        p = out / f"obs.{u}.rrd"
        w = LayerWriter(p, app_id=app, recording_id=run_id, layer="obs", properties={**base, "layer": "obs", "uav": u}, timebase=tb)
        w.static("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP)
        w.static(f"/fleet/{u}/cam0", rr.Transform3D(translation=[0.1, 0.0, -0.05], quaternion=[0.5, -0.5, 0.5, -0.5]))
        w.static(f"/fleet/{u}/cam0/image", rr.Pinhole(resolution=list(image), focal_length=500.0))
        w.static(f"/fleet/{u}/lidar0", rr.Transform3D(translation=[0.0, 0.0, 0.1]))
        cam_every, lid_every = max(1, int(POSE_HZ / cam_hz)), max(1, int(POSE_HZ / lidar_hz))
        for k, t in enumerate(t_pose.tolist()):
            s = t / 1e9
            pos = [10.0 * j + 2.0 * s, 50.0 + np.sin(s), 30.0 + 0.1 * j]
            q = [0.0, 0.0, float(np.sin(0.05 * s / 2)), float(np.cos(0.05 * s / 2))]
            w.transform(f"/fleet/{u}", t, [pos[0] + 0.8, pos[1] - 0.5, pos[2] + 1.2], q)       # 带噪导航解（常值误差）
            truth.transform(f"/truth/fleet/{u}", t, pos, q)
            w.scalars(f"/fleet/{u}/telemetry/altitude", t, [pos[2]])
            w.scalars(f"/fleet/{u}/telemetry/speed", t, [2.0])
            if k % cam_every == 0:
                w.log(f"/fleet/{u}/cam0/image/rgb", t + 5_000_000, rr.EncodedImage(contents=jpg, media_type="image/jpeg"))
                ev.series(f"/eval/visibility/{u}/person_a", ["px_visible", "px_unoccluded"])
                ev.scalars(f"/eval/visibility/{u}/person_a", t + 5_000_000, [float((k // cam_every) % 3) * 100.0, 300.0])
            if lidar_points and k % lid_every == 0:
                w.log(f"/fleet/{u}/lidar0", t + 100_000_000, rr.Points3D(pts))
        w.close()
        files[f"obs.{u}"] = {"path": str(p), "bytes": p.stat().st_size, "uav": u}
    truth.close()
    ev.close()
    for k in ("truth", "eval"):
        files[k] = {"path": str(out / f"{k}.rrd"), "bytes": (out / f"{k}.rrd").stat().st_size}
    man = {"schema": EXPORT_SCHEMA, "run_id": run_id, "recording_id": run_id, "application_id": app, "world": WORLD, "event": EVENT,
           "rerun_version": sdk_version(), "rerun_locked": RERUN_LOCKED, "uavs": uavs, "files": files,
           "sensor_map": {"cam": {"eo": "cam0"}, "lidar": {"lidar0": "lidar0"}}, "sim_s": duration_s}
    (out / "export.json").write_text(json.dumps(man, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return out / "export.json"


def _stats(xs: list[float]) -> dict:
    return {"p50_s": round(statistics.median(xs), 4), "max_s": round(max(xs), 4), "n": len(xs)}


def measure(work: Path, *, runs: int = 3, n_uav: int = 3, duration_s: float = 360.0, cam_hz: float = 10.0, lidar_points: int = 5000,
            hz: tuple[float, ...] = (10.0, 50.0), reps: int = 10, keep: bool = False, optimize: bool = True) -> dict:
    work = Path(work).absolute()
    t0 = time.perf_counter()
    exports = [synth_run(work / "runs" / f"run-{i + 1:04d}", f"run-{i + 1:04d}", n_uav=n_uav, duration_s=duration_s, cam_hz=cam_hz,
                         lidar_points=lidar_points, seed=i) for i in range(runs)]
    synth_s = time.perf_counter() - t0
    size = sum(f.stat().st_size for f in (work / "runs").rglob("*.rrd"))
    root = work / "rerun"
    res: dict = {"runs": runs, "n_uav": n_uav, "duration_s": duration_s, "cam_hz": cam_hz, "lidar_points": lidar_points,
                 "rrd_bytes": size, "optimize": optimize, "synth_s": round(synth_s, 2), "rerun_version": sdk_version(), "queries": {}}
    ids = [f"run-{i + 1:04d}" for i in range(runs)]
    ds = dataset_name(WORLD, EVENT)
    for cat in ("eval", "dist"):
        with CatalogService(root, cat) as svc:
            t = time.perf_counter()
            for ex in exports:
                register_run(ex, catalog=cat, root=root, client=svc.client(), optimize=optimize)
            res[f"register_{cat}_s"] = round(time.perf_counter() - t, 3)
            if cat == "dist":
                q = D3Catalog(client=svc.client(), target="dist")
                res["dist_truth_rows"] = q.query(ds, "/truth/**").num_rows
                res["dist_truth_poses_rows"] = q.poses(ids, 10.0, source="truth").num_rows
            else:
                for h in hz:
                    cold = []
                    for _ in range(3):
                        t = time.perf_counter()
                        tb = D3Catalog(client=svc.client(), target="eval").poses(ids, h)
                        cold.append(time.perf_counter() - t)
                    q = D3Catalog(client=svc.client(), target="eval")
                    q.poses(ids, h)
                    warm = []
                    for _ in range(reps):
                        t = time.perf_counter()
                        tb = q.poses(ids, h)
                        warm.append(time.perf_counter() - t)
                    res["queries"][f"poses_{h:g}hz"] = {"rows": tb.num_rows, "cold": _stats(cold), "warm": _stats(warm)}
        t = time.perf_counter()
        with CatalogService(root, cat) as svc:
            res[f"replay_{cat}"] = {"s": round(time.perf_counter() - t, 3), "entries": svc.report["entries"],
                                    "skipped": len(svc.report["skipped"])}
    if not keep:
        shutil.rmtree(work / "runs", ignore_errors=True)
    return res
