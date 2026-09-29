"""tests/rt 的可控替身：FakeSim（sim-core 的总线与 StateRing 行为）、FakeSupervisor（`sys/procs`、`sys/restart`、
`evt/supervisor/proc`）与 GwStack（FakeSim + 真实 Gateway 应用 + uvicorn 线程）。

与真实 sim-core 相比只保留 Gateway 可观察的契约行为（AWR-17 §9.2–§9.7）：LocalRing 写者（heartbeat、publish、epoch、
segment）、`ctl/sim-core/{cmd,clock,lease,roster,query}` 服务方（幂等表、批量 `per_uav`、席位与租约）、EventPublisher（按步
合批、`_replay`）、`state/sim-core/{ext,safety,sensor,detail,env,perf,mission}` 发布；记录收到的 interest、gcs、setpoint。
不加载世界、不跑动力学，测试可暂停心跳、暂停发布、模拟重开（segment + 1）与 checkpoint 恢复（epoch + 1）。
"""

from __future__ import annotations

import contextlib
import queue
import shutil
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

import msgpack
import numpy as np
import rtc

from awr.contracts import LAYOUT_ID, bus_keys
from awr.contracts.enums import TimeState
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32
from awr.contracts.presets import PRESETS_SHA256
from awr.runtime.bus import LocalBus
from awr.runtime.events import EventPublisher
from awr.runtime.principal import Principal, derive_key, verify_principal
from awr.runtime.statering import LocalRing

WORLD = rtc.WORLD
PROD = "sim-core"


@dataclass
class Call:
    cid: str
    op: str
    uav: str
    batch_id: str | None
    status: str = "accepted"
    code: int = 0
    final: bool = False
    t_accept: float = field(default_factory=time.monotonic)
    args: dict = field(default_factory=dict)

    def state(self) -> dict:
        return {"status": self.status, "code": self.code, "final": self.final, "op": self.op,
                "effect": {"status": "UNVERIFIED", "verify_trust": 2 if self.status == "running" else 1}}


class FakeSim:
    def __init__(self, settings: Any, *, n: int = 3, hz: float = 125.0, batch: bool = True, exec_s: float = 0.3,
                 hold_ops: tuple[str, ...] = ("velocity",), reject_ops: dict[str, int] | None = None,
                 sensors: bool = True, verify: bool = True, auto: bool = True) -> None:
        self.settings = settings
        self.hz = hz
        self.batch = batch
        self.exec_s = exec_s
        self.hold_ops = set(hold_ops)
        self.reject_ops = dict(reject_ops or {})
        self.k_entry = derive_key(settings.secret, settings.run_id, "entry") if verify else None
        self.ring, _ = LocalRing.open_or_create(settings.ring_path, capacity=1024, slots=32, layout_id=LAYOUT_ID,
                                                id_base=0, id_count=1024)
        h = self.ring.header()
        self.epoch, self.segment = h.epoch, h.segment
        self.bus = LocalBus.open(PROD, namespace=settings.namespace)
        self.events = EventPublisher(self.bus, PROD, self.epoch)
        self.vehicles: list[dict] = []
        for i in range(n):
            self.vehicles.append({"agent_no": i, "id": f"f{i + 1:03d}", "kind": "uav", "model": "p600",
                                  "profile_id": "p600_mid360", "backend": "mock", "simulated": True, "producer": PROD,
                                  "lifecycle": "READY",
                                  "sensors": [{"sensor_no": 0, "name": "cam0", "kind": "camera"}] if sensors else [],
                                  "t_world_local": None, "caps_ref": "mock"})
        self.roster_version = 1
        self.lock = threading.RLock()
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.idem: dict[str, tuple[dict, Call | None]] = {}
        self.calls: dict[str, Call] = {}
        self.executions: Counter[str] = Counter()
        self.commands: list[dict] = []
        self.seat: dict[str, Any] = {"state": "FREE", "holder": None}
        self.lease_ops: list[dict] = []
        self.clock_ops: list[dict] = []
        self.interest: list[dict] = []
        self.gcs: list[dict] = []
        self.setpoints: list[bytes] = []
        self.interest_set: list[int] = []
        self.t_sim_ns = 0
        self.k = 0
        self.hb_paused = False
        self.publish_paused = False
        self.env_version = 1
        self.env_sha = PRESETS_SHA256
        self.state = int(TimeState.PLAYING)
        self.handles = [
            self.bus.serve(bus_keys.ctl_cmd(PROD), lambda r: self.inbox.put(("cmd", r))),
            self.bus.serve(bus_keys.CTL_CLOCK, lambda r: self.inbox.put(("clock", r))),
            self.bus.serve(bus_keys.CTL_LEASE, lambda r: self.inbox.put(("lease", r))),
            self.bus.serve(bus_keys.ctl_roster(PROD), lambda r: self.inbox.put(("roster", r))),
            self.bus.serve(bus_keys.CTL_QUERY, lambda r: self.inbox.put(("query", r))),
            self.bus.subscribe(bus_keys.CTL_INTEREST, lambda k, raw: self.inbox.put(("interest", raw))),
            self.bus.subscribe(bus_keys.CTL_GCS, lambda k, raw: self.inbox.put(("gcs", raw))),
            self.bus.subscribe(bus_keys.CTL_SETPOINT, lambda k, raw: self.inbox.put(("setpoint", raw))),
        ]
        self.pubs = {k: self.bus.publisher(k) for k in (bus_keys.state_ext(PROD), bus_keys.state_safety(PROD),
                                                        bus_keys.state_sensor(PROD), bus_keys.STATE_DETAIL,
                                                        bus_keys.STATE_ENV, bus_keys.STATE_PERF, bus_keys.STATE_MISSION)}
        self.ready_tok = self.bus.ready()
        self.stop_ev = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="fakesim", daemon=True)
        self.auto = auto
        self.step()  # 首帧：环头部与 roster 立即可用
        if auto:
            self.thread.start()

    # ------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        period = 1.0 / self.hz
        nxt = time.monotonic()
        while not self.stop_ev.is_set():
            try:
                self.step()
            except Exception:  # 测试替身：异常不终止循环
                import traceback

                traceback.print_exc()
            nxt += period
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.monotonic()

    def step(self) -> None:
        with self.lock:
            self.k += 1
            self.t_sim_ns += int(1e9 / self.hz)
            if not self.hb_paused:
                self.ring.heartbeat(self.t_sim_ns, self.state, 1000, step_seq=self.k)
            self._drain()
            self._advance_calls()
            if not self.publish_paused:
                self.publish_frame()
            per = max(1, int(self.hz))
            if self.k % max(1, per // 2) == 0:
                self._pub_ext()
            if self.k % max(1, per // 10) == 0:
                self._pub_detail()
            if self.k % per == 0:
                self._pub_env()
                self.pubs[bus_keys.STATE_PERF].put(msgpack.packb(
                    {"v": 1, "stage_ms_per_s": {"l1": 1.5}, "n_active": len(self.vehicles), "kernel": "numpy",
                     "cpu_pct": 12.5, "geo": {"probe_qps": 0.0, "queue_len": 0}}, use_bin_type=True))
            self.events.flush()

    def publish_frame(self) -> int:
        n = len(self.vehicles)
        full = np.zeros(n, DRONE_STATE64)
        lite = np.zeros(n, SWARM_LITE32)
        t = self.t_sim_ns / 1e9
        for i, v in enumerate(self.vehicles):
            full["agent_no"][i] = lite["agent_no"][i] = v["agent_no"]
            full["flight_state"][i] = lite["flight_state"][i] = 5
            full["battery_pct"][i] = lite["battery_pct"][i] = 255
            full["pos"][i] = lite["pos"][i] = (10.0 * i + t, 0.0, 10.0)
            full["q"][i] = (0, 0, 0, 1)
        return self.ring.publish(full, lite, self.t_sim_ns, self.roster_version)

    def _pub_ext(self) -> None:
        items = [[v["agent_no"], {"lifecycle": v["lifecycle"], "lease": {"owner": "NONE", "holder": None},
                                  "k": self.k}] for v in self.vehicles]
        self.pubs[bus_keys.state_ext(PROD)].put(msgpack.packb(items, use_bin_type=True))

    def _pub_detail(self) -> None:
        ids = [no for no in self.interest_set if any(v["agent_no"] == no for v in self.vehicles)]
        if not ids:
            return
        env = np.zeros((len(ids), 32), np.uint8)
        env[:, 0] = np.asarray(ids, np.uint8)
        self.pubs[bus_keys.STATE_DETAIL].put(msgpack.packb(
            {"v": 1, "t_sim_ns": self.t_sim_ns, "agent_no": ids, "rows": env.tobytes()}, use_bin_type=True))
        self.pubs[bus_keys.state_safety(PROD)].put(msgpack.packb(
            [[no, {"active": [], "fsm": {"state": "NORMAL", "sub": 0, "latched": False}, "k": self.k}] for no in ids],
            use_bin_type=True))
        rows = np.zeros((len(ids), 48), np.uint8)
        rows[:, 0] = np.asarray(ids, np.uint8)
        self.pubs[bus_keys.state_sensor(PROD)].put(msgpack.packb(
            {"v": 1, "t_sim_ns": self.t_sim_ns, "rows": rows.tobytes()}, use_bin_type=True))

    def env_frame(self) -> dict:
        return {"version": self.env_version, "epoch": 1, "seed": 7, "t_ns": self.t_sim_ns, "mode": "hold",
                "config": {"presets_sha256": self.env_sha}}

    def _pub_env(self) -> None:
        self.pubs[bus_keys.STATE_ENV].put(msgpack.packb(self.env_frame(), use_bin_type=True))

    def emit(self, kind: str, severity: int = 0, uav: str | None = None, **data: Any) -> None:
        with self.lock:
            self.events.emit(kind, t_sim_ns=self.t_sim_ns, severity=severity, uav=uav, fields=data)

    def emit_keyframe(self) -> None:
        with self.lock:
            self.env_version += 1
            self.events.emit("env.keyframe", t_sim_ns=self.t_sim_ns, severity=0, fields=self.env_frame())

    # ------------------------------------------------------------ 服务
    def _drain(self) -> None:
        while True:
            try:
                kind, x = self.inbox.get_nowait()
            except queue.Empty:
                return
            if kind == "cmd":
                x.reply_msg(self._cmd(x.msg()))
            elif kind == "clock":
                x.reply_msg(self._clock(x.msg()))
            elif kind == "lease":
                x.reply_msg(self._lease(x.msg()))
            elif kind == "roster":
                x.reply_msg({"v": 1, "producer": PROD, "roster_version": self.roster_version, "id_base": 0,
                             "id_count": 1024, "entries": [dict(v) for v in self.vehicles]})
            elif kind == "query":
                m = x.msg() or {}
                x.reply_msg({"v": 1, "wind_mps": [[1.0, 2.0, 0.0]] * len((m.get("args") or {}).get("points") or [])})
            elif kind == "interest":
                m = msgpack.unpackb(x, raw=False)
                self.interest.append(m)
                self.interest_set = sorted(set(m.get("detail", [])) | set(m.get("marks", [])))
            elif kind == "gcs":
                self.gcs.append(msgpack.unpackb(x, raw=False))
            elif kind == "setpoint":
                self.setpoints.append(bytes(x))

    def _verified(self, m: dict) -> bool:
        p = m.get("principal")
        if not isinstance(p, dict):
            return False
        if self.k_entry is None:
            return True
        try:
            pr = Principal(str(p["principal_id"]), p["role"], p["entry"], p.get("conn_id"), bool(p.get("seat", False)))
            return verify_principal(pr, str(m.get("cid", "")), bytes(p["sig"]), self.k_entry)
        except (KeyError, TypeError, ValueError):
            return False

    def _adm(self, cid: str, status: str, code: int = 0, **kw: Any) -> dict:
        return {"v": 1, "cid": cid, "status": status, "code": code, "t_sim_ns": self.t_sim_ns, "epoch": self.epoch,
                "segment": self.segment, **kw}

    def _cmd(self, m: dict) -> dict:
        cid = str(m.get("cid"))
        self.commands.append(m)
        if cid in self.idem:
            adm, call = self.idem[cid]
            return self._adm(cid, "duplicate", 0, call_state=call.state() if call else
                             {"status": adm["status"], "code": adm["code"], "final": True})
        if not self._verified(m):
            adm = self._adm(cid, "rejected", 115)
            self.idem[cid] = (adm, None)
            return adm
        op, uav = str(m.get("op")), m.get("uav")
        if isinstance(uav, list):
            if not self.batch:
                return self._adm(cid, "rejected", 109, detail={"why": "BATCH_SKELETON_UNIMPLEMENTED"})
            known = {v["id"] for v in self.vehicles}
            acc = [u for u in uav if u in known]
            rej = [[u, 107] for u in uav if u not in known]
            sub_op = op.rsplit("/", 1)[-1]
            for u in acc:
                self._accept(f"{cid}:{u}", sub_op, u, cid, m.get("args") or {})
            adm = self._adm(cid, "accepted", 0, per_uav={"accepted": acc, "rejected": rej})
            self.idem[cid] = (adm, None)
            return adm
        if op in self.reject_ops:
            adm = self._adm(cid, "rejected", self.reject_ops[op])
            self.events.emit("cmd.rejected", t_sim_ns=self.t_sim_ns, severity=1, uav=uav, cid=cid, op=op,
                             code=self.reject_ops[op], effect={"status": "UNAVAILABLE", "verify_trust": 0})
            self.idem[cid] = (adm, None)
            return adm
        if op not in ("env/set", "env/preset") and not op.startswith("mission/") and \
                uav not in {v["id"] for v in self.vehicles}:
            return self._adm(cid, "rejected", 107)
        if op == "cancel":
            tgt = self.calls.get((m.get("args") or {}).get("call_id"))
            if tgt is None or tgt.final:
                return self._adm(cid, "rejected", 105)
            self._finish(tgt, "canceled", 6)
        call = self._accept(cid, op, str(uav), m.get("batch_id"), m.get("args") or {})
        adm = self._adm(cid, "accepted", 0, apply_tick=self.k + 1)
        self.idem[cid] = (adm, call)
        return adm

    def _accept(self, cid: str, op: str, uav: str, batch_id: str | None, args: dict) -> Call:
        call = Call(cid, op, uav, batch_id, args=dict(args))
        self.calls[cid] = call
        self.executions[cid] += 1
        self._event("cmd.accepted", call)
        return call

    def _event(self, kind: str, call: Call, **extra: Any) -> None:
        self.events.emit(kind, t_sim_ns=self.t_sim_ns, severity=0, uav=call.uav, cid=call.cid, batch_id=call.batch_id,
                         op=call.op, code=call.code, effect={"status": "OK" if kind == "cmd.succeeded" else "UNVERIFIED",
                                                             "verify_trust": 4 if kind == "cmd.succeeded" else 2},
                         **extra)

    def _finish(self, call: Call, status: str, code: int) -> None:
        if call.final:
            return
        call.status, call.code, call.final = status, code, True
        self._event(f"cmd.{status}", call)

    def _advance_calls(self) -> None:
        now = time.monotonic()
        for call in list(self.calls.values()):
            if call.final:
                continue
            if call.status == "accepted" and now - call.t_accept > 0.02:
                call.status = "running"
                self._event("cmd.running", call)
                self.events.emit("cmd.progress", t_sim_ns=self.t_sim_ns, severity=0, uav=call.uav, cid=call.cid,
                                 batch_id=call.batch_id, op=call.op, progress={"phase": "executing", "dist_m": 1.0})
            elif call.status == "running" and call.op not in self.hold_ops and now - call.t_accept > self.exec_s:
                self._finish(call, "succeeded", 0)

    def _clock(self, m: dict) -> dict:
        self.clock_ops.append(m)
        op = m.get("op")
        if op == "reset":
            self._reset_segment("scenario_reset", cancel=True)
        elif op == "pause":
            self.state = int(TimeState.PAUSED)
        elif op == "play":
            self.state = int(TimeState.PLAYING)
        return {"v": 1, "cid": m.get("cid"), "status": "accepted", "code": 0,
                "clock": {"state": TimeState(self.state).name, "rate": 1.0, "t_sim_ns": self.t_sim_ns,
                          "epoch": self.epoch, "segment": self.segment}}

    def _lease(self, m: dict) -> dict:
        self.lease_ops.append(m)
        op = str(m.get("op"))
        pid = str((m.get("principal") or {}).get("principal_id"))
        code = 0
        if op == "seat_claim":
            if self.seat["holder"] in (None, pid):
                self.seat = {"state": "HELD", "holder": pid}
                self.events.emit("seat.acquired", t_sim_ns=self.t_sim_ns, severity=1, principal_id=pid)
            else:
                code = 116
        elif op == "seat_grace" and self.seat["holder"] == pid:
            self.seat = {"state": "GRACE", "holder": pid}
        elif op == "seat_resume" and self.seat["holder"] == pid:
            self.seat = {"state": "HELD", "holder": pid}
        elif op in ("seat_expire", "seat_release") and self.seat["holder"] == pid:
            self.seat = {"state": "FREE", "holder": None}
            self.events.emit("seat.expired" if op == "seat_expire" else "seat.released", t_sim_ns=self.t_sim_ns,
                             severity=1, principal_id=pid)
        elif op == "seat_takeover":
            self.seat = {"state": "HELD", "holder": pid}
        return {"v": 1, "cid": m.get("cid"), "status": "rejected" if code else "accepted", "code": code,
                "lease": None, "seat": dict(self.seat)}

    # ------------------------------------------------------------ 测试操纵
    def _reset_segment(self, reason: str, *, cancel: bool) -> None:
        if cancel:
            for c in list(self.calls.values()):
                self._finish(c, "canceled", 6)
            self.events.flush()
        self.calls.clear()
        self.idem.clear()
        self.segment += 1
        self.epoch += 1
        self.ring.set_segment(self.segment)
        self.ring.set_epoch(self.epoch)
        self.events.set_epoch(self.epoch)
        self.events.emit("sim.reset" if reason == "scenario_reset" else "sim.started", t_sim_ns=0, severity=1,
                         epoch=self.epoch, segment=self.segment, reason=reason, kernel="numpy", n=len(self.vehicles))
        self.t_sim_ns = 0

    def crash_restart(self) -> None:
        """无 checkpoint 重开：剧本从起点（segment + 1、epoch + 1），在途调用丢失（不发终态事件）。"""
        with self.lock:
            self._reset_segment("crash_restart", cancel=False)

    def checkpoint_restore(self) -> None:
        """checkpoint 恢复：只有生产者 epoch + 1（segment 不变）。"""
        with self.lock:
            self.epoch += 1
            self.ring.set_epoch(self.epoch)
            self.events.set_epoch(self.epoch)

    def add_vehicle(self, vid: str) -> None:
        with self.lock:
            no = max(v["agent_no"] for v in self.vehicles) + 1 if self.vehicles else 0
            self.vehicles.append({**self.vehicles[0], "agent_no": no, "id": vid} if self.vehicles else
                                 {"agent_no": no, "id": vid, "kind": "uav", "producer": PROD, "sensors": []})
            self.roster_version += 1

    def remove_vehicle(self, vid: str) -> None:
        with self.lock:
            self.vehicles = [v for v in self.vehicles if v["id"] != vid]
            self.roster_version += 1

    def close(self) -> None:
        self.stop_ev.set()
        if self.thread.is_alive():
            self.thread.join(5)
        for h in self.handles:
            with contextlib.suppress(Exception):
                h.close()
        with contextlib.suppress(Exception):
            self.events.close()
        with contextlib.suppress(Exception):
            self.ready_tok.close()
        self.bus.close()
        LocalRing.remove(self.settings.ring_path)


class FakeSupervisor:
    """`sys/procs`、`sys/restart` 服务方与 `evt/supervisor/proc` 发布者（M11-R supervisor 的回复格式）。"""

    def __init__(self, settings: Any) -> None:
        self.bus = LocalBus.open("supervisor", namespace=settings.namespace)
        self.events = EventPublisher(self.bus, "supervisor", 1)
        self.procs = {"sim-core": "RUNNING", "api": "RUNNING", "recorder": "RUNNING"}
        self.restarts: list[dict] = []
        self.h = [self.bus.serve(bus_keys.SYS_PROCS, self._procs), self.bus.serve(bus_keys.SYS_RESTART, self._restart)]

    def _procs(self, req: Any) -> None:
        req.reply_msg({"v": 1, "run_id": "x", "t_wall_ns": time.time_ns(),
                       "items": [{"name": n, "state": s, "pid": 1, "restarts": 0, "last_exit": None, "uptime_s": 1.0,
                                  "hb_age_ms": 1.0, "cpu_pct": 0.0, "rss_mb": 1.0, "on_demand": False}
                                 for n, s in self.procs.items()]})

    def _restart(self, req: Any) -> None:
        m = req.msg()
        self.restarts.append(m)
        ok = m.get("name") in self.procs
        req.reply_msg({"v": 1, "cid": m.get("cid"), "status": "accepted" if ok else "rejected", "code": 0 if ok else 110})

    def set_state(self, name: str, state: str) -> None:
        prev = self.procs.get(name)
        self.procs[name] = state
        self.events.emit("proc.state", t_sim_ns=0, severity=2 if state != "RUNNING" else 1, name=name,
                         to=state, restarts=0, rc=None, reason="test", **{"from": prev})
        self.events.flush()

    def close(self) -> None:
        for h in self.h:
            h.close()
        self.events.close()
        self.bus.close()


class GwStack:
    """FakeSim + 真实 Gateway 应用（create_app）+ uvicorn 线程（端口 0 选取的空闲端口）。"""

    def __init__(self, *, n: int = 3, sim_kw: dict | None = None, supervisor: bool = False, start_sim: bool = True,
                 **over: Any) -> None:
        from awr.api.inproc import inproc_settings

        base = {"hello_timeout_s": 2.0, "serve_web": False}
        if supervisor:
            base["procs_query"] = True
        self.settings = replace(inproc_settings(WORLD), **(base | over))
        self.sup = FakeSupervisor(self.settings) if supervisor else None
        self.sim = FakeSim(self.settings, n=n, **(sim_kw or {})) if start_sim else None
        self.port = rtc.free_port()
        self._serve()
        self.base = f"http://127.0.0.1:{self.port}"
        self.ws_url = f"ws://127.0.0.1:{self.port}/api/rt"
        self.origin = f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        import uvicorn

        from awr.api.main import create_app

        self.app = create_app(self.settings, ring_cls=LocalRing)
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, ws="websockets", log_level="warning",
                             lifespan="on", ws_per_message_deflate=False, ws_max_size=262144)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name="uvicorn", daemon=True)
        self.thread.start()
        end = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > end or not self.thread.is_alive():
                raise RuntimeError("uvicorn 未启动")
            time.sleep(0.02)

    def stop_api(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)

    def restart_api(self) -> None:
        """重启 api（同一 run 目录与总线命名空间，FakeSim 不受影响）：Gateway 实例与 sessionId 更新。"""
        self.stop_api()
        self._serve()

    @property
    def ctx(self) -> Any:
        return self.app.state.awr

    @property
    def gw(self) -> Any:
        return self.app.state.awr.gateway

    def call_in_loop(self, fn: Any, *args: Any) -> Any:
        """在 Gateway 事件循环线程中执行 fn（测试直接操纵 Gateway 状态时使用），返回结果。"""
        loop = self.gw.loop
        box: dict[str, Any] = {}
        ev = threading.Event()

        def run() -> None:
            try:
                box["v"] = fn(*args)
            except BaseException as e:  # 转交测试线程
                box["e"] = e
            ev.set()

        loop.call_soon_threadsafe(run)
        if not ev.wait(5):
            raise TimeoutError("事件循环未响应")
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def wait(self, pred: Any, timeout: float = 5.0, what: str = "条件") -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.call_in_loop(pred):
                return
            time.sleep(0.02)
        raise AssertionError(f"等待超时：{what}")

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        if self.sim is not None:
            self.sim.close()
        if self.sup is not None:
            self.sup.close()
        shutil.rmtree(self.settings.run_dir, ignore_errors=True)


class FakeReplayWorker:
    """replay-worker 替身（M12 §7.4 的 ctl/replay-worker/* 与回放环行为）：open 写 `state.replay`（REPLAY 标志、复合帧：
    Lite32 n 行 + Full64 只含标记机 1 行），gen 写入环 segment 的时机在回复之后；seek 回复带 backfill 包（env、roster、
    state_ext、safety、missions、sensor）与新 gen。"""

    def __init__(self, settings: Any, sim: FakeSim, *, reject_open: int = 0) -> None:
        from awr.runtime.statering import LocalRing as _LR

        self.settings = settings
        self.sim = sim
        self.reject_open = reject_open
        self.ring_cls = _LR
        self.ring = None
        self.gen = 0
        self.t_ns = 0
        self.state = int(TimeState.PAUSED)
        self.ops: list[dict] = []
        self.bus = LocalBus.open("replay-worker", namespace=settings.namespace)
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.h = [self.bus.serve(bus_keys.ctl_replay_worker(op), lambda r, op=op: self.inbox.put((op, r)))
                  for op in ("open", "seek", "play", "pause", "speed", "close")]
        self.h.append(self.bus.serve(bus_keys.ctl_roster("replay"), lambda r: self.inbox.put(("roster", r))))
        self.pending_segment: int | None = None
        self.stop_ev = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_ev.is_set():
            try:
                op, req = self.inbox.get(timeout=0.01)
            except queue.Empty:
                op = None
            if op is not None:
                m = req.msg() or {}
                self.ops.append({"op": op, **m})
                req.reply_msg(self._handle(op, m))
                if self.pending_segment is not None and self.ring is not None:
                    self.ring.set_segment(self.pending_segment)  # 回复发出之后才写 gen（M12 §7.4）
                    self.pending_segment = None
            if self.ring is not None:
                if self.state == int(TimeState.PLAYING):
                    self.t_ns += 8_000_000
                self.ring.heartbeat(self.t_ns, self.state | 0x80, 1000)
                self._frame()

    def _frame(self) -> None:
        vs = self.sim.vehicles
        lite = np.zeros(len(vs), SWARM_LITE32)
        lite["agent_no"] = [v["agent_no"] for v in vs]
        lite["pos"][:, 0] = self.t_ns / 1e9
        full = np.zeros(1, DRONE_STATE64)
        full["agent_no"][0] = vs[-1]["agent_no"]  # 复合帧：只有标记机（最后一架）的 Full64 行
        full["pos"][0] = (self.t_ns / 1e9, 1.0, 2.0)
        self.ring.publish(full, lite, self.t_ns, 7, flags=1)

    def backfill(self) -> dict:
        vs = self.sim.vehicles
        rows = np.zeros((len(vs), 48), np.uint8)
        rows[:, 0] = [v["agent_no"] for v in vs]
        return {"env": msgpack.packb({"version": 1000 + self.gen, "epoch": 9, "t_ns": self.t_ns,
                                      "config": {"presets_sha256": PRESETS_SHA256}}, use_bin_type=True),
                "roster": msgpack.packb({"v": 1, "producer": "replay", "roster_version": 7, "id_base": 0, "id_count": 1024,
                                         "entries": [dict(v, producer="replay") for v in vs]}, use_bin_type=True),
                "clock": None,
                "state_ext": msgpack.packb([[v["agent_no"], {"lifecycle": "READY", "replay_gen": self.gen}] for v in vs],
                                           use_bin_type=True),
                "safety": None, "missions": [msgpack.packb({"mid": "m9", "state": "RUNNING", "t_ns": self.t_ns},
                                                           use_bin_type=True)],
                "sensor": msgpack.packb({"v": 1, "t_sim_ns": self.t_ns, "rows": rows.tobytes()}, use_bin_type=True)}

    def _handle(self, op: str, m: dict) -> dict:
        base = {"v": 1, "cid": m.get("cid"), "code": 0}
        if op == "roster":
            return {"v": 1, "producer": "replay", "roster_version": 7, "id_base": 0, "id_count": 1024,
                    "entries": [dict(v, producer="replay") for v in self.sim.vehicles]}
        if op == "open":
            if self.reject_open:
                return base | {"status": "rejected", "code": self.reject_open}
            self.ring = self.ring_cls.create(self.settings.run_dir / "state.replay", layout_id=LAYOUT_ID, flags=1)
            self.gen = 1
            self.t_ns = 1_000_000_000
            self._frame()
            self.pending_segment = self.gen
            return base | {"status": "accepted", "data_start_ns": 0, "data_end_ns": 60_000_000_000, "speed_max": 20.0,
                           "decimation_s": None, "lineage": [{"epoch": 1, "t_from_ns": 0, "t_to_ns": 60_000_000_000}],
                           "gen": self.gen, "backfill": self.backfill(), "warnings": []}
        if op == "seek":
            self.gen += 1
            self.t_ns = int(m.get("t_ns", 0))
            self._frame()
            self.pending_segment = self.gen
            return base | {"status": "accepted", "t_ns": self.t_ns, "t_sample_ns": self.t_ns, "gen": self.gen,
                           "ring_head": 0, "backfill": self.backfill(), "worker_ms": 1.0}
        if op in ("play", "pause"):
            self.state = int(TimeState.PLAYING if op == "play" else TimeState.PAUSED)
            return base | {"status": "accepted", "state": op}
        if op == "speed":
            sp = float(m.get("speed", 1.0))
            return base | {"status": "accepted", "speed": min(sp, 10.0), "warnings": ["SPEED_CLAMPED"] if sp > 10 else []}
        if op == "close":
            r, self.ring = self.ring, None
            if r is not None:
                r.close()
                self.ring_cls.remove(self.settings.run_dir / "state.replay")
            return base | {"status": "accepted", "state": "closed"}
        return base | {"status": "rejected", "code": 300}

    def close(self) -> None:
        self.stop_ev.set()
        self.thread.join(5)
        for h in self.h:
            h.close()
        self.bus.close()
