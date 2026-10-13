"""公开站口径下的内部身份（WP-13b 第 2 轮，M16；D2-AC-51 中只在公开模式成立的条款；AWR-04 §15.9.2 至 §15.9.4；17 §17.20.5；
M11-FR-226）。

真实 supervisor 以 `public` profile 起栈（共享展示、show-api、sandbox-pool、sandbox-api；`net.access_mode = public`、受信代理
127.0.0.1、沙盒 `quota_profile = public` 与批处理关闭，与 emax 部署同一配置段），口令只在测试运行时随机生成（`tests/fixtures/identity.py`）。
地址前缀经受信代理的 `X-Forwarded-For` 区分（nginx 在部署中写入同一头）。

| 用例 | 断言 |
|---|---|
| R79 与 D1 R01 `admin_secret` 共用比对与计数 | 同一前缀 R79 错 3 次、R01 `{role: admin, admin_secret}` 错 2 次后，第 6 次（R01 正确口令）429 `516`，R79 正确口令同样 429 `516`（`scope = prefix`）；另一前缀的 R79 正确口令 200 |
| 比对的请求速率 | 另一前缀连发 6 次（不间隔）：前 3 次照常比对（突发 3），其后 429 `111`（每前缀 0.5 次/s） |
| 隐藏 REST 功能族 | viewer token 读 `/api/sys/procs`、`/api/runs`、`/api/sys/perf-reports`、`/api/rt/topics`、`/api/sys/audit`、`/api/rt/inspect` 均 404；同一请求加 `X-AWR-Identity` 后 200（AWR-04 §15.9.3"带身份令牌的 GET 按 admin 处理"）。第 2 轮实测 `/api/sys/audit` 与 `/api/rt/inspect` 为 403 `115`（路由的 admin 依赖不认身份令牌），记为 xfail 并请求 M11；`/api/jobs/**`、`/api/recon/**` 依赖扩展进程组（P1）不在此列 |
| 内部档沙盒与公开站批处理 | 带身份令牌、不带工作量证明创建 201（`tier: internal`；公开访客的工作量证明按触发计数自适应，本用例不判定）；该会话的批处理 SB-18 仍 404 `305`（公开站批处理关闭） |
"""

from __future__ import annotations

import sys
import time

import httpx
import pytest
from awrproc import ROOT, world_ready
from sbstack import SandboxStack

sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import identity as idfx

pytestmark = [pytest.mark.needs_data, pytest.mark.slow]

SITE = "https://drone4d.example.test"
GAP_S = 2.1                                          # 每前缀 0.5 次/s（突发 3）
P1 = "203.0.113.7"                                   # 文档用途地址（RFC 5737），各自一个 /64 与 /24 前缀
P2 = "198.51.100.9"
P3 = "192.0.2.33"


@pytest.fixture(scope="module")
def pub(tmp_path_factory):
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    d = tmp_path_factory.mktemp("ident-public")
    sf = idfx.secret_file(d)
    env = {**sf.env(), "AWR_ORIGINS": SITE}
    st = SandboxStack(profile="public", max_sessions=1, env=env,
                      extra_set=["run.scenario=s0-synthcity-showcase", f"identity.state_dir={sf.state_dir}"]).start()
    try:
        yield st, sf
    finally:
        st.stop()


def _h(prefix: str, **extra: str) -> dict[str, str]:
    return {"Origin": SITE, "X-Forwarded-For": prefix, **extra}


def _r79(st: SandboxStack, secret: str, prefix: str) -> httpx.Response:
    return httpx.post(st.show_base + "/api/auth/identity", json={"secret": secret}, headers=_h(prefix), timeout=15.0)


def _r01(st: SandboxStack, secret: str, prefix: str) -> httpx.Response:
    return httpx.post(st.show_base + "/api/auth/token", json={"role": "admin", "admin_secret": secret,
                                                             "principal_hint": "MSIXPUBADMINHINTAAAAAAAA"},
                      headers=_h(prefix), timeout=15.0)


def test_r79_and_r01_admin_secret_share_one_counter(pub) -> None:
    st, sf = pub
    for i in range(3):
        time.sleep(GAP_S)
        r = _r79(st, f"{sf.secret}-{i}", P1)
        assert r.status_code == 401 and r.json()["code"] == 304, r.text[:200]
    for i in range(2):
        time.sleep(GAP_S)
        r = _r01(st, f"{sf.secret}-r01-{i}", P1)
        assert r.status_code == 401 and r.json()["code"] == 304, r.text[:200]
        assert sf.secret not in r.text
    time.sleep(GAP_S)
    r = _r01(st, sf.secret, P1)                      # 第 6 次：锁定期内正确口令也不比对
    assert r.status_code == 429 and r.json()["code"] == 516, r.text[:300]
    assert (r.json().get("detail") or {}).get("scope") == "prefix"
    assert abs(int(r.headers["retry-after"]) - 1800) <= 60
    time.sleep(GAP_S)
    r = _r79(st, sf.secret, P1)
    assert r.status_code == 429 and r.json()["code"] == 516, r.text[:300]
    time.sleep(GAP_S)
    r = _r79(st, sf.secret, P2)                      # 其他前缀不受影响
    assert r.status_code == 200 and r.json()["tier"] == "internal", r.text[:300]


def test_r79_request_rate_per_prefix(pub) -> None:
    st, sf = pub
    codes = []
    for i in range(6):
        r = _r79(st, f"{sf.secret}-rate-{i}", P3)
        codes.append((r.status_code, r.json().get("code")))
        assert sf.secret not in r.text
    assert codes[:3] == [(401, 304)] * 3, codes                 # 突发 3：照常比对
    assert (429, 111) in codes[3:], codes                       # 其后超出 0.5 次/s
    assert all(c in ((429, 111), (401, 304)) for c in codes[3:]), codes


def _identity(st: SandboxStack, sf) -> str:
    time.sleep(GAP_S)
    r = _r79(st, sf.secret, P2)
    assert r.status_code == 200, r.text[:300]
    return str(r.json()["identity_token"])


HIDDEN = ["/api/sys/procs", "/api/runs", "/api/sys/perf-reports", "/api/rt/topics", "/api/sys/audit", "/api/rt/inspect"]


def test_hidden_rest_family_reads_with_the_identity(pub) -> None:
    st, sf = pub
    tok = _identity(st, sf)
    v = httpx.post(st.show_base + "/api/auth/token", json={"role": "viewer"}, headers=_h(P2), timeout=15.0)
    assert v.status_code == 200, v.text[:300]
    vt = v.json()["token"]
    plain = {p: httpx.get(st.show_base + p, headers=_h(P2, authorization=f"Bearer {vt}"), timeout=15.0).status_code for p in HIDDEN}
    assert set(plain.values()) == {404}, plain                                   # 公开策略：隐藏功能族对 viewer 404
    ident = {p: httpx.get(st.show_base + p, headers=_h(P2, authorization=f"Bearer {vt}", **{"x-awr-identity": tok}),
                          timeout=15.0) for p in HIDDEN}
    bad = {p: (r.status_code, r.json().get("code") if r.status_code >= 400 else None) for p, r in ident.items() if r.status_code != 200}
    assert not bad, bad                         # 含 /api/sys/audit 与 /api/rt/inspect（d2-WP-13b-to-M11 第 7 条，D2-INT 修复）


def test_internal_sandbox_without_pow_and_no_batch_on_the_public_site(pub) -> None:
    st, sf = pub
    body = {"world": "synthcity", "template": "t6_manual", "principal_hint": "MSIXPUBSANDBOXHINTAAAAAA"}
    tok = _identity(st, sf)
    r = httpx.post(st.base + "/api/sandbox/v1/sessions", json=body, headers=_h(P2, **{"x-awr-identity": tok}), timeout=30.0)
    assert r.status_code == 201, r.text[:300]
    j = r.json()
    sid, stok = j["session"]["sid"], j["token"]
    try:
        import base64
        import json

        part = stok.split(".")[1]
        assert json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("tier") == "internal"
        b = httpx.post(st.base + f"/api/sandbox/v1/sessions/{sid}/batch", json={"until_s": 60},
                       headers=_h(P2, authorization=f"Bearer {stok}"), timeout=30.0)
        assert b.status_code == 404 and b.json()["code"] == 305, b.text[:300]
    finally:
        httpx.delete(st.base + f"/api/sandbox/v1/sessions/{sid}", headers=_h(P2, authorization=f"Bearer {stok}"), timeout=15.0)
