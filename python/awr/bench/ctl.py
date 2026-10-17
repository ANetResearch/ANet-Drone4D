"""bench 管理命令（AWR-17 §18.3.1 第 1 条）：`python -m awr.bench.ctl token issue --principal <name> --role <role> [--projects a,b]`、
`token revoke <id>`、`token list`。明文令牌只在签发时输出一次（stdout），服务端只存 sha256。"""

from __future__ import annotations

import argparse
import json
import sys

from awr.bench.auth import ROLES, BenchAuth
from awr.bench.settings import BenchSettings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.bench.ctl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("token").add_subparsers(dest="op", required=True)
    i = t.add_parser("issue")
    i.add_argument("--principal", required=True)
    i.add_argument("--role", required=True, choices=ROLES)
    i.add_argument("--projects", default="")
    r = t.add_parser("revoke")
    r.add_argument("id")
    t.add_parser("list")
    a = ap.parse_args(argv)
    s = BenchSettings.from_env()
    auth = BenchAuth(s.keys_dir, s.db_path)
    if a.op == "issue":
        tid, tok = auth.issue_token(a.principal, a.role, tuple(p for p in a.projects.split(",") if p))
        print(f"id {tid}", file=sys.stderr)
        print(tok)
    elif a.op == "revoke":
        if not auth.revoke_token(a.id):
            print(f"no token {a.id}", file=sys.stderr)
            return 1
    else:
        print(json.dumps(auth.list_tokens(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
