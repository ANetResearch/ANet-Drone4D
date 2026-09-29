"""awr.rt.v1 控制面消息（JSON 文本帧）的构造与校验辅助（AWR-17 §6.3、§7.2、§8；`packages/contracts/rt/ops.schema.json`）。

信封字段沿用 r27 冻结的 camelCase（`sessionId`、`tickHz`、`dataStart_ns` 等），业务载荷（args、data、effect、detail）为
snake_case。JSON 以紧凑分隔符编码（M11 §9.3）；UNIX 纳秒字段以十进制字符串传输（17 §1.3）。二进制帧（TIME、BATCH）
一律经生成的 `awr.contracts.frame` 编码，本包不写格式串（M11 §9.5 第 2 条）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from awr.contracts.reasons import info as reason_info

__all__ = ["CALL_ID_RE", "PROTOCOL", "error_msg", "jdump", "result_msg", "status_msg"]

PROTOCOL = "awr.rt.v1"
CALL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{4,96}$")


def jdump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def error_msg(code: int, op: str, rid: Any = None, message: str | None = None) -> dict:
    ri = reason_info(code)
    ref: dict[str, Any] = {"op": op}
    if rid is not None:
        ref["id"] = rid
    return {"op": "error", "code": int(code), "name": ri.name, "message": message or ri.message_zh, "ref": ref}


def status_msg(sid: str, level: str, message: str, *, source: str | None = None, code: int | None = None) -> dict:
    m: dict[str, Any] = {"op": "status", "id": sid, "level": level, "message": message}
    if source is not None:
        m["source"] = source
    if code is not None:
        m["code"] = int(code)
    return m


def result_msg(cid: str, status: str, code: int = 0, *, effect: dict | None = None, final: bool | None = None,
               detail: Any = None, data: dict | None = None, duplicate: bool = False, dup_of: str | None = None,
               warnings: list[str] | None = None, retry_after_ms: int | None = None) -> dict:
    """`result` 帧（17 §7.2 线上规则 1：code ≠ 0 时带 reason、message、remedy；终态带 final）。"""
    m: dict[str, Any] = {"op": "result", "id": cid, "status": status, "code": int(code)}
    if code:
        ri = reason_info(code)
        m["reason"] = ri.name
        m["message"] = ri.message_zh
        m["remedy"] = ri.remedy_zh
    if detail is not None:
        m["detail"] = detail
    if final is not None:
        m["final"] = bool(final)
    if duplicate:
        m["duplicate"] = True
    if dup_of:
        m["dup_of"] = dup_of
    if warnings:
        m["warnings"] = list(warnings)
    if retry_after_ms is not None:
        m["retry_after_ms"] = int(retry_after_ms)
    if effect is not None:
        m["effect"] = _effect(effect)
    if data is not None:
        m["data"] = data
    return m


_EFFECT_KEYS = ("status", "verify_trust", "auth_trust", "simulated", "native_ack", "protocol", "requested",
                "observed_state", "latency_ms", "quirk", "message", "metrics", "artifacts")


def _effect(e: dict) -> dict:
    """只保留 effect.schema.json 中的字段（additionalProperties: false）。"""
    return {k: e[k] for k in _EFFECT_KEYS if k in e}
