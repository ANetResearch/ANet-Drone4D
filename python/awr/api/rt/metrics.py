"""Gateway 指标：`perf/server`（1 Hz，msgpack `awr.perf.server.v1`）、600 s 窗口聚合（R60 `/api/sys/perf`）、事件循环延迟与
进程 CPU（M11-FR-085 至 FR-087；AWR-17 §4.3.12、§6.5；AWR-18 §9.4）。

- `api.cpu_pct`：`/proc/self/stat` 的 `utime + stime` 1 s 增量求得的单核百分比；
- `api.loop_lag_p99_ms`：10 Hz asyncio 采样任务的调度偏差（最近 10 s 的 p99）；
- `api.tick_age_p50_ms`、`tick_age_p99_ms`：本 1 s 内每 tick 取到的最新帧年龄（tick 时刻 − 帧 `t_pub_ns`）；
- `api.encodes_per_s`、`bytes_per_s`（全部连接的下行字节，含控制面）、`n_clients`、`tick_overruns`（累计）；
- `clients[]`：各连接的 `conn_id, fps, window, credit_skips, bucket_defers, slot_overwrites, ctrl_queue_hwm, srtt_ms, kbps`；
- `sim`：`rtf`、`step_*`、`catchup_saturated` 取环头部，其余取 `state/sim-core/perf`；`geo` 取 `state/sim-core/perf.geo`。
- 窗口：每 1 s 把数值字段展平（`api.*`、`sim.*`、`geo.*`，以及各连接字段在全部连接上的最大值 `clients.*`）放进 600 项环；
  `window(s)` 返回每个字段在窗口内的 `{p50, p95, p99, max, count}` 与 `t_from_unix_ns`、`t_to_unix_ns`（字符串）。
  环项按列存放（FX2-R3-gateway，D1-AC-29）：字段名只登记一次（列号表），每秒一行 `array('d')`（缺失为 NaN），
  每行约 0.3 KB；此前每秒一个以新建字符串为键的 dict（约 2.7 KB/行），600 行约 1.6 MB，在 soak 前 10 min 表现为 RSS 爬升。
"""

from __future__ import annotations

import math
import os
import time
from array import array
from collections import deque
from typing import Any

import numpy as np

__all__ = ["LagSampler", "Metrics", "ProcCpu"]

WINDOW_MAX_S = 600
CLIENT_FIELDS = ("fps", "window", "credit_skips", "bucket_defers", "slot_overwrites", "ctrl_queue_hwm", "srtt_ms", "kbps")


class ProcCpu:
    """本进程 CPU 单核百分比：`/proc/self/stat` 的 utime + stime（时钟滴答）按墙钟增量换算。"""

    def __init__(self) -> None:
        self.hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._last = self._read()

    def _read(self) -> tuple[float, float] | None:
        try:
            with open("/proc/self/stat", "rb") as f:
                parts = f.read().rsplit(b")", 1)[1].split()
            return (int(parts[11]) + int(parts[12])) / self.hz, time.monotonic()
        except (OSError, IndexError, ValueError):
            return None

    def pct(self) -> float:
        cur = self._read()
        prev, self._last = self._last, cur
        if cur is None or prev is None or cur[1] <= prev[1]:
            return 0.0
        return round(100.0 * (cur[0] - prev[0]) / (cur[1] - prev[1]), 2)


class LagSampler:
    """事件循环延迟：10 Hz 任务 `await asyncio.sleep(0.1)` 的实际唤醒偏差（ms），保留最近 100 个样本。"""

    def __init__(self) -> None:
        self.samples: deque[float] = deque(maxlen=100)

    def add(self, lag_ms: float) -> None:
        self.samples.append(max(0.0, lag_ms))

    def p99(self) -> float:
        if not self.samples:
            return 0.0
        return round(float(np.percentile(np.fromiter(self.samples, np.float64), 99)), 3)


class Metrics:
    def __init__(self) -> None:
        self.cpu = ProcCpu()
        self.lag = LagSampler()
        self.tick_ages: list[float] = []
        self.tick_ms: list[float] = []  # on_tick 耗时（不含 send）
        self.ring: deque[tuple[int, array]] = deque(maxlen=WINDOW_MAX_S)  # (t_unix_ns, 按列号的数值行，缺失为 NaN)
        self.cols: dict[str, int] = {}  # 字段名 -> 列号（只增，新字段追加在末尾）
        self._enc_mark = 0
        self._bytes_mark = 0
        self._t_mark = time.monotonic()
        self.last: dict[str, Any] = {}
        self.counters: dict[str, int] = {"epoch_mismatch": 0, "header_retries": 0}

    def on_tick(self, age_ms: float | None, tick_ms: float) -> None:
        if age_ms is not None:
            self.tick_ages.append(age_ms)
        self.tick_ms.append(tick_ms)
        if len(self.tick_ms) > 600:
            del self.tick_ms[:300]
        if len(self.tick_ages) > 600:
            del self.tick_ages[:300]

    def build(self, *, encodes_total: int, bytes_total: int, n_clients: int, tick_overruns: int,
              clients: list[dict[str, Any]], sim: dict[str, Any], geo: dict[str, Any] | None) -> dict[str, Any]:
        now = time.monotonic()
        dt = max(1e-3, now - self._t_mark)
        ages = np.asarray(self.tick_ages, np.float64) if self.tick_ages else np.zeros(1)
        ticks = np.asarray(self.tick_ms, np.float64) if self.tick_ms else np.zeros(1)
        api = {"cpu_pct": self.cpu.pct(), "loop_lag_p99_ms": self.lag.p99(),
               "tick_age_p50_ms": round(float(np.percentile(ages, 50)), 3),
               "tick_age_p99_ms": round(float(np.percentile(ages, 99)), 3), "tick_overruns": int(tick_overruns),
               "encodes_per_s": round((encodes_total - self._enc_mark) / dt, 2),
               "bytes_per_s": round((bytes_total - self._bytes_mark) / dt, 1), "n_clients": int(n_clients),
               "tick_p99_ms": round(float(np.percentile(ticks, 99)), 3),
               "epoch_mismatch": int(self.counters["epoch_mismatch"])}
        self._enc_mark, self._bytes_mark, self._t_mark = encodes_total, bytes_total, now
        self.tick_ages.clear()
        self.tick_ms.clear()
        msg: dict[str, Any] = {"api": api, "clients": clients, "sim": sim}
        if geo:
            msg["geo"] = geo
        self.last = msg
        self._record(msg)
        return msg

    def _col(self, key: str) -> int:
        i = self.cols.get(key)
        if i is None:
            i = self.cols[key] = len(self.cols)
        return i

    def _record(self, msg: dict[str, Any]) -> None:
        vals: list[tuple[int, float]] = []
        for sec in ("api", "sim", "geo"):
            for k, v in (msg.get(sec) or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.append((self._col(f"{sec}.{k}"), float(v)))
        for k in CLIENT_FIELDS:
            cv = [float(c[k]) for c in msg.get("clients") or [] if isinstance(c.get(k), (int, float))]
            if cv:
                vals.append((self._col(f"clients.{k}"), max(cv)))
        row = array("d", [math.nan]) * len(self.cols)
        for i, v in vals:
            row[i] = v
        self.ring.append((time.time_ns(), row))

    def window(self, seconds: int) -> dict[str, Any]:
        t_to = time.time_ns()
        t_from = t_to - int(seconds) * 1_000_000_000
        rows = [r for t, r in self.ring if t >= t_from]
        fields: dict[str, dict[str, float | int]] = {}
        for k in sorted(self.cols):
            i = self.cols[k]
            a = np.asarray([r[i] for r in rows if len(r) > i], np.float64)
            a = a[~np.isnan(a)]
            if a.size == 0:
                continue
            p = np.percentile(a, [50, 95, 99])
            fields[k] = {"p50": round(float(p[0]), 4), "p95": round(float(p[1]), 4), "p99": round(float(p[2]), 4),
                         "max": round(float(a.max()), 4), "count": int(a.size)}
        return {"window_s": int(seconds), "t_from_unix_ns": str(t_from), "t_to_unix_ns": str(t_to), "fields": fields}
