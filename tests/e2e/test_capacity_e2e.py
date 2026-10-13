"""公开容量接口与实例用户口径的真实栈用例（WP-13b，M16；D2-AC-42、D2-AC-43 的沙盒口径、D2-AC-40 的服务端一致性；
AWR-04 §15.4、§15.5.4、§15.6.1；17 §17.20.1、§17.20.2；M17-FR-068、FR-069、FR-076）。

真实 supervisor（`sandbox` profile，N_max = 1）+ sandbox-pool + sandbox-api，池以真实 sim-core 拉起会话。栈环境：
`AWR_RELEASE_FILE`（测试写的部署目录形式 `RELEASE`）、`AWR_SHOW_STATUS_FILE`（show-api 的观看席位状态文件；WP-03b 交付前由本用例
写入，验证池的读取与 5 s 停更判定）、测试运行时生成的身份密钥文件（内部预留槽位）。

| 用例 | 断言 |
|---|---|
| 公开与形状 | 无 token 200；≤ 1 KiB；通过 `awr.capacity.v1` schema（契约 1.2.0 合入前用 M17 草案）；`Cache-Control: public, max-age=1`；不含任何访客标识（principal、sid、地址、token） |
| 构建信息 | `build` 与 RELEASE 逐字段一致（D2-AC-40 的服务端一侧）；`power` 为服务端节能阈值 |
| 与真值一致 | 创建、驻留、恢复、结束后 ≤ 2 s 内 `sandbox.active`、`parked`、`state`、`admit_now`、`hint` 与池一致；SPAWNING 至 DRAINING 占槽位、PARKED 不占 |
| 内部预留 | 公开槽位已满时内部档会话使用预留槽位：`internal_active` 1、`active` 不变（分母仍为 N_max），总占用 N_max + 1 |
| 观看席位 | 状态文件新鲜时 `viewers` 取其值并推出 `queue_viewer`、`queue_both`；停更 > 5 s 为 `unknown` |
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from awrproc import ROOT, world_ready
from sbstack import SandboxStack, sdk

sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import identity as idfx

pytestmark = [pytest.mark.needs_data, pytest.mark.slow]

A = sdk()
RELEASE = {"release": "20261006-000000-abcdef0123", "version": "0.2.0", "channel": "ci", "commit": "abcdef0123", "dirty": "false",
           "built_at": "2026-10-06T00:00:00Z"}
POWER = {"viewer_idle_s": 41, "sandbox_idle_s": 43, "hidden_s": 7, "warn_s": 9}
HINT = {k: f"MSIXCAPA{k}HINTAAAAAAAAAAAAAAA"[:24] for k in ("A", "B", "C", "D")}


def _schema() -> dict | None:
    for p in (ROOT / "packages/contracts/sandbox/capacity.schema.json", ROOT / "tests/sandbox/fixtures/contracts_draft/capacity.schema.json",
              ROOT / ".cache/impl/requests/d2-WP-04b-drafts/sandbox/capacity.schema.json"):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    d = tmp_path_factory.mktemp("cap")
    rel = d / "RELEASE"
    rel.write_text("".join(f"{k}={v}\n" for k, v in RELEASE.items()), encoding="utf-8")
    show = d / "show.json"
    sf = idfx.secret_file(d)
    show_out = d / "show-real.json"   # show-api 的真实写入（AWR_SHOW_STATUS_OUT），池读合成的 show.json
    env = {"AWR_RELEASE_FILE": str(rel), "AWR_SHOW_STATUS_FILE": str(show), "AWR_SHOW_STATUS_OUT": str(show_out),
           "AWR_POWER_JSON": json.dumps(POWER), **sf.env()}
    st = SandboxStack(max_sessions=1, env=env).start()
    try:
        yield {"st": st, "show": show, "sf": sf, "show_out": show_out}
    finally:
        st.stop()


def _cap(st: SandboxStack) -> httpx.Response:
    return httpx.get(st.base + "/api/sandbox/v1/capacity", timeout=5.0)


def _until(st: SandboxStack, pred, timeout_s: float = 2.5) -> dict:
    """D2-AC-42"与真值在 2 s 内一致"：sandbox-api 1 s 缓存 + 池 1 Hz 统计。"""
    end = time.monotonic() + timeout_s
    c = {}
    while time.monotonic() < end:
        c = _cap(st).json()
        if pred(c):
            return c
        time.sleep(0.1)
    raise AssertionError(f"capacity not consistent within {timeout_s} s: {json.dumps(c)[:600]}")


def _write_show(p: Path, live: int, mx: int, queue: int = 0, eta_s: int | None = None, age_s: float = 0.0) -> None:
    doc = {"t_unix_ns": str(int((time.time() - age_s) * 1e9)), "live": live, "max": mx, "queue": queue, "eta_s": eta_s}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(p)


def _wait_gone(st: SandboxStack, s: A.Session) -> None:
    s.close()
    _until(st, lambda c: c["sandbox"]["active"] == 0 and c["sandbox"]["internal_active"] in (0, None), 45.0)


def test_public_shape_size_headers_and_build(ctx) -> None:
    st = ctx["st"]
    r = _cap(st)
    assert r.status_code == 200
    assert len(r.content) <= 1024, len(r.content)
    cc = r.headers.get("cache-control", "")
    assert "public" in cc and "max-age=1" in cc, cc
    d = r.json()
    sch = _schema()
    if sch is not None:
        from jsonschema import Draft202012Validator

        errs = sorted(Draft202012Validator(sch).iter_errors(d), key=lambda e: list(e.path))
        assert not errs, [f"{list(e.path)}: {e.message}" for e in errs]
    assert d["schema"] == "awr.capacity.v1"
    b = d["build"]
    assert {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in b.items()} == RELEASE
    assert d["power"] == {k: float(v) for k, v in POWER.items()}
    assert d["sandbox"]["max"] == 1 and d["sandbox"]["reserved_internal"] == 1
    # 无访客标识：响应中不出现地址、principal 提示、会话号与 token 形状
    raw = r.text
    for bad in ("127.0.0.1", "principal", "sb-", "v1.", "token"):
        assert bad not in raw, bad


def test_instance_user_semantics_follow_the_pool_within_2s(ctx) -> None:
    st = ctx["st"]
    _until(st, lambda c: c["sandbox"]["active"] == 0 and c["hint"] in ("open", "unavailable"), 10.0)
    sb = A.Sandbox(st.base, principal_hint=HINT["A"], auto_keepalive=False, timeout_s=30.0)
    s = sb.create("synthcity", "t1_point_watch", wait_ready=False)
    try:
        # SPAWNING 起即占槽位
        c = _until(st, lambda c: c["sandbox"]["active"] == 1)
        assert c["sandbox"]["state"] == "full" and c["sandbox"]["admit_now"] is False and c["sandbox"]["worlds_admit_now"] == []
        assert c["hint"] == "queue_sandbox" or c["viewers"]["state"] == "full"
        assert c["sandbox"]["eta_s"] is None or c["sandbox"]["eta_s"] >= 0
        s.wait_ready(90)
        s.rt()
        s.park()
        c = _until(st, lambda c: c["sandbox"]["parked"] == 1 and c["sandbox"]["active"] == 0, 15.0)
        assert c["sandbox"]["state"] == "open" and c["sandbox"]["admit_now"] is True
        s.resume(wait=True)
        c = _until(st, lambda c: c["sandbox"]["parked"] == 0 and c["sandbox"]["active"] == 1, 5.0)
        assert c["sandbox"]["state"] == "full"
    finally:
        _wait_gone(st, s)
    c = _cap(st).json()
    assert c["sandbox"]["active"] == 0 and c["sandbox"]["state"] == "open"


def test_internal_session_takes_the_reserved_slot_beyond_n_max(ctx) -> None:
    st, sf = ctx["st"], ctx["sf"]
    pub = A.Sandbox(st.base, principal_hint=HINT["B"], auto_keepalive=False, timeout_s=30.0).create("synthcity", "t6_manual", queue=False)
    internal = None
    try:
        _until(st, lambda c: c["sandbox"]["active"] == 1 and c["sandbox"]["state"] == "full")
        tok, _p = idfx.issue_token(sf)
        internal = A.Sandbox(st.base, principal_hint=HINT["C"], auto_keepalive=False, timeout_s=30.0).create(
            "synthcity", "t6_manual", queue=False, identity_token=tok)
        c = _until(st, lambda c: c["sandbox"]["internal_active"] == 1)
        assert c["sandbox"]["active"] == 1 and c["sandbox"]["max"] == 1, c["sandbox"]     # 分母不变：内部预留不计入 N_max
        # 第三个（公开）仍须排队：总占用至多 N_max + 1
        r = httpx.post(st.base + "/api/sandbox/v1/sessions", json={"world": "synthcity", "template": "t6_manual", "principal_hint": HINT["D"]},
                       timeout=30.0)
        assert r.status_code == 202, r.text[:300]
        q = r.json()["queue"]
        httpx.delete(st.base + f"/api/sandbox/v1/queue/{q['ticket_id']}", headers={"authorization": f"Bearer {q['ticket_token']}"}, timeout=15.0)
    finally:
        if internal is not None:
            internal.close()
        _wait_gone(st, pub)


def test_viewer_seats_from_the_show_status_file_and_staleness(ctx) -> None:
    st, show = ctx["st"], ctx["show"]
    # show-api（WP-03b，D2-INT 交付）每 1 s 写真实的状态文件；本栈以 AWR_SHOW_STATUS_OUT 把真实写入改到另一路径，池读的合成文件
    # 不被覆盖（真实写入与连接数一致性见 test_viewer_power_e2e::test_show_status_file_matches_the_connections）
    real = json.loads(Path(ctx["show_out"]).read_text(encoding="utf-8"))
    assert set(real) >= {"t_unix_ns", "live", "max", "queue", "eta_s", "releases_per_s"}
    _synthetic_status_checks(st, show)


def _synthetic_status_checks(st, show) -> None:
    _write_show(show, live=7, mx=24)
    c = _until(st, lambda c: c["viewers"]["state"] == "open")
    assert c["viewers"] == {"live": 7, "max": 24, "queue": 0, "eta_s": None, "state": "open"}
    assert c["hint"] == "open"
    _write_show(show, live=24, mx=24, queue=3, eta_s=360)
    c = _until(st, lambda c: c["viewers"]["state"] == "full")
    assert c["viewers"]["queue"] == 3 and c["viewers"]["eta_s"] == 360 and c["hint"] == "queue_viewer"
    # 沙盒也满：queue_both
    s = A.Sandbox(st.base, principal_hint=HINT["A"], auto_keepalive=False, timeout_s=30.0).create("synthcity", "t6_manual", queue=False)
    try:
        _write_show(show, live=24, mx=24, queue=3, eta_s=360)
        c = _until(st, lambda c: c["hint"] == "queue_both")
    finally:
        _wait_gone(st, s)
    # 停更 > 5 s：unknown（数值为 null）
    _write_show(show, live=3, mx=24, age_s=6.0)
    c = _until(st, lambda c: c["viewers"]["state"] == "unknown")
    assert c["viewers"] == {"live": None, "max": None, "queue": None, "eta_s": None, "state": "unknown"}
    _write_show(show, live=3, mx=24)
    _until(st, lambda c: c["viewers"]["state"] == "open")
    time.sleep(5.5)                                   # 不再写入
    _until(st, lambda c: c["viewers"]["state"] == "unknown", 3.0)
