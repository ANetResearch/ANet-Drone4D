"""能力提供者协议与 Registry（M14 §6.4.1、§9.2；M14-FR-010、FR-011；移植 ANet `provider/registry.go`）。

- 能力 id 语法 `^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*){1,2}$`、≤ 64 B；禁止 `@` 后缀、大写、连字符、设备 id。
- `Registry.resolve`：先精确匹配，再按 `.` 逐级回退到父能力（`flight` 可服务 `flight.goto`）；同一 AID 内两个 provider 声明
  同一能力时拒绝注册（`ErrCapabilityConflict`）。注册 id 只允许裸能力 id 或 family（一段），带 `@` 的 id 拒绝（471）。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable
from typing import Protocol, runtime_checkable

from .types import CapabilityCall, Effect

__all__ = ["CAP_RE", "FAMILY_RE", "MAX_CAP_BYTES", "CapabilityError", "CapabilityProvider", "Registry", "check_capability_id",
           "valid_capability_id"]

CAP_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$")
FAMILY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_CAP_BYTES = 64


class CapabilityError(ValueError):
    """能力 id 不合语法或不在目录（471 CAPABILITY_UNKNOWN）。"""

    code = 471


def valid_capability_id(cap: str, families: Iterable[str] | None = None) -> bool:
    if not isinstance(cap, str) or len(cap.encode("utf-8")) > MAX_CAP_BYTES or not CAP_RE.fullmatch(cap):
        return False
    return families is None or cap.split(".", 1)[0] in set(families)


def check_capability_id(cap: str, families: Iterable[str] | None = None) -> str:
    if not valid_capability_id(cap, families):
        raise CapabilityError(cap)
    return cap


@runtime_checkable
class CapabilityProvider(Protocol):
    id: str

    def capabilities(self) -> list[str]: ...

    def describe(self) -> dict: ...

    def invoke(self, call: CapabilityCall) -> AsyncIterator[Effect]: ...

    def health(self) -> str | None: ...

    def invoke_timeout(self, capability: str) -> float | None: ...


class Registry:
    """每个 AID（daemon）一份：能力 → provider_id。"""

    def __init__(self) -> None:
        self.caps: dict[str, str] = {}

    def register(self, provider_id: str, caps: Iterable[str]) -> None:
        caps = list(caps)
        for c in caps:
            if not (CAP_RE.fullmatch(c) or FAMILY_RE.fullmatch(c)) or len(c.encode("utf-8")) > MAX_CAP_BYTES:
                raise CapabilityError(c)
        for c in caps:
            if c in self.caps and self.caps[c] != provider_id:
                raise ValueError(f"capability {c} held by {self.caps[c]}")
        for c in caps:
            self.caps[c] = provider_id

    def unregister(self, provider_id: str) -> None:
        for c in [c for c, p in self.caps.items() if p == provider_id]:
            del self.caps[c]

    def resolve(self, capability: str) -> str | None:
        c = capability
        while True:
            if c in self.caps:
                return self.caps[c]
            i = c.rfind(".")
            if i < 0:
                return None
            c = c[:i]

    def resolve_entry(self, capability: str) -> str | None:
        """解析到的注册条目本身（精确 id 或回退到的父能力）。"""
        c = capability
        while True:
            if c in self.caps:
                return c
            i = c.rfind(".")
            if i < 0:
                return None
            c = c[:i]

    def listed(self) -> list[str]:
        return sorted(self.caps)
