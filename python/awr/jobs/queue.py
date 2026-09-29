"""SQLite 任务队列（`runs/jobs/jobs.sqlite`，WAL，单写者 job-worker；M03 §6.14）。D1-ext 骨架：建表、提交、认领、
状态推进与崩溃标记；REST（`rest/jobs.py`）与 bus 服务在 MS6 接入。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .ids import new_job_id

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
QUEUE_MAX = 16

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

    def claim(self, first_stage: str) -> dict | None:
        now = time.time_ns()
        cur = self.db.execute("UPDATE jobs SET state = ?, worker_pid = ?, attempt = attempt + 1, updated_unix_ns = ? "
                              "WHERE job_id = (SELECT job_id FROM jobs WHERE state = 'QUEUED' ORDER BY created_unix_ns LIMIT 1) "
                              "RETURNING job_id", (first_stage, os.getpid(), now))
        row = cur.fetchone()
        return self.get(row[0]) if row else None

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
        for row in self.db.execute("SELECT job_id, worker_pid FROM jobs WHERE state NOT IN ('QUEUED','SUCCEEDED','FAILED','CANCELLED')").fetchall():
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
                            error_json=json.dumps({"code": "WORKER_CRASHED", "resumable": True}))
                n += 1
        return n
