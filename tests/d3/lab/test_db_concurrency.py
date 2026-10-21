"""bench-api 共用 SQLite 连接的并发安全（K12；D3-AC-51 的会话与权限矩阵在部署形态下的稳定性）。

dmax 部署复测发现：`BenchAuth` 的会话表连接被线程池中的多个请求线程同时使用时，`resolve_session` 偶发
`sqlite3.InterfaceError: bad parameter or other API misuse`（GET /events 500、GET /runs 401；F09 记录的 lab-roles 间歇性 401 同源）。
修复后（`awr.bench.dbconn.SerialConnection`）：

1. `Rows` 与 `SerialConnection` 的游标语义（fetchone、fetchall、迭代、rowcount、row_factory）；
2. 16 个线程并发解析会话、登录与注销 4 s：0 个异常，每次解析得到本线程会话的主体；
3. `RunStore` 并发入队、迁移与列表读取：0 个异常，读不到写事务中途的状态，最终计数一致；
4. 经 bench-api 的 TestClient 并发请求（同一会话 Cookie）：全部 200，无 401 与 500。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from awr.bench.auth import BenchAuth
from awr.bench.dbconn import Rows, SerialConnection
from awr.bench.runs_store import RunStore

DURATION_S = 4.0


def _hammer(n: int, fn) -> list[BaseException]:
    """n 个线程在 DURATION_S 内反复调用 fn(i, k)，返回收集到的异常"""
    errs: list[BaseException] = []
    stop = time.monotonic() + DURATION_S
    start = threading.Barrier(n)

    def work(i: int) -> None:
        start.wait()
        k = 0
        while time.monotonic() < stop:
            try:
                fn(i, k)
            except BaseException as e:      # 收集全部异常类型（InterfaceError、AssertionError 等）
                errs.append(e)
                return
            k += 1

    with ThreadPoolExecutor(n) as ex:
        list(ex.map(work, range(n)))
    return errs


def test_serial_connection_cursor_semantics(tmp_path: Path) -> None:
    db = SerialConnection(sqlite3.connect(str(tmp_path / "x.sqlite"), check_same_thread=False, isolation_level=None))
    db.row_factory = sqlite3.Row
    assert db.row_factory is sqlite3.Row
    db.executescript("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT);")
    for k in range(3):
        db.execute("INSERT INTO t VALUES (?, ?)", (k, f"v{k}"))
    rows = db.execute("SELECT k, v FROM t ORDER BY k")
    assert isinstance(rows, Rows)
    assert rows.fetchone()["v"] == "v0"
    assert [r["k"] for r in rows.fetchall()] == [1, 2]
    assert rows.fetchone() is None and rows.fetchall() == []
    assert [r["k"] for r in db.execute("SELECT k FROM t")] == [0, 1, 2]
    assert db.execute("UPDATE t SET v = 'x' WHERE k >= ?", (1,)).rowcount == 2
    assert db.execute("SELECT v FROM t WHERE k = :k", {"k": 2}).fetchone()[0] == "x"
    db.close()
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


def test_concurrent_session_resolution(tmp_path: Path) -> None:
    a = BenchAuth(tmp_path / "keys", tmp_path / "b.sqlite")
    n = 16
    principals = []
    for i in range(n):
        _tid, tok = a.issue_token(f"user-{i}", "researcher", ("d3",))
        principals.append(a.verify_token(tok))
    cookies = [a.create_session(p)[0] for p in principals]

    def step(i: int, k: int) -> None:
        got = a.resolve_session(cookies[i])
        assert got.name == f"user-{i}", (i, got.name)
        if k % 7 == 3:                      # 同时有登录与注销写入会话表
            v, _ = a.create_session(principals[i])
            assert a.resolve_session(v).name == f"user-{i}"
            a.delete_session(v)

    errs = _hammer(n, step)
    assert errs == [], f"{len(errs)} errors, first: {errs[0]!r}" if errs else ""


def test_concurrent_run_store_reads_and_writes(tmp_path: Path) -> None:
    st = RunStore(tmp_path / "runs.sqlite")
    made: list[str] = []
    lock = threading.Lock()

    def step(i: int, k: int) -> None:
        if i < 4:                            # 写线程：入队后迁移到 PREPARING
            rid = f"run-{i:02d}{k:08d}"[:14]
            st.enqueue(rid, experiment_id="exp-k12", event_id="crop-handoff-001", world_id="oryahovo-crop1", spec={}, tenant="d3")
            st.transition(rid, "PREPARING", actor="scheduler")
            with lock:
                made.append(rid)
        else:                                # 读线程：列表中不出现 attempt 行缺失等中间状态
            for it in st.listing():
                assert it["state"] in ("QUEUED", "PREPARING"), it["state"]
            st.ids()

    errs = _hammer(12, step)
    assert errs == [], f"{len(errs)} errors, first: {errs[0]!r}" if errs else ""
    assert len(made) > 0
    assert st.ids() == set(made)
    assert {it["state"] for it in st.listing()} == {"PREPARING"}


def test_bench_api_concurrent_requests_same_session(tmp_path: Path) -> None:
    pytest.importorskip("foxglove_schemas_protobuf")
    from fastapi.testclient import TestClient

    from awr.api.bench_main import create_app
    from awr.bench.settings import BenchSettings

    s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=("https://bench.example",), secure_cookie=False)
    app = create_app(s)
    tok = app.state.auth.issue_token("alice", "researcher", ("d3",))[1]
    statuses: list[int] = []
    slock = threading.Lock()
    with TestClient(app) as c0:
        r = c0.post("/api/bench/v1/session", json={"token": tok}, headers={"origin": "https://bench.example"})
        assert r.status_code == 204
        cookie = r.cookies.get("d3_session") or c0.cookies.get("d3_session")
        assert cookie

        def step(i: int, k: int) -> None:
            path = ("/api/bench/v1/session", "/api/bench/v1/runs", "/api/bench/v1/events", "/api/bench/v1/worlds")[(i + k) % 4]
            code = c0.get(path, cookies={"d3_session": cookie}).status_code
            with slock:
                statuses.append(code)
            assert code == 200, (path, code)

        errs = _hammer(8, step)
    assert errs == [], f"{len(errs)} errors, first: {errs[0]!r}" if errs else ""
    assert len(statuses) > 50 and set(statuses) == {200}
