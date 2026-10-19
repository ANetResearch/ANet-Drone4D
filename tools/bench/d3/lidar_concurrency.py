#!/usr/bin/env python3
"""tools/bench/d3/lidar_concurrency.py：网格 LiDAR 多机并发吞吐与 GPU/CPU 逐射线对拍（D3-AC-12；D3-AC-47 的 CPU 与 GPU 逐射线距离差
分布、逐点位姿与单一位姿的 ATE 比值；M25-FR-070、M25-AC-012/021）。

被测对象为产品路径 `awr.sensorsvc.lidar.warp_mesh.WarpMeshLidar`（S12）：MID-360 花样每帧 20,000 射线、10 Hz、逐点位姿 K = 11、
语义反射率（sem_table_v1）、32 个动态实体（逐射线时刻，ADR-198）、天气开（雾 + 雨的 905 nm 消光）、realistic 噪声档。三部分：

- `throughput`（D3-AC-12 吞吐条款）：N 架（缺省 100 与 400）分布在世界内 8 个演员热点上空环绕飞行（8 m/s，离地 4–40 m，每 4 架
  有 1 架叠加 90°/s 自旋），每 0.1 s tick 把全部 N 架的帧合成一次 `scan_batch(..., copy=False)`（扫描 + GPU 前缀和压缩 + 回传
  有效点到锁页缓冲区），演员状态经采集路径的同一入口 `capture.ActorClock`（整数 t_ns 键）取磁带时刻函数。计时 = 本 tick 的
  请求构造（N × K 位姿节点）至回传完成的墙钟（比条款的"扫描 + GPU 压缩 + 回传"多计请求构造，口径偏保守）；另报
  `last_timing` 分解（主机准备、启动、演员轨迹重建、等待 GPU、D2H、解包）。`--ticks` 个连续 tick 的 p95 为一次重复的结果，
  `--repeats` 次重复取中位（ADR-033）。阈值：100 架 p95 ≤ 30 ms（阻塞）、400 架 p95 ≤ 80 ms（应交付）。
- `diff`（D3-AC-12 几何一致性条款与 D3-AC-47 分布）：同一批"机 × 帧"（取自吞吐场景的不同机、不同 tick，含演员、天气，
  clean/realistic/harsh 三档）分别经 Open3D CPU 参考 `MeshLidar` 与 Warp 后端扫描，逐射线比较最近交点距离与命中判定（两者均为瓦片
  局部原点 float32 求交，随机键相同）。汇总全部射线的距离差分布（p50、p90、p99、p99.9、最大，静态与演员分列）、命中判定不一致率、
  演员实例不一致数与观测帧有效槽位差。阈值：距离差 p99 ≤ 1 mm、最大 ≤ 1 cm、命中不一致 ≤ 1e-4。另把"两侧命中不同静态三角形、较近
  三角形边界与射线的平面内距离在 float32 判定带宽内"的射线单列为轮廓边判定歧义（`edge_ambiguous`：深度台阶处 Embree 与 Warp 的边界
  容差不同），给出去掉歧义后的同一表面距离差分布与"命中不一致 + 歧义"比例；判定按 §11.2 原文口径，拟议口径（歧义计入命中不一致）
  单列且不计入，待 D01 以 ADR 冻结。
- `ate`（D3-AC-47 的"逐点位姿与单一位姿的 ATE 比值"）：同一解析航迹（缺省悬停 3 s 后加速到 8 m/s 绕环线一圈，叠加周期 4 s、
  角速度幅值 32°/s 的偏航摆动，即 r1/r4 的 v8 航段；60 s，航高 = 环线最高表面 + 8 m；环心取网格覆盖内结构丰富、对地航高最低处，
  见 `loop_center`）分别以逐点位姿（K = 11 个真值节点）与单一位姿
  （D2 语义：帧中点位姿，等同帧首位姿加 50 ms 速度外推）生成 MID-360 全槽位扫描，加同一份内置 IMU（250 Hz 真值位姿的运动学合成，
  `imu.synth_imu`），按 SLAM 导出合同（`SlamBagWriter`，无相机）写成两组 `slam_eval` 算例（clean、realistic 各一组）；宿主上以
  `slam_eval run --modes lio`（FAST-LIVO2，与 r4 同为 LIO 模式；可加 livo）评测后，`judge --slam-out` 汇总对齐 ATE 中位数与比值 =
  单一位姿 ATE / 逐点位姿 ATE。另报 r4 §5.2 的两个畸变量：A（逐点数据当作帧末单一位姿拼图时的点误差）与 B（单一位姿数据经 IMU
  去畸变后引入的点误差）。D3-AC-14 的"8 m/s 航段逐点相对单一位姿 ATE 改善 ≥ 5 倍"在正确尺度 town 航迹上由 SL3 判定，本节数字供 D01
  的 P-1 实测报告引用。

性能运行协议（ADR-033；ADR-195；docs/05 ADR-194 决策 (7)）：排他锁（flock `$AWR_PERF_LOCK`）、负载前置条件（1 分钟 loadavg ≤
`--load-max`）、`--repeats` 次重复取中位；吞吐计时窗口内以 `nvidia-smi pmon` 与 `--query-compute-apps` 采样被测卡（复用
`camera_throughput.GpuSampler`），按本进程 PID 区分外部进程判定独占级别：A 级计入，B 级须经用户确认后计入，shared 与 unknown 只作
开发参考，不计入封板。容器内运行须加 `--pid=host`。几何一致性与 ATE 不是性能度量，不受独占级别约束。

用法（dmax，d3-sensorsvc 镜像，单卡绑定，`--pid=host`）：
    python tools/bench/d3/lidar_concurrency.py run --world worlds/oryahovo-crop1 --out out/lidar-concurrency --gpu 2 \\
        [--parts throughput,diff,ate] [--fleets 100,400] [--ticks 300] [--repeats 3] [--diff-frames 96] [--ate-flight-s 60] [--no-lock]
    # 宿主（docker 与 d3-livo 镜像）：对 <out>/ate/cases/ 下的每个算例
    python3 tools/bench/d3/slam_eval run --case <out>/ate/cases/<算例> --out <slam-out>/<算例> --modes lio --repeats 3
    python tools/bench/d3/lidar_concurrency.py judge --out out/lidar-concurrency [--slam-out <slam-out>]
产物：`<out>/lidar_concurrency.json`（`drone4d.bench.lidar_concurrency.v1`），各部分另存 `<out>/<part>.json`（已存在且参数相同则复用，
断点续跑）。退出码：0 计入的条款全部通过（不计入的条款不影响退出码；`seal_ready` 另要求全部条款计入且通过）、1 有条款不通过或未测、
14 前置条件不满足（未取得锁或负载过高）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _p in (ROOT / "python", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import camera_throughput as ct  # noqa: E402  - 同目录：同卡负载采样、排他锁、负载前置条件与主机信息（ADR-033/195 同一实现）

SCHEMA = "drone4d.bench.lidar_concurrency.v1"
TICK_NS = 100_000_000
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14
PARTS = ("throughput", "diff", "ate")

# D3-AC-12（docs/05 §11.2 原文）
AC12_N100_P95_MS = 30.0                 # 100 架 p95（阻塞）
AC12_N400_P95_MS = 80.0                 # 400 架 p95（应交付目标）
AC12_D_P99_M = 1e-3                     # 逐射线距离差 p99
AC12_D_MAX_M = 1e-2                     # 逐射线距离差最大
AC12_HIT_MISMATCH = 1e-4                # 命中判定不一致率

N_ACTORS = 32
N_HOTSPOTS = 8
KINDS = (("person", (500, 300, 1750)), ("vehicle", (4500, 1800, 1500)), ("dog", (800, 300, 600)), ("object", (1200, 1200, 1000)))
AGL_M = (4.0, 8.0, 12.0, 20.0, 30.0, 40.0)
SPIN_DPS = 90.0
MOUNT_RPY = (180.0, 0.0, 0.0)          # p600_mid360_inv：倒装 MID-360


# ---------------------------------------------------------------------------------------------------------------- 统计
def dist(xs: Sequence[float], qs: Sequence[float] = (50, 95, 99)) -> dict:
    """均值、分位数（numpy 线性插值）与最大值；空序列只给 n = 0。"""
    a = np.asarray(xs, np.float64)
    if not len(a):
        return {"n": 0}
    out: dict[str, Any] = {"n": len(a), "mean": float(a.mean())}
    for q in qs:
        out[f"p{q:g}".replace(".", "_")] = float(np.percentile(a, q))
    out["max"] = float(a.max())
    return out


def _median(xs: Sequence[float | None]) -> float | None:
    v = [float(x) for x in xs if x is not None]
    return statistics.median(v) if v else None


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------------------------------------------- 世界几何
def world_extent(q) -> tuple[np.ndarray, np.ndarray]:
    """全部求交瓦片 AABB 的并（世界帧）。"""
    return np.min([t.lo for t in q.tiles], 0).astype(np.float64), np.max([t.hi for t in q.tiles], 0).astype(np.float64)


def top_z(q, xy: np.ndarray, miss: float | None = None) -> np.ndarray:
    """竖直向下求交得到的最高表面高度（网格 DSM）；未命中处（网格覆盖之外）取 `miss`，缺省为世界最低高度。"""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    z0 = float(q._zhi) + 50.0
    o = np.column_stack([xy, np.full(len(xy), z0)])
    d = np.tile([0.0, 0.0, -1.0], (len(xy), 1))
    t, _p, _n = q._cast(o, d, np.full(len(xy), z0 - float(q._zlo) + 100.0))
    return np.where(np.isfinite(t), z0 - t, float(q._zlo) if miss is None else float(miss))


def ground_z(q, xy: np.ndarray) -> np.ndarray:
    """地面高度：世界包的 DTM（`ground_dtm`）；没有 DSM/DTM 栅格的查询对象（构造场景）退回网格最高表面。"""
    try:
        return np.asarray(q.ground_dtm(np.asarray(xy, np.float64).reshape(-1, 2)), np.float64)
    except AttributeError:
        return top_z(q, xy)


def hotspots(q, n: int = N_HOTSPOTS, *, spacing_m: float = 25.0, radius_m: float = 15.0, min_sep_m: float | None = None) -> np.ndarray:
    """n 个演员热点 (n, 2)：在世界内缘 15% 以内的网格候选点中，按半径 `radius_m` 两圈 16 个样本的开阔度（最高表面与地面差 < 0.5 m 的比例）
    从高到低贪心选取，两两间距 ≥ `min_sep_m`（缺省为世界短边的 1/4）。结果只取决于世界几何（确定性）。"""
    lo, hi = world_extent(q)
    span = hi[:2] - lo[:2]
    a, b = lo[:2] + 0.15 * span, hi[:2] - 0.15 * span
    xs = np.arange(a[0], b[0] + 1e-9, spacing_m)
    ys = np.arange(a[1], b[1] + 1e-9, spacing_m)
    C = np.stack(np.meshgrid(xs, ys), -1).reshape(-1, 2)
    ang = np.linspace(0.0, 2 * math.pi, 8, endpoint=False)
    u = np.stack([np.cos(ang), np.sin(ang)], 1)
    ring = np.vstack([0.5 * radius_m * u, radius_m * u])                # 半径 r/2 与 r 两圈各 8 个样本
    S = (C[:, None, :] + ring[None]).reshape(-1, 2)
    open_ = (top_z(q, S) - ground_z(q, S) < 0.5).reshape(len(C), -1).mean(1)
    sep = float(min_sep_m) if min_sep_m is not None else 0.25 * float(min(span))
    order = np.lexsort((np.arange(len(C)), -open_))
    out: list[np.ndarray] = []
    for k in order:
        if all(np.linalg.norm(C[k] - c) >= sep for c in out):
            out.append(C[k])
            if len(out) == n:
                break
    while len(out) < n:                                         # 世界过小：退化为按序重复（仍确定）
        out.append(out[len(out) % max(len(out), 1)] if out else 0.5 * (lo[:2] + hi[:2]))
    return np.array(out, np.float64)


# ---------------------------------------------------------------------------------------------------------------- 演员磁带（32 个）
def actor_table(n: int = N_ACTORS) -> list[dict]:
    """磁带演员表：行人、车辆、狗、物体轮换（与相机代理同一几何，`camera.actors.proxy_mesh`）。"""
    out = []
    for i in range(n):
        cls, size = KINDS[i % 4]
        out.append({"idx": i, "id": f"a{i:02d}", "class": cls, "scripted": i < n // 2, "profile": "vehicle" if cls == "vehicle" else "person",
                    "size_mm": list(size), "appearance": {}})
    return out


def actor_frames(q, centers: np.ndarray, n_actors: int, n_ticks: int, *, seed: int = 16) -> np.ndarray:
    """(n_ticks, n_actors) 磁带记录：演员 a 绕热点 a mod H 运动（行人、狗、物体 0.6–1.6 m/s 的李萨如曲线，车辆 6–9 m/s 半径 15 m 的环线），
    高度贴地（`ground_z`），速度与航向取解析导数；a ≡ 3 (mod 7) 的演员前 1/4 时长不可见、a ≡ 5 (mod 9) 的演员后 1/4 时长离场（可见性阶跃）。"""
    from awr.sim.events import tape as tp

    rng = np.random.default_rng(seed)
    t = np.arange(n_ticks) * (TICK_NS / 1e9)
    f = np.zeros((n_ticks, n_actors), tp.RECORD)
    f["container"] = tp.NO_CONTAINER
    for a in range(n_actors):
        c = centers[a % len(centers)]
        ph = rng.uniform(0, 2 * math.pi)
        if KINDS[a % 4][0] == "vehicle":
            r, v = 15.0, rng.uniform(6.0, 9.0)
            w = v / r * (1 if a % 2 else -1)
            x, y = c[0] + r * np.cos(ph + w * t), c[1] + r * np.sin(ph + w * t)
            vx, vy = -r * w * np.sin(ph + w * t), r * w * np.cos(ph + w * t)
        else:
            ax, ay = rng.uniform(4.0, 12.0, 2)
            v = rng.uniform(0.6, 1.6)
            w = v / max(ax, ay)
            x, y = c[0] + ax * np.sin(w * t + ph), c[1] + ay * np.sin(2 * w * t + ph * 0.5)
            vx, vy = ax * w * np.cos(w * t + ph), 2 * ay * w * np.cos(2 * w * t + ph * 0.5)
        z = ground_z(q, np.column_stack([x, y]))
        f["x_mm"][:, a], f["y_mm"][:, a], f["z_mm"][:, a] = (np.round(u * 1000).astype(np.int64) for u in (x, y, z))
        f["vx_cmps"][:, a], f["vy_cmps"][:, a] = np.round(vx * 100).astype(np.int64), np.round(vy * 100).astype(np.int64)
        f["yaw_cdeg"][:, a] = np.round(np.degrees(np.arctan2(vy, vx)) * 100).astype(np.int64)
        f["state"][:, a] = 1
        if a % 7 == 3:
            f["state"][: n_ticks // 4, a] = 0
        if a % 9 == 5:
            f["state"][3 * n_ticks // 4:, a] = 0
    return f


def write_actor_tape(path: Path, q, centers: np.ndarray, *, n_actors: int = N_ACTORS, n_ticks: int = 600, seed: int = 16) -> Path:
    """写出合成事件磁带（`drone4d.tape.v1`，exogenous）：`actor_frames` 的记录 + `actor_table` 的演员表。"""
    from awr.sim.events import tape as tp

    actors = actor_table(n_actors)
    h64 = "0" * 64
    hd = {"schema": "drone4d.tape.v1", "event_id": "ev-lidar-concurrency", "event_version": 1, "event_sha256": h64, "mode": "exogenous",
          "seed": int(seed), "tick_hz": 10, "n_ticks": int(n_ticks), "duration_ms": int(n_ticks) * 100, "record": tp.record_spec(),
          "codec": {"name": "col-delta-zstd@1", "block_ticks": 100}, "actors": actors,
          "world": {"id": "bench", "content_version": "0" * 12, "raycast_sha256": h64, "navmesh_sha256": {"person": h64}, "anchors_sha256": h64},
          "environment": {}, "env_track": [], "comms": {}, "anim": None, "realized": None}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tp.write_tape(path, hd, actor_frames(q, centers, n_actors, n_ticks, seed=seed), n_scripted=n_actors // 2)
    return path


# ---------------------------------------------------------------------------------------------------------------- 机队航迹
def _rz(yaw: np.ndarray) -> np.ndarray:
    c, s = np.cos(yaw), np.sin(yaw)
    z, o = np.zeros_like(c), np.ones_like(c)
    return np.stack([np.stack([c, -s, z], -1), np.stack([s, c, z], -1), np.stack([z, z, o], -1)], -2)


class Fleet:
    """N 架环绕热点飞行：第 i 架绕热点 i mod H，半径 10–80 m（黄金分割序列铺开）、8 m/s、顺逆时针交替、相位错开；航高 = 环线上
    64 个样本的最高表面 + `AGL_M[i mod 6]`；航向沿切线，i ≡ 3 (mod 4) 另叠加 90°/s 自旋。位姿为解析函数（整数纳秒），帧内 K 个节点
    按同一函数取（t_s + 10 ms·m），倒装 MID-360。"""

    def __init__(self, q, centers: np.ndarray, n: int, *, speed_mps: float = 8.0) -> None:
        from awr.sim.sensors.frames import mount_R

        self.n = int(n)
        H = len(centers)
        i = np.arange(self.n)
        self.c = np.asarray(centers, np.float64)[i % H]
        self.r = 10.0 + 70.0 * np.mod((i // H) * 0.6180339887 + 0.17 * (i % H), 1.0)
        self.dir = np.where(i % 2 == 0, 1.0, -1.0)
        self.w = self.dir * float(speed_mps) / self.r
        self.ph = 2 * math.pi * np.mod(i * 0.3819660113, 1.0)
        self.spin = np.where(i % 4 == 3, math.radians(SPIN_DPS), 0.0)
        ang = np.linspace(0.0, 2 * math.pi, 64, endpoint=False)
        ring = (self.c[:, None, :] + self.r[:, None, None] * np.stack([np.cos(ang), np.sin(ang)], 1)[None]).reshape(-1, 2)
        self.alt = top_z(q, ring).reshape(self.n, -1).max(1) + np.array(AGL_M)[i % len(AGL_M)]
        self.R_mount = mount_R(MOUNT_RPY)
        self.speed = float(speed_mps)

    def nodes(self, t_start_ns: int, K: int = 11, frame_ns: int = TICK_NS) -> tuple[np.ndarray, np.ndarray]:
        """(R (n, K, 3, 3) WORLD←FLU, p (n, K, 3))：t_s + frame_ns·m/(K − 1) 时刻的位姿。"""
        t = (int(t_start_ns) + np.arange(K, dtype=np.int64) * (frame_ns // (K - 1))) / 1e9
        th = self.ph[:, None] + self.w[:, None] * t[None]
        p = np.stack([self.c[:, 0:1] + self.r[:, None] * np.cos(th), self.c[:, 1:2] + self.r[:, None] * np.sin(th),
                      np.repeat(self.alt[:, None], K, 1)], -1)
        yaw = th + self.dir[:, None] * (math.pi / 2) + self.spin[:, None] * t[None]
        return _rz(yaw) @ self.R_mount, p

    def reqs(self, tick: int, t_start_ns: int, *, K: int = 11, seed: int = 1, tier: str = "realistic", beta_905: float = 0.0,
             uavs: Sequence[int] | None = None) -> list:
        """第 tick 帧（frame_seq = tick）的批请求：每架 1 帧，uav_no = 机号。"""
        from awr.sensorsvc.lidar.warp_mesh import LidarFrameReq

        R, p = self.nodes(t_start_ns, K)
        ids = range(self.n) if uavs is None else uavs
        return [LidarFrameReq(int(tick), int(t_start_ns), list(zip(R[u], p[u], strict=True)), seed=int(seed), uav_no=int(u), tier=tier,
                              beta_905=float(beta_905)) for u in ids]


# ---------------------------------------------------------------------------------------------------------------- 场景
class Scene:
    """世界、演员磁带、热点与天气；`clock()` 返回采集路径同一入口 `ActorClock`（磁带偏移 `phase_ns`）。"""

    def __init__(self, q, tape_path: Path, centers: np.ndarray, *, visibility_m: float | None = 500.0, rain_mmh: float = 10.0,
                 tier: str = "realistic", phase_ns: int = 0, t0_ns: int = 2 * 10**9) -> None:
        from awr.sensorsvc.lidar.warp_mesh import beta_905_per_m
        from awr.sim.events.tape import TapeReader

        self.q = q
        self.tape = TapeReader(tape_path)
        self.actors = [dict(a) for a in self.tape.header["actors"]]
        self.centers = np.asarray(centers, np.float64)
        self.visibility_m, self.rain_mmh = visibility_m, float(rain_mmh)
        self.beta = beta_905_per_m(visibility_m, rain_mmh)
        self.tier = tier
        self.phase_ns = int(phase_ns)
        self.t0_ns = int(t0_ns)

    def clock(self):
        from awr.sensorsvc.capture import ActorClock

        return ActorClock(self.tape, self.phase_ns)

    def t_start(self, tick: int) -> int:
        return self.t0_ns + int(tick) * TICK_NS

    def describe(self) -> dict:
        return {"actors": len(self.actors), "hotspots": self.centers.round(3).tolist(), "visibility_m": self.visibility_m, "rain_mmh": self.rain_mmh,
                "beta_905_per_m": self.beta, "tier": self.tier, "tape_phase_ns": self.phase_ns, "t0_ns": self.t0_ns,
                "tape_ticks": int(self.tape.n_ticks)}


def open_scene(world: Path, cache: Path, *, n_ticks: int, visibility_m: float | None, rain_mmh: float, phase_ns: int,
               tape: Path | None = None) -> Scene:
    from awr.world.mesh.query import open_mesh_world_query

    q = open_mesh_world_query(Path(world), Path(cache))
    centers = hotspots(q)
    tp = tape or write_actor_tape(Path(cache) / "tape" / "tape.bin", q, centers, n_ticks=n_ticks)
    return Scene(q, tp, centers, visibility_m=visibility_m, rain_mmh=rain_mmh, phase_ns=phase_ns)


# ---------------------------------------------------------------------------------------------------------------- 吞吐
TIMING_KEYS = ("prep_ms", "launch_ms", "track_ms", "gpu_wait_ms", "d2h_ms", "unpack_ms", "total_ms")


def measure_throughput(lid, sc: Scene, n: int, *, ticks: int, warmup: int, seed: int = 1, tick0: int = 0,
                       on_measure: Callable[[str], None] | None = None) -> dict:
    """N 架连续 `warmup + ticks` 个 tick，每 tick 一次 `scan_batch(copy=False)`；只统计后 `ticks` 个。"""
    fleet = Fleet(sc.q, sc.centers, n)
    clock = sc.clock()
    wall, tim, pts, act, seg, arays = [], [], [], [], [], []
    for k in range(warmup + ticks):
        if k == warmup and on_measure is not None:
            on_measure("measure")
        tick = tick0 + k
        a = time.perf_counter()
        reqs = fleet.reqs(tick, sc.t_start(tick), seed=seed, tier=sc.tier, beta_905=sc.beta)
        scans = lid.scan_batch(reqs, actor_states=clock, copy=False)
        b = time.perf_counter()
        if k < warmup:
            continue
        wall.append((b - a) * 1e3)
        lt = dict(lid.last_timing)
        tim.append(lt)
        pts.append(lt["points"] / n)
        act.append(lt["actors"])
        seg.append(lt["segments"])
        arays.append(sum(s.actor_rays for s in scans))
    if on_measure is not None:
        on_measure("drain")
    return {"n": int(n), "ticks": int(ticks), "warmup": int(warmup), "tick_ms": dist(wall), "scan_total_ms": dist([t["total_ms"] for t in tim]),
            "breakdown_ms_median": {k: float(np.median([t[k] for t in tim])) for k in TIMING_KEYS},
            "breakdown_ms_p95": {k: float(np.percentile([t[k] for t in tim], 95)) for k in TIMING_KEYS},
            "points_per_frame_mean": float(np.mean(pts)), "actor_candidates": dist(act), "actor_segments": dist(seg),
            "actor_rays_per_tick_mean": float(np.mean(arays)), "actor_clock": {"calls": clock.calls, "hits": clock.hits},
            "rt_factor": float(100.0 / np.percentile(wall, 95))}


def run_throughput(sc: Scene, fleets: Sequence[int], *, ticks: int, warmup: int, repeats: int, gpu: tuple, device: str | None = None) -> dict:
    """按 `fleets` × `repeats` 测量；每次测量窗口单独采样同卡负载并判定独占级别。"""
    from awr.sensorsvc.lidar.model import load_lidar_model
    from awr.sensorsvc.lidar.warp_mesh import WarpMeshLidar

    used0 = ct.device_used_mib(gpu[0])
    lid = WarpMeshLidar(sc.q, load_lidar_model(), sc.actors, device=device)
    lid.scan_batch(Fleet(sc.q, sc.centers, 4).reqs(0, sc.t_start(0), tier=sc.tier, beta_905=sc.beta), actor_states=sc.clock())  # 编译内核
    out: dict[str, Any] = {"versions": lid.versions(), "build_ms": lid.build_ms, "fleets": {}}
    own = {os.getpid()}
    for n in fleets:
        reps = []
        for r in range(repeats):
            sampler = ct.GpuSampler(gpu[0], gpu[1]).start()
            sampler.tag("load")
            t_start = _utc()
            try:
                m = measure_throughput(lid, sc, n, ticks=ticks, warmup=warmup, tick0=r * (ticks + warmup), on_measure=sampler.tag)
            except (RuntimeError, MemoryError) as e:                 # 共享卡显存不足等：记录本机队规模未测，继续其余规模
                sys.stderr.write(f"[throughput] n={n} rep={r} failed: {e}\n")
                out["fleets"][str(n)] = {"repeats": reps, "error": f"{type(e).__name__}: {e}"[:500], "tick_p95_ms_median": None,
                                         "tick_p50_ms_median": None, "scan_total_p95_ms_median": None, "levels": []}
                break
            finally:
                sampler.stop()
            used = ct.device_used_mib(gpu[0])
            need = max((used or 0.0) - (used0 or 0.0), 0.0)
            reps.append({**m, "repeat": r, "utc_start": t_start, "utc_end": _utc(), "gpu_load": sampler.summary(own, need),
                         "vram_delta_mib": None if used is None or used0 is None else round(used - used0, 1)})
            sys.stderr.write(f"[throughput] n={n} rep={r} p95={m['tick_ms']['p95']:.2f} ms p50={m['tick_ms']['p50']:.2f} ms "
                             f"level={reps[-1]['gpu_load']['level']}\n")
        if "error" in out["fleets"].get(str(n), {}):
            continue
        p95 = [x["tick_ms"]["p95"] for x in reps]
        out["fleets"][str(n)] = {"repeats": reps, "tick_p95_ms_median": _median(p95), "tick_p50_ms_median": _median([x["tick_ms"]["p50"] for x in reps]),
                                 "scan_total_p95_ms_median": _median([x["scan_total_ms"]["p95"] for x in reps]),
                                 "levels": [x["gpu_load"]["level"] for x in reps]}
    return out


# ---------------------------------------------------------------------------------------------------------------- GPU/CPU 逐射线对拍
def cpu_rays(lid, req, actor_states=None):
    """CPU 参考扫描一帧并取逐射线最近交点（静态求交与演员求交之后的 t、inst）：在实例上临时包装 `_cast` 与 `_actors`，不改动参考实现。
    只记录本帧第一次静态求交（全部槽位、量程 r_hard）；其后的求交（如 `quality` 的 void 判定按无限量程重投）不覆盖记录。
    返回 (LidarScan, t (N,) 未命中 inf, inst (N,), rays)；rays 为求交射线 {"o", "d"（世界帧 float64，含角度噪声）, "prim"（全局三角形号，
    演员 −2，未命中 −1；与 Warp 后端 `last_rays["prim"]` 同一编码）}。"""
    q = lid.q
    rec: dict = {}
    cast0, act0 = q._cast, lid._actors

    def cast(o, d, tmax):
        r = cast0(o, d, tmax)
        if "t" not in rec:
            rec["t"], rec["inst"] = np.asarray(r[0], np.float64).copy(), np.zeros(len(o), np.int64)
            rec["o"], rec["d"] = np.asarray(o, np.float64).copy(), np.asarray(d, np.float64).copy()
            rec["prim"] = np.where(np.isfinite(rec["t"]), np.asarray(r[1]).astype(np.int64), -1)
        return r

    def actors(o, d, t, nrm, sem, inst, *a):
        k = act0(o, d, t, nrm, sem, inst, *a)
        rec["t"], rec["inst"] = np.asarray(t, np.float64).copy(), np.asarray(inst).copy()
        rec["prim"] = np.where(rec["inst"] > 0, -2, rec["prim"])
        return k

    q._cast, lid._actors = cast, actors
    try:
        s = lid.scan(req.frame_seq, req.t_start_ns, list(req.nodes), actor_states=actor_states, seed=req.seed, uav_no=req.uav_no,
                     sensor_no=req.sensor_no, tier=req.tier, beta_905=req.beta_905)
    finally:
        del q._cast, lid._actors
    return s, rec["t"], rec["inst"], {"o": rec["o"], "d": rec["d"], "prim": rec["prim"]}


# 轮廓边判定歧义：两侧命中不同的静态三角形且较近交点所在三角形的边界与射线的平面内距离不超过 float32 判定带宽时，属于"较近三角形
# 的边缘上是否命中"的 float32 歧义（Embree 与 Warp 的边界容差不同，二者都不错），而不是求交错误。带宽取瓦片局部坐标量级的
# EDGE_EPS_REL 倍（2^-18，约 32 个 float32 ulp；瓦片局部坐标 ≤ 500 m 时 ≤ 1.9 mm）。
EDGE_EPS_REL = 2.0 ** -18
EDGE_CHECK_M = 1e-4                     # 只核查距离差超过此值的不同三角形射线（同一三角形的平面重算差在 1e-11 m 量级）


def tri_world(q, prim: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """全局三角形号 → (三角形顶点 (n, 3, 3) 瓦片局部 float32 按值转 float64 后加瓦片原点，所在瓦片原点 (n, 3))。"""
    prim = np.asarray(prim, np.int64)
    P = np.zeros((len(prim), 3, 3))
    org = np.zeros((len(prim), 3))
    for tl in q.tiles:
        m = (prim >= tl.t0) & (prim < tl.t0 + tl.nt)
        if m.any():
            P[m] = tl.V[tl.F[prim[m] - tl.t0].astype(np.int64)].astype(np.float64) + tl.origin
            org[m] = tl.origin
    return P, org


def edge_margin(P: np.ndarray, o: np.ndarray, d: np.ndarray) -> np.ndarray:
    """射线与三角形平面交点到三角形边界的平面内带符号距离（m，内正外负；三条边所在直线距离的最小值）；与平面平行时为 −inf。"""
    n = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
    den = np.einsum("ij,ij->i", n, d)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.einsum("ij,ij->i", n, P[:, 0] - o) / den
    x = o + d * t[:, None]
    nu = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-300)
    out = np.full(len(P), np.inf)
    for a, b in ((0, 1), (1, 2), (2, 0)):
        e = P[:, b] - P[:, a]
        w = np.cross(nu, e)                                           # 平面内指向三角形内侧的边法向（顶点绕 nu 逆时针）
        w /= np.maximum(np.linalg.norm(w, axis=1, keepdims=True), 1e-300)
        out = np.minimum(out, np.einsum("ij,ij->i", x - P[:, a], w))
    return np.where(np.isfinite(t) & (np.abs(den) > 0), out, -np.inf)


def edge_ambiguous(q, o: np.ndarray, d: np.ndarray, t_cpu: np.ndarray, prim_cpu: np.ndarray, t_gpu: np.ndarray,
                   prim_gpu: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """逐射线轮廓边判定歧义：双方都命中静态三角形、三角形不同、距离差 > `EDGE_CHECK_M`，且较近一侧三角形的边界与射线的平面内距离
    ≤ 带宽（瓦片局部坐标量级 × `EDGE_EPS_REL`）。返回 (歧义掩码 (N,)，被核查射线的平面内距离 (N,)，未核查为 NaN)。"""
    tc, tg = np.asarray(t_cpu, np.float64), np.asarray(t_gpu, np.float64)
    pc, pg = np.asarray(prim_cpu, np.int64), np.asarray(prim_gpu, np.int64)
    both = np.isfinite(tc) & (tg >= 0) & (pc >= 0) & (pg >= 0)
    cand = np.flatnonzero(both & (pc != pg) & (np.abs(np.where(both, tc - tg, 0.0)) > EDGE_CHECK_M))
    amb = np.zeros(len(tc), bool)
    gap = np.full(len(tc), np.nan)
    if cand.size:
        near = np.where(tg[cand] < tc[cand], pg[cand], pc[cand])
        P, org = tri_world(q, near)
        oo, dd = np.asarray(o, np.float64)[cand], np.asarray(d, np.float64)[cand]
        g = edge_margin(P, oo, dd)
        scale = np.maximum(np.abs(P - org[:, None]).max(axis=(1, 2)), np.abs(oo - org).max(1))
        gap[cand] = g
        amb[cand] = np.abs(g) <= EDGE_EPS_REL * scale
    return amb, gap


class RayDiff:
    """逐射线对拍累加器：CPU 的 t（未命中 inf）与 GPU 的 t（未命中 −1），汇总全部射线的距离差分布与命中、实例不一致。"""

    def __init__(self) -> None:
        self.d_static: list[np.ndarray] = []
        self.d_actor: list[np.ndarray] = []
        self.rays = self.hit_cpu = self.hit_gpu = self.hit_mismatch = self.inst_mismatch = self.frames = 0
        self.slot_sym_diff: list[int] = []
        self.worst: list[dict] = []
        self.d_edge: list[np.ndarray] = []
        self.edge_gap: list[np.ndarray] = []
        self.prim_mismatch = 0

    def add(self, t_cpu, inst_cpu, t_gpu, inst_gpu, *, slots_cpu=None, slots_gpu=None, prim_cpu=None, prim_gpu=None, edge=None,
            edge_gap=None, tag: dict | None = None) -> None:
        """`edge`（(N,) bool，`edge_ambiguous`）给出时，这些射线的距离差另列为轮廓边判定歧义，不进入同一表面的距离差分布。"""
        tc, tg = np.asarray(t_cpu, np.float64), np.asarray(t_gpu, np.float64)
        ic, ig = np.asarray(inst_cpu, np.int64), np.asarray(inst_gpu, np.int64)
        hc, hg = np.isfinite(tc), tg >= 0
        both = hc & hg
        d = np.abs(tc[both] - tg[both])
        on_actor = (ic[both] > 0) | (ig[both] > 0)
        eb = np.zeros(len(d), bool) if edge is None else np.asarray(edge, bool)[both]
        self.d_static.append(d[~on_actor & ~eb])
        self.d_actor.append(d[on_actor & ~eb])
        self.d_edge.append(d[eb])
        if edge_gap is not None:
            self.edge_gap.append(np.asarray(edge_gap, np.float64)[np.asarray(edge, bool)])
        if prim_cpu is not None and prim_gpu is not None:
            pc, pg = np.asarray(prim_cpu, np.int64)[both], np.asarray(prim_gpu, np.int64)[both]
            self.prim_mismatch += int(((pc >= 0) & (pg >= 0) & (pc != pg)).sum())
        self.rays += len(tc)
        self.frames += 1
        self.hit_cpu += int(hc.sum())
        self.hit_gpu += int(hg.sum())
        mm = int((hc != hg).sum())
        self.hit_mismatch += mm
        self.inst_mismatch += int((ic[both] != ig[both]).sum())
        if slots_cpu is not None and slots_gpu is not None:
            a, b = np.asarray(slots_cpu, np.int64), np.asarray(slots_gpu, np.int64)
            self.slot_sym_diff.append(int(len(a) + len(b) - 2 * len(np.intersect1d(a, b))))
        if d.size:
            self.worst.append({**(tag or {}), "d_max_m": float(d.max()), "edge_ambiguous": int(eb.sum()), "hit_mismatch": mm})

    def summary(self) -> dict:
        """`d_m` 为全部双方命中射线（§11.2 原文口径）；`d_same_surface_m` 去掉轮廓边判定歧义射线；`edge` 为歧义射线的计数、比例与
        距离差；`hit_or_edge_mismatch_rate` = (命中不一致 + 轮廓边歧义) / 射线数（拟议口径，待 D01 以 ADR 冻结）。"""
        cat = lambda xs: np.concatenate(xs) if xs else np.zeros(0)  # noqa: E731
        ds, da, de = cat(self.d_static), cat(self.d_actor), cat(self.d_edge)
        same = np.concatenate([ds, da])
        dd = np.concatenate([same, de])
        qs = (50, 90, 99, 99.9)
        n_edge = len(de)
        gap = cat(self.edge_gap)
        return {"frames": self.frames, "rays": self.rays, "hit_cpu": self.hit_cpu, "hit_gpu": self.hit_gpu, "both_hit": len(dd),
                "hit_mismatch": self.hit_mismatch, "hit_mismatch_rate": self.hit_mismatch / max(self.rays, 1),
                "inst_mismatch": self.inst_mismatch, "prim_mismatch": self.prim_mismatch, "d_m": dist(dd, qs), "d_same_surface_m": dist(same, qs),
                "d_static_m": dist(ds, qs), "d_actor_m": dist(da, qs),
                "edge": {"rays": n_edge, "rate": n_edge / max(self.rays, 1), "d_m": dist(de, (50,)),
                         "gap_abs_max_m": float(np.abs(gap).max()) if gap.size else None, "eps_rel": EDGE_EPS_REL},
                "hit_or_edge_mismatch_rate": (self.hit_mismatch + n_edge) / max(self.rays, 1),
                "slot_sym_diff": {"sum": int(sum(self.slot_sym_diff)), "max": int(max(self.slot_sym_diff, default=0))},
                "worst_frames": sorted(self.worst, key=lambda w: -w["d_max_m"])[:5]}


def diff_frames(sc: Scene, n_frames: int, *, fleet_n: int = 100, seed: int = 7) -> list:
    """对拍帧集合：吞吐场景（fleet_n 架）中按确定性抽样的 (机, tick)，档位 clean/realistic/harsh 按 1:4:1 轮换，2/3 的帧开天气。"""
    rng = np.random.default_rng(seed)
    fleet = Fleet(sc.q, sc.centers, fleet_n)
    tiers = ("realistic", "clean", "realistic", "harsh", "realistic", "realistic")
    out = []
    for k in range(n_frames):
        u = int(rng.integers(0, fleet_n))
        tick = int(rng.integers(0, max(int(sc.tape.n_ticks) - 40, 1)))
        beta = sc.beta if k % 3 else 0.0
        out.extend(fleet.reqs(tick, sc.t_start(tick), seed=seed + k % 3, tier=tiers[k % len(tiers)], beta_905=beta, uavs=[u]))
    return out


def run_diff(sc: Scene, reqs: Sequence, *, device: str | None = None, batch: int = 32, actors: bool = True) -> dict:
    """同一批请求分别经 CPU 参考与 Warp 后端扫描，逐射线对拍（D3-AC-12：p99 ≤ 1 mm、最大 ≤ 1 cm、命中不一致 ≤ 1e-4）。"""
    from awr.sensorsvc.lidar.mesh import MeshLidar
    from awr.sensorsvc.lidar.model import load_lidar_model
    from awr.sensorsvc.lidar.warp_mesh import WarpMeshLidar

    m = load_lidar_model()
    N = m.slots_per_frame
    clid = MeshLidar(sc.q, m, sc.actors)
    glid = WarpMeshLidar(sc.q, m, sc.actors, device=device)
    clock = sc.clock() if actors else None
    acc = RayDiff()
    t_cpu = t_gpu = 0.0
    actor_rays = 0
    for b0 in range(0, len(reqs), batch):
        rq = list(reqs[b0:b0 + batch])
        a = time.perf_counter()
        gs = glid.scan_batch(rq, actor_states=clock, rays=True)
        t_gpu += time.perf_counter() - a
        R = glid.last_rays
        for f, r in enumerate(rq):
            a = time.perf_counter()
            cs, tc, ic, cr = cpu_rays(clid, r, clock)
            t_cpu += time.perf_counter() - a
            sl = slice(f * N, (f + 1) * N)
            tg, pg = R["t"][sl], R["prim"][sl]
            amb, gap = edge_ambiguous(sc.q, cr["o"], cr["d"], tc, cr["prim"], tg, pg)
            acc.add(tc, ic, tg, R["inst"][sl], slots_cpu=cs.points["offset_time"] // m.point_dt_ns,
                    slots_gpu=gs[f].points["offset_time"] // m.point_dt_ns, prim_cpu=cr["prim"], prim_gpu=pg, edge=amb, edge_gap=gap,
                    tag={"frame_seq": r.frame_seq, "uav_no": r.uav_no, "tier": r.tier, "beta_905": r.beta_905})
            actor_rays += gs[f].actor_rays
        sys.stderr.write(f"[diff] {min(b0 + batch, len(reqs))}/{len(reqs)} frames\n")
    s = acc.summary()
    return {**s, "actor_rays_gpu": int(actor_rays), "cpu_s_per_frame": t_cpu / max(len(reqs), 1), "gpu_s_per_frame": t_gpu / max(len(reqs), 1),
            "versions": glid.versions(), "thresholds": {"d_p99_m": AC12_D_P99_M, "d_max_m": AC12_D_MAX_M, "hit_mismatch_rate": AC12_HIT_MISMATCH}}


# ---------------------------------------------------------------------------------------------------------------- 逐点与单一位姿的 ATE 比值（FAST-LIVO2）
ATE_VARIANTS = ("per_point", "single_pose")
ATE_IMU_HZ = 250                        # 真值位姿采样（物理步 4 ms），IMU 由其运动学合成
ATE_T_BASE_NS = 100 * 10**9            # bag 时间原点（ROS 时间 0 有特殊含义，取 100 s；仍在 200 Hz 与 10 Hz 绝对网格上）
ATE_MIN_RETURNS = 1000                  # 每帧真实回波（tag 0）均值下限：低于此值说明航迹不在网格覆盖内，算例无意义


def _quat(R: np.ndarray) -> np.ndarray:
    from awr.sensorsvc.poses import R_to_quat_xyzw

    return np.asarray(R_to_quat_xyzw(np.asarray(R, np.float64)), np.float64)


def slot_poses(nodes: Sequence[tuple[np.ndarray, np.ndarray]], slots: np.ndarray, model) -> tuple[np.ndarray, np.ndarray]:
    """槽位时刻的 (R (n, 3, 3), o (n, 3))：与 `MeshLidar.scan` 同式（相邻节点位置线性、四元数同半球 nlerp）。"""
    from awr.sensorsvc.lidar.mesh import _quat_R

    K = len(nodes)
    t_rel = np.asarray(slots, np.int64) * model.point_dt_ns
    seg_ns = model.frame_ns // (K - 1)
    seg = np.minimum(t_rel // seg_ns, K - 2)
    u = (t_rel - seg * seg_ns) / seg_ns
    P = np.array([p for _, p in nodes], np.float64)
    Q = np.array([_quat(R) for R, _ in nodes])
    for k in range(1, K):
        if np.dot(Q[k], Q[k - 1]) < 0:
            Q[k] = -Q[k]
    o = P[seg] * (1 - u)[:, None] + P[seg + 1] * u[:, None]
    qq = Q[seg] * (1 - u)[:, None] + Q[seg + 1] * u[:, None]
    qq /= np.linalg.norm(qq, axis=1, keepdims=True)
    return _quat_R(qq), o


LOOP_GRID = 15                          # 环心候选网格（每轴）
LOOP_STRUCT_H_M = 2.5                   # 结构判定：最高表面高出地面（DTM）超过此值
LOOP_STRUCT_FRAC = 0.9                  # 结构丰富：环线圆盘（半径 R + 20 m）内结构样本比例下限


def loop_center(q, R: float, agl_m: float) -> tuple[np.ndarray, dict]:
    """ATE 环心：世界内 `LOOP_GRID`² 个网格候选点（距世界边缘 R + 10 m）中，环线 96 个样本与半径 R + 20 m 圆盘 432 个样本都落在网格
    覆盖内（竖直向下有交点；瓦片 AABB 的并可能含无网格的角区）者参选：结构样本比例 ≥ `LOOP_STRUCT_FRAC` 的候选取对地航高（环线最高
    表面 + agl − 圆盘地面中位数）最低者（LIO 需要结构约束，航高越低点越密）；没有结构丰富的候选（如没有 DTM 的构造世界）时取环线
    最高表面最低者；没有全覆盖的候选时退回全部候选。返回 (环心 (2,)，选取信息)。"""
    lo, hi = world_extent(q)
    m = R + 10.0
    xs = np.linspace(lo[0] + m, hi[0] - m, LOOP_GRID) if hi[0] - lo[0] > 2 * m else np.array([0.5 * (lo[0] + hi[0])])
    ys = np.linspace(lo[1] + m, hi[1] - m, LOOP_GRID) if hi[1] - lo[1] > 2 * m else np.array([0.5 * (lo[1] + hi[1])])
    C = np.stack(np.meshgrid(xs, ys), -1).reshape(-1, 2)
    ang = np.linspace(0.0, 2 * math.pi, 96, endpoint=False)
    ring = np.stack([np.cos(ang), np.sin(ang)], 1) * R
    rr, aa = np.meshgrid(np.linspace(0.0, R + 20.0, 12), np.linspace(0.0, 2 * math.pi, 36, endpoint=False))
    disk = np.stack([rr.ravel() * np.cos(aa.ravel()), rr.ravel() * np.sin(aa.ravel())], 1)
    nr = len(ring)
    S = (C[:, None] + np.vstack([ring, disk])[None]).reshape(-1, 2)
    top = top_z(q, S, miss=np.nan).reshape(len(C), -1)
    ground = ground_z(q, S).reshape(len(C), -1)[:, nr:]
    full = np.isfinite(top).all(1) & np.isfinite(ground).all(1)
    ring_top = np.nan_to_num(top[:, :nr], nan=float(q._zlo)).max(1)
    with np.errstate(invalid="ignore"):
        frac = np.where(full, (np.nan_to_num(top[:, nr:] - ground, nan=0.0) > LOOP_STRUCT_H_M).mean(1), 0.0)
        agl_ground = ring_top + agl_m - np.where(full, np.median(np.nan_to_num(ground, nan=0.0), 1), 0.0)
    rich = full & (frac >= LOOP_STRUCT_FRAC)
    if rich.any():
        k, rule = int(np.argmin(np.where(rich, agl_ground, np.inf))), "structure_lowest_agl"
    elif full.any():
        k, rule = int(np.argmin(np.where(full, ring_top, np.inf))), "covered_lowest_top"
    else:
        k, rule = int(np.argmin(ring_top)), "lowest_top"
    return C[k], {"rule": rule, "candidates": len(C), "covered": int(full.sum()), "structure_rich": int(rich.sum()),
                  "structure_frac": float(frac[k]), "alt_over_ground_m": float(agl_ground[k])}


class LoopTrack:
    """ATE 航迹：LiDAR 传感器系（倒装 MID-360，FLU）位姿的解析函数（整数纳秒，C² 连续）。

    悬停 `hover_s`（速度 0，满足 rig 空中出生的悬停保持窗口），`ramp_s` 内以 (1 − cos) 速度剖面加速到 `speed_mps`，之后匀速绕环线
    （半径使悬停后的航程约为一圈）；航向沿切线，叠加周期 4 s 的正弦偏航摆动（角速度幅值 `yaw_amp_dps`，随加速段平滑引入）；
    航高 = 环线最高表面 + `agl_m`。环心缺省由 `loop_center` 选取（网格覆盖内、结构丰富、对地航高最低；确定性）。"""

    def __init__(self, q, *, speed_mps: float = 8.0, yaw_amp_dps: float = 32.0, agl_m: float = 8.0, flight_s: float = 60.0,
                 hover_s: float = 3.0, ramp_s: float = 3.0, center=None, t_base_ns: int = ATE_T_BASE_NS) -> None:
        from awr.sim.sensors.frames import mount_R

        self.V, self.hover, self.ramp, self.flight = float(speed_mps), float(hover_s), float(ramp_s), float(flight_s)
        self.R = max(self.V * (self.flight - self.hover - 0.5 * self.ramp) / (2 * math.pi), 5.0)
        self.w_osc = 2 * math.pi / 4.0
        self.A_yaw = math.radians(yaw_amp_dps) / self.w_osc
        self.t_base = int(t_base_ns)
        ang = np.linspace(0.0, 2 * math.pi, 96, endpoint=False)
        ring = np.stack([np.cos(ang), np.sin(ang)], 1) * self.R
        self.site: dict[str, Any] = {"rule": "given"}
        if center is None:
            center, self.site = loop_center(q, self.R, float(agl_m))
        self.c = np.asarray(center, np.float64)
        self.alt = float(top_z(q, self.c[None] + ring).max()) + float(agl_m)
        self.th0 = 0.0
        self.R_mount = mount_R(MOUNT_RPY)

    def _s(self, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(航程 s，摆动包络 g)，t 为相对 t_base 的秒。"""
        u = np.clip(t - self.hover, 0.0, None)
        tau = np.clip(u / self.ramp, 0.0, 1.0)
        s_ramp = 0.5 * self.V * (np.minimum(u, self.ramp) - self.ramp / math.pi * np.sin(math.pi * tau))
        s = s_ramp + self.V * np.clip(u - self.ramp, 0.0, None)
        return s, 0.5 * (1 - np.cos(math.pi * tau))

    def pose(self, t_ns) -> tuple[np.ndarray, np.ndarray]:
        """(R_WL (n, 3, 3), p_WL (n, 3))。"""
        t = (np.atleast_1d(np.asarray(t_ns, np.int64)) - self.t_base) / 1e9
        s, g = self._s(t)
        th = self.th0 + s / self.R
        p = np.stack([self.c[0] + self.R * np.cos(th), self.c[1] + self.R * np.sin(th), np.full_like(th, self.alt)], 1)
        yaw = th + math.pi / 2 + self.A_yaw * g * np.sin(self.w_osc * np.clip(t - self.hover, 0.0, None))
        return _rz(yaw) @ self.R_mount, p

    def nodes(self, t_start_ns: int, K: int = 11) -> list[tuple[np.ndarray, np.ndarray]]:
        R, p = self.pose(int(t_start_ns) + np.arange(K, dtype=np.int64) * (TICK_NS // (K - 1)))
        return list(zip(R, p, strict=True))

    def single(self, t_start_ns: int, K: int = 11) -> list[tuple[np.ndarray, np.ndarray]]:
        """单一位姿（D2 语义）：帧中点位姿（帧首位姿加 50 ms 速度外推），K 个相同节点。"""
        R, p = self.pose(int(t_start_ns) + TICK_NS // 2)
        return [(R[0], p[0])] * K

    def describe(self) -> dict:
        t = np.arange(self.t_base, self.t_base + int(self.flight * 1e9), 4_000_000)
        R, _p = self.pose(t)
        yaw = np.unwrap(np.arctan2(R[:, 1, 0], R[:, 0, 0]))
        return {"speed_mps": self.V, "radius_m": self.R, "center": self.c.round(3).tolist(), "site": self.site, "alt_m": self.alt, "hover_s": self.hover,
                "ramp_s": self.ramp, "flight_s": self.flight, "yaw_osc_amp_dps": math.degrees(self.A_yaw * self.w_osc),
                "yaw_rate_max_dps": float(np.degrees(np.abs(np.diff(yaw)).max() / 0.004))}


def _scan_many(lid, reqs: Sequence, batch: int = 100) -> list:
    if hasattr(lid, "scan_batch"):
        out = []
        for b0 in range(0, len(reqs), batch):
            out += lid.scan_batch(list(reqs[b0:b0 + batch]))
        return out
    return [lid.scan(r.frame_seq, r.t_start_ns, list(r.nodes), seed=r.seed, uav_no=r.uav_no, sensor_no=r.sensor_no, tier=r.tier,
                     beta_905=r.beta_905) for r in reqs]


def distortion_ab(track: LoopTrack, model, per_scans: Sequence, single_scans: Sequence, frames: Sequence[int]) -> dict:
    """r4 §5.2 的两个畸变量（m）：A = 逐点数据当作帧末单一位姿拼图时的点误差；B = 单一位姿数据经 IMU（真值轨迹）逐点去畸变后引入的点误差。"""
    A, B = [], []
    for f in frames:
        for var, s in (("A", per_scans[f]), ("B", single_scans[f])):
            p = s.points[s.points["tag"] == 0]
            if not len(p):
                continue
            xyz = np.stack([p["x"], p["y"], p["z"]], 1).astype(np.float64)
            Ri, oi = slot_poses(track.nodes(s.t_start_ns), (p["offset_time"] // model.point_dt_ns).astype(np.int64), model)
            Xw = np.einsum("nij,nj->ni", Ri, xyz) + oi                   # 按点时刻真值位姿去畸变
            if var == "A":
                R_e, o_e = track.nodes(s.t_start_ns)[-1]
                A.append(np.linalg.norm(xyz @ R_e.T + o_e - Xw, axis=1))
            else:
                R_m, o_m = track.single(s.t_start_ns)[0]
                B.append(np.linalg.norm(Xw - (xyz @ R_m.T + o_m), axis=1))
    return {"A_m": dist(np.concatenate(A) if A else [], (50, 95)), "B_m": dist(np.concatenate(B) if B else [], (50, 95))}


def build_ate_cases(q, out_dir: Path, *, tiers: Sequence[str] = ("clean", "realistic"), speed_mps: float = 8.0, yaw_amp_dps: float = 32.0,
                    agl_m: float = 8.0, flight_s: float = 60.0, hover_s: float = 3.0, ramp_s: float = 3.0, seed: int = 3,
                    backend: str = "auto", device: str | None = None, world: str | None = None, vehicle: str = "p600_mid360_inv",
                    center: Sequence[float] | None = None) -> dict:
    """逐点位姿与单一位姿两组 SLAM 算例（`drone4d.bench.slam_case.v1`，LIO/LIVO 输入：LiDAR CustomMsg 全槽位 + 内置 IMU 200 Hz，无相机）：
    同一解析航迹、同一噪声键，只有 LiDAR 求交的位姿节点不同。IMU 由 250 Hz 真值位姿的运动学合成（`imu.synth_imu`，IMU 与 LiDAR 同向，
    杆臂为 MID-360 外参），`gt_imu.tum` 为 IMU 真值位姿，配置由 rig（`p600_mid360_inv`，空中出生）生成。算例写到
    `<out_dir>/<variant>-<tier>/`，由宿主上的 `slam_eval run --modes lio[,livo]` 评测，`collect_ate` 汇总比值。`center` 给出环心（世界帧 x、y），
    缺省按 `LoopTrack` 的规则选取。"""
    from slam_eval.case import CASE_SCHEMA
    from slam_eval.traj import Tum, save_tum

    from awr.sensorsvc.imu import kinematics_from_poses, load_imu_model, synth_imu
    from awr.sensorsvc.lidar.model import load_lidar_model
    from awr.sensorsvc.lidar.warp_mesh import LidarFrameReq, open_mesh_lidar
    from awr.sensorsvc.slam.bag import SlamBagWriter, verify_slam_bag
    from awr.sensorsvc.slam.rig import imu_lidar_extrinsics, rig_for_vehicle, write_slam_configs

    m = load_lidar_model()
    lid = open_mesh_lidar(q, m, None, backend=backend, device=device)
    tr = LoopTrack(q, speed_mps=speed_mps, yaw_amp_dps=yaw_amp_dps, agl_m=agl_m, flight_s=flight_s, hover_s=hover_s, ramp_s=ramp_s,
                   center=None if center is None else np.asarray(center, np.float64))
    n_frames = round(flight_s * 10) - 1
    t_frames = [tr.t_base + k * TICK_NS for k in range(n_frames)]
    t_end = tr.t_base + round(flight_s * 1e9)
    t_phys = np.arange(tr.t_base - 200_000_000, t_end + 200_000_000, 10**9 // ATE_IMU_HZ, dtype=np.int64)
    Rw, pw = tr.pose(t_phys)
    omega, f_b = kinematics_from_poses(t_phys, pw, np.array([_quat(R) for R in Rw]))
    out: dict[str, Any] = {"track": tr.describe(), "frames": n_frames, "backend": getattr(lid, "backend", "cpu-open3d"), "cases": {},
                           "distortion": {}}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for tier in tiers:
        rig = rig_for_vehicle(vehicle, start="pose", noise_tier=tier, t_pre_s=hover_s)
        R_il, T_il = imu_lidar_extrinsics(rig)                            # p_I = R·p_L + T
        R_bi, r_imu = R_il.T, -R_il.T @ T_il                             # IMU 在 LiDAR 系中的姿态与位置
        imu = synth_imu(t_phys, omega, f_b, R_bi=R_bi, r_imu_b=r_imu, model=load_imu_model(), t_from_ns=tr.t_base, t_to_ns=t_end, tier=tier,
                        seed=seed, uav_no=0, sensor_no=0)
        Ri, pi_ = tr.pose(imu.t_ns)
        gt = Tum(imu.t_ns / 1e9, pi_ + np.einsum("nij,j->ni", Ri, r_imu), np.array([_quat(R @ R_bi) for R in Ri]))
        scans = {}
        for var in ATE_VARIANTS:
            reqs = [LidarFrameReq(k, t, tr.nodes(t) if var == "per_point" else tr.single(t), seed=seed, uav_no=0, tier=tier)
                    for k, t in enumerate(t_frames)]
            scans[var] = _scan_many(lid, reqs)
            ret = float(np.mean([int((s.points["tag"] == 0).sum()) for s in scans[var][int(hover_s * 10):]]))
            if ret < ATE_MIN_RETURNS:
                raise RuntimeError(f"ATE track has {ret:.0f} returns per frame (< {ATE_MIN_RETURNS}); loop centre {tr.c.tolist()} is off the mesh")
            name = f"{var}-{tier}"
            d = Path(out_dir) / name
            write_slam_configs(rig, d, mode="livo", seq_name="est")
            events = [(int(s.t_start_ns), 0, k) for k, s in enumerate(scans[var])] + [(int(t), 1, j) for j, t in enumerate(imu.t_ns)]
            with SlamBagWriter(d / "sim.bag", camera=False) as w:
                for t, kind, j in sorted(events):
                    if kind == 0:
                        w.lidar_frame(t, scans[var][j].points)
                    else:
                        w.imu(t, imu.gyro_rps[j], imu.acc_mps2[j])
            rep = verify_slam_bag(d / "sim.bag")
            save_tum(d / "gt_imu.tum", gt)
            case = {"schema": CASE_SCHEMA, "name": name, "uav": rig["uav"], "lidar_model": "mid360", "flight_s": float(flight_s), "noise_profile": tier,
                    "seed": int(seed), "trajectory_source": "analytic_loop", "reflectance_model": "sem_table_v1",
                    "noise_table_version": rig["noise"]["noise_table_version"], "pose_nodes": "11" if var == "per_point" else "single_mid",
                    "slam_cam_phase": None, "world": world, "speed_mps": float(speed_mps), "agl_m": float(agl_m),
                    "lidar_backend": out["backend"], "track": out["track"]}
            (d / "case.json").write_text(json.dumps(case, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            out["cases"][name] = {"dir": str(d), "variant": var, "tier": tier, "bag": rep.as_dict(),
                                  "points_per_frame_mean": float(np.mean([len(s.points) for s in scans[var]]))}
        out["distortion"][tier] = distortion_ab(tr, m, scans["per_point"], scans["single_pose"], range(int(hover_s * 10) + 30, n_frames, 10))
        sys.stderr.write(f"[ate] {tier}: cases written; A p50 {out['distortion'][tier]['A_m'].get('p50')}\n")
    return out


def collect_ate(slam_out: Path, cases: Mapping[str, Mapping]) -> dict:
    """汇总 `slam_eval run` 的结果（`<slam_out>/<算例名>/metrics.json`）：按噪声档与模式给出两组的对齐 ATE、RPE100 与锚定漂移的中位数，
    以及比值 = 单一位姿 ATE / 逐点位姿 ATE（D3-AC-47；r4 的 FAST-LIVO2 LIO 口径 8 m/s 为 43 cm / 4.0 cm）。"""
    by: dict[str, dict] = {}
    for name, c in cases.items():
        p = Path(slam_out) / name / "metrics.json"
        with contextlib.suppress(OSError, ValueError):
            doc = json.loads(p.read_text(encoding="utf-8"))
            for mode, s in (doc.get("summary") or {}).items():
                g = by.setdefault(c["tier"], {}).setdefault(mode, {})
                g[c["variant"]] = {"ok": s.get("ok"), "runs": s.get("runs"), "status": doc.get("status"),
                                   **{k: (s.get(k) or {}).get("median") for k in ("ate_aligned_rmse_m", "rpe100_pct", "drift_max_m")}}
    for modes in by.values():
        for g in modes.values():
            pp, sp = (g.get(v, {}).get("ate_aligned_rmse_m") for v in ATE_VARIANTS)
            g["ate_ratio_single_over_per_point"] = sp / pp if pp and sp is not None else None
    return by


# ---------------------------------------------------------------------------------------------------------------- 判定
def _counted(level: str | None) -> tuple[bool, str]:
    if level == "A":
        return True, "A 级独占"
    if level == "B":
        return False, "B 级（外部进程只驻留显存）：经用户确认后计入"
    return False, f"{level or 'unknown'}：共享卡或无法判定，开发参考，不计入封板"


def judge(doc: Mapping) -> dict:
    """逐条对照 D3-AC-12 阈值；吞吐条款按独占级别标注是否计入（全部重复为 A 级才计入），几何一致性条款不受独占级别约束。"""
    items = []
    thr = doc.get("throughput") or {}
    for n, lim, cls in (("100", AC12_N100_P95_MS, "阻塞"), ("400", AC12_N400_P95_MS, "应交付")):
        f = (thr.get("fleets") or {}).get(n)
        if f is None:
            items.append({"id": f"AC12.n{n}_p95_ms", "class": cls, "threshold": lim, "value": None, "pass": None, "counted": False,
                          "note": "未测"})
            continue
        lv = f.get("levels") or []
        lvl = "A" if lv and all(x == "A" for x in lv) else ("B" if lv and all(x in ("A", "B") for x in lv) else
                                                             next((x for x in lv if x not in ("A", "B")), "unknown"))
        c, note = _counted(lvl)
        v = f.get("tick_p95_ms_median")
        if f.get("error"):
            c, note = False, f"未测完：{f['error']}"
        items.append({"id": f"AC12.n{n}_p95_ms", "class": cls, "threshold": lim, "value": v, "pass": v is not None and v <= lim,
                      "counted": c, "level": lvl, "note": note})
    df = doc.get("diff")
    for key, lim, get in (("ray_d_p99_m", AC12_D_P99_M, lambda d: d["d_m"].get("p99")), ("ray_d_max_m", AC12_D_MAX_M, lambda d: d["d_m"].get("max")),
                          ("hit_mismatch_rate", AC12_HIT_MISMATCH, lambda d: d["hit_mismatch_rate"])):
        v = get(df) if df else None
        note = "" if df else "未测"
        e = (df or {}).get("edge") or {}
        if key == "ray_d_max_m" and v is not None and v > lim and e.get("rays"):
            note = (f"{e['rays']} 条射线为轮廓边 float32 判定歧义（两侧命中不同静态三角形，较近三角形边界与射线距离 ≤ 带宽），"
                    "去掉后见 AC12.ray_d_max_m.same_surface")
        items.append({"id": f"AC12.{key}", "class": "阻塞", "threshold": lim, "value": v, "pass": None if v is None else v <= lim,
                      "counted": df is not None, "note": note})
    # 拟议口径（待 D01 以 ADR 冻结；不计入）：轮廓边判定歧义按命中判定不一致计，距离差分布只取同一表面
    for key, lim, get in (("ray_d_max_m.same_surface", AC12_D_MAX_M, lambda d: (d.get("d_same_surface_m") or {}).get("max")),
                          ("ray_d_p99_m.same_surface", AC12_D_P99_M, lambda d: (d.get("d_same_surface_m") or {}).get("p99")),
                          ("hit_or_edge_mismatch_rate", AC12_HIT_MISMATCH, lambda d: d.get("hit_or_edge_mismatch_rate"))):
        v = get(df) if df else None
        items.append({"id": f"AC12.{key}", "class": "拟议", "threshold": lim, "value": v, "pass": None if v is None else v <= lim,
                      "counted": False, "note": "拟议口径：轮廓边 float32 判定歧义计入命中判定不一致；待 D01 以 ADR 冻结后替换原文口径"})
    counted = [i for i in items if i["counted"]]
    ok = all(i["pass"] for i in counted) and bool(counted)
    seal = all(i["counted"] and i["pass"] for i in items if i["class"] == "阻塞") and all(i["pass"] for i in items if i["class"] != "拟议")
    slam = (doc.get("ate") or {}).get("slam") or {}
    info = {"AC47.ray_diff_distribution": None if not df else {k: df["d_m"].get(k) for k in ("p50", "p99", "max")},
            "AC47.ray_diff_distribution_same_surface": None if not df or "d_same_surface_m" not in df else
            {k: df["d_same_surface_m"].get(k) for k in ("p50", "p99", "max")},
            "AC47.ate_ratio": {f"{t}/{m}": g.get("ate_ratio_single_over_per_point") for t, ms in slam.items() for m, g in ms.items()} or None}
    return {"items": items, "counted_pass": ok, "seal_ready": bool(seal), "info": info}


# ---------------------------------------------------------------------------------------------------------------- 编排
def _part_path(out: Path, part: str) -> Path:
    return out / f"{part}.json"


def _load_part(out: Path, part: str, params: Mapping) -> dict | None:
    p = _part_path(out, part)
    with contextlib.suppress(OSError, ValueError):
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("params") == dict(params):
            return d
    return None


def _save(path: Path, doc: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def assemble(out: Path, meta: Mapping | None = None, slam_out: Path | None = None) -> dict:
    """合并各部分结果；给出 `slam_out` 时把 FAST-LIVO2 评测结果并入 `ate.slam`（并写回 ate.json）。"""
    doc: dict[str, Any] = {"schema": SCHEMA, "utc": _utc(), **(meta or {})}
    for part in PARTS:
        with contextlib.suppress(OSError, ValueError):
            doc[part] = json.loads(_part_path(out, part).read_text(encoding="utf-8"))["result"]
    if slam_out is not None and doc.get("ate"):
        doc["ate"]["slam"] = collect_ate(Path(slam_out), doc["ate"]["cases"])
        doc["ate"]["slam_out"] = str(slam_out)
        part = json.loads(_part_path(out, "ate").read_text(encoding="utf-8"))
        part["result"] = doc["ate"]
        _save(_part_path(out, "ate"), part)
    doc["judge"] = judge(doc)
    _save(out / "lidar_concurrency.json", doc)
    return doc


def cmd_run(a: argparse.Namespace) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    parts = [p for p in a.parts.split(",") if p]
    bad = set(parts) - set(PARTS)
    if bad:
        raise SystemExit(f"unknown parts: {sorted(bad)}")
    fleets = [int(x) for x in a.fleets.split(",") if x]
    lock = None
    pre: dict[str, Any] = {}
    if "throughput" in parts:
        if not a.no_lock:
            lock = ct.acquire_lock(Path(a.lock or os.environ.get("AWR_PERF_LOCK", "/tmp/awr-perf.lock")), a.lock_wait_s)
            if lock is None:
                sys.stderr.write("perf lock not acquired\n")
                return EXIT_ENV
        pre["load"] = ct.wait_load(a.load_max, a.load_wait_s)
        if not pre["load"]["ok"]:
            return EXIT_ENV
    gpu = ct.visible_gpu(a.gpu)
    n_ticks = max(a.tape_ticks, (a.ticks + a.warmup) * a.repeats + 60)
    vis = None if a.visibility_m <= 0 else a.visibility_m
    sc = open_scene(Path(a.world), Path(a.cache or out / "cache"), n_ticks=n_ticks, visibility_m=vis, rain_mmh=a.rain_mmh,
                    phase_ns=round(a.tape_phase_ms * 1e6), tape=Path(a.tape) if a.tape else None)
    meta = {"host": ct.host_info(), "gpu": {"index_visible": gpu[0], "uuid": gpu[1], "name": gpu[2], "requested": a.gpu},
            "world": {"dir": str(a.world), "tiles": len(sc.q.tiles), "triangles": int(sc.q.n_triangles)}, "scene": sc.describe(),
            "preconditions": pre, "lock": None if a.no_lock else str(a.lock or os.environ.get("AWR_PERF_LOCK", "/tmp/awr-perf.lock"))}
    _save(out / "meta.json", meta)
    if "throughput" in parts:
        params = {"fleets": fleets, "ticks": a.ticks, "warmup": a.warmup, "repeats": a.repeats, "tape_phase_ms": a.tape_phase_ms,
                  "visibility_m": a.visibility_m, "rain_mmh": a.rain_mmh}
        if a.force or _load_part(out, "throughput", params) is None:
            r = run_throughput(sc, fleets, ticks=a.ticks, warmup=a.warmup, repeats=a.repeats, gpu=gpu, device=a.device)
            _save(_part_path(out, "throughput"), {"params": params, "utc": _utc(), "result": r})
    if lock is not None:
        lock.close()
    if "diff" in parts:
        params = {"frames": a.diff_frames, "fleet": a.diff_fleet, "visibility_m": a.visibility_m, "rain_mmh": a.rain_mmh}
        if a.force or _load_part(out, "diff", params) is None:
            r = run_diff(sc, diff_frames(sc, a.diff_frames, fleet_n=a.diff_fleet), device=a.device)
            _save(_part_path(out, "diff"), {"params": params, "utc": _utc(), "result": r})
    if "ate" in parts:
        params = {"flight_s": a.ate_flight_s, "speed_mps": a.ate_speed, "yaw_amp_dps": a.ate_yaw_dps, "agl_m": a.ate_agl, "tiers": a.ate_tiers,
                  "center": a.ate_center}
        if a.force or _load_part(out, "ate", params) is None:
            r = build_ate_cases(sc.q, out / "ate" / "cases", tiers=[t for t in a.ate_tiers.split(",") if t], speed_mps=a.ate_speed,
                                yaw_amp_dps=a.ate_yaw_dps, agl_m=a.ate_agl, flight_s=a.ate_flight_s, device=a.device, world=Path(a.world).name,
                                center=[float(x) for x in a.ate_center.split(",")] if a.ate_center else None)
            _save(_part_path(out, "ate"), {"params": params, "utc": _utc(), "result": r})
    doc = assemble(out, meta)
    sys.stdout.write(json.dumps(doc["judge"], ensure_ascii=False, indent=1) + "\n")
    return EXIT_OK if doc["judge"]["counted_pass"] else EXIT_FAIL


def cmd_judge(a: argparse.Namespace) -> int:
    out = Path(a.out)
    meta: dict = {}
    with contextlib.suppress(OSError, ValueError):
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    doc = assemble(out, meta, Path(a.slam_out) if a.slam_out else None)
    sys.stdout.write(json.dumps(doc["judge"], ensure_ascii=False, indent=1) + "\n")
    return EXIT_OK if doc["judge"]["counted_pass"] else EXIT_FAIL


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="测量并判定")
    r.add_argument("--world", required=True, help="世界包目录（需 geometry/mesh/raycast）")
    r.add_argument("--out", required=True)
    r.add_argument("--cache", help="网格查询缓存与合成磁带目录（缺省 <out>/cache）")
    r.add_argument("--tape", help="事件磁带 tape.bin（缺省按世界合成 32 个演员）")
    r.add_argument("--parts", default=",".join(PARTS))
    r.add_argument("--gpu", type=int)
    r.add_argument("--device", help="Warp 设备（缺省 cuda:0）")
    r.add_argument("--fleets", default="100,400")
    r.add_argument("--ticks", type=int, default=300)
    r.add_argument("--warmup", type=int, default=20)
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--tape-ticks", type=int, default=600)
    r.add_argument("--tape-phase-ms", type=float, default=0.0, help="磁带时刻相对 LiDAR 帧起点的偏移（非 100 ms 整数倍时帧跨磁带帧边界）")
    r.add_argument("--visibility-m", type=float, default=500.0, help="雾能见度（≤ 0 关闭）")
    r.add_argument("--rain-mmh", type=float, default=10.0)
    r.add_argument("--diff-frames", type=int, default=96)
    r.add_argument("--diff-fleet", type=int, default=100)
    r.add_argument("--ate-flight-s", type=float, default=60.0)
    r.add_argument("--ate-speed", type=float, default=8.0)
    r.add_argument("--ate-yaw-dps", type=float, default=32.0)
    r.add_argument("--ate-agl", type=float, default=8.0)
    r.add_argument("--ate-tiers", default="clean,realistic")
    r.add_argument("--ate-center", help="ATE 环心 x,y（世界帧 m；缺省由 loop_center 选取）")
    r.add_argument("--lock", help="排他锁路径（缺省 $AWR_PERF_LOCK）")
    r.add_argument("--lock-wait-s", type=float, default=3600.0)
    r.add_argument("--no-lock", action="store_true")
    r.add_argument("--load-max", type=float)
    r.add_argument("--load-wait-s", type=float, default=1800.0)
    r.add_argument("--force", action="store_true", help="忽略已有的部分结果")
    r.set_defaults(fn=cmd_run)
    j = sub.add_parser("judge", help="按已有的部分结果重新汇总与判定")
    j.add_argument("--out", required=True)
    j.add_argument("--slam-out", help="slam_eval 输出根目录（<slam-out>/<算例名>/metrics.json），汇总 ATE 比值")
    j.set_defaults(fn=cmd_judge)
    a = ap.parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":
    sys.exit(main())
