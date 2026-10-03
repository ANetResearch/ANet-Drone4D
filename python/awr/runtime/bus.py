"""控制平面与事件平面的总线：Bus 接口、ZenohBus、LocalBus（AWR-17 §9.3–§9.8；ADR-018；M11-FR-006、FR-007）。

全仓库唯一 `import zenoh` 的模块（lint：tools/lint/check_py_imports.py）。key 一律来自生成的
`awr.contracts.bus_keys`（相对 namespace `awr/<world>/<run>`），本文件不写 key 字面量；每个 key 的优先级与 express
取自契约 `packages/contracts/bus/keys.json`。

约定（g05 §3.3、§10 R4–R5）：
1. zenoh 回调线程里只做入队：`serve(key, handler)` 的同步 handler 只允许 `put`、`call_soon_threadsafe`、`set_result`
   或 dict 赋值；协程 handler 由包装器经 `call_soon_threadsafe` 投递到事件循环执行，结束后若未回复则自动 drop。
2. 每个 Query 必须被 drop，否则查询方要等到超时：`Request.reply()` 在 finally 中 drop；未回复的请求 5 s 后由清扫器 drop。
3. 所有发送（publisher、query）一律 `CongestionControl.DROP`；可靠性由事件 seq + `_replay`、命令 cid 幂等 + 重试、
   状态取最新值保证（P-09）。
4. `call` 默认 1 s 超时、间隔 0.3 s、同一请求体（同 cid）重试 2 次；无回复时抛 BusTimeout（211）。
5. 端点只允许回环地址（7447 在任何模式下都不出现在非回环地址上，OPS-NFR-006）。

LocalBus 与 ZenohBus 同接口、同语义（超时、drop、liveliness 模拟），用于单元测试、`--inproc` 与 ≤ 50 架降级演示，
不参与性能验收（17 §9.8）。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgpack
import zenoh

from awr.contracts import bus_keys
from awr.contracts._paths import schema_path

if TYPE_CHECKING:
    from .child import RunCtx

__all__ = [
    "Bus",
    "BusClosed",
    "BusError",
    "BusReplyError",
    "BusTimeout",
    "Handle",
    "LocalBus",
    "LocalNet",
    "Prio",
    "Publisher",
    "QoS",
    "Request",
    "ZenohBus",
    "key_matches",
    "open_bus",
    "pack",
    "qos_for",
    "unpack",
]

log = logging.getLogger("awr.runtime.bus")

REQUEST_TTL_S = 5.0  # 未回复的请求在此之后由清扫器 drop（查询方 1 s 已超时）
_TEMPLATE = Path(__file__).with_name("zenoh.json5")
_LOOPBACK_PREFIXES = ("tcp/127.0.0.1:", "tcp/[::1]:", "tcp/localhost:")


class Prio(IntEnum):
    """zenoh 优先级（数值越小越高）。"""

    REAL_TIME = 1
    INTERACTIVE_HIGH = 2
    INTERACTIVE_LOW = 3
    DATA_HIGH = 4
    DATA = 5
    DATA_LOW = 6
    BACKGROUND = 7


class BusError(Exception):
    code = 213  # SERVICE_UNAVAILABLE


class BusTimeout(BusError):
    """查询无回复（无服务方、服务方未回复或超时），重试后仍失败（原因码 211 SIM_UNAVAILABLE）。"""

    code = 211


class BusReplyError(BusError):
    """服务方以错误回复（reply_err）。"""

    code = 213


class BusClosed(BusError):
    code = 213


class _NoReply(Exception):
    """单次查询结束而没有回复（内部信号，触发重试）。"""


def pack(msg: Any) -> bytes:
    return msgpack.packb(msg, use_bin_type=True)


def unpack(raw: bytes | memoryview) -> Any:
    return msgpack.unpackb(raw, raw=False, strict_map_key=False)


# ---------------------------------------------------------------- key 表达式与 QoS
def key_matches(pattern: str, key: str) -> bool:
    """zenoh key 表达式子集：`*` 匹配一个 chunk，`**` 匹配零个或多个 chunk。"""
    p, k = pattern.split("/"), key.split("/")
    return _km(p, k, 0, 0, {})


def _km(p: list[str], k: list[str], i: int, j: int, memo: dict[tuple[int, int], bool]) -> bool:
    """`key_matches` 的递归体（模块级函数而不是闭包：递归闭包经 cell 引用自身，每次调用留下一个引用环，只能由 gc gen2
    回收；进程内总线每秒数百次匹配即数千个环对象，ADR-073 第 2 条）。"""
    r = memo.get((i, j))
    if r is not None:
        return r
    if i == len(p):
        r = j == len(k)
    elif p[i] == "**":
        r = _km(p, k, i + 1, j, memo) or (j < len(k) and _km(p, k, i, j + 1, memo))
    elif j < len(k) and (p[i] == "*" or p[i] == k[j]):
        r = _km(p, k, i + 1, j + 1, memo)
    else:
        r = False
    memo[(i, j)] = r
    return r


@dataclass(frozen=True)
class QoS:
    priority: Prio
    express: bool


_DEFAULT_QOS = QoS(Prio.INTERACTIVE_LOW, False)
_HIGH_QOS = QoS(Prio.INTERACTIVE_HIGH, False)
_qos_table: list[tuple[re.Pattern[str], QoS]] | None = None
_qos_lock = threading.Lock()


def _load_qos() -> list[tuple[re.Pattern[str], QoS]]:
    try:
        keys = json.loads(schema_path("bus/keys.json").read_text(encoding="utf-8"))["keys"]
    except (OSError, ValueError, KeyError):
        log.warning("bus keys.json unreadable; default QoS used")
        return []
    table: list[tuple[re.Pattern[str], QoS]] = []
    for k in keys:
        q = k.get("qos")
        if not q:
            continue
        rx = re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(k["key"])) + "$")
        table.append((rx, QoS(Prio[q["priority"]], bool(q.get("express", False)))))
    return table


def qos_for(key: str) -> QoS:
    """按契约 keys.json 查找 key 的优先级与 express；`evt/<producer>/safety` 为 INTERACTIVE_HIGH（17 §9.3）。"""
    global _qos_table
    if _qos_table is None:
        with _qos_lock:
            if _qos_table is None:
                _qos_table = _load_qos()
    parts = key.split("/")
    if len(parts) == 3 and parts[0] == "evt" and parts[2] == "safety":
        return _HIGH_QOS
    for rx, q in _qos_table:
        if rx.match(key):
            return q
    return _DEFAULT_QOS


# ---------------------------------------------------------------- 句柄、请求、发布者
class Handle:
    """声明的实体（queryable、subscriber、token、watch）的句柄；close() 幂等。"""

    def __init__(self, closer: Callable[[], None], what: str = "") -> None:
        self._closer: Callable[[], None] | None = closer
        self.what = what

    def close(self) -> None:
        c, self._closer = self._closer, None
        if c is not None:
            try:
                c()
            except Exception:  # 会话已关闭时 undeclare 可能失败
                log.debug("handle close failed", exc_info=True)

    def __enter__(self) -> Handle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Request:
    """queryable 收到的请求；`reply()` 只生效一次并在 finally 中 drop；`close()` 为不回复直接 drop。"""

    __slots__ = ("_bus", "_lock", "done", "key", "payload", "t_rx")

    def __init__(self, bus: Bus, key: str, payload: bytes) -> None:
        self._bus = bus
        self._lock = threading.Lock()
        self.key = key
        self.payload = payload
        self.done = False
        self.t_rx = time.monotonic()

    def msg(self) -> Any:
        return unpack(self.payload) if self.payload else {}

    def reply(self, payload: bytes) -> bool:
        with self._lock:
            if self.done:
                return False
            self.done = True
        try:
            self._send(bytes(payload))
        except Exception:
            log.warning("bus reply failed", extra={"kv": {"key": self.key}}, exc_info=True)
        finally:
            self._drop()
            self._bus._untrack(self)
        return True

    def reply_msg(self, obj: Any) -> bool:
        return self.reply(pack(obj))

    def close(self) -> None:
        with self._lock:
            if self.done:
                return
            self.done = True
        try:
            self._drop()
        finally:
            self._bus._untrack(self)

    def _send(self, payload: bytes) -> None:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def _drop(self) -> None:  # pragma: no cover
        raise NotImplementedError


class Publisher:
    """发布者；拥塞控制恒为 DROP（生产者永不阻塞）。"""

    congestion = "DROP"

    def __init__(self, key: str, priority: Prio, express: bool) -> None:
        self.key = key
        self.priority = priority
        self.express = express
        self.puts = 0

    def put(self, payload: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        pass


def _resolve(fut: asyncio.Future, raw: bytes | None, err: BaseException | None) -> None:
    if fut.done():
        return
    if err is not None:
        fut.set_exception(err)
    else:
        fut.set_result(raw)


# ---------------------------------------------------------------- Bus 接口
class Bus(ABC):
    """进程间总线接口；每个进程一个实例。"""

    kind = "abstract"

    def __init__(self, name: str, namespace: str, loop: asyncio.AbstractEventLoop | None) -> None:
        self.name = name
        self.namespace = namespace
        self.loop = loop
        self._pending: dict[int, Request] = {}
        self._pending_lock = threading.Lock()
        self._closed = False
        self._handles: list[Handle] = []
        self._bg: set[asyncio.Future] = set()
        self.stats = {"calls": 0, "call_retries": 0, "call_timeouts": 0, "queries_served": 0, "requests_expired": 0,
                      "puts": 0}

    # ------------------------------------------------------------ 工厂
    @classmethod
    @abstractmethod
    def open(cls, name: str, ctx: RunCtx | None = None, *, loop: asyncio.AbstractEventLoop | None = None,
             **kw: Any) -> Bus: ...

    # ------------------------------------------------------------ 原语（子类实现）
    @abstractmethod
    def _declare_queryable(self, key: str, on_request: Callable[[Request], None]) -> Handle: ...

    @abstractmethod
    def _start_query(self, key: str, payload: bytes, timeout: float, cb: Callable[[bytes | None, BaseException | None], None]) -> None: ...

    @abstractmethod
    def publisher(self, key: str, *, priority: Prio | None = None, express: bool | None = None) -> Publisher: ...

    @abstractmethod
    def subscribe(self, key: str, cb: Callable[[str, bytes], None]) -> Handle: ...

    @abstractmethod
    def token(self, key: str) -> Handle: ...

    @abstractmethod
    def watch(self, pattern: str, on_change: Callable[[str, bool], None]) -> Handle: ...

    @abstractmethod
    def alive(self, pattern: str, *, timeout: float = 0.5) -> list[str]: ...

    @abstractmethod
    def _close_transport(self) -> None: ...

    # ------------------------------------------------------------ 服务
    def serve(self, key: str, handler: Callable[[Request], Any]) -> Handle:
        """声明 queryable。同步 handler 在回调线程执行，只允许入队；协程 handler 投递到 open(loop=...) 的事件循环。"""
        if inspect.iscoroutinefunction(handler):
            loop = self.loop
            if loop is None:
                raise ValueError("协程 handler 需要 Bus.open(..., loop=事件循环)")

            def on_request(req: Request) -> None:
                loop.call_soon_threadsafe(self._spawn, handler, req)
        else:
            on_request = handler
        h = self._declare_queryable(key, on_request)
        self._handles.append(h)
        return h

    def _spawn(self, handler: Callable[[Request], Any], req: Request) -> None:
        async def run() -> None:
            try:
                await handler(req)
            except Exception:
                log.exception("bus handler failed", extra={"kv": {"key": req.key}})
            finally:
                if not req.done:
                    req.close()

        t = asyncio.ensure_future(run())
        self._bg.add(t)  # 持有引用直到完成
        t.add_done_callback(self._bg.discard)

    def _track(self, req: Request) -> None:
        now = req.t_rx
        with self._pending_lock:
            self._pending[id(req)] = req
            expired = [r for r in self._pending.values() if now - r.t_rx > REQUEST_TTL_S]
        for r in expired:
            self.stats["requests_expired"] += 1
            r.close()
        self.stats["queries_served"] += 1

    def _untrack(self, req: Request) -> None:
        with self._pending_lock:
            self._pending.pop(id(req), None)

    def pending_requests(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    # ------------------------------------------------------------ 调用
    async def call(self, key: str, msg: Any, *, timeout: float = 1.0, retries: int = 2, retry_gap: float = 0.3) -> Any:
        """异步查询：同一请求体（同 cid）最多重试 retries 次；无回复抛 BusTimeout，错误回复抛 BusReplyError。"""
        if self._closed:
            raise BusClosed(f"总线已关闭：{key}")
        loop = asyncio.get_running_loop()
        payload = pack(msg)
        self.stats["calls"] += 1
        for attempt in range(retries + 1):
            fut: asyncio.Future = loop.create_future()

            def cb(raw: bytes | None, err: BaseException | None, fut: asyncio.Future = fut) -> None:
                loop.call_soon_threadsafe(_resolve, fut, raw, err)

            try:
                self._start_query(key, payload, timeout, cb)
                raw = await asyncio.wait_for(fut, timeout + 0.25)
            except (_NoReply, TimeoutError):
                if attempt < retries:
                    self.stats["call_retries"] += 1
                    await asyncio.sleep(retry_gap)
                    continue
                self.stats["call_timeouts"] += 1
                raise BusTimeout(f"{key}：{retries + 1} 次查询均无回复（每次 {timeout} s）") from None
            return unpack(raw)
        raise AssertionError("unreachable")

    def call_cb(self, key: str, msg: Any, on_reply: Callable[[Any, BaseException | None], None], *, timeout: float = 1.0,
                retries: int = 2, retry_gap: float = 0.3) -> None:
        """同步进程使用的回调式查询；on_reply(reply, err) 在回调线程或定时器线程调用一次，只允许入队。"""
        payload = pack(msg)
        attempt = [0]
        self.stats["calls"] += 1

        def fire() -> None:
            if self._closed:
                on_reply(None, BusClosed(f"总线已关闭：{key}"))
                return
            try:
                self._start_query(key, payload, timeout, handle)
            except Exception as e:  # 会话关闭等
                on_reply(None, BusError(f"{key}：{e}"))

        def handle(raw: bytes | None, err: BaseException | None) -> None:
            if err is None:
                try:
                    obj = unpack(raw or b"")
                except Exception as e:
                    on_reply(None, BusError(f"{key}：回复无法解码：{e}"))
                    return
                on_reply(obj, None)
                return
            if isinstance(err, _NoReply):
                if attempt[0] < retries:
                    attempt[0] += 1
                    self.stats["call_retries"] += 1
                    t = threading.Timer(retry_gap, fire)
                    t.daemon = True
                    t.start()
                    return
                self.stats["call_timeouts"] += 1
                on_reply(None, BusTimeout(f"{key}：{retries + 1} 次查询均无回复（每次 {timeout} s）"))
                return
            on_reply(None, err)

        fire()

    # ------------------------------------------------------------ 在线状态
    def ready(self) -> Handle:
        """声明 `proc/<name>/ready`（就绪信号，supervisor 据此置 RUNNING）。"""
        return self.token(bus_keys.proc_ready(self.name))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._pending_lock:
            reqs = list(self._pending.values())
        for r in reqs:
            r.close()
        for h in reversed(self._handles):
            h.close()
        self._handles.clear()
        self._close_transport()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> Bus:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _once(cb: Callable[[bytes | None, BaseException | None], None]) -> Callable[[bytes | None, BaseException | None], None]:
    lock = threading.Lock()
    fired = [False]

    def f(raw: bytes | None, err: BaseException | None) -> None:
        with lock:
            if fired[0]:
                return
            fired[0] = True
        cb(raw, err)

    return f


def _check_endpoints(eps: list[str], what: str) -> None:
    for ep in eps:
        if not ep.startswith(_LOOPBACK_PREFIXES):
            raise ValueError(f"zenoh {what} 端点只允许回环地址：{ep}")


def _current_loop(loop: asyncio.AbstractEventLoop | None) -> asyncio.AbstractEventLoop | None:
    if loop is not None:
        return loop
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# ---------------------------------------------------------------- ZenohBus
class _ZRequest(Request):
    __slots__ = ("_q",)

    def __init__(self, bus: Bus, q: zenoh.Query, key: str) -> None:
        p = q.payload
        super().__init__(bus, key, p.to_bytes() if p is not None else b"")
        self._q = q

    def _send(self, payload: bytes) -> None:
        self._q.reply(self.key, payload)

    def _drop(self) -> None:
        with contextlib.suppress(Exception):
            self._q.drop()


class _ZPublisher(Publisher):
    def __init__(self, key: str, priority: Prio, express: bool, pub: zenoh.Publisher, bus: Bus) -> None:
        super().__init__(key, priority, express)
        self._pub = pub
        self._bus = bus

    def put(self, payload: bytes) -> None:
        self._pub.put(payload)
        self.puts += 1
        self._bus.stats["puts"] += 1

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._pub.undeclare()


def _zprio(p: Prio) -> zenoh.Priority:
    return getattr(zenoh.Priority, p.name)


# 查询不做回复合并：回复到达即交付，不等 ResponseFinal（见 ZenohBus._start_query）
_NO_CONSOLIDATION = zenoh.QueryConsolidation(zenoh.ConsolidationMode.NONE)


class ZenohBus(Bus):
    """zenoh 1.10.1 peer 会话：只在回环上监听；SHM 关闭；全部发送 DROP（17 §9.3）。"""

    kind = "zenoh"

    def __init__(self, name: str, session: zenoh.Session, namespace: str, loop: asyncio.AbstractEventLoop | None) -> None:
        super().__init__(name, namespace, loop)
        self._session = session
        self._pubs: dict[tuple[str, Prio, bool], _ZPublisher] = {}

    @classmethod
    def build_config(cls, *, template: str | None = None, namespace: str | None = None, listen: list[str] | None = None,
                     connect: list[str] | None = None, lease_ms: int | None = None) -> zenoh.Config:
        text = template if template is not None else _TEMPLATE.read_text(encoding="utf-8")
        cfg = zenoh.Config.from_json5(text)
        if namespace is not None:
            cfg.insert_json5("namespace", json.dumps(namespace))
        if listen is not None:
            _check_endpoints(listen, "listen")
            cfg.insert_json5("listen/endpoints", json.dumps(listen))
        if connect is not None:
            _check_endpoints(connect, "connect")
            cfg.insert_json5("connect/endpoints", json.dumps(connect))
        if lease_ms is not None:
            cfg.insert_json5("transport/link/tx/lease", str(int(lease_ms)))
        cfg.insert_json5("transport/shared_memory/enabled", "false")
        cfg.insert_json5("scouting/multicast/enabled", "false")
        return cfg

    @classmethod
    def open(cls, name: str, ctx: RunCtx | None = None, *, loop: asyncio.AbstractEventLoop | None = None,
             namespace: str | None = None, listen: list[str] | None = None, connect: list[str] | None = None,
             lease_ms: int | None = None, announce: bool = True, **_: Any) -> ZenohBus:
        """打开会话。ctx 给出时读取 supervisor 渲染的 `ctx.zenoh_config`（含 namespace 与汇合点）；参数可覆盖。"""
        template = None
        if ctx is not None and ctx.zenoh_config is not None and Path(ctx.zenoh_config).exists():
            template = Path(ctx.zenoh_config).read_text(encoding="utf-8")
        if namespace is None and ctx is not None:
            namespace = ctx.namespace
        cfg = cls.build_config(template=template, namespace=namespace, listen=listen, connect=connect, lease_ms=lease_ms)
        session = zenoh.open(cfg)
        ns = namespace or ""
        bus = cls(name, session, ns, _current_loop(loop))
        if announce:
            bus.token(bus_keys.proc_alive(name))
        return bus

    # ------------------------------------------------------------ queryable
    def _declare_queryable(self, key: str, on_request: Callable[[Request], None]) -> Handle:
        def on_query(q: zenoh.Query) -> None:  # zenoh 回调线程
            req = _ZRequest(self, q, key)
            self._track(req)
            try:
                on_request(req)
            except Exception:
                log.exception("bus request dispatch failed", extra={"kv": {"key": key}})
                req.close()

        qa = self._session.declare_queryable(key, on_query, complete=True)
        return Handle(qa.undeclare, f"queryable {key}")

    def _start_query(self, key: str, payload: bytes, timeout: float, cb: Callable[[bytes | None, BaseException | None], None]) -> None:
        once = _once(cb)

        def on_reply(r: zenoh.Reply) -> None:
            ok = r.ok
            if ok is not None:
                once(ok.payload.to_bytes(), None)
            else:
                err = r.err
                text = err.payload.to_bytes().decode("utf-8", "replace") if err is not None else ""
                if text.strip() == "Timeout":  # zenoh 以错误回复表示查询超时：按"无回复"处理（可重试）
                    once(None, _NoReply(key))
                else:
                    once(None, BusReplyError(f"{key}：{text}"))

        def on_drop() -> None:
            once(None, _NoReply(key))

        q = qos_for(key)
        # consolidation NONE（FX2-R2-gateway）：zenoh 缺省的 AUTO/LATEST 合并把回复扣到查询结束（queryable 的 ResponseFinal）
        # 才交付；ResponseFinal 迟到或在拥塞时丢失，回复就要等到查询超时（seek 实测一次 2003 ms，worker 只用了 41 ms）。
        # 每个 key 只有一个 queryable，`call` 取第一条回复，不需要合并；`on_drop` 仍在查询结束时触发（无回复检测不变）。
        self._session.get(key, zenoh.handlers.Callback(on_reply, on_drop), payload=payload, timeout=timeout,
                          consolidation=_NO_CONSOLIDATION, congestion_control=zenoh.CongestionControl.DROP,
                          priority=_zprio(q.priority), express=q.express)

    # ------------------------------------------------------------ pub/sub 与 liveliness
    def publisher(self, key: str, *, priority: Prio | None = None, express: bool | None = None) -> Publisher:
        q = qos_for(key)
        prio = q.priority if priority is None else priority
        exp = q.express if express is None else express
        k = (key, prio, exp)
        p = self._pubs.get(k)
        if p is None:
            zp = self._session.declare_publisher(key, congestion_control=zenoh.CongestionControl.DROP,
                                                 priority=_zprio(prio), express=exp)
            p = _ZPublisher(key, prio, exp, zp, self)
            self._pubs[k] = p
        return p

    def subscribe(self, key: str, cb: Callable[[str, bytes], None]) -> Handle:
        def on_sample(s: zenoh.Sample) -> None:  # zenoh 回调线程；cb 只允许入队
            cb(str(s.key_expr), s.payload.to_bytes())

        sub = self._session.declare_subscriber(key, on_sample)
        h = Handle(sub.undeclare, f"subscriber {key}")
        self._handles.append(h)
        return h

    def token(self, key: str) -> Handle:
        tok = self._session.liveliness().declare_token(key)
        h = Handle(tok.undeclare, f"token {key}")
        self._handles.append(h)  # 保持引用：zenoh 对象被回收即撤销，token 必须活到显式 close 或会话关闭
        return h

    def watch(self, pattern: str, on_change: Callable[[str, bool], None]) -> Handle:
        def on_sample(s: zenoh.Sample) -> None:
            on_change(str(s.key_expr), s.kind == zenoh.SampleKind.PUT)

        sub = self._session.liveliness().declare_subscriber(pattern, on_sample, history=True)
        h = Handle(sub.undeclare, f"watch {pattern}")
        self._handles.append(h)
        return h

    def alive(self, pattern: str, *, timeout: float = 0.5) -> list[str]:
        out: list[str] = []
        replies = self._session.liveliness().get(pattern, timeout=timeout)
        for r in replies:
            ok = r.ok
            if ok is not None:
                out.append(str(ok.key_expr))
        return sorted(set(out))

    def _close_transport(self) -> None:
        for p in self._pubs.values():
            p.close()
        self._pubs.clear()
        with contextlib.suppress(Exception):
            self._session.close()


# ---------------------------------------------------------------- LocalBus
class LocalNet:
    """进程内"网络"：按 namespace 隔离的服务表、订阅表与 liveliness 表。"""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.lock = threading.RLock()
        self.servers: dict[str, tuple[int, Callable[[Request], None], Bus]] = {}
        self.subs: dict[int, tuple[str, Callable[[str, bytes], None]]] = {}
        self.tokens: dict[str, int] = {}
        self.watchers: dict[int, tuple[str, Callable[[str, bool], None]]] = {}
        self._ids = itertools.count(1)

    def next_id(self) -> int:
        return next(self._ids)

    def publish(self, key: str, payload: bytes) -> int:
        with self.lock:
            targets = [cb for pat, cb in self.subs.values() if key_matches(pat, key)]
        for cb in targets:
            try:
                cb(key, payload)
            except Exception:
                log.exception("local subscriber failed", extra={"kv": {"key": key}})
        return len(targets)

    def add_token(self, key: str) -> None:
        with self.lock:
            n = self.tokens.get(key, 0)
            self.tokens[key] = n + 1
            ws = [cb for pat, cb in self.watchers.values() if key_matches(pat, key)] if n == 0 else []
        for cb in ws:
            cb(key, True)

    def remove_token(self, key: str) -> None:
        with self.lock:
            n = self.tokens.get(key, 0)
            if n <= 1:
                self.tokens.pop(key, None)
                ws = [cb for pat, cb in self.watchers.values() if key_matches(pat, key)] if n == 1 else []
            else:
                self.tokens[key] = n - 1
                ws = []
        for cb in ws:
            cb(key, False)


_NETS: dict[str, LocalNet] = {}
_NETS_LOCK = threading.Lock()


def local_net(namespace: str = "local") -> LocalNet:
    with _NETS_LOCK:
        net = _NETS.get(namespace)
        if net is None:
            net = _NETS[namespace] = LocalNet(namespace)
        return net


class _LocalRequest(Request):
    __slots__ = ("_cb", "_timer")

    def __init__(self, bus: Bus, key: str, payload: bytes, cb: Callable[[bytes | None, BaseException | None], None],
                 timer: threading.Timer) -> None:
        super().__init__(bus, key, payload)
        self._cb = cb
        self._timer = timer

    def _send(self, payload: bytes) -> None:
        self._timer.cancel()
        self._cb(payload, None)

    def _drop(self) -> None:
        self._timer.cancel()
        self._cb(None, _NoReply(self.key))  # 已回复时被 once 守卫忽略


class _LocalPublisher(Publisher):
    def __init__(self, key: str, priority: Prio, express: bool, bus: LocalBus) -> None:
        super().__init__(key, priority, express)
        self._bus = bus

    def put(self, payload: bytes) -> None:
        if self._bus.closed:
            return
        self._bus.net.publish(self.key, bytes(payload))
        self.puts += 1
        self._bus.stats["puts"] += 1


class LocalBus(Bus):
    """进程内 Bus：同步分发 + 线程安全入队；超时、drop、liveliness 语义与 ZenohBus 一致。"""

    kind = "local"

    def __init__(self, name: str, net: LocalNet, loop: asyncio.AbstractEventLoop | None) -> None:
        super().__init__(name, net.namespace, loop)
        self.net = net
        self._served: list[str] = []
        self._my_tokens: list[str] = []

    @classmethod
    def open(cls, name: str, ctx: RunCtx | None = None, *, loop: asyncio.AbstractEventLoop | None = None,
             net: LocalNet | None = None, namespace: str | None = None, announce: bool = True, **_: Any) -> LocalBus:
        if net is None:
            ns = namespace or (ctx.namespace if ctx is not None else None) or "local"
            net = local_net(ns)
        bus = cls(name, net, _current_loop(loop))
        if announce:
            bus._handles.append(bus.token(bus_keys.proc_alive(name)))
        return bus

    def _declare_queryable(self, key: str, on_request: Callable[[Request], None]) -> Handle:
        sid = self.net.next_id()
        with self.net.lock:
            if key in self.net.servers:
                raise ValueError(f"LocalBus：{key} 已有服务方")
            self.net.servers[key] = (sid, on_request, self)

        def undeclare() -> None:
            with self.net.lock:
                cur = self.net.servers.get(key)
                if cur is not None and cur[0] == sid:
                    del self.net.servers[key]

        return Handle(undeclare, f"queryable {key}")

    def _start_query(self, key: str, payload: bytes, timeout: float, cb: Callable[[bytes | None, BaseException | None], None]) -> None:
        once = _once(cb)
        with self.net.lock:
            srv = self.net.servers.get(key)
        if srv is None:
            once(None, _NoReply(key))  # 无服务方：与 zenoh 相同，查询立即结束而没有回复
            return
        timer = threading.Timer(timeout, once, args=(None, _NoReply(key)))
        timer.daemon = True
        server_bus = srv[2]
        req = _LocalRequest(server_bus, key, bytes(payload), once, timer)
        server_bus._track(req)
        timer.start()
        try:
            srv[1](req)
        except Exception:
            log.exception("local request dispatch failed", extra={"kv": {"key": key}})
            req.close()

    def publisher(self, key: str, *, priority: Prio | None = None, express: bool | None = None) -> Publisher:
        q = qos_for(key)
        return _LocalPublisher(key, q.priority if priority is None else priority, q.express if express is None else express,
                               self)

    def subscribe(self, key: str, cb: Callable[[str, bytes], None]) -> Handle:
        sid = self.net.next_id()
        with self.net.lock:
            self.net.subs[sid] = (key, cb)

        def undeclare() -> None:
            with self.net.lock:
                self.net.subs.pop(sid, None)

        h = Handle(undeclare, f"subscriber {key}")
        self._handles.append(h)
        return h

    def token(self, key: str) -> Handle:
        self.net.add_token(key)
        self._my_tokens.append(key)

        def undeclare() -> None:
            if key in self._my_tokens:
                self._my_tokens.remove(key)
                self.net.remove_token(key)

        return Handle(undeclare, f"token {key}")

    def watch(self, pattern: str, on_change: Callable[[str, bool], None]) -> Handle:
        wid = self.net.next_id()
        with self.net.lock:
            self.net.watchers[wid] = (pattern, on_change)
            existing = [k for k in self.net.tokens if key_matches(pattern, k)]
        for k in existing:
            on_change(k, True)

        def undeclare() -> None:
            with self.net.lock:
                self.net.watchers.pop(wid, None)

        h = Handle(undeclare, f"watch {pattern}")
        self._handles.append(h)
        return h

    def alive(self, pattern: str, *, timeout: float = 0.5) -> list[str]:
        with self.net.lock:
            return sorted(k for k in self.net.tokens if key_matches(pattern, k))

    def _close_transport(self) -> None:
        for k in list(self._my_tokens):  # 会话死亡：该进程的全部 token 撤销
            self._my_tokens.remove(k)
            self.net.remove_token(k)


def open_bus(name: str, ctx: RunCtx | None = None, *, kind: str = "zenoh", loop: asyncio.AbstractEventLoop | None = None,
             **kw: Any) -> Bus:
    """按种类打开总线：`zenoh`（默认，进程间）或 `local`（进程内，--inproc 与测试）。"""
    if kind == "zenoh":
        return ZenohBus.open(name, ctx, loop=loop, **kw)
    if kind == "local":
        return LocalBus.open(name, ctx, loop=loop, **kw)
    raise ValueError(f"未知总线种类：{kind}")
