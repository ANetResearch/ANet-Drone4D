"""D2.2-W2：六城监测剧本（`scenarios/d22-<city>-watch.json`，`tools/sim/gen_city_watch.py` 生成）的静态校验：
schema（`awr.sandbox.snapshot.v1`）、计数门槛（4 架以上无人机、6 个以上目标物、2 个以上任务）、任务类型六城互不相同且合计覆盖
全部六种任务类型、目标物与机巢的地理合理性（开阔地、不在禁飞/限制区、彼此与边界有最小间距）、任务几何的高度余量（沿线最近障碍
高程之上有安全余量）。几何校验直接用引擎同一套 `awr.world.geometry.query`，不依赖生成器自身的实现，需要已构建的世界包
（`make worlds`），缺失时跳过（`needs_data`）。

D2.2-FX-world：`relay_watch`（接力监视）原来用 area_guard 的机型组合（awr_v1/nest-b 垂起机），`test_relay_watch_with_a_
vtol_model_is_infeasible_in_a_real_world` 用真实引擎给出这个机型组合本身（不是某城市或某选点）不可行的复现证据；
`tools/sim/gen_city_watch.py` 的 `make_task` 已改用 p600_mid360/nest-a（与巡线等任务同一机型），shenzhen 的 relay_watch
现在用这个机型组合，六城任务类型合计仍覆盖全部六种。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from awr.datasets.scenarios.geometry import point_in_ring
from awr.sandbox.snapshot import check_snapshot
from awr.world.geometry.query import open_world_query

ROOT = Path(__file__).resolve().parents[2]
CITIES = ("shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco")
OPEN_EPS_M = 1.5
LIMITS = {"vehicles_max": 12, "targets_max": 30, "nests_max": 4, "tasks_active_max": 6, "cloned_models_max": 8,
          "snapshot_kib": 512}
ALL_TASK_TYPES = {"point_watch", "polyline_patrol", "area_patrol", "perimeter_patrol", "relay_watch", "area_guard"}

pytestmark = pytest.mark.needs_data


def _doc(city: str) -> dict:
    return json.loads((ROOT / "scenarios" / f"d22-{city}-watch.json").read_text(encoding="utf-8"))


def _skip_if_missing(city: str) -> None:
    if not (ROOT / "worlds" / city / "world.json").exists():
        pytest.skip(f"worlds/{city} 未构建（make worlds）")


@pytest.fixture(scope="module", params=CITIES)
def city(request) -> str:
    _skip_if_missing(request.param)
    return request.param


@pytest.fixture(scope="module")
def doc(city: str) -> dict:
    return _doc(city)


@pytest.fixture(scope="module")
def wq(city: str):
    return open_world_query(ROOT / "worlds" / city)


def test_file_exists_for_every_city() -> None:
    for c in CITIES:
        assert (ROOT / "scenarios" / f"d22-{c}-watch.json").exists(), c


def test_schema_and_limits(doc: dict, city: str) -> None:
    r = check_snapshot(doc, world=city, limits=LIMITS, max_kib=512)
    assert r is None, r
    assert doc["world_id"] == city
    assert doc["schema"] == "awr.sandbox.snapshot.v1"


def test_minimum_counts(doc: dict) -> None:
    assert len(doc["vehicles"]) >= 4, "需要 4 架以上无人机"
    assert len(doc["targets"]) >= 6, "需要 6 个以上目标物"
    assert len(doc["tasks"]) >= 2, "需要 2 个以上任务"
    assert len(doc["nests"]) >= 1


def test_task_types_vary_within_city(doc: dict) -> None:
    types = [t["type"] for t in doc["tasks"]]
    assert len(set(types)) == len(types), f"同城任务类型应互不相同：{types}"


def test_task_types_cover_all_six_across_cities() -> None:
    seen: set[str] = set()
    for c in CITIES:
        seen.update(t["type"] for t in _doc(c)["tasks"])
    assert seen == ALL_TASK_TYPES, f"六城任务类型应合计覆盖全部六种，缺：{ALL_TASK_TYPES - seen}"


def _in_restricted(wq, xy: np.ndarray) -> np.ndarray:
    zi = wq.zones
    out = np.zeros(len(xy), dtype=bool)
    for prism in [*zi.nofly, *zi.restricted]:
        for poly in prism.polygons:
            out |= point_in_ring(xy, np.asarray(poly[0], dtype=np.float64))
    return out


def test_nests_on_open_ground_outside_restricted_zones(doc: dict, wq) -> None:
    xy = np.array([[n["pos"][0], n["pos"][1]] for n in doc["nests"]], dtype=np.float64)
    dtm, dsm = wq.ground_dtm(xy), wq.height_dsm(xy)
    assert np.all(dsm - dtm < OPEN_EPS_M), "机巢必须落在开阔地（不在建筑物或树冠之下）"
    assert not np.any(_in_restricted(wq, xy)), "机巢不得落在禁飞/限制区内"


def test_targets_on_open_ground_not_floating_not_inside_buildings(doc: dict, wq) -> None:
    xy = np.array([[t["pos"][0], t["pos"][1]] for t in doc["targets"]], dtype=np.float64)
    dtm, dsm = wq.ground_dtm(xy), wq.height_dsm(xy)
    assert np.all(dsm - dtm < OPEN_EPS_M), "目标物必须落在开阔地：街道、广场、空地等可行走/可行驶区域"
    assert not np.any(_in_restricted(wq, xy)), "目标物不得落在禁飞/限制区内"
    for t in doc["targets"]:
        assert t["pos"][2] is None, "目标物 z 必须为 null，由仿真按地形计算（不悬空、不嵌地）"


def test_targets_and_nests_minimum_spacing(doc: dict) -> None:
    pts = [tuple(n["pos"][:2]) for n in doc["nests"]] + [tuple(t["pos"][:2]) for t in doc["targets"]]
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
            assert d >= 15.0, f"间距过近：{pts[i]} 与 {pts[j]} 相距 {d:.1f} m"
    nest_xy = [tuple(n["pos"][:2]) for n in doc["nests"]]
    if len(nest_xy) >= 2:
        d = math.hypot(nest_xy[0][0] - nest_xy[1][0], nest_xy[0][1] - nest_xy[1][1])
        assert d >= 40.0, "两机巢间距过近"


def _task_geometry_xy(task: dict) -> np.ndarray | None:
    p, ttype = task["params"], task["type"]
    if ttype == "polyline_patrol":
        return np.array([[w[0], w[1]] for w in p["waypoints"]], dtype=np.float64)
    if ttype in ("area_patrol", "perimeter_patrol", "area_guard"):
        return np.array(p["polygon"], dtype=np.float64)
    if ttype == "point_watch":
        return np.array([[p["point"][0], p["point"][1]]], dtype=np.float64)
    return None  # relay_watch：几何由目标物给出，不在任务参数中


def test_task_geometry_within_world_bounds(doc: dict, wq) -> None:
    b = wq.bounds_m
    for task in doc["tasks"]:
        xy = _task_geometry_xy(task)
        if xy is None:
            continue
        assert np.all(xy[:, 0] >= b[0][0]) and np.all(xy[:, 0] <= b[1][0]), task["type"]
        assert np.all(xy[:, 1] >= b[0][1]) and np.all(xy[:, 1] <= b[1][1]), task["type"]


def test_task_altitude_clears_nearby_obstacles(doc: dict, wq) -> None:
    """任务高度（AGL）在其几何沿线采样点，相对最近障碍高程应有正余量：这是生成脚本提交给任务规划器的参数健全性检查，
    不是实际航线的避障证明——实际避障由既有的 Grid25 / A* / los_batch 规划器在运行时完成（与 S0–S7 同一套）。"""
    for task in doc["tasks"]:
        xy = _task_geometry_xy(task)
        alt_m = task["params"].get("alt_m")
        if xy is None or alt_m is None:
            continue
        obstacle_abs = wq.column_max_within(xy, radius_m=15.0)
        ground = wq.ground_dtm(xy)
        needed = float(np.max(obstacle_abs - ground))
        assert alt_m >= needed, f"{task['type']}: alt_m={alt_m} < 最近障碍需要的 {needed:.1f} m"
        assert 5.0 <= alt_m <= 500.0


def test_relay_and_point_watch_targets_exist(doc: dict) -> None:
    ids = {t["id"] for t in doc["targets"]}
    for task in doc["tasks"]:
        if task["type"] == "relay_watch":
            assert task["params"]["target"] in ids
        if task["type"] == "point_watch":
            look = task["params"].get("look") or {}
            if look.get("mode") == "target":
                assert look["target"] in ids


KEEPOUT_MARGIN_M = 20.3  # awr.sim.tasking.runtime.TaskingRuntime.margin_m()（RTK 未启用），见 tools/sim/gen_city_watch.py


def _target_keepout_circles(doc: dict) -> list[tuple[float, float, float]]:
    out = []
    for t in doc["targets"]:
        r = max(s["radius_max_m"] for s in t["sensitivity"]) + KEEPOUT_MARGIN_M
        out.append((float(t["pos"][0]), float(t["pos"][1]), float(r)))
    return out


def test_point_watch_and_polyline_outside_target_keepout_circles(doc: dict) -> None:
    """定点监视的悬停点、巡线的航点都不得落在任一目标物的禁入圆内（`awr.sim.tasking.runtime.TaskingRuntime
    ._precheck_sync`，码 583）：这正是让观测保持在目标物感知范围之外的既有判据，不是本脚本另加的限制。"""
    circles = _target_keepout_circles(doc)
    for task in doc["tasks"]:
        p, ttype = task["params"], task["type"]
        pts = None
        if ttype == "point_watch":
            pts = [p["point"][:2]]
        elif ttype == "polyline_patrol":
            pts = [w[:2] for w in p["waypoints"]]
        if pts is None:
            continue
        for x, y in pts:
            for cx, cy, r in circles:
                d = math.hypot(x - cx, y - cy)
                assert d >= r, f"{ttype} 的点 ({x},{y}) 落在目标物禁入圆内（距中心 {d:.1f} m < {r:.1f} m）"


def test_nest_slot_grid_matches_spawn_z_window(doc: dict, wq) -> None:
    """机巢各槛位（3 列栅格、间距 5 m，`awr.sim.tasking.nests.slot_offsets`）的 DSM 都要落在出生点核验窗口内
    （`awr.sim.runtime.main` SPAWN_Z：`nest.pos.z - 0.5 <= dsm_slot <= nest.pos.z + 0.05`），否则部分槛位的
    无人机在真实引擎里直接装配失败（M18/M08 NO_LOADER 之外的另一类问题，静态几何校验不会覆盖，这里专门补上）。"""
    from awr.sim.tasking.nests import slot_offsets

    slots_by_kind = {"box": 6, "vtol_pad": 2}
    for n in doc["nests"]:
        cx, cy, cz = n["pos"]
        offs = slot_offsets(slots_by_kind.get(n["kind"], 6))
        xy = np.asarray([[cx + dx, cy + dy] for dx, dy in offs], dtype=np.float64)
        dsm = wq.height_dsm(xy)
        assert np.all(dsm <= cz + 0.05) and np.all(dsm >= cz - 0.5), \
            f"{n['id']}: 槛位 DSM 超出出生点核验窗口 {dsm.tolist()} vs z={cz}"


def test_vehicle_models_and_nest_refs(doc: dict) -> None:
    nest_ids = {n["id"] for n in doc["nests"]}
    for v in doc["vehicles"]:
        assert v["nest_id"] in nest_ids
        assert v["model_id"] in ("p600_mid360", "awr_v1", "awr_q1", "awr_q9", "awr_f1", "x500", "x500_sih")


def test_relay_watch_with_a_vtol_model_is_infeasible_in_a_real_world() -> None:
    """D2.2-FX-world 复现证据（机型选择的坑，不是 relay_watch 这个任务类型本身的限制）：`relay_watch` 原来复用
    area_guard 的机型组合（`scenarios/s7-synthcity-relay.json` 的 nest-b、awr_v1 垂起机，level_req=R），这个组合
    在真实带建筑点云的世界里，站位搜索（`python/awr/sim/tasking/relay.py` 近距离环搜索）几乎找不到满足视线的落点
    （真实判定 code 581、why=los）——这里直接复用 synthcity 已验证可行的 area_guard 的目标点位与机型组合（该任务
    在六城监测功能之前就是 S7 公开演示的既有验收内容），只把任务类型换成 relay_watch，其余参数原样不变，真实引擎
    仍判不可行，说明问题出在机型（awr_v1 的站位环搜索），不是某城市、某个选点或 relay_watch 这个任务类型本身——
    换成 p600_mid360/nest-a（与巡线等任务同一机型）后用真实引擎复核在 shenzhen 的七个候选目标物位置里有两个可行
    （见 `tools/sim/gen_city_watch.py` 模块说明、`make_task` 里 relay_watch 分支的注释），`tools/sim/gen_city_watch.py`
    `make_task` 已据此改用 `common_patrol`，不再是 `common_relay`；下一条用例给出换机型后可行的正例。

    若 awr_v1 这个机型组合的站位搜索将来被 M20 接力任务的工作包修复，本用例会失败（`engine_feasible` 返回空列表），
    那时把这条记录为"已修复"即可，不代表要改动 `make_task` 现在用的 `common_patrol`。"""
    if not (ROOT / "worlds" / "synthcity" / "world.json").exists():
        pytest.skip("worlds/synthcity 未构建（make worlds）")
    from tools.sim.gen_city_watch import engine_feasible

    base = json.loads((ROOT / "scenarios" / "s7-synthcity-relay.json").read_text(encoding="utf-8"))
    area_guard = next(t for t in base["tasks"] if t["type"] == "area_guard")
    assert area_guard["common"]["allowed_models"] == ["awr_v1"]
    assert area_guard["common"]["allowed_nests"] == ["nest-b"]
    doc = dict(base)
    doc["tasks"] = [{"type": "relay_watch", "label": "接力监视（复现用，验证 awr_v1 的机型限制）",
                      "params": {"target": "person-01", "level_req": area_guard["params"]["level_req"], "duration_s": None},
                      "common": dict(area_guard["common"])}]
    bad = engine_feasible("synthcity", doc)
    assert bad, ("relay_watch 配 awr_v1/nest-b，用 synthcity 的 area_guard 已验证可行的目标点位仍应判定不可行；如果"
                "这个断言失败，说明 awr_v1 的站位搜索限制已被修复")


def test_relay_watch_with_a_multirotor_model_can_be_feasible_in_a_real_world() -> None:
    """换成 p600_mid360/nest-a（`tools/sim/gen_city_watch.py` `make_task` 现在给 relay_watch 用的机型组合，与
    `apps/web/perf/d2/integration-live.spec.ts` D2INT_S7_RELAY_PART=1 这条既有、已验收的实景回归同一机型组合）后，
    同一个 person-01 目标点位真实引擎判定可行——证明上一条用例的不可行是 awr_v1 这个机型的站位搜索能力问题，不是
    relay_watch 这个任务类型与真实建筑几何不兼容。"""
    if not (ROOT / "worlds" / "synthcity" / "world.json").exists():
        pytest.skip("worlds/synthcity 未构建（make worlds）")
    from tools.sim.gen_city_watch import engine_feasible

    base = json.loads((ROOT / "scenarios" / "s7-synthcity-relay.json").read_text(encoding="utf-8"))
    doc = dict(base)
    doc["nests"] = [n for n in base["nests"] if n["id"] == "nest-a"]
    doc["vehicles"] = [v for v in base["vehicles"] if v["nest_id"] == "nest-a"][:3]
    doc["targets"] = [t for t in base["targets"] if t["id"] == "person-01"]
    doc["tasks"] = [{"type": "relay_watch", "label": "接力监视（复现用，验证换机型后可行）",
                      "params": {"target": "person-01", "level_req": "R", "duration_s": None},
                      "common": {"priority": "normal", "on_detect": "notify", "allowed_nests": ["nest-a"],
                                "allowed_models": ["p600_mid360"]}}]
    bad = engine_feasible("synthcity", doc)
    assert not bad, f"relay_watch 配 p600_mid360/nest-a 应判定可行，实际不可行：{bad}"
