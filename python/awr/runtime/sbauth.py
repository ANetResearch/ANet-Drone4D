"""沙盒鉴权原语：持久化密钥文件、`K_sbauth`、沙盒 token、池控制面签名（AWR-04 §4.8、ADR-093；AWR-17 §17.3.1、
§17.10.1；M11-FR-207；M17 PRD §7.5、§7.7）。sandbox-api（M11）与 sandbox-pool（M17）共用本模块，二者的派生与编码因此逐字节一致。

- 密钥文件（`var/sandbox/{token.key, pool.secret}`，32 B，0600）：`load_or_create_key` 以 `O_CREAT | O_EXCL` 原子创建，并发的
  两个进程（池与 sandbox-api 同时首次启动）读到同一份；已存在即只读，重启后不变（token 跨重启有效，AWR-04 §4.7 第 6 条）。
- `K_sbauth = HKDF-SHA256(ikm = token.key, salt = "awr-sandbox", info = "awr/sandbox-auth/v1")`：全站一把（17 §17.3.1，
  AWR-04 附录 B 第 65 行的裁决；M17 PRD §7.7 的"salt = sid"写法已被该裁决取代）。
- 沙盒 token：格式与 D1 相同（`v1.<b64url(payload)>.<b64url(sig)>`，`awr.runtime.principal.encode_token`），载荷在
  `make_token_payload` 之上追加 `scope = "sandbox"` 与 `sid`（`run = sid`）；`exp` = 会话寿命截止。
  `decode_sandbox_token` 的检查顺序按 17 §17.3.1：签名（302）→ 过期（302，可给宽限）→ scope、sid、run（503）。
- 池控制面：principal `svc:sandbox-api`，`K_pool = HKDF-SHA256(pool.secret, salt = "_sandbox", info = "awr/pool/v1")`；请求
  `{v, cid, principal, ts, args, sig}`，`sig = hex(HMAC-SHA256(K_pool, canonical_msgpack({op, v, cid, principal, ts, args})))`，
  canonical 为键递归排序后的 msgpack；`|now − ts| ≤ 5 s`（M17 PRD §7.5）。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import msgpack

from .principal import TokenInvalid, _b64d, _hkdf, encode_token, make_token_payload

__all__ = [
    "POOL_PRINCIPAL",
    "POOL_WINDOW_S",
    "SandboxScopeError",
    "canonical_pack",
    "decode_sandbox_token",
    "load_or_create_key",
    "make_sandbox_token",
    "peek_token_payload",
    "pool_key",
    "sandbox_auth_key",
    "sign_pool_request",
    "verify_pool_request",
]

KEY_BYTES = 32
POOL_PRINCIPAL = "svc:sandbox-api"
POOL_WINDOW_S = 5.0
TOKEN_KEY_NAME = "token.key"
POOL_SECRET_NAME = "pool.secret"


class SandboxScopeError(TokenInvalid):
    """签名有效但不属于本会话（scope、sid、run 不符）：沙盒端点返回 403 `503 SANDBOX_SCOPE`（17 §17.3.1）。"""

    code = 503


# ---------------------------------------------------------------- 密钥文件
def load_or_create_key(path: Path | str, nbytes: int = KEY_BYTES) -> bytes:
    """读 32 B 密钥文件；不存在时以 0600 原子创建（O_EXCL：并发首启时只有一方写入，另一方读到同一份）。

    文件长度不足 nbytes 视为损坏，抛 ValueError（不静默重建：重建会使全部沙盒 token 失效，必须由运维显式删除）。"""
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        p.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(p.parent, 0o700)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_bytes(nbytes))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, p)  # 目标已存在时失败（并发首启的另一方已写入）：以已存在者为准
        except FileExistsError:
            pass
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        data = p.read_bytes()
    if len(data) < nbytes:
        raise ValueError(f"密钥文件长度不足 {nbytes} 字节：{p}")
    return data[:nbytes]


def sandbox_auth_key(token_key: bytes) -> bytes:
    """`K_sbauth`（17 §17.3.1）。"""
    return _hkdf(token_key, b"awr-sandbox", b"awr/sandbox-auth/v1")


def pool_key(pool_secret: bytes) -> bytes:
    """`K_pool`（17 §17.10.1；M17 PRD §7.5）。"""
    return _hkdf(pool_secret, b"_sandbox", b"awr/pool/v1")


# ---------------------------------------------------------------- 沙盒 token
def make_sandbox_token(k_sbauth: bytes, *, principal_id: str, sid: str, mode: str, exp_unix_s: int,
                       role: str = "operator", now_s: int | None = None) -> tuple[str, dict[str, Any]]:
    """签发沙盒 token（池在创建、续期时调用；sandbox-api 的测试与观摩链接 P1 也用它）。"""
    now = int(time.time()) if now_s is None else int(now_s)
    p = make_token_payload(principal_id, role, sid, mode, now_s=now, ttl_s=max(0, int(exp_unix_s) - now))
    p["exp"] = int(exp_unix_s)
    p["scope"] = "sandbox"
    p["sid"] = sid
    return encode_token(p, k_sbauth), p


def peek_token_payload(token: str) -> dict[str, Any] | None:
    """不验签地读出载荷（只用于选择密钥、判断 scope；任何授权判定都必须经 decode）。"""
    try:
        import json

        ver, body, _sig = token.split(".")
        if ver != "v1":
            return None
        d = json.loads(_b64d(body))
        return d if isinstance(d, dict) else None
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None


def decode_sandbox_token(token: str, k_sbauth: bytes, *, sid: str | None = None, now_s: int | None = None,
                         grace_s: int = 0) -> dict[str, Any]:
    """校验沙盒 token：签名或格式错误、过期 → TokenInvalid（302）；scope 不是 sandbox、sid 或 run 与路径不符 →
    SandboxScopeError（503）。sid 为 None 时只校验签名、过期与 scope（中间件先验签，路由再比对 sid）。"""
    import json

    try:
        ver, body, sig = token.split(".")
    except (ValueError, AttributeError):
        raise TokenInvalid("token 格式错误") from None
    if ver != "v1":
        raise TokenInvalid("token 版本不支持")
    try:
        want = hmac.new(k_sbauth, ("v1." + body).encode("ascii"), hashlib.sha256).digest()
        got = _b64d(sig)
    except (ValueError, UnicodeEncodeError):
        raise TokenInvalid("token 编码错误") from None
    if not hmac.compare_digest(want, got):
        raise TokenInvalid("token 签名错误")
    try:
        p = json.loads(_b64d(body))
    except (ValueError, UnicodeDecodeError):
        raise TokenInvalid("token 载荷无法解析") from None
    if not isinstance(p, dict) or p.get("v") != 1:
        raise TokenInvalid("token 载荷版本不符")
    now = int(time.time()) if now_s is None else int(now_s)
    if int(p.get("exp", 0) or 0) + max(0, int(grace_s)) < now:
        raise TokenInvalid("token 已过期")
    if p.get("scope") != "sandbox":
        raise SandboxScopeError("token 不是沙盒 token")
    if sid is not None and (p.get("sid") != sid or p.get("run") != sid):
        raise SandboxScopeError("token 不属于该沙盒")
    return p


# ---------------------------------------------------------------- 池控制面签名
def _canon(o: Any) -> Any:
    if isinstance(o, Mapping):
        return {str(k): _canon(o[k]) for k in sorted(o, key=str)}
    if isinstance(o, (list, tuple)):
        return [_canon(v) for v in o]
    return o


def canonical_pack(o: Any) -> bytes:
    """键递归排序后的 msgpack（签名的规范形式）。"""
    return msgpack.packb(_canon(o), use_bin_type=True)


def _pool_mac(k_pool: bytes, op: str, msg: Mapping[str, Any]) -> str:
    body = {"op": op, "v": msg.get("v"), "cid": msg.get("cid"), "principal": msg.get("principal"), "ts": msg.get("ts"),
            "args": msg.get("args")}
    return hmac.new(k_pool, canonical_pack(body), hashlib.sha256).hexdigest()


def sign_pool_request(k_pool: bytes, op: str, args: Mapping[str, Any] | None = None, *, cid: str | None = None,
                      now_s: float | None = None, principal: str = POOL_PRINCIPAL) -> dict[str, Any]:
    """`sys/session/{op}` 请求体（sandbox-api 与运维 CLI 调用）。"""
    msg: dict[str, Any] = {"v": 1, "cid": cid or "sp-" + secrets.token_hex(8), "principal": principal,
                           "ts": round(time.time() if now_s is None else float(now_s), 3), "args": dict(args or {})}
    msg["sig"] = _pool_mac(k_pool, op, msg)
    return msg


def verify_pool_request(k_pool: bytes, op: str, msg: Any, *, now_s: float | None = None,
                        window_s: float = POOL_WINDOW_S, principals: tuple[str, ...] = (POOL_PRINCIPAL,)) -> bool:
    """池侧校验：签名、principal 白名单与 ±window_s 时间窗（M17-FR-028、D2-AC-29）。"""
    if not isinstance(msg, Mapping) or msg.get("v") != 1 or msg.get("principal") not in principals:
        return False
    sig = msg.get("sig")
    ts = msg.get("ts")
    if not isinstance(sig, str) or not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False
    now = time.time() if now_s is None else float(now_s)
    if abs(now - float(ts)) > window_s:
        return False
    return hmac.compare_digest(_pool_mac(k_pool, op, msg), sig)
