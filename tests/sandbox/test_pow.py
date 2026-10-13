"""工作量证明（M17-FR-017、AC-018 的服务端部分；17 §17.3.5）：正确解通过、错误解与重放解拒绝、换前缀使用的题拒绝、过期、难度递增、
落盘后已用题号仍拒绝。为缩短用例，难度取 8 位。"""

from __future__ import annotations

import base64
import hashlib

from awr.sandbox.pow import Pow, leading_zero_bits, solve

K = b"p" * 32
PFX = "198.51.100.7/32"


def _solve(c: dict) -> int:
    n = solve(base64.b64decode(c["prefix_b64"]), int(c["bits"]))
    assert n is not None
    return n


def test_leading_zero_bits() -> None:
    assert leading_zero_bits(b"\x00\x00\x01") == 23
    assert leading_zero_bits(b"\x80") == 0
    assert leading_zero_bits(b"\x00" * 4) == 32


def test_issue_and_verify_roundtrip() -> None:
    p = Pow(K, bits=8, bits_max=12)
    c = p.issue(PFX, "h1", 100.0).wire()
    assert c["alg"] == "sha256-lz" and c["challenge_id"].startswith("c-") and len(base64.b64decode(c["prefix_b64"])) == 32
    assert c["expires_unix_ns"] == str(220 * 10**9)
    n = _solve(c)
    h = hashlib.sha256(base64.b64decode(c["prefix_b64"]) + str(n).encode()).digest()
    assert leading_zero_bits(h) >= 8
    assert p.verify({"challenge_id": c["challenge_id"], "nonce": n}, PFX, "h1", 101.0) is None
    assert p.verify({"challenge_id": c["challenge_id"], "nonce": n}, PFX, "h1", 102.0) == "invalid"   # 重放


def test_rejections() -> None:
    p = Pow(K, bits=8)
    assert p.verify(None, PFX, "h1", 0.0) == "required"
    c = p.issue(PFX, "h1", 0.0).wire()
    n = _solve(c)
    bad = next(x for x in range(n + 1, n + 100000)
               if leading_zero_bits(hashlib.sha256(base64.b64decode(c["prefix_b64"]) + str(x).encode()).digest()) < 8)
    assert p.verify({"challenge_id": c["challenge_id"], "nonce": bad}, PFX, "h1", 1.0) == "invalid"
    assert p.verify({"challenge_id": c["challenge_id"], "nonce": n}, "203.0.113.9/32", "h2", 1.0) == "invalid"  # 换前缀
    assert p.verify({"challenge_id": c["challenge_id"], "nonce": -1}, PFX, "h1", 1.0) == "invalid"
    assert p.verify({"challenge_id": c["challenge_id"], "nonce": n}, PFX, "h1", 500.0) == "expired"
    assert p.verify({"challenge_id": "c-unknown123", "nonce": 1}, PFX, "h1", 1.0) == "expired"


def test_difficulty_and_persistence() -> None:
    p = Pow(K, bits=20, bits_max=24)
    assert [p.difficulty(k) for k in (1, 2, 3, 9)] == [20, 21, 22, 24]
    q = Pow(K, bits=8)
    c = q.issue(PFX, "h1", 0.0).wire()
    n = _solve(c)
    assert q.verify({"challenge_id": c["challenge_id"], "nonce": n}, PFX, "h1", 1.0) is None
    r = Pow(K, bits=8)
    r.load(q.dump())
    assert r.verify({"challenge_id": c["challenge_id"], "nonce": n}, PFX, "h1", 2.0) == "invalid"
    c2 = r.issue(PFX, "h1", 0.0).wire()
    s = Pow(K, bits=8)
    s.load(r.dump())
    assert s.verify({"challenge_id": c2["challenge_id"], "nonce": _solve(c2)}, PFX, "h1", 1.0) is None
    s.prune(10_000.0)
    assert not s.issued and not s.used
