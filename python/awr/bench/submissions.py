"""内部提交（AWR-17 §18.23；BN-85、95、96、98、99；M27 §6.7.9；D3-AC-56、59）：存储、状态机、配额、墙钟上限与可见性。

存储：`<data_root>/submissions/<submission_id>.json`，内容为 `bench/submission.v1` 字段加服务端字段 `_meta{owner, team, role, kind,
created_unix_ns, updated_unix_ns, done_unix_ns?, board_version, history[], gpu[]}`（`_meta` 不经接口返回）。
状态机：RECEIVED → SCANNING → VALIDATING → EVALUATING → SCORING → DONE；扫描或冒烟失败为 REJECTED（692 `scan`、`smoke`），
平台故障为 FAILED；RECEIVED、SCANNING、VALIDATING 可撤回为 WITHDRAWN（EVALUATING 起不可撤回，680）。
可见性：`private`（缺省）或 `board`；终态 REJECTED、FAILED、WITHDRAWN 不可切换（680）。

配额（§5.6.7、Q-D3-09、M27-FR-169；D3-AC-59"超出每周正式提交数或 GPU 时间上限返回 681"）：`Quota` 缺省值取 M27 §配置表
`submit.quota`，管理员以 `submissions/quota.json` 调整（每次登记时重读，可按 `team:<id>`、`user:<name>` 覆写）。

- 计量主体：受邀成员（`invited`）按团队，内部成员（`researcher` 及以上）按本人；GPU 时间一律按团队；
- 正式提交（`kind = formal`）：每 UTC 自然周（周一 00:00 UTC 起）受邀成员每团队 ≤ 2 次、内部成员每人 ≤ 5 次；计入处于受理至评分
  阶段与 DONE 的提交，撤回（WITHDRAWN）、未通过扫描或冒烟（REJECTED）与平台故障（FAILED）不计入；
- 冒烟验证（`kind = smoke`，开发榜 `track.dev` 上的提交）：每人每 UTC 日 ≤ 10 次，撤回与平台故障不计入；
- 同时排队：未终结的提交受邀成员每团队、内部成员每人 ≤ 2 个；
- GPU 时间：每团队每 UTC 自然周 ≤ 40 GPU·h；运行结束时由流水线经 `charge_gpu` 记账（实测值，或运行墙钟 × 正式评测预留份额 1/3），
  已用量达到上限后新的登记返回 681；
- 超额：681 `submit_quota`（`detail.quota ∈ {queued, formal_week, smoke_day}`）或 `gpu_hours`，`detail` 带上限、用量与恢复时刻。

墙钟上限（§5.6.7 容器加固、M27-FR-170）：单 Run 墙钟上限 `10 × duration_s + 600 s`（`wall_limit_s`），超限时 `guarded_run` 以
`WallLimitExceeded` 终止该 Run（693 `RUN_WALL_LIMIT_EXCEEDED`）：Env 照常关闭并写运行清单、Agent 容器停止，运行目录写
`termination.json`，已用 GPU 时间计入提交方团队。
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import datetime as dt
import json
import os
import secrets
import signal
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields, replace
from pathlib import Path

__all__ = ["ACTIVE_PER_TEAM", "ACTIVE_STATES", "EVAL_GPU_SHARE", "TERMINAL", "Quota", "SubmissionError", "SubmissionStore", "WallLimitExceeded",
           "day_start_ns", "guarded_run", "public_view", "run_duration_s", "wall_limit", "wall_limit_s", "week_start_ns"]

ACTIVE_STATES = ("RECEIVED", "SCANNING", "VALIDATING", "EVALUATING", "SCORING")
TERMINAL = ("DONE", "REJECTED", "FAILED", "WITHDRAWN")
WITHDRAWABLE = ("RECEIVED", "SCANNING", "VALIDATING")
NEXT = {"RECEIVED": "SCANNING", "SCANNING": "VALIDATING", "VALIDATING": "EVALUATING", "EVALUATING": "SCORING", "SCORING": "DONE"}
KINDS = ("formal", "smoke")
FORMAL_COUNTED = (*ACTIVE_STATES, "DONE")                # 计入每周正式提交数的状态
SMOKE_UNCOUNTED = ("WITHDRAWN", "FAILED")                # 不计入每日冒烟次数的状态
WALL_FACTOR, WALL_MARGIN_S = 10.0, 600.0                 # 单 Run 墙钟上限 = 仿真时长 × 10 + 10 min（M27-FR-170）
EVAL_GPU_SHARE = 1.0 / 3.0                               # 正式评测每 Run 预留的卡 GPU 时间份额（awr.bench.scheduler.SchedulerConfig.eval_share）
_DAY_NS = 86_400 * 1_000_000_000
_WEEK_NS = 7 * _DAY_NS


class SubmissionError(Exception):
    def __init__(self, code: int, why: str | None = None, **detail) -> None:
        super().__init__(f"{code} {why or ''}".strip())
        self.code, self.detail = code, ({"why": why, **detail} if why else detail)


@dataclass(frozen=True)
class Quota:
    """提交配额（M27 §配置表 `submit.quota`；Q-D3-09 v1.3）。"""
    invited_per_team_week: int = 2
    internal_per_user_week: int = 5
    smoke_per_user_day: int = 10
    queued: int = 2
    gpu_h_per_team_week: float = 40.0

    @classmethod
    def load(cls, path: Path, *, team: str | None = None, user: str | None = None) -> Quota:
        """`quota.json`（管理员维护）：顶层键覆写缺省值，`overrides{"team:<id>" | "user:<name>": {…}}` 再按主体覆写；文件缺失时为缺省值。"""
        q = cls()
        p = Path(path)
        if not p.is_file():
            return q
        doc = json.loads(p.read_text(encoding="utf-8"))
        names = {f.name for f in fields(cls)}
        layers = [doc, *((doc.get("overrides") or {}).get(k) or {} for k in (f"team:{team}" if team else "", f"user:{user}" if user else "") if k)]
        for layer in layers:
            q = replace(q, **{k: type(getattr(q, k))(v) for k, v in layer.items() if k in names})
        return q


ACTIVE_PER_TEAM = Quota().queued                         # 同时排队上限（兼容既有调用方）


def week_start_ns(t_ns: int) -> int:
    """`t_ns` 所在 UTC 自然周的起点（周一 00:00 UTC）。"""
    d = dt.datetime.fromtimestamp(t_ns // 1_000_000_000, dt.UTC).date()      # 整数秒：避免浮点把周界前 1 ns 舍入到周界
    monday = dt.datetime.combine(d - dt.timedelta(days=d.weekday()), dt.time(), dt.UTC)
    return int(monday.timestamp()) * 1_000_000_000


def day_start_ns(t_ns: int) -> int:
    """`t_ns` 所在 UTC 日的起点。"""
    return t_ns - t_ns % _DAY_NS


def _kind(doc: dict) -> str:
    return doc["_meta"].get("kind", "formal")                # 早于 v1.6 的记录无 kind：按正式提交计


def public_view(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


class SubmissionStore:
    def __init__(self, root: Path, *, clock: Callable[[], int] = time.time_ns) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.clock = clock

    # ------------------------------------------------------------ 读写
    def _path(self, sid: str) -> Path:
        if not sid.startswith("sub-") or not sid[4:].isalnum():
            raise SubmissionError(305)
        return self.root / f"{sid}.json"

    def get(self, sid: str) -> dict:
        p = self._path(sid)
        if not p.exists():
            raise SubmissionError(305)
        return json.loads(p.read_text(encoding="utf-8"))

    def _put(self, doc: dict) -> None:
        p = self._path(doc["submission_id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, p)

    def all(self) -> list[dict]:
        docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("sub-*.json"))]
        return sorted(docs, key=lambda d: (d["_meta"]["created_unix_ns"], d["submission_id"]))

    @staticmethod
    def _positions(docs: list[dict]) -> None:
        """排队位置：RECEIVED 按创建先后排在正在处理的提交之后（0 = 正在处理或已终结）。"""
        busy = sum(d["state"] in ACTIVE_STATES and d["state"] != "RECEIVED" for d in docs)
        waiting = [d["submission_id"] for d in docs if d["state"] == "RECEIVED"]
        for d in docs:
            d["position"] = busy + waiting.index(d["submission_id"]) + 1 if d["state"] == "RECEIVED" else 0

    def listing(self, *, team: str | None = None, track: str | None = None, state: str | None = None) -> list[dict]:
        docs = self.all()
        self._positions(docs)
        return [d for d in docs if (team is None or d["_meta"]["team"] == team) and (track is None or d["track"] == track)
                and (state is None or d["state"] == state)]

    def view(self, sid: str) -> dict:
        docs = self.all()
        self._positions(docs)
        for d in docs:
            if d["submission_id"] == sid:
                return d
        raise SubmissionError(305)

    # ------------------------------------------------------------ 配额
    def quota(self, *, team: str | None = None, user: str | None = None) -> Quota:
        return Quota.load(self.root / "quota.json", team=team, user=user)

    def quota_status(self, *, owner: str, team: str, role: str, now_ns: int | None = None, docs: list[dict] | None = None) -> dict:
        """某主体当前的配额用量：`{subject, queued, formal_week, smoke_day, gpu_hours}`，每项 `{used, limit, resets_unix_ns?}`。

        受邀成员（`role = invited`）的提交数按团队计量，内部成员按本人；GPU 时间按团队。"""
        now = self.clock() if now_ns is None else now_ns
        docs = self.all() if docs is None else docs
        invited = role == "invited"
        q = self.quota(team=team, user=None if invited else owner)
        mine = [d for d in docs if (d["_meta"]["team"] == team if invited else d["_meta"]["owner"] == owner)]
        w0, d0 = week_start_ns(now), day_start_ns(now)
        formal = sum(_kind(d) == "formal" and d["state"] in FORMAL_COUNTED and d["_meta"]["created_unix_ns"] >= w0 for d in mine)
        smoke = sum(_kind(d) == "smoke" and d["state"] not in SMOKE_UNCOUNTED and d["_meta"]["created_unix_ns"] >= d0 for d in mine)
        gpu_s = sum(c["gpu_s"] for d in docs if d["_meta"]["team"] == team for c in d["_meta"].get("gpu", ()) if c["t_unix_ns"] >= w0)
        return {"subject": f"team:{team}" if invited else f"user:{owner}",
                "queued": {"used": sum(d["state"] in ACTIVE_STATES for d in mine), "limit": q.queued},
                "formal_week": {"used": formal, "limit": q.invited_per_team_week if invited else q.internal_per_user_week, "resets_unix_ns": str(w0 + _WEEK_NS)},
                "smoke_day": {"used": smoke, "limit": q.smoke_per_user_day, "resets_unix_ns": str(d0 + _DAY_NS)},
                "gpu_hours": {"used": round(gpu_s / 3600.0, 4), "limit": q.gpu_h_per_team_week, "resets_unix_ns": str(w0 + _WEEK_NS)}}

    @staticmethod
    def quota_error(status: dict, kind: str = "formal", *, queued: bool = True) -> SubmissionError | None:
        """配额判定：→ 681 的 SubmissionError（`submit_quota` 或 `gpu_hours`），未超额为 None；`queued = False` 时不判同时排队数。"""
        for key in (("queued",) if queued else ()) + ("smoke_day" if kind == "smoke" else "formal_week",):
            x = status[key]
            if x["used"] >= x["limit"]:
                extra = {"resets_unix_ns": x["resets_unix_ns"]} if "resets_unix_ns" in x else {}
                return SubmissionError(681, "submit_quota", quota=key, limit=x["limit"], used=x["used"], subject=status["subject"], **extra)
        g = status["gpu_hours"]
        if g["used"] >= g["limit"]:
            return SubmissionError(681, "gpu_hours", limit_gpu_h=g["limit"], used_gpu_h=g["used"], subject=status["subject"],
                                   resets_unix_ns=g["resets_unix_ns"])
        return None

    # ------------------------------------------------------------ 变更
    def create(self, req: dict, *, owner: str, team: str, board_version: str, tracks: set[str], lanes: set[str], role: str = "researcher",
               kind: str = "formal") -> dict:
        """登记一个提交。`role` 为提交者角色（决定配额主体），`kind` 为 `formal`（正式榜）或 `smoke`（开发榜冒烟验证）。"""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if req["track"] not in tracks:
            raise SubmissionError(682, "track_unknown", track=req["track"])
        bad = [ln for ln in req.get("lanes") or [] if ln not in lanes]
        if bad:
            raise SubmissionError(682, "lane_unknown", lanes=bad)
        with self.lock:
            now = self.clock()
            err = self.quota_error(self.quota_status(owner=owner, team=team, role=role, now_ns=now), kind)
            if err is not None:
                raise err
            sid = "sub-" + base64.b32encode(secrets.token_bytes(10)).decode().lower()[:16]
            doc = {"schema": "drone4d.bench.submission.v1", "submission_id": sid, "track": req["track"], "image_digest": req["image_digest"],
                   "entry": req["entry"], "notes": req.get("notes", ""), "state": "RECEIVED", "visibility": "private", "position": 0,
                   "_meta": {"owner": owner, "team": team, "role": role, "kind": kind, "created_unix_ns": now, "updated_unix_ns": now,
                             "board_version": board_version, "history": [{"state": "RECEIVED", "t_unix_ns": now}], "gpu": []}}
            if req.get("lanes"):
                doc["lanes"] = list(req["lanes"])
            self._put(doc)
        return self.view(sid)

    def transition(self, sid: str, state: str, *, reason: dict | None = None, result: dict | None = None, expect: str | None = None) -> dict:
        with self.lock:
            d = self.get(sid)
            if expect is not None and d["state"] != expect:
                raise SubmissionError(680, "state", state=d["state"])
            if d["state"] in TERMINAL:
                raise SubmissionError(680, "state", state=d["state"])
            now = time.time_ns()
            d["state"] = state
            d["_meta"]["updated_unix_ns"] = now
            d["_meta"]["history"].append({"state": state, "t_unix_ns": now})
            if state == "DONE":
                d["_meta"]["done_unix_ns"] = now
            if reason is not None:
                d["reason"] = reason
            if result is not None:
                d["result"] = result
            self._put(d)
            return d

    def charge_gpu(self, sid: str, gpu_s: float, *, run_id: str) -> dict:
        """GPU 时间记账（秒）：追加到 `_meta.gpu[]`，按记账时刻计入提交方团队的当周用量。"""
        with self.lock:
            d = self.get(sid)
            d["_meta"].setdefault("gpu", []).append({"t_unix_ns": self.clock(), "run_id": run_id, "gpu_s": round(max(float(gpu_s), 0.0), 3)})
            self._put(d)
            return d

    def withdraw(self, sid: str) -> dict:
        with self.lock:
            d = self.get(sid)
            if d["state"] not in WITHDRAWABLE:
                raise SubmissionError(680, "state", state=d["state"])
        return self.transition(sid, "WITHDRAWN", expect=d["state"])

    def set_visibility(self, sid: str, visibility: str) -> dict:
        if visibility not in ("private", "board"):
            raise SubmissionError(682, "visibility")
        with self.lock:
            d = self.get(sid)
            if d["state"] in ("REJECTED", "FAILED", "WITHDRAWN"):
                raise SubmissionError(680, "state", state=d["state"])
            d["visibility"] = visibility
            d["_meta"]["updated_unix_ns"] = time.time_ns()
            self._put(d)
            return d

    def board_view(self) -> dict[str, dict]:
        """boardstore.publish 所需：submission_id → {state, visibility, team, done_unix_ns}。"""
        return {d["submission_id"]: {"state": d["state"], "visibility": d["visibility"], "team": d["_meta"]["team"],
                                     "done_unix_ns": d["_meta"].get("done_unix_ns", 0)} for d in self.all()}

    def next_received(self) -> dict | None:
        for d in self.all():
            if d["state"] == "RECEIVED":
                return d
        return None


# ── 墙钟上限（693） ──────────────────────────────────────────────────────────────────────────────────────────────────

def wall_limit_s(duration_s: float) -> float:
    """单 Run 墙钟上限（秒）：`10 × duration_s + 600`（M27-FR-170）。"""
    return WALL_FACTOR * float(duration_s) + WALL_MARGIN_S


def run_duration_s(rs: dict, events_root: Path) -> float:
    """Run 的仿真时长：Run 规格的 `duration_s`，缺省为事件时长（`<events_root>/<id>/v<version>/plan.json` 的 `duration_ms`，与 Env 同一口径）；
    事件不可读时为 0（墙钟上限退化为 600 s，Env 也将无法启动）。"""
    if rs.get("duration_s"):
        return float(rs["duration_s"])
    ev = rs.get("event") or {}
    try:
        plan = json.loads((Path(events_root) / str(ev["id"]) / f"v{ev.get('version', 1)}" / "plan.json").read_text(encoding="utf-8"))
        return float(plan["duration_ms"]) / 1000.0
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


class WallLimitExceeded(BaseException):
    """Run 超过墙钟上限（693）。继承 BaseException（与 asyncio.CancelledError 同理）：Env、物理后端与容器管理中的
    `except Exception` 不会吞掉终止信号，各层 finally 照常关闭 Env、写运行清单并停止 Agent 容器。"""

    code = 693

    def __init__(self, limit_s: float = 0.0, elapsed_s: float | None = None) -> None:
        super().__init__(f"693 run wall limit {limit_s:g} s exceeded")
        self.limit_s, self.elapsed_s = float(limit_s), elapsed_s

    def reason(self, **detail) -> dict:
        d = {"why": "wall_limit", "limit_s": round(self.limit_s, 3), **detail}
        if self.elapsed_s is not None:
            d["elapsed_s"] = round(self.elapsed_s, 3)
        return {"code": 693, "name": "RUN_WALL_LIMIT_EXCEEDED", "detail": d}


@contextlib.contextmanager
def wall_limit(limit_s: float) -> Iterator[None]:
    """在 `limit_s` 秒后于当前线程抛出 `WallLimitExceeded`。主线程以 `ITIMER_REAL` 与 SIGALRM 实现（可打断阻塞的套接字读写、
    `time.sleep` 与子进程等待；退出时恢复原有处理器与计时器）；其他线程以异步异常实现（只在执行 Python 字节码时生效）。"""
    if limit_s <= 0:
        raise ValueError("limit_s must be > 0")
    t0 = time.monotonic()
    if threading.current_thread() is threading.main_thread():
        def _fire(_signum, _frame):
            raise WallLimitExceeded(limit_s)

        old = signal.signal(signal.SIGALRM, _fire)
        old_timer = signal.setitimer(signal.ITIMER_REAL, limit_s)
        try:
            yield
        except WallLimitExceeded as e:
            raise WallLimitExceeded(limit_s, time.monotonic() - t0) from e
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
            if old_timer[0] > 0:                                     # 外层计时器扣除本段已用时间后继续
                signal.setitimer(signal.ITIMER_REAL, max(old_timer[0] - (time.monotonic() - t0), 1e-3), old_timer[1])
        return
    tid = threading.get_ident()
    done = threading.Event()

    def _async_fire() -> None:
        if not done.is_set():
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), ctypes.py_object(WallLimitExceeded))

    timer = threading.Timer(limit_s, _async_fire)
    timer.daemon = True
    timer.start()
    try:
        yield
    except WallLimitExceeded as e:
        raise WallLimitExceeded(limit_s, time.monotonic() - t0) from e
    finally:
        done.set()
        timer.cancel()


def guarded_run(run_job: Callable[..., dict], store: SubmissionStore, sid: str, *args, limit_s: float, gpu_share: float = EVAL_GPU_SHARE,
                **kw) -> dict:
    """在墙钟上限内执行一个提交的 Run（`run_job(*args, **kw)`，须含 `run_id`、`run_dir`），并把 GPU 时间计入提交方团队。

    GPU 时间：结果带 `gpu_s`（实测）时取之，否则为运行墙钟 × `gpu_share`。超过上限时写 `<run_dir>/termination.json`、按已用墙钟记账
    并重新抛出 `WallLimitExceeded`（693）；其他异常（平台原因）不记账，原样抛出。"""
    run_id, run_dir = str(kw["run_id"]), Path(kw["run_dir"])
    t0 = time.monotonic()
    try:
        with wall_limit(limit_s):
            res = run_job(*args, **kw)
    except WallLimitExceeded as e:
        el = time.monotonic() - t0
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "termination.json").write_text(json.dumps({"run_id": run_id, **e.reason(stage="run")}, indent=1) + "\n", encoding="utf-8")
        store.charge_gpu(sid, el * gpu_share, run_id=run_id)
        raise
    el = time.monotonic() - t0
    gpu_s = res.get("gpu_s") if isinstance(res, dict) else None
    store.charge_gpu(sid, float(gpu_s) if gpu_s is not None else float((res or {}).get("run_wall_s", el)) * gpu_share, run_id=run_id)
    return res
