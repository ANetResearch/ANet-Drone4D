"""M14-AC-031：REST R43–R46、R73–R77（真实 api + Gateway + agent-runtime 进程逻辑，经 LocalBus 的 `ctl/agent-runtime/*`）。

后端：tests/rt 的 GwStack（FakeSim 充当 sim-core：roster、租约、命令、StateRing 心跳）+ AgentRuntimeApp 线程（剧本 agents 块
来自临时目录）。错误码：115、116、118、121、471、105、305、321 与 agent-runtime 停止时 503 213。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rt"))

httpx = pytest.importorskip("httpx")

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    import fakesim
    import rtc
    from fakes.app_thread import AppThread

    tmp = tmp_path_factory.mktemp("m14rest")
    (tmp / "scen").mkdir()
    (tmp / "scen" / "m14-rest.json").write_text(json.dumps({"agents": {
        "network": "mock",
        "members": [{"vehicle_id": "f001", "capabilities": ["thermal.imaging"], "role": "verifier"},
                    {"vehicle_id": "f002", "capabilities": ["thermal.imaging", "rgb.zoom"], "role": "verifier"}]}}))
    mp = pytest.MonkeyPatch()
    mp.setenv("AWR_SCENARIO", "m14-rest")
    mp.setenv("AWR_SCENARIOS_DIR", str(tmp / "scen"))
    stack = fakesim.GwStack(n=3)
    app = AppThread(stack.settings, tmp / "persist").start()
    op = rtc.token(stack.base, "operator", hint=rtc.hint_of("m14rest"))
    viewer = rtc.token(stack.base, "viewer", hint=rtc.hint_of("m14viewer"))
    yield {"stack": stack, "app": app, "op": op["token"], "viewer": viewer["token"], "tmp": tmp}
    app.stop()
    stack.close()
    mp.undo()


def _h(tok: str, **extra: str) -> dict:
    return {"Authorization": f"Bearer {tok}", **extra}


def _get(env, path: str, tok: str | None = None) -> httpx.Response:
    return httpx.get(env["stack"].base + path, headers=_h(tok or env["viewer"]), timeout=10)


def _post(env, path: str, body: dict, tok: str | None = None, **hdr: str) -> httpx.Response:
    return httpx.post(env["stack"].base + path, json=body, headers=_h(tok or env["op"], **hdr), timeout=10)


SUBMIT = {"capability": "thermal.imaging", "target_enu_m": [10.0, 20.0, None], "args": {"dwell_s": 10, "alt_agl_m": 60}}


def test_r43_r44_agents_and_manifest(env) -> None:
    r = _get(env, "/api/agents")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["coordinator_aid"].startswith("bafyrei") and len(body["items"]) == 2
    it = body["items"][0]
    for k in ("aid", "aid_short", "name", "vehicle_id", "kind", "profile_id", "network", "caps", "health", "load", "trust",
              "current_task", "manifest_sha256"):
        assert k in it, k
    m = _get(env, f"/api/agents/{it['aid']}/manifest")
    assert m.status_code == 200 and m.json()["schema"] == "awr.agent.manifest.v1"
    assert m.json()["manifest_sha256"] == it["manifest_sha256"]
    assert _get(env, "/api/agents/bafyreinosuchagent0000/manifest").json()["code"] == 305
    assert _get(env, "/api/agents?network=anet").json()["items"] == []


def test_r45_submit_and_reads(env) -> None:
    r = _post(env, "/api/agent-tasks", SUBMIT, **{"Idempotency-Key": "m14-idem-1"})
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]
    assert r.json()["state"] == "submitted" and r.json()["merged_into"] is None
    again = _post(env, "/api/agent-tasks", SUBMIT, **{"Idempotency-Key": "m14-idem-1"})
    assert again.status_code == 202 and again.json()["task_id"] == tid
    merged = _post(env, "/api/agent-tasks", {**SUBMIT, "target_enu_m": [15.0, 20.0, None]})
    assert merged.status_code == 202 and merged.json()["merged_into"] == tid
    lst = _get(env, "/api/agent-tasks?limit=10").json()["items"]
    assert any(t["task_id"] == tid for t in lst)
    full = _get(env, f"/api/agent-tasks/{tid}").json()
    assert full["task_id"] == tid and "quotes" in full and "board_phase" in full and full["accept"]["op"] == 1
    board = _get(env, f"/api/agent-tasks/{tid}/board").json()
    assert board["phase"] in ("active", "concluded") and {u["type"] for u in board["units"]} >= {"claim", "intent"}
    ev = _get(env, f"/api/agent-tasks/{tid}/evidence").json()
    assert ev["chains"] and all(c["verified"] for c in ev["chains"])
    assert ev["rows"][0]["type"] == "agent.task.submitted"
    assert _get(env, "/api/agent-tasks/T-9999").status_code == 404


def test_r45_errors(env) -> None:
    assert _post(env, "/api/agent-tasks", {**SUBMIT, "capability": "sonar.scan"}).json()["code"] == 471
    assert _post(env, "/api/agent-tasks", {**SUBMIT, "capability": "lidar.mapping"}).json()["code"] == 471
    bad = _post(env, "/api/agent-tasks", {**SUBMIT, "target_enu_m": [900.0, 0.0, None], "accept": {"op": 99}})
    assert bad.status_code == 422 and bad.json()["code"] == 121
    v = _post(env, "/api/agent-tasks", SUBMIT, tok=env["viewer"])
    assert v.status_code == 403 and v.json()["code"] == 115
    ex = _post(env, "/api/agent-tasks", {**SUBMIT, "evil": 1})
    assert ex.status_code in (400, 422)
    k1 = _post(env, "/api/agent-tasks", {**SUBMIT, "target_enu_m": [500.0, 0.0, None]}, **{"Idempotency-Key": "m14-idem-2"})
    k2 = _post(env, "/api/agent-tasks", {**SUBMIT, "target_enu_m": [700.0, 0.0, None]}, **{"Idempotency-Key": "m14-idem-2"})
    assert k1.status_code == 202 and k2.json()["code"] == 321


def test_r46_cancel_and_r77_assign(env) -> None:
    r = _post(env, "/api/agent-tasks", {**SUBMIT, "target_enu_m": [-800.0, 0.0, None]})
    tid = r.json()["task_id"]
    a = _post(env, f"/api/agent-tasks/{tid}/assign", {"provider_aid": None})
    assert a.status_code == 409 and a.json()["code"] == 105  # 不在 input-required
    c = _post(env, f"/api/agent-tasks/{tid}/cancel", {})
    assert c.status_code == 200 and c.json()["state"] == "canceled"
    c2 = _post(env, f"/api/agent-tasks/{tid}/cancel", {})
    assert c2.status_code == 409 and c2.json()["code"] == 105
    assert _post(env, "/api/agent-tasks/T-7777/cancel", {}).json()["code"] == 305


def test_seat_and_replay_guards(env) -> None:
    stack = env["stack"]
    gw = stack.gw
    orig = gw.is_seat_holder
    gw.is_seat_holder = lambda pid: False
    try:
        r = _post(env, "/api/agent-tasks", SUBMIT)
        assert r.status_code == 409 and r.json()["code"] == 116
    finally:
        gw.is_seat_holder = orig
    gw.mode = "replay"
    try:
        r = _post(env, "/api/agent-tasks", SUBMIT)
        assert r.status_code == 409 and r.json()["code"] == 118
    finally:
        gw.mode = "live"


def test_runtime_down_503_213(env) -> None:
    env["app"].stop()
    t0 = time.monotonic()
    r = _get(env, "/api/agents")
    assert r.status_code == 503 and r.json()["code"] == 213
    assert _post(env, "/api/agent-tasks", SUBMIT).json()["code"] == 213
    assert time.monotonic() - t0 < 15
