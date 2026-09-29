#!/usr/bin/env python3
"""LiDAR 帧夹具生成器（M13-FR-052；M13 §6.5.10 路径 B；D1 桩，P2）。

在 M04 `dsm_grid()`（dsm_eff，柱体语义，取最近格）上做 2.5D 步进求交（步长 0.5 m、量程 40 m）作为测试替身，产出
`awr.sensor.lidar_frame.v1`：每帧 20 000 点（未命中射线不输出），`offset_time = i·5000 ns`、`line = i & 3`，`timebase_ns`
对齐仿真时间的 100 ms 网格，`sync_type = sim`（AWR-03 §5.2 第 8 条：虚拟传感器为 sim），`T_world_sensor` 给出真值，
`reflectivity = clamp(round(150·rho_eff), 0, 150)`（rho 取伪反射率：屋顶 0.60、立面 0.45、地面 0.25）。只用于契约与回归，
不接入 sim-core 运行时与 UI（不进入 awr 包）。
用法：.venv/bin/python tests/sensors/fixtures/gen_lidar_fixture.py --world shenzhen --xy 80 10 --agl 30 [--preset inverted_mapping]
      [--frame 0] [--out runs/fixtures/lidar_frame.bin]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python"))

from awr.sim.sensors.frames import mount_R  # noqa: E402
from awr.sim.sensors.lidar.frame import encode_lidar_frame  # noqa: E402
from awr.sim.sensors.lidar.pattern import NPF, directions_flu, mid360_pattern, offset_time_ns  # noqa: E402

RHO = {"roof": 0.60, "facade": 0.45, "ground": 0.25}
STEP_M = 0.5
RANGE_M = 40.0
GRID_NS = 100_000_000


def raycast_25d(grid: Any, origin: np.ndarray, dirs: np.ndarray, *, step_m: float = STEP_M, range_m: float = RANGE_M,
                ground: Any = None) -> tuple[np.ndarray, np.ndarray]:
    """(t_hit (n,), surface (n,) 0 无、1 屋顶、2 立面、3 地面)：沿射线按 step 采样最近格 DSM，第一次 z ≤ DSM 即命中。"""
    a = np.asarray(grid.a)
    H, W = a.shape
    x0, y0, cell = float(grid.x0_m), float(grid.y0_m), float(grid.cell_m)
    s = np.arange(step_m, range_m + 1e-9, step_m)
    P = origin[None, None, :] + dirs[:, None, :] * s[None, :, None]  # (n, k, 3)
    c = np.clip(np.floor((P[..., 0] - x0) / cell).astype(np.int64), 0, W - 1)
    r = np.clip(np.floor((P[..., 1] - y0) / cell).astype(np.int64), 0, H - 1)
    top = a[r, c]
    below = P[..., 2] <= top
    anyhit = below.any(axis=1)
    k = np.argmax(below, axis=1)
    t = np.where(anyhit, s[k], np.nan)
    surf = np.zeros(len(dirs), np.int8)
    idx = np.flatnonzero(anyhit)
    if idx.size:
        kk = k[idx]
        prev_top = np.where(kk > 0, top[idx, np.maximum(kk - 1, 0)], top[idx, kk])
        gz = top[idx, kk] if ground is None else np.asarray(ground(P[idx, kk, :2]), np.float64)
        is_ground = np.abs(top[idx, kk] - gz) < 0.5
        is_facade = ~is_ground & (np.abs(top[idx, kk] - prev_top) > 0.5) & (P[idx, kk, 2] < top[idx, kk] - STEP_M)
        surf[idx] = np.where(is_ground, 3, np.where(is_facade, 2, 1))
    return t, surf


def make_frame(grid: Any, sensor_pos: np.ndarray, R_ws: np.ndarray, frame_idx: int, *, uav_id: str = "p600-01",
               frame_id: str = "uav01/mid360", t_frame_ns: int | None = None, ground: Any = None) -> tuple[dict, dict]:
    az = np.empty(NPF)
    el = np.empty(NPF)
    mid360_pattern(frame_idx, az, el)
    d_s = directions_flu(az, el)
    d_w = d_s @ R_ws.T
    t, surf = raycast_25d(grid, sensor_pos, d_w, ground=ground)
    hit = np.isfinite(t)
    tb = int((frame_idx * GRID_NS) if t_frame_ns is None else (int(t_frame_ns) // GRID_NS) * GRID_NS)
    p_s = d_s[hit] * t[hit, None]
    rho = np.array([0.0, RHO["roof"], RHO["facade"], RHO["ground"]])[surf[hit]]
    n_dot = np.where(surf[hit] == 2, np.abs(d_w[hit, 0]) + np.abs(d_w[hit, 1]), np.abs(d_w[hit, 2]))
    refl = np.clip(np.round(150.0 * rho * np.maximum(np.minimum(n_dot, 1.0), 0.05)), 0, 150).astype(np.uint8)
    i = np.flatnonzero(hit)
    T = np.eye(4)
    T[:3, :3] = R_ws
    T[:3, 3] = sensor_pos
    hdr = {"schema": "awr.sensor.lidar_frame.v1", "sensor_id": f"{uav_id}/mid360", "frame_id": frame_id, "timebase_ns": tb,
           "sync_type": "sim", "point_count": int(i.size), "T_world_sensor": [[float(x) for x in row] for row in T],
           "frame_seq": int(frame_idx), "timescale": "host_mono", "raw_timebase_ns": tb, "frame_dur_ns": GRID_NS,
           "pattern_mode": 0, "simulated": True}
    pts = {"x": p_s[:, 0], "y": p_s[:, 1], "z": p_s[:, 2], "offset_time": offset_time_ns()[i], "reflectivity": refl,
           "tag": np.zeros(i.size, np.uint8), "line": (i & 3).astype(np.uint8)}
    return hdr, pts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--world", default="shenzhen")
    ap.add_argument("--xy", type=float, nargs=2, default=[87.1, 12.5])
    ap.add_argument("--agl", type=float, default=30.0)
    ap.add_argument("--preset", default="p600_prometheus_sim", choices=["p600_prometheus_sim", "inverted_mapping"])
    ap.add_argument("--frame", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "runs" / "fixtures" / "lidar_frame.bin"))
    a = ap.parse_args()
    import yaml

    from awr.world.geometry.query import open_world_query

    wq = open_world_query(ROOT / "worlds" / a.world)
    mid = yaml.safe_load((ROOT / "vehicles" / "p600" / "sensors" / "mid360.yaml").read_text(encoding="utf-8"))
    pr = mid["mount_presets"][a.preset]
    x, y = a.xy
    gz = float(wq.ground_dtm(np.array([[x, y]]))[0])
    base = np.array([x, y, gz + a.agl])
    R = mount_R(pr["rpy_deg"])
    pos = base + np.asarray(pr["xyz_m"], np.float64)
    hdr, pts = make_frame(wq.dsm_grid(), pos, R, a.frame, ground=lambda xy: wq.ground_dtm(xy))
    buf = encode_lidar_frame(hdr, pts)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(buf)
    print(f"{out}: {hdr['point_count']} points ({hdr['point_count'] / NPF:.0%} hit), {len(buf)} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
