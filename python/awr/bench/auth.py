"""bench 鉴权（AWR-17 §18.3.1–§18.3.5）：bench token、工作区会话、短期令牌与角色。

- bench token：`d3bt_` + 32 字节随机数的 base64url；服务端只存 sha256（`keys/bench_tokens.json`，0600），比对用 compare_digest；
- 会话：不透明值，`bench.sqlite` 只存其 sha256 与 principal、角色、项目、来源令牌、到期；令牌撤销后其会话即时失效；
- 短期令牌：`d3st_v1.<b64url(payload)>.<b64url(sig)>`，`sig = HMAC-SHA256(K_st, "v1." + payload)`，
  `K_st = HKDF-SHA256(ikm = keys/k_bench, salt = "awr-bench", info = "awr/bench-st/v1")`，有效 600 s。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ROLES", "AuthError", "BenchAuth", "Principal"]

ROLES = ("invited", "researcher", "evaluator", "admin")
AUDS = ("lab-rt", "frames", "rerun")


class AuthError(Exception):
    """code：301 缺少凭据、302 凭据无效或过期、115 角色不足。"""

    def __init__(self, code: int, detail: str = "") -> None:
        super().__init__(f"{code} {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    projects: tuple[str, ...] = ()
    via: str = "token"                 # token、session、short
    token_id: str = ""
    scope: dict = field(default_factory=dict)

    def at_least(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hkdf(ikm: bytes, salt: bytes, info: bytes, n: int = 32) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:n]


def _write_private(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, p)


class BenchAuth:
    def __init__(self, keys_dir: Path, db_path: Path, *, session_ttl_s: int = 43_200, short_ttl_s: int = 600) -> None:
        self.keys_dir = Path(keys_dir)
        self.tokens_path = self.keys_dir / "bench_tokens.json"
        self.session_ttl_s = session_ttl_s
        self.short_ttl_s = short_ttl_s
        self._lock = threading.Lock()
        kp = self.keys_dir / "k_bench"
        if not kp.exists():
            _write_private(kp, secrets.token_bytes(32))
        self._k_st = _hkdf(kp.read_bytes(), b"awr-bench", b"awr/bench-st/v1")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._db.execute("CREATE TABLE IF NOT EXISTS sessions (h TEXT PRIMARY KEY, principal TEXT, role TEXT, projects TEXT, token_id TEXT, "
                         "iat INTEGER, exp INTEGER)")

    # ---------------------------------------------------------------- bench token
    def _tokens(self) -> list[dict]:
        if not self.tokens_path.exists():
            return []
        return json.loads(self.tokens_path.read_text(encoding="utf-8")).get("tokens", [])

    def issue_token(self, principal: str, role: str, projects: tuple[str, ...] = ()) -> tuple[str, str]:
        """返回 (令牌 id, 明文令牌)；明文只在此处出现一次。"""
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        tok = "d3bt_" + _b64(secrets.token_bytes(32))
        tid = secrets.token_hex(8)
        with self._lock:
            toks = self._tokens()
            toks.append({"id": tid, "sha256": hashlib.sha256(tok.encode()).hexdigest(), "principal": principal, "role": role,
                         "projects": list(projects), "issued_unix": int(time.time()), "revoked": False})
            _write_private(self.tokens_path, (json.dumps({"tokens": toks}, indent=1) + "\n").encode())
        return tid, tok

    def revoke_token(self, tid: str) -> bool:
        with self._lock:
            toks = self._tokens()
            hit = [t for t in toks if t["id"] == tid]
            for t in hit:
                t["revoked"] = True
            _write_private(self.tokens_path, (json.dumps({"tokens": toks}, indent=1) + "\n").encode())
            self._db.execute("DELETE FROM sessions WHERE token_id = ?", (tid,))
        return bool(hit)

    def list_tokens(self) -> list[dict]:
        return [{k: v for k, v in t.items() if k != "sha256"} for t in self._tokens()]

    def verify_token(self, token: str) -> Principal:
        if not token.startswith("d3bt_"):
            raise AuthError(302, "not a bench token")
        h = hashlib.sha256(token.encode()).hexdigest()
        for t in self._tokens():
            if hmac.compare_digest(t["sha256"], h):
                if t.get("revoked"):
                    raise AuthError(302, "token revoked")
                return Principal(t["principal"], t["role"], tuple(t.get("projects", ())), "token", t["id"])
        raise AuthError(302, "unknown token")

    def _token_live(self, tid: str) -> bool:
        return any(t["id"] == tid and not t.get("revoked") for t in self._tokens())

    # ---------------------------------------------------------------- 会话
    def create_session(self, p: Principal) -> tuple[str, int]:
        v = secrets.token_urlsafe(32)
        now = int(time.time())
        self._db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", (hashlib.sha256(v.encode()).hexdigest(), p.name, p.role,
                                                                             json.dumps(list(p.projects)), p.token_id, now, now + self.session_ttl_s))
        return v, now + self.session_ttl_s

    def resolve_session(self, value: str) -> Principal:
        row = self._db.execute("SELECT principal, role, projects, token_id, exp FROM sessions WHERE h = ?",
                               (hashlib.sha256(value.encode()).hexdigest(),)).fetchone()
        if row is None or row[4] < time.time() or not self._token_live(row[3]):
            raise AuthError(302, "session expired or revoked")
        return Principal(row[0], row[1], tuple(json.loads(row[2])), "session", row[3])

    def delete_session(self, value: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE h = ?", (hashlib.sha256(value.encode()).hexdigest(),))

    # ---------------------------------------------------------------- 短期令牌
    def sign_short(self, p: Principal, aud: str, *, run_id: str | None = None, now: int | None = None) -> tuple[str, int]:
        if aud not in AUDS:
            raise AuthError(302, f"aud must be one of {AUDS}")
        iat = int(now if now is not None else time.time())
        scope: dict = {"projects": list(p.projects)}
        if run_id:
            scope["run_id"] = run_id
        payload = {"v": 1, "sub": p.name, "role": p.role, "aud": aud, "scope": scope, "iat": iat, "exp": iat + self.short_ttl_s,
                   "jti": secrets.token_hex(8), "tid": p.token_id}
        pb = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        sig = _b64(hmac.new(self._k_st, ("v1." + pb).encode(), hashlib.sha256).digest())
        return f"d3st_v1.{pb}.{sig}", payload["exp"]

    def verify_short(self, token: str, aud: str, *, now: int | None = None) -> Principal:
        try:
            head, pb, sig = token.split(".")
        except ValueError:
            raise AuthError(302, "malformed short token") from None
        want = _b64(hmac.new(self._k_st, ("v1." + pb).encode(), hashlib.sha256).digest())
        if head != "d3st_v1" or not hmac.compare_digest(sig, want):
            raise AuthError(302, "bad signature")
        p = json.loads(_unb64(pb))
        t = int(now if now is not None else time.time())
        if p.get("aud") != aud or p["exp"] <= t or not self._token_live(p.get("tid", "")):
            raise AuthError(302, "short token expired, revoked or for another audience")
        return Principal(p["sub"], p["role"], tuple(p["scope"].get("projects", ())), "short", p.get("tid", ""), p["scope"])

    def resolve_bearer(self, value: str, *, aud: str | None = None) -> Principal:
        if value.startswith("d3bt_"):
            return self.verify_token(value)
        if value.startswith("d3st_v1.") and aud is not None:
            return self.verify_short(value, aud)
        raise AuthError(302, "unsupported bearer token")
