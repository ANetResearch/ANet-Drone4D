"""M03-AC-027（D1-ext 骨架部分）：SQLite 任务队列的提交、幂等、同目标冲突 124、队列满 333、认领、取消与崩溃标记；
uuid7 任务 id；world_build 任务已注册。"""

from __future__ import annotations

import json
import uuid

import pytest

from awr.jobs.ids import new_job_id, uuid7
from awr.jobs.queue import JobConflict, JobQueue, QueueFull
from awr.jobs.registry import get_job


@pytest.fixture
def q(tmp_path):
    jq = JobQueue(tmp_path / "jobs.sqlite")
    yield jq
    jq.close()


def test_uuid7_monotonic_and_version():
    a = uuid7(1_000)
    b = uuid7(2_000)
    assert a.version == 7 and a.variant == uuid.RFC_4122 and a.int < b.int
    assert new_job_id().startswith("j-")


def test_submit_idempotent_conflict_and_full(q):
    j = q.submit("world_build", "shenzhen", {"force": False}, "admin", idempotency_key="k1")
    assert j["state"] == "QUEUED"
    assert q.submit("world_build", "shenzhen", {}, "admin", idempotency_key="k1")["job_id"] == j["job_id"]
    with pytest.raises(JobConflict) as ei:
        q.submit("world_build", "shenzhen", {}, "admin")
    assert ei.value.code == 124
    for i in range(15):
        q.submit("world_build", f"w{i}", {}, "admin")
    with pytest.raises(QueueFull) as ei2:
        q.submit("world_build", "w99", {}, "admin")
    assert ei2.value.code == 333


def test_claim_cancel_and_crash_marking(q):
    j = q.submit("world_build", "shanghai", {}, "admin")
    c = q.claim("INGESTING")
    assert c["job_id"] == j["job_id"] and c["state"] == "INGESTING" and c["attempt"] == 1
    assert q.request_cancel(j["job_id"]) and q.get(j["job_id"])["cancel_requested"] == 1
    q.update(j["job_id"], worker_pid=2**22 + 12345)                  # 不存在的进程
    assert q.mark_crashed() == 1
    row = q.get(j["job_id"])
    assert row["state"] == "FAILED" and row["resumable"] == 1 and json.loads(row["error_json"])["code"] == 344


def test_world_build_registered():
    from awr.jobs import world_build  # noqa: F401

    k = get_job("world_build")
    assert k.stages == ("INGESTING", "TILING", "DERIVING", "VALIDATING", "PUBLISHING")
