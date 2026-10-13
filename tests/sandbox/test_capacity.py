"""公开容量接口与池统计（M17-FR-068、FR-069、NFR-017、AC-025、AC-026；AWR-04 §15.4、§15.5.4、§15.6；D2-AC-42 的功能部分）：
构建信息（部署 `RELEASE`、构建环境、无 git 时 `nogit`）、show-api 状态文件（新鲜、已满、停更 5 s 为 unknown、缺失）、实例用户口径
（PARKING 与 DRAINING 计入、PARKED 不计、内部预留不计入分母）、`hint` 推导、`awr.capacity.v1` schema（契约 1.2.0 草案）与 ≤ 1 KiB、
SB-71 经真实 sandbox-api 与池（无 token 200、缓存头、与池真值一致、不含访客标识）。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
from brokerkit import create, finish_parks, make_broker, ready
from sbharness import SbxStack, make_sb

from awr.sandbox.capacity import assemble, build_info, hint_of, pool_stats, read_show_status

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
P = "/api/sandbox/v1"


def _schema() -> dict:
    official = ROOT / "packages" / "contracts" / "sandbox" / "capacity.schema.json"
    p = official if official.exists() else HERE / "fixtures" / "contracts_draft" / "capacity.schema.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _valid(doc: Any) -> list[str]:
    v = jsonschema.Draft202012Validator(_schema())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(doc)]


def _show(p: Path, *, live: int, mx: int, queue: int = 0, eta: int | None = None, age_s: float = 0.0) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"t_unix_ns": str(int((time.time() - age_s) * 1e9)), "live": live, "max": mx, "queue": queue,
                             "eta_s": eta, "releases_per_s": 0.01}))


def test_build_info_sources(tmp_path: Path) -> None:
    rel = tmp_path / "RELEASE"
    rel.write_text("release=20261012-083000-3f9a1c2d4e\ngit=3f9a1c2d4e-dirty\nbuilt_at=2026-10-12T08:30:00Z\n"
                   "web=VITE_AWR_DEMO=public\n")
    b = build_info({"AWR_RELEASE_FILE": str(rel)})
    assert b == {"version": b["version"], "channel": "demo", "commit": "3f9a1c2d4e", "dirty": True,
                 "built_at": "2026-10-12T08:30:00Z", "release": "20261012-083000-3f9a1c2d4e"}
    assert b["version"].count(".") == 2
    env = {"AWR_RELEASE_FILE": str(tmp_path / "none"), "AWR_BUILD_SHA": "0123456789abcdef", "AWR_BUILD_DIRTY": "0",
           "AWR_BUILD_CHANNEL": "ci", "SOURCE_DATE_EPOCH": "1791000000"}
    b2 = build_info(env)
    assert b2["commit"] == "0123456789" and b2["channel"] == "ci" and b2["release"] == "local-0123456789"
    assert b2["built_at"] == "2026-10-03T04:00:00Z"
    b3 = build_info({"AWR_RELEASE_FILE": str(tmp_path / "none")}, root=tmp_path)   # 无 git：nogit
    assert b3["commit"] == "nogit" and b3["release"] == "local-nogit" and b3["version"] == "0.0.0"
    for x in (b, b2, b3):
        assert not _valid(assemble(None, {"live": None, "max": None, "queue": None, "eta_s": None, "state": "unknown"}, x,
                                   {"viewer_idle_s": 300, "sandbox_idle_s": 600, "hidden_s": 60, "warn_s": 30}))


def test_show_status_file(tmp_path: Path) -> None:
    p = tmp_path / "_status" / "show.json"
    assert read_show_status(p)["state"] == "unknown"
    _show(p, live=18, mx=24)
    assert read_show_status(p) == {"live": 18, "max": 24, "queue": 0, "eta_s": None, "state": "open"}
    _show(p, live=24, mx=24, queue=3, eta=240)
    assert read_show_status(p) == {"live": 24, "max": 24, "queue": 3, "eta_s": 240, "state": "full"}
    _show(p, live=3, mx=24, age_s=6)
    assert read_show_status(p) == {"live": None, "max": None, "queue": None, "eta_s": None, "state": "unknown"}
    p.write_text("{not json")
    assert read_show_status(p)["state"] == "unknown"


def test_pool_stats_instance_user_semantics() -> None:
    b, fx, _c = make_broker(n_max=2)
    s1 = create(b, 1)["result"]["session"]["sid"]
    ready(b, s1)
    st = pool_stats(b)
    assert st["active"] == 1 and st["state"] == "open" and st["admit_now"] and "synthcity" in st["worlds_admit_now"]
    assert st["eta_s"] == 0 and st["reserved_internal"] == 1 and st["internal_active"] == 0
    b.activity({s1: {"ws_n": 1, "api_calls": 1}})
    b.park(s1)
    assert pool_stats(b)["active"] == 1                                # PARKING 计入
    finish_parks(b, fx)
    st = pool_stats(b)
    assert st["active"] == 0 and st["parked"] == 1                     # PARKED 不计
    s2 = create(b, 2)["result"]["session"]["sid"]
    s3 = create(b, 3)["result"]["session"]["sid"]
    for s in (s2, s3):
        ready(b, s)
    st = pool_stats(b)
    assert st["state"] == "full" and not st["admit_now"] and st["eta_s"] is not None and st["eta_s"] > 0
    si = create(b, 4, identity={"jti": "j" * 16, "sub": "p-" + "c" * 32, "exp": 2_000_000_000})
    assert si["http"] == 201
    st = pool_stats(b)
    assert st["active"] == 2 and st["internal_active"] == 1 and st["max"] == 2   # 内部预留不计入分母
    create(b, 5)
    st = pool_stats(b)
    assert st["state"] == "queue" and st["queue"] == 1 and st["worlds_admit_now"] == []
    b.stop(s2)
    assert pool_stats(b)["active"] == 2                                # DRAINING 计入
    b.set_accepting(False)
    assert pool_stats(b)["state"] == "closed" and pool_stats(b)["maintenance"] is True


def test_hint_and_assemble_size() -> None:
    unk = {"state": "unknown"}
    assert hint_of(unk, {"state": "closed"}, pool_ok=False) == "unavailable"
    assert hint_of({"state": "full"}, {"state": "queue"}, pool_ok=True) == "queue_both"
    assert hint_of({"state": "full"}, {"state": "open"}, pool_ok=True) == "queue_viewer"
    assert hint_of({"state": "open"}, {"state": "full"}, pool_ok=True) == "queue_sandbox"
    assert hint_of({"state": "open"}, {"state": "closed"}, pool_ok=True) == "closed"
    assert hint_of(unk, {"state": "open"}, pool_ok=True) == "open"
    b, _fx, _c = make_broker(n_max=6)
    doc = assemble(pool_stats(b), {"live": 24, "max": 24, "queue": 100, "eta_s": 6000, "state": "full"},
                   build_info({"AWR_RELEASE_FILE": "/nonexistent"}),
                   {"viewer_idle_s": 300, "sandbox_idle_s": 600, "hidden_s": 60, "warn_s": 30})
    assert not _valid(doc), _valid(doc)
    assert len(json.dumps(doc, separators=(",", ":")).encode()) <= 1024
    assert doc["hint"] == "queue_viewer" and doc["sandbox"]["max"] == 6


@pytest.fixture(scope="module")
def st():
    s = SbxStack(sb=make_sb(max_sessions=1, spawn={"timeout_s": 4}),
                 sb_api={"rest": {"rps": 200, "burst": 400, "clock_rps": 100, "entity_rps": 100}})
    yield s
    s.close()


def test_capacity_endpoint_public_and_consistent(st: SbxStack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    show = tmp_path / "_status" / "show.json"
    monkeypatch.setenv("AWR_SHOW_STATUS_FILE", str(show))
    with httpx.Client(base_url=st.base, headers={"origin": st.origin}, timeout=15) as c:
        r = c.get(f"{P}/capacity")
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=1"
        d = r.json()
        assert not _valid(d), _valid(d)
        assert len(r.content) <= 1024 and d["viewers"]["state"] == "unknown"
        assert d["sandbox"]["max"] == 1 and d["sandbox"]["active"] == 0 and d["sandbox"]["state"] == "open"
        assert d["hint"] == "open" and d["power"]["sandbox_idle_s"] > 0
        text = r.text
        assert "principal" not in text and "prefix" not in text and "sb-" not in text and "198.51" not in text
        _show(show, live=24, mx=24, queue=2, eta=120)
        created = c.post(f"{P}/sessions", json={"world": "synthcity", "principal_hint": f"{9:032x}"})
        assert created.status_code == 201
        # 进程内缓存：1 s 内直接回复，1–2 s 先回旧值并后台刷新（D2-FX-R2），因此 2 s 内与池的真值一致（D2-AC-42）
        t0 = time.monotonic()
        d = c.get(f"{P}/capacity").json()
        while d["sandbox"]["active"] != 1 and time.monotonic() - t0 < 2.5:
            time.sleep(0.1)
            d = c.get(f"{P}/capacity").json()
        assert time.monotonic() - t0 <= 2.5
        assert d["sandbox"]["active"] == 1 and d["sandbox"]["state"] == "full" and d["viewers"]["state"] == "full"
        assert d["hint"] == "queue_both" and d["viewers"]["queue"] == 2
        assert st.pt.op("stats", {})["result"]["active"] == 1
        st.pt.call(lambda: [st.broker.stop(r.sid) for r in st.broker.live()])
        st.pt.wait(lambda: not st.broker.live(), 15, "ended")
    os.environ.pop("AWR_SHOW_STATUS_FILE", None)


def test_capacity_fast_path_serves_cached_bytes(st: SbxStack) -> None:
    """D2-FX-R2（D2-AC-42/55）：缓存可用时由中间件快速路径回复（不经 FastAPI 路由），响应与路由路径同形、带安全头与容量头，
    `t_unix_ns` 逐次更新；1–2 s 的旧值先回复并后台刷新。"""
    from awr.api.rest.sandbox import CapacityCache, _capacity_head, capacity_cache

    with httpx.Client(base_url=st.base, headers={"origin": st.origin}, timeout=15) as c:
        first = c.get(f"{P}/capacity")
        assert first.status_code == 200
        cache = capacity_cache(st.api.ctx)
        hits = cache.hits
        r = c.get(f"{P}/capacity")
        assert r.status_code == 200 and cache.hits == hits + 1
        assert r.headers["cache-control"] == "public, max-age=1" and r.headers["awr-sandbox-api"] == "1"
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["awr-api-version"] == "1"
        a, b = first.json(), r.json()
        assert int(b["t_unix_ns"]) >= int(a["t_unix_ns"]) and {k: v for k, v in a.items() if k != "t_unix_ns"} == \
            {k: v for k, v in b.items() if k != "t_unix_ns"}
        assert len(r.content) == int(r.headers["content-length"]) <= 1024
    cc = CapacityCache()
    cc.set({"schema": "awr.capacity.v1", "t_unix_ns": "1", "x": [1, 2]})
    body = json.loads(cc.body() or b"")
    assert body["x"] == [1, 2] and body["t_unix_ns"] != "1" and list(body)[-1] == "t_unix_ns"
    assert json.loads(_capacity_head({}) + b'5"}') == {"t_unix_ns": "5"}
    cc.t -= 1.5                                    # 1.5 s 旧：先回旧值并触发后台刷新（这里无 hub：不刷新）
    assert cc.fast(None) is not None
    cc.t -= 1.0                                    # 2.5 s 旧：交给路由同步刷新
    assert cc.fast(None) is None


def test_build_info_wp16_release_keys(tmp_path: Path) -> None:
    rel = tmp_path / "RELEASE"
    rel.write_text("release=20261005-192405-nogit\nversion=0.2.0\nchannel=demo\ncommit=nogit\ndirty=false\n"
                   "built_at=2026-10-05T19:24:05Z\ngit=nogit\nweb=VITE_AWR_DEMO=public\n")
    b = build_info({"AWR_RELEASE_FILE": str(rel)})
    assert b == {"version": "0.2.0", "channel": "demo", "commit": "nogit", "dirty": False, "built_at": "2026-10-05T19:24:05Z",
                 "release": "20261005-192405-nogit"}
    rel.write_text("release=20261012-083000-3f9a1c2d4e-dirty\nversion=0.2.0\nchannel=demo\ncommit=3f9a1c2d4e\ndirty=true\n"
                   "built_at=2026-10-12T08:30:00Z\n")
    b2 = build_info({"AWR_RELEASE_FILE": str(rel)})
    assert b2["commit"] == "3f9a1c2d4e" and b2["dirty"] is True
    for x in (b, b2):
        assert not _valid(assemble(None, {"live": None, "max": None, "queue": None, "eta_s": None, "state": "unknown"}, x,
                                   {"viewer_idle_s": 300, "sandbox_idle_s": 600, "hidden_s": 60, "warn_s": 30}))
