"""内部身份的真实栈用例（WP-13b，M16；D2-AC-51 的服务端部分；AWR-04 §15.9；17 §17.20.5；M17-FR-076、FR-077）。

真实 supervisor（`sandbox` profile，N_max = 1）+ sandbox-pool + sandbox-api；口令只在测试运行时随机生成并写入临时密钥文件
（`tests/fixtures/identity.py`），栈经 `AWR_ADMIN_SECRET_FILE`、`AWR_IDENTITY_STATE_DIR` 读取。

两组用例：

1. **池与 sandbox-api（内部档）**：以同一密钥派生的身份令牌创建会话（R79 尚未由 show-api 提供时由夹具直接签发，与服务端同一
   `K_ident` 派生）：公开槽位已满时用预留槽位、沙盒 token 带 `tier: internal`、寿命上限 8 h、每个身份同时 ≤ 2（会话加排队票，
   内部票排在公开票之前）；他人密钥签发或格式错误的令牌 401 `302`；撤销后 ≤ 2 s 拒绝新建（`identity_revoked`）。
2. **R79/R80（show-api）**：错误口令 401 `304`；同一前缀第 6 次 429 `516`（`Retry-After` ≈ 1800 s，`scope = prefix`）且锁定期内正确
   口令也不比对；成功签发的令牌被池接受；正确与错误的响应时间中位数差 ≤ 10 ms（各约 300 ms；`M16_PERF=1` 时各 30 次、
   另起放宽锁定阈值的栈，功能运行各 4 次只判定 ≤ 25 ms 的抖动幅度）；R80 撤销后 ≤ 2 s 在池侧失效；
   锁定在服务重启后保留；审计含 unlock、denied、locked、logout 且不含口令与令牌；栈的全部日志、审计与状态文件中口令 0 处；
   REL-04 对植入口令的临时文件报告且不打印口令，对 lint 范围 0 处。R79/R80 自 D2-INT 起由 show-api 提供（WP-03b），第 2 轮起
   show-api 不提供即失败；`M16_IDENTITY_STANDIN=1` 时仍可改对 `identity_standin`（同一身份库的薄壳）运行，只用于在旧栈上验证用例本身。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx
import pytest
from awrproc import ROOT, world_ready
from identity_standin import IdentityStandin, real_r79
from sbstack import SandboxStack, sdk

sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import identity as idfx

pytestmark = [pytest.mark.needs_data, pytest.mark.slow]

A = sdk()
HINT = {k: f"MSIXIDEN{k}HINTAAAAAAAAAAAAAAA"[:24] for k in ("A", "B", "C", "D", "E", "F")}
RATE_GAP_S = 2.1                                    # 每前缀 0.5 次/s（突发 3）
PERF = os.environ.get("M16_PERF") == "1"


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    d = tmp_path_factory.mktemp("ident")
    sf = idfx.secret_file(d)
    st = SandboxStack(max_sessions=1, env=sf.env()).start()
    standin = None
    try:
        if real_r79(st.show_base):
            r79, kind = st.show_base, "show-api"
        elif os.environ.get("M16_IDENTITY_STANDIN") == "1":
            standin = IdentityStandin({**os.environ, **sf.env()}).start()
            r79, kind = standin.url, "standin"
        else:
            r79, kind = None, None
        yield {"st": st, "sf": sf, "r79": r79, "kind": kind, "standin": standin, "dir": d}
    finally:
        if standin is not None:
            standin.stop()
        st.stop()


def _payload(tok: str) -> dict:
    body = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _create(st: SandboxStack, hint: str, ident: str | None, *, template: str = "t6_manual") -> httpx.Response:
    hdr = {"x-awr-identity": ident} if ident else {}
    return httpx.post(st.base + "/api/sandbox/v1/sessions", json={"world": "synthcity", "template": template, "principal_hint": hint},
                      headers=hdr, timeout=30.0)


def _end(st: SandboxStack, sid: str, token: str) -> None:
    httpx.delete(st.base + f"/api/sandbox/v1/sessions/{sid}", headers={"authorization": "Bearer " + token}, timeout=15.0)


def _drop_ticket(st: SandboxStack, q: dict) -> None:
    httpx.delete(st.base + f"/api/sandbox/v1/queue/{q['ticket_id']}", headers={"authorization": f"Bearer {q['ticket_token']}"}, timeout=15.0)


def _wait_empty(st: SandboxStack, timeout_s: float = 45.0) -> None:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        c = st.capacity()["sandbox"]
        if c["active"] == 0 and not c.get("internal_active") and c["queue"] == 0:
            return
        time.sleep(0.3)
    raise AssertionError(f"pool not empty: {st.capacity()['sandbox']}")


# ---------------------------------------------------------------- 1 池与 sandbox-api
def test_internal_tier_reserved_slot_token_lifetime_and_per_identity_limit(ctx) -> None:
    st, sf = ctx["st"], ctx["sf"]
    tok, _ = idfx.issue_token(sf)
    pub = _create(st, HINT["A"], None)
    assert pub.status_code == 201, pub.text[:300]
    pub_j = pub.json()
    made: list[tuple[str, str]] = [(pub_j["session"]["sid"], pub_j["token"])]
    tickets: list[dict] = []
    try:
        r1 = _create(st, HINT["B"], tok)
        assert r1.status_code == 201, r1.text[:300]
        j1 = r1.json()
        made.append((j1["session"]["sid"], j1["token"]))
        assert _payload(j1["token"]).get("tier") == "internal"
        s1 = j1["session"]
        life = (int(s1["lifetime_max_unix_ns"]) - int(s1["created_unix_ns"])) / 1e9
        assert abs(life - 28800) <= 60, life                      # 内部档寿命上限 8 h（公开 2 h）
        # 一个公开访客排队，随后同一身份的第二个请求：内部票排在公开票之前（不抢占公开会话）
        rp = _create(st, HINT["C"], None)
        assert rp.status_code == 202, rp.text[:300]
        tickets.append(rp.json()["queue"])
        r2 = _create(st, HINT["D"], tok)
        assert r2.status_code == 202, r2.text[:300]
        q2 = r2.json()["queue"]
        tickets.append(q2)
        assert q2["position"] == 1 and q2.get("kind") == "internal", q2
        vp = httpx.get(st.base + f"/api/sandbox/v1/queue/{tickets[0]['ticket_id']}", params={"wait_s": 0},
                       headers={"authorization": f"Bearer {tickets[0]['ticket_token']}"}, timeout=15.0).json()
        assert vp.get("position") == 2, vp
        # 第三个：每个身份同时 ≤ 2（会话加排队票）
        r3 = _create(st, HINT["E"], tok)
        assert r3.status_code == 409 and r3.json()["code"] == 504, r3.text[:300]
        assert (r3.json().get("detail") or {}).get("why") == "per_identity"
        # 公开会话仍在（未被抢占）
        g = httpx.get(st.base + f"/api/sandbox/v1/sessions/{made[0][0]}", headers={"authorization": "Bearer " + made[0][1]}, timeout=15.0)
        assert g.json()["state"] in ("READY", "ACTIVE", "SPAWNING")
    finally:
        for q in tickets:
            _drop_ticket(st, q)
        for sid, t in made:
            _end(st, sid, t)
        _wait_empty(st)


def test_foreign_malformed_and_revoked_identity_tokens(ctx, tmp_path) -> None:
    st, sf = ctx["st"], ctx["sf"]
    other = idfx.secret_file(tmp_path)                            # 另一部署的密钥
    ftok, _ = idfx.issue_token(other)
    r = _create(st, HINT["A"], ftok)
    assert r.status_code == 401 and r.json()["code"] == 302, r.text[:300]
    assert (r.json().get("detail") or {}).get("why") == "identity_signature"
    r = _create(st, HINT["A"], "v1.not-a-token")
    assert r.status_code == 401 and r.json()["code"] == 302, r.text[:300]
    # 撤销：≤ 2 s 内拒绝新建（池按修改时间 ≤ 1 Hz 重载撤销表）
    tok, _ = idfx.issue_token(sf)
    idfx.revoke(sf, tok)
    t0 = time.monotonic()
    code = None
    while time.monotonic() - t0 < 2.5:
        rr = _create(st, HINT["F"], tok)
        if rr.status_code == 201:
            _end(st, rr.json()["session"]["sid"], rr.json()["token"])
            _wait_empty(st)
        else:
            code = (rr.status_code, rr.json().get("code"), (rr.json().get("detail") or {}).get("why"))
            break
        time.sleep(0.2)
    assert code == (401, 302, "identity_revoked"), code
    assert time.monotonic() - t0 <= 2.5


# ---------------------------------------------------------------- 2 R79/R80
def _need_r79(ctx) -> str:
    if ctx["r79"] is None:
        pytest.fail("show-api does not serve R79 POST /api/auth/identity (M11 WP-03b, delivered in D2-INT); M16_IDENTITY_STANDIN=1 "
                    "runs these cases against tests/e2e/identity_standin.py on an older stack")
    return ctx["r79"]


def _r79(base: str, secret: str) -> tuple[httpx.Response, float]:
    t0 = time.perf_counter()
    r = httpx.post(base + "/api/auth/identity", json={"secret": secret}, timeout=15.0)
    return r, time.perf_counter() - t0


def test_r79_ok_token_accepted_by_the_pool(ctx) -> None:
    base = _need_r79(ctx)
    st, sf = ctx["st"], ctx["sf"]
    r, _ = _r79(base, sf.secret)
    assert r.status_code == 200, r.text[:300]
    j = r.json()
    assert j["tier"] == "internal" and j["identity_token"].startswith("v1.")
    exp_s = int(j["exp_unix_ns"]) / 1e9
    assert abs(exp_s - (time.time() + 43200)) <= 120
    p = _payload(j["identity_token"])
    assert p["scope"] == "identity" and p["role"] == "internal" and p["exp"] - p["iat"] == 43200
    rc = _create(st, HINT["A"], j["identity_token"])
    assert rc.status_code == 201, rc.text[:300]
    assert _payload(rc.json()["token"]).get("tier") == "internal"
    _end(st, rc.json()["session"]["sid"], rc.json()["token"])
    _wait_empty(st)


@pytest.fixture(scope="module")
def timing(ctx, tmp_path_factory):
    """恒定时延的样本量受锁定阈值限制（聚合前缀 10 次/10 min 不因成功清零）：功能运行在主栈上各 4 次；M16_PERF=1 时另起一个栈，以
    `AWR_IDENTITY_JSON` 放宽锁定阈值（与时延无关），各 30 次。"""
    if not PERF:
        yield {"base": ctx["r79"], "sf": ctx["sf"], "n": 4, "kind": ctx["kind"]}
        return
    _need_r79(ctx)
    d = tmp_path_factory.mktemp("ident-timing")
    sf = idfx.secret_file(d)
    env = {**sf.env(), "AWR_IDENTITY_JSON": json.dumps({"lockout": {"prefix_fail": 1000, "agg_fail": 1000, "global_fail_h": 1000}})}
    st = SandboxStack(max_sessions=1, env=env).start()
    standin = None
    try:
        if real_r79(st.show_base):
            base = st.show_base
        else:
            standin = IdentityStandin({**os.environ, **env}).start()
            base = standin.url
        yield {"base": base, "sf": sf, "n": 30, "kind": ctx["kind"]}
    finally:
        if standin is not None:
            standin.stop()
        st.stop()


def test_r79_constant_time(ctx, timing) -> None:
    """正确与错误交错（错误之后总有一次成功）。补足时延带 ±25 ms 均匀抖动，中位数差 ≤ 10 ms 是统计判据：M16_PERF=1 时各 30 次判定
    10 ms；功能运行各 4 次，只判定不超过抖动幅度（25 ms）。"""
    _need_r79(ctx)
    base, sf, n = timing["base"], timing["sf"], timing["n"]
    good: list[float] = []
    bad: list[float] = []
    for i in range(2 * n):
        time.sleep(RATE_GAP_S)
        rr, dt = _r79(base, sf.secret if i % 2 else sf.secret + "x")
        assert rr.status_code == (200 if i % 2 else 401), rr.text[:200]
        (good if i % 2 else bad).append(dt)
    mg, mb = statistics.median(good), statistics.median(bad)
    print(f"[WP-13b] R79 median ok {mg * 1e3:.1f} ms, wrong {mb * 1e3:.1f} ms over {n} each ({timing['kind']})")
    assert 0.27 <= mg <= 0.45 and 0.27 <= mb <= 0.45, (good, bad)
    assert abs(mg - mb) <= (0.010 if PERF else 0.025), (mg, mb)


def test_r80_logout_invalidates_the_token_within_2s(ctx) -> None:
    base = _need_r79(ctx)
    st, sf = ctx["st"], ctx["sf"]
    time.sleep(RATE_GAP_S)
    r, _ = _r79(base, sf.secret)
    tok = r.json()["identity_token"]
    d = httpx.delete(base + "/api/auth/identity", headers={"x-awr-identity": tok}, timeout=15.0)
    assert d.status_code == 204, d.text[:300]
    t0 = time.monotonic()
    rr = None
    while time.monotonic() - t0 < 2.5:
        rr = _create(st, HINT["B"], tok)
        if rr.status_code == 401:
            break
        if rr.status_code == 201:
            _end(st, rr.json()["session"]["sid"], rr.json()["token"])
            _wait_empty(st)
        time.sleep(0.2)
    assert rr is not None and rr.status_code == 401 and (rr.json().get("detail") or {}).get("why") == "identity_revoked", rr.text[:300]


def test_r79_wrong_then_locked_on_the_6th_and_lock_survives_a_restart(ctx) -> None:
    base = _need_r79(ctx)
    sf = ctx["sf"]
    for i in range(5):
        time.sleep(RATE_GAP_S)
        r, _ = _r79(base, f"{sf.secret}-{i}")
        assert r.status_code == 401 and r.json()["code"] == 304, r.text[:200]
        assert sf.secret not in r.text
    time.sleep(RATE_GAP_S)
    r, _ = _r79(base, sf.secret)                       # 锁定期内不比对：正确口令同样 429 516
    assert r.status_code == 429 and r.json()["code"] == 516, r.text[:300]
    ra = int(r.headers["retry-after"])
    assert abs(ra - 1800) <= 60, ra
    assert (r.json().get("detail") or {}).get("scope") == "prefix"
    # 重启后锁定仍在（var/identity/lockout.json）
    if ctx["kind"] == "standin":
        ctx["standin"].stop()
        ctx["standin"] = IdentityStandin({**os.environ, **sf.env()}, port=ctx["standin"].port).start()
    else:
        import signal

        pid = ctx["st"].pid_of("api")
        os.kill(pid, signal.SIGKILL)
        end = time.monotonic() + 60
        time.sleep(1.0)
        # 进程表经 show-api 本身查询（重启期间连接被拒）：等到健康检查可达且 pid 已换（D2-INT）
        while time.monotonic() < end:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(base + "/api/health/live", timeout=2.0).status_code == 200 and ctx["st"].pid_of("api") not in (0, pid):
                    break
            time.sleep(0.5)
    time.sleep(RATE_GAP_S)
    r, _ = _r79(base, sf.secret)
    assert r.status_code == 429 and r.json()["code"] == 516, r.text[:300]


def _audit_lines(ctx) -> list[dict]:
    out: list[dict] = []
    paths: list[Path] = []
    if ctx["kind"] == "standin" and ctx["standin"].audit_path:
        paths.append(ctx["standin"].audit_path)
    st = ctx["st"]
    if st.tmp is not None:
        paths += list((st.tmp / "runs").rglob("audit.jsonl"))
    for p in paths:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def test_audit_kinds_of_r79(ctx) -> None:
    _need_r79(ctx)
    kinds = {a.get("kind") for a in _audit_lines(ctx)}
    for k in ("auth.identity_unlock", "auth.identity_denied", "auth.identity_locked", "auth.identity_logout"):
        assert k in kinds, sorted(x for x in kinds if x)


def test_no_secret_or_token_anywhere(ctx) -> None:
    """栈的全部日志、审计（共享展示与池）、状态目录与身份库状态中：口令 0 处，身份令牌不入审计（只记 jti）。"""
    sf, st = ctx["sf"], ctx["st"]
    roots = [st.tmp, sf.state_dir] + ([ctx["standin"].audit_path] if ctx.get("standin") else [])
    hits = [h for h in idfx.scan_paths([r for r in roots if r], sf.secret) if h[0] != str(sf.path)]
    assert hits == [], hits
    for a in _audit_lines(ctx) + st.pool_audit():
        assert "v1." not in json.dumps(a.get("detail") or {}), a.get("kind")


def test_rel04_reports_a_planted_secret_without_printing_it(ctx, tmp_path) -> None:
    sf = ctx["sf"]
    planted = tmp_path / "planted.txt"
    planted.write_text(f"line one\nkey = {sf.secret}\n", encoding="utf-8")
    r = idfx.rel04(sf.path, files=[planted])
    assert r.findings == 1 and not r.secret_in_output, r
    clean = tmp_path / "clean.txt"
    clean.write_text("nothing here\n", encoding="utf-8")
    r = idfx.rel04(sf.path, files=[clean])
    assert r.findings == 0 and not r.secret_in_output, r
