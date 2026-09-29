"""McapSource：回放状态机、播放循环、seek 与 backfill、倍速、查询（M12 §6.7；FR-041 至 FR-050；实现 M11 的 Source 协议）。

状态（12 §4.11 Player；W 编号见 M12 §6.7.1）：IDLE --open--> OPENING --索引就绪--> PAUSED 与 PLAYING 互转（play、pause）；
PLAYING --预读缺数据 > 100 ms--> BUFFERING --就绪--> PLAYING；PLAYING --到已关闭段段尾--> ENDED；seek 在任何打开态执行
（ENDED 回到 PAUSED）；OPEN 段到段尾时追读新 chunk（W09）；close --> IDLE。

播放循环（宿主每 ≤ 4 ms 调用 `step(now)`）：回放时钟 `t_play += rate × Δwall`（int ns），每轮派发 (t_prev, t_play] 的消息：
只发布"不晚于 t_play 的最新复合帧"（至多 1 个），事件逐条，低频按 channel 保留最新（增量块合并进每机最新值）；头部心跳
写 t_play、clock_state（置 bit7）、rate_milli。

seek(t)：索引定位每个 latest 型 channel 不晚于 t 的最后一条、关键块型 channel 的最后关键块加其后增量，写复合帧（块时刻
t_b ≤ t），gen + 1，返回 backfill 包（env、roster、clock、state_ext、safety、missions、sensor）；宿主在回复发出之后才把
gen 写入环头部 `segment`（M12 §7.4）。backfill 使用逐 channel 索引，不使用逆序 `iter_messages`（RK-M12-02）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msgpack

from awr.contracts import CONTRACTS_VERSION, LAYOUT_ID
from awr.contracts.enums import TimeState
from awr.contracts.reasons import Reason
from awr.runtime.bus import Bus
from awr.runtime.source import SourceIncompatible, SourceInfo, SourceSpec
from awr.runtime.statering import StateRing

from .assembler import ReplayEventPublisher, ReplayPublishers, publish_composite
from .chunk_cache import FOREVER, ChunkCache, ReadAhead
from .config import ReplayCfg
from .formats import (
    RFLAG_KEYFRAME,
    T_BLOCK,
    T_CLOCK,
    T_ENV,
    T_EVENT,
    T_EXT,
    T_ROSTER,
    T_SAFETY,
    is_full_topic,
    is_pose_topic,
    split_prefix,
)
from .keydelta import merge_blocks, pack_batch
from .mcap_index import MsgRef, SegmentIndex

__all__ = ["McapSource", "ReplayError", "speed_max_of"]

log = logging.getLogger("awr.recorder.replay")

IDLE, OPENING, PAUSED, PLAYING, BUFFERING, ENDED = "idle", "opening", "paused", "playing", "buffering", "ended"
TIME_CODE = {IDLE: int(TimeState.STOPPED), OPENING: int(TimeState.BUFFERING), PAUSED: int(TimeState.PAUSED),
             PLAYING: int(TimeState.PLAYING), BUFFERING: int(TimeState.BUFFERING), ENDED: int(TimeState.ENDED)}


class ReplayError(Exception):
    def __init__(self, code: int, detail: str = "") -> None:
        super().__init__(detail or str(code))
        self.code = int(code)


def speed_max_of(bytes_per_sim_s: float, events_per_sim_s: float, cfg: ReplayCfg) -> float:
    """speed_max = min(20, 64 MB/s ÷ 每仿真秒字节, 5000 条/s ÷ 每仿真秒事件)（M12 §6.7.7；ADR-040）。"""
    v = cfg.speed_max
    if bytes_per_sim_s > 0:
        v = min(v, cfg.speed_max_bytes_per_s / bytes_per_sim_s)
    if events_per_sim_s > 0:
        v = min(v, cfg.speed_max_events_per_s / events_per_sim_s)
    return round(v, 3)


def _unpack(b: bytes | memoryview) -> Any:
    return msgpack.unpackb(b, raw=False, strict_map_key=False)


class McapSource:
    mode = "replay"

    def __init__(self, cfg: ReplayCfg, *, runs_dir: Path, bus: Bus | None = None,
                 mono_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self.cfg = cfg
        self.runs_dir = Path(runs_dir)
        self.bus = bus
        self.mono_ns = mono_ns
        self.state = IDLE
        self.index: SegmentIndex | None = None
        self.cache: ChunkCache | None = None
        self.readahead: ReadAhead | None = None
        self.ring: StateRing | None = None
        self.events: ReplayEventPublisher | None = None
        self.pubs: ReplayPublishers | None = None
        self.gen = 0
        self.t_play = 0
        self.rate = 1.0
        self.speed_max = cfg.speed_max
        self.decimation_s: float | None = None
        self.warnings: list[str] = []
        self.lineage: list[dict[str, int]] = []
        self.run = ""
        self.segment = 0
        self.seg_closed = False
        self.last_ns = 0
        self.stall_ns = 0
        self.buffering_ns = 0
        self.played_ns = 0
        self.last_block_t = -1
        self.last_backfill: dict[str, Any] | None = None
        self.last_seek_ms: list[float] = []
        self.ext_state: dict[int, Any] = {}
        self.saf_state: dict[int, Any] = {}
        self.pose_latest: dict[int, bytes] = {}
        self.roster_raw: bytes | None = None
        self.ch: dict[str, int | None] = {}
        self.full_chs: list[int] = []
        self.pose_chs: list[int] = []
        self.mission_chs: list[int] = []
        self._pose_next = 0

    # ------------------------------------------------------------ open
    def open(self, spec: SourceSpec, ring: StateRing, events: Any, *, pubs: ReplayPublishers | None = None) -> SourceInfo:
        if self.state != IDLE:
            self.close()
        self.state = OPENING
        path = self.runs_dir / spec.run / f"rec-{spec.segment:03d}.mcap"
        if not path.is_file():
            self.state = IDLE
            raise ReplayError(int(Reason.NOT_FOUND), str(path))
        seg_state = self._meta_state(spec)
        if seg_state == "CORRUPT":
            self.state = IDLE
            raise ReplayError(int(Reason.REC_SEGMENT_CORRUPT), "segment CORRUPT")
        ix = SegmentIndex.build(path, allow_open=True)
        b = ix.binding()
        self.warnings = []
        mism = []
        if b.get("world_id") and b.get("world_id") != spec.world_id:
            mism.append("world_id")
        if b.get("layout_id") and int(b.get("layout_id", LAYOUT_ID)) != int(spec.layout_id):
            mism.append("layout_id")
        if spec.content_version and b.get("content_version") and b.get("content_version") != spec.content_version:
            mism.append("content_version")
        zero = "0" * 64
        if (spec.coordinate_sha256 and spec.coordinate_sha256 != zero and b.get("coordinate_sha256") not in (None, "", zero)
                and b.get("coordinate_sha256") != spec.coordinate_sha256):
            mism.append("coordinate_sha256")
        if mism:
            ix.close()
            self.state = IDLE
            raise SourceIncompatible(",".join(mism))
        cv = b.get("contracts_version", CONTRACTS_VERSION)
        if cv != CONTRACTS_VERSION:
            if cv.split(".")[0] != CONTRACTS_VERSION.split(".")[0]:
                ix.close()
                self.state = IDLE
                raise SourceIncompatible("contracts_version")
            self.warnings.append("CONTRACTS_MINOR_DIFFERS")
        self.index = ix
        self.run, self.segment = spec.run, spec.segment
        self.seg_closed = ix.closed and ix.segment_end() is not None
        self.cache = ChunkCache(ix, self.cfg.cache_mb << 20)
        self.readahead = ReadAhead(self.cache, min_window_ns=int(self.cfg.readahead_min_s * 1e9), wall_window_s=self.cfg.readahead_wall_s)
        self.ring = ring
        self.events = events
        self.pubs = pubs
        self._classify()
        st = ix.stats()
        self.speed_max = speed_max_of(st["bytes_per_sim_s"], st["events_per_sim_s"], self.cfg)
        self.decimation_s = self._decimation()
        self.lineage = self._lineage_ranges()
        self.rate = 1.0
        self.gen = 0
        self.seek(ix.data_start_ns)
        self.state = PAUSED
        self.heartbeat()
        return self.info()

    def _meta_state(self, spec: SourceSpec) -> str | None:
        import json

        try:
            m = json.loads((self.runs_dir / spec.run / "meta.json").read_text(encoding="utf-8"))
            for s in m.get("segments", []):
                if int(s.get("segment", -1)) == spec.segment:
                    return str(s.get("state"))
        except (OSError, ValueError):
            return None
        return None

    def _classify(self) -> None:
        ix = self.index
        assert ix is not None
        for t in (T_BLOCK, T_EVENT, T_ENV, T_ROSTER, T_CLOCK, T_EXT, T_SAFETY):
            self.ch[t] = ix.ch(t)
        self.full_chs = [c for t, c in ix.topic_ch.items() if is_full_topic(t)]
        self.pose_chs = [c for t, c in ix.topic_ch.items() if is_pose_topic(t)]
        self.mission_chs = [c for t, c in ix.topic_ch.items() if t.startswith("/mission/")]

    def _decimation(self) -> float | None:
        ix = self.index
        ch = self.ch.get(T_BLOCK)
        if ix is None or ch is None or ch not in ix.idx or len(ix.idx[ch][0]) < 3:
            return None
        import numpy as np

        d = np.diff(ix.idx[ch][0])
        d = d[d > 0]
        if not len(d):
            return None
        med = float(np.median(d))
        return round(float(d.max()) / 1e9, 3) if med > 45e6 or d.max() > 60e6 else None

    def _lineage_ranges(self) -> list[dict[str, int]]:
        ix = self.index
        assert ix is not None
        lin = ix.lineage()
        b = ix.binding()
        first = int(b.get("epoch_start", 1) or 1)
        out = [{"epoch": first, "t_from_ns": ix.data_start_ns, "t_to_ns": ix.data_end_ns}]
        for e in lin:
            out[-1]["t_to_ns"] = int(e.get("last_t_ns", out[-1]["t_to_ns"]))
            out.append({"epoch": int(e.get("epoch", 0)), "t_from_ns": int(e.get("restored_t_ns", 0)), "t_to_ns": ix.data_end_ns})
        return out

    def info(self) -> SourceInfo:
        ix = self.index
        si = SourceInfo(data_start_ns=ix.data_start_ns if ix else 0, data_end_ns=ix.data_end_ns if ix else 0, speed_max=self.speed_max,
                        channels=ix.topics() if ix else [], warnings=list(self.warnings))
        return si

    # ------------------------------------------------------------ 消息读取
    def _msg(self, ref: MsgRef) -> tuple[int, int, memoryview]:
        """(epoch, rflags, payload)"""
        assert self.cache is not None
        m = self.cache.message(ref)
        ep, rf, _dt, payload = split_prefix(m.data)
        return ep, rf, payload

    def _latest_payload(self, topic_ch: int | None, t: int) -> bytes | None:
        ix = self.index
        if ix is None:
            return None
        r = ix.last_le(topic_ch, t)
        return None if r is None else bytes(self._msg(r)[2])

    def _merged(self, ch: int | None, t: int) -> dict[int, Any]:
        """最后关键块 + 其后不晚于 t 的增量块。"""
        ix = self.index
        if ix is None or ch is None or ch not in ix.idx:
            return {}
        _a, b = ix.range_count(ch, -1, t)
        k = b - 1
        blocks: list[bytes] = []
        while k >= 0:
            _ep, rf, payload = self._msg(ix.at(ch, k))
            blocks.append(bytes(payload))
            if rf & RFLAG_KEYFRAME:
                break
            k -= 1
        blocks.reverse()
        return merge_blocks(blocks)

    def _composite(self, t: int) -> tuple[int, bytes, list[bytes]] | None:
        """不晚于 t 的最后一个块与同刻的标记机 Full64 行。"""
        ix = self.index
        if ix is None:
            return None
        r = ix.last_le(self.ch.get(T_BLOCK), t)
        if r is None:
            return None
        _ep, _rf, blk = self._msg(r)
        rows = []
        for ch in self.full_chs:
            fr = ix.exact(ch, r.t)
            if fr is not None:
                rows.append(bytes(self._msg(fr)[2]))
        return r.t, bytes(blk), rows

    def _sensor_rows(self, t: int) -> bytes | None:
        ix = self.index
        if ix is None or not self.pose_chs:
            return None
        rows = []
        for ch in self.pose_chs:
            r = ix.last_le(ch, t)
            if r is not None:
                rows.append(bytes(self._msg(r)[2]))
        return msgpack.packb({"v": 1, "t_sim_ns": int(t), "rows": b"".join(rows)}, use_bin_type=True) if rows else None

    # ------------------------------------------------------------ seek（W05）
    def seek(self, t_ns: int) -> int:
        ix = self.index
        if ix is None or self.ring is None:
            raise ReplayError(int(Reason.STATE), "not open")
        if not ix.data_start_ns <= t_ns <= ix.data_end_ns:
            raise ReplayError(int(Reason.PARAM_OUT_OF_RANGE), "seek outside [dataStart, dataEnd]")
        t0 = time.perf_counter()
        before = self.state
        self.state = BUFFERING
        self.heartbeat(t_ns)
        comp = self._composite(t_ns)
        t_b = t_ns
        if comp is not None:
            t_b, blk, rows = comp
            publish_composite(self.ring, t_b, blk, rows)
        self.last_block_t = t_b
        env = self._latest_payload(self.ch.get(T_ENV), t_ns)
        roster = self._latest_payload(self.ch.get(T_ROSTER), t_ns)
        clock = self._latest_payload(self.ch.get(T_CLOCK), t_ns)
        self.ext_state = self._merged(self.ch.get(T_EXT), t_ns)
        self.saf_state = self._merged(self.ch.get(T_SAFETY), t_ns)
        missions = [p for p in (self._latest_payload(c, t_ns) for c in self.mission_chs) if p is not None]
        sensor = self._sensor_rows(t_ns)
        self.roster_raw = roster
        self.gen += 1
        if self.events is not None:
            self.events.set_epoch(self.gen)
        self.t_play = int(t_ns)
        self.last_ns = self.mono_ns()
        self.stall_ns = 0
        if self.readahead is not None:
            self.readahead.restart(self.t_play, self.rate)
        self.state = before if before not in (ENDED, OPENING, IDLE) else PAUSED
        self.last_backfill = {"env": env, "roster": roster, "clock": clock,
                              "state_ext": pack_batch(self.ext_state) if self.ext_state else None,
                              "safety": pack_batch(self.saf_state) if self.saf_state else None,
                              "missions": missions, "sensor": sensor}
        self.last_seek_ms.append((time.perf_counter() - t0) * 1000)
        del self.last_seek_ms[:-100]
        self.t_sample = int(t_b)
        return int(t_ns)

    def backfill_bundle(self) -> dict[str, Any]:
        return dict(self.last_backfill or {})

    # ------------------------------------------------------------ 控制
    def play(self) -> None:
        if self.state in (PAUSED, BUFFERING):
            self.state = PLAYING
            self.last_ns = self.mono_ns()
            self.stall_ns = 0
        elif self.state == ENDED:
            raise ReplayError(int(Reason.STATE), "ended: seek first")

    def pause(self) -> None:
        if self.state in (PLAYING, BUFFERING):
            self.state = PAUSED

    def set_speed(self, rate: float) -> tuple[float, list[str]]:
        if not self.cfg.speed_min <= rate <= self.cfg.speed_max:
            raise ReplayError(int(Reason.PARAM_OUT_OF_RANGE), "speed outside [0.1, 20]")
        warnings: list[str] = []
        r = float(rate)
        if r > self.speed_max:
            r = self.speed_max
            warnings.append("SPEED_CLAMPED")
        self.rate = r
        if self.readahead is not None:
            self.readahead.restart(self.t_play, r)
        return r, warnings

    # ------------------------------------------------------------ 播放循环（W03–W09）
    def heartbeat(self, t_ns: int | None = None) -> None:
        if self.ring is not None:
            self.ring.heartbeat(int(self.t_play if t_ns is None else t_ns), TIME_CODE.get(self.state, 2) | 0x80, round(self.rate * 1000))

    def step(self, now_mono_ns: int) -> None:
        ix = self.index
        if ix is None or self.ring is None or self.state in (IDLE, OPENING):
            return
        dt = max(0, now_mono_ns - self.last_ns)
        self.last_ns = now_mono_ns
        ra = self.readahead
        if self.state == PLAYING:
            t_prev = self.t_play
            t_next = min(self.t_play + int(self.rate * dt), ix.data_end_ns)
            ready = ra.ready_until() if ra is not None else FOREVER
            if t_next > ready:
                self.stall_ns += dt
                if self.stall_ns > int(self.cfg.buffering_after_s * 1e9):
                    self.state = BUFFERING
                t_next = max(t_prev, ready)
            else:
                self.stall_ns = 0
            self._dispatch(t_prev, t_next)
            self.t_play = t_next
            self.played_ns += dt
            if ra is not None:
                ra.advance(self.t_play)
            if self.t_play >= ix.data_end_ns:
                if self.seg_closed:
                    self.state = ENDED
                elif ix.refresh_tail() == 0:
                    pass  # W09：OPEN 段无新增数据，保持
        elif self.state == BUFFERING:
            self.buffering_ns += dt
            if ra is None or ra.ready_until() > self.t_play:
                self.state = PLAYING
                self.stall_ns = 0
        if self.events is not None:
            self.events.flush()
        self.heartbeat()

    def _dispatch(self, t0: int, t1: int) -> None:
        """(t0, t1] 的消息：最新复合帧（至多 1 个）、事件逐条、低频按 channel 最新。"""
        ix = self.index
        if ix is None or self.ring is None or t1 <= t0:
            return
        r = ix.last_le(self.ch.get(T_BLOCK), t1)
        if r is not None and r.t > self.last_block_t:
            comp = self._composite(t1)
            if comp is not None:
                publish_composite(self.ring, comp[0], comp[1], comp[2])
                self.last_block_t = comp[0]
        if self.events is not None:
            for ref in ix.range(self.ch.get(T_EVENT), t0, t1):
                _ep, _rf, payload = self._msg(ref)
                try:
                    ev = _unpack(payload)
                except Exception:
                    continue
                if isinstance(ev, dict):
                    self.events.emit_recorded(ev)
        pubs = self.pubs
        for topic, state_attr, which in ((T_EXT, "ext_state", "ext"), (T_SAFETY, "saf_state", "safety")):
            dirty = False
            for ref in ix.range(self.ch.get(topic), t0, t1):
                _ep, rf, payload = self._msg(ref)
                cur = getattr(self, state_attr)
                upd = merge_blocks([bytes(payload)])
                if rf & RFLAG_KEYFRAME:
                    cur = upd
                else:
                    cur.update(upd)
                setattr(self, state_attr, cur)
                dirty = True
            if dirty and pubs is not None:
                pubs.put(which, pack_batch(getattr(self, state_attr)))
        env_ref = None
        for ref in ix.range(self.ch.get(T_ENV), t0, t1):
            env_ref = ref
        if env_ref is not None and pubs is not None:
            pubs.put("env", bytes(self._msg(env_ref)[2]))
        ros = None
        for ref in ix.range(self.ch.get(T_ROSTER), t0, t1):
            ros = ref
        if ros is not None:
            self.roster_raw = bytes(self._msg(ros)[2])
        if pubs is not None:
            items = []
            for ch in self.mission_chs:
                last = None
                for ref in ix.range(ch, t0, t1):
                    last = ref
                if last is not None:
                    try:
                        items.append(_unpack(self._msg(last)[2]))
                    except Exception:
                        continue
            if items:
                pubs.put("mission", msgpack.packb(items, use_bin_type=True))
            if self.pose_chs and t1 >= self._pose_next:
                b = int(1e9 / self.cfg.sensor_hz)
                self._pose_next = (t1 // b + 1) * b
                s = self._sensor_rows(t1)
                if s is not None:
                    pubs.put("sensor", s)

    # ------------------------------------------------------------ 查询（FR-050）
    def query(self, q: dict[str, Any]) -> dict[str, Any]:
        ix = self.index
        if ix is None:
            raise ReplayError(int(Reason.REPLAY_NOT_OPEN), "not open")
        kind = q.get("kind")
        if kind == "env_at":
            t = int(q.get("t_ns", self.t_play))
            return {"keyframe": self._latest_payload(self.ch.get(T_ENV), t)}
        if kind != "events":
            raise ReplayError(int(Reason.BAD_REQUEST), "unknown query kind")
        ch = self.ch.get(T_EVENT)
        limit = max(1, min(1000, int(q.get("limit", 200) or 200)))
        lvl = int(q.get("level_min", 0) or 0)
        to_ns = int(q["to_ns"]) if q.get("to_ns") is not None else ix.data_end_ns
        items: list[dict[str, Any]] = []
        if ch is None or ch not in ix.idx:
            return {"items": [], "next_cursor": None}
        if q.get("from_ns") is None and q.get("cursor") is None:
            # t 之前最近 limit 条（事件视图吸附到新时刻）
            _a, b = ix.range_count(ch, -1, to_ns)
            k = b - 1
            while k >= 0 and len(items) < limit:
                ev = self._event_at(ch, k)
                if ev is not None and int(ev.get("severity", 0) or 0) >= lvl:
                    items.append(ev)
                k -= 1
            items.reverse()
            return {"items": items, "next_cursor": None}
        k = int(q["cursor"]) if q.get("cursor") is not None else ix.range_count(ch, int(q["from_ns"]) - 1, to_ns)[0]
        _a, b = ix.range_count(ch, -1, to_ns)
        while k < b and len(items) < limit:
            ev = self._event_at(ch, k)
            k += 1
            if ev is not None and int(ev.get("severity", 0) or 0) >= lvl:
                items.append(ev)
        return {"items": items, "next_cursor": k if k < b else None}

    def _event_at(self, ch: int, k: int) -> dict[str, Any] | None:
        ix = self.index
        assert ix is not None
        _ep, _rf, payload = self._msg(ix.at(ch, k))
        try:
            ev = _unpack(payload)
        except Exception:
            return None
        if not isinstance(ev, dict):
            return None
        ev["mseq"] = int(ix.idx[ch][3][k])
        return ev

    def roster(self) -> dict[str, Any] | None:
        if self.roster_raw is None:
            return None
        try:
            r = _unpack(self.roster_raw)
        except Exception:
            return None
        if isinstance(r, dict):
            r = dict(r, producer="replay")
            r["entries"] = [dict(e, producer="replay") for e in r.get("entries", []) if isinstance(e, dict)]
        return r

    def perf(self) -> dict[str, Any]:
        sk = sorted(self.last_seek_ms)
        return {"state": self.state, "gen": self.gen, "rate": self.rate, "t_play_ns": self.t_play,
                "cache_mb": round((self.cache.bytes if self.cache else 0) / 2 ** 20, 2),
                "buffering_ratio": round(self.buffering_ns / max(1, self.played_ns + self.buffering_ns), 4),
                "seek_ms_p95": round(sk[int(0.95 * (len(sk) - 1))], 3) if sk else None,
                "index_mb": round((self.index.memory_bytes() if self.index else 0) / 2 ** 20, 2)}

    # ------------------------------------------------------------ close（W11）
    def close(self) -> None:
        if self.readahead is not None:
            self.readahead.close()
        if self.index is not None:
            self.index.close()
        if self.cache is not None:
            self.cache.clear()
        self.index = None
        self.cache = None
        self.readahead = None
        self.ring = None
        self.state = IDLE
