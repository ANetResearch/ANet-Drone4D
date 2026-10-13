"""共享规划服务端核心 `PlanServer`（M10-FR-203、FR-212；M10-AC-202、AC-203、AC-209 的服务端部分；M17-AC-017 的 M10 侧）。

worker 为真实子进程（`python -c` + socketpair）；测试作业来自插件 `plan_test_kinds`（经 AWR_PLAN_PLUGINS 与 PYTHONPATH
在 worker 中登记）。计时为墙钟：强杀判据为 3 × 预算 ± 100 ms（AC-203），本机负载高时放宽到 +250 ms 上限并记录实测。
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from awr.sim.planning.jobs import PlanRequest, PlanResult, packb
from awr.sim.planning.server import PlanServer

pytestmark = pytest.mark.usefixtures("plan_plugins")


def _rq(jid: str, kind: str = "test.echo", key: str | None = None, prio: int = 1, budget: int = 200, **payload) -> PlanRequest:
    return PlanRequest(jid, kind, None, (), payload, {}, prio, 0, budget, key or jid)  # type: ignore[arg-type]


@pytest.fixture
def server(tmp_path):
    srv = PlanServer(workers=1, worlds_dir=None, allow_derive=False, shm_root=tmp_path / "shm", nice=0)
    assert srv.wait_ready(60), srv.stats()
    yield srv
    srv.close()


def test_basic_reply_shape(server) -> None:
    t = server.submit("sb-a", _rq("e1", x=7))
    r = t.wait(20)
    assert r is not None and r["status"] == "ok" and r["code"] == 0 and r["v"] == 1 and r["job_id"] == "e1"
    res = PlanResult.from_bytes(r["result"])
    assert res.extra["echo"] == 7 and res.result_sha256 == r["result_sha256"]
    assert t.done and t.status == "ok"


def test_round_robin_fairness_four_sessions(server) -> None:
    """AC-202：4 个会话各持续提交 200 个作业（每会话保持 8 个在途与排队）；在全部会话都有积压的区间内，每会话完成数
    占比在 25% ± 5% 内（W = 1，按会话轮转）。"""
    sids = ["sb-a", "sb-b", "sb-c", "sb-d"]
    order: list[str] = []
    lock = threading.Lock()
    sent = Counter()
    done_ev = threading.Event()

    def submit(sid: str) -> None:
        with lock:
            if sent[sid] >= 200:
                return
            sent[sid] += 1
            n = sent[sid]
        server.submit(sid, _rq(f"{sid}:{n}", key=f"k{n}", x=n), lambda rep, sid=sid: on_done(sid, rep))

    def on_done(sid: str, rep: dict) -> None:
        assert rep["status"] == "ok", rep
        with lock:
            order.append(sid)
            if len(order) == 800:
                done_ev.set()
        submit(sid)

    for sid in sids:
        for _ in range(8):
            submit(sid)
    assert done_ev.wait(180), (len(order), server.stats())
    # 每会话仍有积压的区间：最先提交完 200 个的会话完成最后一个作业之前
    first_drained = min(max(i for i, s in enumerate(order) if s == sid) for sid in sids)
    window = Counter(order[: first_drained + 1])
    n = sum(window.values())
    shares = {sid: window[sid] / n for sid in sids}
    assert all(0.20 <= v <= 0.30 for v in shares.values()), shares
    st = server.stats()["sessions"]
    assert all(st[sid]["done"] == 200 and st[sid]["rejected"] == 0 for sid in sids)


def test_dedupe_supersedes_and_queue_limit(server) -> None:
    blocker = server.submit("sb-a", _rq("blk", "test.long", budget=3000, sleep_s=1.5))
    time.sleep(0.1)
    old = server.submit("sb-a", _rq("j1", key="same", x=1))
    new = server.submit("sb-a", _rq("j2", key="same", x=2))
    assert old.wait(5)["status"] == "superseded"
    tickets = [server.submit("sb-a", _rq(f"q{i}", x=i)) for i in range(6)]          # 在途 1 + j2 + 6 = 8
    over = server.submit("sb-a", _rq("q-over"))
    r = over.wait(5)
    assert r["status"] == "error" and r["code"] == 111
    other = server.submit("sb-b", _rq("b1", x=9))                                    # 上限按会话计
    for t in [blocker, new, other, *tickets]:
        assert t.wait(30)["status"] == "ok", t
    assert PlanResult.from_bytes(new.reply["result"]).extra["echo"] == 2
    st = server.stats()["sessions"]["sb-a"]
    assert st["superseded"] == 1 and st["rejected"] == 1


def test_priority_within_session(server) -> None:
    blocker = server.submit("sb-a", _rq("blk", "test.long", budget=3000, sleep_s=1.5))
    time.sleep(0.1)
    seen: list[str] = []
    for jid, prio in (("bg", 3), ("start", 1), ("ui", 0)):
        server.submit("sb-a", _rq(jid, prio=prio), lambda rep: seen.append(rep["job_id"]))
    assert blocker.wait(10)["status"] == "ok"
    t_end = time.monotonic() + 20
    while len(seen) < 3 and time.monotonic() < t_end:
        time.sleep(0.01)
    assert seen == ["ui", "start", "bg"]


def test_kill_and_throttle(server) -> None:
    """AC-203（功能部分）：超过 3 × 预算的作业被强杀并回复 125，worker 重建后后续作业正常；同会话第 3 次超时后提交回复
    586（retry_after ≈ 300 s），其他会话不受影响。强杀时刻 ± 100 ms 与重建 ≤ 5 s 的计时判据见 perf 用例
    `test_kill_precision_and_rebuild_time`（时间阈值按 18 §8.2 标 perf）。"""
    for i in range(3):
        t0 = time.monotonic()
        t = server.submit("sb-x", _rq(f"slow{i}", "test.sleep", budget=100, sleep_s=5.0))
        r = t.wait(30)
        dt = time.monotonic() - t0
        assert r["status"] == "timeout" and r["code"] == 125 and r["detail"] == "PLAN_TIMEOUT", r
        assert 0.25 <= dt < 5.0, dt                                                   # 未提前、也未等到作业自然结束
        assert server.wait_ready(60)
        if i < 2:
            assert server.submit("sb-x", _rq(f"ok{i}")).wait(30)["status"] == "ok"   # 后续作业正常
    r = server.submit("sb-x", _rq("after-freeze")).wait(5)
    assert r["code"] == 586 and r["detail"] == "PLAN_THROTTLED" and 290 <= r["retry_after_s"] <= 300
    assert server.submit("sb-y", _rq("other")).wait(30)["status"] == "ok"           # 其他会话不受影响
    st = server.stats()
    assert st["kills"] == 3 and st["sessions"]["sb-x"]["freezes"] == 1 and st["sessions"]["sb-x"]["frozen_s"] > 280


@pytest.mark.perf
def test_kill_precision_and_rebuild_time(server) -> None:
    """AC-203 计时判据：worker 在 3 × 预算 ± 100 ms 被 kill（回复往返计入），重建 ≤ 5 s。"""
    lat = []
    for i in range(3):
        t0 = time.monotonic()
        r = server.submit(f"sb-p{i}", _rq(f"slow{i}", "test.sleep", budget=100, sleep_s=1.0)).wait(20)
        lat.append(time.monotonic() - t0)
        assert r["code"] == 125
        assert server.wait_ready(10)
        assert server.stats()["rebuild_ms_last"] <= 5000
    assert all(0.2 <= dt <= 0.4 for dt in lat), lat


def test_freeze_expires_and_flushes_queue(tmp_path) -> None:
    srv = PlanServer(workers=1, worlds_dir=None, allow_derive=False, shm_root=tmp_path, nice=0, freeze_count=2,
                     freeze_s=1.0)
    try:
        assert srv.wait_ready(60)
        srv.submit("sb-x", _rq("s0", "test.sleep", budget=50, sleep_s=1.0)).wait(20)
        assert srv.wait_ready(10)
        s1 = srv.submit("sb-x", _rq("s1", "test.sleep", budget=50, sleep_s=1.0))
        queued = srv.submit("sb-x", _rq("q"))                                       # 冻结时排队中的作业回复 586
        assert s1.wait(20)["code"] == 125 and queued.wait(20)["code"] == 586
        assert srv.submit("sb-x", _rq("in-freeze")).wait(5)["code"] == 586
        time.sleep(1.1)
        assert srv.wait_ready(10)
        assert srv.submit("sb-x", _rq("after")).wait(20)["status"] == "ok"
    finally:
        srv.close()


def test_crash_retry_then_214(server, tmp_path) -> None:
    r = server.submit("sb-a", _rq("c1", "test.crash", budget=500, once=str(tmp_path / "mark"))).wait(30)
    assert r["status"] == "ok" and PlanResult.from_bytes(r["result"]).extra["crashed"] is False   # 首次崩溃后重交成功
    r = server.submit("sb-a", _rq("c2", "test.crash", budget=500)).wait(30)
    assert r["status"] == "error" and r["code"] == 214 and r["detail"] == "PLANNER_CRASHED"
    assert server.submit("sb-a", _rq("after")).wait(30)["status"] == "ok"
    st = server.stats()
    assert st["crashes"] == 3 and st["sessions"]["sb-a"]["crashes"] == 3


def test_cancel_and_drop_session(server, tmp_path) -> None:
    blocker = server.submit("sb-a", _rq("blk", "test.long", budget=3000, sleep_s=1.5))
    time.sleep(0.1)
    q1 = server.submit("sb-a", _rq("q1"))
    q2 = server.submit("sb-a", _rq("q2"))
    assert server.cancel("sb-a", "q1") and not server.cancel("sb-a", "q1")          # 幂等
    assert q1.wait(5)["status"] == "canceled"
    server.drop_session("sb-a")
    assert q2.wait(5)["status"] == "canceled" and q2.reply["detail"] == "SESSION_ENDED"
    assert blocker.wait(5)["status"] == "canceled"                                  # 在途作业的结果丢弃
    assert "sb-a" not in server.stats()["sessions"]
    assert server.submit("sb-b", _rq("b")).wait(30)["status"] == "ok"


def test_result_spill_over_1mb(server, tmp_path) -> None:
    n = 200_000                                                                      # 1.6 MB
    r = server.submit("sb-a", _rq("big", "test.big", budget=2000, n=n)).wait(30)
    assert r["status"] == "ok" and "result" not in r
    p = r["result_path"]
    assert p.startswith(str(tmp_path / "shm" / "sb-a" / "plan")) and os.path.exists(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    import hashlib

    raw = Path(p).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == r["result_bytes_sha256"]
    res = PlanResult.from_bytes(raw)
    assert res.result_sha256 == r["result_sha256"] and np.array_equal(res.extra["blob"], np.arange(n, dtype=np.float64))
    small = server.submit("sb-a", _rq("small", "test.big", budget=2000, n=1000)).wait(30)
    assert "result" in small and "result_path" not in small
    server.drop_session("sb-a")                                                      # 会话结束删除外置文件
    assert not os.path.exists(p)


def test_admission_rejections(server) -> None:
    r = server.submit("sb-a", _rq("u", "no.such")).wait(5)
    assert r["code"] == 300 and r["detail"] == "UNKNOWN_KIND"
    r = server.submit("sb-a", _rq("q", "test.quota", measured={"vertices": 65, "area_m2": 2e6})).wait(5)
    assert r["code"] == 585 and r["detail"] == "area_m2=2000000>1000000; vertices=65>64"
    assert r["quota"] == {"area_m2": {"value": 2e6, "limit": 1_000_000.0}, "vertices": {"value": 65, "limit": 64}}
    r = server.submit("sb-a", _rq("q2", "test.quota", measured={"vertices": 64, "area_m2": 1e6})).wait(30)
    assert r["status"] == "ok"
    with pytest.raises(ValueError):
        server.submit("../x", _rq("bad-sid"))


def test_wire_submit_and_cancel_paths(server) -> None:
    got: dict = {}
    ev = threading.Event()

    def reply(m: dict) -> None:
        got[m["job_id"]] = m
        if len(got) >= 3:
            ev.set()

    import msgpack

    server.submit_wire("sb-w", msgpack.packb(_rq("w1", x=3).to_wire(), use_bin_type=True), reply)
    server.submit_wire("sb-w", {"job_id": "w-bad", "kind": "test.echo", "budget_ms": 100, "args": b"\xc1"}, reply)
    server.submit_wire("sb-w", {"job_id": "w-q", "kind": "test.quota", "budget_ms": 100,
                                "args": packb({"payload": {"measured": {"keepouts": 65}}})}, reply)
    assert ev.wait(30), got
    assert got["w1"]["status"] == "ok" and PlanResult.from_bytes(got["w1"]["result"]).extra["echo"] == 3
    assert got["w-bad"]["code"] == 300 and got["w-q"]["code"] == 585
    c: list = []
    server.cancel_wire("sb-w", {"v": 1, "job_id": "nope"}, c.append)
    t_end = time.monotonic() + 5
    while not c and time.monotonic() < t_end:
        time.sleep(0.01)
    assert c and c[0]["status"] == "unknown"


def test_queue_deadline_expires(server) -> None:
    blocker = server.submit("sb-a", _rq("blk", "test.long", budget=3000, sleep_s=1.5))
    time.sleep(0.05)
    t = server.submit("sb-b", _rq("late"), deadline_s=time.monotonic() + 0.1)
    r = t.wait(10)
    assert r["status"] == "timeout" and r["detail"] == "QUEUE_EXPIRED"
    assert blocker.wait(10)["status"] == "ok"


@pytest.mark.slow
def test_two_workers_run_sessions_in_parallel(tmp_path) -> None:
    srv = PlanServer(workers=2, worlds_dir=None, allow_derive=False, shm_root=tmp_path, nice=0)
    try:
        assert srv.wait_ready(60)
        assert len(set(srv.worker_pids())) == 2
        t0 = time.monotonic()
        a = srv.submit("sb-a", _rq("a", "test.long", budget=3000, sleep_s=0.8))
        b = srv.submit("sb-b", _rq("b", "test.long", budget=3000, sleep_s=0.8))
        assert a.wait(30)["status"] == "ok" and b.wait(30)["status"] == "ok"
        assert time.monotonic() - t0 >= 0.8
        assert sorted(w["jobs"] for w in srv.stats()["workers"]) == [1, 1]        # 两个会话分别在两个 worker 上执行
    finally:
        srv.close()


def test_world_load_timeout_is_service_side(tmp_path, monkeypatch) -> None:
    """世界装载阶段超时（上限缩短）：回复 123 WORLD_LOAD_TIMEOUT，不计入会话的超时次数（不会因此冻结）。"""
    from awr.sim.planning import server as S

    monkeypatch.setenv("AWR_PLAN_TEST_HOOKS", "1")
    monkeypatch.setattr(S, "WORLD_LOAD_LIMIT_S", 0.3)
    srv = PlanServer(workers=1, worlds_dir=None, allow_derive=False, shm_root=tmp_path, nice=0, freeze_count=1)
    try:
        assert srv.wait_ready(60)
        r = srv.submit("sb-l", _rq("slowload", _test_load_sleep_s=1.0)).wait(20)
        assert r["code"] == 123 and r["detail"] == "WORLD_LOAD_TIMEOUT"
        assert srv.wait_ready(10)
        assert srv.submit("sb-l", _rq("after")).wait(20)["status"] == "ok"           # 未冻结（freeze_count = 1）
        st = srv.stats()
        assert st["sessions"]["sb-l"]["timeouts"] == 0 and st["kills"] == 1 and st["timeouts"] == 0
    finally:
        srv.close()
