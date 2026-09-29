"""MockHub（M14 §6.12.1；M14-FR-044；语义来自 ANetHub `FindByCapability`、`/hub-register`，d05 §2.7）。

- 能力索引 `{capability: sorted[(agent_no, aid)]}`；每 agent ≤ 256 个能力，每个 id ≤ 256 B（本项目能力 id ≤ 64 B）。
- find 模式：精确（`thermal.imaging`）、尾 `*` 字节前缀（`thermal.*`）、逗号 OR（`thermal.*,rgb.zoom`）；区分大小写。
- hub 不知道健康状态（健康由 `task.quote` 判定，§1.4）。
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["MAX_CAPS_PER_AGENT", "MAX_CAP_ID_BYTES", "HubError", "MockHub", "match_pattern"]

MAX_CAPS_PER_AGENT = 256
MAX_CAP_ID_BYTES = 256


class HubError(ValueError):
    pass


def match_pattern(pattern: str, cap: str) -> bool:
    for tok in pattern.split(","):
        if not tok:
            continue
        if tok.endswith("*"):
            if cap.startswith(tok[:-1]):
                return True
        elif tok == cap:
            return True
    return False


class MockHub:
    def __init__(self) -> None:
        self.agents: dict[str, tuple[int, tuple[str, ...]]] = {}
        self.stats = {"finds": 0, "registers": 0}

    def register(self, aid: str, agent_no: int, caps: Iterable[str]) -> None:
        caps = tuple(dict.fromkeys(caps))
        if len(caps) > MAX_CAPS_PER_AGENT:
            raise HubError("too many capabilities")
        for c in caps:
            if not c or len(c.encode("utf-8")) > MAX_CAP_ID_BYTES:
                raise HubError(f"bad capability id {c!r}")
        self.agents[aid] = (int(agent_no), caps)
        self.stats["registers"] += 1

    def unregister(self, aid: str) -> None:
        self.agents.pop(aid, None)

    def find(self, pattern: str) -> list[tuple[int, str]]:
        self.stats["finds"] += 1
        out = [(no, aid) for aid, (no, caps) in self.agents.items() if any(match_pattern(pattern, c) for c in caps)]
        return sorted(out)
