"""REST R38 / R63 (M01-AC-016; M01-FR-014, FR-015, FR-043, FR-044; M01-NFR-010, NFR-012).

TestClient with the recon router, a stand-in principal middleware, the real M03 catalog and SQLite queue, and a bus
stand-in that plays the job-worker (`svc/job/*` served by `awr.reconstruction.jobs.service`). Covered: 301 without
token, 115 for viewers, 116 without the seat, 332 for existing or built-in targets, 123 for a source that is not READY,
124 for a second active job on the same target, 330 for unknown fields, 331 GPU_REQUIRED for lingbot_map, 333 for the
17th queued job, 213 when the worker does not answer, and the R63 fallback. Latency (200 submits, p99 <= 50 ms) is the
perf-marked case.
"""

from __future__ import annotations

import json
import shutil
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from recon_common import job_params, make_worlds, needs_world

from awr.api.problem import ApiProblem, problem
from awr.api.rest import recon as R
from awr.api.security import ApiPrincipal
from awr.contracts.bus_keys import svc_job
from awr.runtime.bus import BusTimeout


class WorkerBus:
    def __init__(self, worlds, queue, *, down: bool = False):
        self.worlds, self.queue, self.down, self.calls = worlds, queue, down, []

    async def call(self, key, msg, *, timeout=1.0, retries=0, retry_gap=0.3):
        from awr.reconstruction.jobs import service

        self.calls.append(key)
        if self.down:
            raise BusTimeout(key)
        if key == svc_job("submit"):
            from awr.jobs.queue import JobQueue

            q = JobQueue(self.queue)                     # the worker owns its SQLite connection (one per thread)
            try:
                return service.submit(msg["params"], submitted_by=msg["submitted_by"], role=msg["role"], worlds_dir=self.worlds,
                                      queue=q, idempotency_key=msg.get("idempotency_key"))
            finally:
                q.close()
        if key == svc_job("engines"):
            return service.engines()
        raise BusTimeout(key)


class Gw:
    def __init__(self, holder: str):
        self.holder = holder

    def is_seat_holder(self, pid: str) -> bool:
        return pid == self.holder


def make_app(bus, catalog, holder="p-op"):
    app = FastAPI()
    app.include_router(R.router)

    @app.exception_handler(ApiProblem)
    async def _problem(request: Request, exc: ApiProblem):
        return problem(exc.code, status=exc.status, detail=exc.detail, headers=exc.headers)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        tok = request.headers.get("Authorization", "")
        if tok.startswith("Bearer "):
            role, pid = tok[7:].split(":")
            request.state.principal = ApiPrincipal(pid, role, "jti", 0, "loopback")
        return await call_next(request)

    app.state.awr = SimpleNamespace(catalog=catalog, gateway=Gw(holder), bus=bus)
    app.state.bus = bus
    return app


OP = {"Authorization": "Bearer operator:p-op"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from awr.jobs.queue import JobQueue
    from awr.world.package.catalog import Catalog

    wd = make_worlds(tmp_path)
    nr = wd / "notready"                                   # a world without a ready status
    nr.mkdir()
    shutil.copy(wd / "shenzhen" / "world.json", nr / "world.json")
    q = JobQueue(tmp_path / "jobs.sqlite")
    monkeypatch.setattr(R, "_engines_cache", {"t": -1e18, "items": None})
    bus = WorkerBus(wd, tmp_path / "jobs.sqlite")
    yield SimpleNamespace(worlds=wd, queue=q, bus=bus, client=TestClient(make_app(bus, Catalog(wd))), catalog=Catalog(wd))
    q.close()


def body(target, **kw):
    b = job_params(target)
    b.update(kw)
    return b


@needs_world
def test_submit_accepted(env):
    r = env.client.post("/api/recon/jobs", json=body("shenzhen-recon-01"), headers={**OP, "Idempotency-Key": "k1"})
    assert r.status_code == 202, r.text
    j = r.json()
    assert j["state"] == "QUEUED" and j["job_id"].startswith("j-") and j["session_id"].startswith("rs-")
    assert r.headers["location"] == f"/api/jobs/{j['job_id']}"
    row = env.queue.get(j["job_id"])
    assert row["kind"] == "recon" and row["submitted_by"] == "p-op" and json.loads(row["params_json"])["engine"] == "mock"
    r2 = env.client.post("/api/recon/jobs", json=body("shenzhen-recon-01"), headers={**OP, "Idempotency-Key": "k1"})
    assert r2.status_code == 202 and r2.json()["job_id"] == j["job_id"]          # idempotent retry
    # default target: <source>-recon-<nn>
    b = body("x")
    b.pop("target_world_id")
    r3 = env.client.post("/api/recon/jobs", json=b, headers=OP)
    assert (r3.status_code == 202 and r3.json()["target_world_id"] == "shenzhen-recon-01") or r3.status_code == 409


@needs_world
def test_rejections(env):
    c = env.client
    assert c.post("/api/recon/jobs", json=body("a-1")).json()["code"] == 301
    r = c.post("/api/recon/jobs", json=body("a-1"), headers={"Authorization": "Bearer viewer:p-v"})
    assert r.status_code == 403 and r.json()["code"] == 115
    r = c.post("/api/recon/jobs", json=body("a-1"), headers={"Authorization": "Bearer operator:p-other"})
    assert r.status_code == 409 and r.json()["code"] == 116
    for t in ("shenzhen", "chicago"):
        r = c.post("/api/recon/jobs", json=body(t), headers=OP)
        assert r.status_code == 409 and r.json()["code"] == 332, t
    r = c.post("/api/recon/jobs", json={**body("a-2"), "source": {**body("a-2")["source"], "world_id": "notready"}}, headers=OP)
    assert r.status_code == 409 and r.json()["code"] == 123
    r = c.post("/api/recon/jobs", json={**body("a-3"), "colour": "red"}, headers=OP)
    assert r.status_code == 422 and r.json()["code"] == 330 and r.json()["detail"]
    r = c.post("/api/recon/jobs", json=body("a-4", engine="lingbot_map"), headers=OP)
    assert r.status_code == 409 and r.json()["code"] == 331 and r.json()["detail"]["reason"] == "GPU_REQUIRED"
    assert c.post("/api/recon/jobs", json=body("a-5"), headers=OP).status_code == 202
    r = c.post("/api/recon/jobs", json=body("a-5"), headers=OP)
    assert r.status_code == 409 and r.json()["code"] == 124
    r = c.post("/api/recon/jobs", content=b"{" + b" " * (65 * 1024) + b"}", headers={**OP, "Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["code"] == 330


@needs_world
def test_queue_full(env):
    for i in range(16):
        assert env.client.post("/api/recon/jobs", json=body(f"q-{i}"), headers=OP).status_code == 202
    r = env.client.post("/api/recon/jobs", json=body("q-16"), headers=OP)
    assert r.status_code == 429 and r.json()["code"] == 333


@needs_world
def test_worker_down_and_engines(env):
    items = env.client.get("/api/recon/engines", headers=OP).json()["items"]
    by = {i["engine"]: i for i in items}
    assert by["mock"]["available"] and by["lingbot_map"]["reason"] == "GPU_REQUIRED" and len(items) == 6
    assert env.client.get("/api/recon/engines").status_code == 401
    env.bus.down = True
    R._engines_cache.update(t=-1e18, items=None)
    items = env.client.get("/api/recon/engines", headers=OP).json()["items"]
    assert all(i["reason"] == "WORKER_UNAVAILABLE" and not i["available"] for i in items)
    r = env.client.post("/api/recon/jobs", json=body("d-1"), headers=OP)
    assert r.status_code == 503 and r.json()["code"] == 213 and r.json()["detail"] == "JOB_WORKER_UNAVAILABLE"


@pytest.mark.perf
@needs_world
def test_submit_latency(env):
    """M01-NFR-010: 200 submissions (api validation + forwarding), p99 <= 50 ms."""
    env.bus.down = False
    dts = []
    for i in range(200):
        t = time.perf_counter()
        env.client.post("/api/recon/jobs", json=body(f"l-{i}"), headers=OP)
        dts.append(time.perf_counter() - t)
    assert float(np.percentile(dts, 99)) <= 0.050
