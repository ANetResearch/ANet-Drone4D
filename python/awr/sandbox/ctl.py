"""运维 CLI：`python -m awr.sandbox.ctl {status, list, stop <sid>, drain [--notice s], accept on|off, limits [--max-sessions n|auto]}`
（M17-FR-065，P1；M17 PRD §8.5）。

读取 `var/sandbox/pool.secret`（只有 awr 用户与 root 可读，0600）派生 `K_pool`，以 principal `svc:sandbox-ctl` 签名，经池命名空间
`awr/_sandbox/pool` 调用 `sys/session/ops`（运维）与 `sys/session/list`（只读）。池只允许该 principal 调用运维与只读操作。
连接参数：`AWR_RENDEZVOUS`（或 `--connect`）；状态目录：`AWR_SANDBOX_STATE_DIR`（或 `--state-dir`，缺省按 `sandbox:` 段）。
退出码：0 成功；1 池拒绝；2 参数或密钥错误；3 池不可达。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from awr.contracts import bus_keys
from awr.runtime.bus import BusError, open_bus
from awr.runtime.sbauth import POOL_SECRET_NAME, pool_key, sign_pool_request

__all__ = ["main", "run"]

CTL_PRINCIPAL = "svc:sandbox-ctl"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m awr.sandbox.ctl", description="sandbox-pool 运维")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--connect", default=None, help="zenoh 汇合点（缺省 AWR_RENDEZVOUS）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("list")
    p = sub.add_parser("stop")
    p.add_argument("sid")
    p = sub.add_parser("drain")
    p.add_argument("--notice", type=float, default=60.0)
    p = sub.add_parser("accept")
    p.add_argument("onoff", choices=["on", "off"])
    p = sub.add_parser("limits")
    p.add_argument("--max-sessions", default="auto")
    return ap


def _state_dir(a: argparse.Namespace) -> Path:
    if a.state_dir:
        return Path(a.state_dir)
    if os.environ.get("AWR_SANDBOX_STATE_DIR"):
        return Path(os.environ["AWR_SANDBOX_STATE_DIR"])
    from awr.runtime.config import sandbox_from_env

    return sandbox_from_env().state_path()


def _request(a: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if a.cmd == "list":
        return "list", {}
    if a.cmd == "status":
        return "ops", {"cmd": "status"}
    if a.cmd == "stop":
        return "ops", {"cmd": "stop", "sid": a.sid}
    if a.cmd == "drain":
        return "ops", {"cmd": "drain", "notice_s": float(a.notice)}
    if a.cmd == "accept":
        return "ops", {"cmd": "accept", "on": a.onoff == "on"}
    ms = a.max_sessions
    return "ops", {"cmd": "limits", "max_sessions": "auto" if ms == "auto" else int(ms)}


def _print(a: argparse.Namespace, rep: dict) -> None:
    res = rep.get("result")
    if a.json or a.cmd != "status" or not isinstance(res, dict):
        print(json.dumps(res if rep.get("status") != "rejected" else rep, ensure_ascii=False, indent=1))
        return
    print(f"N_max {res.get('n_max')}  Q {res.get('q_cores')} 核  委派 {res.get('delegated')}  接受 {res.get('accepting')}  "
          f"降级 {res.get('degrade_level')}  排队 {res.get('queue_len')}")
    for s in res.get("sessions") or []:
        print(f"  {s['sid']}  {s['state']:<9} {s['world']:<13} 剩余 {s['expires_in_s']:>7.0f} s  ×{s['granted_rate']:<5} "
              f"匿名页 {s['anon_mb']:>6.1f} MB  ws {s['ws']}  epoch {s['epoch']}")


async def run(argv: list[str] | None = None, *, bus: Any = None) -> int:
    a = _parser().parse_args(argv)
    try:
        secret = (_state_dir(a) / POOL_SECRET_NAME).read_bytes()
    except OSError as e:
        print(f"无法读取 pool.secret：{e}", file=sys.stderr)
        return 2
    op, args = _request(a)
    msg = sign_pool_request(pool_key(secret), op, args, principal=CTL_PRINCIPAL, now_s=time.time())
    own = bus is None
    if own:
        rv = a.connect or os.environ.get("AWR_RENDEZVOUS")
        bus = open_bus("sandbox-ctl", None, kind="zenoh", namespace=bus_keys.POOL_NAMESPACE, announce=False,
                       **({"connect": [rv]} if rv else {}))
    try:
        rep = await bus.call(bus_keys.sys_session(op), msg, timeout=3.0, retries=1)
    except BusError as e:
        print(f"池不可达：{e}", file=sys.stderr)
        return 3
    finally:
        if own:
            bus.close()
    if not isinstance(rep, dict):
        return 3
    _print(a, rep)
    return 1 if rep.get("status") == "rejected" else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
