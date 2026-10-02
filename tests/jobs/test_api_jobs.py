"""REST R06、R39–R42、R64（M03 `rest/jobs.py`；FX-SIM2）：TestClient + 由 `awr.jobs.service.JobService` 扮演 job-worker 的总线
替身。覆盖：列表与详情、未知任务 404 `347`、日志尾部、取消（排队中直接 CANCELLED、终态 409 `348`）、重试（非提交者 403、
非 FAILED 409）、R06 仅 admin、job-worker 无回复 503 `213`。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from awr.api.problem import ApiProblem, problem
from awr.api.rest import jobs as J
from awr.api.security import ApiPrincipal
from awr.contracts.bus_keys import svc_job
from awr.jobs.service import JobService
from awr.runtime.bus import BusTimeout


class WorkerBus:
    def __init__(self, svc: JobService | None) -> None:
        self.svc = svc

    async def call(self, key, msg, *, timeout=1.0, retries=0, retry_gap=0.3):
        if self.svc is None:
            raise BusTimeout(key)
        op = key.rsplit("/", 1)[1]
        assert key == svc_job(op)
        return self.svc.handle(op, msg)


def make_app(bus) -> FastAPI:
    app = FastAPI()
    app.include_router(J.router)

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

    app.state.awr = SimpleNamespace(bus=bus)
    app.state.bus = bus
    return app


def H(role: str, pid: str) -> dict:
    return {"Authorization": f"Bearer {role}:{pid}"}


def test_jobs_rest(tmp_path) -> None:
    svc = JobService(tmp_path / "jobs.sqlite", worlds_dir=tmp_path / "worlds")
    c = TestClient(make_app(WorkerBus(svc)))
    r = c.post("/api/worlds/shenzhen/build", json={"force": True}, headers=H("operator", "p-op"))
    assert r.status_code == 403
    r = c.post("/api/worlds/shenzhen/build", json={"force": True}, headers=H("admin", "p-adm"))
    assert r.status_code == 202 and r.json()["state"] == "QUEUED", r.text
    jid = r.json()["job_id"]
    assert r.headers["Location"] == f"/api/jobs/{jid}"
    lst = c.get("/api/jobs", headers=H("viewer", "v"))
    assert lst.status_code == 200 and [i["job_id"] for i in lst.json()["items"]] == [jid]
    d = c.get(f"/api/jobs/{jid}", headers=H("viewer", "v")).json()
    assert d["kind"] == "world_build" and d["target_world_id"] == "shenzhen" and d["submitted_by"] == "p-adm"
    assert c.get("/api/jobs/j-00000000-0000-7000-8000-000000000000", headers=H("viewer", "v")).status_code == 404
    assert c.get(f"/api/jobs/{jid}/log?tail=5", headers=H("viewer", "v")).json() == {"lines": []}
    # 重试：非提交者 403；非 FAILED 409 348
    assert c.post(f"/api/jobs/{jid}/retry", headers=H("operator", "p-op")).status_code == 403
    r = c.post(f"/api/jobs/{jid}/retry", headers=H("admin", "p-adm"))
    assert r.status_code == 409 and r.json()["code"] == 348
    # 取消：排队中直接 CANCELLED；再次取消 409 348
    r = c.post(f"/api/jobs/{jid}/cancel", headers=H("admin", "p-adm"))
    assert r.status_code == 200 and r.json()["state"] == "CANCELLED", r.text
    r = c.post(f"/api/jobs/{jid}/cancel", headers=H("admin", "p-adm"))
    assert r.status_code == 409 and r.json()["code"] == 348
    svc.close()


def test_worker_unavailable() -> None:
    c = TestClient(make_app(WorkerBus(None)))
    r = c.get("/api/jobs", headers=H("viewer", "v"))
    assert r.status_code == 503 and r.json()["code"] == 213
