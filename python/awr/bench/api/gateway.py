"""lab-gateway：内部工作区实时网关（AWR-17 §18.11；AWR-05 §7.11、ADR-166；M11 §16.1 扩展点 10；D3-AC-51）。

独立进程 `python -m awr.bench.api.gateway`（缺省 127.0.0.1:18651，dmax `d3` profile 以 `d3-lab-gateway` 容器运行，经鉴权反向代理
暴露在 ZeroTier 内网，`tools/deploy/dmax/lab/`）。复用 `awr.api.rt` 网关库的协议常量、控制消息构造与二进制帧编码
（`protocol`、`awr.contracts.frame`），不改 `api/rt/gateway.py`（AWR-05 §4.8），只承载 §18.11 的 D3 topic。

**握手**（同 D1 §6.2 的顺序）：Origin 不在白名单（`AWR_BENCH_ALLOWED_ORIGINS`）为升级前 403 `303`；子协议不含 `awr.rt.v1` 为升级前
400（`AWR-Supported-Protocols`）→ 以 `awr.rt.v1` 接受升级（不回显 bearer）→ 子协议 `bearer.<d3st 令牌>`（`aud = lab-rt`，BN-04 签发，
10 min）：缺失 `error{301}`、签名或到期或来源 bench token 撤销 `error{302}` 后 4401；受邀成员 `error{115}` 后 4403（§7.11：受邀成员
不访问实时 topic）→ 连接数（全部 ≤ 64、每 principal ≤ 8，`error{316}` + 4429）→ serverInfo（`capabilities ⊇ {"lab"}`、
`lab{principal, role, projects[]}`，不含任何 run 级 sim-core 信息）、空 advertise、TIME（STOPPED：lab-gateway 不持有仿真时钟）→
10 s 内等待 hello（4408），contracts 主版本不同 `error{311}` 后 4426。令牌只用于握手；连接期间来源 bench token 被撤销或到期时
≤ 2 s 内发 `error{302}` 并以 4401 关闭（每秒核对一次令牌表）。

**订阅**（`subscribe{subs: [{id, topic, params: {run_id}, rate?}]}`）：topic 名不含 `run_id`，以 `params.run_id` 绑定运行
（`bench/run/{rid}/status` 取 topic 中的 rid）；同一连接可绑定多个运行。逐项校验：

| 条件 | 结果 |
|---|---|
| topic 不属于 §18.11 五个 topic，或含通配（lab topic 只接受具体名） | `error{314}` |
| 缺 `run_id`、`run_id` 非法或与 topic 中的 rid 不符 | `error{300}` |
| 运行不存在（状态库与数据集目录都没有） | `error{305}` |
| 角色低于 topic 作用域（`LAB_SCOPES`：研究者；`actor/state` 为评测员） | `error{115}`（`why = role`），写 `deny` 审计 |
| 研究者：`bench/run/*/status`、`event/status` 限本人与所属项目，预览与 LiDAR 限本人运行 | `error{115}`（`why = project_scope / owner_scope`），写 `deny` 审计 |
| 每运行预览订阅 > 16，或订阅总数 > 256 | `error{315}` |

通过后回 `advertise`（该连接新见到的 channel，`entity{run_id}`）与 `subscribed{id, topic, run_id, channels, rate, mode}`，并立即
下发该 channel 的最新值（BATCH 置 SNAPSHOT）。`event/status` 的 `phase` 字段只对评测员与管理员下发（研究者收到去掉 `phase` 的
同一载荷）。数据帧为 D1 的 BATCH（`awr.contracts.frame.encode_batch`），每个运行一帧（帧头 `t_sim_ns` 为该运行最新样本时刻），
记录 `KEYFRAME`（全部 lab topic 自包含）；按订阅速率限频（预览 ≤ 5 Hz、LiDAR ≤ 2 Hz、状态与事件状态 1 Hz、`actor/state` 10 Hz），
状态迁移不受限频约束。

**数据来源**（`LabHub`，只对有订阅的运行轮询）：

- `bench/run/{rid}/status`：Run 状态库（`bench.sqlite`，`RunStore`）的 `state`、`attempt`、`restart_count`、`blocked_by`、`reason`，
  叠加 run-controller 写入的实时计数（下述实时投递文件，`t_sim_ns`、`progress_pct`、`frames_delivered`、`agent_timeouts`、
  `m25_wait_s`、`writer_block_s`）；状态库没有的运行取数据集目录 `run_manifest.json`（或 Env 运行的 `manifest.json`）的 `state`；
  每秒一次，状态迁移时立即发布（M27-FR-041）。
- 其余 topic：生产者（预览生产者、M23 插件、LiDAR 降采样）以 `write_live()` 原子写入实时投递文件
  `<data_root>/live/<run_id>/<topic 以 ~ 代替 />.awlv`（头 `b"AWLV"` + `i64 t_sim_ns`，其后为 topic 载荷），lab-gateway 以
  mtime 变化读取并发布（latest 语义，同 D1 `mode = latest`）。生产者与网关在不同容器中，共享数据根卷，不经 sim-core 总线。
  预览载荷由 `preview_blob()` 生成（`u32 seq, i64 t_ns, u16 w, u16 h` + JPEG，480 × 270，质量 75）。

审计（§18.3.7，`AuditLog`）：握手拒绝与订阅越权写 `deny`（`route = lab-rt`），连接建立与关闭写 `lab_rt_open` / `lab_rt_close`
（principal、角色、令牌 id、连接 id、下行字节量）；不记录令牌明文。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import struct
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgpack
from fastapi import FastAPI, WebSocket
from starlette.responses import JSONResponse

from awr.api.problem import PROBLEM_MEDIA, problem_body
from awr.api.rt.protocol import PROTOCOL, error_msg, jdump
from awr.bench.api.deps import project_ok
from awr.bench.auth import AuthError, BenchAuth, Principal
from awr.bench.runs_store import RUN_ID, RunStore, find_run
from awr.bench.settings import BenchSettings
from awr.contracts import CONTRACTS_VERSION
from awr.contracts import frame as F
from awr.contracts import topics as T
from awr.contracts.enums import TimeState
from awr.contracts.reasons import Reason

__all__ = ["GATEWAY_PORT", "LIVE_HDR", "LIVE_MAGIC", "MAX_CONNS", "MAX_CONNS_PER_PRINCIPAL", "MAX_PREVIEW_PER_RUN", "PREVIEW_HDR", "WS_PATH",
           "LabHub", "create_gateway_app", "live_path", "main", "preview_blob", "read_live", "status_payload", "write_live"]

WS_PATH = "/api/lab/rt"
GATEWAY_PORT = 18651                       # 127.0.0.1:18651（AWR-17 §19，经鉴权反向代理暴露在 ZeroTier 内网）
AUD = "lab-rt"
MAX_CONNS = 64                             # API-NFR-036：50 个连接时 CPU ≤ 1 核
MAX_CONNS_PER_PRINCIPAL = 8
MAX_SUBS = 256
MAX_PREVIEW_PER_RUN = 16                   # §18.11：每运行 ≤ 16 个预览订阅
CTRL_MAX = 1024                            # 每连接待发控制消息上限（超出以 1008 关闭，慢消费者）
HELLO_TIMEOUT_S = 10.0
TOKEN_CHECK_S = 1.0                        # 来源 bench token 撤销与到期的核对周期（≤ 2 s 失效）
STATUS_PERIOD_S = 1.0                      # bench/run/{rid}/status 1 Hz
POLL_S = 0.2                               # 状态库与实时投递文件的轮询周期（状态迁移在 ≤ 0.2 s 内发布）
MAX_TEXT_BYTES = 262_144
LIVE_DIR = "live"
LIVE_MAGIC = b"AWLV"
LIVE_HDR = struct.Struct("<4sq")           # 实时投递文件头：魔数、t_sim_ns
LIVE_MAX_BYTES = 4 << 20                   # 单个实时样本上限（预览 JPEG 与 ≤ 5,000 点 LiDAR 远小于此）
PREVIEW_HDR = struct.Struct("<IqHH")       # §18.11：u32 seq、i64 t_ns、u16 w、u16 h，其后为 JPEG
PREVIEW_SIZE = (480, 270)
PREVIEW_QUALITY = 75
STATUS_TOPIC = "bench/run/{rid}/status"
ROLE_FOR_SCOPE = {"lab_researcher": "researcher", "lab_evaluator": "evaluator"}
# 订阅速率上限（Hz）：预览可配 ≤ 5 Hz（§18.11），其余取 topic 原生频率
RATE_CAP = {"uav/{id}/sensor/{name}/preview": 5.0, "uav/{id}/lidar/{name}/v2": 2.0, STATUS_TOPIC: 1.0, "event/status": 1.0,
            "actor/state": 10.0}
OWNER_ONLY = frozenset({"uav/{id}/sensor/{name}/preview", "uav/{id}/lidar/{name}/v2"})   # 研究者只能订阅本人运行（M27-FR-154）
STATUS_COUNTERS = {"t_sim_ns": 0, "progress_pct": 0.0, "frames_delivered": 0, "agent_timeouts": 0, "m25_wait_s": 0.0, "writer_block_s": 0.0}


# ── 实时投递文件（生产者侧辅助） ───────────────────────────────────────────────────────────────────────────────────────

def live_path(data_root: Path, run_id: str, topic: str) -> Path:
    """`<data_root>/live/<run_id>/<topic 以 ~ 代替 />.awlv`（topic chunk 不含 `~`，映射可逆）。"""
    if not RUN_ID.match(run_id):
        raise ValueError(f"invalid run_id {run_id!r}")
    if T.match_lab_topic(topic) is None or not T.valid_topic(topic):
        raise ValueError(f"not a lab-gateway topic: {topic!r}")
    return Path(data_root) / LIVE_DIR / run_id / (topic.replace("/", "~") + ".awlv")


def write_live(data_root: Path, run_id: str, topic: str, payload: bytes, t_sim_ns: int) -> Path:
    """生产者投递一个最新样本（原子替换）。`bench/run/{rid}/status` 的载荷为 msgpack 计数字典（`STATUS_COUNTERS` 的字段），
    lab-gateway 叠加状态库的状态字段后发布。"""
    if len(payload) > LIVE_MAX_BYTES:
        raise ValueError(f"live sample exceeds {LIVE_MAX_BYTES} bytes")
    p = live_path(data_root, run_id, topic)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(LIVE_HDR.pack(LIVE_MAGIC, int(t_sim_ns)) + bytes(payload))
    os.replace(tmp, p)
    return p


def read_live(path: Path) -> tuple[int, bytes] | None:
    """→ (t_sim_ns, 载荷)；文件缺失、过大或头不合法时为 None。"""
    try:
        b = Path(path).read_bytes()
    except OSError:
        return None
    if len(b) < LIVE_HDR.size or len(b) > LIVE_HDR.size + LIVE_MAX_BYTES:
        return None
    magic, t = LIVE_HDR.unpack_from(b)
    return (int(t), b[LIVE_HDR.size:]) if magic == LIVE_MAGIC else None


def preview_blob(seq: int, t_ns: int, image: Any, *, size: tuple[int, int] = PREVIEW_SIZE, quality: int = PREVIEW_QUALITY) -> bytes:
    """预览帧载荷（§18.11）：`image` 为 PIL 图像、HxWx3 uint8 数组或 JPEG 字节；缩放到 `size`（缺省 480 × 270）、JPEG 质量 75。"""
    import io

    from PIL import Image

    if isinstance(image, (bytes, bytearray, memoryview)):
        im = Image.open(io.BytesIO(bytes(image)))
    elif isinstance(image, Image.Image):
        im = image
    else:
        im = Image.fromarray(image)
    im = im.convert("RGB")
    if im.size != size:
        im = im.resize(size, Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=int(quality))
    return PREVIEW_HDR.pack(int(seq) & 0xFFFFFFFF, int(t_ns), size[0], size[1]) + buf.getvalue()


def status_payload(view: dict | None, counters: dict | None = None) -> dict:
    """`bench/run/{rid}/status` 载荷（§18.11、M27-FR-041）：状态字段取状态库（或目录清单），计数取 run-controller 投递。"""
    out: dict[str, Any] = {"state": "UNKNOWN", "attempt": 1, "restart_count": 0, **STATUS_COUNTERS}
    for k, v in (counters or {}).items():
        if k in STATUS_COUNTERS and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = type(STATUS_COUNTERS[k])(v)
    if view:
        out["state"] = str(view.get("state") or "UNKNOWN")
        out["attempt"] = int(view.get("attempt") or 1)
        out["restart_count"] = int(view.get("restart_count") or 0)
        if view.get("blocked_by"):
            out["blocked_by"] = list(view["blocked_by"])
        if view.get("reason"):
            out["reason"] = view["reason"]
    return out


# ── channel 与运行索引 ──────────────────────────────────────────────────────────────────────────────────────────────

class LabChannel:
    """(运行, topic) 一个 channel；载荷为最新值。`event/status` 另存去掉 `phase` 的载荷（研究者）。"""

    __slots__ = ("_cache", "enc_code", "id", "pattern", "payload", "redacted", "run_id", "seq", "spec", "t_sim_ns", "topic")

    def __init__(self, cid: int, run_id: str, topic: str) -> None:
        spec = T.match_lab_topic(topic)
        if spec is None:
            raise ValueError(f"not a lab-gateway topic: {topic!r}")
        self.id = cid
        self.run_id = run_id
        self.topic = topic
        self.spec = spec
        self.pattern = spec.pattern
        self.enc_code = {"raw": F.ENC_RAW, "msgpack": F.ENC_MSGPACK, "json": F.ENC_JSON, "blob": F.ENC_BLOB}[spec.encoding]
        self.seq = 0
        self.t_sim_ns = 0
        self.payload = b""
        self.redacted: bytes | None = None
        self._cache: dict[tuple[int, int, bool], bytes] = {}

    def publish(self, payload: bytes, t_sim_ns: int) -> None:
        self.seq = (self.seq + 1) & 0xFFFFFFFF or 1
        self.payload = bytes(payload)
        self.t_sim_ns = int(t_sim_ns)
        self.redacted = None
        if self.pattern == "event/status":
            with contextlib.suppress(Exception):
                d = msgpack.unpackb(self.payload, raw=False)
                if isinstance(d, dict) and "phase" in d:
                    d.pop("phase")
                    self.redacted = msgpack.packb(d, use_bin_type=True)
        self._cache.clear()

    def record(self, frame_t_ns: int, *, privileged: bool) -> bytes:
        red = not privileged and self.redacted is not None
        key = (self.seq, frame_t_ns, red)
        rec = self._cache.get(key)
        if rec is None:
            dt = max(-(2 ** 31), min(2 ** 31 - 1, (self.t_sim_ns - frame_t_ns) // 1000))
            rec = F.encode_record(self.id, self.enc_code, F.RF_KEYFRAME, self.seq, dt, self.redacted if red else self.payload)
            self._cache[key] = rec
        return rec

    def advert(self) -> dict[str, Any]:
        s = self.spec
        return {"id": self.id, "topic": self.topic, "kind": s.kind, "encoding": s.encoding, "schemaName": s.schema_name, "layout": None,
                "schemaRef": None, "nativeHz": float(s.native_hz), "defaultRate": int((s.default_rate or {}).get("S", 1)),
                "priority": int(s.priority if s.priority is not None else 3), "selfContained": True, "entity": {"run_id": self.run_id}}


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    state: str
    tenant: str | None
    owner: str | None
    managed: bool
    view: dict = field(default_factory=dict)


class RunIndex:
    """运行查找：先查 Run 状态库（受管运行），再查数据集目录（目录运行：`run_manifest.json` 或 Env 运行的 `manifest.json`）。"""

    def __init__(self, settings: BenchSettings) -> None:
        self.settings = settings
        self._store: RunStore | None = None
        self._lock = threading.Lock()

    @property
    def store(self) -> RunStore:
        with self._lock:
            if self._store is None:
                self._store = RunStore(self.settings.db_path)
            return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()

    def lookup(self, run_id: str) -> RunInfo | None:
        if not RUN_ID.match(run_id):
            return None
        v = self.store.get(run_id)
        if v is not None:
            return RunInfo(run_id, v["state"], v.get("tenant"), v.get("submitted_by") or None, True, v)
        d = find_run(self.settings.dataset_dir, run_id)
        if d is None:
            return None
        doc: dict = {}
        for name in ("run_manifest.json", "manifest.json"):
            with contextlib.suppress(OSError, ValueError):
                doc = json.loads((d / name).read_text(encoding="utf-8"))
                break
        st = str(doc.get("state") or ("COMPLETED" if (d / "manifest.json").exists() else "UNKNOWN"))
        view = {"state": st, "attempt": 1, "restart_count": 0}
        return RunInfo(run_id, st, doc.get("tenant") or doc.get("project"), doc.get("submitted_by") or doc.get("owner"), False, view)


# ── 连接 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Sub:
    sid: int
    topic: str
    run_id: str
    channel: LabChannel
    rate: float
    last_sent: float = 0.0
    sent_seq: int = 0


class LabSession:
    def __init__(self, hub: LabHub, ws: WebSocket, principal: Principal, client: str | None) -> None:
        self.hub = hub
        self.ws = ws
        self.principal = principal
        self.role = principal.role
        self.client = client
        self.conn_id = "c-" + secrets.token_hex(6)
        self.hello = False
        self.closing = False
        self.close_code: int | None = None
        self.subs: dict[int, Sub] = {}
        self.known: set[int] = set()            # 已 advertise 给该连接的 channel id
        self.ctrl: deque[str | bytes] = deque()
        self.dirty: set[int] = set()            # 有新值待发的订阅 id
        self.snapshot: set[int] = set()         # 下一帧须置 SNAPSHOT 的订阅 id
        self.urgent: set[int] = set()           # 不受限频的待发订阅 id（状态迁移）
        self.wake = asyncio.Event()
        self.frame_seq = 0
        self.bytes_out = 0
        self.task: asyncio.Task | None = None

    @property
    def privileged(self) -> bool:
        return self.principal.at_least("evaluator")

    def send_ctrl(self, m: dict | str | bytes) -> None:
        if self.closing:
            return
        if len(self.ctrl) >= CTRL_MAX:
            self.shutdown(1008)
            return
        self.ctrl.append(m if isinstance(m, (str, bytes)) else jdump(m))
        self.wake.set()

    def error(self, code: int, op: str, rid: Any = None, message: str | None = None, detail: dict | None = None) -> None:
        m = error_msg(code, op, rid, message)
        if detail:
            m["detail"] = detail
        self.send_ctrl(m)

    def shutdown(self, code: int) -> None:
        if self.close_code is None:
            self.close_code = code
        self.closing = True
        self.wake.set()

    def notify(self, sids: Iterable[int], *, urgent: bool = False) -> None:
        sids = list(sids)
        self.dirty.update(sids)
        if urgent:
            self.urgent.update(sids)
        self.wake.set()

    def _batches(self, now: float) -> list[bytes]:
        """待发订阅 → 每个运行一帧 BATCH（受订阅速率限频；状态 topic 的状态迁移不限频）。"""
        due: dict[str, list[tuple[Sub, bool]]] = {}
        keep: set[int] = set()
        for sid in self.dirty:
            sub = self.subs.get(sid)
            if sub is None or sub.channel.seq == 0 or sub.channel.seq == sub.sent_seq:
                continue
            if sid not in self.snapshot and sid not in self.urgent and now - sub.last_sent < 1.0 / sub.rate:
                keep.add(sid)
                continue
            due.setdefault(sub.run_id, []).append((sub, sid in self.snapshot))
        self.dirty = keep
        out = []
        for subs in due.values():
            t_frame = max(s.channel.t_sim_ns for s, _ in subs)
            recs = []
            snap = False
            for s, is_snap in subs:
                recs.append(s.channel.record(t_frame, privileged=self.privileged))
                s.last_sent = now
                s.sent_seq = s.channel.seq
                snap = snap or is_snap
                self.snapshot.discard(s.sid)
                self.urgent.discard(s.sid)
            self.frame_seq = (self.frame_seq + 1) & 0xFFFFFFFF
            out.append(F.encode_batch(F.BATCH_SNAPSHOT if snap else 0, 0, self.frame_seq, t_frame, recs))
        return out

    async def sender(self) -> None:
        try:
            while not self.closing:
                while self.ctrl and not self.closing:
                    m = self.ctrl.popleft()
                    if isinstance(m, bytes):
                        await self.ws.send_bytes(m)
                    else:
                        await self.ws.send_text(m)
                    self.bytes_out += len(m)
                for b in self._batches(time.monotonic()) if self.dirty and not self.closing else ():
                    await self.ws.send_bytes(b)
                    self.bytes_out += len(b)
                self.wake.clear()
                if self.ctrl or self.closing:
                    continue
                pend = [self.subs[i] for i in self.dirty if i in self.subs]     # 限频中的订阅：到期后再发
                timeout = min(max(0.0, x.last_sent + 1.0 / x.rate - time.monotonic()) for x in pend) if pend else None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout)
        except Exception:
            self.closing = True
        if self.close_code is not None:
            with contextlib.suppress(Exception):
                if self.close_code >= 4000:     # 协议关闭（4401、4408、4426）：先送出关闭原因（error 帧），慢消费者（1008）不补发
                    for m in list(self.ctrl)[:8]:
                        if isinstance(m, str):
                            await self.ws.send_text(m)
                self.ctrl.clear()
                await self.ws.close(code=self.close_code)


# ── 网关核心 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

class LabHub:
    """channel 表、连接表与数据来源轮询（见模块说明）。`clock` 为 UNIX 秒（测试可注入）。"""

    def __init__(self, settings: BenchSettings, auth: BenchAuth, *, poll_s: float = POLL_S, status_period_s: float = STATUS_PERIOD_S,
                 token_check_s: float = TOKEN_CHECK_S, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.auth = auth
        self.runs = RunIndex(settings)
        self.poll_s = poll_s
        self.status_period_s = status_period_s
        self.token_check_s = token_check_s
        self.clock = clock
        self.session_id = "lab-" + secrets.token_hex(6)
        self.sessions: set[LabSession] = set()
        self.channels: dict[tuple[str, str], LabChannel] = {}
        self._next_id = 100
        self._free: list[int] = []                              # 已释放、可复用的 channel id
        self._live_seen: dict[Path, tuple[int, int]] = {}
        self._status_state: dict[str, tuple[str, int]] = {}      # rid → (state, attempt) 最近发布值
        self._status_last: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self.stopping = False

    # ---------------------------------------------------------------- channel
    def channel(self, run_id: str, topic: str) -> LabChannel:
        c = self.channels.get((run_id, topic))
        if c is None:
            if self._free:
                cid = self._free.pop()
            else:
                cid = self._next_id
                self._next_id += 1
                if cid > 0xFFFF:
                    raise OverflowError("channel id exhausted")
            c = LabChannel(cid, run_id, topic)
            self.channels[(run_id, topic)] = c
        return c

    def publish(self, run_id: str, topic: str, payload: bytes, t_sim_ns: int, *, urgent: bool = False) -> LabChannel:
        """发布一个最新值并通知订阅了该 channel 的连接（在事件循环中调用；进程内生产者与测试可直接调用）。`urgent`：不受订阅
        速率限频（状态迁移）。"""
        c = self.channel(run_id, topic)
        c.publish(payload, t_sim_ns)
        for s in self.sessions:
            sids = [sid for sid, sub in s.subs.items() if sub.channel is c]
            if sids:
                s.notify(sids, urgent=urgent)
        return c

    def bound_runs(self) -> set[str]:
        return {sub.run_id for s in self.sessions for sub in s.subs.values()}

    # ---------------------------------------------------------------- 轮询
    def poll_once(self, now: float | None = None) -> int:
        """读状态库与实时投递文件，发布有变化的值；返回本轮发布数（在事件循环中调用：只做目录扫描、小文件读取与一次索引查询）。"""
        now = time.monotonic() if now is None else now
        n = 0
        live_root = self.settings.data_root / LIVE_DIR
        bound = self.bound_runs()
        gone = [self.channels.pop(k).id for k in [k for k in self.channels if k[0] not in bound]]
        if gone:                                    # 无人订阅的运行：释放 channel（id 回收，连接侧的已通告记录同时清除）与轮询状态
            self._free.extend(gone)
            for s in self.sessions:
                s.known.difference_update(gone)
        for rid in [r for r in self._status_state if r not in bound]:
            self._status_state.pop(rid, None)
            self._status_last.pop(rid, None)
        self._live_seen = {p: v for p, v in self._live_seen.items() if p.parent.name in bound}
        for rid in sorted(bound):
            status_topic = f"bench/run/{rid}/status"
            counters: dict | None = None
            files: list[os.DirEntry] = []
            with contextlib.suppress(OSError):
                files = [e for e in os.scandir(live_root / rid) if e.name.endswith(".awlv") and e.is_file()]
            for e in files:
                topic = e.name[:-len(".awlv")].replace("~", "/")
                if topic == status_topic:
                    got = read_live(Path(e.path))
                    with contextlib.suppress(Exception):
                        c = msgpack.unpackb(got[1], raw=False) if got is not None else None
                        counters = c if isinstance(c, dict) else None
                    continue
                if (rid, topic) not in self.channels or T.match_lab_topic(topic) is None:
                    continue                    # 无人订阅：不读取（订阅后下一轮读取）
                p = Path(e.path)
                with contextlib.suppress(OSError):
                    st = e.stat()
                    key = (st.st_mtime_ns, st.st_size)
                    if self._live_seen.get(p) == key:
                        continue
                    got = read_live(p)
                    if got is not None:
                        self._live_seen[p] = key
                        self.publish(rid, topic, got[1], got[0])
                        n += 1
            if (rid, status_topic) not in self.channels:
                continue
            info = self.runs.lookup(rid)
            cur = (info.state, int(info.view.get("attempt") or 1)) if info is not None else ("UNKNOWN", 0)
            prev = self._status_state.get(rid)
            changed = prev is not None and prev != cur
            last = self._status_last.get(rid)
            if prev is None or changed or last is None or now - last >= self.status_period_s:
                body = status_payload(info.view if info is not None else None, counters)
                self.publish(rid, status_topic, msgpack.packb(body, use_bin_type=True), int(body["t_sim_ns"]), urgent=changed)
                self._status_last[rid] = now
                n += 1
            self._status_state[rid] = cur
        return n

    def check_tokens(self) -> None:
        """来源 bench token 撤销或到期：发 `error{302}` 并以 4401 关闭（每 `token_check_s` 一次，≤ 2 s 失效）。"""
        if not self.sessions:
            return
        now = self.clock()
        live = {t["id"] for t in self.auth.list_tokens()
                if not t.get("revoked") and (t.get("expires_unix") is None or now < t["expires_unix"])}
        for s in list(self.sessions):
            if s.principal.token_id not in live and not s.closing:
                s.error(int(Reason.TOKEN_INVALID), "hello", message="来源 bench token 已撤销或到期")
                s.shutdown(4401)

    async def run(self) -> None:
        t_tok = 0.0
        while not self.stopping:
            try:
                if self.sessions:
                    self.poll_once()
                    if time.monotonic() - t_tok >= self.token_check_s:
                        t_tok = time.monotonic()
                        self.check_tokens()
            except Exception:
                pass
            await asyncio.sleep(self.poll_s)

    def start(self) -> None:
        self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        self.stopping = True
        for s in list(self.sessions):
            s.shutdown(1001)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        self.runs.close()

    # ---------------------------------------------------------------- 连接表
    def conn_count(self, principal: str) -> tuple[int, int]:
        return len(self.sessions), sum(1 for s in self.sessions if s.principal.name == principal)

    def server_info(self, s: LabSession) -> dict:
        p = s.principal
        return {"op": "serverInfo", "name": "awr-lab-gateway", "protocol": PROTOCOL, "sessionId": self.session_id, "connId": s.conn_id,
                "capabilities": ["lab"], "tickHz": T.TICK_HZ, "rateClasses": list(T.RATE_CLASSES), "role": s.role, "principal": p.name,
                "contracts": CONTRACTS_VERSION, "lab": {"principal": p.name, "role": p.role, "projects": list(p.projects)},
                "limits": {"maxSubs": MAX_SUBS, "maxPreviewPerRun": MAX_PREVIEW_PER_RUN, "ctrlQueue": CTRL_MAX,
                           "maxTextBytes": MAX_TEXT_BYTES, "maxBinaryBytes": 0},
                "serverUnix_ns": str(time.time_ns())}

    def audit(self, action: str, **fields: Any) -> None:
        with contextlib.suppress(OSError):
            self.auth.audit.write(action, route="lab-rt", **fields)


# ── 订阅 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _deny_sub(s: LabSession, sid: Any, why: str, topic: str, run_id: str | None, message: str) -> None:
    s.error(int(Reason.ROLE_FORBIDDEN), "subscribe", sid, message, {"why": why, "topic": topic, "run_id": run_id, "role": s.role})
    p = s.principal
    s.hub.audit("deny", code=int(Reason.ROLE_FORBIDDEN), status=403, why=why, principal=p.name, role=p.role, via=p.via, token_id=p.token_id,
                conn_id=s.conn_id, topic=topic, run_id=run_id, client=s.client)


def _rate(spec: T.Topic, requested: Any) -> float:
    """订阅速率（Hz）：缺省取 topic 缺省档，上限 `RATE_CAP`。"""
    cap = RATE_CAP.get(spec.pattern, 1.0)
    if isinstance(requested, (int, float)) and not isinstance(requested, bool) and requested > 0:
        return float(min(cap, requested))
    return float(min(cap, (spec.default_rate or {}).get("S", 1)))


def _subscribe(s: LabSession, subs: list) -> None:
    hub = s.hub
    fresh: list[LabChannel] = []
    acks: list[dict] = []
    for item in subs:
        if not isinstance(item, dict):
            s.error(int(Reason.BAD_REQUEST), "subscribe", None, "subscribe 项必须是对象")
            continue
        sid, topic = item.get("id"), item.get("topic")
        if not isinstance(sid, int) or isinstance(sid, bool) or not isinstance(topic, str):
            s.error(int(Reason.BAD_REQUEST), "subscribe", sid if isinstance(sid, (int, str)) else None, "subscribe 项缺少 id 或 topic")
            continue
        spec = T.match_lab_topic(topic) if T.valid_topic(topic) else None
        if spec is None:
            s.error(int(Reason.TOPIC_INVALID), "subscribe", sid, f"不是 lab-gateway topic 或含通配：{topic[:128]}")
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        rid = params.get("run_id")
        if spec.pattern == STATUS_TOPIC:
            in_topic = topic.split("/")[2]
            if rid is not None and rid != in_topic:
                s.error(int(Reason.BAD_REQUEST), "subscribe", sid, "params.run_id 与 topic 中的运行不一致")
                continue
            rid = in_topic
        if not isinstance(rid, str) or not RUN_ID.match(rid):
            s.error(int(Reason.BAD_REQUEST), "subscribe", sid, "缺少或非法的 params.run_id")
            continue
        need = ROLE_FOR_SCOPE.get(T.LAB_SCOPES.get(spec.pattern, "lab_evaluator"), "evaluator")
        if not s.principal.at_least(need):
            _deny_sub(s, sid, "role", topic, rid, f"订阅 {spec.pattern} 需要 {need} 及以上角色")
            continue
        info = hub.runs.lookup(rid)
        if info is None:
            s.error(int(Reason.NOT_FOUND), "subscribe", sid, f"运行不存在：{rid}")
            continue
        p = s.principal
        if not project_ok(p, info.tenant, info.owner):
            _deny_sub(s, sid, "project_scope", topic, rid, "研究者只能订阅本人与所属项目的运行")
            continue
        if spec.pattern in OWNER_ONLY and not p.at_least("evaluator") and info.owner != p.name:
            _deny_sub(s, sid, "owner_scope", topic, rid, "研究者只能订阅本人运行的预览与 LiDAR")
            continue
        old = s.subs.get(sid)
        if old is None and len(s.subs) >= MAX_SUBS:
            s.error(int(Reason.SUB_LIMIT), "subscribe", sid, f"订阅数超过上限 {MAX_SUBS}")
            continue
        if spec.pattern == "uav/{id}/sensor/{name}/preview" and (old is None or old.topic != topic or old.run_id != rid):
            n = sum(1 for x in s.subs.values() if x.run_id == rid and x.channel.pattern == spec.pattern and x.sid != sid)
            if n >= MAX_PREVIEW_PER_RUN:
                s.error(int(Reason.SUB_LIMIT), "subscribe", sid, f"每个运行的预览订阅不超过 {MAX_PREVIEW_PER_RUN} 个")
                continue
        c = hub.channel(rid, topic)
        rate = _rate(spec, item.get("rate"))
        s.subs[sid] = Sub(sid, topic, rid, c, rate)
        if c.id not in s.known:
            s.known.add(c.id)
            fresh.append(c)
        acks.append({"op": "subscribed", "id": sid, "topic": topic, "run_id": rid, "channels": [c.id], "rate": rate, "mode": "latest"})
        if c.seq:
            s.snapshot.add(sid)
            s.dirty.add(sid)
    if fresh:
        s.send_ctrl({"op": "advertise", "channels": [c.advert() for c in fresh]})
    for a in acks:
        s.send_ctrl(a)


def _unsubscribe(s: LabSession, ids: Any) -> None:
    gone = [i for i in (ids if isinstance(ids, list) else []) if isinstance(i, int) and s.subs.pop(i, None) is not None]
    for i in gone:
        s.dirty.discard(i)
        s.snapshot.discard(i)
    s.send_ctrl({"op": "unsubscribed", "ids": gone})


# ── WS 端点 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def _client(ws: WebSocket) -> str | None:
    fwd = ws.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return ws.client.host if ws.client is not None else None


async def _deny_upgrade(ws: WebSocket, code: int, status: int, headers: dict[str, str] | None = None) -> None:
    resp = JSONResponse(problem_body(code, status=status), status_code=status, headers=headers or {}, media_type=PROBLEM_MEDIA)
    try:
        await ws.send_denial_response(resp)
    except Exception:
        with contextlib.suppress(Exception):
            await ws.close(code=1008)


async def _refuse(ws: WebSocket, code: int, close: int, message: str) -> None:
    with contextlib.suppress(Exception):
        await ws.send_text(jdump(error_msg(code, "hello", message=message)))
    with contextlib.suppress(Exception):
        await ws.close(code=close)


async def lab_rt_endpoint(ws: WebSocket) -> None:
    hub: LabHub = ws.app.state.hub
    st: BenchSettings = ws.app.state.settings
    client = _client(ws)
    origin = ws.headers.get("origin")
    if origin is not None and origin not in st.allowed_origins:
        hub.audit("deny", code=int(Reason.ORIGIN_FORBIDDEN), status=403, why="origin", origin=origin[:128], client=client)
        await _deny_upgrade(ws, int(Reason.ORIGIN_FORBIDDEN), 403)
        return
    protos = list(ws.scope.get("subprotocols") or [])
    if PROTOCOL not in protos:
        await _deny_upgrade(ws, int(Reason.PROTOCOL_UNSUPPORTED), 400, {"AWR-Supported-Protocols": PROTOCOL})
        return
    token = next((p[len("bearer."):] for p in protos if p.startswith("bearer.")), None)
    await ws.accept(subprotocol=PROTOCOL)
    if hub.stopping:
        await ws.close(code=1001)
        return
    if not token:
        hub.audit("deny", code=int(Reason.AUTH_REQUIRED), status=401, why="token", client=client)
        await _refuse(ws, int(Reason.AUTH_REQUIRED), 4401, "缺少令牌（子协议 bearer.<短期令牌>）")
        return
    try:
        principal = hub.auth.verify_short(token, AUD)
    except AuthError as e:
        hub.audit("deny", code=e.code, status=403 if e.code == 115 else 401, why="invited_scope" if e.code == 115 else "token", client=client)
        if e.code == 115:
            await _refuse(ws, int(Reason.ROLE_FORBIDDEN), 4403, "受邀成员不能访问实时 topic")
        else:
            await _refuse(ws, int(Reason.TOKEN_INVALID), 4401, "令牌无效、过期或已撤销")
        return
    except Exception:
        hub.audit("deny", code=int(Reason.TOKEN_INVALID), status=401, why="token", client=client)
        await _refuse(ws, int(Reason.TOKEN_INVALID), 4401, "令牌无效、过期或已撤销")
        return
    total, mine = hub.conn_count(principal.name)
    if total >= MAX_CONNS or mine >= MAX_CONNS_PER_PRINCIPAL:
        await _refuse(ws, int(Reason.CONN_LIMIT), 4429, "连接数超过上限")
        return
    s = LabSession(hub, ws, principal, client)
    hub.sessions.add(s)
    hub.audit("lab_rt_open", principal=principal.name, role=principal.role, token_id=principal.token_id, conn_id=s.conn_id, client=client)
    s.task = asyncio.ensure_future(s.sender())
    s.send_ctrl(hub.server_info(s))
    s.send_ctrl({"op": "advertise", "channels": []})
    s.send_ctrl(F.encode_time(int(TimeState.STOPPED), 0, 0.0, 0, time.monotonic_ns()))
    t_hello = time.monotonic() + HELLO_TIMEOUT_S
    try:
        while not s.closing:
            timeout = None if s.hello else max(0.0, t_hello - time.monotonic())
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout)
            except TimeoutError:
                s.error(int(Reason.HELLO_TIMEOUT), "hello", message="10 s 内未收到 hello")
                s.shutdown(4408)
                break
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                s.error(int(Reason.BAD_REQUEST), "clientData", message="lab-gateway 不接受客户端二进制帧")
                continue
            _on_text(s, msg.get("text") or "")
    except Exception:
        pass
    finally:
        if not s.closing:
            s.closing = True
            s.wake.set()
        if s.task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(s.task, 2.0)
        hub.sessions.discard(s)
        hub.audit("lab_rt_close", principal=principal.name, role=principal.role, token_id=principal.token_id, conn_id=s.conn_id,
                  bytes_out=s.bytes_out, close_code=s.close_code)


def _on_text(s: LabSession, text: str) -> None:
    try:
        m = json.loads(text)
        op = m["op"]
        if not isinstance(op, str):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        s.error(int(Reason.BAD_REQUEST), "unknown", message="控制消息必须是含 op 的 JSON 对象")
        return
    if not s.hello:
        if op != "hello":
            s.error(int(Reason.BAD_REQUEST), op[:64], message="hello 之前只接受 hello")
            return
        major = str(m.get("contracts", "")).split(".")[0]
        if major != CONTRACTS_VERSION.split(".")[0]:
            s.error(int(Reason.PROTOCOL_UNSUPPORTED), "hello", message=f"contracts 主版本不同：{str(m.get('contracts'))[:32]}")
            s.shutdown(4426)
            return
        s.hello = True
        return
    if op == "subscribe":
        subs = m.get("subs")
        if not isinstance(subs, list):
            s.error(int(Reason.BAD_REQUEST), op, message="subs 必须是数组")
        else:
            _subscribe(s, subs)
    elif op == "unsubscribe":
        _unsubscribe(s, m.get("ids"))
    elif op == "ping":
        t = m.get("t")
        s.send_ctrl({"op": "pong", "t": t if isinstance(t, (int, float)) and not isinstance(t, bool) else 0,
                     "server_ns": time.monotonic_ns(), "unix_ns": str(time.time_ns())})
    elif op in ("hello", "ack", "clientStats", "presence"):
        pass
    else:
        s.error(int(Reason.UNKNOWN_OP), op[:64], message=f"lab-gateway 不支持的 op：{op[:64]}")


# ── 应用与入口 ────────────────────────────────────────────────────────────────────────────────────────────────────────

def create_gateway_app(settings: BenchSettings, *, auth: BenchAuth | None = None, **hub_kw: Any) -> FastAPI:
    auth = auth or BenchAuth(settings.keys_dir, settings.db_path, session_ttl_s=settings.session_ttl_s, short_ttl_s=settings.short_ttl_s)
    hub = LabHub(settings, auth, **hub_kw)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(title="drone4d lab-gateway", version="1", openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.hub = hub
    app.add_api_websocket_route(WS_PATH, lab_rt_endpoint)

    @app.get(f"{WS_PATH}/healthz")
    def healthz() -> dict:
        """部署探针（只回连接数与绑定运行数，不含 principal 与运行 id）。"""
        return {"ok": True, "protocol": PROTOCOL, "connections": len(hub.sessions), "runs": len(hub.bound_runs())}

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m awr.bench.api.gateway")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int, default=GATEWAY_PORT)
    ns = ap.parse_args(argv)
    s = BenchSettings.from_env()
    uvicorn.run(create_gateway_app(s), host=ns.host or s.bind, port=ns.port, log_level="info", ws_max_size=MAX_TEXT_BYTES,
                ws_per_message_deflate=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
