"""内部身份库 `awr.sandbox.identity`（M17-FR-077、NFR-018、AC-031；AWR-04 §15.9.2、§15.9.3、§15.9.5；D2-AC-51 的库部分）。

全部用例使用运行时生成的随机口令与临时密钥文件（`tests/fixtures/identity.make_secret_file`），仓库中没有任何口令：密钥文件规则（首行、权限、
属主）、比对与身份令牌、错误与未配置的映射、前缀锁定（5 次 / 10 min → 30 min，落盘后新实例仍锁定，其他前缀不受影响，锁定期内
不比对不计数）、聚合前缀与全站锁定（回环免全站锁定）、成功清零、速率限制、撤销表 ≤ 2 s 对另一读者生效、轮换口令使令牌失效、
固定时延（正确与错误的中位数差 ≤ 10 ms）、审计与状态文件与日志中不出现口令和令牌。"""

from __future__ import annotations

import json
import logging
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
from identity import make_secret_file  # 正式夹具（WP-13b，d2-WP-13b-to-M17 第 1 条）

from awr.sandbox.identity import IdentityCfg, IdentityInvalid, IdentityService, read_secret_file


class Clk:
    def __init__(self) -> None:
        self.t = 1_791_000_000.0

    def __call__(self) -> float:
        return self.t


def _svc(tmp: Path, secret: str | None, clk: Clk, audit: list | None = None, **kw: Any) -> IdentityService:
    return IdentityService(secret=secret.encode() if secret else None, state_dir=tmp / "identity", clock=clk,
                           mono=lambda: clk.t, audit=(lambda k, d: audit.append((k, d))) if audit is not None else None, **kw)


def test_secret_file_rules(tmp_path: Path) -> None:
    p, sec = make_secret_file(tmp_path)
    got, why = read_secret_file(p)
    assert got == sec.encode() and why is None                     # 首行，去掉换行
    p2, sec2 = make_secret_file(tmp_path, newline=False)
    assert read_secret_file(p2)[0] == sec2.encode()
    p3, _ = make_secret_file(tmp_path, mode=0o644)
    assert read_secret_file(p3) == (None, "permissions")
    empty = tmp_path / "empty.secret"
    empty.write_bytes(b"\n")
    empty.chmod(0o600)
    assert read_secret_file(empty) == (None, "empty")
    assert read_secret_file(tmp_path / "missing") == (None, "missing")
    assert read_secret_file(None) == (None, "unset")
    svc = IdentityService.from_env({"AWR_ADMIN_SECRET_FILE": str(p3), "AWR_IDENTITY_DIR": str(tmp_path / "i")})
    assert not svc.enabled and svc.disabled_why == "permissions"
    svc = IdentityService.from_env({"AWR_ADMIN_SECRET_FILE": str(p), "AWR_IDENTITY_DIR": str(tmp_path / "i")})
    assert svc.enabled
    assert (tmp_path / "i").stat().st_mode & 0o777 == 0o700


def test_check_ok_issues_identity_token(tmp_path: Path) -> None:
    _p, sec = make_secret_file(tmp_path)
    clk = Clk()
    audit: list = []
    svc = _svc(tmp_path, sec, clk, audit)
    v = svc.decide(sec, "198.51.100.7", principal_id="p-" + "a" * 32)
    assert v.ok and v.problem() == (200, 0, None, None) and v.token and "token" not in repr(v)
    p = svc.verify_token(v.token)
    assert p["role"] == "internal" and p["scope"] == "identity" and p["sub"] == "p-" + "a" * 32
    assert p["exp"] - p["iat"] == 43200 and p["v"] == 1 and len(p["jti"]) == 16
    assert audit[-1][0] == "auth.identity_unlock" and audit[-1][1]["jti"] == p["jti"]
    other = _svc(tmp_path / "x", sec, clk)                         # 另一进程（sandbox-api）以同一密钥派生 K_ident
    assert other.verify_token(v.token)["jti"] == p["jti"]
    clk.t += 43201
    with pytest.raises(IdentityInvalid) as e:
        svc.verify_token(v.token)
    assert e.value.why == "expired"
    for bad in ("", "v1.x", "v1.a.b", v.token[:-2] + "AA", "v2." + v.token[3:]):
        with pytest.raises(IdentityInvalid):
            svc.verify_token(bad)
    assert svc.decide(sec, "198.51.100.7", issue=False).token is None   # R01：admin 签发只要比对结果


def test_wrong_and_disabled_mapping(tmp_path: Path) -> None:
    _p, sec = make_secret_file(tmp_path)
    clk = Clk()
    svc = _svc(tmp_path, sec, clk)
    w = svc.decide(sec + "x", "198.51.100.7")
    assert w.kind == "wrong" and w.problem()[:2] == (401, 304)
    off = _svc(tmp_path / "off", None, clk)
    d = off.decide("anything", "198.51.100.7")
    assert d.kind == "disabled" and d.problem()[:3] == (403, 115, {"why": "PUBLIC_READONLY"})
    with pytest.raises(IdentityInvalid):
        off.verify_token("v1.a.b")


def test_prefix_lockout_persists_and_isolates(tmp_path: Path) -> None:
    _p, sec = make_secret_file(tmp_path)
    clk = Clk()
    audit: list = []
    svc = _svc(tmp_path, sec, clk, audit)
    for _ in range(5):
        clk.t += 2.5
        assert svc.decide("wrong", "198.51.100.7").kind == "wrong"
    clk.t += 2.5
    v = svc.decide(sec, "198.51.100.7")                             # 第 6 次（即使口令正确）：锁定，不比对
    http, code, det, hdr = v.problem()
    assert (http, code) == (429, 516) and det["scope"] == "prefix" and abs(int(hdr["Retry-After"]) - 1800) <= 3
    assert any(k == "auth.identity_locked" for k, _ in audit)
    assert svc.decide(sec, "203.0.113.5").ok                        # 其他前缀不受影响
    restarted = _svc(tmp_path, sec, clk)                            # show-api 重启：锁定保留
    assert restarted.decide(sec, "198.51.100.7").kind == "locked"
    clk.t += 1801
    assert restarted.decide(sec, "198.51.100.7").ok


def test_aggregate_and_global_lock_with_loopback_exempt(tmp_path: Path) -> None:
    _p, sec = make_secret_file(tmp_path)
    clk = Clk()
    svc = _svc(tmp_path, sec, clk)
    for i in range(10):                                              # 同一 /24 的 10 个地址，各 1 次
        clk.t += 1
        assert svc.decide("wrong", f"198.51.100.{i + 1}").kind == "wrong"
    v = svc.decide(sec, "198.51.100.200")
    assert v.kind == "locked" and v.scope == "aggregate"
    g = _svc(tmp_path / "g", sec, clk)
    for i in range(30):                                              # 30 个不同 /24：全站锁定
        clk.t += 1
        g.decide("wrong", f"10.{i}.0.1")
    v = g.decide(sec, "192.0.2.10")
    assert v.kind == "locked" and v.scope == "global" and abs(v.retry_after_s - 900) <= 1
    assert g.decide(sec, "127.0.0.1").ok                            # 回环来源（运维 SSH 转发）不受全站锁定
    assert g.decide(sec, "::1").ok


def test_success_resets_and_rate_limit(tmp_path: Path) -> None:
    _p, sec = make_secret_file(tmp_path)
    clk = Clk()
    svc = _svc(tmp_path, sec, clk)
    for _ in range(4):
        clk.t += 2.5
        svc.decide("wrong", "198.51.100.7")
    clk.t += 2.5
    assert svc.decide(sec, "198.51.100.7").ok
    for _ in range(4):                                               # 计数已清零：再 4 次仍不锁定
        clk.t += 2.5
        assert svc.decide("wrong", "198.51.100.7").kind == "wrong"
    r = _svc(tmp_path / "r", sec, clk)
    kinds = [r.decide("wrong", "203.0.113.5").kind for _ in range(4)]    # 0.5 次/s、突发 3
    assert kinds == ["wrong", "wrong", "wrong", "rate"]
    v = r.decide(sec, "203.0.113.5")
    assert v.kind == "rate" and v.problem()[:2] == (429, 111)


def test_revocation_reaches_other_reader_within_2s(tmp_path: Path) -> None:
    _p, sec = make_secret_file(tmp_path)
    writer = IdentityService(secret=sec.encode(), state_dir=tmp_path / "identity")
    reader = IdentityService(secret=sec.encode(), state_dir=tmp_path / "identity")
    v = writer.decide(sec, "198.51.100.7")
    assert reader.verify_token(v.token)
    t0 = time.monotonic()
    assert writer.revoke(v.token)["jti"] == v.payload["jti"]
    with pytest.raises(IdentityInvalid):
        writer.verify_token(v.token)
    while True:
        try:
            reader.verify_token(v.token)
        except IdentityInvalid as e:
            assert e.why == "revoked"
            break
        assert time.monotonic() - t0 < 2.0
        time.sleep(0.05)
    doc = json.loads((tmp_path / "identity" / "revoked.json").read_text())
    assert list(doc["revoked"]) == [v.payload["jti"]]
    assert (tmp_path / "identity" / "revoked.json").stat().st_mode & 0o777 == 0o600


def test_rotation_invalidates_tokens(tmp_path: Path) -> None:
    _p, sec = make_secret_file(tmp_path)
    _p2, sec2 = make_secret_file(tmp_path)
    a = IdentityService(secret=sec.encode(), state_dir=tmp_path / "identity")
    tok = a.decide(sec, "198.51.100.7").token
    b = IdentityService(secret=sec2.encode(), state_dir=tmp_path / "identity")
    with pytest.raises(IdentityInvalid) as e:
        b.verify_token(tok)
    assert e.value.why == "signature"


def test_fixed_delay_median_difference(tmp_path: Path) -> None:
    _p, sec = make_secret_file(tmp_path)
    svc = IdentityService(secret=sec.encode(), state_dir=tmp_path / "identity", rng=random.Random(7),
                          cfg=IdentityCfg(rate_per_s=1000, rate_burst=1000))
    good, bad = [], []
    for i in range(9):
        for ok in (True, False):
            t0 = time.perf_counter()
            v = svc.check(sec if ok else "wrong", f"198.51.{i}.{1 if ok else 2}")
            (good if ok else bad).append(time.perf_counter() - t0)
            assert v.ok is ok
    mg, mb = statistics.median(good), statistics.median(bad)
    assert 0.27 <= mg <= 0.36 and 0.27 <= mb <= 0.36
    assert abs(mg - mb) <= 0.010


def test_no_secret_or_token_in_audit_state_or_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _p, sec = make_secret_file(tmp_path)
    clk = Clk()
    audit: list = []
    caplog.set_level(logging.DEBUG)
    svc = _svc(tmp_path, sec, clk, audit)
    toks = []
    for i in range(40):
        clk.t += 1
        v = svc.decide(sec if i % 7 == 0 else sec[::-1], f"10.{i}.1.1")
        if v.token:
            toks.append(v.token)
    svc.revoke(toks[0])
    svc.flush(force=True)
    blobs = [json.dumps(audit, ensure_ascii=False), caplog.text]
    blobs += [f.read_bytes().decode("latin-1") for f in (tmp_path / "identity").iterdir()]
    for b in blobs:
        assert sec not in b and sec[::-1] not in b
        assert not any(t in b for t in toks)
    kinds = {k for k, _ in audit}
    assert {"auth.identity_unlock", "auth.identity_denied", "auth.identity_global_lock", "auth.identity_logout"} <= kinds
