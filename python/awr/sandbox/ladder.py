"""降级阶梯 L0 至 L4（M17-FR-021、FR-022；AWR-04 §4.4.6；M17 PRD §6.6.6）。纯逻辑，每 1 s 由池以一次读数调用。

| 级 | 判据（缺省值见 `sandbox.ladder`） |
|---|---|
| L4 | 沙盒组 anon + shmem > `l4_anon_mb`，或 `memory.pressure some avg10 > l4_psi` 持续 `l4_hold_s` |
| L3 | 任一沙盒 RTF < `l3_rtf` 持续 `l3_hold_s`（授予回调窗口内不计），或共享展示 RTF < `l3_show_rtf` 持续 `l3_show_hold_s` |
| L2 | 沙盒组或会话父组 `sandbox/sb/`（D2-FX-R2：其 `cpu.max` 先于沙盒组生效）的 `nr_throttled` 增量 > `l2_throttle` × 周期增量（10 s 窗口）、`cpu.pressure some avg10 > l2_psi`，或治理器预算用尽；解除需判据连续 `l2_exit_s` 不满足 |
| L1 | `N_live ≥ N_max` 或有世界准入不满足 |
| L0 | 正常 |

动作由 broker 执行：L2 起全部快进会话降为 ×1、暂停授予新快进；L3、L4 停止创建并按超额分回收（每 `reclaim_interval_s` 至多一次，
回收前告知 `notice_s`）。共享展示永不在回收之列。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

__all__ = ["Ladder", "LadderInput", "SessionLoad", "reclaim_score"]


@dataclass
class SessionLoad:
    sid: str
    c_i: float
    granted: float
    rtf: float
    anon_mb: float
    anon_expected_mb: float
    ws: int
    idle: bool
    in_grant_window: bool


@dataclass
class LadderInput:
    now: float
    cpu_stat: dict | None = None          # 沙盒组 cpu.stat（nr_periods、nr_throttled）
    cpu_stat_sb: dict | None = None       # 会话父组 sandbox/sb/ 的 cpu.stat（D2-FX-R2）
    psi_cpu: float | None = None          # 沙盒组 cpu.pressure some avg10（%）
    mem_mb: float | None = None           # 沙盒组 anon + shmem（MB）
    psi_mem: float | None = None          # 沙盒组 memory.pressure some avg10（%）
    budget_exhausted: bool = False
    sessions: list[SessionLoad] = field(default_factory=list)
    show_rtf: float | None = None
    full: bool = False


def reclaim_score(x: SessionLoad, level: int, b_sim: float, n_live: int) -> float:
    """超额分 `e_i`（AWR-04 §4.4.6 L3 行）；L4 以内存项为主；超额会话先于无连接的空闲会话。"""
    share = b_sim / max(1, n_live) if b_sim > 0 else 1.0
    over_mem = max(0.0, x.anon_mb - x.anon_expected_mb)
    e = x.c_i * x.granted / max(share, 1e-6) + 10.0 * max(0.0, 0.95 - x.rtf) + over_mem / 50.0
    if level >= 4:
        e += over_mem / 10.0
    return e + (-0.5 if (x.ws == 0 and x.idle) else 0.0)


class Ladder:
    WINDOW_S = 10.0

    def __init__(self, cfg: object) -> None:
        self.c = cfg                          # SbLadder
        self.level = 0
        self.since: dict[str, float] = {}
        self.l2_clear_since: float | None = None
        self.cpu_hist: deque[tuple[float, int, int]] = deque()
        self.cpu_hist_sb: deque[tuple[float, int, int]] = deque()
        self.last: dict[str, float | bool | None] = {}

    def _held(self, key: str, cond: bool, now: float, hold_s: float) -> bool:
        if not cond:
            self.since.pop(key, None)
            return False
        t0 = self.since.setdefault(key, now)
        return now - t0 >= hold_s

    def _throttle_ratio(self, now: float, cs: dict | None, hist: deque | None = None) -> float | None:
        h = self.cpu_hist if hist is None else hist
        if not cs or "nr_periods" not in cs:
            return None
        h.append((now, int(cs.get("nr_periods", 0)), int(cs.get("nr_throttled", 0))))
        while len(h) > 2 and now - h[0][0] > self.WINDOW_S:
            h.popleft()
        _t0, p0, h0 = h[0]
        dp = h[-1][1] - p0
        if dp <= 0:
            return 0.0
        return max(0, h[-1][2] - h0) / dp

    def tick(self, inp: LadderInput) -> int:
        c = self.c
        now = inp.now
        thr = self._throttle_ratio(now, inp.cpu_stat)
        thr_sb = self._throttle_ratio(now, inp.cpu_stat_sb, self.cpu_hist_sb)
        if thr_sb is not None:
            thr = thr_sb if thr is None else max(thr, thr_sb)
        l4 = (inp.mem_mb is not None and inp.mem_mb > c.l4_anon_mb) or \
            self._held("l4psi", inp.psi_mem is not None and inp.psi_mem > c.l4_psi, now, c.l4_hold_s)
        rtf_low = False
        for s in inp.sessions:
            low = (not s.in_grant_window) and s.rtf < c.l3_rtf
            rtf_low = self._held(f"rtf:{s.sid}", low, now, c.l3_hold_s) or rtf_low
        live = {f"rtf:{s.sid}" for s in inp.sessions}
        for k in [k for k in self.since if k.startswith("rtf:") and k not in live]:
            self.since.pop(k, None)
        show_low = self._held("show", inp.show_rtf is not None and inp.show_rtf < c.l3_show_rtf, now, c.l3_show_hold_s)
        l3 = rtf_low or show_low
        l2_now = (thr is not None and thr > c.l2_throttle) or (inp.psi_cpu is not None and inp.psi_cpu > c.l2_psi) or \
            inp.budget_exhausted
        if l2_now:
            self.l2_clear_since = None
        elif self.level >= 2 and self.l2_clear_since is None:
            self.l2_clear_since = now
        l2_hold = self.level >= 2 and self.l2_clear_since is not None and now - self.l2_clear_since < c.l2_exit_s
        l2 = l2_now or l2_hold
        level = 4 if l4 else 3 if l3 else 2 if l2 else 1 if inp.full else 0
        if level < 2:
            self.l2_clear_since = None
        self.last = {"throttle": thr, "psi_cpu": inp.psi_cpu, "mem_mb": inp.mem_mb, "psi_mem": inp.psi_mem,
                     "show_rtf": inp.show_rtf, "budget_exhausted": inp.budget_exhausted}
        self.level = level
        return level
