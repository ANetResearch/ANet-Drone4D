#!/usr/bin/env python3
"""fake_gw：`awr.rt.v1` 早期数据源（ADR-050；AWR-17 §6、§6.13 第 6 条；AWR-03 D1-AC-35 的 Python 部分；M11-AC-040）。

两种模式：
- 合成（默认）：按 `layouts.json` 生成 N ∈ {1, 200, 1000}（任意 N ≤ 1024）架机的 `fleet/roster`、`swarm/uav/state`（Lite32）、
  `uav/{id}/state`（Full64）、`uav/{id}/state_ext`、`uav/{id}/env`（EnvSample32）、`env/state`（EnvKeyframe，变化 + 1 Hz 心跳）、
  `perf/server`、`sys/procs` 与事件（`event` / `events`），经 60 Hz 对齐网格、发送时装帧、credit 窗口与 TIME 10 Hz 下发。
- 回放：`--replay <file.awrrt>` 按记录时刻原样重放服务端发往客户端的全部帧（字节一致），`--loop` 循环握手之后的部分。

`--record <out.awrrt>` 把第一个连接双向的全部帧写成 `.awrrt`（格式 AWR-16 §13.9，读写库 `awr.contracts.frame`）。

握手（17 §6.2）：子协议必须含 `awr.rt.v1`（否则 HTTP 400 与 `AWR-Supported-Protocols`）；`bearer.<token>` 接受任意值（替身不鉴权）；
依次发送 serverInfo、全量 advertise、TIME；10 s 内未收到 hello 以 4408 关闭；hello 之前的其他 op 回 `error{300}`；
contracts 主版本不同回 `error{311}` 并以 4426 关闭。另提供最小 REST：`POST /api/auth/token`、`GET /api/auth/whoami`、
`GET /api/sys/config`、`GET /api/sys/info`、`GET /api/health/{live,ready}`。

与 PRD 的差异：M11 §2.1 设想 fake_gw 复用网关工作包的协议栈与 `awr/api/rt/sources/synthetic.py`；网关尚未交付，
本工具自带一份最小协议实现（行为按 17 §6.8 发送时装帧与尾帧保证），网关交付后可切换为复用。

用法：
  python tools/fake/fake_gw.py --n 200 [--port 8000] [--host 127.0.0.1] [--cpu 0]
  python tools/fake/fake_gw.py --replay packages/contracts/fixtures/rt/swarm_n200.awrrt [--loop]
  python tools/fake/fake_gw.py --n 1 --record /tmp/session.awrrt --record-max-s 5
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import json
import math
import os
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from awr.contracts import CONTRACTS_VERSION, LAYOUT_ID
from awr.contracts import frame as F
from awr.contracts import layouts as L
from awr.contracts import topics as T
from awr.contracts.commands import match_service
from awr.contracts.enums import FlightState, Owner, PoseSrc, TimeState
from awr.contracts.presets import FIELD_PATHS, PRESETS, PRESETS_SHA256

PROTOCOL = "awr.rt.v1"
TICK_HZ = T.TICK_HZ
TIME_EVERY_TICKS = TICK_HZ // 10  # TIME 10 Hz
CTRL_MAX = 1024
HELLO_TIMEOUT_S = 10.0
MAX_SUBS = 256
EVENT_RING = 65536
EVENTS_PER_MSG = 256
FLAGS_OK = 0x01 | 0x02 | 0x04 | 0x10 | 0x20  # armed、in_air、loc_ok、gcs_link、fcu_link
SWARM_MIN_HZ = 10
WILDCARD_MSGPACK_MAX_HZ = 2
INFO_SCHEMAS = ["awr.DroneState64.v1", "awr.SwarmLite32.v1", "awr.EnvSample32.v1", "awr.VelSetpoint16.v1", "awr.SensorPose48.v1",
                "awr.rt.BatchHeader.v1", "awr.rt.RecordHeader.v1", "awr.rt.Time.v1", "awr.rt.ClientDataHeader.v1"]
ENC = {"raw": F.ENC_RAW, "msgpack": F.ENC_MSGPACK, "json": F.ENC_JSON, "blob": F.ENC_BLOB}


def jdump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def key_matches(pattern: str, key: str) -> bool:
    """`*` 匹配一个 chunk，`**` 匹配零个或多个 chunk（17 §6.7 第 3 条）。"""
    p, k = pattern.split("/"), key.split("/")

    def m(i: int, j: int) -> bool:
        if i == len(p):
            return j == len(k)
        if p[i] == "**":
            return m(i + 1, j) or (j < len(k) and m(i, j + 1))
        return j < len(k) and (p[i] in ("*", k[j])) and m(i + 1, j + 1)

    return m(0, 0)


def _int_if_integral(x: Any) -> Any:
    """EnvKeyframe 约定：整数值用最短整数编码，-0 归一为 0（env_state.schema.json）。"""
    if isinstance(x, float) and math.isfinite(x) and x == int(x):
        return int(x)
    if isinstance(x, dict):
        return {k: _int_if_integral(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_int_if_integral(v) for v in x]
    return x


# ---------------------------------------------------------------- 合成数据源
class Synth:
    """N 架机在同心圆上匀速飞行（半径 50–230 m、高度 30–60 m、速度 8 m/s），全部由 t_sim 决定，可复现。"""

    def __init__(self, n: int, world: str, seed: int = 7) -> None:
        if not 1 <= n <= 1024:
            raise ValueError("n 必须在 1..1024")
        self.n, self.world, self.seed = n, world, seed
        idx = np.arange(n)
        self.ids = [f"uav{a + 1:04d}" for a in range(n)]
        self.r = 50.0 + (idx % 10) * 20.0
        self.z0 = 30.0 + (idx % 7) * 5.0
        self.phase = 2.0 * math.pi * idx / n
        self.omega = 8.0 / self.r
        self.full = np.zeros(n, L.DRONE_STATE64)
        self.lite = np.zeros(n, L.SWARM_LITE32)
        f = self.full
        f["agent_no"] = idx
        f["flight_state"] = L.pack_fs(FlightState.FLYING, 0)
        f["flags"] = FLAGS_OK
        f["mission_item"] = 0xFFFF
        f["ctrl"] = L.pack_ctrl(Owner.MISSION, False, 2, PoseSrc.TRUTH)
        f["dt_us"] = 0
        self.env_rows = np.zeros(n, L.ENV_SAMPLE32)
        self.env_rows["flags"] = 1
        self.env_rows["rho"] = round(1.225 / L.ENV_SAMPLE32_SCALE["rho"])
        self.step(0)

    def step(self, t_sim_ns: int) -> None:
        t = t_sim_ns * 1e-9
        ang = self.phase + self.omega * t
        c, s = np.cos(ang), np.sin(ang)
        f = self.full
        idx = np.arange(self.n)
        f["pos"][:, 0] = self.r * c
        f["pos"][:, 1] = self.r * s
        f["pos"][:, 2] = self.z0 + 2.0 * np.sin(0.2 * t + idx)
        f["vel"][:, 0] = -self.r * self.omega * s
        f["vel"][:, 1] = self.r * self.omega * c
        f["vel"][:, 2] = 0.4 * np.cos(0.2 * t + idx)
        yaw = ang + math.pi / 2.0
        f["q"][:, 0] = 0.0
        f["q"][:, 1] = 0.0
        f["q"][:, 2] = np.sin(yaw / 2.0)
        f["q"][:, 3] = np.cos(yaw / 2.0)
        f["omega"][:, 2] = self.omega
        f["battery_pct"] = np.clip(100 - (idx % 40) - int(t // 60), 0, 100)
        L.lite_from_full(f, out=self.lite)
        w = self.env_rows
        w["wind"][:, 0] = 3.0 + 0.5 * np.sin(0.3 * t + idx)
        w["wind"][:, 1] = 0.5 * np.cos(0.2 * t + idx)

    def roster(self, version: int = 1) -> dict:
        return {"roster_version": version, "entries": [
            {"agent_no": a, "id": self.ids[a], "kind": "uav", "model": "p600", "profile_id": "p600_mid360", "backend": "mock",
             "simulated": True, "producer": "sim-core", "lifecycle": "READY", "sensors": [], "t_world_local": None,
             "caps_ref": "mock"} for a in range(self.n)]}

    def state_ext(self, a: int) -> dict:
        return {"lifecycle": "READY", "lease": {"owner": "MISSION", "holder": None, "priority": 2, "ttl_ms": None},
                "loc": {"status": "TRACKING", "gnss_fix": 3, "sats": 14},
                "battery": {"voltage_v": 23.1, "current_a": 18.0, "soc_pct": float(self.full["battery_pct"][a]),
                            "t_remain_s": 1200.0, "wh_used": 10.0},
                "mission": {"mid": "m-fake", "state": "RUNNING", "item": 1, "total": 4},
                "home_enu_m": [0.0, 0.0, 0.0], "link": {"gcs_age_ms": 100, "fcu_age_ms": 10}, "gcs_loss_policy": "hold_rtl"}

    @staticmethod
    def _vector(preset: str | None) -> list[float]:
        d = PRESETS["defaults"]["scalars"]
        sc = next((p["scalars"] for p in PRESETS["presets"] if p["id"] == preset), {}) if preset else {}
        out = []
        for path in FIELD_PATHS:
            g, k = path.split(".")
            out.append(float((sc.get(g) or {}).get(k, d[g][k])))
        return out

    def keyframe(self, version: int, t_ns: int, frm: str | None, to: str, dur_s: float = 30.0) -> dict:
        cfg = dict(PRESETS["defaults"]["config"])
        cfg["presets_sha256"] = PRESETS_SHA256
        kf = {"schema": "awr.env.keyframe.v1", "world_id": self.world, "version": version, "epoch": 1, "seed": self.seed,
              "t_ns": t_ns, "t_apply_ns": t_ns, "config": cfg, "mode": "smooth" if frm else "step", "t0_ns": t_ns,
              "t1_ns": t_ns + int(dur_s * 1e9) if frm else t_ns, "from": self._vector(frm or to), "to": self._vector(to),
              "via": [], "to_preset": to,
              "anchors": {"t_ns": t_ns, "s_m": 0.0, "d_enu_m": [0.0, 0.0, 0.0], "fall_rain_m": 0.0, "fall_snow_m": 0.0,
                          "wetness": 0.0, "puddle": 0.0},
              "events": [], "vis": {"streamlines": None, "vmax_mps": 20.0}}
        return _int_if_integral(kf)


# ---------------------------------------------------------------- channel
class Channel:
    __slots__ = (
        "_cache",
        "_cache_key",
        "default_rate",
        "enc_code",
        "encoding",
        "entity",
        "id",
        "kind",
        "layout",
        "native_hz",
        "payload",
        "priority",
        "row",
        "schema_name",
        "self_contained",
        "seq",
        "subscribers",
        "t_sim_ns",
        "topic",
    )

    def __init__(self, cid: int, topic: str, row: int | None = None) -> None:
        spec = T.match_topic(topic)
        if spec is None:
            raise ValueError(f"topic 不在 topics.json：{topic}")
        self.id, self.topic, self.row = cid, topic, row
        self.kind, self.encoding, self.schema_name = spec.kind, spec.encoding, spec.schema_name
        self.enc_code = ENC[spec.encoding]
        self.priority = spec.priority if spec.priority is not None else 3
        self.native_hz = spec.native_hz
        self.default_rate = (spec.default_rate or {}).get("S", 1)
        self.self_contained = spec.self_contained
        self.entity = {"kind": "uav", "id": topic.split("/")[1]} if topic.startswith("uav/") else None
        self.seq = 0
        self.t_sim_ns = 0
        self.payload = b""
        self.subscribers = 0
        self._cache_key: tuple | None = None
        self._cache = b""
        self.layout = None
        if self.encoding == "raw" and self.entity is None and self.schema_name in L.SCHEMA_HASH:
            self.layout = {"schemaName": self.schema_name, "hash": L.SCHEMA_HASH[self.schema_name],
                           "size": L.SIZES.get(self.schema_name) if hasattr(L, "SIZES") else None}

    def publish(self, payload: bytes, t_sim_ns: int) -> None:
        self.seq = (self.seq + 1) & 0xFFFFFFFF or 1
        self.payload = payload
        self.t_sim_ns = t_sim_ns

    def record(self, frame_t_ns: int, reset: bool, stats: dict) -> bytes:
        key = (self.seq, reset, frame_t_ns)
        if key != self._cache_key:  # 每个 (channel, seq) 只编码一次，同一 tick 的全部连接共享同一 bytes
            dt = max(-(2**31), min(2**31 - 1, (self.t_sim_ns - frame_t_ns) // 1000))
            rf = (F.RF_KEYFRAME if self.self_contained else 0) | (F.RF_RESET if reset else 0)
            self._cache = F.encode_record(self.id, self.enc_code, rf, self.seq, dt, self.payload)
            self._cache_key = key
            stats["encodes"] += 1
        return self._cache

    def advert(self) -> dict:
        return {"id": self.id, "topic": self.topic, "kind": self.kind, "encoding": self.encoding, "schemaName": self.schema_name,
                "layout": self.layout, "schemaRef": None, "nativeHz": float(min(self.native_hz, TICK_HZ)),
                "defaultRate": int(self.default_rate), "priority": int(self.priority), "selfContained": bool(self.self_contained),
                "entity": self.entity}


class SubChan:
    __slots__ = ("ch", "due", "last_seq", "pending", "period_ticks", "rate", "refs", "reset_pending", "snapshot")

    def __init__(self, ch: Channel) -> None:
        self.ch = ch
        self.rate = 0
        self.period_ticks = TICK_HZ
        self.last_seq = 0
        self.due = False
        self.pending = False
        self.snapshot = True
        self.reset_pending = False
        self.refs: set[int] = set()


# ---------------------------------------------------------------- 会话
class Session:
    def __init__(self, gw: FakeGateway, ws, conn_id: str) -> None:
        self.gw, self.ws, self.conn_id = gw, ws, conn_id
        self.ctrl: collections.deque = collections.deque()
        self.wake = asyncio.Event()
        self.closed = False
        self.hello = False
        self.subs: dict[int, dict] = {}
        self.subchans: dict[int, SubChan] = {}
        self.frame_seq = 0
        self.acked = 0
        self.window = gw.window
        self.has_due = False
        self.events_on = False
        self.events_filter: dict = {}
        self.pending_events: list[dict] = []
        self.stats = {"frames": 0, "bytes": 0, "credit_skips": 0, "ctrl_hwm": 0, "client_data": 0}
        self.capture: Capture | None = None

    # 控制面
    def send_ctrl(self, msg: dict | bytes) -> None:
        if self.closed:
            return
        if len(self.ctrl) >= CTRL_MAX:
            self.closed = True
            self._closing = asyncio.ensure_future(self._close_backlog())
            return
        self.ctrl.append(msg if isinstance(msg, bytes) else jdump(msg))
        self.stats["ctrl_hwm"] = max(self.stats["ctrl_hwm"], len(self.ctrl))
        self.wake.set()

    async def _close_backlog(self) -> None:
        with contextlib.suppress(Exception):
            await self._send(jdump({"op": "status", "id": "net.backlog", "level": "error", "message": "控制面积压，连接关闭",
                                    "code": 318}))
            await self.ws.close(code=1013)

    async def _send(self, m: str | bytes) -> None:
        if self.capture is not None:
            self.capture.add(F.AWRT_S2C, m)
        if isinstance(m, bytes):
            await self.ws.send_bytes(m)
        else:
            await self.ws.send_text(m)

    def error(self, code: int, name: str, message: str, op: str, rid: Any = None) -> None:
        ref: dict = {"op": op}
        if rid is not None:
            ref["id"] = rid
        self.send_ctrl({"op": "error", "code": code, "name": name, "message": message, "ref": ref})

    # 数据面：对齐网格到期位（17 §6.8）
    def mark_due(self, k: int) -> None:
        any_due = False
        for sc in self.subchans.values():
            ch = sc.ch
            if ch.seq == 0:
                continue
            if sc.snapshot or ((k % sc.period_ticks == 0 or sc.pending) and ch.seq != sc.last_seq):
                sc.due = True
            if sc.due:
                any_due = True
        if any_due:
            self.has_due = True
            self.wake.set()

    async def sender(self) -> None:
        try:
            while not self.closed:
                while self.ctrl:
                    await self._send(self.ctrl.popleft())
                if self.has_due:
                    if self.frame_seq - self.acked >= self.window:
                        self.stats["credit_skips"] += 1  # 窗口已满：不发数据，到期位保留（尾帧保证）
                    else:
                        frame = self._assemble()
                        if frame is not None:
                            await self._send(frame)
                            self.stats["frames"] += 1
                            self.stats["bytes"] += len(frame)
                        continue
                self.wake.clear()
                if self.ctrl:
                    continue
                await self.wake.wait()
        except Exception:
            self.closed = True

    def _assemble(self) -> bytes | None:
        """发送时装帧：按最新值装配到期的订阅，写出后才提交 last_seq。"""
        due = [sc for sc in self.subchans.values() if sc.due and sc.ch.seq != 0]
        self.has_due = False
        if not due:
            return None
        due.sort(key=lambda sc: (sc.ch.kind != "roster", sc.ch.priority, sc.ch.id))
        t = self.gw.t_sim_ns
        snapshot = any(sc.snapshot for sc in due)
        recs = [sc.ch.record(t, sc.reset_pending, self.gw.stats) for sc in due]
        self.frame_seq += 1
        flags = F.BATCH_SNAPSHOT if snapshot else 0
        frame = F.encode_batch(flags, self.gw.epoch, self.frame_seq, t, recs)
        for sc in due:
            sc.last_seq = sc.ch.seq
            sc.due = sc.pending = sc.snapshot = sc.reset_pending = False
        return frame

    def flush_events(self) -> None:
        if not self.pending_events:
            return
        evs, self.pending_events = self.pending_events, []
        if len(evs) == 1:
            self.send_ctrl({"op": "event", **evs[0]})
            return
        for i in range(0, len(evs), EVENTS_PER_MSG):
            chunk = evs[i:i + EVENTS_PER_MSG]
            if len(chunk) == 1:
                self.send_ctrl({"op": "event", **chunk[0]})
            else:
                self.send_ctrl({"op": "events", "items": chunk})

    def event_ok(self, ev: dict) -> bool:
        f = self.events_filter
        if not f:
            return True
        types = f.get("types")
        if types and not any(ev["type"].startswith(t) for t in types):
            return False
        return ev["level"] >= int(f.get("levelMin", 0))

    # 订阅
    def subscribe(self, subs: list[dict]) -> None:
        for s in subs:
            sid, topic = s.get("id"), s.get("topic", "")
            if not isinstance(sid, int) or not isinstance(topic, str):
                self.error(300, "BAD_REQUEST", "subscribe 项缺少 id 或 topic", "subscribe", sid)
                continue
            if topic == "swarm/state":
                topic = "swarm/uav/state"
            if not T.valid_topic(topic, allow_wildcards=True):
                self.error(314, "TOPIC_INVALID", f"topic 语法非法：{topic}", "subscribe", sid)
                continue
            if sid not in self.subs and len(self.subs) >= MAX_SUBS:
                self.error(315, "SUB_LIMIT", "订阅数超过上限 256", "subscribe", sid)
                continue
            mode = s.get("mode", "latest")
            if topic == "event":
                self.events_on = True
                self.events_filter = s.get("filter") or {}
                self.subs[sid] = {"topic": topic, "rate": 0, "mode": "all", "channels": [self.gw.event_ch.id]}
                self.send_ctrl({"op": "subscribed", "id": sid, "topic": topic, "channels": [self.gw.event_ch.id], "rate": 0,
                                "mode": "all"})
                continue
            chans = self.gw.match(topic)
            rate = T.quantize_rate(float(s.get("rate", 0) or 0))
            if topic.startswith("swarm/"):
                rate = max(rate, SWARM_MIN_HZ)
            wildcard = "*" in topic
            if wildcard and any(c.encoding == "msgpack" and c.topic.startswith("uav/") for c in chans):
                rate = min(rate, WILDCARD_MSGPACK_MAX_HZ)  # 17 §6.7 第 3 条：uav/*/… 的 msgpack 通配上限 2 Hz
            update = sid in self.subs
            if update:
                self._unref(sid)
            self.subs[sid] = {"topic": topic, "rate": rate, "mode": mode, "channels": [c.id for c in chans]}
            for c in chans:
                sc = self.subchans.get(c.id)
                if sc is None:
                    sc = self.subchans[c.id] = SubChan(c)
                    c.subscribers += 1
                elif not update:
                    sc.snapshot = True
                sc.refs.add(sid)
                sc.rate = max(self.subs[r]["rate"] for r in sc.refs)
                sc.period_ticks = max(1, TICK_HZ // max(1, sc.rate))
            self.send_ctrl({"op": "subscribed", "id": sid, "topic": topic, "channels": [c.id for c in chans], "rate": rate,
                            "mode": mode})

    def _unref(self, sid: int) -> None:
        sub = self.subs.get(sid)
        if not sub:
            return
        for cid in sub["channels"]:
            sc = self.subchans.get(cid)
            if sc is None:
                continue
            sc.refs.discard(sid)
            if not sc.refs:
                del self.subchans[cid]
                sc.ch.subscribers -= 1

    def unsubscribe(self, ids: list[int]) -> None:
        for sid in ids:
            sub = self.subs.get(sid)
            if sub is None:
                continue
            if sub["topic"] == "event":
                self.events_on = False
            self._unref(sid)
            self.subs.pop(sid, None)

    def release(self) -> None:
        for sid in list(self.subs):
            self._unref(sid)
        self.subs.clear()


# ---------------------------------------------------------------- .awrrt 采集
class Capture:
    def __init__(self, path: Path, header: dict, max_s: float | None) -> None:
        self.path, self.header, self.max_s = path, header, max_s
        self.t0 = time.monotonic_ns()
        self.t0_wall = time.time_ns()
        self.recs: list[F.AwrtRecord] = []
        self.done = False

    def add(self, direction: int, m: str | bytes) -> None:
        if self.done:
            return
        t = time.monotonic_ns() - self.t0
        if self.max_s is not None and t > self.max_s * 1e9:
            self.finish()
            return
        kind = F.AWRT_BINARY if isinstance(m, bytes) else F.AWRT_TEXT
        self.recs.append(F.AwrtRecord(direction, kind, t, m if isinstance(m, bytes) else m.encode("utf-8")))

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        data = F.write_awrrt(self.header, self.recs, F.AWRT_TIMED, self.t0_wall)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.path)
        sys.stderr.write(f"fake_gw: 已写入 {self.path}（{len(self.recs)} 条记录，{len(data)} B）\n")


# ---------------------------------------------------------------- 网关替身
class FakeGateway:
    def __init__(self, *, n: int = 200, world: str = "shenzhen", seed: int = 7, rate: float = 1.0, window: int = 6,
                 env_period_s: float = 20.0, event_period_s: float = 2.0, detail_channels: bool = True,
                 replay: Path | None = None, loop_replay: bool = False, record: Path | None = None,
                 record_max_s: float | None = None, hello_timeout_s: float = HELLO_TIMEOUT_S,
                 calls: str = "reject") -> None:
        self.n, self.world, self.rate, self.window = n, world, rate, window
        self.calls = calls
        self.env_period_s, self.event_period_s = env_period_s, event_period_s
        self.hello_timeout_s = hello_timeout_s
        self.session_id = "fake-" + secrets.token_hex(6)
        self.gw_t0 = time.monotonic_ns()
        self.epoch = 1
        self.state = TimeState.PLAYING
        self.t_sim_ns = 0
        self.k = 0
        self.sessions: set[Session] = set()
        self.stats = {"encodes": 0, "ticks": 0, "tick_overruns": 0}
        self.events: collections.deque[dict] = collections.deque(maxlen=EVENT_RING)
        self.gseq = 0
        self.replay = F.read_awrrt(Path(replay).read_bytes()) if replay else None
        self.loop_replay = loop_replay
        self.record = Path(record) if record else None
        self.record_max_s = record_max_s
        self._recorded = False
        self.synth = Synth(n, world, seed) if self.replay is None else None
        self.channels: dict[int, Channel] = {}
        self.by_topic: dict[str, Channel] = {}
        self._build_channels(detail_channels)
        self.env_version = 0
        self.env_kf: dict | None = None
        self._env_presets = ["clear", "partlyCloudy", "overcast", "rain", "overcast"]
        self._tick_task: asyncio.Task | None = None

    # 通道表
    def _add(self, cid: int, topic: str, row: int | None = None) -> Channel:
        c = Channel(cid, topic, row)
        self.channels[cid] = c
        self.by_topic[topic] = c
        return c

    def _build_channels(self, detail: bool) -> None:
        self.roster_ch = self._add(1, "fleet/roster")
        self.swarm_ch = self._add(2, "swarm/uav/state")
        self.env_ch = self._add(3, "env/state")
        self.perf_ch = self._add(4, "perf/server")
        self.procs_ch = self._add(5, "sys/procs")
        self.event_ch = self._add(6, "event")
        self.uav_state: list[Channel] = []
        self.uav_ext: list[Channel] = []
        self.uav_env: list[Channel] = []
        if self.synth is None:
            return
        n = self.n
        for a, uid in enumerate(self.synth.ids):
            self.uav_state.append(self._add(100 + a, f"uav/{uid}/state", a))
            if detail:
                self.uav_ext.append(self._add(100 + n + a, f"uav/{uid}/state_ext", a))
                self.uav_env.append(self._add(100 + 2 * n + a, f"uav/{uid}/env", a))

    def match(self, pattern: str) -> list[Channel]:
        if "*" not in pattern:
            c = self.by_topic.get(pattern)
            return [c] if c is not None and c.kind != "event" else []
        return [c for c in self.channels.values() if c.kind != "event" and key_matches(pattern, c.topic)]

    # 握手消息
    def server_info(self, conn_id: str) -> dict:
        return {"op": "serverInfo", "name": "awr-gateway", "protocol": PROTOCOL, "sessionId": self.session_id, "connId": conn_id,
                "capabilities": ["time", "credit", "rpc", "events"], "window": self.window, "tickHz": TICK_HZ,
                "rateClasses": list(T.RATE_CLASSES), "world": {"id": self.world, "frame": "world"},
                "run": {"id": "fake-run", "segment": 0}, "mode": "live",
                "clock": {"mode": "lockstep", "pausable": True, "max_speed": 20, "steppable": True}, "role": "operator",
                "principal": "p-fake", "seat": "held", "contracts": CONTRACTS_VERSION,
                "layouts": {k: L.SCHEMA_HASH[k] for k in INFO_SCHEMAS},
                "limits": {"maxSubs": MAX_SUBS, "maxHighRateFull": 64, "ctrlQueue": CTRL_MAX, "maxTextBytes": 262144,
                           "maxBinaryBytes": 4096},
                "serverUnix_ns": str(time.time_ns())}

    def time_frame(self) -> bytes:
        return F.encode_time(int(self.state), self.epoch, float(self.rate), self.t_sim_ns, time.monotonic_ns() - self.gw_t0)

    # 事件
    def emit(self, typ: str, level: int, data: dict, *, uav: str | None = None, cid: str | None = None) -> None:
        self.gseq += 1
        ev = {"seq": self.gseq, "t_sim_ns": self.t_sim_ns, "t_wall_ns": str(time.time_ns()), "type": typ, "level": level,
              "producer": "sim-core", "uav": uav, "cid": cid, "data": data}
        self.events.append(ev)
        for s in self.sessions:
            if s.hello and s.events_on and s.event_ok(ev):
                s.pending_events.append(ev)

    # tick
    async def run_ticks(self) -> None:
        period = 1.0 / TICK_HZ
        t_start = time.perf_counter()
        nxt = t_start
        while True:
            nxt += period
            delay = nxt - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -period:
                self.stats["tick_overruns"] += 1
                nxt = time.perf_counter()
            self.on_tick(int((time.perf_counter() - t_start) * self.rate * 1e9))

    def on_tick(self, t_sim_ns: int) -> None:
        self.k += 1
        k = self.k
        self.stats["ticks"] += 1
        self.t_sim_ns = t_sim_ns
        syn = self.synth
        if syn is None:
            return
        syn.step(t_sim_ns)
        if k == 1:
            self.roster_ch.publish(msgpack.packb(syn.roster()), t_sim_ns)
        self.swarm_ch.publish(syn.lite.tobytes(), t_sim_ns)
        fb = None
        for c in self.uav_state:
            if c.subscribers:
                if fb is None:
                    fb = syn.full.tobytes()
                c.publish(fb[64 * c.row:64 * (c.row + 1)], t_sim_ns)  # 懒生产：只为有订阅者的机切片
        if k % 30 == 1:
            for c in self.uav_ext:
                if c.subscribers:
                    c.publish(msgpack.packb(syn.state_ext(c.row)), t_sim_ns)
        if k % 6 == 1:
            eb = None
            for c in self.uav_env:
                if c.subscribers:
                    if eb is None:
                        eb = syn.env_rows.tobytes()
                    c.publish(eb[32 * c.row:32 * (c.row + 1)], t_sim_ns)
        env_due = self.env_kf is None or (self.env_period_s > 0 and t_sim_ns >= self.env_kf["t_ns"] + self.env_period_s * 1e9)
        if env_due:
            self.env_version += 1
            frm = self.env_kf["to_preset"] if self.env_kf else None
            to = self._env_presets[(self.env_version - 1) % len(self._env_presets)]
            self.env_kf = syn.keyframe(self.env_version, t_sim_ns, frm, to)
            self.env_ch.publish(msgpack.packb(self.env_kf), t_sim_ns)
            if self.env_version > 1:
                self.emit("env.changed", 1, {"version": self.env_version, "by": "fake", "reason": "preset"})
        elif k % TICK_HZ == 0:
            self.env_ch.publish(msgpack.packb(self.env_kf), t_sim_ns)  # 1 Hz 心跳：同 version 自包含最新值
        if k % TICK_HZ == 0:
            self.perf_ch.publish(msgpack.packb(self.perf_payload()), t_sim_ns)
            self.procs_ch.publish(msgpack.packb({"items": [
                {"name": "sim-core", "pid": None, "state": "RUNNING", "restarts": 0},
                {"name": "api", "pid": os.getpid(), "state": "RUNNING", "restarts": 0}]}), t_sim_ns)
        if self.event_period_s > 0 and k % max(1, int(self.event_period_s * TICK_HZ)) == 0:
            a = (k // TICK_HZ) % syn.n
            self.emit("mission.item_reached", 0, {"mid": "m-fake", "item": int(k // TICK_HZ) % 4}, uav=syn.ids[a])
        for s in list(self.sessions):
            if not s.hello or s.closed:
                continue
            s.flush_events()
            if k % TIME_EVERY_TICKS == 0:
                s.send_ctrl(self.time_frame())
            s.mark_due(k)

    def perf_payload(self) -> dict:
        return {"api": {"cpu_pct": 0.0, "tick_overruns": self.stats["tick_overruns"], "encodes_per_s": 0.0,
                        "n_clients": len(self.sessions)},
                "clients": [{"conn_id": s.conn_id, "window": s.window, "credit_skips": s.stats["credit_skips"],
                             "ctrl_queue_hwm": s.stats["ctrl_hwm"]} for s in self.sessions],
                "sim": {"rtf": float(self.rate), "n_active": self.n, "kernel": "numpy"}}

    # RPC 替身
    def handle_call(self, s: Session, m: dict) -> None:
        cid, service = str(m.get("id", "")), str(m.get("service", ""))
        if not cid:
            s.error(300, "BAD_REQUEST", "call 缺少 id", "call")
            return
        if match_service(service) is None:
            s.send_ctrl({"op": "result", "id": cid, "status": "rejected", "code": 110, "reason": "PARAM_OUT_OF_RANGE",
                         "message": f"未知服务 {service}", "final": True})
            return
        uav = service.split("/")[1] if service.startswith("uav/") else None
        if self.calls == "reject":
            # 合成数据源不承载命令（M16 §4 M-G 行；18 §19 F-08）：211 SIM_UNAVAILABLE，界面据此提示"合成数据"。
            # INT-1（M16-to-M11 第 7 条）；`--calls ack` 保留旧的"确认 + 0.5 s 后 succeeded"口径（协议冒烟用例）
            s.send_ctrl({"op": "result", "id": cid, "status": "rejected", "code": 211, "reason": "SIM_UNAVAILABLE",
                         "message": "合成数据源不执行命令", "final": True})
            return
        s.send_ctrl({"op": "result", "id": cid, "status": "accepted", "code": 0, "final": False})
        self.emit("cmd.accepted", 0, {"op": service.rsplit("/", 1)[-1]}, uav=uav, cid=cid)

        def done() -> None:
            if not s.closed:
                s.send_ctrl({"op": "result", "id": cid, "status": "succeeded", "code": 0, "final": True})
            self.emit("cmd.succeeded", 0, {"op": service.rsplit("/", 1)[-1]}, uav=uav, cid=cid)

        asyncio.get_running_loop().call_later(0.5, done)

    # 连接
    async def serve_ws(self, ws) -> None:
        protos = list(ws.scope.get("subprotocols") or [])
        if PROTOCOL not in protos:
            try:
                from starlette.responses import JSONResponse

                await ws.send_denial_response(JSONResponse({"code": 311, "name": "PROTOCOL_UNSUPPORTED"}, status_code=400,
                                                           headers={"AWR-Supported-Protocols": PROTOCOL}))
            except Exception:
                await ws.close(code=1002)
            return
        await ws.accept(subprotocol=PROTOCOL)
        conn_id = "c-" + secrets.token_hex(6)
        s = Session(self, ws, conn_id)
        if self.record is not None and not self._recorded:
            self._recorded = True
            s.capture = Capture(self.record, {"protocol": PROTOCOL, "layout_id": f"0x{LAYOUT_ID:08X}",
                                              "contracts_version": CONTRACTS_VERSION, "created_by": "tools/fake/fake_gw.py",
                                              "n_uav": self.n, "world_id": self.world,
                                              "description": "fake_gw capture (both directions)"}, self.record_max_s)
        if self.replay is not None:
            await self._serve_replay(s)
            return
        self.sessions.add(s)
        sender = asyncio.ensure_future(s.sender())
        s.send_ctrl(self.server_info(conn_id))
        s.send_ctrl({"op": "advertise", "channels": [c.advert() for c in self.channels.values()]})
        s.send_ctrl(self.time_frame())
        t_hello = time.monotonic() + self.hello_timeout_s
        try:
            while not s.closed:
                timeout = None if s.hello else max(0.0, t_hello - time.monotonic())
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout)
                except TimeoutError:
                    s.error(317, "HELLO_TIMEOUT", "10 s 内未收到 hello", "hello")
                    await asyncio.sleep(0.05)
                    await ws.close(code=4408)
                    break
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    if s.capture is not None:
                        s.capture.add(F.AWRT_C2S, msg["bytes"])
                    s.stats["client_data"] += 1  # CLIENT_DATA：替身只计数
                    continue
                text = msg.get("text") or ""
                if s.capture is not None:
                    s.capture.add(F.AWRT_C2S, text)
                await self._on_text(s, text)
        except Exception:
            pass
        finally:
            s.closed = True
            s.wake.set()
            self.sessions.discard(s)
            s.release()
            sender.cancel()
            if s.capture is not None:
                s.capture.finish()

    async def _on_text(self, s: Session, text: str) -> None:
        try:
            m = json.loads(text)
            op = m["op"]
        except (ValueError, KeyError, TypeError):
            s.error(300, "BAD_REQUEST", "控制消息必须是含 op 的 JSON", "unknown")
            return
        if not s.hello:
            if op != "hello":
                s.error(300, "BAD_REQUEST", "hello 之前只接受 hello", op)
                return
            major = str(m.get("contracts", "")).split(".")[0]
            if major != CONTRACTS_VERSION.split(".")[0]:
                s.error(311, "PROTOCOL_UNSUPPORTED", f"contracts 主版本不同：{m.get('contracts')}", "hello")
                await asyncio.sleep(0.05)
                await s.ws.close(code=4426)
                s.closed = True
                return
            s.hello = True
            res = m.get("resume") or {}
            if res.get("sessionId") == self.session_id:
                since = int(res.get("lastEventSeq", 0))
                s.pending_events = [e for e in self.events if e["seq"] > since]
                s.events_on = True
            return
        if op == "subscribe":
            s.subscribe(m.get("subs") or [])
        elif op == "unsubscribe":
            s.unsubscribe(m.get("ids") or [])
        elif op == "ack":
            s.acked = max(s.acked, int(m.get("frame", 0)))
            s.wake.set()
        elif op == "ping":
            s.send_ctrl({"op": "pong", "t": m.get("t", 0), "server_ns": time.monotonic_ns() - self.gw_t0,
                         "sim_ns": self.t_sim_ns, "epoch": self.epoch, "unix_ns": str(time.time_ns())})
        elif op == "call":
            self.handle_call(s, m)
        elif op == "cancel":
            s.send_ctrl({"op": "result", "id": str(m.get("id", "")), "status": "canceled", "code": 0, "final": True})
        elif op in ("clientStats", "advertise", "unadvertise", "hello"):
            pass
        else:
            s.error(313, "UNKNOWN_OP", f"不认识的 op：{op}", str(op))

    async def _serve_replay(self, s: Session) -> None:
        assert self.replay is not None
        recs = [r for r in self.replay.records if r.dir == F.AWRT_S2C]
        first_bin = next((i for i, r in enumerate(recs) if r.kind == F.AWRT_BINARY), 0)

        async def drain_incoming() -> None:
            while True:
                msg = await s.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if s.capture is not None:
                    s.capture.add(F.AWRT_C2S, msg.get("bytes") if msg.get("bytes") is not None else (msg.get("text") or ""))

        rx = asyncio.ensure_future(drain_incoming())
        try:
            start = 0
            while True:
                t0 = time.monotonic_ns()
                base = recs[start].t_rel_ns if recs else 0
                for r in recs[start:]:
                    wait = (r.t_rel_ns - base) - (time.monotonic_ns() - t0)
                    if wait > 0:
                        await asyncio.sleep(wait / 1e9)
                    if rx.done():
                        return
                    await s._send(r.payload if r.kind == F.AWRT_BINARY else r.payload.decode("utf-8"))
                if not self.loop_replay:
                    break
                start = first_bin
            await rx
        except Exception:
            pass
        finally:
            rx.cancel()
            if s.capture is not None:
                s.capture.finish()


# ---------------------------------------------------------------- HTTP 应用
def build_app(gw: FakeGateway):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    async def token(req: Request) -> JSONResponse:
        try:
            body = await req.json()
        except Exception:
            body = {}
        role = body.get("role", "operator") if isinstance(body, dict) else "operator"
        return JSONResponse({"token": "v1.fake." + secrets.token_hex(8), "role": role, "principal_id": "p-fake",
                             "expires_at": int(time.time()) + 43200, "seat": "held" if role != "viewer" else "none"})

    async def whoami(req: Request) -> JSONResponse:
        return JSONResponse({"principal_id": "p-fake", "role": "operator", "seat": "held"})

    async def sys_config(req: Request) -> JSONResponse:
        return JSONResponse({"worlds_base": "/worlds", "static_split": False, "access_mode": "loopback", "tick_hz": TICK_HZ,
                             "rate_classes": list(T.RATE_CLASSES)}, headers={"Cache-Control": "no-store"})

    async def sys_info(req: Request) -> JSONResponse:
        return JSONResponse({"name": "awr", "version": "0.1.0-fake", "protocol": PROTOCOL, "api_version": 1,
                             "contracts": CONTRACTS_VERSION, "access_mode": "loopback", "layout_id": f"{LAYOUT_ID:08x}"})

    async def live(req: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def ready(req: Request) -> JSONResponse:
        return JSONResponse({"status": "ready", "sim": "ok", "ring_attached": True, "world_loaded": True, "run_id": "fake-run"})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if gw.replay is None:
            gw._tick_task = asyncio.ensure_future(gw.run_ticks())
        try:
            yield
        finally:
            if gw._tick_task is not None:
                gw._tick_task.cancel()

    return Starlette(routes=[
        Route("/api/auth/token", token, methods=["POST"]), Route("/api/auth/whoami", whoami),
        Route("/api/sys/config", sys_config), Route("/api/sys/info", sys_info), Route("/api/health/live", live),
        Route("/api/health/ready", ready), WebSocketRoute("/api/rt", gw.serve_ws)],
        lifespan=lifespan)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fake_gw", description="awr.rt.v1 早期数据源（合成或回放 .awrrt）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000 + 10 * int(os.environ.get("AWR_PORT_OFFSET", "0") or 0))
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--world", default="shenzhen")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rate", type=float, default=1.0)
    ap.add_argument("--window", type=int, default=6)
    ap.add_argument("--env-period-s", type=float, default=20.0)
    ap.add_argument("--event-period-s", type=float, default=2.0)
    ap.add_argument("--no-detail", action="store_true", help="不广播逐机 state_ext 与 env 通道")
    ap.add_argument("--replay", type=Path, default=None)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--record", type=Path, default=None)
    ap.add_argument("--record-max-s", type=float, default=None)
    ap.add_argument("--cpu", type=int, default=None, help="钉核（18 PR-6：fake_gw 钉 core0）")
    ap.add_argument("--hello-timeout-s", type=float, default=HELLO_TIMEOUT_S, help="hello 超时（默认 10 s，测试可缩短）")
    ap.add_argument("--calls", choices=("reject", "ack"), default="reject",
                    help="命令口径：reject（缺省，211 SIM_UNAVAILABLE，M16 §4）| ack（确认后 0.5 s succeeded，协议冒烟）")
    a = ap.parse_args(argv)
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write("错误（退出码 2）：fake_gw 只监听回环地址\n修复：--host 127.0.0.1\n")
        return 2
    if a.cpu is not None and hasattr(os, "sched_setaffinity"):
        with contextlib.suppress(OSError):
            os.sched_setaffinity(0, {a.cpu})
    gw = FakeGateway(n=a.n, world=a.world, seed=a.seed, rate=a.rate, window=a.window, env_period_s=a.env_period_s,
                     event_period_s=a.event_period_s, detail_channels=not a.no_detail, replay=a.replay, loop_replay=a.loop,
                     record=a.record, record_max_s=a.record_max_s, hello_timeout_s=a.hello_timeout_s, calls=a.calls)
    import warnings

    import uvicorn

    warnings.filterwarnings("ignore", message=".*websockets.*implementation is deprecated.*")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((a.host, a.port))
    except OSError as e:
        sys.stderr.write(f"错误（退出码 3）：端口 {a.port} 不可用：{e}\n修复：--port <空闲端口> 或设置 AWR_PORT_OFFSET\n")
        return 3
    port = sock.getsockname()[1]
    sock.listen(128)  # READY 打印前即进入监听：早到的连接在 backlog 中排队，不被拒绝
    cfg = uvicorn.Config(build_app(gw), loop="uvloop", ws="websockets", ws_per_message_deflate=False, ws_max_size=262144,
                         log_level="warning", lifespan="on")
    server = uvicorn.Server(cfg)
    mode = f"replay {a.replay}" if a.replay else f"synthetic n={a.n}"
    print(f"fake_gw READY ws://{a.host}:{port}/api/rt ({mode})", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(server.serve(sockets=[sock]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
