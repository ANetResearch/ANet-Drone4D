"""任务契约（M27 `bench/task.v1`；M26 §7.4 简报投影）：由事件版本目录（plan.json、anchors.json、tape.json）生成。

- 计分事件：plan.json 的 `annotations`（id、type、participants、window[s]），类型即 `scored_event_types`；
- report 类型白名单：事件类型 → report 类型（`REPORT_OF`），基线按此报告（M26 §14.3）；
- 任务区：锚点 `ev_town_center` 区域多边形（缺省取全部锚点外接框外扩 50 m）；公开锚点与目标描述来自事件 `brief`；
- 高度包络 [30, 120] m AGL；安全间距人员与车辆各 10 m；预算每机 10 km、2,000 kB。
任务契约与简报只含公开信息：不含演员轨迹、事件区间与参与者身份（参与者的外观描述只对 `brief.targets` 列出的目标给出）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

__all__ = ["EVENT_OF", "REPORT_OF", "brief_for_agent", "scored_events", "task_from_event"]

REPORT_OF = {"proximity_encounter": "meet", "board_vehicle": "board", "alight_vehicle": "alight", "enter_building": "enter_building",
             "exit_building": "exit_building", "handover": "handover", "vehicle_pass": "vehicle_event", "reacquire": "reacquire",
             "target_found": "found"}
EVENT_OF = {v: k for k, v in REPORT_OF.items()}
ALT_ENVELOPE_AGL_M = (30.0, 120.0)


def _load(d: Path, name: str) -> dict:
    return json.loads((Path(d) / name).read_text(encoding="utf-8"))


def _anchor_pos(a: dict) -> tuple[float, float]:
    if "xy" in a:
        return float(a["xy"][0]), float(a["xy"][1])
    p = np.asarray(a["polygon"], np.float64)
    return float(p[:, 0].mean()), float(p[:, 1].mean())


def scored_events(event_dir: Path) -> list[dict]:
    """→ [{id, type, participants, a_s, b_s, multi, params}]（评测端使用；不进入简报）。"""
    plan = _load(event_dir, "plan.json")
    return [{"id": a["id"], "type": a["type"], "participants": list(a["participants"]), "a_s": float(a["window"][0]),
             "b_s": float(a["window"][1]), "multi": len(a["participants"]) > 1, "params": a.get("params", {})}
            for a in plan.get("annotations", [])]


def task_from_event(event_dir: Path) -> dict:
    plan = _load(event_dir, "plan.json")
    anchors = {a["id"]: a for a in _load(event_dir, "anchors.json")["anchors"]}
    brief = plan.get("brief", {})
    zone = anchors.get("ev_town_center")
    if zone is not None and "polygon" in zone:
        poly = [[float(x), float(y)] for x, y in zone["polygon"]]
    else:
        xy = np.array([_anchor_pos(a) for a in anchors.values()])
        lo, hi = xy.min(0) - 50.0, xy.max(0) + 50.0
        poly = [[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]]
    actors = {a["id"]: a for a in plan["actors"]}
    targets = [{"appearance_desc": t.get("description_en", ""), "class": actors.get(t["actor"], {}).get("class", "person")}
               for t in brief.get("targets", [])]
    ev_types = sorted({a["type"] for a in plan.get("annotations", [])})
    return {
        "schema": "drone4d.bench.task.v1", "task_id": f"{plan['event_id']}-v{plan['version']}", "event_ref": f"{plan['event_id']}@{plan['version']}",
        "version": 1, "event_type": plan["event_type"],
        "brief": {"anchors_public": list(brief.get("public_anchors", [])), "targets": targets, "region_polygon_enu_m": poly,
                  "duration_s": plan["duration_ms"] / 1000.0},
        "known_targets": bool(targets), "min_obs_level": "L1", "altitude_envelope_agl_m": list(ALT_ENVELOPE_AGL_M),
        "scored_event_types": ev_types, "report_types": sorted({REPORT_OF.get(t, t) for t in ev_types}),
        "safety": {"min_sep_person_m": 10.0, "min_sep_vehicle_m": 10.0, "no_fly": []},
        "budget": {"distance_km_per_uav": 10.0, "comm_kb_per_uav": 2000.0},
    }


def brief_for_agent(task: dict, event_dir: Path, *, nominal_noise: dict) -> dict:
    """agent_config.v1 `brief`：公开锚点（含位置）、任务区、目标外观、report 词表与标称噪声。"""
    plan = _load(event_dir, "plan.json")
    actors = {a["id"]: a for a in plan["actors"]}
    targets = []
    for i, t in enumerate(plan.get("brief", {}).get("targets", [])):
        a = actors.get(t["actor"], {})
        ap = a.get("appearance", {})
        targets.append({"label": f"target_{i + 1}", "class": a.get("class", "person"),
                        "appearance": {k: ap[k] for k in ("top_rgb", "bottom_rgb") if k in ap}})
    anchors = {a["id"]: a for a in _load(event_dir, "anchors.json")["anchors"]}
    pub = []
    for aid in task["brief"]["anchors_public"]:
        a = anchors[aid]
        x, y = _anchor_pos(a)
        pub.append({"id": aid, "kind": a["kind"], "name": a.get("label_en") or a.get("label_zh") or aid, "pos_enu_m": [x, y, 0.0]})
    desc = "; ".join(t["appearance_desc"] for t in task["brief"]["targets"]) or "Observe the scored events in the task area."
    return {
        "task": {"type": "event_capture", "description": f"{task['event_type']}: {desc}"},
        "area": {"polygon_enu_m": task["brief"]["region_polygon_enu_m"], "alt_agl_range_m": task["altitude_envelope_agl_m"]},
        "keepouts": [], "anchors": pub,
        "targets": targets,
        "report_vocab": task["report_types"], "maps": {"dsm": "point"}, "nominal_noise": nominal_noise,
    }
