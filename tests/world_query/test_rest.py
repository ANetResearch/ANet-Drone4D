"""M04-AC-014、AC-015（路由部分）：`POST /api/world/{id}/query` 的校验与转发。超限 422 110；未知 op 400 300；非会话世界
409 123；> 10 次/s 时 429 111；bus 超时（1 s × 3 次）503 211；队列满 503 213；越界点返回 null；路由校验 CPU ≤ 1 ms。

bus 以同进程替身接到 `GeoProbeServer`（真 sim-core 的端到端用例标 `--rt`，由 M16 harness 调度）。
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from awr.api.rest import world_query as R
from awr.contracts.bus_keys import svc_geo
from awr.world.geometry import GeoProbeServer
from awr.world.geometry.fake import TOWER


class _Q:
    def __init__(self, payload):
        self.id, self.op, self.args = payload["id"], payload["op"], payload["args"]
        self.reply_payload = None

    def reply(self, p):
        self.reply_payload = p


class FakeBus:
    def __init__(self, srv, fail=0, exc=TimeoutError):
        self.srv, self.fail, self.exc, self.calls = srv, fail, exc, 0

    async def call(self, key, payload, *, timeout_s):
        self.calls += 1
        if self.calls <= self.fail:
            raise self.exc("no reply")
        q = _Q(payload)
        self.srv.enqueue("ray_hit" if key == svc_geo("ray_hit") else "height", q)
        for _ in range(10_000):
            if q.reply_payload is not None:
                break
            self.srv.run(500)
        return q.reply_payload


@pytest.fixture
def app(wq, monkeypatch):
    monkeypatch.setattr(R, "BUCKETS", R.TokenBuckets())
    monkeypatch.setattr(R, "RETRY_GAP_S", 0.0)
    a = FastAPI()
    a.include_router(R.router)
    a.state.session_world_id = "tiny"
    a.state.bus = FakeBus(GeoProbeServer(wq))
    return a


def post(client, body, world="tiny"):
    return client.post(f"/api/world/{world}/query", json=body)


def test_ray_hit_ok(app):
    c = TestClient(app)
    r = post(c, {"op": "ray_hit", "origin_enu_m": [TOWER[0], TOWER[1], 300.0], "dir": [0, 0, -1], "max_range_m": 5000})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["hit"] and b["hit_kind"] == "top" and b["surface"] == "dsm" and abs(b["point_enu_m"][2] - 100.0) < 1e-9
    assert {"content_version", "source", "derive_sha8", "t_proc_us", "normal_enu", "ground_z_m", "agl_m", "origin_inside", "dist_m"} <= set(b)
    assert r.headers["cache-control"] == "no-store" and r.headers["awr-api-version"] == "1"


def test_points_and_null_oob(app):
    c = TestClient(app)
    r = post(c, {"op": "height_dsm", "points": [[TOWER[0], TOWER[1]], [5000.0, 0.0]]})
    assert r.status_code == 200 and r.json()["z_m"] == [100.0, None] and r.json()["source"] == "dsm_2m"
    r = post(c, {"op": "clearance", "points": [[0.0, -100.0, 50.0]], "radius_m": 5})
    assert r.status_code == 200 and isinstance(r.json()["clearance_m"][0], float)
    r = post(c, {"op": "probe", "points": [[120.0, -80.0]]})
    assert r.json()["items"][0]["zones"] == ["nofly-l"]


def test_limits_and_schema(app):
    c = TestClient(app)
    r = post(c, {"op": "height_dsm", "points": [[0.0, 0.0]] * 65})
    assert r.status_code == 422 and r.json()["code"] == 110 and r.headers["content-type"].startswith("application/problem+json")
    r = post(c, {"op": "clearance", "points": [[0.0, 0.0, 1.0]], "radius_m": 11})
    assert r.status_code == 422 and r.json()["code"] == 110
    r = post(c, {"op": "teleport"})
    assert r.status_code == 400 and r.json()["code"] == 300 and r.json()["detail"]["reason"] == "UNKNOWN_OP"
    r = post(c, {"op": "ray_hit", "origin_enu_m": [0, 0, 10], "dir": [0, 0, -2]})
    assert r.status_code == 400 and r.json()["detail"]["reason"] == "BAD_VECTOR"
    r = post(c, {"op": "height_dsm", "points": [[0.0]]})
    assert r.status_code == 400
    body = r.json()
    assert {"type", "title", "status", "code", "reason", "message", "request_id", "remedy"} <= set(body)


def test_world_not_in_session(app):
    c = TestClient(app)
    r = post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]}, world="shenzhen")
    assert r.status_code == 409 and r.json()["code"] == 123 and r.json()["detail"]["reason"] == "WORLD_NOT_IN_SESSION"
    r = post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]}, world="Bad_ID")
    assert r.status_code == 400


def test_rate_limit(app):
    c = TestClient(app)
    codes = [post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]}).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and 429 in codes[10:]
    r = post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]})
    assert r.status_code == 429 and r.json()["code"] == 111 and "retry-after" in r.headers


def test_bus_timeout_retries_then_211(app, wq):
    app.state.bus = FakeBus(GeoProbeServer(wq), fail=3)
    c = TestClient(app)
    r = post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]})
    assert r.status_code == 503 and r.json()["code"] == 211 and app.state.bus.calls == 3
    app.state.bus = FakeBus(GeoProbeServer(wq), fail=2)
    assert post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]}).status_code == 200


def test_queue_full_is_213(app, wq):
    srv = GeoProbeServer(wq, queue_max=0)
    app.state.bus = FakeBus(srv)
    r = post(TestClient(app), {"op": "height_dsm", "points": [[0.0, 0.0]]})
    assert r.status_code == 503 and r.json()["code"] == 213


def test_no_bus_or_session(app):
    app.state.bus = None
    c = TestClient(app)
    assert post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]}).json()["code"] == 211
    app.state.session_world_id = None
    r = post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "GEO_NOT_READY"


def test_route_validation_cpu_budget():
    body = {"op": "ray_hit", "origin_enu_m": [1.0, 2.0, 3.0], "dir": [0.0, 0.6, -0.8], "max_range_m": 1000}
    R.validate_body(body)
    t = time.perf_counter()
    for _ in range(200):
        R.validate_body(body)
    assert (time.perf_counter() - t) / 200 < 1e-3


def test_route_does_not_import_geometry():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(R.__file__).read_text(encoding="utf-8"))
    mods = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)] + \
           [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any(m and m.startswith("awr.world.geometry") for m in mods)


class ZenohLikeBus:
    """与 `awr.runtime.bus.ZenohBus.call` 同签名：总线内重试，无回复抛 BusTimeout，错误回复抛 BusReplyError。"""

    def __init__(self, srv, fail=0, reply_error=False):
        self.inner = FakeBus(srv)
        self.fail, self.reply_error, self.kw = fail, reply_error, None

    async def call(self, key, msg, *, timeout=1.0, retries=2, retry_gap=0.3):
        from awr.runtime.bus import BusReplyError, BusTimeout

        self.kw = {"timeout": timeout, "retries": retries, "retry_gap": retry_gap}
        if self.reply_error:
            raise BusReplyError("err")
        if self.fail > retries:
            raise BusTimeout("no reply")
        return await self.inner.call(key, msg, timeout_s=timeout)


def test_zenoh_bus_signature(app, wq):
    c = TestClient(app)
    app.state.bus = ZenohLikeBus(GeoProbeServer(wq))
    assert post(c, {"op": "height_dsm", "points": [[TOWER[0], TOWER[1]]]}).json()["z_m"] == [100.0]
    assert app.state.bus.kw == {"timeout": 1.0, "retries": 2, "retry_gap": 0.0}
    app.state.bus = ZenohLikeBus(GeoProbeServer(wq), fail=3)
    r = post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]})
    assert r.status_code == 503 and r.json()["code"] == 211
    app.state.bus = ZenohLikeBus(GeoProbeServer(wq), reply_error=True)
    r = post(c, {"op": "height_dsm", "points": [[0.0, 0.0]]})
    assert r.status_code == 503 and r.json()["code"] == 213
