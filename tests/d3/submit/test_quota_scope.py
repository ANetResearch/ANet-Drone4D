"""受邀成员提交的配额、墙钟上限、令牌撤销与数据范围（D3-AC-59 的本机部分；AWR-17 §18.23.3、§18.23.5；§5.6.7；M27-FR-168 至 FR-170）。

- 配额（681）：每 UTC 自然周正式提交数（受邀成员每团队 ≤ 2、内部成员每人 ≤ 5）、冒烟验证每人每日 ≤ 10、同时排队 ≤ 2、每团队每周
  GPU 时间 ≤ 40 GPU·h；撤回、扫描或冒烟拒绝与平台故障不计入；管理员以 `quota.json` 调整；
- 墙钟上限（693）：单 Run `10 × duration_s + 600 s`，超限即终止（阻塞调用与吞异常的代码同样被打断），Env 与容器按 finally 关闭，
  冒烟超限 REJECTED 693，正式 Run 超限记为无效行；
- 撤销：bench token 撤销后，会话、短期令牌与推送令牌在 ≤ 2 s 内失效（另一个进程的 `BenchAuth` 执行撤销，逐次校验无缓存）；
- 数据范围：受邀成员访问实时 topic 与 Rerun catalog 的令牌、真值接口、他人提交与他队镜像仓库一律 403 `115` 并写 `deny` 审计；
  结果缺省只对提交者团队可见。

ZeroTier 授权、`d3-registry` 部署与真实推送在 dmax 上验证（B25）。
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import signal
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from awr.api.bench_main import create_app
from awr.bench import subworker as sw
from awr.bench.auth import INVITED_TOKEN_MAX_S, AuthError, BenchAuth, Principal
from awr.bench.boardstore import append_rows, load_rows, publish, slim_evaluation
from awr.bench.settings import BenchSettings
from awr.bench.submissions import (
    Quota,
    SubmissionError,
    SubmissionStore,
    WallLimitExceeded,
    guarded_run,
    run_duration_s,
    wall_limit,
    wall_limit_s,
    week_start_ns,
)

P = "/api/bench/v1"
REQ = {"track": "std3-l1", "image_digest": "sha256:" + "b" * 64, "entry": {"cmd": ["python", "-m", "my_agent"]}}
MK = {"board_version": "proto-0", "tracks": {"std3-l1"}, "lanes": {"rendezvous_handoff"}}
H = 3600.0
# 2026-10-21（周三）12:00 UTC；所在周起点 2026-10-19（周一）00:00 UTC
T0 = int(dt.datetime(2026, 10, 21, 12, tzinfo=dt.UTC).timestamp()) * 1_000_000_000
DAY = 86_400 * 1_000_000_000


class Clock:
    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def _finish(st: SubmissionStore, sid: str, last: str = "DONE") -> None:
    """把提交推进到终态（释放排队名额）。"""
    if last in ("WITHDRAWN",):
        st.withdraw(sid)
        return
    if last in ("REJECTED", "FAILED"):
        st.transition(sid, last, reason={"code": 692 if last == "REJECTED" else 320, "name": "X"})
        return
    for s in ("SCANNING", "VALIDATING", "EVALUATING", "SCORING", "DONE"):
        st.transition(sid, s, result={"lanes": []} if s == "DONE" else None)


def _create(st: SubmissionStore, owner: str, team: str, role: str = "invited", kind: str = "formal") -> str:
    return st.create(REQ, owner=owner, team=team, role=role, kind=kind, **MK)["submission_id"]


def _quota_err(st: SubmissionStore, owner: str, team: str, role: str = "invited", kind: str = "formal") -> dict:
    with pytest.raises(SubmissionError) as ei:
        _create(st, owner, team, role, kind)
    assert ei.value.code == 681
    return ei.value.detail


# ── 配额（681） ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_quota_defaults_follow_the_spec() -> None:
    """§5.6.7 与 M27 配置表 `submit.quota`：2、5、10、2、40。"""
    q = Quota()
    assert (q.invited_per_team_week, q.internal_per_user_week, q.smoke_per_user_day, q.queued, q.gpu_h_per_team_week) == (2, 5, 10, 2, 40.0)
    assert week_start_ns(T0) == int(dt.datetime(2026, 10, 19, tzinfo=dt.UTC).timestamp()) * 1_000_000_000
    assert week_start_ns(week_start_ns(T0)) == week_start_ns(T0) and week_start_ns(week_start_ns(T0) - 1) == week_start_ns(T0) - 7 * DAY


def test_invited_weekly_formal_quota_is_per_team(tmp_path: Path) -> None:
    clk = Clock()
    st = SubmissionStore(tmp_path / "submissions", clock=clk)
    for _ in range(2):
        _finish(st, _create(st, "ann", "team-a"))
    d = _quota_err(st, "amy", "team-a")                                   # 同队另一成员：团队配额已用尽
    assert d["why"] == "submit_quota" and d["quota"] == "formal_week" and (d["limit"], d["used"]) == (2, 2) and d["subject"] == "team:team-a"
    assert int(d["resets_unix_ns"]) == week_start_ns(T0) + 7 * DAY
    _create(st, "bob", "team-b")                                           # 他队不受影响
    clk.t = week_start_ns(T0) + 7 * DAY                                    # 下一 UTC 自然周（周一 00:00 UTC）恢复
    _create(st, "amy", "team-a")


@pytest.mark.parametrize("last", ["WITHDRAWN", "REJECTED", "FAILED"])
def test_withdrawn_rejected_and_failed_do_not_count(tmp_path: Path, last: str) -> None:
    st = SubmissionStore(tmp_path / "submissions", clock=Clock())
    for _ in range(3):
        _finish(st, _create(st, "ann", "team-a"), last)
    _finish(st, _create(st, "ann", "team-a"))
    _finish(st, _create(st, "ann", "team-a"))
    assert _quota_err(st, "ann", "team-a")["quota"] == "formal_week"


def test_internal_weekly_quota_is_per_user(tmp_path: Path) -> None:
    st = SubmissionStore(tmp_path / "submissions", clock=Clock())
    for _ in range(5):
        _finish(st, _create(st, "alice", "d3", "researcher"))
    d = _quota_err(st, "alice", "d3", "researcher")
    assert d["quota"] == "formal_week" and (d["limit"], d["subject"]) == (5, "user:alice")
    _create(st, "rae", "d3", "researcher")                                  # 同项目另一研究者按本人计量


def test_queue_limit_and_daily_smoke_quota(tmp_path: Path) -> None:
    clk = Clock()
    st = SubmissionStore(tmp_path / "submissions", clock=clk)
    a = _create(st, "ann", "team-a", kind="smoke")
    _create(st, "amy", "team-a", kind="smoke")
    assert _quota_err(st, "ann", "team-a", kind="smoke")["quota"] == "queued"     # 同时排队 ≤ 2（受邀成员按团队）
    _finish(st, a)
    for s in st.listing(team="team-a"):
        if s["state"] == "RECEIVED":
            _finish(st, s["submission_id"])
    for _ in range(8):                                                       # 冒烟按人计量：ann 当日共 10 次
        _finish(st, _create(st, "ann", "team-a", kind="smoke"))
    d = _quota_err(st, "ann", "team-a", kind="smoke")
    assert d["quota"] == "smoke_day" and (d["limit"], d["used"]) == (10, 10)
    _create(st, "ann", "team-a")                                             # 冒烟不占正式提交名额
    clk.t += DAY                                                             # 次日 UTC 恢复
    _create(st, "ann", "team-a", kind="smoke")


def test_weekly_gpu_hours_per_team_and_admin_override(tmp_path: Path) -> None:
    clk = Clock()
    st = SubmissionStore(tmp_path / "submissions", clock=clk)
    clk.t = week_start_ns(T0) - 1                                            # 上周的用量不计入本周
    old = _create(st, "alice", "team-a", "researcher")
    st.charge_gpu(old, 100 * H, run_id=f"{old}-r0")
    _finish(st, old)
    clk.t = T0
    s1 = _create(st, "ann", "team-a")
    st.charge_gpu(s1, 39.5 * H, run_id=f"{s1}-r0")
    _finish(st, s1)
    q = st.quota_status(owner="ann", team="team-a", role="invited")
    assert q["gpu_hours"]["used"] == 39.5
    s2 = _create(st, "ann", "team-a")
    st.charge_gpu(s2, 0.5 * H, run_id=f"{s2}-r0")                             # 合计 40 GPU·h：达到上限
    _finish(st, s2)
    d = _quota_err(st, "rae", "team-a", "researcher")                        # GPU 时间按团队计量，内部成员同样受限
    assert d["why"] == "gpu_hours" and (d["limit_gpu_h"], d["used_gpu_h"]) == (40.0, 40.0)
    _create(st, "bob", "team-b")
    (st.root / "quota.json").write_text(json.dumps({"gpu_h_per_team_week": 60, "overrides": {"team:team-a": {"invited_per_team_week": 3}}}))
    _create(st, "rae", "team-a", "researcher")                               # 管理员调整即时生效（每次登记重读）
    assert st.quota_status(owner="ann", team="team-a", role="invited")["formal_week"]["limit"] == 3


# ── 墙钟上限（693） ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_wall_limit_formula(tmp_path: Path) -> None:
    assert wall_limit_s(60.0) == 1200.0 and wall_limit_s(360.0) == 4200.0 and wall_limit_s(0.0) == 600.0
    ev = tmp_path / "events" / "e1" / "v2"
    ev.mkdir(parents=True)
    (ev / "plan.json").write_text(json.dumps({"duration_ms": 360_000}))
    assert run_duration_s({"event": {"id": "e1", "version": 2}, "duration_s": 60.0}, tmp_path / "events") == 60.0
    assert run_duration_s({"event": {"id": "e1", "version": 2}}, tmp_path / "events") == 360.0              # 缺省为事件时长（与 Env 同口径）
    assert run_duration_s({"event": {"id": "nope"}}, tmp_path / "events") == 0.0


def test_wall_limit_interrupts_blocking_calls_and_swallowing_code() -> None:
    handler = signal.getsignal(signal.SIGALRM)
    a, b = socket.socketpair()
    try:
        t0 = time.monotonic()
        with pytest.raises(WallLimitExceeded) as ei, wall_limit(0.2):
            a.recv(1)                                                        # 阻塞读（Agent 不回动作）
        assert 0.15 <= time.monotonic() - t0 < 1.0 and ei.value.code == 693 and ei.value.elapsed_s >= 0.15
    finally:
        a.close()
        b.close()
    with pytest.raises(WallLimitExceeded), wall_limit(0.2):
        while True:
            with contextlib.suppress(Exception):                             # Env 内部的宽泛捕获不吞掉终止信号
                time.sleep(0.05)
    assert signal.getsignal(signal.SIGALRM) == handler and signal.getitimer(signal.ITIMER_REAL)[0] == 0   # 处理器复原、计时器已解除
    with wall_limit(5.0):
        time.sleep(0.01)                                                     # 未超限：不抛出


def test_wall_limit_in_worker_thread() -> None:
    out: list[BaseException] = []

    def work() -> None:
        try:
            with wall_limit(0.2):
                while True:
                    sum(range(1000))
        except WallLimitExceeded as e:
            out.append(e)

    th = threading.Thread(target=work)
    th.start()
    th.join(5.0)
    assert not th.is_alive() and len(out) == 1 and out[0].elapsed_s >= 0.15


def test_guarded_run_terminates_cleans_up_and_charges(tmp_path: Path) -> None:
    st = SubmissionStore(tmp_path / "submissions")
    sid = _create(st, "ann", "team-a")
    closed: list[str] = []

    def hung(rs, *, run_id, run_dir, **kw):
        try:
            while True:
                time.sleep(0.02)
        finally:                                                             # Env.close 与 AgentContainers.stop 所在的 finally
            closed.append(run_id)

    with pytest.raises(WallLimitExceeded) as ei:
        guarded_run(hung, st, sid, {}, limit_s=0.3, run_id=f"{sid}-r0", run_dir=tmp_path / "r0")
    assert closed == [f"{sid}-r0"]
    term = json.loads((tmp_path / "r0" / "termination.json").read_text())
    assert term["code"] == 693 and term["name"] == "RUN_WALL_LIMIT_EXCEEDED" and term["detail"]["why"] == "wall_limit"
    assert term["detail"]["limit_s"] == 0.3 and term["detail"]["elapsed_s"] >= 0.25 and ei.value.reason()["code"] == 693
    gpu = st.get(sid)["_meta"]["gpu"]
    assert len(gpu) == 1 and gpu[0]["run_id"] == f"{sid}-r0" and 0.25 / 3 <= gpu[0]["gpu_s"] < 1.0      # 已用墙钟 × 1/3 卡

    res = guarded_run(lambda rs, **kw: {"run_wall_s": 30.0}, st, sid, {}, limit_s=5, run_id=f"{sid}-r1", run_dir=tmp_path / "r1")
    assert res == {"run_wall_s": 30.0} and st.get(sid)["_meta"]["gpu"][-1]["gpu_s"] == 10.0
    guarded_run(lambda rs, **kw: {"gpu_s": 7.5}, st, sid, {}, limit_s=5, run_id=f"{sid}-r2", run_dir=tmp_path / "r2")
    assert st.get(sid)["_meta"]["gpu"][-1]["gpu_s"] == 7.5                   # 实测值优先

    def boom(rs, **kw):
        raise RuntimeError("gz instance lost")

    with pytest.raises(RuntimeError):
        guarded_run(boom, st, sid, {}, limit_s=5, run_id=f"{sid}-r3", run_dir=tmp_path / "r3")
    assert len(st.get(sid)["_meta"]["gpu"]) == 3                             # 平台原因失败不记账


def _fake_docker(tmp_path: Path) -> str:
    p = tmp_path / "docker"
    p.write_text("#!/bin/sh\necho '{\"Os\": \"linux\", \"Architecture\": \"amd64\", \"Size\": 1048576}'\n")
    p.chmod(0o755)
    return str(p)


def _ev(ec: float) -> dict:
    return slim_evaluation({"ranked": True, "ranked_valid": True, "invalid_reasons": [], "components": {"ec": ec, "eu": 0.4, "ao": 0.5, "ce": 0.3}})


class _Jobs:
    """`run_job` 替身：`hang` 中的 Run 不结束（由墙钟上限终止），其余写 evaluation.json 并返回墙钟。"""

    def __init__(self, hang: set[str]) -> None:
        self.hang, self.calls = set(hang), []

    def __call__(self, rs: dict, *, run_id: str, run_dir: Path, seed: int, **kw) -> dict:
        self.calls.append(run_id)
        if run_id in self.hang:
            while True:
                time.sleep(0.02)
        if run_id.endswith("-smoke"):
            return {"agents": {"uav-01": {"acts": 10, "exited": "bye", "exit_code": 0}}, "run_wall_s": 3.0}
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "evaluation.json").write_text(json.dumps({"schema": "drone4d.bench.evaluation.v1", **_ev(0.7)}))
        return {"run_dir": str(run_dir), "agents": {"uav-01": {"acts": 100}}, "run_wall_s": 90.0}


@pytest.fixture()
def board_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "d3"
    b = root / "boards" / "proto-0"
    b.mkdir(parents=True)
    cfg = {"run_spec": {"event": {"id": "crop-handoff-001"}, "duration_s": 60.0}, "seeds": [101, 102, 103], "events_root": str(tmp_path),
           "worlds_root": str(tmp_path), "physics": "realistic", "track_id": "std3-l1", "lane": "rendezvous_handoff", "event_type": "rendezvous_handoff"}
    (b / "config.json").write_text(json.dumps(cfg))
    append_rows(b, [{"entry_id": "baseline/x", "team": "D3 reference", "display_name": "X", "baseline": True, "track": "std3-l1",
                     "lane": "rendezvous_handoff", "event_type": "rendezvous_handoff", "event": "crop-handoff-001", "seed": s, "evaluation": _ev(0.5)}
                    for s in range(3)])
    publish(b, n_boot=100)
    seen: list[float] = []

    def short(d: float) -> float:                                            # 墙钟上限按公式计算后缩短到可测的量级
        seen.append(wall_limit_s(d))
        return 0.3

    monkeypatch.setattr(sw, "wall_limit_s", short)
    LIMITS[root] = seen
    return root


LIMITS: dict[Path, list[float]] = {}


def test_subworker_terminates_formal_run_at_wall_limit(board_root: Path, tmp_path: Path) -> None:
    st = SubmissionStore(board_root / "submissions")
    sid = _create(st, "ann", "team-a")
    jobs = _Jobs({f"{sid}-r1"})
    assert sw.process_one(board_root, docker=_fake_docker(tmp_path), run_job=jobs, n_boot=100, log=lambda m: None) == sid
    d = st.get(sid)
    assert d["state"] == "DONE" and jobs.calls == [f"{sid}-smoke", f"{sid}-r0", f"{sid}-r1", f"{sid}-r2"]      # 超限不重跑
    assert LIMITS[board_root] == [wall_limit_s(20.0), *[wall_limit_s(60.0)] * 3]                                  # 冒烟 20 s，正式 60 s → 1200 s
    lane = d["result"]["lanes"][0]
    assert lane["runs"] == {"total": 3, "valid": 2, "invalid": 1, "platform_rerun": 0} and lane["status"] == "invalid"   # 无效占比 > 20%
    row = next(r for r in load_rows(board_root / "boards/proto-0") if r.get("submission_id") == sid and r["seed"] == 1)
    assert row["evaluation"]["ranked_valid"] is False and row["evaluation"]["invalid_reasons"] == ["wall_limit"]
    assert row["termination"]["code"] == 693 and row["termination"]["detail"]["why"] == "wall_limit"
    assert json.loads((board_root / f"dataset/crop-handoff-001/runs/{sid}-r1/termination.json").read_text())["code"] == 693
    gpu = {c["run_id"]: c["gpu_s"] for c in d["_meta"]["gpu"]}
    assert gpu[f"{sid}-smoke"] == 1.0 and gpu[f"{sid}-r0"] == 30.0 and gpu[f"{sid}-r2"] == 30.0 and 0 < gpu[f"{sid}-r1"] < 1.0


def test_subworker_rejects_smoke_over_wall_limit(board_root: Path, tmp_path: Path) -> None:
    st = SubmissionStore(board_root / "submissions")
    sid = _create(st, "ann", "team-a")
    jobs = _Jobs({f"{sid}-smoke"})
    sw.process_one(board_root, docker=_fake_docker(tmp_path), run_job=jobs, n_boot=100, log=lambda m: None)
    d = st.get(sid)
    assert d["state"] == "REJECTED" and d["reason"]["code"] == 693 and d["reason"]["detail"]["stage"] == "smoke"
    assert jobs.calls == [f"{sid}-smoke"]


# ── bench-api：配额、撤销与数据范围 ──────────────────────────────────────────────────────────────────────────────────

class Env:
    def __init__(self, tmp_path: Path) -> None:
        s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=("https://lab.example",), secure_cookie=False)
        b = s.data_root / "boards" / "proto-0"
        b.mkdir(parents=True)
        (b / "config.json").write_text(json.dumps({"track": {"fleet_ref": "3x-x500"}, "seeds": [1]}))
        append_rows(b, [{"entry_id": "baseline/x", "team": "D3 reference", "display_name": "X", "baseline": True, "track": "std3-l1",
                         "lane": "rendezvous_handoff", "event": "e1", "seed": 0, "evaluation": _ev(0.5)}])
        publish(b, n_boot=100)
        self.settings = s
        self.app = create_app(s)
        self.client = TestClient(self.app)
        self.auth: BenchAuth = self.app.state.auth
        self.ids, self.tokens = {}, {}
        for n, role, proj in (("ann", "invited", ("team-a",)), ("bob", "invited", ("team-b",)), ("rae", "researcher", ("d3",)),
                              ("eve", "evaluator", ()), ("root", "admin", ())):
            self.ids[n], self.tokens[n] = self.auth.issue_token(n, role, proj)

    def h(self, n: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[n]}"}

    def store(self) -> SubmissionStore:
        return SubmissionStore(self.settings.data_root / "submissions")

    def deny(self, since: int) -> list[dict]:
        return [a for a in self.auth.audit.read()[since:] if a["action"] == "deny"]


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_api_returns_681_with_quota_detail(env: Env) -> None:
    c = env.client
    for _ in range(2):
        r = c.post(f"{P}/submissions", json=REQ, headers=env.h("ann"))
        assert r.status_code == 202
        _finish(env.store(), r.json()["submission_id"])
    r = c.post(f"{P}/submissions", json=REQ, headers=env.h("ann"))
    assert r.status_code == 429 and r.json()["code"] == 681 and r.json()["detail"]["why"] == "submit_quota"
    assert r.json()["detail"]["quota"] == "formal_week"
    q = c.get(f"{P}/submissions", headers=env.h("ann")).json()["quota"]
    assert q["subject"] == "team:team-a" and q["formal_week"] == {"used": 2, "limit": 2, "resets_unix_ns": q["formal_week"]["resets_unix_ns"]}
    assert q["gpu_hours"]["limit"] == 40.0 and q["smoke_day"]["limit"] == 10 and q["queued"] == {"used": 0, "limit": 2}
    sid = next(iter(env.store().listing(team="team-a")))["submission_id"]
    env.store().charge_gpu(sid, 40 * H, run_id=f"{sid}-r0")
    r = c.post(f"{P}/submissions", json=REQ, headers=env.h("rae"))           # 研究者 rae 的团队是 d3：不受 team-a 的 GPU 用量影响
    assert r.status_code == 202
    assert c.post(f"{P}/registry/token", json={}, headers=env.h("ann")).json()["detail"]["why"] == "gpu_hours"   # 已无可用配额：不再签发推送令牌


def test_dev_board_submissions_count_as_smoke(env: Env) -> None:
    cfg = env.settings.data_root / "boards/proto-0/config.json"
    cfg.write_text(json.dumps({"track": {"fleet_ref": "3x-x500", "dev": True}, "seeds": [1]}))
    sid = env.client.post(f"{P}/submissions", json=REQ, headers=env.h("ann")).json()["submission_id"]
    m = env.store().get(sid)["_meta"]
    assert (m["kind"], m["role"], m["team"], m["owner"]) == ("smoke", "invited", "team-a", "ann")


def test_revocation_invalidates_session_short_and_push_tokens_within_2s(env: Env) -> None:
    c = env.client
    assert c.post(f"{P}/session", json={"token": env.tokens["ann"]}).status_code == 204
    push = c.post(f"{P}/registry/token", json={}, headers=env.h("ann")).json()
    assert push["namespace"] == "team-a" and push["repository_prefix"] == "team-a/" and push["username"] == "ann"
    frames = env.auth.sign_short(env.auth.verify_token(env.tokens["rae"]), "frames")[0]
    sidecar = BenchAuth(env.settings.keys_dir, env.settings.db_path)          # d3-registry 前置鉴权进程
    ctl = BenchAuth(env.settings.keys_dir, env.settings.db_path)              # 管理员 ctl token revoke 进程
    repo = "/v2/team-a/agent/manifests/v1"
    assert sidecar.authorize_registry(push["token"], "PUT", repo).name == "ann"
    assert c.get(f"{P}/session").status_code == 200
    rae = TestClient(env.app)
    assert rae.post(f"{P}/session", json={"token": env.tokens["rae"]}).status_code == 204

    def dead_push() -> bool:
        try:
            sidecar.authorize_registry(push["token"], "PUT", repo)
            return False
        except AuthError as e:
            return e.code == 302

    def dead_frames() -> bool:
        try:
            sidecar.verify_short(frames, "frames")
            return False
        except AuthError as e:
            return e.code == 302

    probes = {"session": lambda: c.get(f"{P}/session").status_code == 401, "push": dead_push}
    t0 = time.monotonic()
    assert ctl.revoke_token(env.ids["ann"]) and ctl.revoke_token(env.ids["rae"])
    probes.update({"frames": dead_frames, "session_researcher": lambda: rae.get(f"{P}/session").status_code == 401})
    took: dict[str, float] = {}
    while len(took) < len(probes) and time.monotonic() - t0 < 5.0:
        for k, f in probes.items():
            if k not in took and f():
                took[k] = time.monotonic() - t0
        time.sleep(0.01)
    print("revocation latency (s):", {k: round(v, 4) for k, v in took.items()})
    assert set(took) == set(probes) and max(took.values()) <= 2.0
    assert c.post(f"{P}/registry/token", json={}, headers=env.h("ann")).status_code == 401
    assert next(t for t in ctl.list_tokens() if t["id"] == env.ids["ann"])["revoked_unix"] > 0
    denies = [a for a in env.auth.audit.read() if a["action"] == "deny" and a.get("route") == "registry"]
    assert denies and denies[-1]["code"] == 302 and denies[-1]["why"] == "push_token"


def test_invited_token_expires_within_90_days(env: Env) -> None:
    now = [time.time()]
    auth = BenchAuth(env.settings.keys_dir, env.settings.db_path, clock=lambda: now[0])
    tid, tok = auth.issue_token("ivy", "invited", ("team-i",))
    rec = next(t for t in auth.list_tokens() if t["id"] == tid)
    assert rec["expires_unix"] - rec["issued_unix"] == INVITED_TOKEN_MAX_S
    with pytest.raises(ValueError):
        auth.issue_token("ivy", "invited", ("team-i",), ttl_s=INVITED_TOKEN_MAX_S + 1)
    session, _exp = auth.create_session(auth.verify_token(tok))
    push = auth.sign_push(auth.verify_token(tok))[0]
    now[0] += INVITED_TOKEN_MAX_S - 60
    assert auth.verify_token(tok).role == "invited"
    now[0] += 60
    for f in (lambda: auth.verify_token(tok), lambda: auth.resolve_session(session), lambda: auth.authorize_registry(push, "GET", "/v2/")):
        with pytest.raises(AuthError) as ei:
            f()
        assert ei.value.code == 302
    assert "expires_unix" not in next(t for t in auth.list_tokens() if t["id"] == env.ids["rae"])   # 内部成员令牌不设缺省到期


def test_invited_cannot_reach_topics_catalog_or_truth(env: Env) -> None:
    c = env.client
    for aud in ("lab-rt", "rerun"):                                          # 实时 topic（lab-gateway）与 Rerun catalog（rerun-proxy）
        before = len(env.auth.audit.read())
        r = c.post(f"{P}/session/short-token", json={"aud": aud}, headers=env.h("ann"))
        assert r.status_code == 403 and r.json()["code"] == 115
        assert [(a["code"], a["principal"], a["route"]) for a in env.deny(before)] == [(115, "ann", f"{P}/session/short-token")]
    p = env.auth.verify_token(env.tokens["ann"])
    for aud in ("lab-rt", "rerun"):                                          # 服务端库层同样拒绝（令牌消费方不依赖 BN-04 的角色）
        with pytest.raises(AuthError) as ei:
            env.auth.sign_short(p, aud)
        assert (ei.value.code, ei.value.detail) == (115, "invited_scope")
    forged = Principal("ann", "invited", ("team-a",), "token", env.ids["ann"])
    assert env.auth.verify_short(env.auth.sign_short(forged, "frames")[0], "frames").role == "invited"
    for path in ("/runs/run-x/truth/frames/uav-01/cam0/0", "/runs/run-x/truth/event/tape"):
        before = len(env.auth.audit.read())
        r = c.get(P + path, headers=env.h("ann"))
        assert r.status_code == 403 and r.json()["code"] == 115 and r.json()["detail"]["required"] == "evaluator"
        assert [(a["code"], a["why"], a["principal"]) for a in env.deny(before)] == [(115, "role", "ann")]


def test_invited_cannot_touch_other_teams_submissions(env: Env) -> None:
    c = env.client
    sb = c.post(f"{P}/submissions", json=REQ, headers=env.h("bob")).json()["submission_id"]
    calls = [("GET", f"/submissions/{sb}", None), ("DELETE", f"/submissions/{sb}", None), ("POST", f"/submissions/{sb}/visibility", {"visibility": "board"})]
    for m, path, body in calls:
        before = len(env.auth.audit.read())
        r = c.request(m, P + path, headers=env.h("ann"), json=body)
        assert r.status_code == 403 and r.json()["code"] == 115 and r.json()["detail"]["why"] == "invited_scope", (m, path)
        assert [(a["code"], a["why"], a["principal"], a["ids"]) for a in env.deny(before)] == [(115, "invited_scope", "ann", {"sid": sb})]
    assert c.get(f"{P}/submissions", headers=env.h("ann")).json()["items"] == []
    assert env.store().get(sb)["state"] == "RECEIVED" and env.store().get(sb)["visibility"] == "private"


def test_registry_scope_by_team_namespace(env: Env) -> None:
    auth = env.auth
    tok = {n: env.client.post(f"{P}/registry/token", json={}, headers=env.h(n)).json()["token"] for n in ("ann", "rae", "eve", "root")}
    ok = [("ann", "GET", "/v2/"), ("ann", "POST", "/v2/team-a/agent/blobs/uploads/"), ("ann", "PATCH", "/v2/team-a/agent/blobs/uploads/abc-123"),
          ("ann", "PUT", "/v2/team-a/agent/manifests/v1?x=1"), ("ann", "HEAD", "/v2/team-a/agent/blobs/sha256:" + "a" * 64),
          ("ann", "GET", "/v2/team-a/agent/tags/list"), ("rae", "PUT", "/v2/d3/agent/manifests/v2"), ("eve", "GET", "/v2/team-a/agent/manifests/v1"),
          ("root", "DELETE", "/v2/team-a/agent/manifests/sha256:" + "a" * 64), ("root", "GET", "/v2/_catalog")]
    for n, m, path in ok:
        assert auth.authorize_registry(tok[n], m, path).name == n, (n, m, path)
    bad = [("ann", "GET", "/v2/_catalog", "invited_scope"), ("ann", "GET", "/v2/team-b/agent/manifests/v1", "invited_scope"),
           ("ann", "PUT", "/v2/team-b/agent/manifests/v1", "invited_scope"), ("ann", "PUT", "/v2/team-a-x/agent/manifests/v1", "invited_scope"),
           ("ann", "PUT", "/v2/team-a/../team-b/agent/manifests/v1", "invited_scope"), ("ann", "DELETE", "/v2/team-a/agent/manifests/v1", "invited_scope"),
           ("rae", "GET", "/v2/team-a/agent/manifests/v1", "project_scope"), ("eve", "PUT", "/v2/team-a/agent/manifests/v1", "project_scope")]
    for n, m, path, why in bad:
        before = len(auth.audit.read())
        with pytest.raises(AuthError) as ei:
            auth.authorize_registry(tok[n], m, path)
        assert (ei.value.code, ei.value.detail) == (115, why), (n, m, path)
        d = env.deny(before)
        assert [(a["code"], a["why"], a["principal"], a["route"], a["path"]) for a in d] == [(115, why, n, "registry", path.split("?")[0])]
    pushes = [a for a in auth.audit.read() if a["action"] == "registry_push"]
    assert [(a["principal"], a["repository"], a["reference"]) for a in pushes] == [("ann", "team-a/agent", "v1"), ("rae", "d3/agent", "v2")]
    with pytest.raises(AuthError) as ei:                                    # bench token 与其他用途的短期令牌不能当推送令牌
        auth.authorize_registry(auth.sign_short(auth.verify_token(env.tokens["rae"]), "frames")[0], "GET", "/v2/")
    assert ei.value.code == 302
    assert not [a for a in auth.audit.read() if env.tokens["ann"] in json.dumps(a) or tok["ann"] in json.dumps(a)]   # 审计不含令牌


def test_results_visible_only_to_submitter_team_by_default(env: Env) -> None:
    c = env.client
    sa = c.post(f"{P}/submissions", json=REQ, headers=env.h("ann")).json()["submission_id"]
    st = env.store()
    for s in ("SCANNING", "VALIDATING", "EVALUATING", "SCORING"):
        st.transition(sa, s)
    st.transition(sa, "DONE", result={"lanes": [{"lane": "rendezvous_handoff", "metrics": {}, "runs": {"total": 1, "valid": 1, "invalid": 0,
                                                                                                        "platform_rerun": 0}, "status": "valid", "composite": None}]})
    assert "result" in c.get(f"{P}/submissions/{sa}", headers=env.h("ann")).json()                      # 提交者团队
    assert "result" not in c.get(f"{P}/submissions/{sa}", headers=env.h("rae")).json()                  # 团队以外的研究员：private 不返回结果
    assert "result" not in next(x for x in c.get(f"{P}/submissions", headers=env.h("rae")).json()["items"] if x["submission_id"] == sa)
    assert "result" in c.get(f"{P}/submissions/{sa}", headers=env.h("eve")).json()                      # 评测员及以上不受限
    assert c.get(f"{P}/submissions/{sa}", headers=env.h("bob")).status_code == 403
    assert c.post(f"{P}/submissions/{sa}/visibility", json={"visibility": "board"}, headers=env.h("ann")).status_code == 200
    assert "result" in c.get(f"{P}/submissions/{sa}", headers=env.h("rae")).json()                      # 进入内部榜后可见


def test_push_token_basic_auth_shape(env: Env) -> None:
    """推送令牌按 `docker login -u <principal> --password-stdin` 使用：前置代理把 Basic 凭据的密码部分交给 authorize_registry。"""
    r = env.client.post(f"{P}/registry/token", json={}, headers=env.h("ann")).json()
    basic = base64.b64encode(f"{r['username']}:{r['token']}".encode()).decode()
    pw = base64.b64decode(basic).decode().split(":", 1)[1]
    assert env.auth.authorize_registry(pw, "GET", "/v2/team-a/agent/tags/list").team == "team-a"
    assert int(r["exp_unix_ns"]) // 1_000_000_000 - int(time.time()) in range(595, 601)               # 10 min
