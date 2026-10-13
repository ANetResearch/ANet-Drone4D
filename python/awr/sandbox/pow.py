"""自托管工作量证明（hashcash，`alg = sha256-lz`；M17-FR-017；17 §17.3.5；ADR-093、Q-D2-03）。

- 题目 `{challenge_id, alg, prefix_b64, bits, expires_unix_ns}`：`prefix = 16 B 随机数 ‖ HMAC-SHA256(K_pow, challenge_id ‖
  地址前缀 ‖ expires)[:16]`（32 B），有效 `ttl_s`（120 s）【墙钟】，绑定地址前缀；
- 解：`0 ≤ nonce < 2^53`，`SHA-256(prefix ‖ str(nonce).encode("ascii"))` 的前导零位数 ≥ bits；
- 难度：缺省 `bits`（20），同一前缀 1 h 内每多一次触发 + 1，上限 `bits_hot`（24）；
- 校验：题目存在、未使用、未过期、前缀一致（并重算 HMAC）、前导零位数满足 → 记入已用表（至过期）；单次使用，不可重放。

`Pow` 只保存已发出的题目与已用题号（随 `state.json` 落盘，重启后已用题号仍拒绝）；求解函数 `solve` 供 SDK 与测试使用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .model import new_challenge_id, ns

__all__ = ["Challenge", "Pow", "leading_zero_bits", "solve"]

NONCE_MAX = 2**53
ISSUED_MAX = 4096
ISSUED_PER_PREFIX = 8


def leading_zero_bits(h: bytes) -> int:
    n = 0
    for b in h:
        if b == 0:
            n += 8
            continue
        return n + (8 - b.bit_length())
    return n


def solve(prefix: bytes, bits: int, *, start: int = 0, limit: int = NONCE_MAX) -> int | None:
    """穷举求解（测试与 SDK 的参考实现）。"""
    for n in range(start, min(limit, NONCE_MAX)):
        if leading_zero_bits(hashlib.sha256(prefix + str(n).encode("ascii")).digest()) >= bits:
            return n
    return None


@dataclass
class Challenge:
    challenge_id: str
    prefix: bytes
    bits: int
    exp: float
    prefix_h: str

    def wire(self) -> dict[str, Any]:
        return {"challenge_id": self.challenge_id, "alg": "sha256-lz",
                "prefix_b64": base64.b64encode(self.prefix).decode("ascii"), "bits": int(self.bits),
                "expires_unix_ns": ns(self.exp)}

    def to_json(self) -> dict[str, Any]:
        return {"id": self.challenge_id, "prefix_b64": base64.b64encode(self.prefix).decode("ascii"), "bits": self.bits,
                "exp": self.exp, "prefix_h": self.prefix_h}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Challenge:
        return cls(str(d["id"]), base64.b64decode(d["prefix_b64"]), int(d["bits"]), float(d["exp"]), str(d["prefix_h"]))


class Pow:
    def __init__(self, k_pow: bytes, *, ttl_s: float = 120.0, bits: int = 20, bits_max: int = 24) -> None:
        self.k_pow = k_pow
        self.ttl_s = float(ttl_s)
        self.bits = int(bits)
        self.bits_max = max(int(bits_max), int(bits))
        self.issued: OrderedDict[str, Challenge] = OrderedDict()
        self.used: dict[str, float] = {}           # challenge_id -> exp（UNIX 秒）

    def _mac(self, cid: str, prefix: str, exp: float) -> bytes:
        return hmac.new(self.k_pow, f"{cid}|{prefix}|{ns(exp)}".encode(), hashlib.sha256).digest()[:16]

    def difficulty(self, triggers_1h: int) -> int:
        """同一前缀 1 h 内的第 k 次触发（k ≥ 1）取 bits + (k − 1)，上限 bits_max。"""
        return min(self.bits_max, self.bits + max(0, int(triggers_1h) - 1))

    def issue(self, prefix: str, prefix_h: str, now: float, *, bits: int | None = None) -> Challenge:
        cid = new_challenge_id()
        while cid in self.issued or cid in self.used:
            cid = new_challenge_id()
        exp = now + self.ttl_s
        c = Challenge(cid, secrets.token_bytes(16) + self._mac(cid, prefix, exp), int(bits if bits is not None else self.bits),
                      exp, prefix_h)
        mine = [k for k, v in self.issued.items() if v.prefix_h == prefix_h]
        for k in mine[: max(0, len(mine) - ISSUED_PER_PREFIX + 1)]:
            self.issued.pop(k, None)
        self.issued[cid] = c
        while len(self.issued) > ISSUED_MAX:
            self.issued.popitem(last=False)
        return c

    def verify(self, sol: Any, prefix: str, prefix_h: str, now: float) -> str | None:
        """None 表示通过；否则返回 `why ∈ {required, invalid, expired}`（17 §17.3.5）。通过的题目记为已用。"""
        if not isinstance(sol, dict) or not sol.get("challenge_id"):
            return "required"
        cid = str(sol.get("challenge_id"))
        nonce = sol.get("nonce")
        if cid in self.used:
            return "invalid"          # 重放
        c = self.issued.get(cid)
        if c is None:
            return "expired"          # 未知或已清理（过期题目被清理后同样按过期处理）
        if now > c.exp:
            self.issued.pop(cid, None)
            return "expired"
        if c.prefix_h != prefix_h or not hmac.compare_digest(c.prefix[16:], self._mac(cid, prefix, c.exp)):
            return "invalid"          # 换前缀使用
        if not isinstance(nonce, int) or isinstance(nonce, bool) or not 0 <= nonce < NONCE_MAX:
            return "invalid"
        if leading_zero_bits(hashlib.sha256(c.prefix + str(nonce).encode("ascii")).digest()) < c.bits:
            return "invalid"
        self.issued.pop(cid, None)
        self.used[cid] = c.exp
        return None

    def prune(self, now: float) -> None:
        for k in [k for k, v in self.issued.items() if v.exp < now]:
            del self.issued[k]
        for k in [k for k, e in self.used.items() if e < now]:
            del self.used[k]

    def dump(self) -> dict[str, Any]:
        return {"issued": [c.to_json() for c in self.issued.values()], "used": dict(self.used)}

    def load(self, d: dict[str, Any] | None) -> None:
        if not isinstance(d, dict):
            return
        for item in d.get("issued") or []:
            try:
                c = Challenge.from_json(item)
            except (KeyError, TypeError, ValueError):
                continue
            self.issued[c.challenge_id] = c
        for k, v in (d.get("used") or {}).items():
            try:
                self.used[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
