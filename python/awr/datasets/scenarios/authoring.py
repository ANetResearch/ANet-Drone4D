"""剧本与 curated 区域的编写工具（M16-FR-029；M16 §6.2.4、§6.4、§7.3、§9.2）。

全部内置剧本（S1–S6、ladder、free、soak）、`catalog.json` 与六城 `zones/<id>.zones.geojson` 都由 `build_all()` 生成后
提交入库；同一输入两次生成逐字节一致（M16-AC-019）：浮点统一舍入、键序固定、`dumps_canonical` 的换行规则确定。

业务数值的定义方：S1 为 AWR-12 §7.2（ADR-052），S2–S6 为 AWR-12 §7.3 与 M16 §6.4.4–§6.4.7，ladder 为 M16 §6.4.8
（AWR-12 §7.4 已采用），语法为 AWR-16 §12。角度约定：`start_az_deg` 与 `entry_azimuth_deg` 以 +E 为 0、逆时针为正
（与 ENU 航向 ψ_enu 相同，AWR-03 §5.3）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from ..urbanscene3d.cities import CITIES, CITY_IDS, ZoneDef

__all__ = ["LADDER_AGL_M", "LADDER_CENTER", "LADDER_NS", "LADDER_START_S", "build_all", "catalog_doc", "circle_zone",
           "dumps_canonical", "free_scenario", "ladder_scenario", "ladder_sets", "ring", "s1_scenario", "soak_scenario",
           "square", "write_json_canonical", "zones_doc"]

SCHEMA = {"schema": "awr.scenario.v1", "schema_version": "1.0.0"}
ZERO_SHA = "0" * 64

# ---------------------------------------------------------------- ladder（M16 §6.4.8）
LADDER_CENTER = (-375.0, 20.0)
LADDER_SPACING_M = 24.0
LADDER_OFFSETS = ((0.0, 0.0), (12.0, 0.0), (0.0, 12.0), (12.0, 12.0))     # L0–L3 相对 L0 的偏移（12 m 交错格网）
LADDER_AGL_M = (60.0, 75.0, 90.0, 105.0)
LADDER_START_S = (15.0, 10.0, 5.0, 0.0)                                     # 高层先飞，错时 5 s
LADDER_NS = (10, 50, 100, 200, 500, 1000)
LADDER_ORBIT = {"radius_m": 3, "speed_mps": 2, "turns": 20, "cw": False, "yaw": "center"}


def _r(v: float, nd: int = 3) -> float:
    """确定性舍入：-0.0 归一为 0.0，整数值写成 int（JSON 中不出现 12.0 与 12 的差异）。"""
    x = round(float(v), nd) + 0.0
    return int(x) if x == int(x) and abs(x) < 1e15 else x


# ---------------------------------------------------------------- 几何原语（M16 §9.2）
def ring(center: tuple[float, float], r_m: float, n: int, start_az_deg: float = 90.0) -> list[list[float]]:
    """逆时针 n 边闭合折线（n + 1 点，首尾相同），首点方位 `start_az_deg`（+E 为 0，逆时针）。"""
    a0 = math.radians(start_az_deg)
    pts = [[_r(center[0] + r_m * math.cos(a0 + 2 * math.pi * k / n), 2),
            _r(center[1] + r_m * math.sin(a0 + 2 * math.pi * k / n), 2)] for k in range(n)]
    return [*pts, list(pts[0])]


def square(center: tuple[float, float], side_m: float) -> list[list[float]]:
    """逆时针方形（西南角起，不闭合）。"""
    h = side_m / 2
    cx, cy = center
    return [[_r(cx - h), _r(cy - h)], [_r(cx + h), _r(cy - h)], [_r(cx + h), _r(cy + h)], [_r(cx - h), _r(cy + h)]]


def circle_zone(zone_id: str, kind: str, center: tuple[float, float], r_m: float, n: int = 32,
                min_z_m: float | None = None, max_z_m: float | None = None, label: str = "", label_zh: str = "") -> dict:
    """32 边形近似圆的 curated 区域要素（外环逆时针、闭合；与 M03 `circle_ring` 同一舍入）。"""
    pts = [[_r(center[0] + r_m * math.cos(2 * math.pi * k / n)), _r(center[1] + r_m * math.sin(2 * math.pi * k / n))]
           for k in range(n)]
    return {"type": "Feature", "id": zone_id,
            "geometry": {"type": "Polygon", "coordinates": [[*pts, list(pts[0])]]},
            "properties": {"zone_id": zone_id, "kind": kind, "min_z_m": min_z_m, "max_z_m": max_z_m,
                           "label": label or zone_id, "label_zh": label_zh or zone_id, "origin": "curated",
                           "editable": False}}


def zones_doc(world_id: str, coordinate_sha256: str | None = None) -> dict:
    """`scenarios/zones/<world_id>.zones.geojson`（AWR-16 §7；不含 border，`origin = curated`）。"""
    feats = [circle_zone(z.zone_id, z.kind, z.center, z.radius_m, label=z.label, label_zh=z.label_zh)
             for z in CITIES[world_id].zones]
    return {"type": "FeatureCollection",
            "awr": {"schema": "awr.zones.v1", "schema_version": "1.0.0", "world_id": world_id, "frame": "world",
                    "units": "m", "coordinate_sha256": coordinate_sha256 or ZERO_SHA, "source_sha256": None},
            "features": feats}


def zone_defs(world_id: str) -> tuple[ZoneDef, ...]:
    return CITIES[world_id].zones


# ---------------------------------------------------------------- 剧本
def _head(sid: str, world_id: str, name: str, name_zh: str, description: str, pin: str | None) -> dict:
    return {**SCHEMA, "scenario_id": sid, "name": name, "name_zh": name_zh, "description": description,
            "world_id": world_id, "world_coordinate_sha256": pin}


def s1_scenario(pin: str | None = None) -> dict:
    """S1 深圳超高层双机立面巡检（M16 §6.4.2、§7.3.2；AWR-12 §7.2；ADR-052）。"""
    def helix(z_range: list[float]) -> dict:
        # facade_z_range_m：立面网格取 world z ∈ [45, 374.1]（12 §7.1.3；z < 45 m 被裙楼包围），M10 据此计算 facade_coverage
        return {"center_enu_m": [-162.2, 77.3], "radius_m": 57, "standoff_m": 30, "z_range_m": z_range,
                "dz_per_rev_m": 18.47, "speed_mps": 6.0, "gimbal": "look_at_axis", "direction": "ccw",
                "facade_z_range_m": [45, 374.1]}

    safe_end = [{"metric": "min_separation_m", "op": ">=", "value": 10},
                {"metric": "landed_all", "op": "==", "value": True}]
    return {
        **_head("s1-shenzhen-facade", "shenzhen", "Shenzhen facade helix duo", "深圳超高层双机立面巡检",
                "Two P600 scan the 381 m tower facade top-down in two altitude bands; gust front at t = 420 s.", pin),
        "seed": 7, "rate": 1, "autoplay": True, "gcs_loss_policy": "ignore", "record": True,
        "time_limit_s": 1800, "on_complete": "pause", "energy_precheck": "reject",
        "env": {"preset": "clear", "patch": {"wind": {"speed_ref_mps": 6.0, "dir_from_deg": 135,
                                                      "turb_sigma_u_ref_mps": 1.0}}},
        "vehicles": [
            {"vehicle_id": "p600-01", "profile_id": "p600_mid360", "home_enu_m": [-230, 20, None], "yaw_rad": 0.0,
             "initial_soc": 1.0, "speed_profile": "px4_default", "sensors": ["camera"], "caps": ["rgb.zoom"],
             "marked": True},
            {"vehicle_id": "p600-02", "profile_id": "p600_mid360", "home_enu_m": [-230, 40, None], "yaw_rad": 0.0,
             "initial_soc": 1.0, "speed_profile": "px4_default", "sensors": ["camera", "mid360"],
             "caps": ["rgb.zoom", "lidar.mapping"], "marked": True}],
        "missions": [
            {"mission_id": "m-lower", "vehicle_ids": ["p600-01"], "generator": "helix_scan",
             "params": helix([252, 50]), "sync_policy": "free", "priority": 0, "on_done": "rtl",
             "on_abort": "hover"},
            {"mission_id": "m-upper", "vehicle_ids": ["p600-02"], "generator": "helix_scan",
             "params": helix([391, 248]), "sync_policy": "free", "priority": 1, "on_done": "rtl",
             "on_abort": "hover"}],
        "transit": {"planner": "safe_transit", "margin_m": 5, "layer_dz_m": 4},
        "zones": {"active": ["border", "nofly-sz-t2", "restricted-sz-t3"]},
        "events": [
            {"event_id": "gust-420", "at_s": 420, "action": "env.gust", "args": {"amp_mps": 6.0, "length_m": 120}},
            {"event_id": "soc-guard-01", "when": {"metric": "battery_soc_min", "args": {"vehicle_ids": ["p600-01"]},
                                                  "op": "<", "value": 0.25},
             "action": "cmd", "args": {"vehicle_id": "p600-01", "op": "rtl", "args": {}}},
            {"event_id": "soc-guard-02", "when": {"metric": "battery_soc_min", "args": {"vehicle_ids": ["p600-02"]},
                                                  "op": "<", "value": 0.25},
             "action": "cmd", "args": {"vehicle_id": "p600-02", "op": "rtl", "args": {}}},
            {"event_id": "mark-done", "when": {"metric": "missions_done", "op": "==", "value": True},
             "action": "mark", "args": {"label": "scan complete"}}],
        "success": {"all": [
            {"metric": "missions_done", "op": "==", "value": True},
            {"metric": "facade_coverage", "args": {"mission_ids": ["m-lower", "m-upper"]}, "op": ">=", "value": 0.9},
            {"metric": "min_separation_m", "op": ">=", "value": 10},
            {"metric": "guard_events", "op": "==", "value": 0},
            {"metric": "pos_err_max_m", "args": {"window": "gust"}, "op": "<", "value": 3.0},
            {"metric": "landed_all", "op": "==", "value": True},
            {"metric": "battery_soc_min", "op": ">=", "value": 0.20},
            {"metric": "energy_rtl_count", "op": "==", "value": 0},
            {"metric": "landed_home_err_m", "op": "<", "value": 2.0}]},
        "profiles": {
            "ci": {"rate": 10, "record": False},
            "perf": {"rate": 1, "record": False},
            "demo": {"rate": 1, "record": True, "on_complete": "continue"},
            "wx-fog": {"env": {"preset": "fog"}},
            "wx-rain": {"energy_precheck": "warn",
                        "env": {"preset": "rain", "patch": {"wind": {"speed_ref_mps": 8.0, "turb_sigma_u_ref_mps": 2.0}}},
                        "success": {"all": [safe_end[0],
                                            {"metric": "guard_events", "args": {"level": "critical"}, "op": "==",
                                             "value": 0},
                                            safe_end[1]]}},
            "wx-storm": {"energy_precheck": "warn",
                         "env": {"preset": "thunderstorm",
                                 "patch": {"wind": {"speed_ref_mps": 14.0, "turb_sigma_u_ref_mps": 5.0}}},
                         "success": {"all": safe_end}}},
        "tags": ["demo", "ci", "inspection"],
    }


def ladder_sets(n: int, center: tuple[float, float] = LADDER_CENTER, spacing_m: float = LADDER_SPACING_M,
                offset_m: float = 12.0, agl_m: tuple[float, ...] = LADDER_AGL_M, id_prefix: str = "sim",
                profile_id: str = "p600_mid360", turns: int = 20) -> list[dict]:
    """ladder 的 4 个高度层（M16 §6.4.8 表）：各层机数 N//4（余数给低层），同层格距 24 m，12 m 交错，高层先飞。"""
    if n < 4:
        raise ValueError("ladder needs at least 4 vehicles (one per layer)")
    counts = [n // 4 + (1 if i < n % 4 else 0) for i in range(4)]
    cols = math.ceil(math.sqrt(counts[0]))
    rows = math.ceil(counts[0] / cols)
    w = (cols - 1) * spacing_m + offset_m
    h = (rows - 1) * spacing_m + offset_m
    ox, oy = center[0] - w / 2, center[1] - h / 2
    offs = tuple((dx * offset_m / 12.0, dy * offset_m / 12.0) for dx, dy in LADDER_OFFSETS)
    out, start = [], 1
    for L in range(4):
        out.append({
            "set_id": f"l{L}", "id_prefix": id_prefix, "id_start": start, "id_digits": 4, "count": counts[L],
            "profile_id": profile_id,
            "layout": {"kind": "grid", "origin_enu_m": [_r(ox + offs[L][0], 1), _r(oy + offs[L][1], 1), None],
                       "spacing_m": _r(spacing_m), "cols": cols},
            "mission": {"generator": "orbit", "center": "home", "start": {"at_s": _r(LADDER_START_S[L])},
                        "params": {"radius_m": LADDER_ORBIT["radius_m"], "agl_m": _r(agl_m[L]),
                                   "speed_mps": LADDER_ORBIT["speed_mps"], "turns": turns,
                                   "cw": LADDER_ORBIT["cw"], "yaw": LADDER_ORBIT["yaw"]}}})
        start += counts[L]
    return out


def ladder_scenario(pin: str | None = None) -> dict:
    """机群阶梯（M16 §6.4.8、§7.3.3）：缺省即 n200；每个 `n<N>` profile 写出完整的 4 个 set（数组整体替换）。"""
    profiles: dict[str, Any] = {f"n{n}": {"vehicle_sets": ladder_sets(n)} for n in LADDER_NS}
    profiles["x500"] = {"vehicle_sets": ladder_sets(200, profile_id="x500")}
    return {
        **_head("ladder-shenzhen", "shenzhen", "Shenzhen fleet ladder", "深圳机群阶梯",
                "Four altitude layers (60/75/90/105 m AGL) on a staggered 12 m grid take off top layer first and orbit "
                "their homes; profiles n10 to n1000 select the fleet size.", pin),
        "seed": 7, "rate": 1, "gcs_loss_policy": "ignore", "record": False, "energy_precheck": "warn",
        "time_limit_s": 600, "on_complete": "continue", "env": {"preset": "partlyCloudy"},
        "transit": {"planner": "direct", "margin_m": 5, "layer_dz_m": 4},
        "vehicle_sets": ladder_sets(200),
        "events": [{"event_id": "steady", "when": {"metric": "elapsed_s", "op": ">=", "value": 45},
                    "action": "mark", "args": {"label": "ladder.steady"}}],
        "success": {"all": [
            {"metric": "missions_done", "op": "==", "value": True},
            {"metric": "guard_events", "op": "==", "value": 0},
            {"metric": "min_separation_m", "op": ">=", "value": 14},
            {"metric": "landed_all", "op": "==", "value": True}]},
        "profiles": profiles,
        "tags": ["ladder", "ci", "stress"],
    }


def free_scenario(world_id: str, pad: tuple[float, float] | None = None, spacing_m: float = 6.0,
                  pin: str | None = None) -> dict:
    """free-<world>（M16-FR-003，§7.3.3 模板）：2 架 P600 在平坦地块待命、无任务。"""
    c = CITIES[world_id]
    px, py = pad if pad is not None else c.free_pad
    z = c.free_pad_z
    return {
        **_head(f"free-{world_id}", world_id, f"{world_id.capitalize()} free play", f"{c.name_zh}自由剧本",
                "Two P600 wait on a flat pad; no missions; the operator commands them from the UI.", pin),
        "seed": 7, "gcs_loss_policy": "ignore", "on_complete": "continue",
        "vehicles": [
            {"vehicle_id": "p600-01", "profile_id": "p600_mid360", "home_enu_m": [_r(px - spacing_m / 2), _r(py), z]},
            {"vehicle_id": "p600-02", "profile_id": "p600_mid360", "home_enu_m": [_r(px + spacing_m / 2), _r(py), z]}],
        "success": {"all": [{"metric": "guard_events", "op": "==", "value": 0}]},
        "tags": ["free"],
    }


def _grid_vehicles(prefix: str, count: int, origin: tuple[float, float], cols: int, spacing_m: float = 6.0) -> list[dict]:
    """编组机体（M16 §6.4.4、§6.4.6 的 `vehicle_sets` 等价展开）：`scenario.schema.json` 的 `id_prefix` 只允许
    `^[a-z0-9]+$`，而定稿 id 为 `p600-a-01`，因此写成显式 `vehicles[]`（网格、间距 6 m），id 与 M16 §6.4 一致。"""
    out = []
    for k in range(count):
        r_, c_ = divmod(k, cols)
        out.append({"vehicle_id": f"{prefix}-{k + 1:02d}", "profile_id": "p600_mid360",
                    "home_enu_m": [_r(origin[0] + c_ * spacing_m), _r(origin[1] + r_ * spacing_m), None],
                    "speed_profile": "px4_default"})
    return out


def _ids(prefix: str, n: int) -> list[str]:
    return [f"{prefix}-{k:02d}" for k in range(1, n + 1)]


CI = {"ci": {"rate": 10, "record": False}}


def s2_scenario(pin: str | None = None) -> dict:
    """S2 上海编队环绕 + 公园覆盖（M16 §6.4.4；D1-ext）。"""
    return {
        **_head("s2-shanghai-formation", "shanghai", "Shanghai formation loop and park coverage",
                "上海编队环绕与公园覆盖",
                "Five P600 fly a V formation around the three towers; three P600 mow the Century Park box.", pin),
        "seed": 7, "gcs_loss_policy": "ignore", "time_limit_s": 1800, "energy_precheck": "reject",
        "env": {"preset": "partlyCloudy"},
        "vehicles": [*_grid_vehicles("p600-a", 5, (-2841, 1398), 3), *_grid_vehicles("p600-b", 3, (1030, -1230), 3)],
        "missions": [
            {"mission_id": "m-formation", "vehicle_ids": _ids("p600-a", 5), "generator": "formation",
             "params": {"shape": "v", "spacing_m": 12, "half_angle_deg": 35, "heading_mode": "filtered", "tau_psi_s": 2,
                        "anchor_path_enu_m": ring((-2853.0, 934.0), 350.0, 72, start_az_deg=90.0),
                        "speed_mps": 6, "corner_radius_m": 40, "z_m": 250}, "on_done": "rtl"},
            {"mission_id": "m-coverage", "vehicle_ids": _ids("p600-b", 3), "generator": "lawnmower",
             "params": {"polygon_enu_m": square((1158.0, -1094.0), 300.0),
                        "altitude": {"mode": "fly_over", "agl_m": 120, "clearance_m": 10},
                        "side_overlap": 0.7, "front_overlap": 0.8, "speed_mps": 5}, "on_done": "rtl"}],
        "zones": {"active": ["border", "nofly-sh-pearl"]},
        "success": {"all": [
            {"metric": "formation_err_rms_m", "args": {"mission_id": "m-formation"}, "op": "<=", "value": 3},
            {"metric": "min_separation_m", "op": ">=", "value": 10},
            {"metric": "area_coverage", "args": {"mission_ids": ["m-coverage"]}, "op": ">=", "value": 0.95},
            {"metric": "guard_events", "op": "==", "value": 0},
            {"metric": "landed_all", "op": "==", "value": True},
            {"metric": "battery_soc_min", "op": ">=", "value": 0.25}]},
        "profiles": dict(CI), "tags": ["formation", "coverage", "ci"],
    }


S3_TARGETS = (("t1", (-24.0, -1253.0)), ("t2", (160.0, -1060.0)), ("t3", (-290.0, -1500.0)))


def s3_scenario(pin: str | None = None) -> dict:
    """S3 纽约港口搜救与 thermal 复核（M16 §6.4.5；M14 §6.10.3、§6.11.2、§7.4；D1-ext）。"""
    rtl_after_search = [
        {"event_id": f"home-{v}", "when": {"metric": "missions_done", "args": {"mission_ids": ["m-search"]}, "op": "==",
                                          "value": True},
         "action": "cmd", "args": {"vehicle_id": f"p600-{v}", "op": "rtl", "args": {}}} for v in ("b1", "b2", "c1")]
    return {
        **_head("s3-newyork-sar", "newyork", "New York harbour search and thermal verification",
                "纽约港口搜救与热成像复核",
                "An RGB searcher flies an expanding square; a first detection at 0.42 is delegated over Mock ANet to a "
                "thermal verifier that confirms it above 0.9.", pin),
        "seed": 7, "rate": 1, "gcs_loss_policy": "ignore", "time_limit_s": 900, "energy_precheck": "reject",
        "env": {"preset": "partlyCloudy"},
        "vehicles": [
            {"vehicle_id": "p600-a1", "home_enu_m": [-58, -1515, None], "speed_profile": "px4_default",
             "sensors": ["camera"], "caps": ["rgb.zoom"], "marked": True},
            {"vehicle_id": "p600-b1", "home_enu_m": [-52, -1515, None], "initial_soc": 0.80, "caps": ["thermal.imaging"],
             "marked": True},
            {"vehicle_id": "p600-b2", "home_enu_m": [-46, -1515, None], "initial_soc": 0.95, "caps": ["thermal.imaging"]},
            {"vehicle_id": "p600-b3", "home_enu_m": [-40, -1515, None], "initial_soc": 0.26, "caps": ["thermal.imaging"]},
            {"vehicle_id": "p600-c1", "home_enu_m": [-34, -1515, None], "caps": ["relay.communication"]}],
        "missions": [
            {"mission_id": "m-search", "vehicle_ids": ["p600-a1"], "generator": "expanding_square",
             "params": {"datum_enu_m": [-64, -1283], "z_m": 60, "leg0_m": 55, "legs": 12, "first_heading_deg": 0,
                        "turn": "ccw", "speed_mps": 5}, "on_done": "rtl"},
            {"mission_id": "m-standby-b1", "vehicle_ids": ["p600-b1"], "generator": "follow_path",
             "params": {"waypoints_enu_m": [[-52, -1515, 80], [86, -1133, 80]], "speed_mps": 3}, "on_done": "hover"},
            {"mission_id": "m-standby-b2", "vehicle_ids": ["p600-b2"], "generator": "follow_path",
             "params": {"waypoints_enu_m": [[-46, -1515, 80], [-314, -1033, 80]], "speed_mps": 3}, "on_done": "hover"},
            {"mission_id": "m-relay", "vehicle_ids": ["p600-c1"], "generator": "follow_path",
             "params": {"waypoints_enu_m": [[-34, -1515, 150], [-64, -1283, 150]], "speed_mps": 5}, "on_done": "hover"}],
        "zones": {"active": ["border", "nofly-ny-70pine"]},
        "events": [
            *[{"event_id": f"spawn-{tid}", "at_s": 0, "action": "target.spawn",
               "args": {"target_id": tid, "pos_enu_m": [_r(p[0]), _r(p[1]), 0], "kind": "person", "conf_first": 0.42,
                        "conf_confirm": 0.9}} for tid, p in S3_TARGETS],
            *rtl_after_search],
        "agents": {"network": "mock", "members": [
            {"vehicle_id": "p600-a1", "capabilities": ["rgb.zoom"], "role": "searcher"},
            {"vehicle_id": "p600-b1", "capabilities": ["thermal.imaging"], "role": "verifier"},
            {"vehicle_id": "p600-b2", "capabilities": ["thermal.imaging"], "role": "verifier"},
            {"vehicle_id": "p600-b3", "capabilities": ["thermal.imaging"], "role": "verifier"}],
            "tasks": [{"template_id": "thermal-verify",
                       # M14 §7.4 (owner of $defs/agents; M14-to-M16 item 1): capability pattern and state, not "sensor"
                       "trigger": {"on": "detection", "from_roles": ["searcher"], "capability": "rgb.*", "state": "suspect",
                                   "conf_lt": 0.8, "merge_radius_m": 30},
                       "capability": "thermal.imaging", "args": {"dwell_s": 10, "alt_agl_m": 60, "orbit_radius_m": 20},
                       "accept": {"op": 1, "children": [
                           {"op": 12, "thresh": {"metric": "confidence", "op": 4, "value": 0.8}},
                           {"op": 10, "artifact": {"path_glob": "thermal/**", "min_size_bytes": 1024}},
                           {"op": 11, "test": {"test_id": "station_reached", "expect": 1}}]},
                       "strategy": "auction", "max_retries": 2}]},
        "success": {"all": [
            {"metric": "target_confidence", "args": {"target_id": "t1"}, "op": ">=", "value": 0.9},
            {"metric": "t_conf_s", "args": {"target_id": "t1", "threshold": 0.9}, "op": "<=", "value": 300},
            {"metric": "guard_events", "op": "==", "value": 0},
            {"metric": "min_separation_m", "op": ">=", "value": 10},
            {"metric": "landed_all", "op": "==", "value": True}]},
        "profiles": dict(CI), "tags": ["search_thermal", "agents", "ci"],
    }


def s4_scenario(pin: str | None = None) -> dict:
    """S4 芝加哥湖岸编队 + Loop 覆盖（M16 §6.4.6；走廊东移到 x = +800；D1-ext）。"""
    a_ids, b_ids = _ids("p600-a", 5), _ids("p600-b", 5)
    form = {"spacing_m": 12, "heading_mode": "filtered", "tau_psi_s": 2, "speed_mps": 8, "agl_m": 150}
    return {
        **_head("s4-chicago-lakeshore", "chicago", "Chicago lakeshore formation and Loop coverage",
                "芝加哥湖岸编队与 Loop 覆盖",
                "Five P600 fly a line abreast up the lake corridor and a V back; five P600 cover the Willis Tower box "
                "lane by lane at a safe altitude.", pin),
        "seed": 7, "gcs_loss_policy": "ignore", "time_limit_s": 1800, "energy_precheck": "reject",
        "vehicles": [*_grid_vehicles("p600-a", 5, (573, -1174), 5), *_grid_vehicles("p600-b", 5, (-1152, -665), 5)],
        "missions": [
            {"mission_id": "m-corridor-out", "vehicle_ids": a_ids, "generator": "formation",
             "params": {"shape": "line", **form, "anchor_path_enu_m": [[800, -1200], [800, 600]]}, "on_done": "hover"},
            {"mission_id": "m-corridor-back", "vehicle_ids": a_ids, "generator": "formation",
             "params": {"shape": "v", "half_angle_deg": 35, **form, "anchor_path_enu_m": [[800, 600], [800, -1200]]},
             "start": {"after": "m-corridor-out"}, "on_done": "rtl"},
            {"mission_id": "m-loop", "vehicle_ids": b_ids, "generator": "lawnmower",
             "params": {"polygon_enu_m": square((-1101.0, -584.0), 600.0),
                        "altitude": {"mode": "per_lane", "agl_m": 150, "clearance_m": 10},
                        "side_overlap": 0.7, "front_overlap": 0.8, "speed_mps": 5}, "on_done": "rtl"}],
        "zones": {"active": ["border", "nofly-chi-hancock"]},
        "success": {"all": [
            {"metric": "formation_err_rms_m", "args": {"mission_id": "m-corridor-out"}, "op": "<=", "value": 3},
            {"metric": "formation_err_rms_m", "args": {"mission_id": "m-corridor-back"}, "op": "<=", "value": 3},
            {"metric": "min_separation_m", "op": ">=", "value": 10},
            {"metric": "area_coverage", "args": {"mission_ids": ["m-loop"]}, "op": ">=", "value": 0.9},
            {"metric": "guard_events", "op": "==", "value": 0},
            {"metric": "landed_all", "op": "==", "value": True},
            {"metric": "battery_soc_min", "op": ">=", "value": 0.25}]},
        "profiles": dict(CI), "tags": ["formation", "coverage", "ci"],
    }


def s5_scenario(pin: str | None = None) -> dict:
    """S5 旧金山丘陵地形跟随（M16 §6.4.7；300 m × 300 m 方形；D1-ext）。"""
    return {
        **_head("s5-sanfrancisco-terrain", "sanfrancisco", "San Francisco hillside terrain following",
                "旧金山丘陵地形跟随", "One P600 surveys a 300 m hillside box at 80 m above the terrain.", pin),
        "seed": 7, "gcs_loss_policy": "ignore", "time_limit_s": 1800, "energy_precheck": "reject",
        "vehicles": [{"vehicle_id": "p600-01", "home_enu_m": [-1895, -2262, None], "speed_profile": "px4_default",
                      "sensors": ["camera"]}],
        "missions": [
            {"mission_id": "m-survey", "vehicle_ids": ["p600-01"], "generator": "terrain_follow",
             "params": {"area": {"polygon_enu_m": square((-2000.0, -2500.0), 300.0), "side_overlap": 0.6,
                                 "front_overlap": 0.8, "speed_mps": 5},
                        "agl_m": 80, "max_slope_deg": 15, "clearance_m": 10}, "on_done": "rtl"}],
        "zones": {"active": ["border", "nofly-sf-sutro"]},
        "success": {"all": [
            {"metric": "agl_min_m", "args": {"mission_id": "m-survey"}, "op": ">=", "value": 60},
            {"metric": "area_coverage", "args": {"mission_ids": ["m-survey"]}, "op": ">=", "value": 0.95},
            {"metric": "guard_events", "op": "==", "value": 0},
            {"metric": "landed_all", "op": "==", "value": True},
            {"metric": "battery_soc_min", "op": ">=", "value": 0.25}]},
        "profiles": dict(CI), "tags": ["coverage", "terrain", "ci"],
    }


def s6_scenario(pin: str | None = None) -> dict:
    """S6 苏州走廊巡检 + 中继（M16 §6.4.7；四机分东西两半、一机中继；D1-ext）。"""
    corridor = {"offset_m": 200, "sides": "both", "agl_m": 80, "gimbal_tilt_deg": 45, "terrain_follow": True,
                "speed_mps": 10}
    return {
        **_head("s6-suzhou-corridor", "suzhou", "Suzhou corridor inspection with relay", "苏州走廊巡检与中继",
                "Four P600 inspect both sides of a 4 km corridor in two halves while a relay holds station.", pin),
        "seed": 7, "gcs_loss_policy": "ignore", "time_limit_s": 1800, "energy_precheck": "reject",
        "vehicles": [
            {"vehicle_id": "p600-i1", "home_enu_m": [-1000, 200, 0], "speed_profile": "px4_default", "sensors": ["camera"]},
            {"vehicle_id": "p600-i2", "home_enu_m": [-1000, -200, 0], "speed_profile": "px4_default", "sensors": ["camera"]},
            {"vehicle_id": "p600-i3", "home_enu_m": [1000, 200, 0], "speed_profile": "px4_default", "sensors": ["camera"]},
            {"vehicle_id": "p600-i4", "home_enu_m": [1000, -200, 0], "speed_profile": "px4_default", "sensors": ["camera"]},
            {"vehicle_id": "p600-r1", "home_enu_m": [0, 300, 0], "caps": ["relay.communication"]}],
        "missions": [
            {"mission_id": "m-west", "vehicle_ids": ["p600-i1", "p600-i2"], "generator": "corridor",
             "params": {"polyline_enu_m": [[-2000, 0], [0, 0]], **corridor}, "on_done": "rtl"},
            {"mission_id": "m-east", "vehicle_ids": ["p600-i3", "p600-i4"], "generator": "corridor",
             "params": {"polyline_enu_m": [[0, 0], [2000, 0]], **corridor}, "on_done": "rtl"},
            {"mission_id": "m-relay", "vehicle_ids": ["p600-r1"], "generator": "follow_path",
             "params": {"waypoints_enu_m": [[0, 300, 170], [0, 0, 170]], "speed_mps": 5}, "on_done": "hover"}],
        "events": [
            {"event_id": "relay-home", "when": {"metric": "missions_done", "args": {"mission_ids": ["m-west", "m-east"]},
                                                "op": "==", "value": True},
             "action": "cmd", "args": {"vehicle_id": "p600-r1", "op": "rtl", "args": {}}}],
        "success": {"all": [
            {"metric": "missions_done", "args": {"mission_ids": ["m-west", "m-east"]}, "op": "==", "value": True},
            {"metric": "min_separation_m", "op": ">=", "value": 10},
            {"metric": "guard_events", "op": "==", "value": 0},
            {"metric": "landed_all", "op": "==", "value": True},
            {"metric": "battery_soc_min", "op": ">=", "value": 0.25}]},
        "profiles": dict(CI), "tags": ["inspection", "relay", "ci"],
    }


def soak_scenario(pin: str | None = None) -> dict:
    """soak-shenzhen（M16 §7.3.4）：S1 两机与任务原样保留，另加 ladder n200 的 4 个 set（x500、200 圈，约 31 min）。"""
    s1 = s1_scenario(pin)
    return {
        **_head("soak-shenzhen", "shenzhen", "Shenzhen soak: facade duo plus 200 orbiters", "深圳长稳：立面双机加 200 架环绕",
                "S1 and the n200 ladder together for a 30 min soak; ladder vehicles use x500 (no battery model).", pin),
        "seed": 7, "rate": 1, "autoplay": True, "gcs_loss_policy": "ignore", "record": False,
        "time_limit_s": 2400, "on_complete": "continue", "energy_precheck": "warn",
        "env": s1["env"], "vehicles": s1["vehicles"], "vehicle_sets": ladder_sets(200, profile_id="x500", turns=200),
        "missions": s1["missions"], "transit": s1["transit"], "zones": s1["zones"], "events": s1["events"],
        "success": {"all": [{"metric": "min_separation_m", "op": ">=", "value": 10},
                            {"metric": "guard_events", "op": "==", "value": 0}]},
        "profiles": {"rec": {"record": True}},
        "tags": ["soak", "stress"],
    }


def catalog_doc() -> dict:
    """`scenarios/catalog.json`（`awr.scenario_catalog.v1`，M16 §7.3.1；V-SC-13）。"""
    worlds: dict[str, Any] = {}
    for wid in CITY_IDS:
        c = CITIES[wid]
        w: dict[str, Any] = {"default": c.default_scenario, "demo": list(c.demo_scenarios)}
        if wid == "shenzhen":
            w["gate"] = True
        worlds[wid] = w
    return {
        "schema": "awr.scenario_catalog.v1", "schema_version": "1.0.0", "worlds": worlds,
        "ui_profiles": {"s1-shenzhen-facade": ["demo", "wx-fog", "wx-rain", "wx-storm"],
                        "ladder-shenzhen": [f"n{n}" for n in LADDER_NS]},
        "themes": {
            "inspection": ["s1-shenzhen-facade", "s6-suzhou-corridor"],
            "coverage": ["s2-shanghai-formation", "s4-chicago-lakeshore", "s5-sanfrancisco-terrain"],
            "formation": ["s2-shanghai-formation", "s4-chicago-lakeshore"],
            "search_thermal": ["s3-newyork-sar"],
            "weather": ["s1-shenzhen-facade#wx-fog", "s1-shenzhen-facade#wx-rain", "s1-shenzhen-facade#wx-storm"],
            "stress": ["ladder-shenzhen", "soak-shenzhen"]},
    }


SCENARIO_BUILDERS = {
    "s1-shenzhen-facade": s1_scenario, "ladder-shenzhen": ladder_scenario, "soak-shenzhen": soak_scenario,
    "s2-shanghai-formation": s2_scenario, "s3-newyork-sar": s3_scenario, "s4-chicago-lakeshore": s4_scenario,
    "s5-sanfrancisco-terrain": s5_scenario, "s6-suzhou-corridor": s6_scenario,
}
CORE_SCENARIOS = ("s1-shenzhen-facade", "ladder-shenzhen", *(f"free-{w}" for w in CITY_IDS))


def world_of(sid: str) -> str:
    if sid.startswith("free-"):
        return sid[5:]
    return {"s1-shenzhen-facade": "shenzhen", "ladder-shenzhen": "shenzhen", "soak-shenzhen": "shenzhen",
            "s2-shanghai-formation": "shanghai", "s3-newyork-sar": "newyork", "s4-chicago-lakeshore": "chicago",
            "s5-sanfrancisco-terrain": "sanfrancisco", "s6-suzhou-corridor": "suzhou"}[sid]


def build_all(pins: dict[str, str | None] | None = None) -> dict[str, dict]:
    """全部生成物：`{相对 scenarios/ 的路径: 文档}`。`pins` 为 `{world_id: coordinate.sha256}`（`make scenarios-pin`）。"""
    pins = pins or {}
    out: dict[str, dict] = {}
    for sid, fn in SCENARIO_BUILDERS.items():
        out[f"{sid}.json"] = fn(pins.get(world_of(sid)))
    for wid in CITY_IDS:
        out[f"free-{wid}.json"] = free_scenario(wid, pin=pins.get(wid))
        out[f"zones/{wid}.zones.geojson"] = zones_doc(wid, pins.get(wid))
    out["catalog.json"] = catalog_doc()
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- 确定性 JSON
def _scalar(o: Any) -> bool:
    return o is None or isinstance(o, (bool, int, float, str))


def dumps_canonical(obj: Any, width: int = 120) -> str:
    """键序保持插入顺序；一行放得下（≤ width）的数组与对象写在一行，否则逐项换行。末尾换行。"""

    def one(o: Any) -> str:
        return json.dumps(o, ensure_ascii=False, separators=(", ", ": "), allow_nan=False)

    def enc(o: Any, ind: int, lead: int) -> str:
        """ind：本层缩进；lead：当前行上值之前已占的列数（决定能否写成一行）。"""
        if _scalar(o):
            return one(o)
        flat = one(o)
        if lead + len(flat) <= width and (isinstance(o, list) or len(o) <= 6):
            return flat
        pad = " " * (ind + 2)
        if isinstance(o, list):
            if not o:
                return "[]"
            return "[\n" + ",\n".join(pad + enc(v, ind + 2, ind + 2) for v in o) + "\n" + " " * ind + "]"
        if not o:
            return "{}"
        rows = []
        for k, v in o.items():
            key = f"{pad}{one(k)}: "
            rows.append(key + enc(v, ind + 2, len(key)))
        return "{\n" + ",\n".join(rows) + "\n" + " " * ind + "}"

    return enc(obj, 0, 0) + "\n"


def write_json_canonical(obj: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dumps_canonical(obj).encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
