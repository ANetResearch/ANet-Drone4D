"""工作区会话 Cookie 的 12 h 寿命（D3-AC-51"bench token 换取会话 Cookie（HttpOnly、`SameSite=Strict`、12 h）"；AWR-17 §18.3.1 第 2 条；
M27-AC-029）。

- `Set-Cookie: d3_session=…; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=43200`；
- 服务端会话记录 `exp = iat + 43200`；到期前 1 s 有效，到期时刻起 401 `302`；`GET /session` 返回的 `exp_unix_ns - iat_unix_ns` = 12 h；
- 设置不允许超过 12 h 的会话寿命；短期令牌 600 s。
"""

from __future__ import annotations

from http.cookies import SimpleCookie

import pytest

from awr.bench.auth import SESSION_TTL_S, BenchAuth, Principal
from awr.bench.settings import BenchSettings

H12 = 12 * 3600


class FakeClock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_cookie_attributes_12h(lab_factory) -> None:
    lab = lab_factory(secure_cookie=True)                                                    # 生产设置：Secure
    r = lab.client.post(f"{lab.P}/session", json={"token": lab.tokens["researcher"]})
    assert r.status_code == 204
    raw = r.headers["set-cookie"]
    c = SimpleCookie()
    c.load(raw)
    m = c["d3_session"]
    assert m["max-age"] == str(H12) and m["path"] == "/" and m["httponly"] is True and m["secure"] is True
    assert m["samesite"].lower() == "strict"
    assert len(m.value) >= 43 and m.value not in lab.tokens.values()                          # 不透明值，不是 bench token


def test_server_side_expiry_at_12h(lab) -> None:
    clock = FakeClock(1_800_000_000.0)
    auth = lab.app.state.auth
    auth.clock = auth.audit.clock = clock
    c = lab.session_client("invited")
    s = c.get(f"{lab.P}/session").json()
    assert s["via"] == "session" and int(s["exp_unix_ns"]) - int(s["iat_unix_ns"]) == H12 * 10**9
    assert int(s["iat_unix_ns"]) == 1_800_000_000 * 10**9
    clock.t += H12 - 1
    assert c.get(f"{lab.P}/runs", headers={}).status_code == 200                              # 到期前 1 s 仍有效
    clock.t += 1
    r = c.get(f"{lab.P}/session")
    assert r.status_code == 401 and r.json()["code"] == 302                                   # 到期时刻起失效
    assert lab.client.get(f"{lab.P}/session", headers=lab.bearer("invited")).status_code == 200   # bench token 本身不受会话寿命影响


def test_ttl_defaults_and_upper_bound(tmp_path) -> None:
    assert SESSION_TTL_S == H12 and BenchSettings().session_ttl_s == H12 and BenchSettings().short_ttl_s == 600
    a = BenchAuth(tmp_path / "keys", tmp_path / "b.sqlite")
    assert a.session_ttl_s == H12
    with pytest.raises(ValueError):
        BenchAuth(tmp_path / "keys", tmp_path / "b.sqlite", session_ttl_s=H12 + 1)
    with pytest.raises(ValueError):
        BenchAuth(tmp_path / "keys", tmp_path / "b.sqlite", session_ttl_s=0)


def test_session_record_and_short_token_lifetime(tmp_path) -> None:
    clock = FakeClock(1_800_000_000.0)
    a = BenchAuth(tmp_path / "keys", tmp_path / "b.sqlite", clock=clock)
    tid, tok = a.issue_token("alice", "researcher", ("d3",))
    v, exp = a.create_session(a.verify_token(tok))
    assert exp == 1_800_000_000 + H12
    p = a.resolve_session(v)
    assert (p.iat, p.exp, p.token_id) == (1_800_000_000, 1_800_000_000 + H12, tid)
    _st, st_exp = a.sign_short(Principal("alice", "researcher", ("d3",), token_id=tid), "frames")
    assert st_exp - int(clock.t) == 600
    clock.t = exp
    with pytest.raises(Exception, match="302"):
        a.resolve_session(v)
