"""lab 后端：世界与事件只读接口、Run 状态机、取消、重试与原因码（AWR-17 §18.5.2、§18.5.3、§18.6；M22 §7.6.4；M23 §7.3.1；
M27 §6.3、M27-FR-024、FR-035、FR-039；D3-AC-51 的 API 部分：`/lab/worlds`、`/lab/worlds/:wid`、`/lab/events`、`/lab/runs`
所需的状态机、取消、重试与原因码）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from awr.api.bench_main import create_app
from awr.bench.api.worlds import world_summary_doc
from awr.bench.runs_store import TERMINAL, TRANSITIONS, RunStateError, RunStore
from awr.bench.settings import BenchSettings
from awr.world.geometry.status import metric_ok

P = "/api/bench/v1"
ORIGIN = "https://lab.example"
ROOT = Path(__file__).resolve().parents[3]
CROP = ROOT / "worlds" / "oryahovo-crop1"
S = 1_000_000_000
T0 = 1_792_000_000 * S


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> int:
        return self.t

    def tick(self, s: float = 1.0) -> None:
        self.t += int(s * S)


def _world(root: Path, wid: str, *, scale: str | None = None) -> Path:
    """crop 世界包的三个 JSON 证据文件（world.json、coordinate.json、qa/report.json）副本；`scale` 改写 world.json 的尺度等级。"""
    d = root / wid
    (d / "qa").mkdir(parents=True)
    for rel in ("world.json", "coordinate.json", "qa/report.json"):
        shutil.copyfile(CROP / rel, d / rel)
    w = json.loads((d / "world.json").read_text(encoding="utf-8"))
    w["id"] = wid
    if scale:
        w["scaleStatus"] = scale
    (d / "world.json").write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
    return d


def _gate(n: int, status: str = "pass", errors: list | None = None) -> dict:
    return {"gate": n, "status": status, "errors": errors or [], "warnings": [], "duration_ms": 3}


def _event(root: Path, eid: str, v: int, *, world: str, status: str = "compiled", reason: int | None = None, published: bool = False,
           name: str | None = None) -> Path:
    """M23 编译产物目录（compile_report.json 为 `CompileResult.as_dict()` 形状）。"""
    d = root / eid / f"v{v}"
    d.mkdir(parents=True)
    plan = {"schema": "drone4d.plan.v1", "event_id": eid, "version": v, "event_type": "rendezvous_handoff",
            "world": {"id": world, "content_version": "cv-1"}, "duration_ms": 360_000, "tick_hz": 10,
            "actors": [{"id": "person_A"}, {"id": "vehicle_C"}], "background": [{"class": "person", "count": 40, "zone": "z1"}]}
    (d / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (d / "event.json").write_text(json.dumps({"event_id": eid, "world": plan["world"], "duration_s": 360.0, **({"name": name} if name else {})}),
                                  encoding="utf-8")
    tape = None
    gates = [_gate(1), _gate(2), _gate(3, "skipped")]
    if status == "compiled":
        tape = {"content_sha256": "c" * 64, "body_sha256": "b" * 64, "n_actors": 42, "n_ticks": 3600, "bytes": 3_100_000,
                "bytes_per_actor_s": 20.5}
        (d / "tape.bin").write_bytes(b"D4DT" + bytes(64))
        (d / "tape.json").write_text(json.dumps({"schema": "drone4d.tape_manifest.v1", **{k: tape[k] for k in ("content_sha256", "body_sha256",
                                                                                                                "bytes", "n_actors", "n_ticks")}}))
    else:
        gates = [_gate(1), _gate(2, "fail", [{"code": "E-INFEASIBLE", "reason": reason, "path": "/actors/0/route/1",
                                               "message_zh": "路段不可达", "message_en": "Segment infeasible"}])]
    rep = {"event_id": eid, "version": v, "status": status, "reason": None if reason is None else {"code": reason, "name": "x"},
           "gates": gates, "segments": [{"actor": "person_A", "len_m": 120.0, "avail_s": 90.0, "need_mps": 1.33}], "retime": {"mode": "minimal"},
           "snaps": [], "boards": [], "tape": tape, "out": None, "world_metric": None, "provenance": {"generator": "awr.sim.events.gen"}}
    (d / "compile_report.json").write_text(json.dumps(rep), encoding="utf-8")
    if published:
        (d / "published.json").write_text(json.dumps({"by": "alice", "published_unix_ns": T0, "content_sha256": "c" * 64}))
    return d


@pytest.fixture()
def env(tmp_path: Path):
    s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=(ORIGIN,), secure_cookie=False)
    _world(s.worlds_dir, "oryahovo-crop1")
    _world(s.worlds_dir, "crop-relative", scale="relative")
    (s.worlds_dir / "_shared").mkdir()                                        # 非世界目录不进入列表
    ev = s.data_root / "events"
    _event(ev, "abc-crossing", 1, world="oryahovo-crop1", published=True, name="交叉口接力")
    _event(ev, "abc-crossing", 2, world="oryahovo-crop1")
    _event(ev, "def-gate", 1, world="oryahovo-crop1", status="infeasible", reason=612)
    _event(ev, "ghi-other", 1, world="crop-relative")
    legacy = s.dataset_dir / "abc-crossing" / "runs" / "cap-0001"
    legacy.mkdir(parents=True)
    (legacy / "run_manifest.json").write_text(json.dumps({"state": "SUCCEEDED", "kind": "capture", "world_id": "oryahovo-crop1"}))
    app = create_app(s)
    clock = Clock()
    app.state.runs = RunStore(s.db_path, clock=clock)
    c = TestClient(app)
    tok = app.state.auth.issue_token("alice", "researcher", ("d3",))[1]
    tok_inv = app.state.auth.issue_token("bob", "invited")[1]
    return c, app, s, clock, {"Authorization": f"Bearer {tok}"}, {"Authorization": f"Bearer {tok_inv}"}


def _enqueue(st: RunStore, rid: str, *, event: str = "abc-crossing", tenant: str = "d3", ranked: bool = False, cls: str = "research", **kw):
    spec = {"schema": "drone4d.bench.run_spec.v1", "policy": {"kind": "reference", "name": "track_share"}, "physics": "realistic",
            "obs_level": "L1", "fleet": [{"model": "x500", "count": 3}], "duration_s": 360.0}
    kw.setdefault("seed", None if ranked else 7)
    return st.enqueue(rid, experiment_id="exp-aaaaaaaaaa", event_id=event, world_id="oryahovo-crop1", spec=spec, tenant=tenant,
                      submitted_by="alice", ranked=ranked, priority_class=cls, seed_commit="e" * 64, **kw)


def _walk(st: RunStore, rid: str, *states: str, **kw) -> dict:
    out = None
    for s in states:
        out = st.transition(rid, s, **kw)
    return out


# ------------------------------------------------------------------------------------------------------------- 世界
def test_worlds_list_and_summary(env) -> None:
    c, _app, s, _clock, h, h_inv = env
    r = c.get(f"{P}/worlds", headers=h)
    assert r.status_code == 200
    items = {i["world_id"]: i for i in r.json()["items"]}
    assert sorted(items) == ["crop-relative", "oryahovo-crop1"]
    w = items["oryahovo-crop1"]
    ms = metric_ok(s.worlds_dir / "oryahovo-crop1")
    assert w["metric_ok"] is ms.ok and w["ranked_ok"] is ms.ranked_ok and w["scale_status"] == "landmark"
    assert set(w) >= {"world_id", "name", "name_zh", "role", "content_version", "scale_status", "metric_ok", "bounds_m", "layers_count",
                      "license_id"}                                                                     # AWR-17 §18.5.2 列表项
    assert w["license_id"] == "CC-BY-4.0" and w["layers_count"] >= 1 and w["bounds_m"]["xmin"] < w["bounds_m"]["xmax"]
    assert w["events_count"] == 2 and items["crop-relative"]["events_count"] == 1
    rel = items["crop-relative"]
    assert rel["metric_ok"] is False and rel["scale_status"] == "relative" and rel["metric_reason"]
    d = c.get(f"{P}/worlds/oryahovo-crop1", headers=h).json()
    assert d["world_id"] == "oryahovo-crop1" and d["summary"] == world_summary_doc(s.worlds_dir / "oryahovo-crop1")
    sm = d["summary"]
    assert set(sm) >= {"id", "name", "nameZh", "role", "contentVersion", "scaleStatus", "metric", "registration", "trueNorth", "anchor",
                       "bounds", "sensorHints", "quality", "layers", "anchorsCount", "license", "qa", "build"}  # M22 §7.6.4
    assert sm["metric"]["ok"] is ms.ok and sm["registration"]["method"] == "inherit-frame" and sm["license"]["dataset"] == "CC-BY-4.0"
    assert set(sm["anchorsCount"]) == {"auto", "review", "confirmed"} and all(set(L) == {"id", "type", "format", "bytes"} for L in sm["layers"])
    assert [e["event_id"] for e in d["events"]] == ["abc-crossing", "def-gate"]
    for bad in ("nope", "_shared", "..", "a%2F..%2Fkeys"):
        r = c.get(f"{P}/worlds/{bad}", headers=h)
        assert r.status_code == 404 and r.json()["code"] == 305, bad
    assert c.get(f"{P}/worlds", headers=h_inv).json()["code"] == 115                  # 受邀成员无世界接口（最低角色 researcher）
    assert c.get(f"{P}/worlds").json()["code"] == 301


def test_world_summary_cache_follows_file_changes(env) -> None:
    c, _app, s, _clock, h, _ = env
    assert c.get(f"{P}/worlds/oryahovo-crop1", headers=h).json()["summary"]["nameZh"] != "改名"
    p = s.worlds_dir / "oryahovo-crop1" / "world.json"
    w = json.loads(p.read_text(encoding="utf-8"))
    w["nameZh"] = "改名"
    p.write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
    import os
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5 * S))
    assert c.get(f"{P}/worlds/oryahovo-crop1", headers=h).json()["summary"]["nameZh"] == "改名"


# ------------------------------------------------------------------------------------------------------------- 事件
def test_events_list_and_detail(env) -> None:
    c, _app, _s, _clock, h, h_inv = env
    r = c.get(f"{P}/events", headers=h).json()
    items = {i["event_id"]: i for i in r["items"]}
    assert sorted(items) == ["abc-crossing", "def-gate", "ghi-other"] and r["total"] == 3
    a = items["abc-crossing"]
    assert a["version"] == 2 and a["versions"] == 2 and a["status"] == "compiled" and a["world"]["id"] == "oryahovo-crop1"
    assert a["duration_s"] == 360.0 and a["n_scripted"] == 2 and a["n_background"] == 40 and a["tape_content_sha256"] == "c" * 64
    assert a["reason"] is None and a["strict"] is True
    g = items["def-gate"]
    assert g["status"] == "infeasible" and g["reason"]["code"] == 612 and g["reason"]["name"] == "EVENT_INFEASIBLE"
    assert g["reason"]["message"] and g["reason"]["detail"]["errors"][0]["code"] == "E-INFEASIBLE"   # 编译失败以载荷中的原因码返回
    assert [i["event_id"] for i in c.get(f"{P}/events?world=crop-relative", headers=h).json()["items"]] == ["ghi-other"]
    assert [i["event_id"] for i in c.get(f"{P}/events?status=infeasible,failed", headers=h).json()["items"]] == ["def-gate"]
    assert [i["event_id"] for i in c.get(f"{P}/events?q=GATE", headers=h).json()["items"]] == ["def-gate"]
    r = c.get(f"{P}/events?status=bogus", headers=h)
    assert r.status_code == 400 and r.json()["code"] == 300
    d = c.get(f"{P}/events/abc-crossing", headers=h).json()
    assert d["version"] == 2 and d["latest_version"] == 2 and d["versions"] == [{"version": 1, "status": "published"},
                                                                                  {"version": 2, "status": "compiled"}]
    assert d["tape"]["n_actors"] == 42 and [g["gate"] for g in d["gates"]] == [1, 2, 3] and d["segments"][0]["len_m"] == 120.0
    assert {"name": "tape.bin", "bytes": 68} in d["artifacts"]
    v1 = c.get(f"{P}/events/abc-crossing?version=1", headers=h).json()
    assert v1["status"] == "published" and v1["published"]["by"] == "alice" and v1["name"] == "交叉口接力"
    for url in (f"{P}/events/abc-crossing?version=9", f"{P}/events/nope", f"{P}/events/..%2Fkeys"):
        r = c.get(url, headers=h)
        assert r.status_code == 404 and r.json()["code"] == 305, url
    assert c.get(f"{P}/events", headers=h_inv).json()["code"] == 115


# ------------------------------------------------------------------------------------------------------------- 状态机（库）
def test_state_machine_transitions_and_audit(tmp_path: Path) -> None:
    clock = Clock()
    st = RunStore(tmp_path / "bench.sqlite", clock=clock)
    _enqueue(st, "run-aaaaaaaaaa")
    assert st.get("run-aaaaaaaaaa")["state"] == "QUEUED"
    with pytest.raises(RunStateError) as e:
        st.transition("run-aaaaaaaaaa", "RUNNING")                                    # 不可跳过 PREPARING
    assert e.value.code == 680 and e.value.detail["why"] == "transition"
    clock.tick()
    r = st.transition("run-aaaaaaaaaa", "PREPARING", gpu=2, actor="scheduler")
    assert r["started_unix_ns"] == T0 + S and r["state"] == "PREPARING"
    st.transition("run-aaaaaaaaaa", "PREPARING", actor="controller")                   # 就绪屏障超时：restart + 1
    st.transition("run-aaaaaaaaaa", "RUNNING")
    st.transition("run-aaaaaaaaaa", "PREPARING")                                       # 系统故障重启：restart + 1 = 2
    assert st.get("run-aaaaaaaaaa")["restart_count"] == 2
    st.transition("run-aaaaaaaaaa", "RUNNING")
    with pytest.raises(RunStateError) as e:
        st.transition("run-aaaaaaaaaa", "PREPARING")                                  # 上限 2 次（run.max_restarts）
    assert e.value.detail["why"] == "restart_limit"
    clock.tick(30)
    r = _walk(st, "run-aaaaaaaaaa", "FINALIZING", "EVALUATING", "COMPLETED", run_hash="f" * 64, ranked_valid=True)
    assert r["state"] == "COMPLETED" and r["terminal"] and r["ended_unix_ns"] == T0 + 31 * S and r["run_hash"] == "f" * 64
    assert r["actions"] == {"cancel": False, "retry": False} and r["reason"] is None
    for to in ("QUEUED", "PREPARING", "FAILED", "CANCELLED"):
        with pytest.raises(RunStateError):
            st.transition("run-aaaaaaaaaa", to, reason_code=688)
    tr = st.transitions("run-aaaaaaaaaa")
    assert [t["to"] for t in tr] == ["QUEUED", "PREPARING", "PREPARING", "RUNNING", "PREPARING", "RUNNING", "FINALIZING", "EVALUATING", "COMPLETED"]
    assert tr[0]["actor"] == "alice" and tr[2]["actor"] == "controller"
    assert all(TRANSITIONS[s] == frozenset() for s in TERMINAL)
    _enqueue(st, "run-bbbbbbbbbb")
    with pytest.raises(ValueError):
        st.transition("run-bbbbbbbbbb", "FAILED")                                      # FAILED 必须带原因码
    with pytest.raises(ValueError):
        st.transition("run-bbbbbbbbbb", "FAILED", reason_code=6999)                   # 未登记的原因码
    with pytest.raises(ValueError):
        _enqueue(st, "run-cccccccccc", ranked=True, seed=5)                          # 排名运行不落明文种子
    _enqueue(st, "run-cccccccccc", ranked=True)
    assert st.get("run-cccccccccc")["ranked"] and "seed" not in st.get("run-cccccccccc")
    with pytest.raises(RunStateError) as e:
        st.transition("run-nope000000", "PREPARING")
    assert e.value.code == 305


def test_concurrent_stores_compare_and_set(tmp_path: Path) -> None:
    """调度器与 API 各持一个连接：调度器先迁到 PREPARING 后，API 的取消转为取消请求，不覆盖调度器的迁移。"""
    api, sched = RunStore(tmp_path / "b.sqlite"), RunStore(tmp_path / "b.sqlite")
    _enqueue(api, "run-dddddddddd")
    sched.transition("run-dddddddddd", "PREPARING", expect="QUEUED")
    r = api.request_cancel("run-dddddddddd", actor="alice")
    assert r["state"] == "PREPARING" and r["cancel"]["by"] == "alice"
    assert [c["run_id"] for c in sched.pending_cancels()] == ["run-dddddddddd"]
    with pytest.raises(RunStateError):
        api.transition("run-dddddddddd", "RUNNING", expect="QUEUED")                  # 过期的期望状态被拒绝
    assert sched.transition("run-dddddddddd", "CANCELLED", actor="scheduler")["state"] == "CANCELLED"
    assert sched.pending_cancels() == []


# ------------------------------------------------------------------------------------------------------------- 取消（BN-44）
def test_cancel_endpoint(env) -> None:
    c, app, _s, clock, h, h_inv = env
    st: RunStore = app.state.runs
    for rid in ("run-q000000001", "run-r000000001", "run-f000000001", "run-e000000001"):
        _enqueue(st, rid)
    r = c.delete(f"{P}/runs/run-q000000001", headers=h)
    assert r.status_code == 204                                                         # QUEUED 立即生效
    q = c.get(f"{P}/runs/run-q000000001", headers=h).json()
    assert q["state"] == "CANCELLED" and q["terminal"] and q["cancel"]["by"] == "alice" and q["cancel"]["deadline_unix_ns"] is None
    assert q["actions"] == {"cancel": False, "retry": True}
    _walk(st, "run-r000000001", "PREPARING", "RUNNING")
    clock.tick(2)
    assert c.delete(f"{P}/runs/run-r000000001", headers=h).status_code == 204
    rr = c.get(f"{P}/runs/run-r000000001", headers=h).json()
    assert rr["state"] == "RUNNING" and rr["cancel"]["deadline_unix_ns"] == rr["cancel"]["requested_unix_ns"] + 10 * S   # 10 s 内终止
    assert rr["actions"]["cancel"] is False
    assert c.delete(f"{P}/runs/run-r000000001", headers=h).status_code == 204          # 重复取消幂等
    assert st.overdue_cancels() == []
    clock.tick(11)
    assert [x["run_id"] for x in st.overdue_cancels()] == ["run-r000000001"]
    st.transition("run-r000000001", "CANCELLED", actor="scheduler")
    rr = c.get(f"{P}/runs/run-r000000001", headers=h).json()
    assert rr["state"] == "CANCELLED" and [t["to"] for t in rr["transitions"]][-2:] == ["CANCEL_REQUESTED", "CANCELLED"]
    _walk(st, "run-f000000001", "PREPARING", "RUNNING", "FINALIZING")
    r = c.delete(f"{P}/runs/run-f000000001", headers=h)
    assert r.status_code == 409 and r.json()["code"] == 680 and r.json()["detail"] == {"why": "not_cancellable", "state": "FINALIZING"}
    _walk(st, "run-e000000001", "PREPARING", "RUNNING", "FINALIZING", "EVALUATING", "COMPLETED")
    r = c.delete(f"{P}/runs/run-e000000001", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["why"] == "terminal"           # 终态上的取消 409 680
    r = c.delete(f"{P}/runs/cap-0001", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["why"] == "unmanaged"
    assert c.delete(f"{P}/runs/run-nope000000", headers=h).json()["code"] == 305
    assert c.delete(f"{P}/runs/run-f000000001", headers=h_inv).json()["code"] == 115
    tok = app.state.auth.issue_token("carol", "researcher")[1]
    assert c.post(f"{P}/session", json={"token": tok}).status_code == 204
    _enqueue(st, "run-c000000001")
    r = c.delete(f"{P}/runs/run-c000000001")                                            # Cookie 写请求缺 Origin 与 X-D3-CSRF
    assert r.status_code == 403 and r.json()["code"] == 303
    assert c.delete(f"{P}/runs/run-c000000001", headers={"Origin": ORIGIN, "X-D3-CSRF": "1"}).status_code == 204
    assert c.get(f"{P}/runs/run-c000000001", headers=h).json()["cancel"]["by"] == "carol"


# ------------------------------------------------------------------------------------------------------------- 重试（BN-45）与原因码
def test_retry_endpoint_and_reason_codes(env) -> None:
    c, app, s, clock, h, _ = env
    st: RunStore = app.state.runs
    _enqueue(st, "run-x000000001")
    _walk(st, "run-x000000001", "PREPARING", "RUNNING", "FINALIZING")
    clock.tick(5)
    st.transition("run-x000000001", "FAILED", reason_code=687, reason_detail={"why": "mcap_crc", "chunk": 12}, actor="controller")
    rd = s.dataset_dir / "abc-crossing" / "runs" / "run-x000000001"
    rd.mkdir(parents=True)
    (rd / "raw.mcap").write_bytes(b"partial")
    f = c.get(f"{P}/runs/run-x000000001", headers=h).json()
    assert f["state"] == "FAILED" and f["actions"] == {"cancel": False, "retry": True}
    assert f["reason"] == {"code": 687, "name": "RECORDING_INTEGRITY_FAILED", "message": f["reason"]["message"], "remedy": f["reason"]["remedy"],
                           "detail": {"why": "mcap_crc", "chunk": 12}} and f["reason"]["message"]
    lst = c.get(f"{P}/runs?state=FAILED", headers=h).json()["items"]
    assert [i["run_id"] for i in lst] == ["run-x000000001"] and lst[0]["reason"]["code"] == 687        # 列表同样输出原因码
    r = c.post(f"{P}/runs/run-x000000001/retry", headers={**h, "Idempotency-Key": "k-1"})
    assert r.status_code == 202 and r.json() == {"run_id": "run-x000000001", "attempt": 2}
    g = c.get(f"{P}/runs/run-x000000001", headers=h).json()
    assert g["state"] == "QUEUED" and g["attempt"] == 2 and g["restart_count"] == 0 and g["reason"] is None and g["queue_position"] == 1
    assert g["started_unix_ns"] is None and g["ended_unix_ns"] is None
    assert [(a["attempt"], a["state"], a["dir"], a["reason"] and a["reason"]["code"]) for a in g["attempts"]] == [
        (1, "FAILED", ".attempts/1", 687), (2, "QUEUED", None, None)]
    assert (rd / ".attempts" / "1" / "raw.mcap").read_bytes() == b"partial" and not (rd / "raw.mcap").exists()   # 上一 attempt 产物归档
    r = c.post(f"{P}/runs/run-x000000001/retry", headers={**h, "Idempotency-Key": "k-1"})
    assert r.status_code == 202 and r.json() == {"run_id": "run-x000000001", "attempt": 2}               # 幂等重放
    assert len(c.get(f"{P}/runs/run-x000000001", headers=h).json()["attempts"]) == 2
    r = c.post(f"{P}/runs/run-x000000001/retry", headers=h)
    assert r.status_code == 409 and r.json()["code"] == 680 and r.json()["detail"]["why"] == "not_retryable"
    _enqueue(st, "run-y000000001")
    st.request_cancel("run-y000000001", actor="alice")
    r = c.post(f"{P}/runs/run-y000000001/retry", headers={**h, "Idempotency-Key": "k-1"})
    assert r.status_code == 409 and r.json()["detail"]["why"] == "idempotency_key_reused"
    r = c.post(f"{P}/runs/run-y000000001/retry", headers=h)
    assert r.status_code == 202 and r.json()["attempt"] == 2                          # CANCELLED 也可重试
    assert c.post(f"{P}/runs/cap-0001/retry", headers=h).json()["detail"]["why"] == "unmanaged"
    assert c.post(f"{P}/runs/run-nope000000/retry", headers=h).json()["code"] == 305


def test_retry_queue_quota(tmp_path: Path) -> None:
    st = RunStore(tmp_path / "q.sqlite", queue_per_tenant=1)
    _enqueue(st, "run-a000000001")
    _enqueue(st, "run-b000000001")
    st.transition("run-b000000001", "PREPARING")
    st.transition("run-b000000001", "FAILED", reason_code=683, reason_detail={"why": "ready_timeout"})
    with pytest.raises(RunStateError) as e:
        st.retry("run-b000000001", actor="alice")
    assert e.value.code == 681 and e.value.detail["why"] == "queue_full" and e.value.detail["limit"] == 1
    st.request_cancel("run-a000000001", actor="alice")
    assert st.retry("run-b000000001", actor="alice") == {"run_id": "run-b000000001", "attempt": 2, "replayed": False}


def test_retry_quota_maps_to_429(env) -> None:
    c, app, s, _clock, h, _ = env
    app.state.runs = st = RunStore(s.db_path, queue_per_tenant=1)
    _enqueue(st, "run-a000000001")
    _enqueue(st, "run-b000000001")
    st.transition("run-b000000001", "PREPARING")
    st.transition("run-b000000001", "FAILED", reason_code=631)
    r = c.post(f"{P}/runs/run-b000000001/retry", headers=h)
    assert r.status_code == 429 and r.json()["code"] == 681 and "retry-after" in r.headers


# ------------------------------------------------------------------------------------------------------------- 列表（BN-42）
def test_runs_listing_filters_and_cursor(env) -> None:
    c, app, _s, clock, h, _ = env
    st: RunStore = app.state.runs
    for i in range(5):
        clock.tick()
        _enqueue(st, f"run-l00000000{i}", cls="batch" if i == 0 else "research")
    _enqueue(st, "run-m000000001", event="other-event", ranked=True)
    st.set_blocked("run-l000000002", ["gpu_share", "write"])
    st.transition("run-l000000004", "PREPARING")
    r = c.get(f"{P}/runs", headers=h).json()
    ids = [i["run_id"] for i in r["items"]]
    assert ids == ["cap-0001", *[f"run-l00000000{i}" for i in range(5)], "run-m000000001"] and r["total"] == 7
    by = {i["run_id"]: i for i in r["items"]}
    assert by["cap-0001"]["managed"] is False and by["cap-0001"]["state"] == "SUCCEEDED" and by["cap-0001"]["actions"]["retry"] is False
    assert by["run-l000000001"]["queue_position"] == 1 and by["run-l000000000"]["queue_position"] == 5   # research 先于 batch
    assert by["run-m000000001"]["queue_position"] == 4 and by["run-l000000004"]["queue_position"] is None
    assert by["run-l000000002"]["blocked_by"] == ["gpu_share", "write"] and by["run-l000000001"]["policy"] == "track_share"
    assert by["run-l000000001"]["n_uav"] == 3 and by["run-l000000001"]["actions"] == {"cancel": True, "retry": False}
    assert "seed" not in by["run-l000000001"] and "spec_json" not in by["run-l000000001"]
    assert [i["run_id"] for i in c.get(f"{P}/runs?event=other-event", headers=h).json()["items"]] == ["run-m000000001"]
    assert [i["run_id"] for i in c.get(f"{P}/runs?ranked=true", headers=h).json()["items"]] == ["run-m000000001"]
    assert [i["run_id"] for i in c.get(f"{P}/runs?state=PREPARING,SUCCEEDED", headers=h).json()["items"]] == ["cap-0001", "run-l000000004"]
    assert [i["run_id"] for i in c.get(f"{P}/runs?ids=run-l000000003,cap-0001", headers=h).json()["items"]] == ["cap-0001", "run-l000000003"]
    assert [i["run_id"] for i in c.get(f"{P}/runs?experiment=exp-aaaaaaaaaa&limit=100", headers=h).json()["items"]][:1] == ["run-l000000000"]
    assert c.get(f"{P}/runs?policy=coverage_scan", headers=h).json()["items"] == []
    seen, cur = [], None
    while True:
        page = c.get(f"{P}/runs?limit=3" + (f"&cursor={cur}" if cur else ""), headers=h).json()
        seen += [i["run_id"] for i in page["items"]]
        cur = page["next_cursor"]
        if cur is None:
            break
    assert seen == ids
    assert c.get(f"{P}/runs?state=DONE", headers=h).json()["code"] == 300
    assert c.get(f"{P}/runs?cursor=%%%", headers=h).json()["code"] == 300
    d = c.get(f"{P}/runs/cap-0001", headers=h).json()
    assert d["managed"] is False and d["state"] == "SUCCEEDED" and d["world_id"] == "oryahovo-crop1"


def test_meta_announces_lab_features(env) -> None:
    c, _app, _s, _clock, h, _ = env
    f = c.get(f"{P}/meta", headers=h).json()["features"]
    assert f["worlds"] is True and f["events"] == "read" and f["runs"] == "manage"
