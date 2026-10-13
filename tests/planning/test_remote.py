"""PlanPoolClient remote 模式（M10-FR-202、FR-212、NFR-202；M10-AC-202 的客户端部分、AC-209）。

sim-core 侧客户端与 sandbox-pool 侧 `PlanServer` 经同一会话命名空间的总线（LocalBus，与 ZenohBus 同一语义）连接：
服务端 `attach_bus(sid, bus)` 声明 `ctl/plan/submit` 与 `ctl/plan/cancel`，客户端以相对 key 调用。断言：结果只在
`drain(tick)` 生效并把 apply_tick 与 `result_sha256` 写入输入日志；同键取代后旧结果不生效；> 1 MB 结果经文件外置、
sha256 校验后删除；结果哈希与 inline 模式逐字节相同；服务端超时、无服务方与配额拒绝各自映射为 125、213、585。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest
from geo import tiny

from awr.runtime.bus import LocalBus, local_net
from awr.sim.planning.jobs import PlanRequest, PlanResult
from awr.sim.planning.pool import PlanPoolClient
from awr.sim.planning.server import PlanServer

pytestmark = pytest.mark.usefixtures("plan_plugins")


class Log:
    def __init__(self) -> None:
        self.items: list = []

    def append(self, kind, tick, payload) -> None:
        self.items.append((kind, tick, payload))


def _rq(jid: str, kind: str = "test.echo", key: str | None = None, prio: int = 1, budget: int = 200, world_key=None,
        **payload) -> PlanRequest:
    return PlanRequest(jid, kind, world_key, (), payload, {}, prio, 0, budget, key or jid)  # type: ignore[arg-type]


def _drain_until(pool: PlanPoolClient, pred, timeout_s: float = 30.0, tick0: int = 0) -> int:
    t_end = time.monotonic() + timeout_s
    tick = tick0
    while time.monotonic() < t_end:
        tick += 1
        pool.drain(tick)
        if pred():
            return tick
        time.sleep(0.005)
    raise AssertionError("timeout waiting for plan results")


@pytest.fixture
def env(tmp_path, request):
    """(server, client 工厂, sid, shm)：会话命名空间唯一，避免与其他用例的 LocalNet 串扰。"""
    sid = "sb-" + request.node.name.replace("_", "-")[-20:].strip("-").lower()
    ns = f"awr/tiny/{sid}"
    shm = tmp_path / "shm"
    w = tiny(str(tmp_path))
    srv = PlanServer(workers=1, worlds_dir=tmp_path, allow_derive=False, shm_root=shm, nice=0)
    assert srv.wait_ready(60)
    pool_bus = LocalBus.open("sandbox-pool", net=local_net(ns), announce=False)
    srv.attach_bus(sid, pool_bus)
    clients: list = []

    def client(**kw) -> PlanPoolClient:
        bus = LocalBus.open("sim-core", net=local_net(ns), announce=False)
        c = PlanPoolClient((w.world_id, w.content_version, w.coordinate_sha256), mode="remote", bus=bus,
                           result_dir=shm / sid / "plan", **kw)
        clients.append((c, bus))
        return c

    yield srv, client, sid, shm, w
    for c, b in clients:
        c.close()
        b.close()
    srv.close()
    pool_bus.close()


def test_remote_applies_at_drain_and_logs(env) -> None:
    srv, client, sid, _shm, w = env
    lg = Log()
    pool = client(inputlog=lg)
    assert pool.mode == "remote"
    key = (w.world_id, w.content_version, w.coordinate_sha256)
    got: list = []
    req = PlanRequest("t1", "safe_transit", key, ("v",), {"start": [-120.0, -20.0, 30.0], "goal": [30.0, -20.0, 60.0]},
                      {"v_max_mps": 12.0}, 0, 5, 50, "slot:1")
    pool.submit(req, lambda r, t: got.append((r, t)))
    assert got == []                                                             # 只在 drain（步边界）生效
    tick = _drain_until(pool, lambda: bool(got), 30.0, 100)
    r, t = got[0]
    assert r.ok and t == tick and r.trajectories[0]["duration_s"] > 0
    assert lg.items == [("plan_result", tick, {"job_id": "t1", "kind": "safe_transit", "status": r.status,
                                               "sha256": r.result_sha256, "request_tick": 5})]
    # 与 inline 模式逐字节相同（NFR-202）
    inl = PlanPoolClient(key, mode="inline", world=w)
    got2: list = []
    inl.submit(req, lambda r, t: got2.append(r))
    inl.drain(1)
    assert got2[0].result_sha256 == r.result_sha256
    np.testing.assert_array_equal(got2[0].trajectories[0]["ctrl_pts"], r.trajectories[0]["ctrl_pts"])
    assert srv.stats()["sessions"][sid]["done"] == 1


def test_remote_supersede_old_result_not_applied(env) -> None:
    _srv, client, _sid, _shm, _w = env
    pool = client()
    got: list = []
    pool.submit(_rq("blk", "test.long", budget=3000, sleep_s=1.5), lambda r, t: got.append(r.job_id))
    pool.submit(_rq("old", key="same", x=1), lambda r, t: got.append(r.job_id))
    pool.submit(_rq("new", key="same", x=2), lambda r, t: got.append((r.job_id, r.extra.get("echo"))))
    _drain_until(pool, lambda: len(got) == 2, 30.0)
    time.sleep(0.2)
    pool.drain(10_000)
    assert got == ["blk", ("new", 2)] and pool.stats.superseded == 1 and pool.pending() == 0


def test_remote_supersede_in_flight_sends_cancel(env) -> None:
    srv, client, sid, _shm, _w = env
    pool = client(remote_inflight=4)
    got: list = []
    pool.submit(_rq("blk", "test.long", budget=3000, sleep_s=1.5), lambda r, t: got.append(r.job_id))
    pool.submit(_rq("old", key="k", x=1), lambda r, t: got.append(r.job_id))        # 已发往服务端、排队中
    time.sleep(0.05)
    pool.cancel("k")
    _drain_until(pool, lambda: got == ["blk"] and not pool._inflight, 30.0)
    assert srv.stats()["sessions"][sid]["canceled"] + srv.stats()["sessions"][sid]["superseded"] == 1


def test_remote_spill_file_verified_and_deleted(env) -> None:
    srv, client, sid, shm, _w = env
    pool = client()
    got: list = []
    pool.submit(_rq("big", "test.big", budget=2000, n=200_000), lambda r, t: got.append(r))
    _drain_until(pool, lambda: bool(got), 30.0)
    r = got[0]
    assert r.ok and np.array_equal(r.extra["blob"], np.arange(200_000, dtype=np.float64))
    assert srv.stats()["result_files"] == 1
    assert not list((shm / sid / "plan").glob("*.msgpack"))                     # 读取即删除


def test_remote_server_timeout_maps_to_125(env) -> None:
    _srv, client, _sid, _shm, _w = env
    pool = client()
    got: list = []
    pool.submit(_rq("slow", "test.sleep", budget=100, sleep_s=1.0), lambda r, t: got.append(r))
    _drain_until(pool, lambda: bool(got), 30.0)
    assert got[0].status == "timeout" and got[0].code == 125 and got[0].detail == "PLAN_TIMEOUT"


def test_remote_quota_rejected_before_submit(env) -> None:
    srv, client, sid, _shm, _w = env
    pool = client()
    got: list = []
    pool.submit(_rq("q", "test.quota", measured={"stripes": 201}), lambda r, t: got.append((r, t)))
    pool.drain(7)
    r, t = got[0]
    assert t == 7 and r.code == 585 and r.detail == "stripes=201>200" and r.stats["quota"]["stripes"]["limit"] == 200
    assert sid not in srv.stats()["sessions"]                                    # 未提交到服务端
    assert pool.stats.rejected == 1


def test_remote_no_service_maps_to_213(tmp_path) -> None:
    bus = LocalBus.open("sim-core", net=local_net("awr/tiny/sb-noservice"), announce=False)
    pool = PlanPoolClient(None, mode="remote", bus=bus, result_dir=tmp_path)
    got: list = []
    try:
        pool.submit(_rq("x"), lambda r, t: got.append(r))
        _drain_until(pool, lambda: bool(got), 10.0)
        assert got[0].code == 213 and got[0].detail == "PLAN_SERVICE_UNAVAILABLE"
    finally:
        pool.close()
        bus.close()


def test_remote_without_bus_falls_back_to_process() -> None:
    pool = PlanPoolClient(None, mode="remote")
    try:
        assert pool.mode == "process" and pool.snapshot()["requested"] == "remote"
    finally:
        pool.close()


class _FakeBus:
    """伪造回复：校验客户端对越界路径、sha256 不符的处理。"""

    def __init__(self, reply) -> None:
        self.reply = reply
        self.calls: list = []

    def call_cb(self, key, msg, on_reply, *, timeout=1.0, retries=2, retry_gap=0.3) -> None:
        self.calls.append((key, msg, timeout, retries))
        if key.endswith("submit"):
            on_reply(self.reply(msg), None)


def test_remote_rejects_tampered_replies(tmp_path) -> None:
    rd = tmp_path / "plan"
    rd.mkdir()
    outside = tmp_path / "evil.msgpack"
    good = PlanResult("x", "ok", (), None, {}, None, None, "s" * 64).to_bytes()
    outside.write_bytes(good)
    inside = rd / "x.msgpack"
    cases = [
        (lambda m: {"v": 1, "job_id": "x", "status": "ok", "code": 0, "result_path": str(outside),
                    "result_bytes_sha256": "0"}, "outside"),
        (lambda m: (inside.write_bytes(good), {"v": 1, "job_id": "x", "status": "ok", "code": 0,
                                               "result_path": str(inside), "result_bytes_sha256": "0" * 64})[1], "sha"),
        (lambda m: {"v": 1, "job_id": "other", "status": "ok", "code": 0, "result": good}, "job"),
        (lambda m: {"v": 1, "job_id": "x", "status": "ok", "code": 0, "result": good, "result_sha256": "f" * 64}, "digest"),
    ]
    for reply, what in cases:
        bus = _FakeBus(reply)
        pool = PlanPoolClient(None, mode="remote", bus=bus, result_dir=rd)
        got: list = []
        pool.submit(_rq("x", budget=200), lambda r, t, got=got: got.append(r))
        _drain_until(pool, lambda got=got: bool(got), 5.0)
        assert got[0].code == 320 and "RemoteReplyError" in got[0].detail, (what, got[0])
        assert bus.calls[0][2] == pytest.approx(5.6) and bus.calls[0][3] == 0     # query 超时 = 预算 × 3 + 5 s，不重试
        pool.close()
    assert outside.exists() and not inside.exists()                              # 越界路径不读不删；目录内文件读后删除


def test_remote_inflight_cap_keeps_local_priority(tmp_path) -> None:
    sent: list = []

    class Hold:
        def call_cb(self, key, msg, on_reply, **kw) -> None:
            if key.endswith("submit"):
                sent.append(msg["job_id"])

    pool = PlanPoolClient(None, mode="remote", bus=Hold(), result_dir=tmp_path, remote_inflight=2)
    for jid, prio in (("a", 3), ("b", 3), ("c", 3), ("d", 0)):
        pool.submit(_rq(jid, prio=prio), lambda r, t: None)
    assert sent == ["a", "b"] and pool.snapshot()["inflight"] == 2
    pool.close()


@pytest.mark.slow
def test_long_query_survives_bus_request_sweeper(env) -> None:
    """规划 query 可持续 预算 × 3 + 5 s：服务端把请求移出总线的 5 s 未回复清扫（期间另有请求到达触发清扫）。"""
    _srv, client, _sid, _shm, _w = env
    pool = client()
    got: list = []
    pool.submit(_rq("long", "test.long", budget=3000, sleep_s=5.6), lambda r, t: got.append(r))
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5.3:
        pool.drain(1)
        time.sleep(0.05)
    pool.submit(_rq("poke", x=1), lambda r, t: got.append(r))                   # 触发服务端总线的清扫
    _drain_until(pool, lambda: len(got) == 2, 20.0)
    by = {r.job_id: r for r in got}
    assert by["long"].ok and by["poke"].ok, [(r.job_id, r.status, r.detail) for r in got]


def test_runtime_passes_session_bus(monkeypatch) -> None:
    """M10 任务运行时把 sim-core 的会话总线交给 PlanPoolClient（remote 模式所需）。"""
    import inspect

    from awr.sim.mission import runtime as RT

    src = inspect.getsource(RT)
    assert 'bus=getattr(self.events, "bus", None)' in src and "bus=self.pool.bus" in src
    assert os.environ.get("AWR_PLAN_POOL") == "inline"                           # 本目录用例缺省 inline（conftest）
    assert Path(RT.__file__).name == "runtime.py"


@pytest.mark.parametrize("absolute", [False, True])
def test_remote_over_zenoh(tmp_path, absolute: bool) -> None:
    """真实 zenoh 会话：池以会话命名空间的总线（相对 key）或无前缀会话（绝对 key，17 §17.10.1）声明 queryable；
    sim-core 侧以相对 key 调用。"""
    import socket
    import threading

    from awr.contracts import bus_keys
    from awr.runtime.bus import ZenohBus

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    ep = f"tcp/127.0.0.1:{s.getsockname()[1]}"
    s.close()
    sid = "sb-zenoh" + ("abs" if absolute else "rel")
    ns = f"awr/tiny/{sid}"
    shm = tmp_path / "shm"
    srv = PlanServer(workers=1, worlds_dir=None, allow_derive=False, shm_root=shm, nice=0)
    if absolute:                                    # 无前缀会话（模板缺省 namespace 置空），以绝对 key 声明
        from awr.runtime import bus as B

        cfg = ZenohBus.build_config(listen=[ep], connect=[])
        cfg.insert_json5("namespace", "null")
        pool_bus = ZenohBus("sandbox-pool", B.zenoh.open(cfg), "", None)
    else:
        pool_bus = ZenohBus.open("sandbox-pool", namespace=ns, listen=[ep], connect=[], announce=False)
    sim_bus = ZenohBus.open("sim-core", namespace=ns, listen=["tcp/127.0.0.1:0"], connect=[ep], announce=False)
    pool = None
    try:
        assert srv.wait_ready(60)
        srv.attach_bus(sid, pool_bus, key_prefix=ns if absolute else None)
        t_end = time.monotonic() + 15
        ok = threading.Event()
        while not ok.is_set() and time.monotonic() < t_end:                   # 等待两个 peer 建立连接
            sim_bus.call_cb(bus_keys.ctl_plan("cancel"), {"v": 1, "job_id": "probe"},
                            lambda rep, err: ok.set() if err is None else None, timeout=0.5, retries=0)
            ok.wait(0.6)
        assert ok.is_set()
        pool = PlanPoolClient(None, mode="remote", bus=sim_bus, result_dir=shm / sid / "plan")
        got: list = []
        pool.submit(_rq("e", x=5), lambda r, t: got.append(r))
        pool.submit(_rq("big", "test.big", budget=2000, n=200_000), lambda r, t: got.append(r))
        _drain_until(pool, lambda: len(got) == 2, 30.0)
        by = {r.job_id: r for r in got}
        assert by["e"].extra["echo"] == 5 and by["big"].ok and by["big"].extra["blob"].shape == (200_000,)
        assert not list((shm / sid / "plan").glob("*.msgpack"))
    finally:
        if pool is not None:
            pool.close()
        srv.close()
        sim_bus.close()
        pool_bus.close()
