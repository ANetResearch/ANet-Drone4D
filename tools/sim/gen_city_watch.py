"""D2.2-W2：六座城市的模拟无人机监测剧本生成（确定性、可重跑；见 docs/impl/D2.2-W2-报告.md 第 2 节设计）。

为 shenzhen、suzhou、newyork、chicago、shanghai、sanfrancisco 六个世界各生成一份 `awr.sandbox.snapshot.v1` 剧本
（`scenarios/d22-<city>-watch.json`），供 `run.scenario` 以 `dev`/`ci`/`perf` 等 profile 直接加载（supervisor
`_session_config_env`，同 synthcity 的 `s7-synthcity-relay.json`）：两个机巢、4 架以上无人机、6 个以上目标物
（含静止与运动）、2 个任务类型互不相同的监测任务（接力监视、区域巡查、定点监视、周界巡逻、区域值守、巡线，六城合计覆盖
全部六种任务类型）。

D2.2-FX-world：`relay_watch`（接力监视）的 `common` 原来复用 area_guard 的 `common_relay`（awr_v1/nest-b 垂起机）；
真实引擎复核（`tools.sim._engine_check`，见该文件修复说明）发现这个机型组合的站位搜索（`python/awr/sim/tasking
/relay.py` 近距离环搜索）在真实带建筑点云的世界里几乎找不到满足视线的落点（code 581、why=los；shenzhen 七个目标物
候选逐一试过，awr_v1/nest-b 全部不可行），而换成 `common_patrol`（p600_mid360/nest-a，与巡线等任务同一能精确悬停
的多旋翼）后同样七个候选点里有两个可行——是机型选择的问题，不是 relay_watch 这个任务类型本身与真实几何不兼容（详见
`make_task` 里 relay_watch 分支的注释与 `tests/scenarios/test_d22_city_watch.py`
`test_relay_watch_with_a_vtol_model_is_infeasible_in_a_real_world`）。现在的 relay_watch 用 `common_patrol`，
配合既有的 GeoRetry 重试（换种子重新摆放全部目标物位置）找可行的点位组合。

地理合理性只用世界自身的 DSM/DTM/zones（`awr.world.geometry.query.open_world_query`，与引擎用的是同一套查询）：
- 开阔地判据：`height_dsm(xy) - ground_dtm(xy) < OPEN_EPS_M`（该处地表高程接近裸地高程，即没有建筑物或树冠遮盖）；
- 不在禁飞 / 限制区（`zones.nofly`、`zones.restricted` 的多边形，射线法 `point_in_ring`）；
- 彼此最小间距、离世界边界的内缩；
- 任务高度：`alt_m` 取任务几何沿线采样点 `column_max_within` 的最大值（最近障碍高程）之上的安全余量（`ALT_MARGIN_M`），
  与地面裁剪的上下限（契约 5–500 m）；这只是提交给任务规划器的参数，实际航线仍由既有的 Grid25 / A* / los_batch 规划与避障
  （与 S0–S7、既有剧本同一套规划器，本脚本不重新实现避障）。

目标物的 `sensitivity`（视觉扇形、声学半球）与任务的 `on_detect`/`level_req` 原样沿用 `s7-synthcity-relay.json` 的取值
（只改坐标，不改形状参数与量级），以沿用该剧本已验证的“不被目标物发现”（`target.discovered` 事件数为 0）结果（D2.2-W1
之前 S7 的既有验收口径；本脚本不改变、不重新论证该判据的数值）。

用法：`python -m tools.sim.gen_city_watch [--check] [--city shenzhen ...]`；`--check` 只比较不写（退出码 1 表示有差异）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

OPEN_EPS_M = 1.5          # dsm - dtm 容差：小于它视为开阔地（街道、广场、草坪、低矮车辆）
BORDER_MARGIN_M = 40.0    # 离世界边界的内缩
ALT_MARGIN_M = 28.0       # 任务高度相对最近障碍高程的安全余量
ALT_FLOOR_M = 40.0
ALT_CEIL_M = 480.0
NEST_SEP_M = 45.0         # 两个机巢之间的最小间距
POINT_SEP_M = 22.0        # 目标物/候选点之间的最小间距
GRID_STEP_M = 6.0         # 局部候选网格步长

# 任务规划的“禁入圆”（`awr.sim.tasking.runtime.TaskingRuntime.margin_m`、`_precheck_sync`）：每个目标物按其
# sensitivity 的最大 radius_max_m 加这个余量划一个水平禁入圆，定点监视/巡线的航点落在圆内会被 583 拒绝——这正是
# “既有判据”里让观测保持在目标物感知范围之外的机制，本脚本据此摆放定点监视的悬停点与巡线航点，不与它对抗。
KEEPOUT_MARGIN_M = 20.3
KEEPOUT_BUFFER_M = 12.0   # 额外安全余量（取整误差、margin_m 随 RTK 档变化的保守上限）
CONTRACTS = "1.1.0"
CALENDAR = {"start_utc": "2026-10-05T00:00:00Z", "tz": "Asia/Shanghai"}

# S7（synthcity）既有验证过的 sensitivity 形状，原样沿用（只搬坐标，不改量级）
SENS_PERSON = [
    {"kind": "visual", "shape": "sector", "radius_max_m": 150, "z_min_m": 0, "z_max_m": 150, "half_angle_deg": 60,
     "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True},
    {"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 120, "z_min_m": 0, "z_max_m": 120, "half_angle_deg": 180,
     "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True},
]
SENS_VEHICLE = [
    {"kind": "visual", "shape": "sector", "radius_max_m": 80, "z_min_m": 0, "z_max_m": 80, "half_angle_deg": 60,
     "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True},
    {"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 50, "z_min_m": 0, "z_max_m": 50, "half_angle_deg": 180,
     "azimuth_offset_deg": 0, "gain_back_db": 0, "enabled": True},
]


# ---------------------------------------------------------------------- 世界几何：候选点与安全高度
class CityGeo:
    def __init__(self, city: str) -> None:
        from awr.world.geometry.query import open_world_query

        self.city = city
        self.wq = open_world_query(ROOT / "worlds" / city)
        self.bounds = self.wq.bounds_m  # [[xmin,ymin,zmin],[xmax,ymax,zmax]]

    def _in_restricted(self, xy: np.ndarray) -> np.ndarray:
        from awr.datasets.scenarios.geometry import point_in_ring

        zi = self.wq.zones
        out = np.zeros(len(xy), dtype=bool)
        for prism in [*zi.nofly, *zi.restricted]:
            for poly in prism.polygons:
                out |= point_in_ring(xy, np.asarray(poly[0], dtype=np.float64))
        return out

    def open_candidates(self, center: tuple[float, float], half_m: float, step_m: float = GRID_STEP_M,
                        avoid: list[tuple[float, float, float]] = ()) -> np.ndarray:
        """`center` 周围 2·half_m 方窗内的开阔地候选点（世界 ENU xy），不含禁飞/限制区，不越界，不落在 `avoid`
        给出的任一圆（例如目标物的禁入圆，见 `target_clear_circles`）内。"""
        xmin = max(self.bounds[0][0] + BORDER_MARGIN_M, center[0] - half_m)
        xmax = min(self.bounds[1][0] - BORDER_MARGIN_M, center[0] + half_m)
        ymin = max(self.bounds[0][1] + BORDER_MARGIN_M, center[1] - half_m)
        ymax = min(self.bounds[1][1] - BORDER_MARGIN_M, center[1] + half_m)
        xs = np.arange(xmin, xmax, step_m)
        ys = np.arange(ymin, ymax, step_m)
        X, Y = np.meshgrid(xs, ys)
        xy = np.stack([X.ravel(), Y.ravel()], axis=1)
        dtm = self.wq.ground_dtm(xy)
        dsm = self.wq.height_dsm(xy)
        mask = (dsm - dtm) < OPEN_EPS_M
        mask &= ~self._in_restricted(xy)
        for cx, cy, r in avoid:
            mask &= (np.hypot(xy[:, 0] - cx, xy[:, 1] - cy) >= r)
        return xy[mask]

    def pick_points(self, center: tuple[float, float], n: int, *, half_m: float = 220.0, min_sep_m: float = POINT_SEP_M,
                    rng: np.random.Generator, grow: tuple[float, ...] = (220, 320, 460, 650),
                    avoid: list[tuple[float, float, float]] = ()) -> np.ndarray:
        """贪心最远点采样：候选不足时扩大搜索窗；返回 (n, 2)，不足 n 时抛错（调用方据此调整锚点或窗口）。"""
        cand: np.ndarray | None = None
        for h in grow:
            c = self.open_candidates(center, h, avoid=avoid)
            if len(c) >= n * 6:
                cand = c
                break
            cand = c
        if cand is None or len(cand) == 0:
            raise RuntimeError(f"{self.city}: no open candidates near {center}")
        order = rng.permutation(len(cand))
        cand = cand[order]
        chosen: list[np.ndarray] = []
        for p in cand:
            if all(float(np.hypot(*(p - q))) >= min_sep_m for q in chosen):
                chosen.append(p)
            if len(chosen) >= n:
                break
        if len(chosen) < n:
            raise RuntimeError(f"{self.city}: only {len(chosen)}/{n} points with sep>={min_sep_m} near {center}")
        return np.asarray(chosen)

    def ring_point(self, center: tuple[float, float], radius_m: float, rng: np.random.Generator, *,
                   avoid: list[tuple[float, float, float]] = (), n_angles: int = 24) -> np.ndarray | None:
        """`center` 周围固定半径的环上找一点：不落在禁飞/限制区、不落在 `avoid` 的任一圆内、不越界。用于把定点监视的
        悬停点放在目标物禁入圆之外、又尽量贴近它（看得清楚）。按随机起始角等步长扫描一整圈，取第一个合格点。"""
        a0 = float(rng.uniform(0, 2 * math.pi))
        angles = a0 + np.linspace(0, 2 * math.pi, n_angles, endpoint=False)
        xy = np.stack([center[0] + radius_m * np.cos(angles), center[1] + radius_m * np.sin(angles)], axis=1)
        xmin, ymin = self.bounds[0][0] + BORDER_MARGIN_M, self.bounds[0][1] + BORDER_MARGIN_M
        xmax, ymax = self.bounds[1][0] - BORDER_MARGIN_M, self.bounds[1][1] - BORDER_MARGIN_M
        inb = (xy[:, 0] >= xmin) & (xy[:, 0] <= xmax) & (xy[:, 1] >= ymin) & (xy[:, 1] <= ymax)
        bad = self._in_restricted(xy)
        for cx, cy, r in avoid:
            bad |= np.hypot(xy[:, 0] - cx, xy[:, 1] - cy) < r
        ok = inb & ~bad
        idx = np.flatnonzero(ok)
        return xy[idx[0]] if len(idx) else None

    def ground_z(self, xy: tuple[float, float]) -> float:
        return float(self.wq.ground_dtm(np.asarray([xy], dtype=np.float64))[0])

    def surface_z(self, xy: tuple[float, float]) -> float:
        """该点的 DSM 高程（世界引擎按它核验机巢/出生点的 z，见 `awr.sim.runtime.main` SPAWN_Z：
        `dsm - 0.05 <= z <= dsm + 0.5`）。"""
        return float(self.wq.height_dsm(np.asarray([xy], dtype=np.float64))[0])

    def nest_flat_ok(self, center_xy: tuple[float, float], n_slots: int, *, tol_up_m: float = 0.0,
                     tol_down_m: float = 0.35) -> bool:
        """机巢中心周围各槽位（`awr.sim.tasking.nests.slot_offsets`，3 列栅格、间距 5 m）的 DSM 相对中心的落差在
        SPAWN_Z 允许的窗口内——机巢的 `pos.z` 固定为中心高程，各槽位的出生点校验用的是各自 xy 处的 DSM（同一高程），
        中心周边不平（台阶、矮墙、停驶车辆）时部分槛位会落在窗口之外。"""
        from awr.sim.tasking.nests import slot_offsets

        cx, cy = float(center_xy[0]), float(center_xy[1])
        offs = slot_offsets(max(int(n_slots), 1))
        xy = np.asarray([[cx + dx, cy + dy] for dx, dy in offs], dtype=np.float64)
        dsm = self.wq.height_dsm(xy)
        z = self.surface_z((cx, cy))
        return bool(np.all(dsm <= z + tol_up_m) and np.all(dsm >= z - tol_down_m))

    def safe_alt_m(self, pts_xy: np.ndarray, *, probe_radius_m: float = 15.0) -> float:
        """沿给定点集的安全任务高度（AGL，相对各采样点当地地面）：最近障碍高程 + 安全余量，限制在契约上下限内。"""
        pts = np.asarray(pts_xy, dtype=np.float64).reshape(-1, 2)
        obstacle_abs = self.wq.column_max_within(pts, radius_m=probe_radius_m)
        ground = self.wq.ground_dtm(pts)
        agl_needed = (obstacle_abs - ground) + ALT_MARGIN_M
        return float(np.clip(np.max(agl_needed), ALT_FLOOR_M, ALT_CEIL_M))

    def path_ok(self, xy_chain: np.ndarray, alt_m: float, *, closed: bool = False, buffer_m: float = 2.0) -> tuple[bool, str]:
        """沿 xy_chain（在给定 AGL 高度）各航段是否不进入禁入体、不贴近障碍物（`WorldQuery.path_valid`，与引擎任务
        装配用的同一判据）。"""
        pts = np.asarray(xy_chain, dtype=np.float64).reshape(-1, 2)
        if closed:
            pts = np.concatenate([pts, pts[:1]], axis=0)
        z = self.wq.ground_dtm(pts) + alt_m
        xyz = np.concatenate([pts, z[:, None]], axis=1)
        r = self.wq.path_valid(xyz, buffer_m=buffer_m)
        return bool(r.ok), (r.reason or "")


def _ccw_hull(points: np.ndarray) -> list[list[float]]:
    """凸包，顶点按逆时针排列（契约要求 polygon 逆时针）。Andrew's monotone chain。"""
    pts = sorted({(round(float(x), 3), round(float(y), 3)) for x, y in points})
    if len(pts) < 3:
        raise RuntimeError("not enough distinct points for a polygon")

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    area2 = sum(hull[i][0] * hull[(i + 1) % len(hull)][1] - hull[(i + 1) % len(hull)][0] * hull[i][1]
                for i in range(len(hull)))
    if area2 < 0:
        hull.reverse()
    return [[round(x, 2), round(y, 2)] for x, y in hull]


def _pad_polygon(hull: list[list[float]], pad_m: float) -> list[list[float]]:
    cx = sum(p[0] for p in hull) / len(hull)
    cy = sum(p[1] for p in hull) / len(hull)
    out = []
    for x, y in hull:
        d = math.hypot(x - cx, y - cy) or 1.0
        f = (d + pad_m) / d
        out.append([round(cx + (x - cx) * f, 2), round(cy + (y - cy) * f, 2)])
    return out


def _nearest_chain(pts: np.ndarray) -> np.ndarray:
    """按最近邻排序成一条链（起点为 pts[0]），使相邻航点之间的航段尽量短，不绕远穿越其他区域。"""
    remain = [p for p in pts]
    chain = [remain.pop(0)]
    while remain:
        last = chain[-1]
        i = min(range(len(remain)), key=lambda k: float(np.hypot(*(remain[k] - last))))
        chain.append(remain.pop(i))
    return np.asarray(chain)


# ---------------------------------------------------------------------- 城市内容规划
TASK_TYPE_ZH = {
    "point_watch": "定点监视", "polyline_patrol": "巡线", "area_patrol": "区域巡查",
    "perimeter_patrol": "周界巡逻", "relay_watch": "接力监视", "area_guard": "区域值守",
}

# D2.2-FX-world：shenzhen 的第二个任务恢复为 relay_watch（修了 make_task 里 relay_watch 的机型问题后用真实引擎
# 复核可行，见本文件顶部模块说明）；newyork 的第二个任务由 relay_watch 改为 perimeter_patrol（已在 chicago 验证
# 可行）——六城只需一城承载 relay_watch 即可合计覆盖全部六种类型，newyork 换掉是为了减少本次返工与复核面，不是因为
# relay_watch 在 newyork 不可行（未重新验证）；种子不变，不影响其余三城。
CITY_PLAN: dict[str, dict[str, Any]] = {
    "shenzhen":      {"anchor": (-150.0, 60.0),  "tasks": ("polyline_patrol", "relay_watch"), "seed": 2201},
    "suzhou":        {"anchor": (0.0, 0.0),       "tasks": ("polyline_patrol", "point_watch"), "seed": 2202},
    "newyork":       {"anchor": (-70.0, -1480.0), "tasks": ("area_patrol", "perimeter_patrol"), "seed": 2203},
    "chicago":       {"anchor": (590.0, -1140.0), "tasks": ("perimeter_patrol", "area_guard"), "seed": 2204},
    "shanghai":      {"anchor": (-2810.0, 1370.0), "tasks": ("area_guard", "point_watch"),     "seed": 2205},
    "sanfrancisco":  {"anchor": (-1870.0, -2230.0), "tasks": ("point_watch", "polyline_patrol"), "seed": 2206},
}
CITIES = tuple(CITY_PLAN)

N_VEHICLES_P600 = 5
N_VEHICLES_V1 = 2
N_TARGETS = 7


class GeoRetry(RuntimeError):
    """本次点位抽样下任务几何未通过 `path_valid`（禁入体或障碍物）：上层换一个种子重试。"""


def build_city(city: str, *, seed: int | None = None, geo: CityGeo | None = None) -> dict[str, Any]:
    plan = CITY_PLAN[city]
    t_types = plan["tasks"]
    # 受 583 逐点禁入圆核验必须避开全部目标物（巡线），或分解阶段要求覆盖区域里有圆外空间（区域巡查，否则
    # "area_empty"：目标物密集时 25 m 窄边距的多边形整个落在某个目标物的禁入圆内，见模块说明与报告）
    needs_far_task_pts = "polyline_patrol" in t_types or "area_patrol" in t_types
    rng = np.random.default_rng(seed if seed is not None else plan["seed"])
    geo = geo or CityGeo(city)
    anchor = plan["anchor"]

    n_nests = 2
    n_targets = N_TARGETS
    n_task_pts = 6  # 轨迹/多边形用的额外点（与机巢、目标物分开取，避免几何退化）

    # ---- 机巢中心：在开阔地候选中，额外要求各自槛位栅格（3 列、间距 5 m，按各机巢槛位数）范围内的 DSM 落差落在
    # SPAWN_Z 窗口内（机巢 pos.z 固定为中心高程，各槛位出生点用自己 xy 的 DSM 核验，中心周边不平时部分槛位会越界）
    nest_slots = [6, 2]  # nest-a（机巢箱，6 槛）、nest-b（VTOL 起降台，2 槛）
    # 一次性取够大的候选窗口再按地势平坦筛选：候选点数量不是瓶颈（丘陵地形里大多数开阔点槛位范围不够平），
    # 分级扩窗一遇到候选点数达标就会提前停在小窗口，反而找不到地势平坦的那一小部分
    nest_cand = geo.open_candidates(anchor, 900.0)
    order = rng.permutation(len(nest_cand))
    nest_cand = nest_cand[order]
    nest_xy_list: list[np.ndarray] = []
    for want_slots in nest_slots:
        picked = None
        for p in nest_cand:
            if any(float(np.hypot(*(p - q))) < NEST_SEP_M for q in nest_xy_list):
                continue
            if not geo.nest_flat_ok(tuple(p), want_slots):
                continue
            picked = p
            break
        if picked is None:
            raise GeoRetry(f"{city}: 找不到第 {len(nest_xy_list) + 1} 个槛位范围内地势平坦且间距足够的机巢中心")
        nest_xy_list.append(picked)
    if len(nest_xy_list) < n_nests:
        raise GeoRetry(f"{city}: 找不到 {n_nests} 个槛位范围内地势平坦且间距足够的机巢中心")
    nest_xy = np.asarray(nest_xy_list)
    nest_xy_by_id = {"nest-a": nest_xy[0], "nest-b": nest_xy[1]}

    # 目标物位置：贴近机巢（S7 同一尺度的工作范围），与机巢保持最小间距
    tries = 0
    target_xy = geo.pick_points(anchor, n_targets, half_m=260.0, min_sep_m=POINT_SEP_M, rng=rng)
    while any(float(np.hypot(*(p - q))) < POINT_SEP_M for p in target_xy for q in nest_xy) and tries < 6:
        target_xy = geo.pick_points(anchor, n_targets, half_m=260.0 + 40.0 * tries, min_sep_m=POINT_SEP_M, rng=rng)
        tries += 1

    # 每个目标物的水平禁入圆（KEEPOUT_MARGIN_M + KEEPOUT_BUFFER_M 之上，按 sensitivity 最大 radius_max_m）：
    # 巡线/定点监视的航点、悬停点不得落入，否则创建时被 583 拒绝（见模块说明）
    kinds = (["person_static", "person_walk", "vehicle_path"] * ((n_targets + 2) // 3))[:n_targets]
    target_clear: list[tuple[float, float, float]] = []
    for (x, y), kind in zip(target_xy, kinds, strict=True):
        sens = SENS_PERSON if kind.startswith("person") else SENS_VEHICLE
        r = max(s["radius_max_m"] for s in sens) + KEEPOUT_MARGIN_M + KEEPOUT_BUFFER_M
        target_clear.append((float(x), float(y), float(r)))

    # 巡线/周界用的额外点：
    # - polyline_patrol 受 583 逐点禁入圆核验，必须避开全部目标物（在更大的窗口里找）；
    # - perimeter_patrol 不受该核验，就近取点（窗口同目标物），否则巡边多边形会跨出目标物工作区太远；
    # - 其余任务类型不用 task_xy，仍按窗口就近取，不影响几何。
    task_xy: np.ndarray
    if needs_far_task_pts:
        task_xy = geo.pick_points(anchor, n_task_pts, half_m=420.0, min_sep_m=POINT_SEP_M, rng=rng,
                                  grow=(420, 560, 720, 900), avoid=target_clear)
        tries = 0
        while any(float(np.hypot(*(p - q))) < POINT_SEP_M for p in task_xy for q in nest_xy) and tries < 4:
            task_xy = geo.pick_points(anchor, n_task_pts, half_m=420.0 + 80.0 * tries, min_sep_m=POINT_SEP_M, rng=rng,
                                      grow=(420, 560, 720, 900, 1100), avoid=target_clear)
            tries += 1
    else:
        task_xy = geo.pick_points(anchor, n_task_pts, half_m=260.0, min_sep_m=POINT_SEP_M, rng=rng)
        tries = 0
        while any(float(np.hypot(*(p - q))) < POINT_SEP_M for p in task_xy for q in nest_xy) and tries < 4:
            task_xy = geo.pick_points(anchor, n_task_pts, half_m=260.0 + 40.0 * tries, min_sep_m=POINT_SEP_M, rng=rng)
            tries += 1

    # ---- 机巢：z 取当地 DSM 面高程之上的薄壳（0.02/0.01 m），满足引擎出生点核验
    # `dsm - 0.05 <= z <= dsm + 0.5`（`awr.sim.runtime.main` SPAWN_Z；机巢已限定在开阔地，DSM ≈ 裸地高程）
    za = round(geo.surface_z((float(nest_xy[0][0]), float(nest_xy[0][1]))) + 0.02, 3)
    zb = round(geo.surface_z((float(nest_xy[1][0]), float(nest_xy[1][1]))) + 0.01, 3)
    nests = [
        {"id": "nest-a", "name": "机巢箱 A", "pos": [round(float(nest_xy[0][0]), 2), round(float(nest_xy[0][1]), 2), za],
         "kind": "box", "slots": 6,
         "energy": {"mode": "swap", "swap_s": 180, "packs": [{"cells": 6, "count": 8, "capacity_ah": 10}]},
         "chargers": [{"model": "c1_xr", "count": 8, "channels": 1, "i_max_a": 10, "cells_max": 6}],
         "turnaround_s": 60, "launch_interval_s": 30, "link_range_m": 3500},
        {"id": "nest-b", "name": "VTOL 起降台 B", "pos": [round(float(nest_xy[1][0]), 2), round(float(nest_xy[1][1]), 2), zb],
         "kind": "vtol_pad", "slots": 2,
         "energy": {"mode": "swap", "swap_s": 180, "packs": [{"cells": 12, "count": 2, "capacity_ah": 22}]},
         "chargers": [{"model": "chg12s_ref", "count": 2, "channels": 1, "i_max_a": 10, "cells_max": 12}],
         "turnaround_s": 60, "launch_interval_s": 30, "link_range_m": 3500},
    ]

    # ---- 机队
    cn = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
    vehicles = [{"id": f"p600-{i + 1:02d}", "model_id": "p600_mid360", "nest_id": "nest-a",
                 "label": f"P600 {cn[i]}号", "sensors": [{"name": "nir_ill", "enabled": True}]}
                for i in range(N_VEHICLES_P600)]
    vehicles += [{"id": f"v1-{i + 1:02d}", "model_id": "awr_v1", "nest_id": "nest-b", "label": f"AWR-V1 {cn[i]}号"}
                 for i in range(N_VEHICLES_V1)]

    # ---- 目标物：静止的人 / 游走的人 / 沿路车辆 交替，位置来自已校验的开阔地候选点
    targets = []
    for i, (x, y) in enumerate(target_xy):
        x, y = float(x), float(y)
        kind = kinds[i]
        tid = f"{'person' if kind.startswith('person') else 'vehicle'}-{i + 1:02d}"
        if kind == "person_static":
            targets.append({"id": tid, "label": "静止的人", "class": "person", "pos": [round(x, 2), round(y, 2), None],
                             "yaw_rad": round(float(rng.uniform(-math.pi, math.pi)), 3),
                             "motion": {"mode": "static"}, "sensitivity": SENS_PERSON})
        elif kind == "person_walk":
            r = 28.0
            region = [[round(x - r, 2), round(y - r, 2)], [round(x + r, 2), round(y - r, 2)],
                      [round(x + r, 2), round(y + r, 2)], [round(x - r, 2), round(y + r, 2)]]
            targets.append({"id": tid, "label": "游走的人", "class": "person", "pos": [round(x, 2), round(y, 2), None],
                             "motion": {"mode": "random_walk", "region": region, "speed_mps": 1.0},
                             "sensitivity": SENS_PERSON})
        else:
            dx, dy = (40.0, 0.0) if i % 2 == 0 else (0.0, 40.0)
            targets.append({"id": tid, "label": "沿路车辆", "class": "vehicle",
                             "pos": [round(x, 2), round(y, 2), None], "yaw_rad": 0,
                             "motion": {"mode": "path", "loop": "pingpong", "speed_mps": 3.0,
                                        "path": [[round(x - dx, 2), round(y - dy, 2), None],
                                                 [round(x + dx, 2), round(y + dy, 2), None]]},
                             "sensitivity": SENS_VEHICLE})

    # ---- 任务：两个类型不同的监测任务，几何来自 task_xy / target_xy，高度来自 safe_alt_m
    tasks = []
    label0 = TASK_TYPE_ZH[t_types[0]]
    label1 = TASK_TYPE_ZH[t_types[1]]
    common_patrol = {"priority": "normal", "on_detect": "notify", "allowed_nests": ["nest-a"], "allowed_models": ["p600_mid360"]}
    common_relay = {"priority": "normal", "on_detect": "relay", "allowed_nests": ["nest-b"], "allowed_models": ["awr_v1"]}

    def _with_transit(nest_id: str, pts: np.ndarray, n: int = 16) -> np.ndarray:
        """`pts` 前补一段机巢到 `pts[0]` 的密集插值采样，使 `safe_alt_m` 把转场沿途的障碍物也计入高度余量
        （否则只看任务几何自身的邻域，转场途中的建筑可能被漏过）。"""
        home = nest_xy_by_id[nest_id]
        leg = np.linspace(home, np.asarray(pts[0], dtype=np.float64), n)
        return np.concatenate([leg, np.asarray(pts, dtype=np.float64)], axis=0)

    def make_task(ttype: str, label: str) -> dict[str, Any]:
        if ttype == "point_watch":
            # 悬停点放在被看目标物（person-01）禁入圆之外：既不会被 583 拒绝，也仍在视觉感知半径附近，便于明确捕获
            tx, ty, tr = target_clear[0]
            p_xy = geo.ring_point((tx, ty), tr + 15.0, rng, avoid=target_clear)
            if p_xy is None:
                raise GeoRetry(f"{city}: point_watch 在目标物禁入圆外找不到合规悬停点")
            x, y = float(p_xy[0]), float(p_xy[1])
            alt = geo.safe_alt_m(_with_transit("nest-a", np.asarray([[x, y]])))
            return {"type": ttype, "label": label,
                    "params": {"point": [round(x, 2), round(y, 2), None], "alt_m": round(alt, 1), "alt_ref": "agl",
                               "dwell_s": None, "look": {"mode": "target", "target": "person-01"}},
                    "common": dict(common_patrol)}
        if ttype == "polyline_patrol":
            chain = _nearest_chain(task_xy[:4])
            wps = [[round(float(x), 2), round(float(y), 2), None] for x, y in chain]
            alt = geo.safe_alt_m(_with_transit("nest-a", chain))
            return {"type": ttype, "label": label,
                    "params": {"waypoints": wps, "mode": "loop", "speed_mps": 8.0, "alt_m": round(alt, 1),
                               "sensor_facing": "down", "auto_detour": True},
                    "common": dict(common_patrol)}
        if ttype == "relay_watch":
            # D2.2-FX-world：原来复用 area_guard 的 common_relay（awr_v1/nest-b）。awr_v1 是垂起/固定翼机型，接力站位
            # 的站位搜索（`python/awr/sim/tasking/relay.py` 近距离环搜索）在真实带建筑点云的世界里几乎找不到它能满足
            # 视线的落点（真实引擎复核：shenzhen 七个目标物候选点逐一试过，awr_v1/nest-b 全部判不可行，code 581、
            # why=los；换成 common_patrol 即 p600_mid360/nest-a——与巡线等任务同一机型、同一套能精确悬停的多旋翼——
            # 同样七个候选点里有两个可行），这是机型选择的问题，不是 relay_watch 这个任务类型本身与真实几何不兼容
            # （`on_detect` 对 relay_watch 自身是无操作的：`python/awr/sim/tasking/runtime.py` `_on_detect` 的
            # 升级机制明确排除 `T.type == "relay_watch"`，只对 area_guard 之类的巡查任务有效）。换成 common_patrol
            # 后配合既有的 GeoRetry 重试（换种子重新摆放全部目标物位置）足以在合理次数内找到可行的点位组合。
            return {"type": ttype, "label": label, "params": {"target": "person-01", "level_req": "R", "duration_s": None},
                    "common": dict(common_patrol)}
        if ttype == "area_patrol":
            # 多边形来自 task_xy（避开全部目标物禁入圆的点）：区域巡查会扫描覆盖面，整块落在某个目标物禁入圆内
            # 时判 area_empty；巡查区域与目标物分开，仍有另一任务（接力监视等）覆盖目标物
            hull = _ccw_hull(task_xy)
            poly = _pad_polygon(hull, 25.0)
            alt = geo.safe_alt_m(_with_transit("nest-a", np.asarray(poly, dtype=np.float64)))
            return {"type": ttype, "label": label,
                    "params": {"polygon": poly, "n_vehicles": "auto", "target_class": "person", "level_req": "R",
                               "revisit_s": 300, "alt_m": round(alt, 1)},
                    "common": dict(common_patrol)}
        if ttype == "perimeter_patrol":
            hull = _ccw_hull(np.concatenate([target_xy, task_xy], axis=0))
            poly = _pad_polygon(hull, 20.0)
            alt = geo.safe_alt_m(_with_transit("nest-a", np.asarray(poly, dtype=np.float64)))
            return {"type": ttype, "label": label,
                    "params": {"polygon": poly, "offset_m": 15.0, "direction": "cw", "revisit_s": 240,
                               "n_vehicles": "auto", "alt_m": round(alt, 1)},
                    "common": dict(common_patrol)}
        if ttype == "area_guard":
            hull = _ccw_hull(target_xy)
            poly = _pad_polygon(hull, 20.0)
            return {"type": ttype, "label": label,
                    "params": {"polygon": poly, "target_class": "person", "level_req": "R", "revisit_s": 600,
                               "perimeter_offset_m": 10.0},
                    "common": dict(common_relay)}
        raise ValueError(ttype)

    tasks.append(make_task(t_types[0], label0))
    tasks.append(make_task(t_types[1], label1))

    # ---- 几何复核：机巢到任务区域的转场与任务几何本身都不得进入禁入体、不得贴近障碍物
    # （与引擎任务装配同一判据 `path_valid`；转场段用任务巡航高度做保守近似，不是实际爬升航迹）
    def _nest_for(task: dict) -> np.ndarray:
        nid = ((task.get("common") or {}).get("allowed_nests") or [None])[0]
        return nest_xy_by_id.get(nid, nest_xy[0])

    for task in tasks:
        p, ttype = task["params"], task["type"]
        n_xy = _nest_for(task)[None, :]
        if ttype == "polyline_patrol":
            chain = np.array([w[:2] for w in p["waypoints"]], dtype=np.float64)
            full = np.concatenate([n_xy, chain, chain[:1]], axis=0)  # 机巢 → wp0..wpN → 回 wp0（mode=loop）
            ok, reason = geo.path_ok(full, p["alt_m"], closed=False)
        elif ttype in ("area_patrol", "perimeter_patrol", "area_guard"):
            poly = np.array(p["polygon"], dtype=np.float64)
            full = np.concatenate([n_xy, poly, poly[:1]], axis=0)
            # area_guard 的 schema 没有 alt_m（巡航高度由引擎自行决定）：复核时按同一安全高度公式现算一个，
            # 不是写进任务参数的值
            alt = p["alt_m"] if "alt_m" in p else geo.safe_alt_m(_with_transit(((task.get("common") or {}).get(
                "allowed_nests") or ["nest-a"])[0], poly))
            ok, reason = geo.path_ok(full, alt, closed=False)
        elif ttype == "point_watch":
            pt = np.array([p["point"][:2]], dtype=np.float64)
            full = np.concatenate([n_xy, pt], axis=0)
            ok, reason = geo.path_ok(full, p["alt_m"], closed=False)
        elif ttype == "relay_watch":
            tgt = next(t for t in targets if t["id"] == p["target"])
            pt = np.array([tgt["pos"][:2]], dtype=np.float64)
            full = np.concatenate([n_xy, pt], axis=0)
            alt = geo.safe_alt_m(_with_transit(((task.get("common") or {}).get("allowed_nests") or ["nest-a"])[0], pt))
            ok, reason = geo.path_ok(full, alt, closed=False)
        else:
            ok, reason = True, ""
        if not ok:
            raise GeoRetry(f"{city}/{ttype}: path_valid failed ({reason})")

    doc = {
        "schema": "awr.sandbox.snapshot.v1", "contracts": CONTRACTS, "world_id": city,
        "template_id": f"d22-{city}-watch", "calendar": dict(CALENDAR), "acoustic_bg": "urban",
        "seed": plan["seed"], "env": {"preset": "clear", "overrides": {}},
        "nests": nests, "vehicles": vehicles, "targets": targets, "tasks": tasks,
    }
    return doc


def engine_feasible(city: str, doc: dict[str, Any]) -> list[str]:
    """用真实引擎（子进程，`tools/sim/_engine_check.py`）核验任务是否真的可分解、可运行：几何复核只挡得住本脚本
    认识的禁入圆与障碍物算法，任务分解阶段对运动识别物的预测包络外扩（`TaskingRuntime.motion_hull`）不易手工复刻，
    直接跑一遍最准。返回非 INFEASIBLE/FAILED/CANCELED 之外的问题任务 id 列表（空为全部可行）。"""
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(doc, f)
        cfg_path = f.name
    try:
        r = subprocess.run([sys.executable, "-m", "tools.sim._engine_check", city, cfg_path, "--seconds", "4"],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return [f"_engine_check exited {r.returncode}: {r.stderr[-800:]}"]
        out = json.loads(r.stdout.strip().splitlines()[-1])
        return list(out.get("infeasible") or [])
    finally:
        Path(cfg_path).unlink(missing_ok=True)


def build_city_validated(city: str, *, max_attempts: int = 80, start_attempt: int = 0) -> dict[str, Any]:
    """`start_attempt` 让调用方取一段不与之前尝试过的种子重叠的区间（`tools/sim/capture_city_watch.py` 在真实录制中
    发现任务终态化、需要换一批候选点重来时用；一次生成通常只用默认的 `start_attempt=0`）。D2.2-FX-world：默认
    `max_attempts` 由 30 提到 80——多数攻击只在几何复核（`GeoRetry`，近乎即时）落败，只有通过几何复核的候选才会跑
    真实引擎复核（`engine_feasible`，每次约几秒），30 次对 area_guard/perimeter_patrol 这类多边形任务偏紧（shenzhen
    改配 area_guard 后在 30 次内未必能找到几何合法的候选，见 docs/impl/D2.2-FX-world-报告.md）。"""
    geo = CityGeo(city)
    plan = CITY_PLAN[city]
    last: Exception | None = None
    for attempt in range(start_attempt, start_attempt + max_attempts):
        try:
            doc = build_city(city, seed=plan["seed"] + attempt * 911, geo=geo)
        except (GeoRetry, RuntimeError) as e:
            last = e
            continue
        bad = engine_feasible(city, doc)
        if not bad:
            return doc
        last = GeoRetry(f"{city}: 引擎判定任务不可行 {bad}")
    raise RuntimeError(f"{city}: 在第 {start_attempt} 至 {start_attempt + max_attempts - 1} 次尝试内未找到几何合法且"
                       f"引擎判定可行的点位组合") from last


def dumps_canonical(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.sim.gen_city_watch")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--city", nargs="*", choices=CITIES, default=list(CITIES))
    a = ap.parse_args(argv)
    root = ROOT / "scenarios"
    diffs = []
    for city in a.city:
        doc = build_city_validated(city)
        text = dumps_canonical(doc)
        p = root / f"d22-{city}-watch.json"
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            diffs.append(p.name)
            if not a.check:
                p.write_text(text, encoding="utf-8")
        n_v, n_t, n_k = len(doc["vehicles"]), len(doc["targets"]), len(doc["tasks"])
        print(f"{city}: vehicles={n_v} targets={n_t} tasks={n_k} types={[t['type'] for t in doc['tasks']]}")
    for d in diffs:
        print(f"{'differs' if a.check else 'wrote'}: scenarios/{d}")
    if a.check and diffs:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
