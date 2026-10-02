"""job-worker 的 bus 服务 `svc/job/{submit,cancel,status,engines}` 与任务事件（M03 §6.14、§7.3；M03-FR-055、FR-057；
M01-to-M03 第 4 条；AWR-17 §4.3.10、§9.3）。

线程模型：job-worker 主线程一次执行一个任务（长时间占用）；本服务在独立线程上处理查询，zenoh 回调只把请求入队（PY-CB-01）。
服务线程使用自己的 SQLite 连接（WAL），提交时唤醒主线程认领（`wake`）。事件经 `LockedEvents`（EventPublisher 的线程安全
包装）从两个线程发出：服务线程发 `job.state`（QUEUED、排队中取消的 CANCELLED），主线程与 runner 发其余状态、进度与日志。

请求与回复（均为 dict，失败为 `{code, detail}`，码见 17 §8.4）：
- `submit {kind, target_world_id, params, submitted_by, role, idempotency_key, retry_of?}` → `{job_id, state, ...}`；
  `kind = recon` 转 M01 `awr.reconstruction.jobs.service.submit`（R01 守卫：330、331、332、123、124、333、346）；
  `world_build` 由本模块入队（124、333）；`retry_of` 为 R42（只接受 FAILED 且 resumable，否则 348，不存在 347）；
- `cancel {job_id}` → `{ok, state}`（QUEUED 直接 CANCELLED；执行中置 cancel_requested，runner 在检查点响应；终态 348）；
- `status {job_id?, limit?, tail?}` → 单个任务记录（`recon` 任务附 `recon{}`，M01 `recon_fields`）或 `{items}`；
  `tail` 给出时返回 `{lines}`（`job.log` 尾部 ≤ 1000 行，R64）；
- `engines {}` → M01 `service.engines()`。
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import queue
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import world_build  # noqa: F401 - 注册 world_build
from .queue import TERMINAL, JobConflict, JobQueue, QueueFull
from .registry import get_job

__all__ = ["OPS", "JobService", "LockedEvents", "job_json"]

log = logging.getLogger("awr.jobs.service")

OPS = ("submit", "cancel", "status", "engines")
TARGET_RE = re.compile(r"^[a-z0-9-]{1,63}$")
LOG_TAIL_MAX = 1000
RECON_SERVICE = "awr.reconstruction.jobs.service"


class LockedEvents:
    """EventPublisher 的线程安全包装（emit 与 flush 串行化）；`serve_replays` 由服务线程周期调用。"""

    def __init__(self, pub: Any) -> None:
        self.pub = pub
        self.lock = threading.Lock()

    def emit(self, kind: str, **kw: Any) -> int:
        with self.lock:
            return int(self.pub.emit(kind, **kw))

    def flush(self) -> int:
        with self.lock:
            return int(self.pub.flush())

    def close(self) -> None:
        with self.lock, contextlib.suppress(Exception):
            self.pub.close()


def _recon() -> Any:
    try:
        return importlib.import_module(RECON_SERVICE)
    except ImportError:
        return None


def job_json(row: dict, *, workdir: Path | None = None) -> dict:
    """任务记录（17 §4.3.10 字段；`recon` 任务附 `recon{}` 与 `scale_status`）。"""
    err = None
    if row.get("error_json"):
        with contextlib.suppress(ValueError, TypeError):
            e = json.loads(row["error_json"])
            err = {"code": e.get("code"), "message": e.get("detail") if isinstance(e.get("detail"), str) else e.get("name"),
                   **{k: e[k] for k in ("name", "stage", "resumable", "detail") if k in e}}
    out = {"job_id": row["job_id"], "kind": row["kind"], "state": row["state"], "stage": row.get("stage"),
           "progress_pct": float(row.get("progress_pct") or 0.0), "submitted_by": row.get("submitted_by"),
           "created_unix_ns": str(row.get("created_unix_ns")), "updated_unix_ns": str(row.get("updated_unix_ns")),
           "target_world_id": row.get("target_world_id"), "attempt": int(row.get("attempt") or 0),
           "resumable": bool(row.get("resumable")), "cancel_requested": bool(row.get("cancel_requested")),
           "last_completed_stage": row.get("last_completed_stage"), "scale_status": None, "error": err}
    if row.get("kind") == "recon":
        mod = _recon()
        if mod is not None and workdir is not None:
            with contextlib.suppress(Exception):
                rf = mod.recon_fields(row, workdir)
                out["recon"] = rf
                out["scale_status"] = rf.get("scale_status")
    return out


class JobService:
    def __init__(self, db_path: Path, *, events: Any = None, worlds_dir: Path | None = None,
                 wake: Callable[[], None] | None = None) -> None:
        self.db_path = Path(db_path)
        self.events = events
        self.worlds_dir = Path(worlds_dir) if worlds_dir is not None else _default_worlds_dir()
        self.wake = wake or (lambda: None)
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.handles: list[Any] = []
        self._local = threading.local()
        self._conns: list[JobQueue] = []
        self.stats = {op: 0 for op in OPS}

    # ---------------------------------------------------------------- 线程
    @property
    def q(self) -> JobQueue:
        """当前线程的队列连接（sqlite3 连接不跨线程；服务线程之外的调用方，例如测试，各用各的连接）。"""
        jq = getattr(self._local, "q", None)
        if jq is None:
            jq = self._local.q = JobQueue(self.db_path)
            self._conns.append(jq)
        return jq

    def serve(self, bus: Any) -> None:
        from awr.contracts import bus_keys

        for op in OPS:
            self.handles.append(bus.serve(bus_keys.svc_job(op), lambda r, op=op: self.inbox.put((op, r))))

    def pump(self, timeout: float = 0.2) -> int:
        n = 0
        try:
            item = self.inbox.get(timeout=timeout)
        except queue.Empty:
            item = None
        while item is not None:
            op, req = item
            try:
                rep = self.handle(op, req.msg() if hasattr(req, "msg") else req)
            except Exception as e:
                log.exception("svc/job request failed", extra={"kv": {"op": op}})
                rep = {"code": 213, "detail": {"why": "JOB_WORKER_ERROR", "error": type(e).__name__}}
            with contextlib.suppress(Exception):
                req.reply_msg(rep)
            n += 1
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                item = None
        if self.events is not None:
            with contextlib.suppress(Exception):
                self.events.flush()  # 应答排队的 `_replay` 请求
        return n

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.pump(0.2)

    def close(self) -> None:
        for h in self.handles:
            with contextlib.suppress(Exception):
                h.close()
        self.handles.clear()
        for jq in self._conns:
            with contextlib.suppress(Exception):
                jq.close()
        self._conns.clear()
        self._local = threading.local()

    # ---------------------------------------------------------------- 处理
    def handle(self, op: str, msg: Any) -> dict:
        msg = msg if isinstance(msg, dict) else {}
        self.stats[op] = self.stats.get(op, 0) + 1
        if op == "submit":
            return self.submit(msg)
        if op == "cancel":
            return self.cancel(msg)
        if op == "status":
            return self.status(msg)
        if op == "engines":
            mod = _recon()
            return mod.engines() if mod is not None else {"items": []}
        return {"code": 300, "detail": {"op": op}}

    def _event_state(self, row: dict, state: str, prev: str | None) -> None:
        if self.events is None:
            return
        with contextlib.suppress(Exception):
            self.events.emit("job.state", t_sim_ns=0, severity=1 if state == "CANCELLED" else 0,
                             fields={"job_id": row["job_id"], "kind": row["kind"], "target_world_id": row["target_world_id"],
                                     "state": state, "prev_state": prev, "stage": row.get("stage"),
                                     "progress_pct": float(row.get("progress_pct") or 0.0),
                                     "attempt": int(row.get("attempt") or 0), "output": None, "error": None})
            self.events.flush()

    def submit(self, m: dict) -> dict:
        if m.get("retry_of"):
            jid = str(m["retry_of"])
            code = self.q.retry(jid)
            if code:
                return {"code": code, "detail": {"job_id": jid}}
            row = self.q.get(jid)
            self._event_state(row, "QUEUED", "FAILED")
            self.wake()
            return {"job_id": jid, "state": "QUEUED", "target_world_id": row["target_world_id"]}
        kind = str(m.get("kind") or "")
        params = m.get("params") if isinstance(m.get("params"), dict) else {}
        by = str(m.get("submitted_by") or "")
        idem = m.get("idempotency_key") or None
        if kind == "recon":
            mod = _recon()
            if mod is None:
                return {"code": 331, "detail": {"why": "RECON_NOT_INSTALLED"}}
            rep = mod.submit(params, submitted_by=by, role=str(m.get("role") or "operator"), worlds_dir=self.worlds_dir,
                             queue=self.q, idempotency_key=idem)
            if rep.get("job_id"):
                row = self.q.get(rep["job_id"])
                if row is not None:
                    self._event_state(row, "QUEUED", None)
                self.wake()
            return rep
        try:
            get_job(kind)
        except KeyError:
            return {"code": 300, "detail": {"field": "kind", "value": kind}}
        target = str(m.get("target_world_id") or "")
        if not TARGET_RE.match(target):
            return {"code": 300, "detail": {"field": "target_world_id"}}
        try:
            row = self.q.submit(kind, target, params, by, idem)
        except JobConflict:
            return {"code": 124, "detail": {"target_world_id": target}}
        except QueueFull:
            return {"code": 333, "detail": None}
        if row["state"] == "QUEUED":
            self._event_state(row, "QUEUED", None)
        self.wake()
        return {"job_id": row["job_id"], "state": row["state"], "target_world_id": target}

    def cancel(self, m: dict) -> dict:
        jid = str(m.get("job_id") or "")
        row = self.q.get(jid)
        if row is None:
            return {"code": 347, "detail": {"job_id": jid}}
        by = m.get("submitted_by")
        if by is not None and m.get("role") != "admin" and row.get("submitted_by") not in (None, "", by):
            return {"code": 115, "detail": {"why": "NOT_SUBMITTER"}}
        if row["state"] in TERMINAL:
            return {"code": 348, "detail": {"state": row["state"]}}
        if row["state"] == "QUEUED":
            self.q.update(jid, state="CANCELLED", cancel_requested=1)
            self._event_state(row, "CANCELLED", "QUEUED")
            return {"ok": True, "state": "CANCELLED"}
        self.q.request_cancel(jid)
        return {"ok": True, "state": row["state"], "cancel_requested": True}

    def status(self, m: dict) -> dict:
        jid = m.get("job_id")
        if jid:
            row = self.q.get(str(jid))
            if row is None:
                return {"code": 347, "detail": {"job_id": jid}}
            wd = self.db_path.parent / row["job_id"]
            if m.get("tail") is not None:
                n = max(1, min(LOG_TAIL_MAX, int(m.get("tail") or 200)))
                lines: list[str] = []
                with contextlib.suppress(OSError):
                    lines = (wd / "job.log").read_text(encoding="utf-8").splitlines()[-n:]
                return {"job_id": row["job_id"], "lines": lines}
            return job_json(row, workdir=wd)
        limit = max(1, min(200, int(m.get("limit") or 100)))
        return {"items": [job_json(r, workdir=self.db_path.parent / r["job_id"]) for r in self.q.list(limit=limit)]}


def _default_worlds_dir() -> Path:
    import os

    return Path(os.environ.get("AWR_WORLDS_DIR", Path(__file__).resolve().parents[3] / "worlds"))
