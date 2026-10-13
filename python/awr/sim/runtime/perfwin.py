"""`state/sim-core/perf` 的窗口自报（M08-FR-224；ADR-091；AWR-04 §4.4.5 第 1 条；M08 §15.3.7）。

每 1 s【墙钟】一个窗口：`cpu_win_core` = 进程 CPU 秒（user + system，全部线程）÷ 墙钟秒；`rtf_win` = 仿真秒 ÷ 墙钟秒；
`cost_core_per_rate` = `cpu_win_core ÷ rtf_win` 的指数平均（系数 0.3，只在 `rtf_win ≥ 0.2` 的窗口更新，暂停与极慢倍率不污染估计）。
sandbox-pool 的倍速治理器据此按 `r_cap = min(10, 0.85 / c_i)` 授予倍速。`rss_anon_mb` 取 `/proc/self/status` 的 RssAnon。
"""

from __future__ import annotations

import contextlib

__all__ = ["ALPHA", "RTF_MIN", "PerfWindow", "rss_anon_mb"]

ALPHA = 0.3
RTF_MIN = 0.2


class PerfWindow:
    def __init__(self) -> None:
        self._prev: tuple[float, int, int] | None = None
        self.cpu_win_core = 0.0
        self.rtf_win = 0.0
        self.cost_core_per_rate: float | None = None

    def sample(self, cpu_s: float, wall_ns: int, t_sim_ns: int) -> None:
        """记一个窗口端点（每次 perf 发布调用一次）。仿真时钟回退（重置、恢复）时该窗口的 rtf 记 0。"""
        prev = self._prev
        self._prev = (float(cpu_s), int(wall_ns), int(t_sim_ns))
        if prev is None:
            return
        dw = (int(wall_ns) - prev[1]) / 1e9
        if dw <= 0:
            return
        self.cpu_win_core = max(0.0, (float(cpu_s) - prev[0]) / dw)
        ds = (int(t_sim_ns) - prev[2]) / 1e9
        self.rtf_win = max(0.0, ds) / dw
        if self.rtf_win >= RTF_MIN:
            x = self.cpu_win_core / self.rtf_win
            c = self.cost_core_per_rate
            self.cost_core_per_rate = x if c is None else c + ALPHA * (x - c)

    def fields(self) -> dict:
        c = self.cost_core_per_rate
        return {"cpu_win_core": round(self.cpu_win_core, 4), "rtf_win": round(self.rtf_win, 4),
                "cost_core_per_rate": None if c is None else round(c, 5)}


def rss_anon_mb() -> float | None:
    with contextlib.suppress(OSError, ValueError), open("/proc/self/status", encoding="ascii") as fh:
        for line in fh:
            if line.startswith("RssAnon:"):
                return round(int(line.split()[1]) / 1024.0, 1)
    return None
