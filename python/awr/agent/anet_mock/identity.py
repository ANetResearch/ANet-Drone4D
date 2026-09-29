"""Mock 伪 AID 与每 AID 密钥（M14 §6.3；M14-FR-009、FR-033）。

```text
digest(world_id, vehicle_id) = sha256("awr-mock-aid/v1" ‖ 0x00 ‖ world_id ‖ 0x00 ‖ vehicle_id)      # UTF-8
aid(world_id, vehicle_id)    = "b" + base32_lower_nopad(0x01 ‖ 0x71 ‖ 0x12 ‖ 0x20 ‖ digest)       # CIDv1 dag-cbor sha2-256
coordinator_aid(world_id)    = aid(world_id, "gcs")                                                 # "gcs" 为保留 id
aid_short(aid)               = aid[0:7] + "…" + aid[-6:]
K_agent(aid)                 = HKDF-SHA256(AWR_SECRET, salt = run_id, info = "awr/agent/<aid>/v1")
```

与真网 AID 的前缀 `bafyrei` 与 59 字符长度一致，跨 run 稳定，便于对比实验。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

__all__ = ["AID_LEN", "CID_PREFIX", "COORDINATOR_ID", "agent_key", "aid", "aid_short", "aid_short_safe", "coordinator_aid",
           "decode_aid", "hkdf_sha256", "is_aid", "sign", "verify"]

CID_PREFIX = bytes((0x01, 0x71, 0x12, 0x20))  # CIDv1、dag-cbor、sha2-256、32 B
COORDINATOR_ID = "gcs"
AID_LEN = 59
_B32 = "abcdefghijklmnopqrstuvwxyz234567"


def _digest(world_id: str, vehicle_id: str) -> bytes:
    return hashlib.sha256(b"awr-mock-aid/v1" + b"\x00" + world_id.encode("utf-8") + b"\x00" + vehicle_id.encode("utf-8")).digest()


def aid(world_id: str, vehicle_id: str) -> str:
    raw = CID_PREFIX + _digest(world_id, vehicle_id)
    return "b" + base64.b32encode(raw).decode("ascii").lower().rstrip("=")


def coordinator_aid(world_id: str) -> str:
    return aid(world_id, COORDINATOR_ID)


def aid_short(a: str) -> str:
    return a[0:7] + "…" + a[-6:]


def aid_short_safe(a: str) -> str:
    return a[:16]


def is_aid(s: str) -> bool:
    return isinstance(s, str) and len(s) == AID_LEN and s.startswith("bafyrei") and all(c in _B32 for c in s[1:])


def decode_aid(a: str) -> bytes:
    body = a[1:].upper()
    return base64.b32decode(body + "=" * (-len(body) % 8))


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()
    okm, t, i = b"", b"", 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


def agent_key(secret: bytes, run_id: str, a: str) -> bytes:
    """`K_agent(aid)`（Mock 签名密钥，§6.3）。"""
    return hkdf_sha256(secret, run_id.encode("utf-8"), f"awr/agent/{a}/v1".encode())


def sign(key: bytes, msg: str | bytes) -> str:
    m = msg.encode("utf-8") if isinstance(msg, str) else msg
    return hmac.new(key, m, hashlib.sha256).hexdigest()


def verify(key: bytes, msg: str | bytes, sig: str) -> bool:
    return isinstance(sig, str) and hmac.compare_digest(sign(key, msg), sig)
