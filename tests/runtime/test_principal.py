"""分钥、principal 签名与 token（AWR-17 §3.2、§9.4；ADR-027）。"""

from __future__ import annotations

import pytest

from awr.runtime import principal as P
from awr.runtime.principal import Principal, TokenInvalid


def test_hkdf_rfc5869_case1() -> None:
    ikm = bytes([0x0B] * 22)
    salt = bytes(range(0x00, 0x0D))
    info = bytes(range(0xF0, 0xFA))
    okm = P._hkdf(ikm, salt, info, 42)
    assert okm.hex() == ("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
                         "34007208d5b887185865")


def test_derive_keys_distinct() -> None:
    s = P.new_secret()
    assert len(s) == 32
    keys = {p: P.derive_key(s, "r20260928-143200-a3f1", p) for p in P.PURPOSES}
    assert len(set(keys.values())) == 4
    assert P.derive_key(s, "r20260928-143200-a3f2", "auth") != keys["auth"]
    with pytest.raises(ValueError):
        P.derive_key(s, "r", "bogus")  # type: ignore[arg-type]


def test_token_roundtrip_and_rejections() -> None:
    key = P.derive_key(b"k" * 32, "run-a", "auth")
    payload = P.make_token_payload("p-abc", "operator", "run-a", "loopback", now_s=1000)
    tok = P.encode_token(payload, key)
    assert tok.startswith("v1.") and "=" not in tok and tok.count(".") == 2
    assert P.decode_token(tok, key, run_id="run-a", now_s=1001) == payload
    for bad, kw in ((tok[:-2] + "AA", {}), (tok, {"run_id": "run-b"}), (tok, {"now_s": 1000 + 43201}),
                    ("v2.x.y", {}), ("garbage", {})):
        args = {"run_id": "run-a", "now_s": 1001} | kw
        with pytest.raises(TokenInvalid) as ei:
            P.decode_token(bad, key, **args)
        assert ei.value.code == 302
    with pytest.raises(TokenInvalid):
        P.decode_token(tok, P.derive_key(b"x" * 32, "run-a", "auth"), run_id="run-a", now_s=1001)


def test_principal_sign_verify() -> None:
    key = P.derive_key(b"s" * 32, "run", "entry")
    p = Principal("p-1", "operator", "api", "c-1", True)
    sig = P.sign_principal(p, "cid-1", key)
    assert P.verify_principal(p, "cid-1", sig, key)
    assert not P.verify_principal(p, "cid-2", sig, key)
    assert not P.verify_principal(Principal("p-1", "admin", "api", "c-1", True), "cid-1", sig, key)
    assert P.check_password("abc", "abc") and not P.check_password("abc", "abd")
