"""bench 鉴权（AWR-17 §18.3.1–§18.3.5）：bench token、工作区会话、短期令牌与角色。

- bench token：`d3bt_` + 32 字节随机数的 base64url；服务端只存 sha256（`keys/bench_tokens.json`，0600），比对用 compare_digest；
- 会话：不透明值，`bench.sqlite` 只存其 sha256 与 principal、角色、项目、来源令牌、到期；令牌撤销后其会话即时失效；
- 短期令牌：`d3st_v1.<b64url(payload)>.<b64url(sig)>`，`sig = HMAC-SHA256(K_st, "v1." + payload)`，
  `K_st = HKDF-SHA256(ikm = keys/k_bench, salt = "awr-bench", info = "awr/bench-st/v1")`，有效 600 s；
- 会话有效期上限 12 h（`SESSION_TTL_S`，D3-AC-51）：到期时刻 `exp = iat + session_ttl_s`，`now >= exp` 即失效；
- 审计（§18.3.7，M27-FR-156）：`AuditLog` 追加写 `<data_root>/audit/access-<UTC 日期>.jsonl`（目录 0700、文件 0600），记录登录与登出、
  令牌签发与撤销（本模块）以及越权、写操作与特权读取（`awr.bench.api.deps`）；不记录令牌、Cookie 值、种子明文与请求体，
  字段值中出现的令牌形态字符串一律替换为 `[redacted]`。
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["AUDS", "ROLES", "SESSION_TTL_S", "AuditLog", "AuthError", "BenchAuth", "Principal"]

ROLES = ("invited", "researcher", "evaluator", "admin")
AUDS = ("lab-rt", "frames", "rerun")
SESSION_TTL_S = 43_200                 # 会话 Cookie 有效期上限 12 h（AWR-17 §18.3.1 第 2 条，D3-AC-51）
_SECRET_KEYS = frozenset({"token", "cookie", "authorization", "seed", "secret", "password", "value"})
_SECRET_PREFIX = ("d3bt_", "d3st_v1.", "d3at_v1.")


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
    iat: int = 0                       # 会话签发与到期时刻（UNIX 秒；只对 via = session 有值）
    exp: int = 0

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


def _scrub(v):
    """审计字段值脱敏：令牌形态的字符串替换为 `[redacted]`；嵌套结构逐项处理。"""
    if isinstance(v, str):
        return "[redacted]" if v.startswith(_SECRET_PREFIX) else v
    if isinstance(v, dict):
        return {k: ("[redacted]" if str(k).lower() in _SECRET_KEYS else _scrub(x)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x) for x in v]
    return v


class AuditLog:
    """bench 访问审计（AWR-17 §18.3.7）：每行一个 JSON 对象 `{t_unix_ns, action, …}`，按 UTC 日期分文件，追加写。

    `action`：`login`、`logout`、`token_issue`、`token_revoke`（本模块）；`deny`（403：115 角色或数据范围不足、303 Origin 或 CSRF、
    691 test 划分、695 种子）、`write`（非 GET 请求及其结果码）、`read_privileged`（最低角色为评测员及以上的读取，如真值帧）。"""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.clock = clock
        self._lock = threading.Lock()

    def path_for(self, t: float | None = None) -> Path:
        return self.root / f"access-{time.strftime('%Y%m%d', time.gmtime(self.clock() if t is None else t))}.jsonl"

    def write(self, action: str, **fields) -> dict:
        t = self.clock()
        rec = {"t_unix_ns": str(int(t * 1_000_000_000)), "action": action, **_scrub(fields)}
        line = (json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self._lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path_for(t), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return rec

    def read(self, t: float | None = None) -> list[dict]:
        """某一 UTC 日（缺省当日）的全部审计记录（测试与离线核查使用）。"""
        p = self.path_for(t)
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


class BenchAuth:
    def __init__(self, keys_dir: Path, db_path: Path, *, session_ttl_s: int = SESSION_TTL_S, short_ttl_s: int = 600,
                 audit_dir: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        if not 0 < session_ttl_s <= SESSION_TTL_S:
            raise ValueError(f"session_ttl_s must be in (0, {SESSION_TTL_S}] (12 h)")
        self.keys_dir = Path(keys_dir)
        self.tokens_path = self.keys_dir / "bench_tokens.json"
        self.session_ttl_s = session_ttl_s
        self.short_ttl_s = short_ttl_s
        self.clock = clock
        self.audit = AuditLog(Path(audit_dir) if audit_dir is not None else self.keys_dir.parent / "audit", clock=clock)
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
                         "projects": list(projects), "issued_unix": int(self.clock()), "revoked": False})
            _write_private(self.tokens_path, (json.dumps({"tokens": toks}, indent=1) + "\n").encode())
        self.audit.write("token_issue", principal=principal, role=role, token_id=tid, projects=list(projects))
        return tid, tok

    def revoke_token(self, tid: str) -> bool:
        with self._lock:
            toks = self._tokens()
            hit = [t for t in toks if t["id"] == tid]
            for t in hit:
                t["revoked"] = True
            _write_private(self.tokens_path, (json.dumps({"tokens": toks}, indent=1) + "\n").encode())
            self._db.execute("DELETE FROM sessions WHERE token_id = ?", (tid,))
        self.audit.write("token_revoke", token_id=tid, principal=hit[0]["principal"] if hit else None, result="revoked" if hit else "unknown")
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
        """新建会话（登录）：返回 (Cookie 值, 到期 UNIX 秒)；`exp = iat + session_ttl_s`（≤ 12 h）。"""
        v = secrets.token_urlsafe(32)
        now = int(self.clock())
        self._db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", (hashlib.sha256(v.encode()).hexdigest(), p.name, p.role,
                                                                             json.dumps(list(p.projects)), p.token_id, now, now + self.session_ttl_s))
        self.audit.write("login", principal=p.name, role=p.role, token_id=p.token_id, exp_unix=now + self.session_ttl_s)
        return v, now + self.session_ttl_s

    def resolve_session(self, value: str) -> Principal:
        row = self._db.execute("SELECT principal, role, projects, token_id, exp, iat FROM sessions WHERE h = ?",
                               (hashlib.sha256(value.encode()).hexdigest(),)).fetchone()
        if row is None or row[4] <= self.clock() or not self._token_live(row[3]):
            raise AuthError(302, "session expired or revoked")
        return Principal(row[0], row[1], tuple(json.loads(row[2])), "session", row[3], iat=int(row[5]), exp=int(row[4]))

    def delete_session(self, value: str) -> None:
        """注销：删除会话行并写 `logout` 审计（未知会话不写）。"""
        h = hashlib.sha256(value.encode()).hexdigest()
        row = self._db.execute("SELECT principal, role, token_id FROM sessions WHERE h = ?", (h,)).fetchone()
        self._db.execute("DELETE FROM sessions WHERE h = ?", (h,))
        if row is not None:
            self.audit.write("logout", principal=row[0], role=row[1], token_id=row[2])

    # ---------------------------------------------------------------- 短期令牌
    def sign_short(self, p: Principal, aud: str, *, run_id: str | None = None, now: int | None = None) -> tuple[str, int]:
        if aud not in AUDS:
            raise AuthError(302, f"aud must be one of {AUDS}")
        iat = int(now if now is not None else self.clock())
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
        t = int(now if now is not None else self.clock())
        if p.get("aud") != aud or p["exp"] <= t or not self._token_live(p.get("tid", "")):
            raise AuthError(302, "short token expired, revoked or for another audience")
        return Principal(p["sub"], p["role"], tuple(p["scope"].get("projects", ())), "short", p.get("tid", ""), p["scope"])

    def resolve_bearer(self, value: str, *, aud: str | None = None) -> Principal:
        if value.startswith("d3bt_"):
            return self.verify_token(value)
        if value.startswith("d3st_v1.") and aud is not None:
            return self.verify_short(value, aud)
        raise AuthError(302, "unsupported bearer token")
