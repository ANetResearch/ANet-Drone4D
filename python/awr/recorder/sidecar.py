"""派生索引 `.ovw`（1 s 概览）与 `.evx`（事件索引）的写出（M12 §6.6.6、§7.5.1、§7.5.2；FR-037）。

两个文件都由 `SidecarBuilder` 从"写入 MCAP 的消息序列"确定性地生成：writer 线程在每条消息 `add_message` 之后、每条
`awr.lineage` metadata 之后调用同一个构建器，`reindex` 按文件顺序重放 MCAP 记录调用同一个构建器，因此在线写出与重建
逐字节一致（M12-AC-037）。

- `.ovw`：以 1 s 仿真时间为 bin；机群统计取 bin 内最后一个整群块（在场、空中、红色高亮、ELAND/FAILSAFE/CRASHED、
  最低电量、最大速度、平均与最大高度），轨迹记录取段首约定的至多 16 架（`awr.binding` 的 `ovw_tracks`）在该块中的行；
  事件按 level 计数；无块的 bin 置 GAP，块间隔 > 60 ms 的 bin 置 DECIMATED，回滚重跑后重写的 bin 置 RERUN
  （时间回退时截掉 restored_t 之后的 bin 重新生成）。
- `.evx`：每条 `/event` 消息 16 B（t_sim_ns、段内写入序号 mseq、level、marker、agent_no）；`awr.lineage` 到达时，旧纪元中
  t ≥ restored_t 的记录回写 SUPERSEDED 标志。
- marker 类别表是契约 `packages/contracts/rec/markers.json` 的草案（M12 §7.5.2，前端同表 `engine/time/markers.ts`）。
写文件：内存中保留全部记录（1 h 约 1 MB），`flush()` 只写脏区间并截断到当前长度，`close()` 回写头部 CLOSED 与计数。
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from awr.contracts.enums import FlightFlags, FlightState, red_highlight
from awr.contracts.layouts import SWARM_LITE32, SWARM_LITE32_OFFSETS

from .formats import (
    BLOCK_HDR,
    EVX_HDR,
    EVX_MAGIC,
    EVX_REC,
    EVX_SUPERSEDED,
    EVX_VERSION,
    MAX_TRACKS,
    NO_AGENT,
    OVW_BIN,
    OVW_BIN_DECIMATED,
    OVW_BIN_GAP,
    OVW_BIN_RERUN,
    OVW_HDR,
    OVW_MAGIC,
    OVW_TRACK,
    OVW_VERSION,
    T_BLOCK,
    T_EVENT,
    split_prefix,
)

__all__ = ["MARKER_RULES", "MarkerClass", "SidecarBuilder", "marker_class"]


class MarkerClass:
    NONE, LIFECYCLE, ROUTE, WARNING, CRITICAL, SYSTEM = 0, 1, 2, 3, 4, 5


MARKER_RULES: dict[str, Any] = {
    "schema": "awr.rec.markers.v1", "version": 1,
    "rules": [
        {"level_min": 3, "class": "critical"},
        {"level_min": 2, "class": "warning"},
        {"type": "uav.state", "data.to": ["TAKING_OFF", "LANDED"], "class": "lifecycle"},
        {"type": "sim.vehicle.state", "class": "lifecycle"},
        {"type": "cmd.accepted", "data.op": ["goto", "follow_path", "orbit", "rtl", "land"], "class": "route"},
        {"type": "mission.state", "class": "route"},
        {"type_prefix": ["sim.reset", "sim.started", "rec.", "session.", "scenario."], "class": "system"},
    ],
    "default": "none",
}
_CODE = {"none": 0, "lifecycle": 1, "route": 2, "warning": 3, "critical": 4, "system": 5}


def marker_class(kind: str, severity: int, data: dict | None) -> int:
    """markers.json 规则按顺序取第一条命中（总线字段 kind、severity 对应 WS 的 type、level）。"""
    d = data if isinstance(data, dict) else {}
    for r in MARKER_RULES["rules"]:
        if "level_min" in r and severity < r["level_min"]:
            continue
        if "type" in r and r["type"] != kind:
            continue
        if "type_prefix" in r and not any(kind.startswith(p) for p in r["type_prefix"]):
            continue
        if "data.to" in r and d.get("to") not in r["data.to"]:
            continue
        if "data.op" in r and d.get("op") not in r["data.op"]:
            continue
        return _CODE[r["class"]]
    return MarkerClass.NONE


_CRIT_FS = {int(FlightState.ELAND), int(FlightState.FAILSAFE), int(FlightState.CRASHED)}
_POS = SWARM_LITE32_OFFSETS["pos"]
_DECIMATED_GAP_NS = 60_000_000  # 块间隔 > 1.5 × 40 ms 视为抽取


class _Overview:
    def __init__(self, path: Path, segment: int, bin_ns: int, tracks: list[int]) -> None:
        self.path = path
        self.segment = segment
        self.bin_ns = bin_ns
        self.tracks = tracks[:MAX_TRACKS]
        self.t_start = -1
        self.roster_version = 0
        self.bin_bytes = OVW_BIN.size + OVW_TRACK.size * len(self.tracks)
        self.bins = bytearray()
        self.cur = -1  # 当前 bin 序号
        self.cur_block: bytes | None = None
        self.cur_events = [0, 0, 0, 0]
        self.cur_decimated = False
        self.cur_last_t = -1
        self.rerun: list[tuple[int, int]] = []
        self.dirty_from = 0
        self.closed = False

    def _t0(self, t_ns: int) -> None:
        if self.t_start < 0:
            self.t_start = (t_ns // self.bin_ns) * self.bin_ns

    def _bin_of(self, t_ns: int) -> int:
        return (t_ns - self.t_start) // self.bin_ns

    def _emit(self) -> None:
        """当前 bin 结束：写一条记录。"""
        if self.cur < 0:
            return
        flags = 0
        b0 = self.t_start + self.cur * self.bin_ns
        if any(lo < b0 + self.bin_ns and hi >= b0 for lo, hi in self.rerun):
            flags |= OVW_BIN_RERUN
        tracks = []
        if self.cur_block is None:
            flags |= OVW_BIN_GAP
            head = (0, 0, 0, 0, *self.cur_events, 255, flags, 0, math.nan, math.nan, math.nan)
            tracks = [(math.nan, math.nan, math.nan, 255, 0, 0)] * len(self.tracks)
        else:
            if self.cur_decimated:
                flags |= OVW_BIN_DECIMATED
            n, _rv, _r = BLOCK_HDR.unpack_from(self.cur_block, 0)
            rows = np.frombuffer(self.cur_block, SWARM_LITE32, count=n, offset=BLOCK_HDR.size)
            fs = rows["flight_state"] & 0x1F
            fl = rows["flags"]
            bat = rows["battery_pct"]
            known = bat[bat != 255]
            vel = rows["vel_cms"].astype(np.float64) * 0.01
            spd = np.sqrt((vel ** 2).sum(axis=1)) if n else np.zeros(0)
            z = rows["pos"][:, 2].astype(np.float64) if n else np.zeros(0)
            n_alert = int(sum(1 for a, b in zip(fs.tolist(), fl.tolist(), strict=True) if red_highlight(a, b)))
            head = (min(n, 0xFFFF), int(np.count_nonzero(fl & int(FlightFlags.IN_AIR))), n_alert,
                    int(np.isin(fs, list(_CRIT_FS)).sum()), *[min(x, 0xFFFF) for x in self.cur_events],
                    int(known.min()) if known.size else 255, flags, 0,
                    float(spd.max()) if n else math.nan, float(z.mean()) if n else math.nan, float(z.max()) if n else math.nan)
            idx = {int(a): i for i, a in enumerate(rows["agent_no"].tolist())}
            for a in self.tracks:
                i = idx.get(a)
                if i is None:
                    tracks.append((math.nan, math.nan, math.nan, 255, 0, 0))
                else:
                    r = rows[i]
                    tracks.append((float(r["pos"][0]), float(r["pos"][1]), float(r["pos"][2]), int(r["battery_pct"]),
                                   int(r["flight_state"]), 1))
        rec = OVW_BIN.pack(*head) + b"".join(OVW_TRACK.pack(*t) for t in tracks)
        off = self.cur * self.bin_bytes
        if len(self.bins) < off:  # 中间空 bin（整段无数据）：补 GAP
            self._fill_gaps(off)
        self.bins[off:off + self.bin_bytes] = rec
        del self.bins[off + self.bin_bytes:]
        self.dirty_from = min(self.dirty_from, off)

    def _fill_gaps(self, until: int) -> None:
        k = len(self.bins) // self.bin_bytes
        while len(self.bins) < until:
            b0 = self.t_start + k * self.bin_ns
            flags = OVW_BIN_GAP | (OVW_BIN_RERUN if any(lo < b0 + self.bin_ns and hi >= b0 for lo, hi in self.rerun) else 0)
            rec = OVW_BIN.pack(0, 0, 0, 0, 0, 0, 0, 0, 255, flags, 0, math.nan, math.nan, math.nan) + b"".join(
                OVW_TRACK.pack(math.nan, math.nan, math.nan, 255, 0, 0) for _ in self.tracks)
            self.bins += rec
            k += 1
        self.dirty_from = min(self.dirty_from, until)

    def _advance(self, t_ns: int) -> None:
        self._t0(t_ns)
        b = self._bin_of(t_ns)
        if b == self.cur:
            return
        if b > self.cur:
            self._emit()
        else:
            # 时间回退（新纪元从 restored_t 重跑）：丢掉 b 之后的 bin，从 b 重新累计
            del self.bins[b * self.bin_bytes:]
            self.dirty_from = min(self.dirty_from, b * self.bin_bytes)
        self.cur = b
        self.cur_block = None
        self.cur_events = [0, 0, 0, 0]
        self.cur_decimated = False
        self.cur_last_t = -1

    def on_block(self, t_ns: int, payload: bytes) -> None:
        if self.t_start < 0:
            _n, rv, _ = BLOCK_HDR.unpack_from(payload, 0)
            self.roster_version = rv
        self._advance(t_ns)
        if self.cur_last_t >= 0 and t_ns - self.cur_last_t > _DECIMATED_GAP_NS:
            self.cur_decimated = True
        self.cur_last_t = t_ns
        self.cur_block = bytes(payload)

    def on_event(self, t_ns: int, level: int) -> None:
        if self.t_start < 0:
            return  # 段首块之前的事件计入第一个 bin 之前没有意义
        if self._bin_of(t_ns) != self.cur:
            return  # 迟到或超前的事件（与块不同 bin）只进 .evx
        self.cur_events[max(0, min(3, level))] += 1

    def on_lineage(self, restored_t: int, last_t: int) -> None:
        self.rerun.append((restored_t, last_t))

    def header(self) -> bytes:
        tr = list(self.tracks) + [0xFFFF] * (MAX_TRACKS - len(self.tracks))
        n_bins = len(self.bins) // self.bin_bytes if self.bin_bytes else 0
        return OVW_HDR.pack(OVW_MAGIC, OVW_VERSION, 1 if self.closed else 0, max(self.t_start, 0), self.bin_ns,
                            n_bins if self.closed else 0, self.bin_bytes, len(self.tracks), *tr, self.roster_version, self.segment)

    def close(self) -> None:
        self._emit()
        self.cur = -1
        self.closed = True


class _EventIndex:
    def __init__(self, segment: int) -> None:
        self.segment = segment
        self.recs = bytearray()
        self.epochs: list[int] = []
        self.t_start = -1
        self.mseq = 0
        self.dirty_from = 0
        self.closed = False

    def on_event(self, t_ns: int, epoch: int, level: int, marker: int, agent_no: int) -> None:
        if self.t_start < 0:
            self.t_start = t_ns
        self.recs += EVX_REC.pack(t_ns, self.mseq, max(0, min(3, level)), marker & 0xFF, agent_no & 0xFFFF)
        self.epochs.append(epoch)
        self.mseq += 1

    def on_lineage(self, new_epoch: int, restored_t: int) -> None:
        for i, ep in enumerate(self.epochs):
            if ep == new_epoch:
                continue
            off = i * EVX_REC.size
            t, _ms, _lv, mk, _a = EVX_REC.unpack_from(self.recs, off)
            if t >= restored_t and not mk & EVX_SUPERSEDED:
                self.recs[off + 13] = mk | EVX_SUPERSEDED
                self.dirty_from = min(self.dirty_from, off)

    def header(self) -> bytes:
        n = len(self.recs) // EVX_REC.size
        return EVX_HDR.pack(EVX_MAGIC, EVX_VERSION, 1 if self.closed else 0, EVX_REC.size, int(MARKER_RULES["version"]),
                            n if self.closed else 0, max(self.t_start, 0), self.segment, 0)


class SidecarBuilder:
    """一段录制的 `.ovw` 与 `.evx`；`on_message`/`on_metadata` 按 MCAP 写入顺序调用。"""

    def __init__(self, ovw_path: Path | None, evx_path: Path | None, *, segment: int, tracks: list[int],
                 roster_agent_of: dict[str, int] | None = None, bin_ns: int = 1_000_000_000) -> None:
        self.ovw_path = ovw_path
        self.evx_path = evx_path
        self.ovw = _Overview(ovw_path or Path("/dev/null"), segment, bin_ns, tracks)
        self.evx = _EventIndex(segment)
        self.agent_of = roster_agent_of if roster_agent_of is not None else {}
        self._written = {"ovw": 0, "evx": 0}

    # ------------------------------------------------------------ 输入（写入顺序）
    def on_message(self, topic: str, t_ns: int, data: bytes | memoryview) -> None:
        if topic == T_BLOCK:
            _ep, _rf, _dt, payload = split_prefix(data)
            self.ovw.on_block(t_ns, bytes(payload))
        elif topic == T_EVENT:
            ep, _rf, _dt, payload = split_prefix(data)
            try:
                ev = msgpack.unpackb(payload, raw=False, strict_map_key=False)
            except Exception:
                return
            if not isinstance(ev, dict):
                return
            kind = str(ev.get("kind", ""))
            sev = int(ev.get("severity", 0) or 0)
            uav = ev.get("uav")
            a = self.agent_of.get(uav, NO_AGENT) if isinstance(uav, str) else NO_AGENT
            self.evx.on_event(int(ev.get("t_sim_ns", t_ns)), ep, sev, marker_class(kind, sev, ev.get("data")), a)
            self.ovw.on_event(t_ns, sev)
        elif topic == "/sim/roster":
            _ep, _rf, _dt, payload = split_prefix(data)
            try:
                ros = msgpack.unpackb(payload, raw=False, strict_map_key=False)
                for e in ros.get("entries", []):
                    self.agent_of[str(e.get("id"))] = int(e.get("agent_no"))
            except Exception:
                return

    def on_lineage(self, epoch: int, restored_t: int, last_t: int) -> None:
        self.ovw.on_lineage(restored_t, last_t)
        self.evx.on_lineage(epoch & 0xFFFF, restored_t)

    # ------------------------------------------------------------ 输出
    def _write(self, path: Path | None, header: bytes, body: bytearray, dirty_from: int, key: str) -> None:
        if path is None:
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            os.pwrite(fd, header, 0)
            start = min(dirty_from, self._written[key])
            if start < len(body):
                os.pwrite(fd, bytes(body[start:]), len(header) + start)
            os.ftruncate(fd, len(header) + len(body))
        finally:
            os.close(fd)
        self._written[key] = len(body)

    def flush(self) -> None:
        self._write(self.ovw_path, self.ovw.header(), self.ovw.bins, self.ovw.dirty_from, "ovw")
        self.ovw.dirty_from = len(self.ovw.bins)
        self._write(self.evx_path, self.evx.header(), self.evx.recs, self.evx.dirty_from, "evx")
        self.evx.dirty_from = len(self.evx.recs)

    def close(self) -> None:
        self.ovw.close()
        self.evx.closed = True
        self.flush()

    def bytes(self) -> tuple[bytes, bytes]:
        """内存中的文件内容（测试与 reindex 对拍）。"""
        return self.ovw.header() + bytes(self.ovw.bins), self.evx.header() + bytes(self.evx.recs)
