"""研究者接口（M26 §6.2、§7.3–§7.5）：实现 `DroneAgent.reset(config)` 与 `step(obs, inbox) -> act`。

观测与动作是 `bench/obs.v1`、`bench/act.v1` 的普通 dict（线协议解码结果与进程内交付相同）；本模块给出动作构造函数，
不做校验（Env 侧守卫按 schema 校验并回执）。消息载荷为 bytes；`encode_msg` / `decode_msg` 以 UTF-8 JSON 编码对象。
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["DroneAgent", "act", "decode_msg", "encode_msg", "gimbal_angles", "gimbal_look_at", "nav_goto", "nav_hover", "nav_land",
           "nav_takeoff", "nav_velocity", "out_msg", "report"]


class DroneAgent:
    """每架机一个实例。`config` 为 agent_config.v1（简报、机型限幅、传感器、决策时延与通信参数）。"""

    def reset(self, config: dict) -> None:
        self.config = config

    def step(self, obs: dict, inbox: list[dict]) -> dict:
        raise NotImplementedError


def act(k: int, *, nav: dict | None = None, gimbal: dict | None = None, msgs: list[dict] | None = None,
        report: list[dict] | None = None, tau_ms: int | None = None) -> dict:
    a: dict[str, Any] = {"schema": "drone4d.bench.act.v1", "k": int(k)}
    if nav is not None:
        a["nav"] = nav
    if gimbal is not None:
        a["gimbal"] = gimbal
    if msgs:
        a["msgs"] = msgs
    if report:
        a["report"] = report
    if tau_ms is not None:
        a["tau_ms"] = int(tau_ms)
    return a


def nav_velocity(v_mps, yaw_rate_rps: float | None = None, frame: str = "world") -> dict:
    return {"mode": "velocity", "vel_mps": [None if x is None else float(x) for x in v_mps], "yaw_rate_rps": yaw_rate_rps, "frame": frame}


def nav_goto(pos_enu_m, speed_mps: float | None = None) -> dict:
    n = {"mode": "goto", "pos_enu_m": [float(x) for x in pos_enu_m]}
    if speed_mps is not None:
        n["speed_mps"] = float(speed_mps)
    return n


def nav_hover() -> dict:
    return {"mode": "hover"}


def nav_takeoff(alt_agl_m: float) -> dict:
    return {"mode": "takeoff", "alt_agl_m": float(alt_agl_m)}


def nav_land() -> dict:
    return {"mode": "land"}


def gimbal_angles(sensor_id: str, pitch_deg: float, yaw_deg: float) -> dict:
    return {"sensor_id": sensor_id, "mode": "angles", "pitch_deg": float(pitch_deg), "yaw_deg": float(yaw_deg)}


def gimbal_look_at(sensor_id: str, target_enu_m) -> dict:
    return {"sensor_id": sensor_id, "mode": "look_at", "target_enu_m": [float(x) for x in target_enu_m]}


def encode_msg(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def decode_msg(m: dict):
    try:
        return json.loads(bytes(m["payload"]).decode())
    except (ValueError, UnicodeDecodeError):
        return None


def out_msg(dst: str, topic: str, obj) -> dict:
    return {"dst": dst, "topic": topic, "payload": encode_msg(obj)}


def report(type_: str, participants: list[dict], t_start_ns: int, t_end_ns: int, position_enu_m=None, confidence: float | None = None) -> dict:
    """participants: [{"track_id": str, "evidence": [{"uav", "sensor", "frame_seq", "det_id"}]}]。"""
    r: dict[str, Any] = {"type": type_, "participants": participants, "window": {"t_start_ns": int(t_start_ns), "t_end_ns": int(t_end_ns)}}
    if position_enu_m is not None:
        r["position_enu_m"] = [float(x) for x in position_enu_m]
    if confidence is not None:
        r["confidence"] = float(confidence)
    return r
