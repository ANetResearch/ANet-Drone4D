"""内部提交（AWR-17 §18.23；BN-85、95、96、98、99；M27 §6.7.9；D3-AC-56）：存储、状态机、配额与可见性。

存储：`<data_root>/submissions/<submission_id>.json`，内容为 `bench/submission.v1` 字段加服务端字段 `_meta{owner, team,
created_unix_ns, updated_unix_ns, done_unix_ns?, board_version, history[]}`（`_meta` 不经接口返回）。
状态机：RECEIVED → SCANNING → VALIDATING → EVALUATING → SCORING → DONE；扫描或冒烟失败为 REJECTED（692 `scan`、`smoke`），
平台故障为 FAILED；RECEIVED、SCANNING、VALIDATING 可撤回为 WITHDRAWN（EVALUATING 起不可撤回，680）。
可见性：`private`（缺省）或 `board`；终态 REJECTED、FAILED、WITHDRAWN 不可切换（680）。
配额：每团队同时未终结的提交 ≤ `ACTIVE_PER_TEAM`（681 `submit_quota`）。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from pathlib import Path

__all__ = ["ACTIVE_PER_TEAM", "ACTIVE_STATES", "TERMINAL", "SubmissionError", "SubmissionStore", "public_view"]

ACTIVE_PER_TEAM = 3
ACTIVE_STATES = ("RECEIVED", "SCANNING", "VALIDATING", "EVALUATING", "SCORING")
TERMINAL = ("DONE", "REJECTED", "FAILED", "WITHDRAWN")
WITHDRAWABLE = ("RECEIVED", "SCANNING", "VALIDATING")
NEXT = {"RECEIVED": "SCANNING", "SCANNING": "VALIDATING", "VALIDATING": "EVALUATING", "EVALUATING": "SCORING", "SCORING": "DONE"}


class SubmissionError(Exception):
    def __init__(self, code: int, why: str | None = None, **detail) -> None:
        super().__init__(f"{code} {why or ''}".strip())
        self.code, self.detail = code, ({"why": why, **detail} if why else detail)


def public_view(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


class SubmissionStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

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

    # ------------------------------------------------------------ 变更
    def create(self, req: dict, *, owner: str, team: str, board_version: str, tracks: set[str], lanes: set[str]) -> dict:
        if req["track"] not in tracks:
            raise SubmissionError(682, "track_unknown", track=req["track"])
        bad = [ln for ln in req.get("lanes") or [] if ln not in lanes]
        if bad:
            raise SubmissionError(682, "lane_unknown", lanes=bad)
        with self.lock:
            active = [d for d in self.all() if d["_meta"]["team"] == team and d["state"] in ACTIVE_STATES]
            if len(active) >= ACTIVE_PER_TEAM:
                raise SubmissionError(681, "submit_quota", limit=ACTIVE_PER_TEAM)
            now = time.time_ns()
            sid = "sub-" + base64.b32encode(secrets.token_bytes(10)).decode().lower()[:16]
            doc = {"schema": "drone4d.bench.submission.v1", "submission_id": sid, "track": req["track"], "image_digest": req["image_digest"],
                   "entry": req["entry"], "notes": req.get("notes", ""), "state": "RECEIVED", "visibility": "private", "position": 0,
                   "_meta": {"owner": owner, "team": team, "created_unix_ns": now, "updated_unix_ns": now, "board_version": board_version,
                             "history": [{"state": "RECEIVED", "t_unix_ns": now}]}}
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
