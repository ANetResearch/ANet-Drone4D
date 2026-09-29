"""低频状态的"关键块 + 增量块"（M12 §6.6.3、§6.7.6；AWR-16 §13.4；契约 `rec/blocks.schema.json`）。

写入侧 `KeyDeltaBlocker`：输入为总线批 `[[agent_no, item], ...]`（`state/sim-core/ext` 2 Hz 全机、`state/sim-core/safety`
10 Hz 变化行），合并为每机最新值；按桶（2 Hz、5 Hz，仿真时间）输出一条块消息：段首第一次与每 5 s 仿真时间写全量关键块
（RecPrefix8 `rflags.KEYFRAME`），其余时刻只写与上次写出不同的机体（`rflags.DELTA`），无变化不写。块载荷为
`{roster_version, keyframe, items: {"<agent_no>": item}}`（键为 agent_no 的十进制文本，回放时无需名册即可还原）。

读取侧 `merge_blocks`：最后一个关键块 + 其后不晚于 t 的增量块 → 每机最新值，`pack_batch` 还原为与实时总线相同的
`[[agent_no, item], ...]` 批（按 agent_no 升序）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import msgpack

__all__ = ["KeyDeltaBlocker", "merge_blocks", "pack_batch", "parse_batch"]


def _packb(x: Any) -> bytes:
    return msgpack.packb(x, use_bin_type=True)


def parse_batch(raw: bytes | memoryview) -> dict[int, Any]:
    """总线批 → {agent_no: item}；也接受 {agent_no: item} 与 {"items": ...} 形式。"""
    obj = msgpack.unpackb(raw, raw=False, strict_map_key=False)
    out: dict[int, Any] = {}
    if isinstance(obj, dict) and "items" in obj:
        obj = obj["items"]
    if isinstance(obj, dict):
        for k, v in obj.items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                continue
    elif isinstance(obj, list):
        for pair in obj:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                try:
                    out[int(pair[0])] = pair[1]
                except (TypeError, ValueError):
                    continue
    return out


def pack_batch(items: dict[int, Any]) -> bytes:
    return _packb([[a, items[a]] for a in sorted(items)])


class KeyDeltaBlocker:
    def __init__(self, hz: float, keyframe_every_ns: int) -> None:
        self.bucket_ns = max(1, int(1e9 / hz))
        self.key_ns = int(keyframe_every_ns)
        self.reset()

    def reset(self) -> None:
        """段首与纪元变化：下一次输出为关键块。"""
        self.latest: dict[int, bytes] = {}
        self.latest_obj: dict[int, Any] = {}
        self.written: dict[int, bytes] = {}
        self.next_t = -1
        self.next_key_t = -1
        self.roster_version = 0

    def offer(self, items: dict[int, Any], roster_version: int = 0) -> None:
        for a, v in items.items():
            self.latest_obj[a] = v
            self.latest[a] = _packb(v)
        if roster_version:
            self.roster_version = roster_version

    def poll(self, t_ns: int) -> tuple[bytes, bool] | None:
        """在帧时刻 t 检查是否到期：返回 (msgpack 载荷, 是否关键块) 或 None。只取决于 t 与已合并的值（确定性）。"""
        if not self.latest or t_ns < self.next_t:
            return None
        self.next_t = (t_ns // self.bucket_ns + 1) * self.bucket_ns
        key = self.next_key_t < 0 or t_ns >= self.next_key_t
        if key:
            self.next_key_t = (t_ns // self.key_ns + 1) * self.key_ns
            items = {str(a): self.latest_obj[a] for a in sorted(self.latest)}
            self.written = dict(self.latest)
        else:
            changed = [a for a in sorted(self.latest) if self.written.get(a) != self.latest[a]]
            if not changed:
                return None
            items = {str(a): self.latest_obj[a] for a in changed}
            for a in changed:
                self.written[a] = self.latest[a]
        return _packb({"roster_version": int(self.roster_version), "keyframe": key, "items": items}), key


def merge_blocks(blocks: Iterable[bytes | memoryview]) -> dict[int, Any]:
    """按时间顺序的块载荷（第一条应为关键块）合并为 {agent_no: item}。"""
    out: dict[int, Any] = {}
    for b in blocks:
        obj = msgpack.unpackb(b, raw=False, strict_map_key=False)
        if not isinstance(obj, dict):
            continue
        if obj.get("keyframe"):
            out = {}
        for k, v in (obj.get("items") or {}).items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                continue
    return out
