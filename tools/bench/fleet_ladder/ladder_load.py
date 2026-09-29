"""fleet_ladder 负载驱动（M08-FR-078；M16 §6.4.8 `ladder-shenzhen` 的等价载荷）：以席位持有者（operator，入口签名）经总线向
sim-core 下发命令：批量起飞到 4 层交错高度，随后每架绕出生点环绕（orbit，持续）；`--churn <s>` 时每 s 秒【仿真】在本机 24 m
单元内随机 goto（按 slot 升序、固定种子）。剧本加载器（M10）与剧本文件（M16）就绪后改用 `--scenario ladder-shenzhen`。
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import numpy as np

from awr.contracts import bus_keys
from awr.runtime.principal import Principal, derive_key, sign_principal

__all__ = ["LadderLoad"]

LAYERS_M = (30.0, 38.0, 46.0, 54.0)


class LadderLoad:
    def __init__(self, bus: Any, run_id: str, secret: bytes, *, seed: int = 7) -> None:
        self.bus = bus
        self.k = derive_key(secret, run_id, "entry")
        self.pid = "p-" + "fleetladder" + secrets.token_hex(3)
        self.rng = np.random.default_rng(seed)
        self.n_cmd = 0
        self.failed = 0

    def principal(self, cid: str) -> dict:
        p = Principal(self.pid, "operator", "api", "bench", True)
        d = p.fields()
        d["sig"] = sign_principal(p, cid, self.k)
        return d

    def _call(self, key: str, msg: dict, timeout: float = 2.0) -> dict | None:
        out: dict = {}
        done = []

        def cb(rep, err):
            out["rep"] = rep if err is None else None
            done.append(1)

        self.bus.call_cb(key, msg, cb, timeout=timeout, retries=1)
        t_end = time.monotonic() + timeout * 3
        while not done and time.monotonic() < t_end:
            time.sleep(0.005)
        return out.get("rep")

    def cmd(self, op: str, uav: Any, args: dict) -> dict | None:
        self.n_cmd += 1
        cid = f"fl-{self.n_cmd:07d}"
        rep = self._call(bus_keys.ctl_cmd("sim-core"), {"v": 1, "cid": cid, "op": op, "uav": uav, "args": args,
                                                         "principal": self.principal(cid), "lease": None, "t_wall_ns": 0,
                                                         "epoch_seen": 0, "batch_id": None})
        if rep is None or rep.get("status") == "rejected":
            self.failed += 1
        return rep

    def seat(self) -> bool:
        cid = "fl-seat"
        rep = self._call(bus_keys.CTL_LEASE, {"v": 1, "cid": cid, "op": "seat_claim", "principal": self.principal(cid)})
        return bool(rep and rep.get("code") == 0)

    def roster(self) -> list[dict]:
        rep = self._call(bus_keys.ctl_roster("sim-core"), {"v": 1})
        return list((rep or {}).get("entries") or [])

    def positions(self) -> dict[str, list[float]]:
        """`ctl/sim-core/query{op: fleet/vehicles}` 的当前 ENU 位置（M08 的只读查询）。"""
        rep = self._call(bus_keys.CTL_QUERY, {"v": 1, "op": "fleet/vehicles", "args": {}}, timeout=3.0)
        return {it["id"]: it["pos_enu_m"] for it in (rep or {}).get("items", [])}

    def takeoff_layers(self, ids: list[str]) -> None:
        for k, vid in enumerate(ids):
            self.cmd("takeoff", vid, {"alt_m": LAYERS_M[k % len(LAYERS_M)]})

    def orbit_all(self, ids: list[str], pos: dict[str, list[float]], radius_m: float = 8.0) -> None:
        """每架绕"当前位置西侧 radius_m 处"的圆心环绕（机体本在圆上，不需入圈）。"""
        for k, vid in enumerate(ids):
            p = pos.get(vid)
            if p is None:
                continue
            self.cmd("orbit", vid, {"center": [p[0] - radius_m, p[1], p[2]], "radius_m": radius_m, "speed_mps": 3.0,
                                    "turns": 0, "cw": bool(k % 2)})

    def churn(self, ids: list[str], pos: dict[str, list[float]]) -> None:
        """在各机 24 m 单元内随机 goto（按 slot 升序、固定种子）。"""
        for vid in ids:
            p = pos.get(vid)
            if p is None:
                continue
            dx, dy = self.rng.uniform(-12.0, 12.0, 2)
            self.cmd("goto", vid, {"pos": [p[0] + dx, p[1] + dy, p[2]], "route": "direct"})
