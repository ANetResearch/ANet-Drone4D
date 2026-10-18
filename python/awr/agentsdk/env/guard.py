"""动作守卫与估计系换算（M26 §6.6.5；P0a）。

每个 `bench/act.v1` 先做 schema 校验（失败整条拒绝，666 `schema`），再逐项检查并给出回执 `ActionAck`：
- nav：velocity（`world` 或 `body` 系，null 轴记 0，按机型限幅档限幅 → `clamped`）；goto（导航系目标减去当前导航误差换算为
  真值系目标，速度限幅）；hover、takeoff（`alt_agl_m` 限于任务高度包络）、land；orbit、path、rtl 在 P1（663 `unsupported`）。
- gimbal：angles 按云台范围限幅；look_at 以本机导航解计算角度；zoom 只接受 1×（663 `zoom_range`）；sensor_mode 在 P0b。
- msgs：通信关闭（667 `comm_off`）、目的方不存在（667 `dst`）、载荷超长（667 `too_large`）。
- report：类型不在任务契约 `report_types` 中（663 `report_type`，FR-125）；证据引用本机未收到的检测或帧（663 `evidence_unknown`）。
守卫不调用 D2 TrustedGuard（§13 第 1 条），只以仿真时间实现同等语义。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from awr.agentsdk.schemas import bench_validator

__all__ = ["ActionGuard", "GuardResult", "Limits"]


@dataclass(frozen=True)
class Limits:
    vmax_h_mps: float = 12.0
    vup_mps: float = 3.0
    vdown_mps: float = 2.0
    yaw_rate_max_rps: float = 1.0
    acc_h_mps2: float = 3.0
    alt_agl_range_m: tuple[float, float] = (5.0, 150.0)
    cruise_mps: float = 8.0

    def config(self) -> dict:
        return {"vmax_h_mps": self.vmax_h_mps, "vup_mps": self.vup_mps, "vdown_mps": self.vdown_mps,
                "yaw_rate_max_rps": self.yaw_rate_max_rps, "acc_h_mps2": self.acc_h_mps2}


@dataclass
class GuardResult:
    nav: dict | None = None                      # {kind, vel_enu, yaw_rate, target_enu_true, speed, alt_m}
    gimbal: tuple[float, float] | None = None    # (pitch_deg, yaw_rel_deg)
    msgs: list[dict] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)
    acks: list[dict] = field(default_factory=list)

    def ack(self, k: int, item: str, index: int, result: str, code: int = 0, why: str | None = None) -> None:
        a = {"k": int(k), "item": item, "index": int(index), "result": result, "code": int(code)}
        if why:
            a["why"] = why
        self.acks.append(a)


class ActionGuard:
    def __init__(self, uav: str, peers: list[str], limits: Limits, report_types: list[str], comm_enabled: bool, msg_max_B: int,
                 cam) -> None:
        self.uav, self.peers, self.lim, self.report_types = uav, set(peers), limits, set(report_types)
        self.comm_enabled, self.msg_max_B, self.cam = comm_enabled, msg_max_B, cam
        self.seen: dict[str, int] = {}           # det_id → 帧序号（本机已交付的检测）
        self.frames: set[tuple[str, int]] = set()

    def saw(self, dets: list[dict]) -> None:
        for d in dets:
            self.seen[d["det_id"]] = d["seq"]
            self.frames.add((d["sensor_id"], d["seq"]))

    def check(self, act: dict, k_obs: int, nav_pos: np.ndarray, nav_yaw: float, e_nav: np.ndarray, ground_z: float) -> GuardResult:
        r = GuardResult()
        errs = sorted(bench_validator("act").iter_errors(act), key=lambda e: list(e.path))
        if errs:
            r.ack(k_obs, "act", 0, "rejected", 666, "schema")
            return r
        if act["k"] != k_obs:
            r.ack(k_obs, "act", 0, "rejected", 666, "k_mismatch")
            return r
        if "nav" in act:
            self._nav(r, act["nav"], k_obs, nav_yaw, e_nav, ground_z)
        if "gimbal" in act:
            self._gimbal(r, act["gimbal"], k_obs, nav_pos, nav_yaw)
        for i, z in enumerate(act.get("zoom", [])):
            ok = abs(float(z["zoom_x"]) - 1.0) < 1e-9 and z["sensor_id"] == self.cam.sensor_id
            r.ack(k_obs, "zoom", i, "applied" if ok else "rejected", 0 if ok else 663, None if ok else "zoom_range")
        for i, _m in enumerate(act.get("sensor_mode", [])):
            r.ack(k_obs, "sensor_mode", i, "rejected", 663, "unsupported")
        for i, m in enumerate(act.get("msgs", [])):
            why = (None if self.comm_enabled else "comm_off") or (None if m["dst"] == "*" or m["dst"] in self.peers else "dst") \
                or (None if isinstance(m["payload"], bytes | bytearray) else "payload_type") \
                or (None if len(m["payload"]) <= self.msg_max_B else "too_large")
            if why:
                r.ack(k_obs, "msg", i, "rejected", 667, why)
            else:
                r.msgs.append({"dst": m["dst"], "topic": m["topic"], "payload": bytes(m["payload"])})
                r.ack(k_obs, "msg", i, "applied")
        for i, rep in enumerate(act.get("report", [])):
            if rep["type"] not in self.report_types:
                r.ack(k_obs, "report", i, "rejected", 663, "report_type")
                continue
            ev = [e for p in rep["participants"] for e in p["evidence"]]
            if any(not self._known(e) for e in ev):
                r.ack(k_obs, "report", i, "rejected", 663, "evidence_unknown")
                continue
            r.reports.append(rep)
            r.ack(k_obs, "report", i, "applied")
        return r

    def _known(self, e: dict) -> bool:
        if e["uav"] != self.uav:
            return True                          # 转述他机证据：由评测端按该机的交付记录解析
        if "det_id" in e:
            return self.seen.get(e["det_id"]) == e["frame_seq"]
        return (e["sensor"], e["frame_seq"]) in self.frames

    def _nav(self, r: GuardResult, n: dict, k: int, yaw: float, e: np.ndarray, ground_z: float) -> None:
        L = self.lim
        mode = n["mode"]
        if any(isinstance(x, float) and not math.isfinite(x) for x in _flat(n)):
            r.ack(k, "nav", 0, "rejected", 663, "non_finite")
            return
        if mode == "velocity":
            v = np.array([0.0 if x is None else float(x) for x in n["vel_mps"]])
            if n["frame"] == "body":
                c, s = math.cos(yaw), math.sin(yaw)
                v[:2] = (c * v[0] - s * v[1], s * v[0] + c * v[1])
            v0 = v.copy()
            h = float(np.hypot(v[0], v[1]))
            if h > L.vmax_h_mps:
                v[:2] *= L.vmax_h_mps / h
            v[2] = min(max(v[2], -L.vdown_mps), L.vup_mps)
            yr = float(n.get("yaw_rate_rps") or 0.0)
            yrc = min(max(yr, -L.yaw_rate_max_rps), L.yaw_rate_max_rps)
            clamped = not np.allclose(v, v0) or yrc != yr
            r.nav = {"kind": "velocity", "vel_enu": tuple(float(x) for x in v), "yaw_rate": yrc}
            r.ack(k, "nav", 0, "clamped" if clamped else "applied", 0, "limit" if clamped else None)
        elif mode == "goto":
            p = np.array(n["pos_enu_m"], np.float64)
            lo, hi = L.alt_agl_range_m
            z = min(max(p[2], ground_z + lo), ground_z + hi)
            sp = float(n.get("speed_mps", L.cruise_mps))
            spc = min(max(sp, 0.5), L.vmax_h_mps)
            tgt = np.array([p[0], p[1], z]) - e                 # 估计系 → 真值系
            r.nav = {"kind": "goto", "target_enu_true": tuple(float(x) for x in tgt), "target_enu_nav": tuple(float(x) for x in (p[0], p[1], z)),
                     "speed": spc}
            clamped = z != p[2] or spc != sp
            r.ack(k, "nav", 0, "clamped" if clamped else "applied", 0, "limit" if clamped else None)
        elif mode == "takeoff":
            lo, hi = L.alt_agl_range_m
            a = float(n["alt_agl_m"])
            ac = min(max(a, lo), hi)
            r.nav = {"kind": "takeoff", "alt_m": ac}
            r.ack(k, "nav", 0, "clamped" if ac != a else "applied", 0, "limit" if ac != a else None)
        elif mode in ("hover", "land"):
            r.nav = {"kind": mode}
            r.ack(k, "nav", 0, "applied")
        else:
            r.ack(k, "nav", 0, "rejected", 663, "unsupported")

    def _gimbal(self, r: GuardResult, g: dict, k: int, nav_pos: np.ndarray, nav_yaw: float) -> None:
        if g["sensor_id"] != self.cam.sensor_id:
            r.ack(k, "gimbal", 0, "rejected", 663, "sensor")
            return
        if g["mode"] == "angles":
            p, y = float(g["pitch_deg"]), float(g["yaw_deg"])
        else:
            d = np.asarray(g["target_enu_m"], np.float64) - nav_pos
            p = math.degrees(math.atan2(d[2], max(float(np.hypot(d[0], d[1])), 1e-6)))
            y = math.degrees(math.atan2(d[1], d[0]) - nav_yaw)
        lo, hi = self.cam.pitch_range_deg
        ylo, yhi = self.cam.yaw_range_deg
        yw = (y + 180.0) % 360.0 - 180.0
        pc, yc = min(max(p, lo), hi), min(max(yw, ylo), yhi)
        r.gimbal = (pc, yc)
        clamped = g["mode"] == "angles" and (pc != p or yc != yw)
        r.ack(k, "gimbal", 0, "clamped" if clamped else "applied", 0, "limit" if clamped else None)


def _flat(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _flat(v)
    elif isinstance(x, list | tuple):
        for v in x:
            yield from _flat(v)
    else:
        yield x
