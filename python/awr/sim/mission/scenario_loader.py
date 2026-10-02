"""剧本加载器（M10-FR-062；AWR-16 §12；AWR-12 §3.3.6、§7.1）。

`load_scenario(sid, profile, world, profiles)`：读取 `scenarios/<sid>.json`（`AWR_SCENARIOS_DIR` 可覆盖）→ `profiles`
深合并（对象逐键递归，数组与标量整体替换，profiles 不可嵌套）→ 执行 V-SC-01 至 V-SC-11（失败抛 `ScenarioError`，
对外为 121 SCENARIO_INVALID，detail 为规则号；V-SC-11 按 16 §12.6 为告警）→ 展开 `vehicle_sets`（id 为 `<id_prefix>-<序号>`，布局 grid 或 ring，
`mission.center = "home"` 以出生点为中心实例化生成器）。V-SC-12（能量预检）在任务启动时执行（119，不是 121）；
V-SC-13（剧本目录）为静态校验（M16）；V-SC-14（编组同时段最小间距，ADR-062）在展开后执行（121）。

`apply_scenario(rt, sc)`：倍速、机群（移除不在剧本中的骨架机体 → 下一次 stage 起 `fleet/add` 剧本机体）、任务
（`MissionEngine.create`，origin scenario，能量预检策略取 `energy_precheck`）、导演（事件与成功谓词）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "ACTIONS",
    "BOOL_METRICS",
    "METRICS",
    "LoadedScenario",
    "ScenarioError",
    "apply_scenario",
    "deep_merge",
    "expand_vehicle_sets",
    "load_scenario",
    "scenario_dirs",
    "scenario_path",
    "validate_doc",
    "warm_validators",
]

ROOT = Path(__file__).resolve().parents[4]
METRICS = ("elapsed_s", "missions_done", "mission_progress", "facade_coverage", "area_coverage", "min_separation_m",
           "guard_events", "pos_err_max_m", "energy_rtl_count", "battery_soc_min", "flight_state", "agl_m", "landed_all",
           "landed_home_err_m", "formation_err_rms_m", "agl_min_m", "agl_rms_err_m", "target_confidence", "t_conf_s",
           "link_quality_min")
BOOL_METRICS = ("missions_done", "landed_all")
ACTIONS = ("env.preset", "env.set", "env.gust", "vehicle.add", "vehicle.remove", "mission.start", "mission.pause",
           "mission.abort", "cmd", "mark", "target.spawn", "agent.task", "fault.inject")
OPS = ("<", "<=", "==", "!=", ">=", ">")
MAX_VEHICLES = 1000


class ScenarioError(ValueError):
    def __init__(self, rule: str, detail: Any = None) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail
        self.code = 121


@dataclass
class LoadedScenario:
    scenario_id: str
    doc: dict                              # 合并 profile 后的剧本
    sha256: str                            # 文件字节
    profile: str | None
    vehicles: list[dict]                   # vehicles[] 与 vehicle_sets 展开后的机体
    missions: list[dict]                   # missions[] 与 vehicle_sets[].mission 展开后的任务
    zones_active: list[str] | None
    warnings: list[str] = field(default_factory=list)


def scenario_dirs() -> list[Path]:
    out = []
    env = os.environ.get("AWR_SCENARIOS_DIR")
    if env:
        out.append(Path(env))
    out.append(ROOT / "scenarios")
    return out


def scenario_path(sid: str) -> Path | None:
    if not isinstance(sid, str) or "/" in sid or sid.startswith("."):
        return None
    for d in scenario_dirs():
        p = d / f"{sid}.json"
        if p.is_file():
            return p
    return None


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _registry():
    from awr.sim.schemas import contracts_registry  # 与机型、传感器校验共用一次扫描（ADR-061）

    return contracts_registry()


@cache
def _scenario_schema() -> dict:
    from awr.contracts._paths import contracts_root

    return json.loads((contracts_root() / "scenario" / "scenario.schema.json").read_text(encoding="utf-8"))


@cache
def _validator():
    from jsonschema import Draft202012Validator

    return Draft202012Validator(_scenario_schema(), registry=_registry())


@cache
def gen_validator(name: str):
    """`scenario.schema.json#/$defs/gen_<name>` 的独立校验器（R25、R26 参数校验复用）。"""
    from jsonschema import Draft202012Validator

    sch = _scenario_schema()
    sub = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": sch["$id"],
           "$ref": f"#/$defs/gen_{name}", "$defs": sch["$defs"]}
    return Draft202012Validator(sub, registry=_registry())


def warm_validators() -> None:
    """构造剧本校验器并做一次空校验（jsonschema 按需编译子 schema），供 sim-core 热备用进程预热（ADR-070）。"""
    v = _validator()
    for _ in v.iter_errors({}):
        break


@cache
def _presets() -> frozenset[str]:
    from awr.contracts._paths import contracts_root

    try:
        d = json.loads((contracts_root() / "env" / "presets.json").read_text(encoding="utf-8"))
        return frozenset(str(p.get("id")) for p in d.get("presets", []))
    except (OSError, json.JSONDecodeError):
        return frozenset()


# ------------------------------------------------------------------------------------------------ vehicle_sets
def expand_vehicle_sets(doc: dict, world: Any = None) -> tuple[list[dict], list[dict]]:
    vehicles = [dict(v) for v in doc.get("vehicles") or []]
    missions = [dict(m) for m in doc.get("missions") or []]
    for vs in doc.get("vehicle_sets") or []:
        n = int(vs["count"])
        prefix = str(vs.get("id_prefix") or vs.get("set_id") or "sim")
        start = int(vs.get("id_start", 1))
        digits = int(vs.get("id_digits", 4))
        lay = dict(vs.get("layout") or {"kind": "grid"})
        org = list(lay.get("origin_enu_m") or [0.0, 0.0, None])
        sp = float(lay.get("spacing_m", 12.0))
        pts = []
        if lay.get("kind") == "ring":
            R = float(lay.get("radius_m", max(sp * n / (2 * math.pi), sp)))
            for k in range(n):
                a = 2 * math.pi * k / n
                pts.append((org[0] + R * math.cos(a), org[1] + R * math.sin(a)))
        else:
            cols = int(lay.get("cols") or math.ceil(math.sqrt(n)))
            stagger = float(lay.get("stagger_m", 0.0))
            for k in range(n):
                r, c = divmod(k, cols)
                pts.append((org[0] + c * sp + (stagger if r % 2 else 0.0), org[1] + r * sp))
        ids = []
        z = lay.get("z_m", org[2] if len(org) > 2 else None)
        for k, (x, y) in enumerate(pts):
            vid = f"{prefix}-{start + k:0{digits}d}"
            ids.append(vid)
            vehicles.append({"vehicle_id": vid, "profile_id": vs.get("profile_id", "p600_mid360"),
                             "backend": vs.get("backend", "mock"), "home_enu_m": [x, y, z],
                             "initial_soc": vs.get("initial_soc", 1.0), "speed_profile": vs.get("speed_profile"),
                             "_set": vs.get("set_id")})
        mi = vs.get("mission")
        if mi:
            params = dict(mi.get("params") or {})
            if mi.get("center", "home") == "home":
                params.setdefault("center_enu_m", "home")
            mm = {"mission_id": f"{vs.get('set_id') or prefix}-mission", "vehicle_ids": ids, "generator": mi["generator"],
                  "params": params, "on_done": mi.get("on_done", "rtl")}
            if mi.get("start"):
                mm["start"] = mi["start"]
            missions.append(mm)
    return vehicles, missions


# ------------------------------------------------------------------------------------------------ V-SC-14（ADR-062）
SEP_FLOOR_M = 10.0          # FleetGuard 告警线（ADR-026、12 §5.10）：guard_events = 0 的前提
SEP_CLIMB_MPS = 1.0         # 时段估计用的保守垂直速度（爬升与返航下降都按 1 m/s 计，时段偏长即偏保守）
SEP_WINDOW_PAD_S = 120.0


def required_separation_m(doc: dict) -> float:
    """V-SC-14 的要求值：max(FleetGuard 告警线 10 m, 成功谓词中 `min_separation_m >= / > v` 的最大 v)。"""
    req = SEP_FLOOR_M

    def walk(p: Any) -> None:
        nonlocal req
        if isinstance(p, dict):
            if p.get("metric") == "min_separation_m" and p.get("op") in (">=", ">") and isinstance(p.get("value"), (int, float)):
                req = max(req, float(p["value"]))
            for k in ("all", "any"):
                for x in p.get(k) or []:
                    walk(x)
    walk(doc.get("success") or {})
    return req


def set_separation_violation(vehicles: list[dict], missions: list[dict], required_m: float, world: Any = None) -> str | None:
    """编组（vehicle_sets 展开）机体两两三维距离的几何下界（ADR-062）；低于 `required_m` 时返回说明，否则 None。

    每架机的占位 = 出生点正上方的竖直线段 [z_home, z_orb] 并上高度 z_orb、半径 r 的绕飞圆（orbit 生成器以出生点为圆心；其余
    生成器 r 取 0、z_orb 取出生高度，只作地面占位）。竖直段对应起飞爬升与返航下降（M10 先原地爬升再入圆，返航先回到出生点
    上方再下降），与时序无关，因此下界对任意起飞错时与收尾先后都成立：
      段–段：竖直区间相交时为水平距离 d，否则 hypot(d, 区间间隙)；
      段–圆：圆所在高度落在对方竖直区间内时为 d − r，否则 hypot(d − r, 到区间端点的垂直距离)；
      圆–圆：hypot(max(d − r_i − r_j, 0), z_orb,i − z_orb,j)。
    "同时段"：任务时段 [start.at_s, start.at_s + T] 相交的机对才检查，T 按 1 m/s 的爬升与下降、orbit 圈数与速度加 120 s 保守估计；
    `start.after` 或未给圈数时视为与所有机体同时段。有世界时 z 取 DSM（出生点）与 DTM + agl_m（绕飞高度），否则按平地。"""
    idx = [k for k, v in enumerate(vehicles) if v.get("_set") is not None]
    if len(idx) < 2:
        return None
    by_vid: dict[str, dict] = {}
    for m in missions:
        for vid in m.get("vehicle_ids") or []:
            by_vid.setdefault(vid, m)
    V = [vehicles[k] for k in idx]
    H = np.asarray([[float(v["home_enu_m"][0]), float(v["home_enu_m"][1])] for v in V], np.float64)
    zh = np.asarray([float(v["home_enu_m"][2]) if len(v["home_enu_m"]) > 2 and v["home_enu_m"][2] is not None else np.nan
                     for v in V], np.float64)
    ground = np.zeros(len(V))
    if world is not None:
        try:
            dsm = np.asarray(world.height_dsm(H), np.float64)
            ground = np.asarray(world.ground_dtm(H), np.float64)
            zh = np.where(np.isnan(zh), dsm, zh)
        except Exception:
            pass
    zh = np.where(np.isnan(zh), ground, zh)
    r = np.zeros(len(V))
    zo = zh.copy()
    t0 = np.zeros(len(V))
    t1 = np.full(len(V), np.inf)
    for k, v in enumerate(V):
        m = by_vid.get(v["vehicle_id"]) or {}
        p = m.get("params") or {}
        st = m.get("start") or {}
        if m.get("generator") == "orbit":
            r[k] = float(p.get("radius_m") or 0.0)
            if p.get("z_m") is not None:
                zo[k] = float(p["z_m"])
            elif p.get("agl_m") is not None:
                zo[k] = ground[k] + float(p["agl_m"])
            turns, spd = float(p.get("turns") or 0.0), float(p.get("speed_mps") or 0.0)
            if st.get("after") is None:
                t0[k] = float(st.get("at_s") or 0.0)
                if turns > 0 and spd > 0:
                    t1[k] = t0[k] + 2.0 * abs(zo[k] - zh[k]) / SEP_CLIMB_MPS + 2 * math.pi * r[k] * turns / spd + SEP_WINDOW_PAD_S
    lo, hi = np.minimum(zh, zo), np.maximum(zh, zo)
    for i in range(len(V) - 1):
        j = np.arange(i + 1, len(V))
        d = np.hypot(H[j, 0] - H[i, 0], H[j, 1] - H[i, 1])
        near = (d < required_m + r[i] + r[j] + 1e-9) & (t0[j] <= t1[i]) & (t0[i] <= t1[j])
        if not near.any():
            continue
        j, d = j[near], d[near]
        gap = np.maximum(0.0, np.maximum(lo[j] - hi[i], lo[i] - hi[j]))
        seg = np.hypot(d, gap)
        dz_ij = np.maximum(0.0, np.maximum(lo[i] - zo[j], zo[j] - hi[i]))      # 机 j 的圆到机 i 的竖直段
        dz_ji = np.maximum(0.0, np.maximum(lo[j] - zo[i], zo[i] - hi[j]))
        s_ij = np.hypot(np.maximum(d - r[j], 0.0), dz_ij)
        s_ji = np.hypot(np.maximum(d - r[i], 0.0), dz_ji)
        cc = np.hypot(np.maximum(d - r[i] - r[j], 0.0), zo[i] - zo[j])
        b = np.minimum(np.minimum(seg, cc), np.minimum(s_ij, s_ji))
        k = int(np.argmin(b))
        if b[k] < required_m - 1e-9:
            a, c = V[i], V[int(j[k])]
            return (f"{a['vehicle_id']}({a.get('_set')}) and {c['vehicle_id']}({c.get('_set')}): separation lower bound "
                    f"{b[k]:.1f} m < {required_m:g} m (horizontal {d[k]:.1f} m, orbit radii {r[i]:g}/{r[int(j[k])]:g} m); "
                    f"increase home spacing (layout.spacing_m / set offsets) or separate the flight windows")
    return None


def orbit_yaw_violation(vehicles: list[dict], missions: list[dict], profiles: Any) -> str | None:
    """航向朝心（`yaw = center`，缺省）的 orbit 任务：有效速度（`speed_mps` 或巡航速度，再受限速与 √(3R) 钳制，与生成器
    同一公式）除以半径即所需偏航角速度，不得超过机体自动模式偏航上限（限速配置 yawrate 与 MPC_YAWRAUTO_MAX 的较小者）的
    `YAW_RATE_FRAC`；否则航向误差持续累积（M08 姿态环按上限钳制偏航角速度），ladder v1 的 3 m、2 m/s 即因此在约 35 s 后
    航向误差到 180° 附近并触发 TILT_ERR_KILL（ADR-062）。无机型表时跳过。"""
    if profiles is None:
        return None
    from awr.sim.fleet import kernels_l1 as K

    from .generators.orbit import YAW_RATE_FRAC

    by_id = {v["vehicle_id"]: v for v in vehicles}
    for m in missions:
        if m.get("generator") != "orbit":
            continue
        p = m.get("params") or {}
        if str(p.get("yaw", "center")) != "center":
            continue
        try:
            R = float(p["radius_m"])
        except (KeyError, TypeError, ValueError):
            continue
        if R <= 0:
            continue
        for vid in m.get("vehicle_ids") or []:
            v = by_id.get(vid)
            if v is None:
                continue
            try:
                pid = v.get("profile_id", "p600_mid360")
                lim = profiles.limits_index(v.get("speed_profile"), pid)
                LT = profiles.LT[lim]
            except Exception:
                continue
            speed = min(float(p.get("speed_mps") or LT[K.L_CRUISE]), float(LT[K.L_VXY]), math.sqrt(3.0 * R))
            ymax = min(float(LT[K.L_YAWRATE]), K.YAWRAUTO)
            if speed / R > YAW_RATE_FRAC * ymax + 1e-9:
                return (f"mission {m['mission_id']} vehicle {vid}: orbit yaw=center needs {math.degrees(speed / R):.1f} deg/s "
                        f"> {YAW_RATE_FRAC:.0%} of {pid} limit {math.degrees(ymax):.0f} deg/s; lower speed_mps, enlarge radius_m "
                        f"or use yaw=tangent")
    return None


# ------------------------------------------------------------------------------------------------ V-SC
def _depth_check(p: Any, depth: int = 1) -> None:
    if depth > 16:
        raise ScenarioError("V-SC-10", "predicate depth > 16")
    if not isinstance(p, dict):
        raise ScenarioError("V-SC-10", "predicate must be an object")
    for k in ("all", "any"):
        if k in p:
            items = p[k]
            if not isinstance(items, list) or not 1 <= len(items) <= 64:
                raise ScenarioError("V-SC-10", f"{k} must have 1..64 items")
            for x in items:
                _depth_check(x, depth + 1)
            return
    if "not" in p:
        _depth_check(p["not"], depth + 1)
        return
    m = p.get("metric")
    if m not in METRICS:
        raise ScenarioError("V-SC-10", f"unknown metric {m!r}")
    if p.get("op") not in OPS:
        raise ScenarioError("V-SC-10", f"unknown operator {p.get('op')!r}")
    if m in BOOL_METRICS and (p.get("op") not in ("==", "!=") or not isinstance(p.get("value"), bool)):
        raise ScenarioError("V-SC-10", f"bool metric {m} allows only == and != with a bool value")


def validate_doc(doc: dict, *, world: Any = None, profiles: Any = None, vehicles: list[dict] | None = None,
                 missions: list[dict] | None = None) -> list[str]:
    """V-SC-01 至 V-SC-11；返回告警列表，失败抛 ScenarioError。"""
    errs = sorted(_validator().iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    if errs:
        e = errs[0]
        raise ScenarioError("V-SC-01", f"/{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}")
    for m in doc.get("missions") or []:
        gv = gen_validator(m["generator"])
        ge = sorted(gv.iter_errors(m.get("params") or {}), key=lambda e: list(map(str, e.absolute_path)))
        if ge:
            raise ScenarioError("V-SC-01", f"/missions/{m['mission_id']}/params/{'/'.join(map(str, ge[0].absolute_path))}: "
                                           f"{ge[0].message[:160]}")
    # V-SC-02
    if world is not None:
        if doc["world_id"] != world.world_id:
            raise ScenarioError("V-SC-02", f"world_id {doc['world_id']} != session world {world.world_id}")
        cs = doc.get("world_coordinate_sha256")
        if cs is not None and cs != world.coordinate_sha256:
            raise ScenarioError("V-SC-02", "world_coordinate_sha256 mismatch")
    vehicles = vehicles if vehicles is not None else list(doc.get("vehicles") or [])
    missions = missions if missions is not None else list(doc.get("missions") or [])
    # V-SC-03
    if profiles is not None:
        for v in vehicles:
            pid = v.get("profile_id", "p600_mid360")
            if pid not in profiles.profiles:
                raise ScenarioError("V-SC-03", f"unknown profile_id {pid}")
            p = profiles.get(pid)
            sp = v.get("speed_profile")
            if sp is not None and sp not in p.limits:
                raise ScenarioError("V-SC-03", f"unknown speed_profile {sp} for {pid}")
            sens = (p.doc or {}).get("sensors") if isinstance(getattr(p, "doc", None), dict) else None
            for sname in v.get("sensors") or []:
                if isinstance(sens, dict) and sname not in sens:
                    raise ScenarioError("V-SC-03", f"unknown sensor {sname} for {pid}")
    # V-SC-04
    ids = [v["vehicle_id"] for v in vehicles]
    if len(ids) != len(set(ids)):
        dup = sorted({x for x in ids if ids.count(x) > 1})
        raise ScenarioError("V-SC-04", f"duplicate vehicle ids {dup[:5]}")
    if len(ids) > MAX_VEHICLES:
        raise ScenarioError("V-SC-04", f"{len(ids)} vehicles > {MAX_VEHICLES}")
    # V-SC-05
    if world is not None and vehicles:
        H = np.asarray([[float(v["home_enu_m"][0]), float(v["home_enu_m"][1])] for v in vehicles])
        inb = world.zones.border.contains_xy(H)
        if not bool(np.all(inb)):
            raise ScenarioError("V-SC-05", f"home outside border: {ids[int(np.argmin(inb))]}")
        for z in world.zones.nofly:
            ins = z.contains_xy(H)
            if bool(np.any(ins)):
                raise ScenarioError("V-SC-05", f"home inside nofly {z.zone_id}: {ids[int(np.argmax(ins))]}")
        dsm = np.asarray(world.height_dsm(H), np.float64)
        for v, d in zip(vehicles, dsm, strict=True):
            z = v["home_enu_m"][2] if len(v["home_enu_m"]) > 2 else None
            if z is not None and float(z) < float(d) - 0.05:
                raise ScenarioError("V-SC-05", f"home z below dsm: {v['vehicle_id']}")
    # V-SC-06
    idset = set(ids)
    mids = {m["mission_id"] for m in missions}
    if len(mids) != len(missions):
        raise ScenarioError("V-SC-06", "duplicate mission ids")
    after: dict[str, str] = {}
    for m in missions:
        for vid in m.get("vehicle_ids") or []:
            if vid not in idset:
                raise ScenarioError("V-SC-06", f"mission {m['mission_id']} references unknown vehicle {vid}")
        st = m.get("start") or {}
        if st.get("after") is not None:
            if st["after"] not in mids:
                raise ScenarioError("V-SC-06", f"start.after references unknown mission {st['after']}")
            after[m["mission_id"]] = st["after"]
    for m0 in after:
        seen, cur = set(), m0
        while cur in after:
            if cur in seen:
                raise ScenarioError("V-SC-06", f"start.after cycle at {m0}")
            seen.add(cur)
            cur = after[cur]
    # V-SC-07
    zmax = math.inf
    if world is not None:
        try:
            zmax = float(world.zones.border.zmax)
        except Exception:
            zmax = math.inf
    for m in missions:
        p = m.get("params") or {}
        zs = []
        if isinstance(p.get("z_m"), (int, float)):
            zs.append(float(p["z_m"]))
        if isinstance(p.get("z_range_m"), list):
            zs += [float(x) for x in p["z_range_m"]]
        for z in zs:
            if z > zmax + 1e-6:
                raise ScenarioError("V-SC-07", f"mission {m['mission_id']} altitude {z} > border max_z {zmax}")
        for k in ("agl_m",):
            if isinstance(p.get(k), (int, float)) and p[k] < 0:
                raise ScenarioError("V-SC-07", f"mission {m['mission_id']} {k} < 0")
        alt = p.get("altitude") or {}
        if isinstance(alt.get("agl_m"), (int, float)) and alt["agl_m"] < 0:
            raise ScenarioError("V-SC-07", f"mission {m['mission_id']} altitude.agl_m < 0")
    # V-SC-08
    pres = _presets()
    if pres:
        env = doc.get("env") or {}
        if env.get("preset") is not None and env["preset"] not in pres:
            raise ScenarioError("V-SC-08", f"unknown env preset {env['preset']}")
        for ev in doc.get("events") or []:
            if ev.get("action") == "env.preset" and (ev.get("args") or {}).get("preset") not in pres:
                raise ScenarioError("V-SC-08", f"event {ev.get('event_id')}: unknown preset")
    # V-SC-09
    if world is not None:
        for zid in (doc.get("zones") or {}).get("active") or []:
            if zid not in world.zones.by_id:
                raise ScenarioError("V-SC-09", f"unknown zone {zid}")
    # V-SC-10
    tl = float(doc.get("time_limit_s", 1800))
    for ev in doc.get("events") or []:
        if ev.get("action") not in ACTIONS:
            raise ScenarioError("V-SC-10", f"unknown action {ev.get('action')}")
        if ev.get("at_s") is not None and float(ev["at_s"]) > tl:
            raise ScenarioError("V-SC-10", f"event {ev.get('event_id')} at_s > time_limit_s")
        if ev.get("when") is not None:
            _depth_check(ev["when"])
        if ev.get("action") == "cmd":
            op = (ev.get("args") or {}).get("op")
            if op not in ("rtl", "land", "hover"):
                raise ScenarioError("V-SC-10", f"event {ev.get('event_id')}: cmd op {op} not allowed")
    _depth_check(doc["success"])
    # V-SC-14（ADR-062）：vehicle_sets 展开后，同时段在空中的编组机体两两间距的几何下界不低于要求
    bad = set_separation_violation(vehicles, missions, required_separation_m(doc), world)
    if bad is not None:
        raise ScenarioError("V-SC-14", bad)
    # V-SC-15（ADR-062）：航向朝心的 orbit，偏航角速度需求 v/R 不超过机体自动模式上限的 90%
    bad = orbit_yaw_violation(vehicles, missions, profiles)
    if bad is not None:
        raise ScenarioError("V-SC-15", bad)
    # V-SC-11（告警，16 §12.6：无意义配置；S1 的 ci profile 即 record = false 且保留 marked）
    warnings: list[str] = []
    if doc.get("record") is False and any(v.get("marked") for v in doc.get("vehicles") or []):
        warnings.append("V-SC-11: record = false with marked vehicles (marked ignored)")
    return warnings


def load_scenario(sid: str, *, profile: str | None = None, world: Any = None, profiles: Any = None,
                  doc: dict | None = None) -> LoadedScenario:
    if doc is None:
        p = scenario_path(sid)
        if p is None:
            raise ScenarioError("V-SC-00", f"scenario file not found: {sid}")
        raw = p.read_bytes()
        try:
            doc0 = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ScenarioError("V-SC-01", f"invalid JSON: {e}") from None
    else:
        doc0 = copy.deepcopy(doc)
        raw = json.dumps(doc0, sort_keys=True).encode()
    sha = hashlib.sha256(raw).hexdigest()
    profs = doc0.get("profiles") or {}
    if profile:
        if profile not in profs:
            raise ScenarioError("V-SC-01", f"unknown profile {profile}")
        over = profs[profile]
        if isinstance(over, dict) and "profiles" in over:
            raise ScenarioError("V-SC-01", "profiles must not nest")
        merged = deep_merge({k: v for k, v in doc0.items() if k != "profiles"}, over)
        merged["profiles"] = profs
    else:
        merged = doc0
    vehicles, missions = expand_vehicle_sets(merged, world)
    warnings = validate_doc(merged, world=world, profiles=profiles, vehicles=vehicles, missions=missions)
    active = (merged.get("zones") or {}).get("active")
    return LoadedScenario(str(merged["scenario_id"]), merged, sha, profile, vehicles, missions,
                          list(active) if active is not None else None, warnings)


# ------------------------------------------------------------------------------------------------ 应用
def apply_scenario(rt: Any, sc: LoadedScenario) -> None:
    """倍速与机群布设交给导演逐步执行（移除骨架机体需要一个 tick 生效），任务在剧本机体就位后创建。"""
    from .director import Director

    if rt.director is None:
        rt.director = Director(rt)
    rt.director.load(sc)
