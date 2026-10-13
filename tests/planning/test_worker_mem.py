"""共享规划 worker 的世界、Grid25 与内存（M10-FR-204、FR-205；M10-AC-204；D2-AC-28 的 worker 部分）。

- 多世界 LRU（≤ 4）：按作业键打开、淘汰时连同 Grid25 一起释放；世界 id 不合规拒绝（路径穿越）；
- Grid25 取 M04 派生缓存（写时复制 memmap，文件页）；世界未提供缓存时，允许派生（D1、本机开发）在进程内构建，公开模式
  （`allow_derive = false`）回复 123 derived_cache_missing；
- 派生缓存缺失（`allow_derive = false`）的作业回复 123 derived_cache_missing；
- 缓存 synthcity 与 sanfrancisco 并各跑 A* 与 safe_transit 后，worker 匿名页 PSS ≤ 90 MB，Grid25 位于 `.geo-cache` 的文件映射。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np
import pytest
from registry_cases import ROOT, city_cases

from awr.sim.planning import worker as W
from awr.sim.planning.jobs import PlanRequest
from awr.sim.planning.server import PlanServer
from awr.world.geometry.fake import fake_world_query, tiny_world


@pytest.fixture
def worker_state():
    saved = {k: (copy.copy(v) if k in ("worlds", "grids") else v) for k, v in W._STATE.items()}
    yield W._STATE
    W._STATE.clear()
    W._STATE.update(saved)


def _key(w) -> tuple[str, str, str]:
    return (w.world_id, w.content_version, w.coordinate_sha256)


def test_lru_evicts_world_and_its_grids(tmp_path, worker_state) -> None:
    w1 = fake_world_query(tmp_path)
    tiny_world(tmp_path, "tiny2")
    from awr.world.geometry.query import open_world_query

    w2 = open_world_query(tmp_path / "tiny2", tmp_path / ".geo-cache")
    W.configure_worlds(tmp_path, allow_derive=False, lru_worlds=1, cache_root=tmp_path / ".geo-cache")
    a = W.get_world(_key(w1))
    W.grid25_for(a, 20.0)
    assert list(W._STATE["worlds"]) == [_key(w1)] and any(k[0] == _key(w1) for k in W._STATE["grids"])
    W.get_world(_key(w2))
    assert list(W._STATE["worlds"]) == [_key(w2)] and not any(k[0] == _key(w1) for k in W._STATE["grids"])
    W.configure_worlds(tmp_path, allow_derive=False, lru_worlds=9)
    assert W._STATE["lru_worlds"] == 4                                           # 上限 4（M10-FR-204）
    W.get_world(_key(w1))
    assert W.get_world(_key(w1)) is W._STATE["worlds"][_key(w1)]                # 命中缓存


def test_bad_world_id_and_mismatch(tmp_path, worker_state) -> None:
    w = fake_world_query(tmp_path)
    W.configure_worlds(tmp_path, allow_derive=False)
    with pytest.raises(W.PlanGridMismatch):
        W.get_world(("../tiny", w.content_version, w.coordinate_sha256))
    with pytest.raises(W.PlanGridMismatch):
        W.get_world((w.world_id, w.content_version, "0" * 64))
    r = W.worker_main(PlanRequest("x", "warm", ("nope", "a", "b"), (), {}, {}, 3, 0, 1000, "x"))
    assert r.code == 123 and r.detail == "geo_missing_file"


def test_derived_cache_missing_is_123(tmp_path, worker_state) -> None:
    w = fake_world_query(tmp_path)
    empty = tmp_path / "empty-cache"
    empty.mkdir()
    W.configure_worlds(tmp_path, allow_derive=False, cache_root=empty)
    r = W.worker_main(PlanRequest("x", "safe_transit", _key(w), ("v",), {"start": [0, 0, 50.0], "goal": [10, 0, 50.0]},
                                  {}, 0, 0, 50, "x"))
    assert r.status == "error" and r.code == 123 and r.detail == "derived_cache_missing"


class _NoGrid25:
    """遮住 `grid25` 的世界代理（模拟 M04 未提供 Grid25 缓存的世界）。"""

    def __init__(self, w) -> None:
        self._w = w

    def __getattr__(self, name: str):
        if name == "grid25":
            raise AttributeError(name)
        return getattr(self._w, name)


def test_grid25_from_cache_or_build_policy(tmp_path, worker_state) -> None:
    w = fake_world_query(tmp_path)
    W.set_world(_key(w), w)
    g = W.grid25_for(w, 20.0)
    assert g is not None and isinstance(g.a, np.memmap) and g.a.flags.writeable              # 写时复制映射
    assert W.grid25_for(w, 20.0) is g                                                         # 按 (世界, 高度) 缓存
    from awr.sim.planning.grid25 import Grid25

    ref = Grid25.from_world(w, 20.0)
    assert np.array_equal(np.asarray(g.a), ref.a)                                             # 与 D1 进程内构建逐位相同
    p = _NoGrid25(w)
    W._STATE["grids"].clear()
    W._STATE["grid_build"] = True                                                             # D1、本机开发：进程内构建
    g2 = W.grid25_for(p, 20.0)
    assert g2 is not None and np.array_equal(g2.a, ref.a)
    W._STATE["grids"].clear()
    W._STATE["grid_build"] = False                                                            # 公开模式：拒绝
    with pytest.raises(W.WorldNotReady):
        W.grid25_for(p, 20.0)
    W.set_world(_key(w), p)
    W._STATE["grid_build"] = False
    r = W.worker_main(PlanRequest("a", "astar25", _key(w), ("v",), {"start": [-120.0, -20.0, 30.0],
                                                                     "goal": [30.0, -20.0, 60.0], "planner": "astar25"},
                                  {"v_max_mps": 12.0}, 0, 0, 300, "a"))
    assert r.code == 123 and r.detail == "derived_cache_missing"


def _pss_anon_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
        if line.startswith("Pss_Anon:"):
            return int(line.split()[1])
    raise AssertionError("Pss_Anon missing")


def _city_or_skip(name: str):
    from awr.world.geometry.query import open_world_query
    from awr.world.geometry.types import GeoLoadError

    d = ROOT / "worlds" / name
    if not (d / "world.json").exists():
        pytest.skip(f"world {name} not built")
    try:
        w = open_world_query(d, allow_derive=False)
    except GeoLoadError as e:
        pytest.skip(f"world {name} derived cache unavailable: {e.code}")
    if not hasattr(w, "grid25_info") or w.grid25_info() is None:
        pytest.skip(f"world {name} has no Grid25 in the derived cache")
    return w


@pytest.mark.needs_data
@pytest.mark.slow
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc smaps_rollup")
def test_worker_pss_with_two_worlds(tmp_path, monkeypatch) -> None:
    import registry_cases as RC

    worlds = {n: _city_or_skip(n) for n in ("synthcity", "sanfrancisco")}
    monkeypatch.setattr(RC, "CITIES", (*RC.CITIES, "synthcity"))
    srv = PlanServer(workers=1, worlds_dir=ROOT / "worlds", allow_derive=False, shm_root=tmp_path, nice=0)
    try:
        assert srv.wait_ready(60)
        pid = srv.worker_pids()[0]
        for name, w in worlds.items():
            reqs = city_cases(name, w)
            for rq in [r for r in reqs if r.kind == "astar25"][:3] + [r for r in reqs if r.kind == "safe_transit"][:3]:
                rep = srv.submit("sb-mem", rq).wait(120)
                assert rep is not None and rep["code"] != 123 and rep["status"] != "timeout", (rq.job_id, rep)
        kb = _pss_anon_kb(pid)
        maps = Path(f"/proc/{pid}/maps").read_text()
        assert kb <= 90 * 1024, f"worker Pss_Anon {kb / 1024:.1f} MB"
        g25 = [ln for ln in maps.splitlines() if ".geo-cache" in ln and "/grid25" in ln]
        assert any("/sanfrancisco/" in ln for ln in g25) and any("/synthcity/" in ln for ln in g25)
    finally:
        srv.close()


def test_retain_worlds_unmaps_inactive(tmp_path, worker_state) -> None:
    w1 = fake_world_query(tmp_path)
    tiny_world(tmp_path, "tiny2")
    from awr.world.geometry.query import open_world_query

    w2 = open_world_query(tmp_path / "tiny2", tmp_path / ".geo-cache")
    W.configure_worlds(tmp_path, allow_derive=False, lru_worlds=4)
    W.grid25_for(W.get_world(_key(w1)), 20.0)
    W.get_world(_key(w2))
    assert W.retain_worlds(["tiny2"]) == 1
    assert list(W._STATE["worlds"]) == [_key(w2)] and not any(k[0] == _key(w1) for k in W._STATE["grids"])
    assert W.retain_worlds({"tiny2"}) == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc maps")
def test_server_unmaps_world_after_session_ends(tmp_path, plan_plugins) -> None:
    """只映射活动世界（AWR-04 §4.4.2，D2F-06）：会话结束后，其余会话不再使用的世界在 worker 中解除映射。"""
    import time

    w1 = fake_world_query(tmp_path)
    tiny_world(tmp_path, "tiny2")
    from awr.world.geometry.query import open_world_query

    w2 = open_world_query(tmp_path / "tiny2", tmp_path / ".geo-cache")
    srv = PlanServer(workers=1, worlds_dir=tmp_path, allow_derive=False, shm_root=tmp_path / "shm", nice=0)
    try:
        assert srv.wait_ready(60)
        for sid, w in (("sb-a", w1), ("sb-b", w2)):
            rq = PlanRequest(f"{sid}:e", "test.echo", _key(w), (), {"x": 1}, {}, 0, 0, 200, "e")
            r = srv.submit(sid, rq).wait(30)
            assert r["status"] == "ok", r
        pid = srv.worker_pids()[0]

        def mapped(name: str) -> bool:
            return f"/.geo-cache/{name}/" in Path(f"/proc/{pid}/maps").read_text()

        assert mapped("tiny") and mapped("tiny2")
        srv.drop_session("sb-a")
        t_end = time.monotonic() + 10
        while mapped("tiny") and time.monotonic() < t_end:
            time.sleep(0.05)
        assert not mapped("tiny") and mapped("tiny2")
        assert srv.submit("sb-c", PlanRequest("c", "test.echo", _key(w1), (), {"x": 2}, {}, 0, 0, 200, "c")).wait(30)[
            "status"] == "ok"                                                         # 再次使用时重新打开
    finally:
        srv.close()
