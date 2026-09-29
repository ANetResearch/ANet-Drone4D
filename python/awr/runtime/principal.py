"""分钥、principal 签名与 token 编解码（AWR-17 §3.2、§9.4；ADR-027；M11 §7.4）。api 与 agent-runtime 共用。

- `K_x = HKDF-SHA256(ikm = AWR_SECRET, salt = run_id, info = "awr/<x>/v1")`，x ∈ {auth, lease, entry, confirm}。
- principal 签名：`sig = HMAC-SHA256(K_entry, msgpack(其余字段) + cid)`；其余字段按键名排序后打包，签名与验签两端一致。
- token：`v1.<b64url(payload)>.<b64url(sig)>`（无填充），`sig = HMAC-SHA256(K_auth, "v1." + b64url(payload))`；
  payload 为紧凑 JSON（键排序）。解码失败、签名不符、过期或不属于本 run 一律抛 TokenInvalid（302）。
- 比较一律常数时间（hmac.compare_digest）；本模块不记录 token 与口令原文。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass
from typing import Literal

import msgpack

__all__ = [
    "PURPOSES",
    "Principal",
    "TokenInvalid",
    "check_password",
    "decode_token",
    "derive_key",
    "encode_token",
    "make_token_payload",
    "new_admin_password",
    "new_secret",
    "sign_principal",
    "verify_principal",
]

Purpose = Literal["auth", "lease", "entry", "confirm"]
PURPOSES: tuple[str, ...] = ("auth", "lease", "entry", "confirm")
TOKEN_TTL_S = 43200  # 12 h（17 §3.2）
SECRET_BYTES = 32


class TokenInvalid(Exception):
    code = 302


@dataclass(frozen=True)
class Principal:
    principal_id: str
    role: Literal["viewer", "operator", "admin", "agent"]
    entry: Literal["api", "agent-runtime"]
    conn_id: str | None = None
    seat: bool = False

    def fields(self) -> dict:
        return asdict(self)


def new_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


def new_admin_password() -> str:
    """管理口令（URL 安全字符，24 字符）。"""
    return secrets.token_urlsafe(18)


def check_password(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()
    okm, t, i = b"", b"", 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


def derive_key(secret: bytes, run_id: str, purpose: Purpose) -> bytes:
    if purpose not in PURPOSES:
        raise ValueError(f"未知用途：{purpose}")
    return _hkdf(secret, run_id.encode("utf-8"), f"awr/{purpose}/v1".encode())


def _principal_bytes(p: Principal) -> bytes:
    d = p.fields()
    return msgpack.packb({k: d[k] for k in sorted(d)}, use_bin_type=True)


def sign_principal(p: Principal, cid: str, key: bytes) -> bytes:
    return hmac.new(key, _principal_bytes(p) + cid.encode("utf-8"), hashlib.sha256).digest()


def verify_principal(p: Principal, cid: str, sig: bytes, key: bytes) -> bool:
    return hmac.compare_digest(sign_principal(p, cid, key), bytes(sig))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_token(payload: dict, key: bytes) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(key, ("v1." + body).encode("ascii"), hashlib.sha256).digest()
    return f"v1.{body}.{_b64e(sig)}"


def make_token_payload(principal_id: str, role: str, run_id: str, mode: str, *, now_s: int, ttl_s: int = TOKEN_TTL_S) -> dict:
    return {"v": 1, "sub": principal_id, "role": role, "run": run_id, "mode": mode, "iat": int(now_s),
            "exp": int(now_s) + ttl_s, "jti": os.urandom(8).hex()}


def decode_token(token: str, key: bytes, *, run_id: str, now_s: int) -> dict:
    try:
        ver, body, sig = token.split(".")
    except ValueError:
        raise TokenInvalid("token 格式错误") from None
    if ver != "v1":
        raise TokenInvalid("token 版本不支持")
    try:
        want = hmac.new(key, ("v1." + body).encode("ascii"), hashlib.sha256).digest()
        got = _b64d(sig)
    except (ValueError, UnicodeEncodeError):
        raise TokenInvalid("token 编码错误") from None
    if not hmac.compare_digest(want, got):
        raise TokenInvalid("token 签名错误")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, UnicodeDecodeError):
        raise TokenInvalid("token 载荷无法解析") from None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise TokenInvalid("token 载荷版本不符")
    if payload.get("run") != run_id:
        raise TokenInvalid("token 不属于当前运行")
    if int(payload.get("exp", 0)) < now_s:
        raise TokenInvalid("token 已过期")
    return payload
