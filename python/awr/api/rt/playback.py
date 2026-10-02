"""PlaybackController：`playback` op 与回放模式切换（D1-ext；M11-FR-050、FR-077 至 FR-079；AWR-17 §6.11；M12 §6.7.6、§7.4）。

cmd：
- open `{run, segment}`：席位持有者（116）且 operator 以上（115）；实时会话须为 PAUSED（117）→ `playbackState{opening}` →
  受监管时经 supervisor `sys/start{name: replay-worker, args{run, segment}}` 启动按需进程（105 进程已在运行视为可继续，其余失败
  213），再等待 liveliness `proc/replay-worker/ready`（≤ 10 s，M11-FR-079；冷启动导入 numpy、mcap、zenoh 约 1–2 s，此前没有
  queryable）→ `ctl/replay-worker/open` → 回复 `{status, code, data_start_ns, data_end_ns, speed_max, decimation_s, lineage, gen, backfill,
  warnings}`（绑定不一致 122）→ Gateway 切到 ReplaySource（`state.replay`）、把 backfill 原子装入各 channel（各 seq + 1）、
  mode = replay、全局 epoch + 1（TIME 置 bit7）→ 重发 `serverInfo{mode: replay, dataStart_ns, dataEnd_ns}` →
  `playbackState{paused}` → SNAPSHOT；
- seek `{seek_ns}`：`dataStart ≤ seek_ns ≤ dataEnd`（110）→ `playbackState{buffering}` → `ctl/replay-worker/seek` → 装入 backfill →
  epoch + 1 → 严格按 TIME → `playbackState{did_seek: true}` → SNAPSHOT 的顺序（同一同步块内排入控制面，sender 先排空控制面）；
- play、pause → `ctl/replay-worker/{play,pause}` → playbackState；speed `{speed}` ∈ [0.1, 20]（否则 110），超过 speed_max 时
  replay-worker 钳制并在 warnings 带 `SPEED_CLAMPED`；
- close → `ctl/replay-worker/close` → 切回 LiveSource、mode = live、epoch + 1 → `serverInfo{mode: live}` → `playbackState{idle}` →
  SNAPSHOT；受监管时 `sys/stop{replay-worker}`。
回放打开期间监视 `proc/replay-worker/alive`：进程崩溃（kill -9 等）时向全部连接广播 `playbackState{status: error, code: 213}`
（M12-FR-051、M12-AC-051：UI 1 s 内提示），保持回放模式直到 close（此时不再调用已不存在的 worker）。
每条 cmd 以同一 `request_id` 回一条 playbackState（发给全部连接：回放是会话级模式）；回复中的 gen 记入 ReplaySource 的已知 gen，
同 gen 的环 segment 变化不再 + 1。回放期间一切写操作返回 118（RpcRouter 入口 ①）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

import msgpack

from awr.contracts import bus_keys
from awr.contracts.enums import TimeState
from awr.contracts.reasons import Reason
from awr.runtime.bus import BusError, BusTimeout

from .protocol import jdump

if TYPE_CHECKING:
    from .gateway import Gateway
    from .session import ClientSession

__all__ = ["PlaybackController"]

log = logging.getLogger("awr.api.playback")

WORKER = "replay-worker"
SPEED_MIN, SPEED_MAX = 0.1, 20.0
READY_TIMEOUT_S = 10.0  # sys/start 之后等待 proc/replay-worker/ready 的上限（M12-to-M11 第 1 条）


class PlaybackController:
    def __init__(self, gw: Gateway) -> None:
        self.gw = gw
        self.status = "idle"
        self.run: str = ""
        self.segment = 0
        self.data_start_ns = 0
        self.data_end_ns = 0
        self.speed = 1.0
        self.speed_max = SPEED_MAX
        self.gen = 0
        self.lineage: list[dict] = []
        self.decimation_s: float | None = None
        self.busy = False
        self.worker_lost = False
        self.stats = {"opens": 0, "seeks": 0, "closes": 0, "errors": 0, "worker_lost": 0, "ready_waits": 0}
        self.seek_ms: list[float] = []  # 最近 64 次 seek 的服务端总时延（ms，诊断）
        self._tasks: set[asyncio.Task] = set()
        self._alive_watch: Any = None

    # ------------------------------------------------------------ 入口
    def handle(self, s: ClientSession, m: dict) -> None:
        rid = m.get("request_id") if isinstance(m.get("request_id"), str) else ""
        cmd = m.get("cmd")
        if cmd not in ("open", "close", "play", "pause", "seek", "speed"):
            s.error(int(Reason.BAD_REQUEST), "playback", rid or None, "cmd 取值非法")
            return
        gw = self.gw
        if s.role == "viewer":
            self._error(s, rid, int(Reason.ROLE_FORBIDDEN))
            return
        if not gw.is_seat_holder(s.principal.id):
            self._error(s, rid, int(Reason.SEAT_TAKEN))
            return
        if self.busy:
            self._error(s, rid, int(Reason.STATE))
            return
        if cmd != "open" and gw.mode != "replay":
            self._error(s, rid, int(Reason.STATE))
            return
        if cmd == "open" and gw.mode == "replay":
            self._error(s, rid, int(Reason.STATE))
            return
        if cmd == "speed":
            sp = m.get("speed")
            if not isinstance(sp, (int, float)) or not SPEED_MIN <= float(sp) <= SPEED_MAX:
                self._error(s, rid, int(Reason.PARAM_OUT_OF_RANGE))
                return
        if cmd == "seek":
            t = m.get("seek_ns")
            if not isinstance(t, int) or not self.data_start_ns <= t <= self.data_end_ns:
                self._error(s, rid, int(Reason.PARAM_OUT_OF_RANGE))
                return
        if cmd == "open":
            if (gw.clock.state & 0x0F) != int(TimeState.PAUSED):
                self._error(s, rid, int(Reason.CLOCK_CONSTRAINT))
                return
            if not isinstance(m.get("run"), str) or not isinstance(m.get("segment"), int):
                self._error(s, rid, int(Reason.BAD_REQUEST))
                return
        self.busy = True
        t = asyncio.ensure_future(self._run(s, cmd, rid, m))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _run(self, s: ClientSession, cmd: str, rid: str, m: dict) -> None:
        try:
            await getattr(self, f"_cmd_{cmd}")(s, rid, m)
        except (BusTimeout, BusError):
            self._error(s, rid, int(Reason.SERVICE_UNAVAILABLE))
        except Exception:
            log.exception("playback failed", extra={"kv": {"cmd": cmd}})
            self._error(s, rid, int(Reason.INTERNAL_ERROR))
        finally:
            self.busy = False

    async def _call(self, op: str, msg: dict, timeout: float = 2.0) -> dict:
        rep = await self.gw.bus.call(bus_keys.ctl_replay_worker(op), msg, timeout=timeout, retries=1, retry_gap=0.3)
        return rep if isinstance(rep, dict) else {}

    def _principal(self, s: ClientSession, cid: str) -> dict:
        return self.gw.tokens.sign_principal(s.principal.id, s.role, cid, conn_id=s.conn_id, seat=True)

    # ------------------------------------------------------------ cmd
    async def _cmd_open(self, s: ClientSession, rid: str, m: dict) -> None:
        gw = self.gw
        self.run, self.segment = m["run"], int(m["segment"])
        self.status = "opening"
        self._broadcast(rid)
        cid = "pb-" + secrets.token_hex(6)
        if gw.settings.has_supervisor:
            rep = await gw.bus.call(bus_keys.SYS_START, {"v": 1, "cid": cid, "name": WORKER,
                                                         "args": {"run": self.run, "segment": self.segment}},
                                    timeout=20.0, retries=0)
            code = int(rep.get("code") or 0) if isinstance(rep, dict) else int(Reason.SERVICE_UNAVAILABLE)
            # 105：进程已在运行（例如上一次 open 失败或 close 后的空闲期内），可以继续 open
            if not isinstance(rep, dict) or (rep.get("status") != "accepted" and code != int(Reason.STATE)):
                self._error(s, rid, code or int(Reason.SERVICE_UNAVAILABLE))
                return
            if not await self._wait_ready(READY_TIMEOUT_S):
                self._error(s, rid, int(Reason.SERVICE_UNAVAILABLE))
                return
        rep = await self._call("open", {"v": 1, "cid": cid, "run": self.run, "segment": self.segment,
                                        "principal": self._principal(s, cid)}, timeout=5.0)
        if rep.get("status") != "accepted":
            self._error(s, rid, int(rep.get("code") or Reason.RECORDING_INCOMPATIBLE))
            return
        self.data_start_ns = int(rep.get("data_start_ns") or 0)
        self.data_end_ns = int(rep.get("data_end_ns") or 0)
        self.speed_max = float(rep.get("speed_max") or SPEED_MAX)
        self.decimation_s = rep.get("decimation_s")
        self.lineage = [x for x in rep.get("lineage") or [] if isinstance(x, dict)]
        self.gen = int(rep.get("gen") or 0)
        self.speed = 1.0
        self.status = "paused"
        self.stats["opens"] += 1
        self.worker_lost = False
        self._watch_worker()
        gw.enter_replay(self.gen, rep.get("backfill"))
        self._broadcast(rid, warnings=rep.get("warnings"))
        gw.audit.write("playback.open", principal_id=s.principal.id, cid=cid,
                       detail={"run": self.run, "segment": self.segment})

    async def _cmd_seek(self, s: ClientSession, rid: str, m: dict) -> None:
        gw = self.gw
        t0 = time.perf_counter()
        prev = self.status
        self.status = "buffering"
        self._broadcast(rid)
        cid = "pb-" + secrets.token_hex(6)
        rep = await self._call("seek", {"v": 1, "cid": cid, "t_ns": int(m["seek_ns"])})
        t1 = time.perf_counter()
        if rep.get("status") not in ("accepted", "ok", None) or int(rep.get("code") or 0):
            self.status = prev
            self._error(s, rid, int(rep.get("code") or Reason.STATE))
            return
        self.gen = int(rep.get("gen") or self.gen + 1)
        self.status = prev if prev not in ("ended", "buffering") else "paused"
        self.stats["seeks"] += 1
        gw.replay_seek(self.gen, rep.get("backfill"))  # 装入 backfill → epoch + 1（TIME 入队）
        self._broadcast(rid, did_seek=True, current_ns=int(rep.get("t_ns") or m["seek_ns"]))  # 再 playbackState，后 SNAPSHOT
        # 服务端分段时延（D1-AC-18 诊断：区分服务端与前端）：worker 往返（含 worker 侧 seek）、backfill 装入与纪元切换
        t2 = time.perf_counter()
        sk = {"call_ms": round((t1 - t0) * 1e3, 2), "load_ms": round((t2 - t1) * 1e3, 2), "total_ms": round((t2 - t0) * 1e3, 2),
              "worker_ms": rep.get("worker_ms"), "worker_queue_ms": rep.get("queue_ms"), "seek_ns": int(m["seek_ns"])}
        self.seek_ms.append(sk["total_ms"])
        del self.seek_ms[:-64]
        log.info("playback seek", extra={"kv": sk})

    async def _cmd_play(self, s: ClientSession, rid: str, m: dict) -> None:
        await self._simple(s, rid, "play", "playing")

    async def _cmd_pause(self, s: ClientSession, rid: str, m: dict) -> None:
        await self._simple(s, rid, "pause", "paused")

    async def _simple(self, s: ClientSession, rid: str, op: str, status: str) -> None:
        cid = "pb-" + secrets.token_hex(6)
        rep = await self._call(op, {"v": 1, "cid": cid})
        if int(rep.get("code") or 0):
            self._error(s, rid, int(rep["code"]))
            return
        self.status = status
        self._broadcast(rid)

    async def _cmd_speed(self, s: ClientSession, rid: str, m: dict) -> None:
        cid = "pb-" + secrets.token_hex(6)
        rep = await self._call("speed", {"v": 1, "cid": cid, "speed": float(m["speed"])})
        if int(rep.get("code") or 0):
            self._error(s, rid, int(rep["code"]))
            return
        self.speed = float(rep.get("speed") or m["speed"])
        self._broadcast(rid, warnings=rep.get("warnings"))

    async def _cmd_close(self, s: ClientSession, rid: str, m: dict) -> None:
        gw = self.gw
        cid = "pb-" + secrets.token_hex(6)
        self._unwatch_worker()
        if not self.worker_lost:  # 进程已丢失时不再等待两次查询超时
            with contextlib.suppress(BusTimeout, BusError):
                await self._call("close", {"v": 1, "cid": cid})
        self.worker_lost = False
        self.status = "idle"
        self.stats["closes"] += 1
        gw.exit_replay()
        self._broadcast(rid)
        if gw.settings.has_supervisor:
            with contextlib.suppress(BusTimeout, BusError):
                await gw.bus.call(bus_keys.SYS_STOP, {"v": 1, "cid": cid, "name": WORKER}, timeout=5.0, retries=0)
        gw.audit.write("playback.close", principal_id=s.principal.id, cid=cid)

    # ------------------------------------------------------------ replay-worker 就绪与存活
    async def _wait_ready(self, timeout: float) -> bool:
        """等待 liveliness `proc/replay-worker/ready`（replay-worker 在全部 queryable 声明之后才 `bus.ready()`）。
        watch 带历史：token 已存在时立即回调。"""
        loop = asyncio.get_running_loop()
        ev = asyncio.Event()

        def on_change(_k: str, alive: bool) -> None:  # 总线回调线程：只入队（PY-CB-01）
            if alive and not loop.is_closed():
                loop.call_soon_threadsafe(ev.set)

        self.stats["ready_waits"] += 1
        h = self.gw.bus.watch(bus_keys.proc_ready(WORKER), on_change)
        try:
            await asyncio.wait_for(ev.wait(), timeout)
            return True
        except TimeoutError:
            log.warning("replay-worker not ready", extra={"kv": {"timeout_s": timeout}})
            return False
        finally:
            with contextlib.suppress(Exception):
                h.close()

    def _watch_worker(self) -> None:
        """回放打开期间监视 `proc/replay-worker/alive`；撤销即视为进程丢失（M12-FR-051）。"""
        self._unwatch_worker()
        loop = self.gw.loop
        if loop is None:
            return

        def on_change(_k: str, alive: bool) -> None:  # 总线回调线程：只入队（PY-CB-01）；停止过程中迟到的撤销不再投递
            if not alive and not loop.is_closed():
                loop.call_soon_threadsafe(self._on_worker_lost)

        with contextlib.suppress(Exception):
            self._alive_watch = self.gw.bus.watch(bus_keys.proc_alive(WORKER), on_change)

    def _unwatch_worker(self) -> None:
        h, self._alive_watch = self._alive_watch, None
        if h is not None:
            with contextlib.suppress(Exception):
                h.close()

    def _on_worker_lost(self) -> None:
        if self.gw.mode != "replay" or self.worker_lost or self._alive_watch is None:
            return
        self.worker_lost = True
        self.stats["worker_lost"] += 1
        self.status = "error"
        log.warning("replay-worker lost during replay", extra={"kv": {"run": self.run, "segment": self.segment}})
        msg = jdump(self.state_msg("", code=int(Reason.SERVICE_UNAVAILABLE)))
        for s in self.gw.sessions:
            if s.hello and not s.closing:
                s.send_ctrl(msg)
        self.gw.audit.write("playback.worker_lost", detail={"run": self.run, "segment": self.segment})

    def close(self) -> None:
        self._unwatch_worker()

    def on_hello(self, s: ClientSession) -> None:
        """回放模式下的新连接（页面刷新、另一客户端加入）：hello 之后单独补发一次当前 playbackState（`request_id` 为空；
        replay-worker 已丢失时为 `error, 213`）。此前只在状态变化时广播，迟到者不知道 run、段、dataStart/End 与 speed_max
        （FX-WEB2-to-M11 第 1 条；17 §6.11 补充约定第 5 条）。"""
        if self.gw.mode != "replay":
            return
        code = int(Reason.SERVICE_UNAVAILABLE) if self.worker_lost else None
        s.send_ctrl(jdump(self.state_msg("", code=code)))

    # ------------------------------------------------------------ playbackState
    def state_msg(self, rid: str, *, did_seek: bool = False, code: int | None = None, current_ns: int | None = None,
                  warnings: Any = None) -> dict:
        gw = self.gw
        m: dict[str, Any] = {"op": "playbackState", "status": self.status,
                             "current_ns": int(current_ns if current_ns is not None else gw.clock.t_sim_ns),
                             "speed": float(self.speed), "did_seek": did_seek, "request_id": rid,
                             "epoch": gw.clock.epoch_u16, "run": self.run or "", "segment": int(self.segment),
                             "dataStart_ns": int(self.data_start_ns), "dataEnd_ns": int(self.data_end_ns),
                             "speed_max": float(self.speed_max)}
        if self.lineage:
            m["lineage"] = [{"epoch": int(x.get("epoch", 0)), "t_from_ns": int(x.get("t_from_ns", 0)),
                             "t_to_ns": int(x.get("t_to_ns", 0))} for x in self.lineage]
        if self.decimation_s is not None:
            m["decimation_s"] = float(self.decimation_s)
        if code is not None:
            m["code"] = int(code)
        if isinstance(warnings, list) and warnings:
            m["warnings"] = [str(w) for w in warnings]
        return m

    def _broadcast(self, rid: str, **kw: Any) -> None:
        msg = jdump(self.state_msg(rid, **kw))
        for s in self.gw.sessions:
            if s.hello and not s.closing:
                s.send_ctrl(msg)

    def _error(self, s: ClientSession, rid: str, code: int) -> None:
        self.stats["errors"] += 1
        prev = self.status
        self.status = "error" if self.gw.mode != "replay" else (prev if prev != "opening" else "paused")
        s.send_ctrl(self.state_msg(rid, code=code) | {"status": "error"})
        if self.gw.mode != "replay":
            self.status = "idle"


def unpack_or_none(b: Any) -> Any:
    if not isinstance(b, (bytes, bytearray)):
        return b
    try:
        return msgpack.unpackb(b, raw=False, strict_map_key=False)
    except Exception:
        return None
