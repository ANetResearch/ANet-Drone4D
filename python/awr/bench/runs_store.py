"""Run 目录查找与 Run 状态库（M27 §6.2.3、§6.3、§7.6；AWR-17 §18.6；D3-AC-51）。

**目录**：`<data_root>/dataset/<event_id>/runs/<run_id>/{run_manifest.json, raw.mcap, frames.idx}`。Env 基准运行（M26 recorder，
`manifest.json`，schema `drone4d.bench.env_run.v1`）同处一目录，列表项另带策略、物理档、排名标记与评测摘要（`evaluation.json` 的
四项分量与 `ranked_valid`）。失败或取消的 attempt 的产物在重试时移入 `runs/<run_id>/.attempts/<n>/`（M27 §6.3 不变量 2）。

**状态库**（`RunStore`，`bench.sqlite` 的 `experiments`、`runs`、`attempts`、`run_transitions`、`run_idempotency` 表，WAL）：
状态机 QUEUED → PREPARING → RUNNING → FINALIZING → EVALUATING → COMPLETED，任一非终态可至 FAILED（必须带原因码），
QUEUED、PREPARING、RUNNING 可取消；FAILED 与 CANCELLED 经手动重试回到 QUEUED（`run_id` 不变、`attempt + 1`、
`restart_count = 0`）。迁移一律以"当前状态 + attempt"为条件更新（比较并交换），并发的调度器迁移与 API 取消不会互相覆盖；
每次迁移写一行 `run_transitions` 审计。

- 取消（BN-44，M27-FR-024）：QUEUED 立即 CANCELLED；PREPARING、RUNNING 写 `cancel_requested` 与时限（`cancel_timeout_s` = 10 s），
  由调度器在时限内终止进程组后以 `transition(…, "CANCELLED")` 确认；FINALIZING、EVALUATING 与终态返回 680；
- 重试（BN-45，M27-FR-039）：只对 FAILED、CANCELLED；队列上限（全局 10,000、每租户 2,000，M27 §8 `queue`）超出时 681；
  同一 principal 的同一 `Idempotency-Key` 重放返回首次结果；
- 自动重启（M27-FR-035）：PREPARING、RUNNING → PREPARING 时 `restart_count + 1`，达到 `max_restarts`（缺省 2）后拒绝（680），
  调用方应改迁 FAILED（631、683 等）。

bench-api 只插入 QUEUED、写取消请求与重试；其余迁移由 bench-scheduler 与 run-controller 推进（M27 §6.2.3）。时刻为 UTC 纳秒。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from awr.bench.dbconn import SerialConnection
from awr.contracts.reasons import REASONS

__all__ = ["ACTIVE", "CANCEL_REQUESTABLE", "CANCEL_TIMEOUT_S", "RETRYABLE", "RUN_ID", "STATES", "TERMINAL", "TRANSITIONS", "RunStateError",
           "RunStore", "archive_attempt", "env_run_item", "find_run", "list_runs", "reason_view", "truth_paths"]

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

STATES = ("QUEUED", "PREPARING", "RUNNING", "FINALIZING", "EVALUATING", "COMPLETED", "FAILED", "CANCELLED")
TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
ACTIVE = frozenset(STATES) - TERMINAL
CANCEL_REQUESTABLE = frozenset({"PREPARING", "RUNNING"})
RETRYABLE = frozenset({"FAILED", "CANCELLED"})
# M27 §6.3 迁移表（不含手动重试，重试见 RunStore.retry）；PREPARING → CANCELLED 按 M27-FR-024
TRANSITIONS: dict[str, frozenset[str]] = {
    "QUEUED": frozenset({"PREPARING", "CANCELLED"}),
    "PREPARING": frozenset({"RUNNING", "PREPARING", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({"FINALIZING", "PREPARING", "FAILED", "CANCELLED"}),
    "FINALIZING": frozenset({"EVALUATING", "FAILED"}),
    "EVALUATING": frozenset({"COMPLETED", "FAILED"}),
    "COMPLETED": frozenset(), "FAILED": frozenset(), "CANCELLED": frozenset(),
}
CANCEL_TIMEOUT_S = 10.0
MAX_RESTARTS = 2
QUEUE_GLOBAL = 10_000
QUEUE_PER_TENANT = 2_000
_CLASS_RANK = {"preview": 0, "eval": 1, "research": 2, "batch": 3, "public": 4}


# ── 目录 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def find_run(dataset_dir: Path, run_id: str) -> Path | None:
    if not RUN_ID.match(run_id):
        return None
    hits = sorted(Path(dataset_dir).glob(f"*/runs/{run_id}"))
    return hits[0] if hits else None


def list_runs(dataset_dir: Path) -> list[dict]:
    out = []
    for d in sorted(Path(dataset_dir).glob("*/runs/*")):
        if not d.is_dir() or not RUN_ID.match(d.name):
            continue
        m = d / "run_manifest.json"
        doc = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
        item = {"run_id": d.name, "event_id": d.parent.parent.name, "state": doc.get("state", "UNKNOWN"),
                "has_frames": (d / "frames.idx").exists(), "created_unix_ns": doc.get("created_unix_ns"),
                "kind": doc.get("kind"), "world_id": doc.get("world_id")}
        env = d / "manifest.json"
        if "state" not in doc and env.exists():     # Env 运行：run_manifest.v1 不含 state，以 manifest.json 与评测结果判定
            item.update(env_run_item(d, json.loads(env.read_text(encoding="utf-8"))))
        out.append(item)
    return out


def env_run_item(d: Path, man: dict) -> dict:
    ev = d / "evaluation.json"
    e = json.loads(ev.read_text(encoding="utf-8")) if ev.exists() else None
    pol = man.get("policy") or {}
    return {"state": "EVALUATED" if e else "SUCCEEDED", "kind": "bench", "world_id": man.get("world"),
            "created_unix_ns": int((d / "manifest.json").stat().st_mtime_ns), "policy": pol.get("name") or pol.get("kind"),
            "physics": man.get("tier"), "obs_level": man.get("obs_level"), "ranked": bool(man.get("ranked")), "n_uav": len(man.get("fleet", [])),
            "duration_s": man.get("duration_s"), "evaluation": None if e is None else {"components": e["components"], "ranked_valid": e["ranked_valid"],
                                                                                      "invalid_reasons": e["invalid_reasons"]}}


def truth_paths(run_dir: Path) -> tuple[Path, Path]:
    """dev 划分的真值文件与索引：`<event>/truth/run_<id>.mcap`、`<event>/truth/run_<id>.frames.idx`（test 划分在 hidden/，P0a 不涉及）。"""
    t = Path(run_dir).parent.parent / "truth"
    return t / f"run_{Path(run_dir).name}.mcap", t / f"run_{Path(run_dir).name}.frames.idx"


def archive_attempt(run_dir: Path, attempt: int) -> Path | None:
    """把运行目录下除 `.attempts/` 之外的全部产物移入 `.attempts/<attempt>/`（重试前调用；M27 §6.3 不变量 2）。
    目录不存在或为空时返回 None；目标已存在时追加 `-<k>` 后缀，不覆盖。"""
    d = Path(run_dir)
    if not d.is_dir():
        return None
    items = sorted(p for p in d.iterdir() if p.name != ".attempts")
    if not items:
        return None
    dst = d / ".attempts" / str(int(attempt))
    k = 1
    while dst.exists():
        dst = d / ".attempts" / f"{int(attempt)}-{k}"
        k += 1
    dst.mkdir(parents=True)
    for p in items:
        shutil.move(str(p), str(dst / p.name))
    return dst


# ── 状态库 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

class RunStateError(Exception):
    """code：305 Run 不存在、680 当前状态不允许该操作、681 队列上限；`detail` 进入 problem+json。"""

    def __init__(self, code: int, why: str, **detail: Any) -> None:
        super().__init__(f"{code} {why}")
        self.code = code
        self.detail = {"why": why, **detail}


def reason_view(code: int | None, detail: Any = None) -> dict | None:
    """原因码输出 `{code, name, message, remedy, detail}`（AWR-17 §18.5.3 `reason` 形状加中文文案；未登记的码只给 code 与 detail）。"""
    if code is None:
        return None
    ri = REASONS.get(int(code))
    return {"code": int(code), "name": ri.name if ri else None, "message": ri.message_zh if ri else None,
            "remedy": ri.remedy_zh if ri else None, "detail": detail}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments(experiment_id TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL, note TEXT,
  created_by TEXT NOT NULL, world_id TEXT NOT NULL, world_cv TEXT NOT NULL, event_id TEXT NOT NULL,
  event_version INTEGER NOT NULL, tape_sha256 TEXT NOT NULL, created_unix_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, tenant TEXT NOT NULL, priority_class TEXT NOT NULL,
  split TEXT NOT NULL DEFAULT 'dev', spec_json TEXT NOT NULL, seed INTEGER, seed_commit TEXT NOT NULL, config_sha256 TEXT NOT NULL,
  spec_sha256 TEXT NOT NULL, run_hash TEXT, ranked INTEGER NOT NULL, ranked_valid INTEGER, state TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1, restart_count INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
  cost_json TEXT NOT NULL, blocked_by TEXT, gpu INTEGER, reason_code INTEGER, reason_json TEXT, retention TEXT NOT NULL,
  expires_unix_ns INTEGER, queued_unix_ns INTEGER NOT NULL, started_unix_ns INTEGER, ended_unix_ns INTEGER, idempotency_key TEXT,
  event_id TEXT NOT NULL, world_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'bench', submitted_by TEXT NOT NULL DEFAULT '',
  cancel_requested_unix_ns INTEGER, cancel_by TEXT, updated_unix_ns INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS runs_sched ON runs(state, priority_class, tenant, queued_unix_ns);
CREATE INDEX IF NOT EXISTS runs_cfg ON runs(config_sha256);
CREATE TABLE IF NOT EXISTS attempts(run_id TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL, reason_code INTEGER,
  dir TEXT NOT NULL, containers_json TEXT, started_unix_ns INTEGER, ended_unix_ns INTEGER, reason_json TEXT, restart_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(run_id, attempt));
CREATE TABLE IF NOT EXISTS run_transitions(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL, reason_code INTEGER, actor TEXT NOT NULL, t_unix_ns INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS run_transitions_run ON run_transitions(run_id, id);
CREATE TABLE IF NOT EXISTS run_idempotency(principal TEXT NOT NULL, key TEXT NOT NULL, op TEXT NOT NULL, run_id TEXT NOT NULL,
  response_json TEXT NOT NULL, t_unix_ns INTEGER NOT NULL, PRIMARY KEY(principal, key));
"""

_COLS = ("run_id", "experiment_id", "tenant", "priority_class", "split", "spec_json", "seed_commit", "ranked", "ranked_valid", "state",
         "attempt", "restart_count", "cancel_requested", "blocked_by", "gpu", "reason_code", "reason_json", "retention", "expires_unix_ns",
         "queued_unix_ns", "started_unix_ns", "ended_unix_ns", "event_id", "world_id", "kind", "submitted_by", "cancel_requested_unix_ns",
         "cancel_by", "updated_unix_ns", "run_hash")


def _spec_summary(spec: dict) -> dict:
    pol = spec.get("policy") or {}
    fleet = spec.get("fleet") or []
    return {"policy": pol.get("name") or pol.get("kind"), "physics": spec.get("physics"), "obs_level": spec.get("obs_level"),
            "n_uav": sum(int(f.get("count", 1)) for f in fleet if isinstance(f, dict)) if isinstance(fleet, list) else None,
            "duration_s": spec.get("duration_s")}


class RunStore:
    """Run 状态库。`clock` 返回 UTC 纳秒（测试可注入）。"""

    def __init__(self, db_path: Path, *, clock: Callable[[], int] = time.time_ns, max_restarts: int = MAX_RESTARTS,
                 queue_global: int = QUEUE_GLOBAL, queue_per_tenant: int = QUEUE_PER_TENANT, cancel_timeout_s: float = CANCEL_TIMEOUT_S) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.max_restarts = int(max_restarts)
        self.queue_global = int(queue_global)
        self.queue_per_tenant = int(queue_per_tenant)
        self.cancel_timeout_ns = int(cancel_timeout_s * 1e9)
        self._lock = threading.RLock()
        # 连接被 bench-api 线程池共用：全部语句经同一把 RLock 串行化，写事务期间的读也在锁外等待（dbconn，K12）
        self._db = SerialConnection(sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None, timeout=5.0), self._lock)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ---------------------------------------------------------------- 写入（API 与调度器）
    def _audit(self, run_id: str, attempt: int, src: str | None, dst: str, code: int | None, actor: str, now: int) -> None:
        self._db.execute("INSERT INTO run_transitions(run_id, attempt, from_state, to_state, reason_code, actor, t_unix_ns) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (run_id, attempt, src, dst, code, actor, now))

    def enqueue(self, run_id: str, *, experiment_id: str, event_id: str, world_id: str, spec: dict, tenant: str, submitted_by: str = "",
                priority_class: str = "research", ranked: bool = False, seed: int | None = None, seed_commit: str = "",
                config_sha256: str = "", spec_sha256: str = "", retention: str = "standard", split: str = "dev", kind: str = "bench",
                cost: dict | None = None, idempotency_key: str | None = None, experiment: dict | None = None) -> dict:
        """插入一个 QUEUED Run（attempt 1）；`experiment` 给出时同时登记实验行（已存在则保留）。排名运行不得传入明文种子。"""
        if not RUN_ID.match(run_id):
            raise ValueError(f"invalid run_id {run_id!r}")
        if ranked and seed is not None:
            raise ValueError("ranked runs keep the seed in EvalStore only")
        now = self.clock()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if experiment is not None:
                    e = experiment
                    self._db.execute("INSERT OR IGNORE INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                     (experiment_id, e.get("project", tenant), e.get("name", experiment_id), e.get("note"), submitted_by or tenant,
                                      world_id, e.get("world_cv", ""), event_id, int(e.get("event_version", 1)), e.get("tape_sha256", ""), now))
                self._db.execute(
                    "INSERT INTO runs(run_id, experiment_id, tenant, priority_class, split, spec_json, seed, seed_commit, config_sha256, spec_sha256, "
                    "ranked, state, attempt, restart_count, cancel_requested, cost_json, retention, queued_unix_ns, idempotency_key, event_id, "
                    "world_id, kind, submitted_by, updated_unix_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', 1, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, experiment_id, tenant, priority_class, split, json.dumps(spec, sort_keys=True), seed, seed_commit, config_sha256,
                     spec_sha256, int(bool(ranked)), json.dumps(cost or {}, sort_keys=True), retention, now, idempotency_key, event_id, world_id,
                     kind, submitted_by, now))
                self._db.execute("INSERT INTO attempts(run_id, attempt, state, dir) VALUES (?, 1, 'QUEUED', '')", (run_id,))
                self._audit(run_id, 1, None, "QUEUED", None, submitted_by or "api", now)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return self.get(run_id)

    def _row(self, run_id: str) -> sqlite3.Row | None:
        return self._db.execute(f"SELECT {', '.join(_COLS)} FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def _need(self, run_id: str) -> sqlite3.Row:
        r = self._row(run_id)
        if r is None:
            raise RunStateError(305, "run_unknown", run_id=run_id)
        return r

    def transition(self, run_id: str, to: str, *, reason_code: int | None = None, reason_detail: Any = None, actor: str = "scheduler",
                   expect: str | None = None, gpu: int | None = None, run_hash: str | None = None, ranked_valid: bool | None = None,
                   _extra: dict | None = None) -> dict:
        """按 M27 §6.3 迁移；`expect` 给出时另要求当前状态等于它。FAILED 必须带已登记的原因码。不合法的迁移抛 680。"""
        if to not in STATES:
            raise ValueError(f"unknown state {to!r}")
        if to == "FAILED" and reason_code is None:
            raise ValueError("FAILED requires a reason code")
        if reason_code is not None and int(reason_code) not in REASONS:
            raise ValueError(f"reason code {reason_code} is not registered in rt/reasons.json")
        now = self.clock()
        with self._lock:
            r = self._need(run_id)
            cur, att, rc = r["state"], int(r["attempt"]), int(r["restart_count"])
            if (expect is not None and cur != expect) or to not in TRANSITIONS[cur]:
                raise RunStateError(680, "transition", state=cur, to=to, attempt=att)
            restart = to == "PREPARING" and cur in ("PREPARING", "RUNNING")
            if restart and rc >= self.max_restarts:
                raise RunStateError(680, "restart_limit", state=cur, restart_count=rc, max_restarts=self.max_restarts)
            sets = {"state": to, "updated_unix_ns": now}
            if restart:
                sets["restart_count"] = rc + 1
            if cur == "QUEUED" and to == "PREPARING":
                sets["started_unix_ns"] = now
                sets["blocked_by"] = None
                if gpu is not None:
                    sets["gpu"] = int(gpu)
            if run_hash is not None:
                sets["run_hash"] = run_hash
            if ranked_valid is not None:
                sets["ranked_valid"] = int(bool(ranked_valid))
            if reason_code is not None:
                sets["reason_code"] = int(reason_code)
                sets["reason_json"] = json.dumps(reason_detail, sort_keys=True, ensure_ascii=False)
            if to in TERMINAL:
                sets["ended_unix_ns"] = now
            sets.update(_extra or {})
            self._db.execute("BEGIN IMMEDIATE")
            try:
                n = self._db.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in sets)} WHERE run_id = ? AND state = ? AND attempt = ?",
                                     (*sets.values(), run_id, cur, att)).rowcount
                if n != 1:
                    raise RunStateError(680, "concurrent_transition", state=cur, to=to)
                a = {"state": to, "restart_count": sets.get("restart_count", rc)}
                if cur == "QUEUED" and to == "PREPARING":
                    a["started_unix_ns"] = now
                if to in TERMINAL:
                    a["ended_unix_ns"] = now
                if reason_code is not None:
                    a["reason_code"] = int(reason_code)
                    a["reason_json"] = sets["reason_json"]
                self._db.execute(f"UPDATE attempts SET {', '.join(f'{k} = ?' for k in a)} WHERE run_id = ? AND attempt = ?",
                                 (*a.values(), run_id, att))
                self._audit(run_id, att, cur, to, reason_code, actor, now)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return self.get(run_id)

    def request_cancel(self, run_id: str, *, actor: str, reason_code: int | None = None, reason_detail: Any = None) -> dict:
        """BN-44：QUEUED 立即 CANCELLED；PREPARING、RUNNING 登记取消请求（幂等，时限 `cancel_timeout_s`）；其余 680。"""
        with self._lock:
            r = self._need(run_id)
            cur = r["state"]
            if cur == "QUEUED":
                now = self.clock()
                return self.transition(run_id, "CANCELLED", expect="QUEUED", actor=actor, reason_code=reason_code, reason_detail=reason_detail,
                                       _extra={"cancel_requested": 1, "cancel_requested_unix_ns": now, "cancel_by": actor})
            if cur in CANCEL_REQUESTABLE:
                if not r["cancel_requested"]:
                    now = self.clock()
                    n = self._db.execute("UPDATE runs SET cancel_requested = 1, cancel_requested_unix_ns = ?, cancel_by = ?, updated_unix_ns = ? "
                                         "WHERE run_id = ? AND state = ? AND attempt = ?", (now, actor, now, run_id, cur, r["attempt"])).rowcount
                    if n != 1:                                    # 调度器恰在此刻迁移：按新状态重判
                        return self.request_cancel(run_id, actor=actor, reason_code=reason_code, reason_detail=reason_detail)
                    self._audit(run_id, int(r["attempt"]), cur, "CANCEL_REQUESTED", reason_code, actor, now)
                return self.get(run_id)
            why = "terminal" if cur in TERMINAL else "not_cancellable"
            raise RunStateError(680, why, state=cur)

    def pending_cancels(self) -> list[dict]:
        """调度器轮询：已请求取消、仍处 PREPARING 或 RUNNING 的 Run（`deadline_unix_ns` 为终止时限）。"""
        rows = self._db.execute("SELECT run_id, attempt, state, cancel_requested_unix_ns FROM runs WHERE cancel_requested = 1 "
                                "AND state IN ('PREPARING', 'RUNNING') ORDER BY cancel_requested_unix_ns, run_id").fetchall()
        return [{"run_id": r["run_id"], "attempt": r["attempt"], "state": r["state"], "requested_unix_ns": r["cancel_requested_unix_ns"],
                 "deadline_unix_ns": r["cancel_requested_unix_ns"] + self.cancel_timeout_ns} for r in rows]

    def overdue_cancels(self, now_ns: int | None = None) -> list[dict]:
        """超过取消时限仍未终止的 Run（告警用）。"""
        t = self.clock() if now_ns is None else now_ns
        return [c for c in self.pending_cancels() if c["deadline_unix_ns"] < t]

    def set_blocked(self, run_id: str, blocked_by: Iterable[str]) -> None:
        """调度器写入排队阻塞原因（`gpu_share`、`vram`、`gz`、`cpu`、`mem`、`storage`、`write`、`calendar`、`quota`）；只对 QUEUED。"""
        b = ",".join(sorted(set(blocked_by)))
        self._db.execute("UPDATE runs SET blocked_by = ?, updated_unix_ns = ? WHERE run_id = ? AND state = 'QUEUED'",
                         (b or None, self.clock(), run_id))

    def retry(self, run_id: str, *, actor: str, idempotency_key: str | None = None) -> dict:
        """BN-45：FAILED 或 CANCELLED → 新 attempt（QUEUED）；→ `{run_id, attempt, replayed}`（`replayed` 为幂等重放）。"""
        with self._lock:
            if idempotency_key:
                hit = self._db.execute("SELECT op, run_id, response_json FROM run_idempotency WHERE principal = ? AND key = ?",
                                       (actor, idempotency_key)).fetchone()
                if hit is not None:
                    if hit["op"] != "retry" or hit["run_id"] != run_id:
                        raise RunStateError(680, "idempotency_key_reused", run_id=hit["run_id"])
                    return {**json.loads(hit["response_json"]), "replayed": True}
            r = self._need(run_id)
            cur, att = r["state"], int(r["attempt"])
            if cur not in RETRYABLE:
                raise RunStateError(680, "not_retryable", state=cur, attempt=att)
            n_all = self._db.execute("SELECT COUNT(*) FROM runs WHERE state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')").fetchone()[0]
            n_ten = self._db.execute("SELECT COUNT(*) FROM runs WHERE tenant = ? AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')",
                                     (r["tenant"],)).fetchone()[0]
            if n_all >= self.queue_global or n_ten >= self.queue_per_tenant:
                raise RunStateError(681, "queue_full", tenant=r["tenant"], active=int(n_ten), limit=self.queue_per_tenant,
                                    global_active=int(n_all), global_limit=self.queue_global)
            now = self.clock()
            new = att + 1
            out = {"run_id": run_id, "attempt": new}
            self._db.execute("BEGIN IMMEDIATE")
            try:
                n = self._db.execute(
                    "UPDATE runs SET state = 'QUEUED', attempt = ?, restart_count = 0, cancel_requested = 0, cancel_requested_unix_ns = NULL, "
                    "cancel_by = NULL, reason_code = NULL, reason_json = NULL, blocked_by = NULL, gpu = NULL, run_hash = NULL, ranked_valid = NULL, "
                    "queued_unix_ns = ?, started_unix_ns = NULL, ended_unix_ns = NULL, updated_unix_ns = ? WHERE run_id = ? AND state = ? AND attempt = ?",
                    (new, now, now, run_id, cur, att)).rowcount
                if n != 1:
                    raise RunStateError(680, "concurrent_transition", state=cur)
                self._db.execute("UPDATE attempts SET dir = ? WHERE run_id = ? AND attempt = ?", (f".attempts/{att}", run_id, att))
                self._db.execute("INSERT INTO attempts(run_id, attempt, state, dir) VALUES (?, ?, 'QUEUED', '')", (run_id, new))
                self._audit(run_id, new, cur, "QUEUED", None, actor, now)
                if idempotency_key:
                    self._db.execute("INSERT INTO run_idempotency VALUES (?, ?, 'retry', ?, ?, ?)",
                                     (actor, idempotency_key, run_id, json.dumps(out), now))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return {**out, "replayed": False}

    # ---------------------------------------------------------------- 读取
    def _positions(self) -> dict[str, int]:
        """排队位置（1 起）：先按优先级类序（preview > eval > research > batch > public），再按入队时刻；调度器的租户轮转可能改变
        实际次序，此处为近似值（AWR-17 §18.6 `queue_position`）。"""
        rows = self._db.execute("SELECT run_id, priority_class, queued_unix_ns FROM runs WHERE state = 'QUEUED'").fetchall()
        rows.sort(key=lambda q: (_CLASS_RANK.get(q["priority_class"], 9), q["queued_unix_ns"], q["run_id"]))
        return {q["run_id"]: i + 1 for i, q in enumerate(rows)}

    def _view(self, r: sqlite3.Row, pos: dict[str, int]) -> dict:
        st = r["state"]
        spec = json.loads(r["spec_json"])
        rq = bool(r["cancel_requested"])
        out = {"run_id": r["run_id"], "experiment_id": r["experiment_id"], "event_id": r["event_id"], "world_id": r["world_id"],
               "kind": r["kind"], "managed": True, "state": st, "terminal": st in TERMINAL, "attempt": int(r["attempt"]),
               "restart_count": int(r["restart_count"]), "tenant": r["tenant"], "priority_class": r["priority_class"], "split": r["split"],
               "ranked": bool(r["ranked"]), "ranked_valid": None if r["ranked_valid"] is None else bool(r["ranked_valid"]),
               "submitted_by": r["submitted_by"], "retention": r["retention"],
               "queued_unix_ns": r["queued_unix_ns"], "started_unix_ns": r["started_unix_ns"], "ended_unix_ns": r["ended_unix_ns"],
               "created_unix_ns": r["queued_unix_ns"], "updated_unix_ns": r["updated_unix_ns"],
               "queue_position": pos.get(r["run_id"]), "blocked_by": r["blocked_by"].split(",") if r["blocked_by"] else [],
               "reason": reason_view(r["reason_code"], None if r["reason_json"] is None else json.loads(r["reason_json"])),
               "cancel": None if not rq else {"requested_unix_ns": r["cancel_requested_unix_ns"], "by": r["cancel_by"],
                                              "deadline_unix_ns": None if st in TERMINAL else r["cancel_requested_unix_ns"] + self.cancel_timeout_ns},
               "actions": {"cancel": st == "QUEUED" or (st in CANCEL_REQUESTABLE and not rq), "retry": st in RETRYABLE},
               "run_hash": r["run_hash"], "seed_commit": r["seed_commit"] or None, **_spec_summary(spec)}
        return out

    def ids(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT run_id FROM runs")}

    def get(self, run_id: str) -> dict | None:
        r = self._row(run_id)
        return None if r is None else self._view(r, self._positions())

    def listing(self, *, ids: Iterable[str] | None = None, event: str | None = None, experiment: str | None = None,
                states: Iterable[str] | None = None, ranked: bool | None = None) -> list[dict]:
        q, args = [], []
        if ids is not None:
            ids = list(ids)
            q.append(f"run_id IN ({', '.join('?' * len(ids))})" if ids else "0")
            args += ids
        if event is not None:
            q.append("event_id = ?")
            args.append(event)
        if experiment is not None:
            q.append("experiment_id = ?")
            args.append(experiment)
        if states is not None:
            states = list(states)
            q.append(f"state IN ({', '.join('?' * len(states))})" if states else "0")
            args += states
        if ranked is not None:
            q.append("ranked = ?")
            args.append(int(ranked))
        sql = f"SELECT {', '.join(_COLS)} FROM runs" + (f" WHERE {' AND '.join(q)}" if q else "") + " ORDER BY event_id, run_id"
        pos = self._positions()
        return [self._view(r, pos) for r in self._db.execute(sql, args).fetchall()]

    def attempts(self, run_id: str) -> list[dict]:
        rows = self._db.execute("SELECT attempt, state, reason_code, reason_json, dir, started_unix_ns, ended_unix_ns, restart_count "
                                "FROM attempts WHERE run_id = ? ORDER BY attempt", (run_id,)).fetchall()
        return [{"attempt": a["attempt"], "state": a["state"], "restart_count": a["restart_count"], "dir": a["dir"] or None,
                 "started_unix_ns": a["started_unix_ns"], "ended_unix_ns": a["ended_unix_ns"],
                 "reason": reason_view(a["reason_code"], None if a["reason_json"] is None else json.loads(a["reason_json"]))} for a in rows]

    def transitions(self, run_id: str, limit: int = 100) -> list[dict]:
        rows = self._db.execute("SELECT attempt, from_state, to_state, reason_code, actor, t_unix_ns FROM run_transitions WHERE run_id = ? "
                                "ORDER BY id DESC LIMIT ?", (run_id, int(limit))).fetchall()
        return [{"attempt": t["attempt"], "from": t["from_state"], "to": t["to_state"], "reason_code": t["reason_code"], "actor": t["actor"],
                 "t_unix_ns": t["t_unix_ns"]} for t in reversed(rows)]
