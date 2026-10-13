"""token 签发与校验、principal 与角色依赖（AWR-17 §3.1、§3.2、§3.5；M11-FR-021、FR-056；ADR-027）。

token：`v1.<b64url(payload)>.<b64url(sig)>`，`sig = HMAC-SHA256(K_auth, "v1." + b64url(payload))`，12 h，绑定 run；
`K_x = HKDF-SHA256(AWR_SECRET, salt = run_id, info = "awr/<x>/v1")`（实现在 `awr.runtime.principal`，api 与 agent-runtime 共用）。
可信入口签名的 principal：`{principal_id, role, entry: "api", conn_id, seat, sig}`，`sig = HMAC-SHA256(K_entry, msgpack(其余字段) + cid)`，
生产者验签后才信任。token 只经 `Authorization: Bearer` 或 WS 子协议 `bearer.<token>` 携带（禁止 URL 与 cookie）。

D2（M11-FR-207、FR-208；AWR-04 §4.8、ADR-093；17 §17.3.1）：
- show-api 的 `TokenService.verify` 显式拒绝带 `scope` 的 token（沙盒 token 本就因 `K_auth` 不同而验签失败，302）；
- `SandboxTokenService`（sandbox-api）：主密钥 `var/sandbox/token.key`（32 B，0600，首次启动原子创建，重启后不变），
  `K_sbauth = HKDF(token.key, "awr-sandbox", "awr/sandbox-auth/v1")`，全站一把；`verify` 检查签名、过期与 scope（302），
  给出 sid 时再检查 sid 与 run（503）；签发（池在创建与续期时签，观摩链接 P1）同一格式，载荷追加 `scope`、`sid`；
- 进入 sim-core 的命令仍以该 run 的 `K_entry` 签名：每个 SessionContext 持有一个按 per-run secret 构造的 `TokenService`。
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awr.runtime.principal import Principal, TokenInvalid, decode_token, derive_key, encode_token, make_token_payload
from awr.runtime.principal import sign_principal as _sign
from awr.runtime.sbauth import (
    SandboxScopeError,
    decode_sandbox_token,
    load_or_create_key,
    make_sandbox_token,
    sandbox_auth_key,
)

from .settings import ApiSettings

__all__ = ["ROLE_RANK", "ApiPrincipal", "SandboxScopeError", "SandboxTokenService", "TokenService", "principal_id_from_hint"]

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
HINT_RE = re.compile(r"^(?:[0-9a-f]{32}|[A-Za-z2-7]{16,64})$")


def principal_id_from_hint(hint: str | None) -> tuple[str, str]:
    """(principal_id, principal_hint)：hint 为 16–64 位 base32（浏览器 localStorage 保存），缺省时生成 128 位随机数。
    D2-INT：另接受沙盒接口的 32 位十六进制形式（17 §17.3，`rest/sandbox.py` 同一正则），浏览器的沙盒、观看排队（R81）与
    页面 token 共用 localStorage 的同一提示，两种形式都映射为同一 `p-<hint 小写>`（d2-WP-13b-to-M15 第 10 条）。"""
    if hint is None or not HINT_RE.match(hint):
        hint = base64.b32encode(os.urandom(16)).decode("ascii").rstrip("=")
    return "p-" + hint.lower(), hint


@dataclass(frozen=True)
class ApiPrincipal:
    id: str
    role: str
    jti: str
    exp: int
    mode: str
    sid: str | None = None  # 沙盒 token 的会话（D2）
    scope: str | None = None  # "sandbox" 或 None（D1 token）

    def at_least(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK[role]


class TokenService:
    def __init__(self, settings: ApiSettings) -> None:
        self.s = settings
        self.k_auth = derive_key(settings.secret, settings.run_id, "auth")
        self.k_entry = derive_key(settings.secret, settings.run_id, "entry")
        self.k_confirm = derive_key(settings.secret, settings.run_id, "confirm")

    def issue(self, principal_id: str, role: str) -> tuple[str, dict[str, Any]]:
        payload = make_token_payload(principal_id, role, self.s.run_id, self.s.access_mode, now_s=int(time.time()))
        return encode_token(payload, self.k_auth), payload

    def verify(self, token: str) -> ApiPrincipal:
        """校验签名、过期与 run；失败抛 TokenInvalid（302）。"""
        p = decode_token(token, self.k_auth, run_id=self.s.run_id, now_s=int(time.time()))
        if p.get("scope") is not None:
            raise TokenInvalid("沙盒 token 不能用于本端点")  # FR-208 ②：show-api 显式拒绝带 scope 的 token
        role = p.get("role")
        if role not in ROLE_RANK:
            raise TokenInvalid("token 角色非法")
        return ApiPrincipal(str(p["sub"]), role, str(p.get("jti", "")), int(p.get("exp", 0)), str(p.get("mode", "")))

    def sign_principal(self, principal_id: str, role: str, cid: str, *, conn_id: str | None, seat: bool) -> dict[str, Any]:
        pr = Principal(principal_id, role, "api", conn_id, seat)  # type: ignore[arg-type]
        d = pr.fields()
        d["sig"] = _sign(pr, cid, self.k_entry)
        return d


class SandboxTokenService:
    """sandbox-api 的沙盒 token 验签与签发（FR-207）。`token_key` 给出时不读文件（测试）；否则读或创建 `key_path`。"""

    def __init__(self, key_path: Path | str | None = None, *, token_key: bytes | None = None, mode: str = "loopback") -> None:
        if token_key is None:
            if key_path is None:
                raise ValueError("SandboxTokenService 需要 key_path 或 token_key")
            token_key = load_or_create_key(key_path)
        self.k_sbauth = sandbox_auth_key(token_key)
        self.mode = mode

    def issue(self, principal_id: str, sid: str, *, exp_unix_s: int, role: str = "operator") -> tuple[str, dict[str, Any]]:
        return make_sandbox_token(self.k_sbauth, principal_id=principal_id, sid=sid, mode=self.mode,
                                  exp_unix_s=exp_unix_s, role=role)

    def verify(self, token: str, *, sid: str | None = None, grace_s: int = 0) -> ApiPrincipal:
        """签名、过期（grace_s 宽限）与 scope 失败抛 TokenInvalid（302）；sid 给出且不符抛 SandboxScopeError（503）。"""
        p = decode_sandbox_token(token, self.k_sbauth, sid=sid, now_s=int(time.time()), grace_s=grace_s)
        role = p.get("role")
        if role not in ("operator", "viewer"):
            raise TokenInvalid("沙盒 token 角色非法")
        return ApiPrincipal(str(p["sub"]), role, str(p.get("jti", "")), int(p.get("exp", 0)), str(p.get("mode", "")),
                            sid=str(p.get("sid")), scope="sandbox")
