"""注册表等价用例（M10-AC-201）：确定性构造的规划作业集合。

`gen_registry_golden.py` 在 if/elif 分派（D1）下记录每个作业的 `(status, code, result_sha256)`，`test_registry.py` 在
注册表分派下重放并逐字节比较。三组：
- `tiny_cases`：合成小世界（M04 `fake_world_query`，内容确定）上覆盖 D1 的 11 种作业；
- `city_cases`：六城各 40 组转场（30 组 safe_transit、10 组 astar25），合计 240 组；
- `scenario_cases`：S1（深圳立面，helix_scan × 2）与 S4（芝加哥湖岸，formation × 2、lawnmower × 1，另加 preview）的
  生成作业，载荷与 `MissionEngine._generate` 相同（机体位于 home，z 取地面）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from awr.sim.planning.jobs import PlanRequest

ROOT = Path(__file__).resolve().parents[2]
CITIES = ("shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago")
LIM = {"v_max_mps": 12.0, "a_tan_mps2": 2.0, "a_lat_mps2": 3.0, "vz_up_mps": 3.0, "vz_dn_mps": 1.5, "a_max_mps2": 3.0}
GOLDEN = Path(__file__).resolve().parent / "golden" / "registry_d1.json"


def _key(w: Any) -> tuple[str, str, str]:
    return (w.world_id, w.content_version, w.coordinate_sha256)


def _req(jid: str, kind: str, w: Any, vids: tuple[str, ...], payload: dict, budget: int = 2000,
         prio: int = 1) -> PlanRequest:
    return PlanRequest(jid, kind, _key(w), vids, payload, dict(LIM), prio, 0, budget, jid)


def tiny_cases(w: Any) -> list[PlanRequest]:
    out: list[PlanRequest] = []
    out.append(_req("tiny:warm", "warm", w, (), {"grid25": True}, 10_000, 3))
    out.append(_req("tiny:pv-ok", "path_valid", w, (), {"polyline": np.array([[-150.0, -100.0, 40.0],
                                                                             [-60.0, -100.0, 40.0]])}))
    out.append(_req("tiny:pv-bad", "path_valid", w, (), {"polyline": np.array([[-120.0, -20.0, 30.0],
                                                                              [30.0, -20.0, 30.0]])}))
    out.append(_req("tiny:st-tower", "safe_transit", w, ("v",), {"start": [-120.0, -20.0, 30.0],
                                                                "goal": [30.0, -20.0, 60.0], "speed_mps": 6.0}))
    out.append(_req("tiny:st-goal", "safe_transit", w, ("v",), {"start": [-120.0, -20.0, 30.0],
                                                               "goal": [-40.0, -20.0, 60.0], "check_goal": True}))
    out.append(_req("tiny:st-ceiling", "safe_transit", w, ("v",), {"start": [-120.0, -20.0, 30.0],
                                                                  "goal": [30.0, -20.0, 60.0], "alt_max_m": 80.0}))
    out.append(_req("tiny:astar", "astar25", w, ("v",), {"start": [-120.0, -20.0, 30.0], "goal": [30.0, -20.0, 60.0],
                                                         "planner": "astar25", "alt_max_m": 90.0}))
    out.append(_req("tiny:st-low", "safe_transit", w, ("v",), {"start": [-150.0, -100.0, 40.0],
                                                              "goal": [120.0, 80.0, 45.0], "prefer_low": True}))
    rng = np.random.default_rng(11)
    for i in range(12):
        xy = rng.uniform([-170, -120], [160, 120], (2, 2))
        z = w.column_max_within(xy, 0.49) + rng.uniform(3, 20, 2)
        out.append(_req(f"tiny:rand-{i}", "safe_transit", w, ("v",), {"start": np.r_[xy[0], z[0]],
                                                                       "goal": np.r_[xy[1], z[1]]}))
    out.append(_req("tiny:fp-ok", "follow_path", w, ("v",), {"waypoints": np.array([[-120.0, -20.0, 130.0],
                                                                                    [30.0, -20.0, 130.0],
                                                                                    [30.0, 40.0, 130.0]])}, 500))
    out.append(_req("tiny:fp-bad", "follow_path", w, ("v",), {"waypoints": np.array([[-120.0, -20.0, 30.0],
                                                                                     [30.0, -20.0, 30.0]])}, 500))
    veh = [{"vehicle_id": "a", "home_enu_m": [-160.0, -110.0, 0.5], "pos_enu_m": [-160.0, -110.0, 0.5]},
           {"vehicle_id": "b", "home_enu_m": [-150.0, -110.0, 0.5], "pos_enu_m": [-150.0, -110.0, 0.5]}]
    box = [[-100.0, -80.0], [80.0, -80.0], [80.0, 60.0], [-100.0, 60.0]]
    lawn = {"generator": "lawnmower", "vehicles": veh, "params": {"polygon_enu_m": box, "altitude": {
        "mode": "fly_over", "agl_m": 60, "clearance_m": 10}, "speed_mps": 6.0}, "constraints": {}, "zones": ["border"],
        "mission_id": "m-lawn"}
    out.append(_req("tiny:gen-lawn", "generator", w, ("a", "b"), lawn))
    out.append(_req("tiny:cov-lawn", "coverage", w, ("a", "b"), lawn | {"mission_id": "m-cov"}))
    out.append(_req("tiny:preview-lawn", "preview", w, ("a", "b"), lawn | {"mission_id": "m-pv"}))
    orbit = {"generator": "orbit", "vehicles": veh[:1], "params": {"center_enu_m": [-40.0, -20.0], "radius_m": 60.0,
                                                                   "z_m": 130.0, "turns": 1}, "constraints": {},
             "zones": ["border"], "mission_id": "m-orbit"}
    out.append(_req("tiny:gen-orbit", "generator", w, ("a",), orbit))
    sq = {"generator": "expanding_square", "vehicles": veh[:1], "params": {"datum_enu_m": [100.0, 60.0], "z_m": 40.0,
                                                                           "legs": 4}, "constraints": {}, "zones": ["border"],
          "mission_id": "m-sq"}
    out.append(_req("tiny:gen-sq", "generator", w, ("a",), sq))
    form = {"generator": "formation", "vehicles": veh, "params": {"shape": "line", "spacing_m": 12, "speed_mps": 6,
                                                                  "agl_m": 130,
                                                                  "anchor_path_enu_m": [[-150, 90], [100, 90]]},
            "constraints": {}, "zones": ["border"], "mission_id": "m-form"}
    out.append(_req("tiny:gen-form", "formation", w, ("a", "b"), form))
    asm = {"op": "assemble", "members": ["a", "b", "c"], "pos": [[-160.0, -110.0, 0.5], [-150.0, -110.0, 0.5],
                                                                 [-140.0, -110.0, 0.5]],
           "slots": [[-150.0, 90.0, 130.0], [-150.0, 78.0, 130.0], [-150.0, 66.0, 130.0]], "z_form_m": 130.0,
           "v_mps": 6.0, "min_sep_m": 8.0}
    out.append(_req("tiny:asm", "formation", w, ("a", "b", "c"), asm, 100))
    # resume：取 fp-ok 的形状构造控制点
    Q = np.array([[-120.0, -20.0, 130.0]] * 3 + [[-45.0, -20.0, 130.0], [30.0, -20.0, 130.0]] + [[30.0, 40.0, 130.0]] * 3)
    out.append(_req("tiny:resume", "resume", w, ("v",), {"start": [-150.0, -100.0, 60.0], "ctrl_pts": Q, "ts_s": 4.0,
                                                         "tau_s": 6.0}, 500))
    tr = [{"vehicle_id": "a", "ctrl_pts": np.array([[0.0, -60.0, 130.0]] * 3 + [[0.0, 60.0, 130.0]] * 3), "ts_s": 3.0,
           "prio": 0.0},
          {"vehicle_id": "b", "ctrl_pts": np.array([[-60.0, 0.0, 130.0]] * 3 + [[60.0, 0.0, 130.0]] * 3), "ts_s": 3.0,
           "prio": 1.0}]
    out.append(_req("tiny:deconf", "deconflict", w, ("a", "b"), {"trajs": tr, "clearance_m": 10.0}, 200))
    out.append(_req("tiny:unknown", "nope", w, (), {}))
    return out


def city_cases(name: str, w: Any) -> list[PlanRequest]:
    """六城各 40 组转场：起终点在 hm 栅格中部 80% 范围内均匀取，高度为碰撞柱顶以上 10–60 m。"""
    g = w.hm
    W = g.a.shape[1] * g.cell
    H = g.a.shape[0] * g.cell
    lo = np.array([g.x0 + 0.1 * W, g.y0 + 0.1 * H])
    hi = np.array([g.x0 + 0.9 * W, g.y0 + 0.9 * H])
    rng = np.random.default_rng(1000 + CITIES.index(name))
    out = []
    for i in range(40):
        xy = rng.uniform(lo, hi, (2, 2))
        d = float(np.linalg.norm(xy[1] - xy[0]))
        if d > 600.0:                                   # 限制转场距离（与任务入场转场的量级一致）
            xy[1] = xy[0] + (xy[1] - xy[0]) * (600.0 / d)
        z = w.column_max_within(xy, 0.49) + rng.uniform(10, 60, 2)
        pl: dict = {"start": np.r_[xy[0], z[0]], "goal": np.r_[xy[1], z[1]], "speed_mps": 8.0}
        kind = "safe_transit"
        if i >= 30:
            kind = "astar25"
            pl["planner"] = "astar25"
        out.append(_req(f"{name}:t{i:02d}", kind, w, ("v",), pl, 300))
    return out


def _vehicles(sc: dict, w: Any, ids: list[str]) -> list[dict]:
    by = {v["vehicle_id"]: v for v in sc["vehicles"]}
    out = []
    for vid in ids:
        h = list(by[vid]["home_enu_m"])
        if h[2] is None:
            h[2] = float(w.ground_dtm(np.array([[h[0], h[1]]], np.float64))[0])
        out.append({"vehicle_id": vid, "home_enu_m": h, "pos_enu_m": h, "v_limit_mps": 12.0, "cruise_mps": 5.0,
                    "r_col_m": 0.49})
    return out


def scenario_cases(scenario_id: str, w: Any) -> list[PlanRequest]:
    sc = json.loads((ROOT / "scenarios" / f"{scenario_id}.json").read_text(encoding="utf-8"))
    zones = (sc.get("zones") or {}).get("active")
    out = []
    for m in sc["missions"]:
        veh = _vehicles(sc, w, list(m["vehicle_ids"]))
        payload = {"generator": m["generator"], "params": m["params"], "vehicles": veh,
                   "constraints": m.get("constraints") or {}, "zones": zones, "mission_id": m["mission_id"],
                   "camera": {"hfov_deg": 60.0, "vfov_deg": 42.1}}
        out.append(_req(f"{scenario_id}:{m['mission_id']}", "generator", w, tuple(m["vehicle_ids"]), payload, 20_000))
        if m["generator"] == "lawnmower":
            out.append(_req(f"{scenario_id}:{m['mission_id']}:pv", "preview", w, tuple(m["vehicle_ids"]),
                            payload | {"mission_id": m["mission_id"] + "-pv"}, 20_000, 0))
    return out


def summary(res: Any) -> list:
    return [res.status, int(res.code or 0), res.result_sha256]
