"""ANet Drone4D 沙盒 Python SDK（单文件；M17-FR-056、FR-057；AWR-17 §17.13.1；M17 PRD §7.8）。

依赖：httpx >= 0.24、websockets >= 12（同步客户端）、msgpack >= 1.0；Python 3.9 至 3.13；不依赖 awr 包。

    from awr_sandbox import Sandbox
    sb = Sandbox("https://drone4d.agentnetwork.org.cn")
    with sb.create("synthcity", "t5_area_guard") as s:      # 满员时自动排队、长轮询并认领；需要时自动求解工作量证明
        print(s.state()["state"])
        s.clock("speed", rate=5)                             # 受限时返回授予值（警告 501），不抛异常
        for msg in s.stream(["sandbox/session"], seconds=3):
            print(msg.topic, msg.data)

SDK 只是 REST v1 与 `awr.rt.v1` WebSocket 的薄封装：不提供界面没有的能力，也不绕过任何准入（UI 与 API 同权）。
- 错误：problem+json 与 `result{rejected, failed}` 抛 `AwrError(code, name, message, detail, remedy, retry_after_s)`；
  `111`、`504`（速率类）、`586` 与 429/503 按 `Retry-After` 自动退避（最多 3 次）；`509` 自动求解；`211`（SPAWNING）10 s 内自动
  等待；会话结束（`506`、`510`、WS 4410）抛 `SandboxGone`（附最近一次取得的快照）。
- 排队：202 时长轮询 `GET /queue/{id}?wait_s=25`，`slot_ready` 时调用 `on_queue("sandbox.slot_ready", view)` 并认领。
- 续期：`auto_keepalive`（缺省开）由后台线程在剩余寿命 <= 300 s 时调用 `POST /keepalive` 并换发 token；队列非空时服务端拒绝续期
  （`renewed = false`），SDK 停止尝试直到下一次寿命检查。续期不是"活动"：无实时连接且 `idle_ttl_s`（120 s）内无会话内调用的会话
  会被服务端驻留（ADR-121），需要持续推进时保持 `stream()` 或定期读取（见 README）。
- 实时：`stream(topics)` 订阅并按 `awr.rt.v1` 解码（raw 记录按内嵌布局表解码为 dict，msgpack 原样解包），自动 ack、按 TIME
  跟踪仿真时钟；`velocity()` 首次调用时 `call uav/{id}/cmd/velocity` 并 `advertise` 客户端通道，之后发送 CLIENT_DATA
  VelSetpoint16（<= 50 Hz，过密的样本在客户端合并）。
- 节能驻留（1.1.0，AWR-17 §17.20.4）：`Session.park()`、`Session.resume(wait=True)`；会话端点返回 `515 SANDBOX_PARKED`
  或实时连接以 4430 关闭时自动恢复一次（满员时按恢复票排队并认领），然后重试原请求；恢复会换发 token。
- 容量与内部身份（1.1.0）：`Sandbox.capacity()`（公开容量接口 `awr.capacity.v1`）；`create(identity_token=...)` 以
  `X-AWR-Identity` 创建内部档会话（身份令牌由部署方的身份入口签发，SDK 不处理口令）。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import queue
import random
import secrets
import struct
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import msgpack

__version__ = "1.1.0"
API_VERSION = "v1"
PREFIX = "/api/sandbox/v1"
SUBPROTOCOL = "awr.rt.v1"
CONTRACTS_MAJOR = 1

__all__ = ["API_VERSION", "AwrError", "Msg", "Sandbox", "SandboxGone", "Session", "__version__", "solve_pow"]

# ---------------------------------------------------------------- 线上布局（rt/layouts.json 的子集，契约 1.1.0）
OP_TIME, OP_BATCH, OP_CLIENT_DATA = 0x02, 0x10, 0x20
ENC_RAW, ENC_MSGPACK, ENC_JSON, ENC_BLOB = 0, 1, 2, 3
RF_ZSTD = 0x04
CD_FINAL = 0x01
_BATCH_HDR = struct.Struct("<BBHIq")
_REC_HDR = struct.Struct("<HBBIIi")
_TIME = struct.Struct("<BBHfqq")
_CD_HDR = struct.Struct("<BBHIq")
_VEL16 = struct.Struct("<4f")
TIME_STATES = ("STOPPED", "PLAYING", "PAUSED", "STEPPING", "BUFFERING", "ENDED", "STALLED", "RESTARTING", "FAILED", "LIVE")

# schemaName -> (行大小, [(字段, struct 格式, 偏移, 比例或 None, 新名或 None)])
LAYOUTS: dict[str, Any] = {
    "awr.DroneState64.v1": (64, [("agent_no", "<H", 0, None, None), ("flight_state", "B", 2, None, None),
                                 ("flags", "B", 3, None, None), ("mission_item", "<H", 4, None, None),
                                 ("battery_pct", "B", 6, None, None), ("ctrl", "B", 7, None, None),
                                 ("pos", "<3f", 8, None, None), ("vel", "<3f", 20, None, None), ("q", "<4f", 32, None, None),
                                 ("omega", "<3f", 48, None, None), ("dt_us", "<i", 60, None, None)]),
    "awr.SwarmLite32.v1": (32, [("agent_no", "<H", 0, None, None), ("flight_state", "B", 2, None, None),
                                ("battery_pct", "B", 3, None, None), ("pos", "<3f", 4, None, None),
                                ("q_snorm", "<4h", 16, 1.0 / 32767.0, "q"), ("vel_cms", "<3h", 24, 0.01, "vel"),
                                ("flags", "B", 30, None, None), ("ctrl", "B", 31, None, None)]),
    "awr.TargetLite32.v1": (32, [("agent_no", "<H", 0, None, None), ("class", "B", 2, None, None), ("state", "B", 3, None, None),
                                 ("pos", "<3f", 4, None, None), ("heading_rad", "<f", 16, None, None),
                                 ("speed_cms", "<h", 20, 0.01, "speed_mps"), ("best", "B", 22, None, None),
                                 ("alertness_u8", "B", 23, 1.0 / 255.0, "alertness"), ("discovered_n", "<H", 24, None, None),
                                 ("n_vol", "B", 26, None, None), ("flags", "B", 27, None, None),
                                 ("keepout_r_m", "<f", 28, None, None)]),
    "awr.EnvSample32.v1": (32, [("wind", "<3f", 0, None, None), ("wind_mean", "<3h", 12, 0.01, None),
                                ("turb_sigma_uw", "<2B", 18, 0.05, None), ("sigma_ext", "<f", 20, None, None),
                                ("rain_eff", "<H", 24, 0.01, None), ("rho", "<H", 26, 3e-05, None), ("flags", "B", 28, None, None),
                                ("source_level", "B", 29, None, None), ("gust", "<h", 30, 0.01, None)]),
    "awr.SensorPose48.v1": (48, [("agent_no", "<H", 0, None, None), ("sensor_no", "B", 2, None, None), ("kind", "B", 3, None, None),
                                 ("flags", "B", 4, None, None), ("pos", "<3f", 8, None, None), ("q", "<4f", 20, None, None),
                                 ("hfov_rad", "<f", 36, None, None), ("vfov_rad", "<f", 40, None, None),
                                 ("range_m", "<f", 44, None, None)]),
}


def decode_rows(schema: str, buf: bytes) -> list[dict[str, Any]]:
    """按内嵌布局把 raw 记录（n 行连续）解码为 dict 列表；未知 schema 返回空列表。"""
    lay = LAYOUTS.get(schema)
    if lay is None:
        return []
    size, fields = lay
    out = []
    for i in range(len(buf) // size):
        base = i * size
        row: dict[str, Any] = {}
        for name, fmt, off, scale, new in fields:
            v = struct.unpack_from(fmt, buf, base + off)
            val: Any = list(v) if len(v) > 1 else v[0]
            if scale is not None:
                val = [x * scale for x in val] if isinstance(val, list) else val * scale
            row[new or name] = val
        out.append(row)
    return out


class Msg(NamedTuple):
    topic: str
    t_sim_ns: int
    data: Any


# ---------------------------------------------------------------- 错误
class AwrError(Exception):
    def __init__(self, code: int, name: str = "", message: str = "", detail: Any = None, remedy: str = "",
                 status: int = 0, retry_after_s: float | None = None) -> None:
        super().__init__(f"{code} {name}: {message}")
        self.code = int(code)
        self.name = name
        self.message = message
        self.detail = detail
        self.remedy = remedy
        self.status = status
        self.retry_after_s = retry_after_s


PARKED_CODE = 515
CLOSE_PARKED = 4430


class SandboxGone(AwrError):
    """会话已结束（506、510 或 WS 4410）；`snapshot` 为最近一次取得的配置快照（可能为 None）。"""

    snapshot: dict | None = None


def _leading_zero_bits(h: bytes) -> int:
    n = 0
    for b in h:
        if b == 0:
            n += 8
            continue
        return n + (8 - b.bit_length())
    return n


def solve_pow(challenge: dict, *, limit: int = 1 << 32) -> int:
    """`sha256-lz`：找 nonce 使 SHA-256(prefix || str(nonce)) 的前导零位数 >= bits（17 §17.3.5）。"""
    prefix = base64.b64decode(challenge["prefix_b64"])
    bits = int(challenge["bits"])
    for n in range(limit):
        if _leading_zero_bits(hashlib.sha256(prefix + str(n).encode("ascii")).digest()) >= bits:
            return n
    raise AwrError(509, "SANDBOX_CHALLENGE_REQUIRED", "no solution found within limit")


def _principal_path() -> Path:
    return Path(os.environ.get("AWR_SANDBOX_PRINCIPAL_FILE") or Path.home() / ".config" / "awr_sandbox" / "principal")


def _load_hint() -> str:
    """`~/.config/awr_sandbox/principal`（0600，32 位十六进制；17 §17.13.1）；不存在时生成。"""
    p = _principal_path()
    try:
        h = p.read_text(encoding="ascii").strip()
        if len(h) == 32 and all(c in "0123456789abcdef" for c in h):
            return h
    except OSError:
        pass
    h = secrets.token_hex(16)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(h)
    except OSError:
        pass
    return h


# ---------------------------------------------------------------- REST
class _Http:
    RETRY_CODES = (111, 504, 586, 211)

    def __init__(self, base_url: str, *, timeout_s: float, verify_tls: bool) -> None:
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=timeout_s, verify=verify_tls,
                                   headers={"accept": "application/json", "user-agent": f"awr-sandbox-sdk/{__version__}"})

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _error(r: httpx.Response) -> AwrError:
        try:
            p = r.json()
        except ValueError:
            p = {}
        ra = r.headers.get("retry-after")
        code = int(p.get("code") or 0) if isinstance(p, dict) else 0
        cls = SandboxGone if code in (506, 510) else AwrError
        # problem+json 的 `retry_after_ms`（毫秒，限流桶给出的精确等待）优先于 `Retry-After` 头（整秒、下限 1 s）：限流只差
        # 一个令牌时服务端给出几十毫秒，按头部等 1 s 会把一次增删改拖到 1.1–1.2 s（D2-AC-09 第 1 轮）
        ms = p.get("retry_after_ms") if isinstance(p, dict) else None
        if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms >= 0:
            ra_s: float | None = float(ms) / 1000.0
        else:
            ra_s = float(ra) if ra and ra.isdigit() else None
        return cls(code or r.status_code, str(p.get("name") or ""), str(p.get("message") or r.text[:200]),
                   p.get("detail"), str(p.get("remedy") or ""), r.status_code, ra_s)

    def request(self, method: str, path: str, *, token: str | None = None, json_body: Any = None,
                params: dict | None = None, retries: int = 3, headers: dict | None = None) -> httpx.Response:
        h = dict(headers or {})
        if token:
            h["authorization"] = f"Bearer {token}"
        attempt = 0
        while True:
            r = self.client.request(method, path, json=json_body, params=params, headers=h)
            if r.status_code < 400:
                return r
            err = self._error(r)
            retryable = r.status_code in (429, 503) and (err.code in self.RETRY_CODES or err.code >= 500)
            if retryable and attempt < retries:
                attempt += 1
                wait = err.retry_after_s if err.retry_after_s is not None else min(8.0, 0.5 * 2 ** attempt)
                time.sleep(min(30.0, wait) + random.uniform(0, min(0.2, 0.02 + 0.1 * wait)))  # 抖动随等待缩放
                continue
            raise err

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw).json()

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        r = self.request("POST", path, json_body=body if body is not None else {}, **kw)
        return r.json() if r.content else None

    def patch(self, path: str, body: Any, **kw: Any) -> Any:
        return self.request("PATCH", path, json_body=body, **kw).json()

    def delete(self, path: str, **kw: Any) -> Any:
        r = self.request("DELETE", path, **kw)
        return r.json() if r.content else None


# ---------------------------------------------------------------- 实时连接
class _Rt:
    """`awr.rt.v1` 同步客户端：读线程解码 TIME、BATCH（自动 ack）、控制消息；调用结果与流消息经队列交给调用方。"""

    def __init__(self, session: Session) -> None:
        from websockets.sync.client import connect

        self.s = session
        base = session.sb.http.base
        url = ("wss" if base.startswith("https") else "ws") + base[base.index(":"):] + f"{PREFIX}/sessions/{session.sid}/rt"
        self._cm = connect(url, subprotocols=[SUBPROTOCOL, f"bearer.{session.token}"], max_size=None, compression=None,
                           open_timeout=10, origin=session.sb.origin)
        self.ws = self._cm.__enter__()          # 上下文管理器写法（websockets 12 至 17 通用）
        self.channels: dict[int, dict] = {}
        self.results: dict[str, list[dict]] = {}
        self.cv = threading.Condition()
        self.streams: list[queue.Queue[Msg]] = []
        self.events: queue.Queue[dict] = queue.Queue()
        self.time: tuple | None = None        # (state, rate, t_sim_ns, mono)
        self.closed: int | None = None
        self.server_info: dict = {}
        self.client_ch: dict[str, int] = {}
        self.next_ch = 1
        self.seq: dict[int, int] = {}
        self.sub_id = 0
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._reader, name="awr-sandbox-rt", daemon=True)
        self.thread.start()
        self._wait(lambda: bool(self.server_info) and self.time is not None, 15.0, "serverInfo")
        self.send({"op": "hello", "client": f"awr-sandbox-sdk/{__version__}", "contracts": "1.1.0"})

    def _wait(self, pred: Callable[[], bool], timeout: float, what: str) -> None:
        end = time.monotonic() + timeout
        with self.cv:
            while not pred():
                if self.closed is not None:
                    raise SandboxGone(506, "SANDBOX_GONE", f"realtime connection closed ({self.closed}) while waiting for {what}")
                left = end - time.monotonic()
                if left <= 0:
                    raise AwrError(0, "TIMEOUT", f"timeout waiting for {what}")
                self.cv.wait(left)

    def send(self, obj: dict) -> None:
        self.ws.send(json.dumps(obj, separators=(",", ":")))

    def _reader(self) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            for m in self.ws:
                if isinstance(m, (bytes, bytearray)):
                    self._binary(bytes(m))
                else:
                    self._text(json.loads(m))
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else 1006
            with self.cv:
                self.closed = code
                self.cv.notify_all()
        except Exception:
            with self.cv:
                self.closed = 1011
                self.cv.notify_all()
        else:
            with self.cv:
                self.closed = 1000
                self.cv.notify_all()

    def _text(self, j: dict) -> None:
        op = j.get("op")
        with self.cv:
            if op == "serverInfo":
                self.server_info = j
            elif op == "advertise":
                for c in j.get("channels") or []:
                    self.channels[int(c["id"])] = c
            elif op == "unadvertise":
                for i in j.get("ids") or []:
                    self.channels.pop(int(i), None)
            elif op in ("result", "progress"):
                self.results.setdefault(str(j.get("id")), []).append(j)
            elif op == "event":
                self.events.put(j)
            elif op == "events":
                for e in j.get("items") or []:
                    self.events.put(e)
            elif op == "error":
                rid = j.get("id")
                if rid is not None:
                    self.results.setdefault(str(rid), []).append({"op": "result", "id": rid, "status": "rejected",
                                                                  "code": j.get("code"), "message": j.get("message"),
                                                                  "final": True})
            self.cv.notify_all()

    def _binary(self, b: bytes) -> None:
        if not b:
            return
        if b[0] == OP_TIME:
            _op, st, _epoch, rate, t_sim, _t_srv = _TIME.unpack_from(b, 0)
            with self.cv:
                self.time = (st & 0x0F, rate, t_sim, time.monotonic())
                self.cv.notify_all()
            return
        if b[0] != OP_BATCH:
            return
        _op, _flags, _epoch, frame_seq, t_sim = _BATCH_HDR.unpack_from(b, 0)
        off = _BATCH_HDR.size
        msgs = []
        while off + _REC_HDR.size <= len(b):
            ch, enc, rf, length, _seq, _dt = _REC_HDR.unpack_from(b, off)
            p = off + _REC_HDR.size
            payload = b[p:p + length]
            off = p + ((length + 7) & ~7)
            c = self.channels.get(ch)
            if c is None:
                continue
            if rf & RF_ZSTD:
                data: Any = payload           # zstd 压缩记录（激光雷达帧）原样交给调用方
            elif enc == ENC_MSGPACK:
                data = msgpack.unpackb(payload, raw=False, strict_map_key=False)
            elif enc == ENC_JSON:
                data = json.loads(payload)
            elif enc == ENC_RAW:
                rows = decode_rows(str(c.get("schemaName") or ""), payload)
                data = rows if rows or not payload else payload
            else:
                data = payload
            msgs.append(Msg(str(c.get("topic")), int(t_sim), data))
        with contextlib.suppress(Exception):
            self.send({"op": "ack", "frame": frame_seq})
        for m in msgs:
            for q in list(self.streams):
                q.put(m)

    # ------------------------------------------------------------ 公开操作
    def sim_now_ns(self) -> int:
        t = self.time
        if t is None:
            return 0
        st, rate, t_sim, mono = t
        adv = st in (1, 9)
        return int(t_sim + (time.monotonic() - mono) * 1e9 * (rate if adv else 0.0))

    def call(self, service: str, args: dict, *, cid: str | None = None, timeout: float = 10.0, wait_final: bool = False) -> dict:
        cid = cid or ("sdk-" + uuid.uuid4().hex[:20])
        self.send({"op": "call", "id": cid, "service": service, "args": args})

        def got() -> bool:
            rs = self.results.get(cid) or []
            return any(r.get("op") == "result" and (r.get("final") or not wait_final or r.get("status") == "rejected")
                       for r in rs)

        self._wait(got, timeout, f"result {cid}")
        res = [r for r in self.results.get(cid, []) if r.get("op") == "result"][-1]
        if res.get("status") in ("rejected", "failed", "timeout"):
            raise AwrError(int(res.get("code") or 0), str(res.get("reason") or ""), str(res.get("message") or ""),
                           res.get("detail"))
        return res

    def subscribe(self, topics: list[str]) -> None:
        subs = []
        for t in topics:
            self.sub_id += 1
            subs.append({"id": self.sub_id, "topic": t, "rate": 10})
        self.send({"op": "subscribe", "subs": subs})

    def client_channel(self, topic: str) -> int:
        with self.lock:
            if topic in self.client_ch:
                return self.client_ch[topic]
            cid = self.next_ch
            self.next_ch += 1
            self.client_ch[topic] = cid
        self.send({"op": "advertise", "channels": [{"id": cid, "topic": topic, "encoding": "raw",
                                                    "schemaName": "awr.VelSetpoint16.v1"}]})
        return cid

    def send_setpoint(self, ch: int, vel: tuple, yaw_rate: float, *, final: bool = False) -> None:
        with self.lock:
            seq = self.seq.get(ch, 0) + 1
            self.seq[ch] = seq
        frame = _CD_HDR.pack(OP_CLIENT_DATA, CD_FINAL if final else 0, ch, seq, self.sim_now_ns()) + \
            _VEL16.pack(float(vel[0]), float(vel[1]), float(vel[2]), float(yaw_rate))
        self.ws.send(frame)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._cm.__exit__(None, None, None)
        self.thread.join(5)


# ---------------------------------------------------------------- 资源集合
class _Coll:
    def __init__(self, s: Session, rel: str) -> None:
        self.s = s
        self.rel = rel

    def _p(self, *parts: str) -> str:
        return self.s.path(self.rel, *parts)

    def list(self) -> Any:
        return self.s.http.get(self._p())

    def get(self, rid: str) -> Any:
        return self.s.http.get(self._p(rid))

    def create(self, body: dict, **kw: Any) -> Any:
        return self.s.http.post(self._p(), body, headers={"idempotency-key": uuid.uuid4().hex}, **kw)

    def update(self, rid: str, patch: dict) -> Any:
        return self.s.http.patch(self._p(rid), patch)

    def remove(self, rid: str) -> Any:
        return self.s.http.delete(self._p(rid))


class Vehicles(_Coll):
    def add(self, model_id: str, **kw: Any) -> Any:
        return self.create({"model_id": model_id, **kw})

    def state(self, vid: str) -> Any:
        return self.s.http.get(self._p(vid, "state"))

    def sensors(self, vid: str) -> Any:
        return self.s.http.get(self._p(vid, "sensors"))

    def nav(self, vid: str) -> Any:
        return self.s.http.get(self._p(vid, "sensors", "nav"))

    def radar_tracks(self, vid: str) -> Any:
        return self.s.http.get(self._p(vid, "sensors", "radar", "tracks"))

    def acoustic_bearings(self, vid: str) -> Any:
        return self.s.http.get(self._p(vid, "sensors", "acoustic", "bearings"))

    def perception(self, vid: str) -> Any:
        return self.s.http.get(self._p(vid, "perception"))


class Targets(_Coll):
    def state(self, tid: str) -> Any:
        return self.s.http.get(self._p(tid, "state"))

    def suggested_radius(self, tid: str, model_id: str, **params: Any) -> Any:
        return self.s.http.get(self._p(tid, "suggested_radius"), params={"model_id": model_id, **params})


class Tasks(_Coll):
    def create(self, body: dict, *, dry_run: bool = False, **kw: Any) -> Any:  # type: ignore[override]
        return self.s.http.post(self._p(), body, params={"dry_run": str(dry_run).lower()},
                                   headers={"idempotency-key": uuid.uuid4().hex})

    def control(self, tid: str, op: str) -> Any:
        return self.s.http.post(self._p(f"{tid}:{op}"), {})

    def start(self, tid: str) -> Any:
        return self.control(tid, "start")

    def pause(self, tid: str) -> Any:
        return self.control(tid, "pause")

    def resume(self, tid: str) -> Any:
        return self.control(tid, "resume")

    def cancel(self, tid: str) -> Any:
        return self.control(tid, "cancel")

    def sustainability(self, tid: str) -> Any:
        return self.s.http.get(self._p(tid, "sustainability"))

    def relay_stats(self, tid: str) -> Any:
        return self.s.http.get(self.s.path("relay", tid, "stats"))


class Env:
    def __init__(self, s: Session) -> None:
        self.s = s

    def state(self) -> Any:
        return self.s.http.get(self.s.path("env", "state"))

    def preset(self, name: str, duration_s: float = 0.0) -> Any:
        return self.s.http.post(self.s.path("env", "preset"), {"name": name, "duration_s": duration_s})

    def calendar(self, start_utc: str, tz: str, jump: bool = True) -> Any:
        return self.s.http.post(self.s.path("env", "calendar"), {"start_utc": start_utc, "tz": tz, "jump": jump})

    def query(self, points: list, fields: list | None = None) -> Any:
        body: dict[str, Any] = {"points": points}
        if fields:
            body["fields"] = fields
        return self.s.http.post(self.s.path("env", "query"), body)


class Tasking:
    def __init__(self, s: Session) -> None:
        self.s = s

    def snapshot(self) -> Any:
        return self.s.http.get(self.s.path("tasking", "snapshot"))

    def submit(self, plan: dict) -> Any:
        return self.s.http.post(self.s.path("tasking", "plans"), plan,
                                   headers={"idempotency-key": uuid.uuid4().hex})


class Catalog:
    def __init__(self, sb: Sandbox, s: Session | None = None) -> None:
        self.sb = sb
        self.s = s

    def models(self) -> Any:
        return self.sb.http.get(f"{PREFIX}/catalog/models")

    def model(self, model_id: str) -> Any:
        return self.sb.http.get(f"{PREFIX}/catalog/models/{model_id}")

    def sensors(self) -> Any:
        return self.sb.http.get(f"{PREFIX}/catalog/sensors")


class Lease:
    def __init__(self, s: Session, vid: str, priority: str) -> None:
        self.s = s
        self.vid = vid
        self.priority = priority
        self.result = s.http.post(s.path(f"vehicles/{vid}:acquire"), {"priority": priority})

    def release(self) -> Any:
        with contextlib.suppress(AwrError):
            self.s.velocity_stop(self.vid)
        return self.s.http.post(self.s.path(f"vehicles/{self.vid}:release"), {})

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ---------------------------------------------------------------- 会话
class _SessionHttp:
    """会话内请求：总是带当前 token；遇 `515 SANDBOX_PARKED` 时自动恢复一次（满员时排队认领）后重试（M17-FR-080）。"""

    def __init__(self, s: Session) -> None:
        self.s = s

    def _call(self, fn: Callable[..., Any], path: str, *a: Any, **kw: Any) -> Any:
        try:
            return fn(path, *a, token=self.s.token, **kw)
        except AwrError as e:
            if e.code != PARKED_CODE or self.s._resuming:
                raise
        self.s.resume(wait=True)
        h = kw.get("headers")
        if isinstance(h, dict) and "idempotency-key" in h:   # 被拒的请求未生效：换新键，避免服务端重放缓存的 515
            kw["headers"] = {**h, "idempotency-key": uuid.uuid4().hex}
        return fn(path, *a, token=self.s.token, **kw)

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        return self._call(lambda p, **k: self.s.sb.http.request(method, p, **k), path, **kw)

    def get(self, path: str, **kw: Any) -> Any:
        return self._call(self.s.sb.http.get, path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self._call(self.s.sb.http.post, path, body, **kw)

    def patch(self, path: str, body: Any, **kw: Any) -> Any:
        return self._call(self.s.sb.http.patch, path, body, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self._call(self.s.sb.http.delete, path, **kw)


class Session:
    KEEPALIVE_MARGIN_S = 300

    def __init__(self, sb: Sandbox, sid: str, token: str, info: dict | None = None) -> None:
        self.sb = sb
        self.sid = sid
        self.token = token
        self.info: dict = dict(info or {})
        self._rt: _Rt | None = None
        self._vel: dict[str, dict] = {}
        self._last_snapshot: dict | None = None
        self._ka_stop = threading.Event()
        self._ka_thread: threading.Thread | None = None
        self.http = _SessionHttp(self)
        self._resuming = False
        self.vehicles = Vehicles(self, "vehicles")
        self.targets = Targets(self, "targets")
        self.nests = _Coll(self, "nests")
        self.tasks = Tasks(self, "tasks")
        self.models = _Coll(self, "models")
        self.env = Env(self)
        self.tasking = Tasking(self)
        self.catalog = Catalog(sb, self)
        if sb.auto_keepalive:
            self._ka_thread = threading.Thread(target=self._keepalive_loop, name="awr-sandbox-keepalive", daemon=True)
            self._ka_thread.start()

    # ------------------------------------------------------------ 属性
    @property
    def world(self) -> str:
        return str(self.info.get("world_id") or "")

    @property
    def limits(self) -> dict:
        return dict(self.info.get("limits") or {})

    @property
    def expires_unix_ns(self) -> int:
        return int(self.info.get("expires_unix_ns") or 0)

    def path(self, *parts: str) -> str:
        rel = "/".join(p.strip("/") for p in parts if p)
        return f"{PREFIX}/sessions/{self.sid}" + (f"/{rel}" if rel else "")

    def _gone(self, e: AwrError) -> None:
        if isinstance(e, SandboxGone):
            e.snapshot = self._last_snapshot
        raise e

    # ------------------------------------------------------------ 会话 API
    def state(self) -> dict:
        try:
            self.info = self.http.get(self.path())
        except AwrError as e:
            self._gone(e)
        return self.info

    def wait_ready(self, timeout_s: float = 30.0) -> dict:
        end = time.monotonic() + timeout_s
        while True:
            try:
                d = self.http.get(self.path(), params={"wait_state": "READY", "wait_s": 10})
            except AwrError as e:
                self._gone(e)
                raise
            self.info = d
            if d.get("state") in ("READY", "ACTIVE", "IDLE"):
                return d
            if time.monotonic() > end:
                raise AwrError(502, "SANDBOX_SPAWN_FAILED", f"session not ready after {timeout_s} s", d)

    def keepalive(self) -> dict:
        """SB-10：续期并换发 token。驻留中的会话返回 515 而不自动恢复（续期不应唤醒节能中的会话）。"""
        d = self.sb.http.post(self.path("keepalive"), {}, token=self.token)
        if d.get("token"):
            self.token = d["token"]
        if isinstance(d.get("session"), dict):
            self.info = d["session"]
        return d

    def park(self, reason: str = "user") -> dict:
        """SB-69：保存并暂停会话、释放槽位（PARKING → PARKED）；之后任何会话请求都会先自动恢复。"""
        if self._rt is not None:
            self._rt.close()
            self._rt = None
            self._vel.clear()
        d = self.sb.http.post(self.path("park"), {"reason": reason}, token=self.token)
        if isinstance(d, dict) and isinstance(d.get("session"), dict):
            self.info = d["session"]
        return d or {}

    def resume(self, *, wait: bool = True, timeout_s: float = 1800.0,
               on_queue: Callable[[str, dict], None] | None = None) -> dict:
        """SB-70：恢复驻留的会话（201 拉起；满员 202 时按恢复票长轮询并认领）；换发的 token 自动替换。wait 时等到 READY。"""
        self._resuming = True
        try:
            r = self.sb.http.request("POST", self.path("resume"), token=self.token, json_body={})
            d = r.json()
            if r.status_code == 202:
                if not wait:
                    return d
                d = self.sb._wait_queue(d["queue"], timeout_s, on_queue)
            if d.get("token"):
                self.token = d["token"]
            if isinstance(d.get("session"), dict):
                self.info = d["session"]
            if self._rt is not None and self._rt.closed is not None:
                self._rt = None
            return self.wait_ready() if wait else self.info
        finally:
            self._resuming = False

    def _keepalive_loop(self) -> None:
        while not self._ka_stop.wait(30.0):
            exp = self.expires_unix_ns / 1e9
            if exp and exp - time.time() <= self.KEEPALIVE_MARGIN_S:
                with contextlib.suppress(AwrError):
                    self.keepalive()

    def snapshot(self) -> dict:
        d = self.http.get(self.path("snapshot"))
        self._last_snapshot = d
        return d

    def restore(self, snapshot: dict) -> dict:
        self.info = self.http.post(self.path("restore"), {"snapshot": snapshot})
        return self.info

    def load_template(self, template: str) -> dict:
        self.info = self.http.post(self.path("template"), {"template": template})
        return self.info

    def switch_world(self, world: str, template: str | None = None, *, wait: bool = True) -> dict:
        body: dict[str, Any] = {"world": world}
        if template:
            body["template"] = template
        if self._rt is not None:
            self._rt.close()
            self._rt = None
            self._vel.clear()
        self.info = self.http.post(self.path("world"), body)
        return self.wait_ready() if wait else self.info

    def clock(self, op: str, *, rate: float | None = None, ticks: int | None = None) -> dict:
        body: dict[str, Any] = {"op": op}
        if rate is not None:
            body["rate"] = rate
        if ticks is not None:
            body["ticks"] = ticks
        res = self.http.post(self.path("clock"), body, headers={"idempotency-key": uuid.uuid4().hex})
        return dict(res.get("data") or {}, status=res.get("status"), warnings=res.get("warnings") or [])

    def events(self, since_seq: int = 0, limit: int = 1000, types: list[str] | None = None) -> dict:
        params: dict[str, Any] = {"since_seq": since_seq, "limit": limit}
        if types:
            params["types"] = ",".join(types)
        return self.http.get(self.path("events"), params=params)

    def batch(self, until_s: float, *, seed: int | None = None) -> dict:
        body: dict[str, Any] = {"until_s": until_s}
        if seed is not None:
            body["seed"] = seed
        return self.http.post(self.path("batch"), body)

    def batch_status(self, batch_id: str) -> dict:
        return self.http.get(self.path("batch", batch_id))

    def relay(self, tid: str) -> Any:
        return self.tasks.relay_stats(tid)

    def close(self) -> None:
        self._ka_stop.set()
        if self._rt is not None:
            self._rt.close()
            self._rt = None
        with contextlib.suppress(AwrError):
            self._last_snapshot = self.snapshot()
        with contextlib.suppress(AwrError):
            self.http.delete(self.path())

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------ 命令与控制
    def rt(self) -> _Rt:
        if self._rt is None or self._rt.closed is not None:
            if self._rt is not None and self._rt.closed == 4410:
                raise SandboxGone(506, "SANDBOX_GONE", "session ended (4410)")
            if self._rt is not None and self._rt.closed == CLOSE_PARKED:   # 节能断开：先恢复（AWR-17 §17.20.7）
                self._rt = None
                self._vel.clear()
                self.resume(wait=True)
            elif self.info.get("state") in ("PARKING", "PARKED"):
                self.resume(wait=True)
            self._rt = _Rt(self)
        return self._rt

    def command(self, vid: str, name: str, *, wait: bool = True, timeout_s: float = 10.0, **args: Any) -> dict:
        """`uav/{vid}/cmd/{name}`：有实时连接时经 WS `call`，否则 REST `POST .../commands`（同一 call id 幂等重试 2 次）。"""
        cid = "sdk-" + uuid.uuid4().hex[:20]
        if self._rt is not None and self._rt.closed is None:
            return self._rt.call(f"uav/{vid}/cmd/{name}", args, cid=cid, timeout=timeout_s, wait_final=wait)
        last: AwrError | None = None
        for _ in range(3):
            try:
                return self.http.post(self.path("vehicles", vid, "commands"),
                                      {"id": cid, "op": name, "args": args, "timeout_ms": int(timeout_s * 1000)})
            except AwrError as e:
                last = e
                if e.status not in (502, 503, 0):
                    raise
                time.sleep(0.3)
        assert last is not None
        raise last

    def acquire(self, vid: str, priority: str = "normal") -> Lease:
        return Lease(self, vid, priority)

    def velocity(self, vid: str, vx: float, vy: float, vz: float, yaw_rate: float = 0.0, frame: str = "world",
                 keepout_guard: str = "clamp") -> None:
        """速度控制（17 §17.7.8）：首次调用时 `call uav/{vid}/cmd/velocity` 与 `advertise`，之后发送 CLIENT_DATA；<= 50 Hz。"""
        rt = self.rt()
        st = self._vel.get(vid)
        if st is None:
            res = rt.call(f"uav/{vid}/cmd/velocity", {"frame": frame, "keepout_guard": keepout_guard}, timeout=10)
            st = {"cid": res.get("id"), "ch": rt.client_channel(f"uav/{vid}/setpoint"), "last": 0.0, "pending": None,
                  "sent": 0}
            self._vel[vid] = st
        now = time.monotonic()
        hz = float(self.limits.get("setpoint_hz_max") or 50.0)
        if now - st["last"] < 1.0 / hz:
            st["pending"] = (vx, vy, vz, yaw_rate)          # 合并：最近一个样本在下一次可发送时发出
            return
        if st["pending"] is not None:
            st["pending"] = None
        rt.send_setpoint(st["ch"], (vx, vy, vz), yaw_rate)
        st["last"] = now
        st["sent"] += 1

    def velocity_stop(self, vid: str) -> None:
        st = self._vel.pop(vid, None)
        if st is None or self._rt is None:
            return
        try:
            self._rt.send_setpoint(st["ch"], (0.0, 0.0, 0.0), 0.0, final=True)
            self._rt.call(f"uav/{vid}/cmd/velocity_stop", {}, timeout=5)
        except AwrError:
            pass

    # ------------------------------------------------------------ 流
    def stream(self, topics: list[str], *, seconds: float | None = None) -> Iterator[Msg]:
        """订阅并迭代 `Msg(topic, t_sim_ns, data)`；seconds 给出时到期结束；会话结束（4410）抛 SandboxGone。"""
        rt = self.rt()
        q: queue.Queue[Msg] = queue.Queue()
        rt.streams.append(q)
        rt.subscribe(topics)
        end = None if seconds is None else time.monotonic() + seconds
        try:
            while True:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return
                try:
                    m = q.get(timeout=min(0.5, left) if left is not None else 0.5)
                except queue.Empty:
                    if rt.closed is not None:
                        if rt.closed == 4410:
                            g = SandboxGone(506, "SANDBOX_GONE", "session ended (4410)")
                            g.snapshot = self._last_snapshot
                            raise g from None
                        if rt.closed == CLOSE_PARKED:            # 节能断开：恢复后重新订阅，流继续
                            if q in rt.streams:
                                rt.streams.remove(q)
                            rt = self.rt()
                            rt.streams.append(q)
                            rt.subscribe(topics)
                            continue
                        return
                    continue
                if m.topic in topics or any(t.endswith("*") and m.topic.startswith(t[:-1]) for t in topics):
                    yield m
        finally:
            if q in rt.streams:
                rt.streams.remove(q)

    def sleep(self, s: float) -> None:
        """等待 s 秒；期间实时连接的读线程继续 ack 与心跳。"""
        time.sleep(max(0.0, s))


# ---------------------------------------------------------------- 入口
class Sandbox:
    def __init__(self, base_url: str, *, principal_hint: str | None = None, timeout_s: float = 10.0,
                 auto_keepalive: bool = True, verify_tls: bool = True, origin: str | None = None) -> None:
        self.http = _Http(base_url, timeout_s=timeout_s, verify_tls=verify_tls)
        self.principal_hint = principal_hint or _load_hint()
        self.auto_keepalive = auto_keepalive
        self.origin = origin
        self.catalog = Catalog(self)
        meta = self.meta()
        if int(meta.get("api_version") or 0) != 1 or int(str(meta.get("contracts", "1")).split(".")[0]) != CONTRACTS_MAJOR:
            raise AwrError(0, "INCOMPATIBLE", f"server api {meta.get('api_version')} contracts {meta.get('contracts')}")
        self.features = list(meta.get("features") or [])

    def meta(self) -> dict:
        return self.http.get(f"{PREFIX}/meta")

    def capacity(self) -> dict:
        """SB-71：公开容量（实时观看席位、沙盒槽位、排队人数与预计等待、构建信息；`awr.capacity.v1`）。"""
        return self.http.get(f"{PREFIX}/capacity")

    def status(self) -> dict:
        return self.meta()

    def worlds(self) -> list[dict]:
        return list(self.http.get(f"{PREFIX}/worlds").get("items") or [])

    def templates(self, world: str | None = None) -> list[dict]:
        return list(self.http.get(f"{PREFIX}/templates", params={"world": world} if world else None).get("items") or [])

    def attach(self, sid: str, token: str) -> Session:
        s = Session(self, sid, token)
        s.state()
        return s

    def close(self) -> None:
        self.http.close()

    def create(self, world: str = "synthcity", template: str | None = "t5_area_guard", *, snapshot: dict | None = None,
               calendar: dict | None = None, acoustic_bg: str | None = None, seed: int | None = None,
               queue: bool = True, queue_ok: bool = False, queue_timeout_s: float = 1800.0,
               on_queue: Callable[[str, dict], None] | None = None, force_new: bool = False,
               wait_ready: bool = True, identity_token: str | None = None) -> Session:
        body: dict[str, Any] = {"world": world, "principal_hint": self.principal_hint, "client": {"kind": "sdk",
                                                                                                  "version": __version__}}
        if snapshot is not None:
            body["config_snapshot"] = snapshot
        elif template is not None:
            body["template"] = template
        body.update({k: v for k, v in (("calendar", calendar), ("acoustic_bg", acoustic_bg), ("seed", seed)) if v is not None})
        if force_new:
            body["force_new"] = True
        if queue_ok:
            body["queue_ok"] = True
        hdr = {"x-awr-identity": identity_token} if identity_token else None   # 内部档（AWR-17 §17.20.5）
        for _attempt in range(4):
            try:
                r = self.http.request("POST", f"{PREFIX}/sessions", json_body=body, headers=hdr)
            except AwrError as e:
                if e.code == 509 and isinstance(e.detail, dict) and isinstance(e.detail.get("challenge"), dict):
                    ch = e.detail["challenge"]
                    body["pow"] = {"challenge_id": ch["challenge_id"], "nonce": solve_pow(ch)}
                    continue
                raise
            d = r.json()
            if r.status_code == 202:
                if not queue:
                    q = d["queue"]
                    self.http.delete(f"{PREFIX}/queue/{q['ticket_id']}", headers={"authorization": f"Bearer {q['ticket_token']}"})
                    raise AwrError(500, "SANDBOX_FULL", "sandbox full and queue=False", d)
                d = self._wait_queue(d["queue"], queue_timeout_s, on_queue)
            if not d.get("token"):
                raise AwrError(409, "SANDBOX_EXISTS", "an active session already exists for this principal and address "
                               "prefix; attach() with its token or create(force_new=True)", d.get("session"))
            s = Session(self, d["session"]["sid"], d["token"], d["session"])
            if wait_ready:
                s.wait_ready()
            return s
        raise AwrError(509, "SANDBOX_CHALLENGE_REQUIRED", "proof of work not accepted")

    def _wait_queue(self, q: dict, timeout_s: float, on_queue: Callable[[str, dict], None] | None) -> dict:
        auth = {"authorization": f"Bearer {q['ticket_token']}"}
        end = time.monotonic() + timeout_s
        tid = q["ticket_id"]
        if on_queue:
            on_queue("sandbox.queued", dict(q))
        while time.monotonic() < end:
            v = self.http.get(f"{PREFIX}/queue/{tid}", params={"wait_s": 25}, headers=auth)
            if v.get("state") == "slot_ready":
                if on_queue:
                    on_queue("sandbox.slot_ready", v)
                r = self.http.request("POST", f"{PREFIX}/queue/{tid}/claim", json_body={"principal_hint": self.principal_hint},
                                      headers=auth)
                return r.json()
            if v.get("state") in ("expired", "claimed"):
                raise AwrError(511, "SANDBOX_TICKET_INVALID", f"ticket {v.get('state')}", v)
            if on_queue:
                on_queue("sandbox.queue", v)
        with contextlib.suppress(AwrError, httpx.HTTPError):
            self.http.delete(f"{PREFIX}/queue/{tid}", headers=auth)
        raise AwrError(500, "SANDBOX_FULL", f"still queued after {timeout_s} s")
