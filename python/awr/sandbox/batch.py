"""本机与局域网的批处理评估（M17-FR-048、AC-024，P1；AWR-04 §4.10 第 3 条；17 §17.6.10；SB-18、SB-19）。

`POST /sessions/{sid}/batch{until_s, config_snapshot?, seed?}`：以请求给出的配置快照（缺省为该会话当前快照）另起
`python -m awr.sim.runtime --batch --until <s> --config <file> --out <dir> --clock sandbox100 --world <w>`（M08-FR-221）进程，
同时至多 `batch.concurrency`（1）个，不占槽位，`nice +10`；忙时 409 `514 SANDBOX_BATCH_BUSY`。只在 `sandbox.batch.enabled`
且非公开配额档时可用（公开站 404 `305`）。`GET .../batch/{bid}` 取进度（由批处理日志的 `batch progress` 行估计）与结果
（`relay_stats.json` 的 items）。批处理记录只在池内存中保留（池重启后不可查询，P1 的简化）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awr.runtime.procutil import spawn_logged, write_private

from .broker import err, ok
from .model import ACTIVE, IDLE, READY

if TYPE_CHECKING:
    from .pool import Pool

__all__ = ["batch_op", "cancel_all"]

log = logging.getLogger("awr.sandbox.batch")

B32 = "abcdefghijklmnopqrstuvwxyz234567"


def _bid() -> str:
    return "bt-" + "".join(secrets.choice(B32) for _ in range(8))


def _registry(pool: Pool) -> dict[str, dict]:
    reg = getattr(pool, "batches", None)
    if reg is None:
        reg = {}
        pool.batches = reg  # type: ignore[attr-defined]
    return reg


def _progress(d: dict) -> float:
    p = Path(d["dir"]) / "batch.log"
    best = 0.0
    with contextlib.suppress(OSError):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
            if "batch progress" not in line:
                continue
            with contextlib.suppress(ValueError, KeyError, TypeError):
                kv = json.loads(line).get("kv") or {}
                best = max(best, float(kv.get("t_sim_h", 0)) * 3600.0)
    return min(100.0, 100.0 * best / max(1e-6, float(d["until_s"])))


def _status(d: dict) -> dict[str, Any]:
    out: dict[str, Any] = {"batch_id": d["batch_id"], "state": d["state"],
                           "wall_s": round((d.get("t_end") or time.monotonic()) - d["t0"], 1)}
    if d["state"] == "done":
        out["progress_pct"] = 100.0
        out["sim_t_s"] = float(d["until_s"])
        rs: list = []
        with contextlib.suppress(OSError, ValueError):
            doc = json.loads((Path(d["dir"]) / "relay_stats.json").read_text(encoding="utf-8"))
            rs = list(doc.get("items") or []) if isinstance(doc, dict) else list(doc)
        out["result"] = {"relay_stats": rs}
    else:
        pct = _progress(d)
        out["progress_pct"] = round(pct, 1)
        out["sim_t_s"] = round(pct / 100.0 * float(d["until_s"]), 1)
        out["result"] = None
    return out


async def batch_op(pool: Pool, args: dict) -> dict[str, Any]:
    s = pool.s
    if not s.sb.batch.enabled or s.public:
        return err(404, 305, why="batch_disabled")
    reg = _registry(pool)
    op = str(args.get("op") or "get")
    sid = str(args.get("sid") or "")
    if op == "get":
        d = reg.get(str(args.get("batch_id") or ""))
        if d is None or d["sid"] != sid:
            return err(404, 305, batch_id=args.get("batch_id"))
        return ok(_status(d))
    if op != "submit":
        return err(422, 300, field="op")
    b = pool.broker
    assert b is not None
    rec = b.sessions.get(sid)
    if rec is None or rec.state not in (READY, ACTIVE, IDLE):
        return err(409, 513, state=rec.state if rec else None)
    running = [d for d in reg.values() if d["state"] in ("queued", "running")]
    if len(running) >= max(1, int(s.sb.batch.concurrency)):
        return err(409, 514, batch_id=running[0]["batch_id"])
    snap = args.get("config_snapshot")
    if snap is None:
        sb = pool.sbus.get(sid)
        snap = await sb.snapshot() if sb is not None else None
    if not isinstance(snap, dict):
        return err(503, 211, why="snapshot_unavailable", retry_after_s=2)
    bid = _bid()
    out = s.state_dir / "batch" / bid
    out.mkdir(parents=True, exist_ok=True)
    cfg = out / "config.json"
    write_private(cfg, json.dumps(snap, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    argv = [sys.executable, "-m", "awr.sim.runtime", "--batch", "--until", str(float(args.get("until_s") or 3600)),
            "--config", str(cfg), "--out", str(out), "--clock", "sandbox100", "--world", rec.world]
    if isinstance(args.get("seed"), int):
        argv += ["--seed", str(int(args["seed"]))]
    env = {k: v for k, v in (s.env or os.environ).items() if k not in ("AWR_SUPERVISOR_PID", "AWR_CGROUP")}
    env.update({"AWR_DETACHED": "1", "AWR_WORLDS_DIR": str(s.worlds_dir)})
    d = {"batch_id": bid, "sid": sid, "state": "running", "t0": time.monotonic(), "t_end": None,
         "until_s": float(args.get("until_s") or 3600), "dir": str(out)}
    try:
        proc = await spawn_logged(argv, env=env, log_path=out / "batch.log", nice=int(s.sb.planning.nice))
    except OSError as e:
        return err(503, 211, why="spawn", error=str(e))
    d["proc"] = proc
    reg[bid] = d
    pool.audit_write("batch.submit", {"sid": sid, "batch_id": bid, "until_s": d["until_s"]})

    async def wait() -> None:
        code = await proc.wait()
        d["state"] = "done" if code == 0 else "failed"
        d["t_end"] = time.monotonic()
        d["exit"] = code
        log.info("batch finished", extra={"kv": {"batch_id": bid, "exit": code}})

    pool.task(wait(), f"batch:{bid}")
    return ok(_status(d), 202)


def cancel_all(pool: Pool) -> None:
    """池停机：终止仍在运行的批处理进程（批处理不跨池重启保留）。"""
    for d in _registry(pool).values():
        proc = d.get("proc")
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
