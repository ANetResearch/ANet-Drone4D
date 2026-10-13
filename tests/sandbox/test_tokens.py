"""沙盒 token 与排队票令牌（M17-FR-050、AC-011 的池侧部分；17 §17.3.1、§17.3.2）：池签发的 token 由 sandbox-api 的
SandboxTokenService 逐字节验证；sid 不符为 503；排队票令牌只保存散列。"""

from __future__ import annotations

import time

import pytest

from awr.api.security import SandboxTokenService
from awr.runtime.principal import TokenInvalid
from awr.runtime.sbauth import SandboxScopeError, decode_sandbox_token
from awr.sandbox.model import new_sid, new_ticket_token
from awr.sandbox.tokens import Keys, ticket_token_hash, ticket_token_ok


def test_pool_token_verified_by_sandbox_api(tmp_path) -> None:
    keys = Keys.load(tmp_path)
    svc = SandboxTokenService(tmp_path / "token.key", mode="loopback")
    sid = new_sid()
    tok, payload = keys.issue("p-" + "a" * 32, sid, time.time() + 600)
    assert payload["scope"] == "sandbox" and payload["sid"] == sid == payload["run"] and payload["role"] == "operator"
    p = svc.verify(tok, sid=sid)
    assert p.sid == sid and p.id == "p-" + "a" * 32
    with pytest.raises(SandboxScopeError):
        svc.verify(tok, sid=new_sid())
    old, _ = keys.issue("p-x", sid, time.time() - 30)
    with pytest.raises(TokenInvalid):
        svc.verify(old, sid=sid)
    assert decode_sandbox_token(old, keys.k_sbauth, sid=sid, grace_s=600)["sid"] == sid   # 快照导出的宽限


def test_keys_stable_and_distinct(tmp_path) -> None:
    a = Keys.load(tmp_path)
    b = Keys.load(tmp_path)
    assert a == b
    assert len({a.k_sbauth, a.k_pow, a.k_state, a.k_pool}) == 4
    assert (tmp_path / "token.key").stat().st_mode & 0o777 == 0o600


def test_ticket_token() -> None:
    t = new_ticket_token()
    assert t.startswith("q1.") and len(t) > 40
    h = ticket_token_hash(t)
    assert ticket_token_ok(t, h) and not ticket_token_ok(new_ticket_token(), h)
    assert not ticket_token_ok("v1.x.y", h) and not ticket_token_ok(None, h)
