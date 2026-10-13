"""内部身份库 `awr.sandbox.identity`（M17-FR-077、NFR-018；AWR-04 §15.9.2、§15.9.3、§15.9.5；17 §17.20.5；M17 PRD §14.3.4）。

纯库，不含路由：show-api 的 `rest/auth.py`（M11，R79 `POST /api/auth/identity`、R80 `DELETE /api/auth/identity`、R01 的
`admin_secret` 路径）调用 `IdentityService.acheck()` 比对口令并签发身份令牌；sandbox-api 与沙盒池只用 `verify_token()` 与撤销表。

| 项 | 规则 |
|---|---|
| 密钥 | 公开模式只读 `AWR_ADMIN_SECRET_FILE` 的首行（去掉行尾换行）；文件缺失、为空、权限含组或其他位、属主不是本进程有效用户时视为未配置（启动时告警一次） |
| 比对 | `hmac.compare_digest(SHA-256(输入), SHA-256(密钥))`；每个请求（成功、失败、锁定、未配置、限速）在返回前补足到固定时延 `delay_ms`（300 ms），抖动 ±`jitter_ms`（25 ms）只加在补足部分 |
| 速率 | 每地址前缀 0.5 次/s、突发 3（超限 `rate`，429 `111`；不比对、不计失败） |
| 锁定 | 前缀 10 min 内失败 5 次 → 30 min；聚合前缀（IPv4 /24、IPv6 /48）10 次 → 30 min；全站 1 h 内 30 次 → 除回环来源外 15 min；锁定期内不比对、不计数；成功清零该前缀的失败计数 |
| 持久化 | `var/identity/lockout.json`（0600）：前缀以 `HMAC(K_state, 前缀)` 表示，不落明文地址；锁定建立时立即写，普通计数合批 ≤ 1 Hz |
| 令牌 | `v1.<payload>.<sig>`，载荷 `{v: 1, sub, role: "internal", scope: "identity", mode, iat, exp = iat + 43200, jti}`，`K_ident = HKDF-SHA256(ikm = 密钥, salt = "awr-identity", info = "awr/identity/v1")`；轮换口令即令全部令牌失效 |
| 撤销 | `var/identity/revoked.json`（`{jti: exp}`，≤ 1000 条，过期即删）；读者按文件修改时间 ≤ 1 Hz 重载 |
| 审计 | 回调 `audit(kind, detail)`：`auth.identity_unlock`、`auth.identity_denied`、`auth.identity_locked`、`auth.identity_global_lock`、`auth.identity_logout`；只记 jti 与前缀散列，从不记录输入、口令或令牌 |

任何代码路径都不把输入、密钥或令牌写入日志、异常文本或审计（`Verdict.__repr__` 不含令牌）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import random
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from awr.runtime.config import ROOT
from awr.runtime.principal import _b64d, _hkdf, encode_token
from awr.runtime.procutil import atomic_write

from .prefix import prefixes

__all__ = ["SECRET_ENV", "IdentityCfg", "IdentityInvalid", "IdentityService", "LockoutCfg", "RevocationList", "Verdict",
           "default_state_dir", "derive_k_ident", "read_secret_file"]

log = logging.getLogger("awr.sandbox.identity")

SECRET_ENV = "AWR_ADMIN_SECRET_FILE"
SALT = b"awr-identity"
INFO = b"awr/identity/v1"
STATE_KEY_NAME = "state.key"
LOCKOUT_NAME = "lockout.json"
REVOKED_NAME = "revoked.json"
FLUSH_MIN_S = 1.0
RELOAD_MIN_S = 1.0


def default_state_dir(env: dict[str, str] | None = None) -> Path:
    """身份库状态目录（锁定表、撤销表、前缀散列密钥）：`AWR_IDENTITY_STATE_DIR`（WP-16 env 名）或 `AWR_IDENTITY_DIR`，其次
    `identity.state_dir`（M11 配置段，相对路径按仓库根），缺省 `var/identity`（install.sh 以 0700 创建）。"""
    from .config import raw_section

    e = os.environ if env is None else env
    v = e.get("AWR_IDENTITY_STATE_DIR") or e.get("AWR_IDENTITY_DIR") or (raw_section("identity", e) or {}).get("state_dir")
    if not v:
        return ROOT / "var" / "identity"
    p = Path(str(v))
    return p if p.is_absolute() else ROOT / p


# ---------------------------------------------------------------- 配置
@dataclass
class LockoutCfg:
    prefix_fail: int = 5
    window_s: float = 600.0
    prefix_lock_s: float = 1800.0
    agg_fail: int = 10
    agg_lock_s: float = 1800.0
    global_fail_h: int = 30
    global_window_s: float = 3600.0
    global_lock_s: float = 900.0


@dataclass
class IdentityCfg:
    """`identity:` 段（M11 文件；AWR-04 §15.9.2、M17 PRD §14.4）。"""

    token_ttl_s: float = 43200.0
    delay_ms: float = 300.0
    jitter_ms: float = 25.0
    rate_per_s: float = 0.5
    rate_burst: float = 3.0
    revoked_max: int = 1000
    lockout: LockoutCfg = field(default_factory=LockoutCfg)

    @classmethod
    def from_obj(cls, obj: Any) -> IdentityCfg:
        if hasattr(obj, "model_dump"):
            obj = obj.model_dump(mode="json")
        if not isinstance(obj, dict):
            return cls()
        out = cls()
        for k, alias in (("token_ttl_s", None), ("delay_ms", None), ("jitter_ms", None), ("rate_per_s", "rps"),
                         ("rate_burst", "burst")):
            v = obj.get(k, obj.get(alias) if alias else None)
            with contextlib.suppress(TypeError, ValueError):
                if v is not None:
                    setattr(out, k, float(v))
        with contextlib.suppress(TypeError, ValueError):
            if obj.get("revoked_max") is not None:
                out.revoked_max = int(obj["revoked_max"])
        lo = obj.get("lockout")
        if isinstance(lo, dict):
            for k in ("prefix_fail", "agg_fail", "global_fail_h"):
                with contextlib.suppress(TypeError, ValueError):
                    if lo.get(k) is not None:
                        setattr(out.lockout, k, int(lo[k]))
            for k in ("window_s", "prefix_lock_s", "agg_lock_s", "global_window_s", "global_lock_s"):
                with contextlib.suppress(TypeError, ValueError):
                    if lo.get(k) is not None:
                        setattr(out.lockout, k, float(lo[k]))
        return out


# ---------------------------------------------------------------- 密钥文件
def read_secret_file(path: str | Path | None) -> tuple[bytes | None, str | None]:
    """(密钥, 不可用原因)。首行去掉行尾换行；权限必须只有属主位（0600 或 0400），属主为本进程有效用户。"""
    if not path:
        return None, "unset"
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None, "missing"
    if st.st_mode & 0o077:
        return None, "permissions"
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        return None, "owner"
    try:
        raw = p.read_bytes()
    except OSError:
        return None, "unreadable"
    line = raw.split(b"\n", 1)[0].rstrip(b"\r")
    if not line:
        return None, "empty"
    return line, None


def derive_k_ident(secret: bytes) -> bytes:
    return _hkdf(secret, SALT, INFO)


class IdentityInvalid(Exception):
    """身份令牌无效（格式、签名、过期、作用域或已撤销）。消息只含原因，不含令牌。"""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


# ---------------------------------------------------------------- 判定结果
@dataclass(frozen=True)
class Verdict:
    kind: str                                   # ok | wrong | locked | disabled | rate
    token: str | None = field(default=None, repr=False)
    payload: dict | None = None
    scope: str | None = None                    # locked：prefix | aggregate | global
    retry_after_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    def problem(self) -> tuple[int, int, dict | None, dict[str, str] | None]:
        """(HTTP, 原因码, detail, 头)：R79 与 R01 的失败映射（17 §17.20.5）。成功为 (200, 0, None, None)。"""
        if self.kind == "ok":
            return 200, 0, None, None
        if self.kind == "wrong":
            return 401, 304, None, None
        if self.kind == "disabled":
            return 403, 115, {"why": "PUBLIC_READONLY"}, None
        ra = max(1, math.ceil(self.retry_after_s or 1))
        if self.kind == "locked":
            return 429, 516, {"scope": self.scope, "retry_after_s": ra}, {"Retry-After": str(ra)}
        return 429, 111, {"retry_after_s": ra}, {"Retry-After": str(ra)}


# ---------------------------------------------------------------- 撤销表
class RevocationList:
    """`var/identity/revoked.json`：`{"v": 1, "revoked": {jti: exp_unix_s}}`。写者为 show-api（R80），读者按修改时间重载。"""

    def __init__(self, path: Path, *, max_items: int = 1000, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.path = Path(path)
        self.max_items = int(max_items)
        self.clock = clock
        self.mono = mono
        self.items: dict[str, float] = {}
        self._mtime: int | None = None
        self._checked = -1e18
        self._lock = threading.Lock()
        self.reload(force=True)

    def reload(self, *, force: bool = False) -> None:
        now_m = self.mono()
        if not force and now_m - self._checked < RELOAD_MIN_S:
            return
        self._checked = now_m
        try:
            st = self.path.stat()
        except OSError:
            if self._mtime is not None:
                self.items, self._mtime = {}, None
            return
        if not force and st.st_mtime_ns == self._mtime:
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            rv = d.get("revoked") if isinstance(d, dict) else None
            self.items = {str(k): float(v) for k, v in (rv or {}).items() if isinstance(v, (int, float))}
            self._mtime = st.st_mtime_ns
        except (OSError, ValueError):
            log.warning("revocation list unreadable", extra={"kv": {"path": str(self.path)}})

    def is_revoked(self, jti: str | None) -> bool:
        if not jti:
            return False
        self.reload()
        return jti in self.items

    def add(self, jti: str, exp_unix_s: float) -> None:
        with self._lock:
            self.reload(force=True)
            now = self.clock()
            items = {k: v for k, v in self.items.items() if v > now}
            items[str(jti)] = float(exp_unix_s)
            if len(items) > self.max_items:
                for k, _v in sorted(items.items(), key=lambda kv: kv[1])[:len(items) - self.max_items]:
                    items.pop(k, None)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(self.path, json.dumps({"v": 1, "revoked": items}, separators=(",", ":")).encode("utf-8"), 0o600)
            self.items = items
            with contextlib.suppress(OSError):
                self._mtime = self.path.stat().st_mtime_ns


# ---------------------------------------------------------------- 服务
def _loopback(addr: str | None) -> bool:
    if not addr:
        return False
    s = addr.split("/", 1)[0].strip()
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return a.is_loopback


class IdentityService:
    def __init__(self, *, secret: bytes | None, state_dir: Path | None = None, cfg: IdentityCfg | None = None,
                 mode: str = "public", clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 audit: Callable[[str, dict], None] | None = None, rng: random.Random | None = None,
                 disabled_why: str | None = None) -> None:
        self.cfg = cfg or IdentityCfg()
        self.mode = mode
        self.clock = clock
        self.mono = mono
        self.audit = audit
        self.rng = rng or random.SystemRandom()
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self._secret_digest = hashlib.sha256(secret).digest() if secret else None
        self.k_ident = derive_k_ident(secret) if secret else None
        self.disabled_why = None if secret else (disabled_why or "unset")
        self._lock = threading.Lock()
        self.k_state = self._state_key()
        self.revoked = RevocationList(self.state_dir / REVOKED_NAME, max_items=self.cfg.revoked_max, clock=clock, mono=mono)
        self.prefix: dict[str, dict] = {}      # 前缀散列 -> {"fails": [t], "until": t}
        self.agg: dict[str, dict] = {}
        self.glob: dict[str, Any] = {"fails": [], "until": 0.0}
        self.buckets: dict[str, list[float]] = {}
        self._dirty = False
        self._last_flush = -1e18
        self._load()

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *, state_dir: Path | None = None,
                 audit: Callable[[str, dict], None] | None = None, cfg: IdentityCfg | None = None,
                 secret: bytes | None = None) -> IdentityService:
        """公开模式：密钥只取 `AWR_ADMIN_SECRET_FILE`；`secret` 显式给出时（本机与局域网部署的管理口令）以之为准。"""
        from .config import raw_section

        e = os.environ if env is None else env
        why = None
        if secret is None:
            secret, why = read_secret_file(e.get(SECRET_ENV))
            if why not in (None, "unset"):
                log.warning("identity secret file not usable; internal identity disabled", extra={"kv": {"why": why}})
        return cls(secret=secret, state_dir=state_dir or default_state_dir(e), cfg=cfg or IdentityCfg.from_obj(raw_section("identity", e)),
                   mode=e.get("AWR_ACCESS_MODE") or "public", audit=audit, disabled_why=why)

    @property
    def enabled(self) -> bool:
        return self.k_ident is not None

    def _state_key(self) -> bytes:
        p = self.state_dir / STATE_KEY_NAME
        try:
            b = p.read_bytes()
            if len(b) >= 32:
                return b[:32]
        except OSError:
            pass
        k = secrets.token_bytes(32)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(OSError):
                os.chmod(self.state_dir, 0o700)
            atomic_write(p, k, 0o600)
        except OSError:
            log.warning("identity state dir not writable; lockout state kept in memory only")
        return k

    # ------------------------------------------------------------ 锁定状态
    def _h(self, s: str) -> str:
        return hmac.new(self.k_state, s.encode("utf-8"), hashlib.sha256).digest()[:16].hex()

    def _load(self) -> None:
        try:
            d = json.loads((self.state_dir / LOCKOUT_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(d, dict) or d.get("v") != 1:
            return
        for name in ("prefix", "agg"):
            src = d.get(name)
            if isinstance(src, dict):
                getattr(self, name).update({str(k): {"fails": [float(t) for t in (v.get("fails") or [])],
                                                     "until": float(v.get("until") or 0.0)}
                                            for k, v in src.items() if isinstance(v, dict)})
        g = d.get("global")
        if isinstance(g, dict):
            self.glob = {"fails": [float(t) for t in (g.get("fails") or [])], "until": float(g.get("until") or 0.0)}

    def _prune(self, now: float) -> None:
        lo = self.cfg.lockout
        for tab in (self.prefix, self.agg):
            for k in list(tab):
                e = tab[k]
                e["fails"] = [t for t in e["fails"] if now - t < lo.window_s]
                if not e["fails"] and e["until"] <= now:
                    del tab[k]
        self.glob["fails"] = [t for t in self.glob["fails"] if now - t < lo.global_window_s]

    def flush(self, *, force: bool = False) -> None:
        """落盘锁定状态（合批 ≤ 1 Hz；force 时立即）。"""
        if not self._dirty and not force:
            return
        now_m = self.mono()
        if not force and now_m - self._last_flush < FLUSH_MIN_S:
            return
        doc = {"v": 1, "prefix": self.prefix, "agg": self.agg, "global": self.glob}
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(self.state_dir / LOCKOUT_NAME, json.dumps(doc, separators=(",", ":")).encode("utf-8"), 0o600)
            self._dirty = False
            self._last_flush = now_m
        except OSError:
            log.warning("identity lockout state not saved")

    def lock_state(self, addr: str | None, now: float | None = None) -> tuple[str, float] | None:
        """(范围, 剩余秒) 或 None。回环来源不受全站锁定。"""
        now = self.clock() if now is None else now
        pfx, agg = prefixes(addr)
        for scope, tab, key in (("prefix", self.prefix, self._h(pfx)), ("aggregate", self.agg, self._h(agg))):
            e = tab.get(key)
            if e is not None and e["until"] > now:
                return scope, e["until"] - now
        if self.glob["until"] > now and not _loopback(addr):
            return "global", self.glob["until"] - now
        return None

    def _rate_wait(self, key: str, now: float) -> float:
        if len(self.buckets) > 4096:
            self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < 60.0}
        tok, t0 = self.buckets.get(key, [self.cfg.rate_burst, now])
        tok = min(self.cfg.rate_burst, tok + max(0.0, now - t0) * self.cfg.rate_per_s)
        if tok >= 1.0:
            self.buckets[key] = [tok - 1.0, now]
            return 0.0
        self.buckets[key] = [tok, now]
        return (1.0 - tok) / self.cfg.rate_per_s

    def _fail(self, ph: str, ah: str, now: float) -> None:
        lo = self.cfg.lockout
        self._prune(now)
        e = self.prefix.setdefault(ph, {"fails": [], "until": 0.0})
        e["fails"].append(now)
        a = self.agg.setdefault(ah, {"fails": [], "until": 0.0})
        a["fails"].append(now)
        self.glob["fails"].append(now)
        self._dirty = True
        locked = False
        if len(e["fails"]) >= lo.prefix_fail and e["until"] <= now:
            e["until"], e["fails"] = now + lo.prefix_lock_s, []
            locked = True
            self._audit("auth.identity_locked", {"scope": "prefix", "prefix_h": ph, "until_unix_ns": str(int(e["until"] * 1e9))})
        if len(a["fails"]) >= lo.agg_fail and a["until"] <= now:
            a["until"], a["fails"] = now + lo.agg_lock_s, []
            locked = True
            self._audit("auth.identity_locked", {"scope": "aggregate", "agg_h": ah, "until_unix_ns": str(int(a["until"] * 1e9))})
        if len(self.glob["fails"]) >= lo.global_fail_h and self.glob["until"] <= now:
            self.glob["until"], self.glob["fails"] = now + lo.global_lock_s, []
            locked = True
            self._audit("auth.identity_global_lock", {"until_unix_ns": str(int(self.glob["until"] * 1e9))})
            log.warning("identity global lock engaged", extra={"kv": {"lock_s": lo.global_lock_s}})
        self.flush(force=locked)

    def _audit(self, kind: str, detail: dict) -> None:
        if self.audit is None:
            return
        with contextlib.suppress(Exception):
            self.audit(kind, detail)

    # ------------------------------------------------------------ 比对
    def decide(self, secret_in: str | bytes | None, addr: str | None, *, principal_id: str | None = None,
               issue: bool = True) -> Verdict:
        """不补时延的判定（调用方负责补足；`check`、`acheck` 已补足）。issue 为 False 时成功只返回 ok（R01 的 admin 签发）。"""
        now = self.clock()
        pfx, agg = prefixes(addr)
        ph, ah = self._h(pfx), self._h(agg)
        with self._lock:
            lk = self.lock_state(addr, now)
            if lk is not None:
                return Verdict("locked", scope=lk[0], retry_after_s=lk[1])
            wait = self._rate_wait(ph, now)
            if wait > 0:
                return Verdict("rate", retry_after_s=wait)
            if self._secret_digest is None:
                return Verdict("disabled")
            raw = secret_in.encode("utf-8") if isinstance(secret_in, str) else (secret_in or b"")
            good = hmac.compare_digest(hashlib.sha256(raw).digest(), self._secret_digest)
            if not good:
                self._fail(ph, ah, now)
                n = len((self.prefix.get(ph) or {}).get("fails") or [])
                self._audit("auth.identity_denied", {"prefix_h": ph, "count": n})
                return Verdict("wrong")
            if ph in self.prefix:
                self.prefix.pop(ph, None)
                self._dirty = True
                self.flush()
        if not issue:
            return Verdict("ok")
        tok, p = self.issue(principal_id, now=now)
        self._audit("auth.identity_unlock", {"principal": p["sub"], "prefix_h": ph, "jti": p["jti"]})
        return Verdict("ok", token=tok, payload=p)

    def _pad_s(self, started: float) -> float:
        target = self.cfg.delay_ms / 1000.0 + self.rng.uniform(-self.cfg.jitter_ms, self.cfg.jitter_ms) / 1000.0
        return max(0.0, target - (self.mono() - started))

    def check(self, secret_in: str | bytes | None, addr: str | None, **kw: Any) -> Verdict:
        """同步比对，返回前补足固定时延（阻塞当前线程；事件循环中用 `acheck`）。"""
        t0 = self.mono()
        v = self.decide(secret_in, addr, **kw)
        time.sleep(self._pad_s(t0))
        return v

    async def acheck(self, secret_in: str | bytes | None, addr: str | None, **kw: Any) -> Verdict:
        t0 = self.mono()
        v = self.decide(secret_in, addr, **kw)
        await asyncio.sleep(self._pad_s(t0))
        return v

    # ------------------------------------------------------------ 令牌
    def issue(self, principal_id: str | None = None, *, now: float | None = None) -> tuple[str, dict]:
        if self.k_ident is None:
            raise IdentityInvalid("disabled")
        now = self.clock() if now is None else now
        sub = principal_id if isinstance(principal_id, str) and principal_id.startswith("p-") else \
            "p-" + secrets.token_hex(16)
        p = {"v": 1, "sub": sub, "role": "internal", "scope": "identity", "mode": self.mode, "iat": int(now),
             "exp": int(now + self.cfg.token_ttl_s), "jti": secrets.token_hex(8)}
        return encode_token(p, self.k_ident), p

    def verify_token(self, token: str | None, *, now: float | None = None) -> dict:
        """身份令牌验签：格式、签名、版本、作用域、角色、过期与撤销；失败抛 IdentityInvalid。"""
        if self.k_ident is None:
            raise IdentityInvalid("disabled")
        if not isinstance(token, str) or len(token) > 4096:
            raise IdentityInvalid("format")
        try:
            ver, body, sig = token.split(".")
        except ValueError:
            raise IdentityInvalid("format") from None
        if ver != "v1":
            raise IdentityInvalid("version")
        try:
            want = hmac.new(self.k_ident, ("v1." + body).encode("ascii"), hashlib.sha256).digest()
            got = _b64d(sig)
        except (ValueError, UnicodeEncodeError):
            raise IdentityInvalid("encoding") from None
        if not hmac.compare_digest(want, got):
            raise IdentityInvalid("signature")
        try:
            p = json.loads(_b64d(body))
        except (ValueError, UnicodeDecodeError):
            raise IdentityInvalid("payload") from None
        if not isinstance(p, dict) or p.get("v") != 1 or p.get("scope") != "identity" or p.get("role") != "internal":
            raise IdentityInvalid("scope")
        now = self.clock() if now is None else now
        if float(p.get("exp") or 0) < now:
            raise IdentityInvalid("expired")
        if self.revoked.is_revoked(str(p.get("jti") or "")):
            raise IdentityInvalid("revoked")
        return p

    def revoke(self, token_or_payload: str | dict) -> dict | None:
        """撤销（R80）：写撤销表并审计 `auth.identity_logout`；令牌无效时返回 None。"""
        try:
            p = token_or_payload if isinstance(token_or_payload, dict) else self.verify_token(token_or_payload)
        except IdentityInvalid:
            return None
        jti = str(p.get("jti") or "")
        if not jti:
            return None
        self.revoked.add(jti, float(p.get("exp") or self.clock() + self.cfg.token_ttl_s))
        self._audit("auth.identity_logout", {"jti": jti, "principal": p.get("sub")})
        return p

    def is_revoked(self, jti: str | None) -> bool:
        return self.revoked.is_revoked(jti)
