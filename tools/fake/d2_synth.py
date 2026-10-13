"""D2 synthetic data source: contract-shaped payloads for every D2 topic, event and sandbox REST body (WP-01; AWR-17 §17.11 last
paragraph: "fake_gw.py and FakeSource.ts must produce every new topic and event (fixtures packages/contracts/fixtures/d2/*.awrrt)").

Used by tools/fake/fake_gw.py (`--d2`, `--sandbox`) and tools/contracts/gen_fixtures.py (fixtures/d2/*). Everything is a pure function of
the simulation time and the seed, so fixtures are byte-reproducible. Payloads follow the frozen schemas (tests/contracts/test_d2_fake.py
validates every builder); the numbers are illustrative, not simulation results.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import perception_stub as P
import targets_stub as T

from awr.contracts import CONTRACTS_VERSION

SID = "sb-k3m7q2x7ab"  # AWR-17 §17.4.9 example sb-k3m9q2x7ab is not base32 (digit 9)
NEST = {"id": "nest-01", "name": "机巢 1", "pos": [-120.0, 80.0, 0.0], "kind": "box", "crewed": False, "slots": 4,
        "energy": {"mode": "swap", "swap_s": 180, "packs": [{"cells": 6, "count": 6, "capacity_ah": 10.0, "chemistry": "lihv", "pack_id": "lpb610hv"}]},
        "chargers": [{"model": "c1_xr", "count": 4, "channels": 1, "i_max_a": 10, "cells_max": 6}], "turnaround_s": 60, "launch_interval_s": 30,
        "wind_max_mps": 12, "rtk_base": True, "link_range_m": 3500}
TASK_GUARD = {"tid": "task-01", "type": "area_guard", "label": "区域值守", "params": {"polygon": [[0, -600], [600, -600], [600, -200], [0, -200]],
                                                                                    "target_class": "person", "level_req": "R", "revisit_s": 300}}
TASK_RELAY = {"tid": "task-02", "type": "relay_watch", "label": "接力 person-01", "params": {"target": "person-01", "level_req": "R"}}
LEVEL_NAMES = ("none", "D", "R", "I")


def _r(x: float, n: int = 3) -> float:
    return float(round(float(x), n))


class D2Synth:
    """Targets, perception, payload, nav, radar, acoustic, nest, task, relay and sandbox session payloads for n vehicles."""

    def __init__(self, uav_ids: list[str], seed: int = 7, world: str = "synthcity", sid: str = SID, targets: list[dict] | None = None):
        self.uav_ids = list(uav_ids)
        self.seed, self.world, self.sid = seed, world, sid
        self.targets = T.FakeTargets(targets if targets is not None else T.TEMPLATE_T5, seed)
        self.t_sim_ns = 0
        self.eo = P.gx40()
        self.lwir = P.lwir640(100.0)
        self.uav_pos = np.zeros((len(uav_ids), 3))
        self.uav_vel = np.zeros((len(uav_ids), 3))
        self.event_seq = 0

    # -------------------------------------------------------------- time
    def step(self, t_sim_ns: int, uav_pos: np.ndarray | None = None, uav_vel: np.ndarray | None = None) -> None:
        self.t_sim_ns = int(t_sim_ns)
        self.targets.step(self.t_sim_ns)
        if uav_pos is not None:
            self.uav_pos = np.asarray(uav_pos, float)
        if uav_vel is not None:
            self.uav_vel = np.asarray(uav_vel, float)
        t = self.t_sim_ns * 1e-9
        # perception summary written into the target columns (M19 and M20 writers in the real block)
        for i in range(self.targets.n):
            lvl = 2 if t >= 2.0 and i == 0 else (1 if t >= 1.0 else 0)
            self.targets.set_perception(i, lvl, captured=(i == 0 and t >= 3.0), sensor=1 if i == 0 else 3, relay=(i == 0 and t >= 3.0))

    # -------------------------------------------------------------- roster and targets
    def roster_entries(self) -> list[dict]:
        return self.targets.roster_entries()

    def target_rows(self) -> bytes:
        return self.targets.lite_rows().tobytes()

    def target_detail(self, i: int, with_cfg: bool = False) -> dict:
        return self.targets.detail(i, ("p600_mid360", "awr_v1"), with_cfg)

    # -------------------------------------------------------------- per-vehicle products
    def perception(self, a: int) -> dict:
        view = self.targets.view()
        p = tuple(float(x) for x in self.uav_pos[a]) if len(self.uav_pos) > a else (0.0, 0.0, 60.0)
        f = 48.0
        out_t = []
        for i in range(view.n):
            cfg = view.cfg[i]
            n_req = P.n_req_for(cfg.level_req)
            v_perp = float(np.linalg.norm(view.vel[i][:2]))
            res = P.perception_level(P.SensorState(self.eo, f), view, i, P.ViewGeom(p, 1.0, True, v_perp), P.EnvSample(mor_m=10000.0), n_req)
            out_t.append({"target": cfg.id, "cls_est": cfg.cls if res.level >= 2 else "unknown", "level": LEVEL_NAMES[res.level], "p": _r(res.p, 4),
                          "n": _r(res.n), "n_eff": _r(res.n_eff), "n_req": _r(n_req), "level_req": cfg.level_req, "range_m": _r(res.range_m, 1),
                          "gsd_m": _r(res.gsd_m, 4), "limiting": res.limiting,
                          "k": {"los": _r(res.k[0]), "c": _r(res.k[1]), "light": _r(res.k[2]), "blur": _r(res.k[3]), "occ": _r(res.k[4])},
                          "hold_s": 1.0 if res.n_eff >= n_req else 0.0, "captured": bool(res.n_eff >= n_req and i == 0),
                          "capture_state": "CAPTURED" if (res.n_eff >= n_req and i == 0) else "IDLE",
                          "meas_enu_m": [_r(x, 2) for x in view.pos[i]], "sigma_m": _r(max(res.range_m * 1e-3, 0.05), 3)})
        out_t.sort(key=lambda x: x["range_m"])
        hud = None
        if out_t:
            h = out_t[0]
            hud = {"sensor": "eo_gx40", "target": h["target"], "n_eff": h["n_eff"], "n_req": h["n_req"], "limiting": h["limiting"]}
        return {"uav": self.uav_ids[a], "t_sim_ns": self.t_sim_ns,
                "sensors": [{"sensor": "eo_gx40", "kind": "eo", "mode": "auto", "f_mm": f, "hfov_rad": _r(P.hfov_rad(self.eo, f), 5),
                             "vfov_rad": _r(P.vfov_rad(self.eo, f), 5), "illum": {"builtin": "auto", "ext": "off"}, "n_in_fov": len(out_t),
                             "targets": out_t[:8]}],
                "hud": hud, "budget": {"rays_tick_max": 8, "rays_s": 120.0, "dropped": 0}}

    def payload(self, a: int) -> dict:
        t = self.t_sim_ns * 1e-9
        return {"uav": self.uav_ids[a], "t_sim_ns": self.t_sim_ns,
                "gimbal": {"pitch_rad": _r(-0.35 - 0.05 * math.sin(0.2 * t), 4), "yaw_rad": 0.0, "mode": "track", "track_target": "person-01", "src": "tasking"},
                "sensors": [{"sensor": "eo_gx40", "kind": "eo", "enabled": True, "active": True, "f_mm": 48.0, "hfov_rad": _r(P.hfov_rad(self.eo, 48.0), 5),
                             "vfov_rad": _r(P.vfov_rad(self.eo, 48.0), 5), "mode": "auto", "resolution": "1080p", "palette": None, "cue": "on",
                             "illuminator": {"on": False, "kind": "builtin", "power_w": 0.8, "divergence_rad": _r(P.hfov_rad(self.eo, 48.0), 5), "range_m": 200.0}},
                            {"sensor": "mid360", "kind": "lidar", "enabled": True, "active": False, "f_mm": None, "hfov_rad": None, "vfov_rad": None,
                             "mode": None, "resolution": None, "palette": None, "cue": None, "illuminator": None}]}

    def nav(self, a: int) -> dict:
        p = self.uav_pos[a] if len(self.uav_pos) > a else np.zeros(3)
        v = self.uav_vel[a] if len(self.uav_vel) > a else np.zeros(3)
        return {"uav": self.uav_ids[a], "t_sim_ns": self.t_sim_ns, "imu": {"f_mps2": [0.02, -0.01, 9.81], "w_rad_s": [0.0, 0.0, 0.01]},
                "gnss": {"pos_enu_m": [_r(x, 3) for x in p], "vel_mps": [_r(x, 3) for x in v], "fix": "rtk_fixed", "hacc_m": 0.02, "vacc_m": 0.03,
                         "sats": 24, "hdop": 0.7},
                "baro": {"alt_m": _r(p[2], 2), "p_pa": _r(101325.0 * (1 - 2.25577e-5 * p[2]) ** 5.25588, 1)}, "loc_mode": "rtk"}

    def radar(self, a: int) -> dict:
        t = self.t_sim_ns * 1e-9
        return {"uav": self.uav_ids[a], "t_sim_ns": self.t_sim_ns, "sensor": "mmw77",
                "tracks": [{"track_id": 101, "range_m": _r(140 + 5 * math.sin(0.1 * t), 2), "az_rad": 0.21, "el_rad": -0.3, "vr_mps": _r(-0.4 * math.cos(0.1 * t)),
                            "snr_db": 16.5, "level": "D", "class_hint": None}], "clutter_gated": False}

    def acoustic(self, a: int) -> dict:
        return {"uav": self.uav_ids[a], "t_sim_ns": self.t_sim_ns, "sensor": "mic8", "ego_noise_dba": 63.0,
                "bearings": [{"bearing_id": 7, "az_rad": 1.05, "el_rad": -0.2, "level": "D",
                              "bands": [{"fc_hz": 630.0, "snr_db": 7.2}, {"fc_hz": 800.0, "snr_db": 4.1}], "harmonic_f0_hz": None}]}

    # -------------------------------------------------------------- nests, tasks, relay, sandbox
    def nest_status(self) -> dict:
        t = self.t_sim_ns * 1e-9
        soc = min(99.0, 40.0 + 0.5 * t)
        return {"id": NEST["id"], "kind": NEST["kind"], "t_sim_ns": self.t_sim_ns, "slots": {"total": 4, "occupied": 1},
                "inventory": [{"model_id": "p600_mid360", "count": len(self.uav_ids), "in_nest": 1, "airborne": max(len(self.uav_ids) - 1, 0)}],
                "aircraft": [{"uav": u, "state": "airborne" if k else "ready", "soc": 0.9} for k, u in enumerate(self.uav_ids[:4])],
                "packs": [{"pack_id": f"pk-{k + 1}", "cells": 6, "soc": 1.0 if k else _r(soc / 100, 3), "state": "ready" if k else "charging",
                           "t_ready_s": None if k else _r(max(0.0, (95 - soc) * 40.0), 1)} for k in range(3)],
                "chargers": [{"ch": c, "model": "c1_xr", "state": "charging" if c == 0 else "idle", "pack_id": "pk-1" if c == 0 else None,
                              "soc_pct": _r(soc, 1) if c == 0 else None, "eta_s": _r(max(0.0, (95 - soc) * 40.0), 1) if c == 0 else None,
                              "i_a": 10.0 if c == 0 else None} for c in range(4)],
                "launch_queue": [], "recover_queue": [], "wind_ok": True}

    def task_status(self, which: int = 0) -> dict:
        t = self.t_sim_ns * 1e-9
        task = (TASK_GUARD, TASK_RELAY)[which]
        uav = self.uav_ids[min(which, len(self.uav_ids) - 1)]
        return {"tid": task["tid"], "type": task["type"], "state": "RUNNING", "revision": 1, "t_sim_ns": self.t_sim_ns,
                "progress": {"pct": _r(min(100.0, t), 1), "coverage_pct": _r(min(100.0, 2 * t), 1) if which == 0 else None, "laps_done": None,
                             "dwell_s": _r(t, 1) if which else None},
                "n_vehicles": 1, "makespan_s": None,
                "items": [{"wid": f"{task['tid']}-w1", "kind": "station" if which else "subregion", "uav": uav, "state": "WORKING", "eta_s": 0.0, "reason": None}],
                "children": ["task-02"] if which == 0 else [], "warnings": []}

    def relay_stats(self) -> dict:
        t = self.t_sim_ns * 1e-9
        mon = max(0.0, t - 3.0)
        return {"tid": "task-02", "target": "person-01", "t_monitor_s": _r(mon, 1), "coverage": 1.0 if mon > 0 else 0.0,
                "gaps": {"count": 0, "max_s": 0.0, "by_cause": {"keepout_priority": 0, "illum_transition": 0, "infeasible": 0, "battery": 0, "other": 0}},
                "discovered": 0, "config_entries": {"count": 0, "total_s": 0.0}, "handovers": {"count": 0, "mean_overlap_s": None},
                "min_keepout_margin_m": 35.2, "sorties": 1, "flight_h": _r(mon / 3600.0, 4), "energy_wh": _r(mon * 0.11, 2), "min_ready_packs": 5,
                "energy_rtl": 0, "on_station": [{"uav": self.uav_ids[0], "station": "st-1", "since_t_sim_ns": 3_000_000_000}],
                "next": {"uav": self.uav_ids[min(1, len(self.uav_ids) - 1)], "eta_s": 1150.0, "reason": "energy"}}

    def session(self, state: str = "ACTIVE") -> dict:
        """awr.sandbox.session.v1 (REST)."""
        base = 1_791_168_123_000_000_000
        return {"sid": self.sid, "state": state, "world_id": self.world, "template_id": "t5_area_guard",
                "created_unix_ns": str(base), "expires_unix_ns": str(base + 1800 * 10**9), "lifetime_max_unix_ns": str(base + 7200 * 10**9),
                "expires_in_s": 1800, "renewable": True, "renew_block_reason": None, "idle_ttl_s": 600,
                "clock": {"state": "PLAYING" if state == "ACTIVE" else "STOPPED", "rate_requested": 1, "rate_granted": 1, "rate_cap": 5.6,
                          "limited_by": None, "tick_hz": 100},
                "limits": LIMITS, "usage": {"vehicles": len(self.uav_ids), "targets": self.targets.n, "nests": 1, "tasks_active": 2, "ws": 1},
                "epoch": 1, "restarts": 0, "degrade_level": "L0", "calendar": {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"},
                "acoustic_bg": "rural", "seed": self.seed, "end_reason": None,
                "endpoints": {"rest": f"/api/sandbox/v1/sessions/{self.sid}", "rt": f"/api/sandbox/v1/sessions/{self.sid}/rt"}}

    def session_state(self) -> dict:
        """awr.sandbox.session_state.v1 (topic sandbox/session)."""
        s = self.session()
        return {"sid": s["sid"], "state": s["state"], "world_id": s["world_id"], "template_id": s["template_id"], "expires_unix_ns": s["expires_unix_ns"],
                "max_expires_unix_ns": s["lifetime_max_unix_ns"], "expires_in_s": s["expires_in_s"], "renewable": True, "renew_block_reason": None,
                "clock": {"rate_requested": 1, "rate_granted": 1, "rate_cap": 5.6, "limited_by": None}, "usage": s["usage"], "limits_rev": 1,
                "queue_len": 0, "degrade_level": "L0", "expiring": None}

    def meta(self) -> dict:
        return {"api_version": 1, "contracts": CONTRACTS_VERSION, "access_mode": "loopback", "features": ["queue", "pow", "batch"], "max_sessions": 4,
                "active_sessions": 1, "queue_len": 0, "pow": {"required_now": False, "bits": 20}, "sdk": {"min_version": "1.0.0"}}

    def worlds(self) -> dict:
        names = {"synthcity": ("合成城市", "S"), "shenzhen": ("深圳", "S"), "suzhou": ("苏州", "S"), "newyork": ("纽约", "S"),
                 "chicago": ("芝加哥", "L"), "shanghai": ("上海", "L"), "sanfrancisco": ("旧金山", "L")}
        items = []
        for w, (zh, tier) in names.items():
            items.append({"world_id": w, "name_zh": zh, "tier": tier, "available": True, "reason": None, "admit_now": True,
                          "cost": {"anon_mb": 140.0 if tier == "S" else 155.0, "ws_mb": 60.0 if tier == "S" else 160.0},
                          "dataset": {"name": "synthcity" if w == "synthcity" else "UrbanScene3D",
                                      "terms_zh": "合成数据" if w == "synthcity" else "经数据集权利方授权使用", "citation": "" if w == "synthcity" else "ECCV 2022"},
                          "templates": [f"t{k}_{n}" for k, n in enumerate(("point_watch", "polyline", "area_patrol", "scan_gsd", "area_guard", "manual"), 1)]})
        return {"items": items}

    def templates(self, world: str | None = None) -> dict:
        w = world or self.world
        rows = [("t1_point_watch", "定点巡航"), ("t2_polyline", "多点巡线"), ("t3_area_patrol", "多机区域巡查"), ("t4_scan_gsd", "GSD 完整扫描"),
                ("t5_area_guard", "区域值守与接力"), ("t6_manual", "手动飞行与传感器视图")]
        return {"items": [{"template_id": tid, "world_id": w, "name_zh": zh, "summary_zh": zh, "first_feedback_zh": "明确捕获提示",
                           "entities": {"vehicles": 2, "targets": 2, "nests": 1, "tasks": 1}, "calendar_default": {"start_local": "10:00", "tz": "Asia/Shanghai"},
                           "recommended": tid == "t5_area_guard"} for tid, zh in rows]}

    # -------------------------------------------------------------- events (AWR-17 §17.7.5)
    def events_at(self, t_s: float) -> list[tuple[str, int, dict, str | None]]:
        """(type, level, data, uav) scheduled at whole simulation seconds of the fake timeline."""
        u0 = self.uav_ids[0]
        u1 = self.uav_ids[min(1, len(self.uav_ids) - 1)]
        k = round(t_s)
        sched: dict[int, list] = {
            1: [("task.created", 1, {"tid": "task-01", "type": "area_guard", "from": None, "to": "PLANNING", "code": 0, "why": None}, None),
                ("task.allocated", 0, {"tid": "task-01", "items": [{"wid": "task-01-w1", "uav": u0}]}, None),
                ("nest.launch", 0, {"nest": "nest-01", "uav": u0, "tid": "task-01", "wid": "task-01-w1"}, u0),
                ("perception.detect", 0, {"uav": u0, "sensor": "mmw77", "sensor_kind": "radar", "track_id": 101, "level": "D"}, u0)],
            2: [("perception.level", 0, {"uav": u0, "sensor": "eo_gx40", "target": "person-01", "level_from": "D", "level_to": "R", "n_eff": 15.2,
                                         "n_req": 14.0, "range_m": 612.4, "gsd_m": 0.037, "p": 0.93, "limiting": None}, u0),
                ("target.state", 0, {"target": "dog-01", "from": "UNAWARE", "to": "SUSPICIOUS", "reason": "dwell"}, None)],
            3: [("perception.capture", 1, {"uav": u0, "sensor": "eo_gx40", "target": "person-01", "level_from": "D", "level_to": "R", "level_req": "R",
                                           "n_eff": 15.2, "n_req": 14.0, "range_m": 612.4, "gsd_m": 0.037, "p": 0.93, "limiting": None, "t_hold_s": 1.0}, u0),
                ("task.created", 1, {"tid": "task-02", "type": "relay_watch", "from": None, "to": "RUNNING", "code": 0, "why": None}, None),
                ("relay.dispatch", 1, {"tid": "task-02", "target": "person-01", "uav": u0, "reason": "initial", "eta_s": 0.0, "station": "st-1"}, u0)],
            4: [("uav.keepout_guard", 1, {"uav": u1, "target": "person-01", "vol_idx": 0, "d_m": 4.2, "clamped": True}, u1),
                ("sandbox.rate", 1, {"sid": self.sid, "requested": 10, "granted": 5, "cap": 5.6, "limited_by": "cost_cap", "code": 501, "why": "grant"}, None)],
            5: [("target.discovered", 3, {"target": "dog-01", "uav": u1, "kind": "acoustic", "mode": "geometric", "range_m": 180.5, "margin_db": None,
                                          "margin_ratio": None, "count": 1, "vol_idx": 1}, u1),
                ("target.state", 0, {"target": "dog-01", "from": "SUSPICIOUS", "to": "REACTING", "reason": "dwell"}, None),
                ("nest.dispatch_delayed", 2, {"nest": "nest-01", "tid": "task-02", "wid": "task-02-w2", "reason": "battery", "delay_s": 120.0}, None)],
            6: [("relay.handover", 1, {"tid": "task-02", "from_uav": u0, "to_uav": u1, "overlap_s": 18.0}, None),
                ("task.reallocated", 0, {"tid": "task-01", "items": [{"wid": "task-01-w1", "uav": u1}], "wid": "task-01-w1", "from_uav": u0, "to_uav": u1,
                                         "reason": "energy"}, None),
                ("nest.recover", 0, {"nest": "nest-01", "uav": u0, "tid": "task-02", "wid": "task-02-w1"}, u0),
                ("nest.swap_done", 0, {"nest": "nest-01", "uav": u0, "tid": None, "wid": None, "pack_id": "pk-2", "soc_pct": 100.0}, u0)],
            7: [("perception.capture_lost", 1, {"uav": u0, "sensor": "eo_gx40", "target": "person-01", "reason": "los"}, u0),
                ("relay.gap", 2, {"tid": "task-02", "target": "person-01", "t_start_ns": 6_500_000_000, "duration_s": 0.5, "cause": "keepout_priority"}, None),
                ("task.state", 1, {"tid": "task-01", "type": "area_guard", "from": "RUNNING", "to": "PAUSED", "code": 0, "why": None}, None),
                ("fw.wind_exceeds", 2, {"uav": u1, "wind_mps": 14.0, "airspeed_mps": 13.5, "fallback": "racetrack"}, u1),
                ("sandbox.expiring", 2, {"sid": self.sid, "reason": "lifetime", "in_s": 60, "at_unix_ns": "1791169923000000000"}, None),
                ("sandbox.state", 1, {"sid": self.sid, "from": "ACTIVE", "to": "IDLE", "reason": "no_clients"}, None)],
        }
        return sched.get(k, []) if abs(t_s - k) < 1e-9 else []


LIMITS = {"vehicles_max": 12, "targets_max": 30, "nests_max": 4, "tasks_active_max": 6, "cloned_models_max": 8, "rest_rps": 10, "rest_burst": 30,
          "call_rps": 20, "call_burst": 40, "clock_rps": 2, "entity_rps": 5, "task_rps": 1, "ws_max": 4, "setpoint_hz_max": 50, "snapshot_kib": 512,
          "plan": {"area_max_m2": 1000000, "vertices_max": 64, "strips_max": 200, "work_items_max": 50, "station_candidates_max": 2400,
                   "coverage_cells_max": 250000}}

# topic -> (channel kind, encoding, schemaName) is taken from awr.contracts.topics; this table lists what D2Synth produces per tick
D2_TOPICS = ("swarm/target/state", "target/{id}/detail", "uav/{id}/perception", "uav/{id}/payload", "uav/{id}/sensor/nav",
             "uav/{id}/sensor/radar/tracks", "uav/{id}/sensor/acoustic/bearings", "nest/{id}/status", "task/{tid}/status", "relay/{tid}/stats",
             "sandbox/session")
SANDBOX_ONLY_EVENT = "sandbox.slot_ready"   # delivered through the SB-05 long poll only, never in a session event ring


def event_types() -> list[str]:
    s = D2Synth(["uav0001", "uav0002"])
    seen = []
    for k in range(10):
        for typ, *_ in s.events_at(float(k)):
            if typ not in seen:
                seen.append(typ)
    return seen


def jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    return x
