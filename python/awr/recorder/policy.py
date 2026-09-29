"""录制策略（M12 §6.6.2、§6.10、FR-032、FR-033；ADR-040）。

- 整群块桶宽 `Δb = max(40 ms, rate × 20 ms)`（×1 为 25 Hz 仿真，倍速 > 2 时墙钟 ≤ 50 Hz）；
- 标记机 Full64 桶宽 `Δf = max(8 ms, rate × 8 ms)`（墙钟 ≤ 125 Hz）；
- 关键块周期 5 s（仿真）；`state_ext` 2 Hz、`safety` 5 Hz、传感器位姿 10 Hz；
- 标记机集合 = 剧本机体 `marked: true` 并上兴趣集 `marks`（各客户端选中机与 FPV 焦点机），总数 ≤ 16，剧本标记优先、其余按最近选中
  优先；N ≤ 50 时全部机体。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PolicyCfg

__all__ = ["MarkedSet", "RecordingPolicy", "load_scenario"]


@dataclass(frozen=True)
class RecordingPolicy:
    swarm_hz: float = 25.0
    full_hz: float = 125.0
    keyframe_every_ns: int = 5_000_000_000
    state_ext_hz: float = 2.0
    safety_hz: float = 5.0
    pose_hz: float = 10.0
    max_marked: int = 16
    full_all_if_n_le: int = 50

    @classmethod
    def from_cfg(cls, c: PolicyCfg) -> RecordingPolicy:
        return cls(swarm_hz=c.swarm_hz, full_hz=c.full_hz, keyframe_every_ns=int(c.keyframe_every_s * 1e9), state_ext_hz=c.state_ext_hz,
                   safety_hz=c.safety_hz, pose_hz=c.pose_hz, max_marked=c.max_marked, full_all_if_n_le=c.full_all_if_n_le)

    def swarm_bucket_ns(self, rate: float) -> int:
        base = round(1e9 / self.swarm_hz)  # 40 ms
        return max(base, int(rate * base / 2))  # rate × 20 ms

    def full_bucket_ns(self, rate: float) -> int:
        base = round(1e9 / self.full_hz)  # 8 ms
        return max(base, int(rate * base))


class MarkedSet:
    """标记机集合（agent_no）；`current()` 的结果只取决于剧本标记、兴趣集历史与名册，不取决于墙钟。"""

    def __init__(self, max_marked: int = 16, full_all_if_n_le: int = 50) -> None:
        self.max_marked = max_marked
        self.full_all_if_n_le = full_all_if_n_le
        self.scenario: list[int] = []
        self.recent: list[int] = []  # 最近选中优先
        self._scenario_ids: list[str] = []

    def set_scenario_ids(self, ids: list[str]) -> None:
        self._scenario_ids = list(ids)

    def resolve_scenario(self, roster_entries: list[dict[str, Any]]) -> None:
        by_id = {str(e.get("id")): int(e.get("agent_no")) for e in roster_entries}
        self.scenario = [by_id[i] for i in self._scenario_ids if i in by_id][: self.max_marked]

    def on_interest_marks(self, marks: list[int]) -> bool:
        """兴趣集 marks（≤ 16）；返回集合是否变化。新出现的标记排到最前。"""
        before = list(self.recent)
        for a in reversed([int(x) for x in marks]):
            if a in self.recent:
                self.recent.remove(a)
            self.recent.insert(0, a)
        del self.recent[4 * self.max_marked:]
        return before != self.recent

    def current(self, agent_nos: list[int]) -> frozenset[int]:
        present = set(agent_nos)
        if len(agent_nos) <= self.full_all_if_n_le:
            return frozenset(present)
        out: list[int] = [a for a in self.scenario if a in present]
        for a in self.recent:
            if len(out) >= self.max_marked:
                break
            if a in present and a not in out:
                out.append(a)
        return frozenset(out[: self.max_marked])


def load_scenario(root: Path | None = None) -> dict[str, Any] | None:
    """`AWR_SCENARIO`（剧本 id 或路径）指向的剧本 JSON；不存在时 None。

    INT-1：`AWR_SCENARIO_LOAD=0`（sim-core 不加载剧本，M10）时同样视为无剧本；`AWR_SCENARIO_PROFILE` 给出的 profile 中的
    `record`（例如 S1 的 ci profile 为 false）覆盖剧本顶层 `record`，与 sim-core 按 profile 合并的口径一致。"""
    sid = os.environ.get("AWR_SCENARIO", "")
    if not sid or os.environ.get("AWR_SCENARIO_LOAD", "1") == "0":
        return None
    base = root or Path(os.environ.get("AWR_ROOT", Path(__file__).resolve().parents[3]))
    for p in (Path(sid), base / "scenarios" / f"{sid}.json", base / "scenarios" / sid):
        if p.is_file():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(d, dict):
                return None
            prof = (d.get("profiles") or {}).get(os.environ.get("AWR_SCENARIO_PROFILE") or "")
            if isinstance(prof, dict) and "record" in prof:
                d = {**d, "record": bool(prof["record"])}
            return d
    return None
