"""SQLite 任务队列（`runs/jobs/jobs.sqlite`，WAL；M03 §6.14）。建表、提交、认领（按任务类型的首阶段）、状态推进、取消、
重试与崩溃标记（344 JOB_WORKER_CRASHED）。写者为 job-worker 进程（主线程执行任务，服务线程处理 `svc/job/*`，各自一条连接）。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from .ids import new_job_id

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
QUEUE_MAX = 16
JOB_WORKER_CRASHED = 344

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_world_id TEXT NOT NULL,
  state TEXT NOT NULL, stage TEXT, progress_pct REAL NOT NULL DEFAULT 0,
  params_json TEXT NOT NULL, submitted_by TEXT NOT NULL,
  created_unix_ns INTEGER NOT NULL, updated_unix_ns INTEGER NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0, last_completed_stage TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER,
  error_json TEXT, resumable INTEGER NOT NULL DEFAULT 0, idempotency_key TEXT UNIQUE);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_target ON jobs(target_world_id)
  WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED');
"""


class JobConflict(Exception):
    code = 124          # JOB_CONFLICT


class QueueFull(Exception):
    code = 333          # JOB_QUEUE_FULL


class JobQueue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def submit(self, kind: str, target_world_id: str, params: dict, submitted_by: str, idempotency_key: str | None = None) -> dict:
        now = time.time_ns()
        if idempotency_key:
            row = self.db.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row:
                return dict(row)
        n = self.db.execute("SELECT COUNT(*) FROM jobs WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED')").fetchone()[0]
        if n >= QUEUE_MAX:
            raise QueueFull(f"{n} active jobs >= {QUEUE_MAX}")
        jid = new_job_id()
        try:
            self.db.execute("INSERT INTO jobs(job_id, kind, target_world_id, state, params_json, submitted_by, created_unix_ns, "
                            "updated_unix_ns, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)",
                            (jid, kind, target_world_id, "QUEUED", json.dumps(params), submitted_by, now, now, idempotency_key))
        except sqlite3.IntegrityError as e:
            raise JobConflict(f"an active job already targets {target_world_id}") from e
        return self.get(jid)

    def get(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def claim(self, first_stage: str | Callable[[str], str]) -> dict | None:
        """认领最早的 QUEUED 任务并置为其首个阶段；`first_stage` 为阶段名或 `kind -> 首阶段` 函数（M01-to-M03 第 4 条：
        按任务类型的首个阶段认领）。"""
        now = time.time_ns()
        row = self.db.execute("SELECT job_id, kind FROM jobs WHERE state = 'QUEUED' ORDER BY created_unix_ns LIMIT 1").fetchone()
        if row is None:
            return None
        stage = first_stage(str(row["kind"])) if callable(first_stage) else first_stage
        cur = self.db.execute("UPDATE jobs SET state = ?, worker_pid = ?, attempt = attempt + 1, updated_unix_ns = ? "
                              "WHERE job_id = ? AND state = 'QUEUED' RETURNING job_id", (stage, os.getpid(), now, row["job_id"]))
        got = cur.fetchone()
        return self.get(got[0]) if got else None

    def list(self, *, limit: int = 100, states: tuple[str, ...] | None = None) -> list[dict]:
        if states:
            q = f"SELECT * FROM jobs WHERE state IN ({','.join('?' * len(states))}) ORDER BY created_unix_ns DESC LIMIT ?"
            rows = self.db.execute(q, (*states, int(limit))).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY created_unix_ns DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def retry(self, job_id: str) -> int:
        """R42：只接受 FAILED 且 resumable 的任务，回到 QUEUED（续跑由 runner 按完成标记决定）；返回 0 或 348 / 347。"""
        row = self.get(job_id)
        if row is None:
            return 347
        if row["state"] != "FAILED" or not row["resumable"]:
            return 348
        try:
            self.update(job_id, state="QUEUED", cancel_requested=0, error_json=None, worker_pid=None)
        except sqlite3.IntegrityError:
            return 124
        return 0

    def update(self, job_id: str, **fields) -> None:
        fields["updated_unix_ns"] = time.time_ns()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {cols} WHERE job_id = ?", (*fields.values(), job_id))

    def request_cancel(self, job_id: str) -> bool:
        cur = self.db.execute("UPDATE jobs SET cancel_requested = 1 WHERE job_id = ? AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED')",
                              (job_id,))
        return cur.rowcount > 0

    def mark_crashed(self) -> int:
        """启动时把 worker_pid 已不存在的非终态任务置 FAILED(resumable)（12 §4.12 J06）。"""
        n = 0
        for row in self.db.execute("SELECT job_id, worker_pid, stage FROM jobs "
                                   "WHERE state NOT IN ('QUEUED','SUCCEEDED','FAILED','CANCELLED')").fetchall():
            pid = row["worker_pid"]
            alive = False
            if pid:
                try:
                    os.kill(int(pid), 0)
                    alive = True
                except OSError:
                    alive = False
            if not alive:
                self.update(row["job_id"], state="FAILED", resumable=1,
                            error_json=json.dumps({"code": JOB_WORKER_CRASHED, "name": "JOB_WORKER_CRASHED",
                                                   "stage": row["stage"],
                                                   "resumable": True}))
                n += 1
        return n
