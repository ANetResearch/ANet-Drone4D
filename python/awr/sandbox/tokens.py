"""池的密钥与令牌（M17-FR-050、FR-012、FR-017；17 §17.3.1、§17.3.2、§17.3.5、§17.10.1）。

| 密钥 | 派生 | 用途 |
|---|---|---|
| `token.key`（`var/sandbox/token.key`，32 B，0600） | `K_sbauth = HKDF(token.key, "awr-sandbox", "awr/sandbox-auth/v1")`（`awr.runtime.sbauth`，与 sandbox-api 逐字节一致） | 沙盒 token 签名 |
| 同上 | `K_pow = HKDF(token.key, "awr-sandbox", "awr/sandbox-pow/v1")`（17 §17.3.5） | 工作量证明题目的前缀绑定 |
| 同上 | `K_state = HKDF(token.key, "awr-sandbox", "awr/sandbox-state/v1")` | 状态文件与审计中的前缀散列 |
| `pool.secret`（32 B，0600） | `K_pool`（`awr.runtime.sbauth.pool_key`） | `sys/session/*` 请求验签 |

删除 `token.key` 即令全部沙盒 token 失效（运维轮换），前缀散列随之改变（配额计数重新开始）。

D2.1：内部档会话的沙盒 token 载荷增加 `tier: "internal"`（17 §17.20.5；公开档不带该字段，载荷与 D2 逐字节相同）。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from awr.runtime.principal import _hkdf, encode_token
from awr.runtime.sbauth import (
    POOL_SECRET_NAME,
    TOKEN_KEY_NAME,
    load_or_create_key,
    make_sandbox_token,
    pool_key,
    sandbox_auth_key,
)

__all__ = ["Keys", "ticket_token_hash", "ticket_token_ok"]


def ticket_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ticket_token_ok(token: str | None, sha: str) -> bool:
    if not isinstance(token, str) or not token.startswith("q1."):
        return False
    return hmac.compare_digest(ticket_token_hash(token), sha)


@dataclass(frozen=True)
class Keys:
    k_sbauth: bytes
    k_pow: bytes
    k_state: bytes
    k_pool: bytes
    mode: str = "loopback"

    @classmethod
    def load(cls, state_dir: Path, *, mode: str = "loopback") -> Keys:
        tk = load_or_create_key(Path(state_dir) / TOKEN_KEY_NAME)
        ps = load_or_create_key(Path(state_dir) / POOL_SECRET_NAME)
        return cls.derive(tk, ps, mode=mode)

    @classmethod
    def derive(cls, token_key: bytes, pool_secret: bytes, *, mode: str = "loopback") -> Keys:
        return cls(k_sbauth=sandbox_auth_key(token_key), k_pow=_hkdf(token_key, b"awr-sandbox", b"awr/sandbox-pow/v1"),
                   k_state=_hkdf(token_key, b"awr-sandbox", b"awr/sandbox-state/v1"), k_pool=pool_key(pool_secret), mode=mode)

    def issue(self, principal_id: str, sid: str, exp_unix_s: float, *, tier: str = "public") -> tuple[str, dict]:
        """签发沙盒 token（`exp` = 当前寿命截止；17 §17.3.1）；内部档载荷另带 `tier`。"""
        tok, p = make_sandbox_token(self.k_sbauth, principal_id=principal_id, sid=sid, mode=self.mode,
                                    exp_unix_s=int(exp_unix_s))
        if tier == "public":
            return tok, p
        p = {**p, "tier": tier}
        return encode_token(p, self.k_sbauth), p
