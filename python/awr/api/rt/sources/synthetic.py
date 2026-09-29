"""SyntheticSource：以真实 Gateway 协议栈合成数据的早期数据源（M11-FR-099；M11-AC-040；ADR-050；D1-AC-35）。

SyntheticSource 是 sim-core 的进程内替身（生产者侧）：按 `hz` 写同一布局的 StateRing（默认 LocalRing）——N ∈ {1, 200, 1000}
的 Lite32 与 Full64（`orbit` 圆周或 `lissajous` 八字轨迹）——并在同一总线命名空间上提供 `ctl/sim-core/{roster,cmd,lease,clock,
query}`（命令一律准入并按 accepted → running → succeeded 走完生命周期，批量命令按 `per_uav` 回复）、按 `events_per_s` 发布
事件（EventPublisher，按步合批、`_replay`）、1 Hz `state/sim-core/env` 环境关键帧心跳与 2 Hz `state/sim-core/ext`。
Gateway 以 LiveSource 读它，下游路径（调度、编码、TIME、事件、RPC）与真实运行完全相同，因此 `fake_gateway_app()`
得到的是"真实协议栈 + 合成数据"。`.awrrt` 的逐字节回放仍由 `tools/fake/fake_gw.py --replay` 负责（读写库为
`awr.contracts.frame.read_awrrt/write_awrrt`，采集见 `rt/awrrt.py`）。

用法：`python -m awr.api.rt.sources.synthetic --n 200 --port 8097`（只监听回环）；测试与 fake_gw：
`app, src = fake_gateway_app(n=200)`，由调用方用 uvicorn 服务 app，结束时 `src.stop()`。
"""

from __future__ import annotations

import argparse
import contextlib
import math
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from awr.contracts import LAYOUT_ID, bus_keys
from awr.contracts.enums import TimeState
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32, pack_fs
from awr.contracts.presets import PRESETS_SHA256
from awr.runtime.bus import LocalBus
from awr.runtime.events import EventPublisher
from awr.runtime.statering import LocalRing

__all__ = ["SyntheticSource", "fake_gateway_app", "main"]

PROD = "sim-core"
FLYING = 5
EXEC_S = 0.5


class SyntheticSource:
    def __init__(self, n: int, *, pattern: str = "orbit", hz: float = 125.0, env: bool = True,
                 events_per_s: float = 5.0, seed: int = 7, awrrt: Path | None = None, settings: Any = None,
                 ring_cls: type = LocalRing, bus: Any = None, ring_path: Path | None = None) -> None:
        if not 1 <= n <= 1024:
            raise ValueError("n ∈ [1, 1024]")
        if pattern not in ("orbit", "lissajous"):
            raise ValueError("pattern ∈ {orbit, lissajous}（.awrrt 回放由 fake_gw --replay 负责）")
        self.n, self.pattern, self.hz, self.env_on, self.events_per_s = n, pattern, float(hz), env, float(events_per_s)
        self.awrrt = awrrt
        self.rng = np.random.default_rng(seed)
        self.settings = settings
        if ring_path is None:
            ring_path = settings.ring_path if settings is not None else Path(f"/dev/shm/awr/synthetic-{seed}/state.sim-core")
        self.ring_path = Path(ring_path)
        self.ring_path.parent.mkdir(parents=True, exist_ok=True)
        self.ring, _ = ring_cls.open_or_create(ring_path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0,
                                               id_count=1024)
        ns = settings.namespace if settings is not None else "local"
        self.bus = bus if bus is not None else LocalBus.open(PROD, namespace=ns)
        h = self.ring.header()
        self.epoch, self.segment = h.epoch, h.segment
        self.events = EventPublisher(self.bus, PROD, self.epoch)
        self.ids = ["p600-01"] if n == 1 else [f"uav{i + 1:04d}" for i in range(n)]
        self.phase = self.rng.uniform(0, 2 * math.pi, n)
        self.radius = self.rng.uniform(20, 200, n)
        self.alt = self.rng.uniform(20, 80, n)
        self.full = np.zeros(n, DRONE_STATE64)
        self.lite = np.zeros(n, SWARM_LITE32)
        self.full["agent_no"] = self.lite["agent_no"] = np.arange(n)
        self.full["flight_state"] = self.lite["flight_state"] = pack_fs(FLYING)
        self.full["flags"] = self.lite["flags"] = 0x37
        self.full["battery_pct"] = self.lite["battery_pct"] = 255
        self.full["mission_item"] = 0xFFFF
        self.t_sim_ns = 0
        self.k = 0
        self.state = int(TimeState.PLAYING)
        self.rate = 1.0
        self.seat: dict[str, Any] = {"state": "FREE", "holder": None}
        self.calls: dict[str, dict] = {}
        self.idem: dict[str, dict] = {}
        self.env_version = 1
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.handles = [self.bus.serve(k, lambda r, kind=kind: self.inbox.put((kind, r))) for k, kind in (
            (bus_keys.ctl_cmd(PROD), "cmd"), (bus_keys.CTL_CLOCK, "clock"), (bus_keys.CTL_LEASE, "lease"),
            (bus_keys.ctl_roster(PROD), "roster"), (bus_keys.CTL_QUERY, "query"))]
        self.pub_ext = self.bus.publisher(bus_keys.state_ext(PROD))
        self.pub_env = self.bus.publisher(bus_keys.STATE_ENV)
        self.pub_perf = self.bus.publisher(bus_keys.STATE_PERF)
        self.stop_ev = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="synthetic-source", daemon=True)
        self._ready: Any = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> SyntheticSource:
        self.step()
        self.events.emit("sim.started", t_sim_ns=0, severity=1, epoch=self.epoch, segment=self.segment,
                         reason="cold_start", kernel="numpy", n=self.n)
        self._ready = self.bus.ready()
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_ev.set()
        if self.thread.is_alive():
            self.thread.join(5)
        for h in self.handles:
            with contextlib.suppress(Exception):
                h.close()
        with contextlib.suppress(Exception):
            self.events.close()
        if self._ready is not None:
            self._ready.close()
        self.bus.close()
        with contextlib.suppress(Exception):
            type(self.ring).remove(self.ring_path)  # LocalRing；mmap 版由 supervisor 清理 /dev/shm/awr/<run>

    def _loop(self) -> None:
        period = 1.0 / self.hz
        nxt = time.monotonic()
        while not self.stop_ev.is_set():
            self.step()
            nxt += period
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.monotonic()

    # ------------------------------------------------------------ 一步
    def step(self) -> None:
        self.k += 1
        if self.state == int(TimeState.PLAYING):
            self.t_sim_ns += int(self.rate * 1e9 / self.hz)
        self.ring.heartbeat(self.t_sim_ns, self.state, int(self.rate * 1000), step_seq=self.k)
        self._drain()
        self._advance()
        self._kinematics(self.t_sim_ns / 1e9)
        self.ring.publish(self.full, self.lite, self.t_sim_ns, 1)
        per = max(1, int(self.hz))
        if self.k % max(1, per // 2) == 0:
            self.pub_ext.put(msgpack.packb([[i, {"lifecycle": "READY", "lease": {"owner": "NONE", "holder": None}}]
                                            for i in range(self.n)], use_bin_type=True))
        if self.k % per == 0:
            if self.env_on:
                self.pub_env.put(msgpack.packb(self._env(), use_bin_type=True))
            self.pub_perf.put(msgpack.packb({"v": 1, "stage_ms_per_s": {}, "n_active": self.n, "kernel": "numpy",
                                             "cpu_pct": 0.0}, use_bin_type=True))
        if self.events_per_s > 0 and self.rng.random() < self.events_per_s / self.hz:
            i = int(self.rng.integers(self.n))
            self.events.emit("uav.state", t_sim_ns=self.t_sim_ns, severity=int(self.rng.integers(0, 2)), uav=self.ids[i],
                             **{"from": "FLYING", "to": "FLYING", "reason": "synthetic"})
        self.events.flush()

    def _kinematics(self, t: float) -> None:
        w = 0.1 * (40.0 / self.radius)
        a = self.phase + w * t
        if self.pattern == "orbit":
            x, y = self.radius * np.cos(a), self.radius * np.sin(a)
            vx, vy = -self.radius * w * np.sin(a), self.radius * w * np.cos(a)
        else:
            x, y = self.radius * np.sin(a), self.radius * np.sin(2 * a) / 2
            vx, vy = self.radius * w * np.cos(a), self.radius * w * np.cos(2 * a)
        yaw = np.arctan2(vy, vx)
        pos = np.stack([x, y, self.alt], axis=1).astype(np.float32)
        vel = np.stack([vx, vy, np.zeros(self.n)], axis=1).astype(np.float32)
        q = np.stack([np.zeros(self.n), np.zeros(self.n), np.sin(yaw / 2), np.cos(yaw / 2)], axis=1).astype(np.float32)
        self.full["pos"], self.full["vel"], self.full["q"] = pos, vel, q
        self.lite["pos"] = pos
        self.lite["q_snorm"] = np.clip(np.round(q * 32767), -32767, 32767).astype(np.int16)
        self.lite["vel_cms"] = np.clip(np.round(vel * 100), -32767, 32767).astype(np.int16)

    def _env(self) -> dict:
        return {"version": self.env_version, "epoch": 1, "seed": 7, "t_ns": self.t_sim_ns, "mode": "hold",
                "config": {"presets_sha256": PRESETS_SHA256}}

    # ------------------------------------------------------------ 服务
    def _drain(self) -> None:
        while True:
            try:
                kind, req = self.inbox.get_nowait()
            except queue.Empty:
                return
            try:
                m = req.msg() or {}
                req.reply_msg(getattr(self, f"_h_{kind}")(m))
            except Exception:
                req.close()

    def _h_roster(self, m: dict) -> dict:
        return {"v": 1, "producer": PROD, "roster_version": 1, "id_base": 0, "id_count": 1024,
                "entries": [{"agent_no": i, "id": vid, "kind": "uav", "model": "p600", "profile_id": "p600_mid360",
                             "backend": "mock", "simulated": True, "producer": PROD, "lifecycle": "READY", "sensors": [],
                             "t_world_local": None, "caps_ref": "mock"} for i, vid in enumerate(self.ids)]}

    def _h_cmd(self, m: dict) -> dict:
        cid = str(m.get("cid"))
        base = {"v": 1, "cid": cid, "t_sim_ns": self.t_sim_ns, "epoch": self.epoch, "segment": self.segment}
        if cid in self.idem:
            c = self.calls.get(cid) or {}
            return base | {"status": "duplicate", "code": 0,
                           "call_state": {"status": c.get("status", "succeeded"), "code": 0, "final": c.get("final", True)}}
        uav, op = m.get("uav"), str(m.get("op"))
        if isinstance(uav, list):
            ok = [u for u in uav if u in self.ids]
            for u in ok:
                self._accept(f"{cid}:{u}", op.rsplit("/", 1)[-1], u, cid)
            rep = base | {"status": "accepted", "code": 0,
                          "per_uav": {"accepted": ok, "rejected": [[u, 107] for u in uav if u not in self.ids]}}
        elif isinstance(uav, str) and uav not in self.ids:
            rep = base | {"status": "rejected", "code": 107}
        else:
            self._accept(cid, op, uav if isinstance(uav, str) else None, m.get("batch_id"))
            rep = base | {"status": "accepted", "code": 0, "apply_tick": self.k + 1}
        self.idem[cid] = rep
        return rep

    def _accept(self, cid: str, op: str, uav: str | None, batch_id: str | None) -> None:
        self.calls[cid] = {"op": op, "uav": uav, "batch_id": batch_id, "status": "accepted", "final": False,
                           "t": time.monotonic()}
        self._ev("cmd.accepted", cid)

    def _ev(self, kind: str, cid: str) -> None:
        c = self.calls[cid]
        ok = kind == "cmd.succeeded"
        self.events.emit(kind, t_sim_ns=self.t_sim_ns, severity=0, uav=c["uav"], cid=cid, batch_id=c["batch_id"],
                         op=c["op"], code=0, effect={"status": "OK" if ok else "UNVERIFIED", "verify_trust": 4 if ok else 1,
                                                     **({"simulated": True, "metrics": {"t_exec_s": EXEC_S}} if ok else {})})

    def _advance(self) -> None:
        now = time.monotonic()
        for cid, c in list(self.calls.items()):
            if c["final"]:
                continue
            if c["status"] == "accepted" and now - c["t"] > 0.02:
                c["status"] = "running"
                self._ev("cmd.running", cid)
            elif c["status"] == "running" and now - c["t"] > EXEC_S:
                c["status"], c["final"] = "succeeded", True
                self._ev("cmd.succeeded", cid)
        if len(self.calls) > 8192:
            for cid in [k for k, c in self.calls.items() if c["final"]][:4096]:
                self.calls.pop(cid, None)
                self.idem.pop(cid, None)

    def _h_clock(self, m: dict) -> dict:
        op = m.get("op")
        if op == "pause":
            self.state = int(TimeState.PAUSED)
        elif op == "play":
            self.state = int(TimeState.PLAYING)
        elif op == "speed":
            self.rate = float((m.get("args") or {}).get("rate", 1.0))
        elif op == "reset":
            self.segment += 1
            self.epoch += 1
            self.ring.set_segment(self.segment)
            self.ring.set_epoch(self.epoch)
            self.events.set_epoch(self.epoch)
            self.t_sim_ns = 0
            self.events.emit("sim.reset", t_sim_ns=0, severity=1, epoch=self.epoch, segment=self.segment,
                             reason="scenario_reset", kernel="numpy", n=self.n)
        return {"v": 1, "cid": m.get("cid"), "status": "accepted", "code": 0,
                "clock": {"state": TimeState(self.state).name, "rate": self.rate, "t_sim_ns": self.t_sim_ns,
                          "epoch": self.epoch, "segment": self.segment}}

    def _h_lease(self, m: dict) -> dict:
        op = str(m.get("op"))
        pid = str((m.get("principal") or {}).get("principal_id"))
        code = 0
        if op == "seat_claim":
            if self.seat["holder"] in (None, pid):
                self.seat = {"state": "HELD", "holder": pid}
            else:
                code = 116
        elif op in ("seat_grace", "seat_resume") and self.seat["holder"] == pid:
            self.seat = {"state": "GRACE" if op == "seat_grace" else "HELD", "holder": pid}
        elif op in ("seat_expire", "seat_release") and self.seat["holder"] == pid:
            self.seat = {"state": "FREE", "holder": None}
        elif op == "seat_takeover":
            self.seat = {"state": "HELD", "holder": pid}
        return {"v": 1, "cid": m.get("cid"), "status": "rejected" if code else "accepted", "code": code, "lease": None,
                "seat": dict(self.seat)}

    def _h_query(self, m: dict) -> dict:
        pts = (m.get("args") or {}).get("points") or []
        return {"v": 1, "wind_mps": [[0.0, 0.0, 0.0]] * len(pts)}


def fake_gateway_app(n: int = 200, *, world_id: str = "shenzhen", pattern: str = "orbit", hz: float = 125.0,
                     events_per_s: float = 5.0, seed: int = 7, **settings_over: Any) -> tuple[Any, SyntheticSource]:
    """真实 Gateway 应用（LocalBus + LocalRing）+ 已启动的 SyntheticSource；调用方负责用 uvicorn 服务并在结束时 stop()。"""
    from ...inproc import inproc_settings
    from ...main import create_app

    s = inproc_settings(world_id)
    if settings_over:
        s = replace(s, **settings_over)
    src = SyntheticSource(n, pattern=pattern, hz=hz, events_per_s=events_per_s, seed=seed, settings=s).start()
    return create_app(s, ring_cls=LocalRing), src


def main(argv: list[str] | None = None) -> int:
    import shutil

    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m awr.api.rt.sources.synthetic",
                                 description="真实 Gateway 协议栈 + 合成数据（早期数据源，只监听回环）")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--port", type=int, default=8097)
    ap.add_argument("--pattern", default="orbit", choices=("orbit", "lissajous"))
    ap.add_argument("--hz", type=float, default=125.0)
    ap.add_argument("--events-per-s", type=float, default=5.0)
    ap.add_argument("--world", default="shenzhen")
    a = ap.parse_args(argv)
    app, src = fake_gateway_app(a.n, world_id=a.world, pattern=a.pattern, hz=a.hz, events_per_s=a.events_per_s)
    print(f"READY synthetic n={a.n} api http://127.0.0.1:{a.port}/world/{a.world}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port, ws="websockets", ws_per_message_deflate=False,
                    ws_max_size=262144, log_level="warning")
    finally:
        src.stop()
        shutil.rmtree(src.settings.run_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
