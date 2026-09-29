"""token 签发与校验、principal 与角色依赖（AWR-17 §3.1、§3.2、§3.5；M11-FR-021、FR-056；ADR-027）。

token：`v1.<b64url(payload)>.<b64url(sig)>`，`sig = HMAC-SHA256(K_auth, "v1." + b64url(payload))`，12 h，绑定 run；
`K_x = HKDF-SHA256(AWR_SECRET, salt = run_id, info = "awr/<x>/v1")`（实现在 `awr.runtime.principal`，api 与 agent-runtime 共用）。
可信入口签名的 principal：`{principal_id, role, entry: "api", conn_id, seat, sig}`，`sig = HMAC-SHA256(K_entry, msgpack(其余字段) + cid)`，
生产者验签后才信任。token 只经 `Authorization: Bearer` 或 WS 子协议 `bearer.<token>` 携带（禁止 URL 与 cookie）。
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from awr.runtime.principal import Principal, TokenInvalid, decode_token, derive_key, encode_token, make_token_payload
from awr.runtime.principal import sign_principal as _sign

from .settings import ApiSettings

__all__ = ["ROLE_RANK", "ApiPrincipal", "TokenService", "principal_id_from_hint"]

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
HINT_RE = re.compile(r"^[A-Za-z2-7]{16,64}$")


def principal_id_from_hint(hint: str | None) -> tuple[str, str]:
    """(principal_id, principal_hint)：hint 为 16–64 位 base32（浏览器 localStorage 保存），缺省时生成 128 位随机数。"""
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
        role = p.get("role")
        if role not in ROLE_RANK:
            raise TokenInvalid("token 角色非法")
        return ApiPrincipal(str(p["sub"]), role, str(p.get("jti", "")), int(p.get("exp", 0)), str(p.get("mode", "")))

    def sign_principal(self, principal_id: str, role: str, cid: str, *, conn_id: str | None, seat: bool) -> dict[str, Any]:
        pr = Principal(principal_id, role, "api", conn_id, seat)  # type: ignore[arg-type]
        d = pr.fields()
        d["sig"] = _sign(pr, cid, self.k_entry)
        return d
