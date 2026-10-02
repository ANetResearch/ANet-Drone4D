"""fleet_ladder 负载驱动（M08-FR-078；M16 §6.4.8 `ladder-shenzhen` 的等价载荷）：以席位持有者（operator，入口签名）经总线向
sim-core 下发命令：按层批量起飞到 4 层交错高度，全部进入 FLYING 后每架绕出生点小半径环绕（orbit，持续）；`--churn <s>` 时每
s 秒【仿真】在本机单元内随机 goto（按 slot 升序、固定种子）。剧本口径另见 `run.py --scenario ladder-shenzhen`。

FX2-R2 修订（D1 验收第 1 轮 4.4、4.7）：
- 只启动 sim-core 时没有网关发 GCS 信标，operator 租约的链路源判为丢失，全部机体 HOLD 后 RTL（"机体基本停在地面"）：
  负载以 2 Hz 经 `ctl/sim-core/gcs` 发信标（与网关 `GcsBeacon` 同一格式）；
- 起飞改为每层一条批量命令（`uav` 为 id 列表，sim-core 分块准入），等 ≥ 95% 机体 FLYING 后再下发环绕（此前固定等 15 s，
  爬升到 54 m 需约 40 s，环绕全部被 105 拒绝）；
- 层高间隔 12 m（M09 间距 `rearm_m`）、环绕半径 3 m：此前层间 8 m 与 6 m 网格使相邻机体 3D 距离约 10 m，持续触发 M09 间距告警
  与让行，8 m 半径的相邻环绕圆相交，环绕中有机体相撞，测得的是冲突处理而不是稳态负载。
"""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from typing import Any

import msgpack
import numpy as np

from awr.contracts import bus_keys
from awr.runtime.principal import Principal, derive_key, sign_principal

__all__ = ["LAYERS_M", "LadderLoad"]

LAYERS_M = (30.0, 42.0, 54.0, 66.0)
GCS_PERIOD_S = 0.5


class LadderLoad:
    def __init__(self, bus: Any, run_id: str, secret: bytes, *, seed: int = 7) -> None:
        self.bus = bus
        self.k = derive_key(secret, run_id, "entry")
        self.pid = "p-" + "fleetladder" + secrets.token_hex(3)
        self.rng = np.random.default_rng(seed)
        self.n_cmd = 0
        self.failed = 0
        self.rejected: dict[str, int] = {}
        self._stop = threading.Event()
        self._beacon: threading.Thread | None = None
        self.marks: list[dict] = []
        self._mark_sub: Any = None

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

    def cmd(self, op: str, uav: Any, args: dict, timeout: float = 2.0) -> dict | None:
        self.n_cmd += 1
        cid = f"fl-{self.n_cmd:07d}"
        rep = self._call(bus_keys.ctl_cmd("sim-core"), {"v": 1, "cid": cid, "op": op, "uav": uav, "args": args,
                                                         "principal": self.principal(cid), "lease": None, "t_wall_ns": 0,
                                                         "epoch_seen": 0, "batch_id": None}, timeout=timeout)
        if rep is None or rep.get("status") == "rejected":
            self.failed += 1
            key = f"{op}:{(rep or {}).get('code', 'timeout')}"
            self.rejected[key] = self.rejected.get(key, 0) + 1
        return rep

    def seat(self) -> bool:
        cid = "fl-seat"
        rep = self._call(bus_keys.CTL_LEASE, {"v": 1, "cid": cid, "op": "seat_claim", "principal": self.principal(cid)})
        ok = bool(rep and rep.get("code") == 0)
        if ok:
            self.start_beacon()
        return ok

    # ------------------------------------------------------------ GCS 信标（网关缺席时由负载代发）
    def start_beacon(self) -> None:
        if self._beacon is not None:
            return
        pub = self.bus.publisher(bus_keys.CTL_GCS)

        def run() -> None:
            seq = 0
            while not self._stop.wait(GCS_PERIOD_S if seq else 0.0):
                with contextlib.suppress(Exception):
                    pub.put(msgpack.packb({"v": 1, "seq": seq, "principal_id": self.pid, "seat_state": "HELD",
                                           "ping_age_ms": 0}, use_bin_type=True))
                seq += 1

        self._beacon = threading.Thread(target=run, name="fleet-ladder-gcs", daemon=True)
        self._beacon.start()

    def close(self) -> None:
        self._stop.set()
        if self._beacon is not None:
            self._beacon.join(timeout=2.0)

    # ------------------------------------------------------------ 查询
    def roster(self) -> list[dict]:
        rep = self._call(bus_keys.ctl_roster("sim-core"), {"v": 1})
        return list((rep or {}).get("entries") or [])

    def vehicles(self) -> list[dict]:
        """`ctl/sim-core/query{op: fleet/vehicles}`（M08 的只读查询；N = 1000 时为一次数毫秒的慢任务，只在测量窗口外调用）。"""
        rep = self._call(bus_keys.CTL_QUERY, {"v": 1, "op": "fleet/vehicles", "args": {}}, timeout=5.0)
        return list((rep or {}).get("items") or [])

    def positions(self) -> dict[str, list[float]]:
        return {it["id"]: it["pos_enu_m"] for it in self.vehicles()}

    def wait_flying(self, ids: list[str], *, frac: float = 0.95, timeout_s: float = 150.0, poll_s: float = 3.0) -> dict:
        """等 ≥ frac 的机体进入 FLYING（起飞调用完成）；返回 {flying, total, waited_s, by_state}。"""
        t0 = time.monotonic()
        want = set(ids)
        by: dict[str, int] = {}
        while True:
            items = [it for it in self.vehicles() if it.get("id") in want]
            by = {}
            for it in items:
                by[str(it.get("flight_state"))] = by.get(str(it.get("flight_state")), 0) + 1
            fly = by.get("FLYING", 0)
            if fly >= frac * max(1, len(want)) or time.monotonic() - t0 > timeout_s:
                return {"flying": fly, "total": len(want), "waited_s": round(time.monotonic() - t0, 1), "by_state": by}
            time.sleep(poll_s)

    def watch_marks(self) -> None:
        """订阅 sim-core 的 mission 类事件（`scenario.mark` 的 category，17 §9.3），记录剧本标记。"""
        def on(_k: str, raw: bytes) -> None:
            try:
                for ev in msgpack.unpackb(raw, raw=False):
                    if ev.get("kind") == "scenario.mark":
                        self.marks.append(ev)
            except Exception:
                pass

        self._mark_sub = self.bus.subscribe(bus_keys.evt("sim-core", "mission"), on)

    def wait_mark(self, label: str, timeout_s: float) -> float | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if any((m.get("data") or {}).get("label") == label for m in self.marks):
                return time.monotonic() - t0
            time.sleep(0.5)
        return None

    # ------------------------------------------------------------ 负载
    def takeoff_layers(self, ids: list[str]) -> None:
        """每层一条批量起飞（`uav` 为 id 列表；sim-core 分块准入，ADR-065）。"""
        for li, alt in enumerate(LAYERS_M):
            grp = [vid for k, vid in enumerate(ids) if k % len(LAYERS_M) == li]
            for i in range(0, len(grp), 1000):
                self.cmd("takeoff", grp[i:i + 1000], {"alt_m": alt}, timeout=10.0)

    def orbit_all(self, ids: list[str], pos: dict[str, list[float]], radius_m: float = 3.0) -> None:
        """每架绕"当前位置西侧 radius_m 处"的圆心环绕（机体本在圆上，不需入圈）；同层相邻机体（同向、同角速度）保持出生
        网格间距（≥ 2 × 6 m）。"""
        for k, vid in enumerate(ids):
            p = pos.get(vid)
            if p is None:
                continue
            self.cmd("orbit", vid, {"center": [p[0] - radius_m, p[1], p[2]], "radius_m": radius_m, "speed_mps": 3.0,
                                    "turns": 0, "cw": bool(k % 2)})

    def churn(self, ids: list[str], pos: dict[str, list[float]]) -> None:
        """在各机 ±3 m 单元内随机 goto（按 slot 升序、固定种子）。"""
        for vid in ids:
            p = pos.get(vid)
            if p is None:
                continue
            dx, dy = self.rng.uniform(-3.0, 3.0, 2)
            self.cmd("goto", vid, {"pos": [p[0] + dx, p[1] + dy, p[2]], "route": "direct"})
