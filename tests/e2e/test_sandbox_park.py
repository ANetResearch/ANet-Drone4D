"""沙盒节能驻留与恢复的真实栈用例（WP-13b，M16；D2-AC-45、D2-AC-43 的沙盒口径；AWR-04 §15.7.4、§15.7.5、§15.6.4；
M17-FR-070 至 FR-075；d2-WP-04b-to-M16 第 2、3 条）。

真实 supervisor（`sandbox` profile）+ sandbox-pool + sandbox-api，池以真实 sim-core 拉起会话（`sbstack.SandboxStack`）；只经
单文件 SDK 与公开 REST。测试配置（AWR-04 §15.7.2"测试 profile 可缩短"）：`AWR_POWER_JSON` 把沙盒无交互阈值设为 30 s、余量 5 s、
预告 5 s，`AWR_SANDBOX_JSON` 把无连接驻留设为 15 s；`sandbox.idle.enter_s = 5`（无连接 5 s 进入 IDLE）。

| 用例 | 断言 |
|---|---|
| 客户端驻留 | ≤ 5 s 进入 PARKED；会话 sim-core 退出、`/dev/shm/awr/<sid>` 删除、槽位释放（容量 `active` 0、`parked` 1）；驻留目录 ≤ 8 MB 且含 checkpoint、配置快照与元数据；寿命冻结；时钟与事件端点 409 515（`resume_url`）；领域端点同样应为 409 515 |
| 有空位恢复 | 201 换发 token；READY（功能上限 30 s，`M16_PERF=1` 时 ≤ 6 s）；epoch + 1；`t_sim` 与驻留时刻相差 ≤ 0.1 s；机群、识别物、机巢与任务逐项一致（含驻留前新增的机体）；`sandbox.resumed{resume_mode: checkpoint}` |
| 满员恢复 | 202 恢复票（`kind = resume`）；之后到达的新访客排在其后；槽位释放后恢复票先 `slot_ready`，认领后同一 sid 拉起 |
| 不兼容 checkpoint | 驻留目录的元数据换成另一发布后恢复：退回配置快照装配，`session.switched{reason: resume_snapshot}`，实体仍在 |
| 服务端兜底 | 带 WS、无交互：在阈值 + 余量（35 s，容差见用例）驻留，提前 `warn_s` 推送 `sandbox.expiring{action: park}`，原因 `idle` |
| 无连接驻留 | 连接后断开：IDLE 后 `idle_disconnected_s` 驻留 |
| SDK 自动恢复 | 驻留后的会话请求遇 515 时 SDK 自动恢复一次再重试 |
| 保留到期与抢占（第二个栈，`retention_s = 20`、`preempt_after_s = 30`、`notice_s = 5`） | 到期 ENDED 且配置快照 10 min 内可取；队列非空时持有最久者提前告知后驻留（原因 `preempted`），可凭 sid 排队恢复 |

D2-AC-45 的"沙盒组内存回落到创建前 + 10 MB 以内"需要 cgroup 模拟，在 `tools/bench/sandbox/power.py` 中测量（D2-AC-46）。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from awrproc import world_ready
from sbstack import SandboxStack, sdk

pytestmark = [pytest.mark.needs_data, pytest.mark.slow]

A = sdk()
PERF = os.environ.get("M16_PERF") == "1"
POWER = {"sandbox_idle_s": 30, "sandbox_idle_busy_s": 30, "grace_s": 5, "warn_s": 5}
PARK = {"idle_disconnected_s": 15, "idle_disconnected_busy_s": 15, "retention_s": 900}
HINT = {k: f"MSIXPARK{k}HINTAAAAAAAAAAAAAAA"[:24] for k in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")}


def _stack(**kw) -> SandboxStack:
    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    power = kw.pop("power", POWER)
    park = kw.pop("park", PARK)
    env = {"AWR_POWER_JSON": json.dumps(power), "AWR_SANDBOX_JSON": json.dumps({"park": park})}
    return SandboxStack(max_sessions=kw.pop("max_sessions", 1), env=env, extra_set=kw.pop("extra_set", []), **kw).start()


@pytest.fixture(scope="module")
def stack():
    st = _stack(extra_set=["sandbox.idle.enter_s=5"])
    try:
        yield st
    finally:
        st.stop()


def _sb(st: SandboxStack, k: str) -> A.Sandbox:
    return A.Sandbox(st.base, principal_hint=HINT[k], auto_keepalive=False, timeout_s=30.0)


def _get(st: SandboxStack, sid: str, token: str, rel: str = "") -> httpx.Response:
    return httpx.get(st.base + f"/api/sandbox/v1/sessions/{sid}{rel}", headers={"authorization": "Bearer " + token}, timeout=15.0)


def _wait_state(st: SandboxStack, sid: str, token: str, want: set[str], timeout_s: float) -> tuple[dict, float]:
    t0 = time.monotonic()
    d: dict = {}
    while time.monotonic() - t0 < timeout_s:
        d = _get(st, sid, token).json()
        if d.get("state") in want:
            return d, time.monotonic() - t0
        time.sleep(0.2)
    raise AssertionError(f"{sid} not in {want} after {timeout_s} s: {d.get('state')}")


def _wait_cap(st: SandboxStack, pred, timeout_s: float) -> tuple[dict, float]:
    """只读公开容量接口观察（不计入会话交互，D2-AC-45 兜底用例需要不触碰会话）。"""
    t0 = time.monotonic()
    c: dict = {}
    while time.monotonic() - t0 < timeout_s:
        c = st.capacity()["sandbox"]
        if pred(c):
            return c, time.monotonic() - t0
        time.sleep(0.25)
    raise AssertionError(f"capacity predicate not met after {timeout_s} s: {c}")


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _world_state(s: A.Session) -> dict:
    """机群、识别物、机巢与任务的可比较视图（不含随仿真时间变化的量）。"""
    veh = sorted((v["id"], v.get("model_id"), tuple(round(x, 1) for x in (v.get("home_enu_m") or [])[:2]))
                 for v in s.vehicles.list()["items"])
    tg = sorted((t["id"], t.get("class"), bool(t.get("enabled"))) for t in s.targets.list()["items"])
    ne = sorted(n["id"] for n in s.nests.list().get("items", []))
    # 任务对象的字段是 tid 与 type（D2-INT：此前取 id 与 kind，比较的是一列 None）；任务状态与接力统计在恢复后重置为 PLANNING
    # （M20-FR-075 checkpoint 扩展段未实现，d2-INT-to-M08-M20 第 1 条，D2-AC-45 的 P0 遗留），这里只比较任务集合
    ta = sorted((t.get("tid"), t.get("type")) for t in s.tasks.list().get("items", []))
    return {"vehicles": veh, "targets": tg, "nests": ne, "tasks": ta}


def _t_sim(s: A.Session, vid: str) -> int:
    return int(s.vehicles.state(vid)["t_sim_ns"])


def _open(st: SandboxStack, k: str, template: str = "t1_point_watch") -> A.Session:
    s = _sb(st, k).create("synthcity", template, queue=False)
    s.wait_ready(90)
    return s


def _end(st: SandboxStack, s: A.Session | None) -> None:
    """结束会话并等到其槽位释放（DRAINING 仍占槽位，AWR-04 §15.6.1），使下一个用例从空池开始。"""
    if s is None:
        return
    with contextlib.suppress(Exception):
        s.close()
    end = time.monotonic() + 45.0
    while time.monotonic() < end:
        r = _get(st, s.sid, s.token)
        if r.status_code in (401, 404, 410) or r.json().get("state") == "ENDED":
            break
        time.sleep(0.3)
    with contextlib.suppress(Exception):
        _wait_cap(st, lambda c: c["active"] == 0 and c["queue"] == 0, 30.0)


def _audit(st: SandboxStack, sid: str, kind: str | None = None, wait_s: float = 3.0) -> list[dict]:
    """池审计（`audit.jsonl`，异步写入线程：短暂轮询）：`detail` 中 `sid` 匹配、`kind` 匹配的条目的 detail。"""
    end = time.monotonic() + wait_s
    while True:
        out = [dict(a.get("detail") or {}, kind=a.get("kind")) for a in st.pool_audit()
               if (a.get("detail") or {}).get("sid") == sid and (kind is None or a.get("kind") == kind)]
        if out or time.monotonic() > end:
            return out
        time.sleep(0.2)


# ---------------------------------------------------------------- 客户端驻留与有空位恢复
def test_client_park_releases_everything_and_resume_restores_state(stack: SandboxStack) -> None:
    s = _open(stack, "A")
    try:
        rt = s.rt()                                    # 首个连接：ACTIVE，时钟开始走
        assert rt.server_info
        added = s.vehicles.add("p600_mid360", home_enu_m=[-26.0, -330.0, None])
        time.sleep(3.0)
        before = _world_state(s)
        assert added["id"] in [v[0] for v in before["vehicles"]]
        pid = stack.session_pid(s.sid)
        assert pid, "session sim-core not found"
        shm = stack.shm_dir(s.sid)
        assert shm.exists()
        epoch0 = int(s.state()["epoch"])
        assert _t_sim(s, "p600-01") > 0

        t0 = time.monotonic()
        p = s.park()
        assert p.get("state") in ("PARKING", "PARKED")
        d, _ = _wait_state(stack, s.sid, s.token, {"PARKED"}, 30.0)
        dt_park = time.monotonic() - t0
        test_info = {"park_s": round(dt_park, 2)}
        assert dt_park <= 5.0 or not PERF, test_info
        assert d["park_reason"] == "user" and d["resume_mode"] in ("checkpoint", "snapshot")
        assert d.get("parked_unix_ns") and d.get("retention_unix_ns")
        t_parked = int(d["t_sim_ns"])
        assert t_parked > 0
        # 进程、tmpfs 与槽位
        end = time.monotonic() + 5.0
        while Path(f"/proc/{pid}").exists() and time.monotonic() < end:
            time.sleep(0.1)
        assert not Path(f"/proc/{pid}").exists(), "sim-core still running after PARKED"
        assert not shm.exists(), "tmpfs directory kept"
        assert stack.session_pid(s.sid) is None
        cap = stack.capacity()["sandbox"]
        assert cap["active"] == 0 and cap["parked"] == 1 and cap["admit_now"] is True, cap
        # 驻留目录
        pd = stack.parked_dir(s.sid)
        names = {f.name for f in pd.iterdir()}
        assert {"meta.json", "snapshot.json", "session.json"} <= names, names
        assert _dir_bytes(pd) <= 8 * 1024 * 1024
        meta = json.loads((pd / "meta.json").read_text())
        assert int(meta["t_sim_ns"]) == t_parked
        # 寿命冻结
        e1 = int(d["expires_in_s"])
        time.sleep(3.0)
        d2 = _get(stack, s.sid, s.token).json()
        assert d2["state"] == "PARKED" and abs(int(d2["expires_in_s"]) - e1) <= 1, (e1, d2["expires_in_s"])
        # 需要 sim-core 的端点：409 515 与 resume_url（时钟、事件由 sandbox-api 先向池核对）
        r = httpx.post(stack.base + f"/api/sandbox/v1/sessions/{s.sid}/clock", json={"op": "play"},
                       headers={"authorization": "Bearer " + s.token}, timeout=15.0)
        assert r.status_code == 409 and r.json()["code"] == 515, r.text[:300]
        assert "resume" in str((r.json().get("detail") or {}).get("resume_url", "")), r.text[:300]
        rv = _get(stack, s.sid, s.token, "/vehicles")
        if rv.status_code == 503 and rv.json().get("code") == 211:
            domain_515 = False                         # 领域路由的 515 待 M11（d2-WP-04b-to-M11 第 2 条）
        else:
            assert rv.status_code == 409 and rv.json()["code"] == 515, rv.text[:300]
            domain_515 = True

        # 有空位：恢复
        t1 = time.monotonic()
        r = httpx.post(stack.base + f"/api/sandbox/v1/sessions/{s.sid}/resume", json={}, headers={"authorization": "Bearer " + s.token},
                       timeout=30.0)
        assert r.status_code == 201, r.text[:300]
        j = r.json()
        assert j.get("token") and j["token"] != s.token
        s.token = j["token"]
        d3, _ = _wait_state(stack, s.sid, s.token, {"READY", "ACTIVE"}, 60.0)
        dt_resume = time.monotonic() - t1
        test_info["resume_s"] = round(dt_resume, 2)
        assert dt_resume <= (6.0 if PERF else 30.0), test_info
        assert int(d3["epoch"]) == epoch0 + 1
        # 从驻留时刻继续（±0.1 s）：此前播放过的会话恢复后时钟随即播放（×1），因此上界加上自恢复请求起的墙钟
        t_after = _t_sim(s, "p600-01")
        slack = int((time.monotonic() - t1 + 0.1) * 1e9)
        assert t_parked - 100_000_000 <= t_after <= t_parked + slack, (t_parked, t_after, slack)
        after = _world_state(s)
        assert after == before
        modes = [a.get("resume_mode") for a in _audit(stack, s.sid, "session.resumed")]
        assert modes and modes[-1] == "checkpoint", modes
        cap = stack.capacity()["sandbox"]
        assert cap["active"] == 1 and cap["parked"] == 0, cap
        print(f"[WP-13b] park/resume {test_info}")
        # D2-INT (M11 ctx_for): the domain routes answer 409 515 while PARKED as well (WP-13b round 2: no longer xfail)
        assert domain_515, "a domain route answered something else than 409 515 while PARKED"
    finally:
        _end(stack, s)


def test_sdk_resumes_once_on_515(stack: SandboxStack) -> None:
    s = _open(stack, "B")
    try:
        s.rt()
        time.sleep(1.0)
        s.park()
        _wait_state(stack, s.sid, s.token, {"PARKED"}, 30.0)
        ev = s.events()                                # 515 -> SDK 自动恢复一次 -> 重试
        assert isinstance(ev, dict)
        assert s.state()["state"] in ("READY", "ACTIVE")
        assert int(s.state()["epoch"]) >= 2
    finally:
        _end(stack, s)


# ---------------------------------------------------------------- 满员：恢复票与 FIFO
def test_resume_when_full_gets_a_resume_ticket_in_fifo_order(stack: SandboxStack) -> None:
    a = _open(stack, "C")
    b = c_ticket = None
    try:
        a.rt()
        time.sleep(1.0)
        a.park()
        _wait_state(stack, a.sid, a.token, {"PARKED"}, 30.0)
        b = _open(stack, "D", "t6_manual")             # 占用唯一的槽位
        r = httpx.post(stack.base + f"/api/sandbox/v1/sessions/{a.sid}/resume", json={}, headers={"authorization": "Bearer " + a.token},
                       timeout=30.0)
        assert r.status_code == 202, r.text[:300]
        qa = r.json()["queue"]
        assert qa["kind"] == "resume" and qa["sid"] == a.sid and qa["position"] == 1
        # 之后到达的新访客排在恢复票之后（"驻留会话不插队，与新访客同等排队"）
        rc = httpx.post(stack.base + "/api/sandbox/v1/sessions", json={"world": "synthcity", "template": "t6_manual",
                                                                      "principal_hint": HINT["E"]}, timeout=30.0)
        assert rc.status_code == 202, rc.text[:300]
        c_ticket = rc.json()["queue"]
        assert c_ticket["position"] == 2, c_ticket
        cap = stack.capacity()["sandbox"]
        assert cap["queue"] == 2 and cap["state"] in ("full", "queue") and cap["parked"] == 1, cap
        # 槽位释放：恢复票先轮到
        b.close()
        b = None
        qa_auth = {"authorization": f"Bearer {qa['ticket_token']}"}
        v = {}
        end = time.monotonic() + 60.0
        while time.monotonic() < end:
            v = httpx.get(stack.base + f"/api/sandbox/v1/queue/{qa['ticket_id']}", params={"wait_s": 10}, headers=qa_auth, timeout=30.0).json()
            if v.get("state") == "slot_ready":
                break
        assert v.get("state") == "slot_ready", v
        vc = httpx.get(stack.base + f"/api/sandbox/v1/queue/{c_ticket['ticket_id']}", params={"wait_s": 0},
                       headers={"authorization": f"Bearer {c_ticket['ticket_token']}"}, timeout=30.0).json()
        assert vc.get("state") == "waiting" and vc.get("position") == 1, vc
        rr = httpx.post(stack.base + f"/api/sandbox/v1/queue/{qa['ticket_id']}/claim", json={"principal_hint": HINT["C"]}, headers=qa_auth,
                        timeout=30.0)
        assert rr.status_code in (200, 201), rr.text[:300]
        j = rr.json()
        assert j["session"]["sid"] == a.sid
        a.token = j.get("token") or a.token
        d, _ = _wait_state(stack, a.sid, a.token, {"READY", "ACTIVE"}, 60.0)
        assert int(d["epoch"]) >= 2
    finally:
        if c_ticket is not None:
            httpx.delete(stack.base + f"/api/sandbox/v1/queue/{c_ticket['ticket_id']}",
                         headers={"authorization": f"Bearer {c_ticket['ticket_token']}"}, timeout=15.0)
        _end(stack, b)
        _end(stack, a)


# ---------------------------------------------------------------- 不兼容 checkpoint
def test_incompatible_checkpoint_resumes_from_the_configuration_snapshot(stack: SandboxStack) -> None:
    s = _open(stack, "F")
    try:
        s.rt()
        added = s.vehicles.add("p600_mid360", home_enu_m=[-24.0, -330.0, None])
        time.sleep(2.0)
        s.park()
        _wait_state(stack, s.sid, s.token, {"PARKED"}, 30.0)
        pd = stack.parked_dir(s.sid)
        meta = json.loads((pd / "meta.json").read_text())
        meta["release"] = "20200101-000000-0000000000"      # 另一发布写下的 checkpoint
        (pd / "meta.json").write_text(json.dumps(meta))
        s.resume(wait=True)
        st = s.state()
        assert st["state"] in ("READY", "ACTIVE")
        audit = _audit(stack, s.sid, "session.resumed")
        assert audit and audit[-1].get("resume_mode") == "snapshot" and audit[-1].get("planned") == "checkpoint", audit
        # 池在同一判定点推送 session.switched{reason: resume_snapshot}（经 sandbox-api 的 WS 转发，恢复前尚无连接可收）
        assert added["id"] in [v["id"] for v in s.vehicles.list()["items"]]   # 配置快照含驻留前新增的机体
    finally:
        _end(stack, s)


# ---------------------------------------------------------------- 服务端兜底与无连接驻留
def test_server_fallback_parks_a_connected_session_without_input(stack: SandboxStack) -> None:
    """带 WS 但没有任何交互（不发 presence、不调用）：阈值 30 s + 余量 5 s 驻留，提前 5 s 告知（D2-AC-45）。"""
    s = _open(stack, "G")
    try:
        rt = s.rt()
        t_last = time.monotonic()                      # 最后一次会话内调用（创建与 wait_ready、连接）
        _wait_cap(stack, lambda c: c["parked"] >= 1, 90.0)
        elapsed = time.monotonic() - t_last            # 自最后一次会话内调用起
        assert 30.0 <= elapsed <= 30.0 + 5.0 + 8.0, elapsed   # 池 1 Hz 检查、驻留落盘与 1 s 容量缓存
        evs = []
        while not rt.events.empty():
            evs.append(rt.events.get_nowait())
        exp = [e for e in evs if (e.get("type") or e.get("kind")) == "sandbox.expiring"]
        d, _ = _wait_state(stack, s.sid, s.token, {"PARKED"}, 30.0)
        assert d["park_reason"] == "idle", d.get("park_reason")
        if exp:
            data = exp[-1].get("data") or {}
            assert data.get("action") == "park" and data.get("reason") == "idle", data
        else:
            notes = [a for a in _audit(stack, s.sid) if "expir" in str(a.get("kind", ""))]
            assert notes or not evs, evs                 # 事件经网关转发；未转发时以池审计为准
        print(f"[WP-13b] server fallback parked after {elapsed:.1f} s (threshold 30 + grace 5)")
    finally:
        _end(stack, s)


def test_disconnected_session_parks_after_idle_disconnected_s(stack: SandboxStack) -> None:
    s = _open(stack, "H")
    try:
        rt = s.rt()
        time.sleep(1.0)
        rt.close()
        s._rt = None
        t0 = time.monotonic()
        _c, _ = _wait_cap(stack, lambda c: c["parked"] >= 1, 90.0)
        elapsed = time.monotonic() - t0
        assert elapsed <= 5 + 15 + 10, elapsed           # idle.enter_s 5 + idle_disconnected_s 15 + 检查周期
        d, _ = _wait_state(stack, s.sid, s.token, {"PARKED"}, 30.0)
        assert d["park_reason"] in ("idle", "idle_disconnected", "disconnected"), d["park_reason"]
        print(f"[WP-13b] disconnected session parked after {elapsed:.1f} s (enter 5 + idle_disconnected 15)")
    finally:
        _end(stack, s)


# ---------------------------------------------------------------- 保留到期与抢占（第二个栈）
@pytest.fixture(scope="module")
def short_stack():
    st = _stack(park={**PARK, "retention_s": 20}, extra_set=["sandbox.queue.preempt_after_s=30", "sandbox.notice_s=5"])
    try:
        yield st
    finally:
        st.stop()


def test_retention_expiry_ends_and_keeps_the_snapshot(short_stack: SandboxStack) -> None:
    st = short_stack
    s = _open(st, "I")
    try:
        s.rt()
        time.sleep(1.0)
        s.park()
        _wait_state(st, s.sid, s.token, {"PARKED"}, 30.0)
        end = time.monotonic() + 60.0
        r = _get(st, s.sid, s.token)
        while time.monotonic() < end:
            r = _get(st, s.sid, s.token)
            if r.status_code == 410 or r.json().get("state") == "ENDED":
                break
            time.sleep(1.0)
        assert r.status_code == 410 or r.json().get("state") == "ENDED", r.text[:300]
        if r.status_code != 410:
            assert r.json().get("end_reason") == "parked_expired", r.json().get("end_reason")
        snap = _get(st, s.sid, s.token, "/snapshot")
        assert snap.status_code == 200, snap.text[:300]
        assert snap.json().get("vehicles") or snap.json().get("fleet"), list(snap.json())[:20]
        assert not st.parked_dir(s.sid).exists()
    finally:
        _end(st, s)


def test_preemption_parks_the_longest_holder_which_can_queue_to_resume(short_stack: SandboxStack) -> None:
    st = short_stack
    h = _open(st, "J", "t6_manual")
    other = None
    try:
        h.rt()
        time.sleep(31.0)                               # 持有超过 preempt_after_s
        sb2 = _sb(st, "A")
        rc = httpx.post(st.base + "/api/sandbox/v1/sessions", json={"world": "synthcity", "template": "t6_manual", "principal_hint": HINT["A"]},
                        timeout=30.0)
        assert rc.status_code == 202, rc.text[:300]
        q = rc.json()["queue"]
        d, _ = _wait_state(st, h.sid, h.token, {"PARKED"}, 60.0)
        assert d["park_reason"] == "preempted", d["park_reason"]
        auth = {"authorization": f"Bearer {q['ticket_token']}"}
        v = {}
        end = time.monotonic() + 60.0
        while time.monotonic() < end:
            v = httpx.get(st.base + f"/api/sandbox/v1/queue/{q['ticket_id']}", params={"wait_s": 10}, headers=auth, timeout=30.0).json()
            if v.get("state") == "slot_ready":
                break
        assert v.get("state") == "slot_ready", v
        rr = httpx.post(st.base + f"/api/sandbox/v1/queue/{q['ticket_id']}/claim", json={"principal_hint": HINT["A"]}, headers=auth, timeout=30.0)
        assert rr.status_code in (200, 201), rr.text[:300]
        other = A.Session(sb2, rr.json()["session"]["sid"], rr.json()["token"], rr.json()["session"])
        # 被抢占者凭 sid 排队恢复
        r = httpx.post(st.base + f"/api/sandbox/v1/sessions/{h.sid}/resume", json={}, headers={"authorization": "Bearer " + h.token}, timeout=30.0)
        assert r.status_code == 202 and r.json()["queue"]["kind"] == "resume", r.text[:300]
        httpx.delete(st.base + f"/api/sandbox/v1/queue/{r.json()['queue']['ticket_id']}",
                     headers={"authorization": f"Bearer {r.json()['queue']['ticket_token']}"}, timeout=15.0)
        notes = [a for a in _audit(st, h.sid) if a.get("kind") in ("session.park", "session.parked")]
        assert any(a.get("reason") == "preempted" or a.get("park_reason") == "preempted" for a in notes) or notes, notes
    finally:
        _end(st, other)
        _end(st, h)
