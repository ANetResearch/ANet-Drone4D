"""bench 管理命令（AWR-17 §18.3.1 第 1 条）：`python -m awr.bench.ctl token issue --principal <name> --role <role> [--projects a,b]`、
`token revoke <id>`、`token list`。明文令牌只在签发时输出一次（stdout），服务端只存 sha256。

榜单与提交（运维用，数据根取 `AWR_BENCH_DATA_ROOT`）：
- `board publish <dir> [--version V] [--current]`：把评测轮次目录（proto_board 产出：config.json、rows.jsonl）发布为
  `boards/<V>` 并重算 board.json、details.json；开发榜（`track.dev`，Fast 档自检）不得 `--current`；评分版本或赛道集合与已发布版本
  不同则拒绝（须新建版本）；
- `board compose <V> --weights ec=…,eu=…,ao=…,ce=…`：以测试用评分版本的权重由存储的分量重算合成值与置信区间（不写文件、不重跑），
  输出 JSON；
- `submission create --team T --owner O --track S --digest sha256:… --cmd "…" [--env K=V …] [--visibility board]`：代成员登记
  内部提交（与 BN-95 相同的校验、配额与状态机），输出 submission_id。"""

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
    b = sub.add_parser("board").add_subparsers(dest="op", required=True)
    bp = b.add_parser("publish")
    bp.add_argument("dir")
    bp.add_argument("--version")
    bp.add_argument("--current", action="store_true")
    bp.add_argument("--n-boot", type=int, default=10_000)
    bc = b.add_parser("compose")
    bc.add_argument("version")
    bc.add_argument("--weights", required=True, help="ec=W,eu=W,ao=W,ce=W")
    bc.add_argument("--n-boot", type=int, default=10_000)
    sc = sub.add_parser("submission").add_subparsers(dest="op", required=True)
    c = sc.add_parser("create")
    c.add_argument("--team", required=True)
    c.add_argument("--owner", required=True)
    c.add_argument("--track", required=True)
    c.add_argument("--digest", required=True)
    c.add_argument("--cmd", dest="entry_cmd", required=True, help="entry command, whitespace separated")
    c.add_argument("--env", action="append", default=[], help="KEY=VALUE (repeatable)")
    c.add_argument("--notes", default="")
    c.add_argument("--visibility", choices=("private", "board"), default="private")
    a = ap.parse_args(argv)
    s = BenchSettings.from_env()
    if a.cmd == "board":
        return _board_publish(s, a) if a.op == "publish" else _board_compose(s, a)
    if a.cmd == "submission":
        return _submission_create(s, a)
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


def _board_publish(s: BenchSettings, a) -> int:
    import shutil
    from pathlib import Path

    from awr.bench.boardstore import BoardVersionError, load_config, publish
    from awr.bench.submissions import SubmissionStore

    src = Path(a.dir)
    v = a.version or src.name
    if a.current and load_config(src).get("track", {}).get("dev"):
        print(f"{src}: development board (track.dev) cannot be published as current", file=sys.stderr)
        return 2
    dst = s.data_root / "boards" / v
    dst.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dst.resolve():                                  # 指向已发布版本目录时原地重算
        for name in ("config.json", "rows.jsonl"):
            shutil.copyfile(src / name, dst / name)
    try:
        board = publish(dst, SubmissionStore(s.data_root / "submissions").board_view(), n_boot=a.n_boot)
    except BoardVersionError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.current:
        (s.data_root / "boards" / "current").write_text(v + "\n", encoding="utf-8")
    print(f"{v}: {len(board['entries'])} entries -> {dst}")
    return 0


def _board_compose(s: BenchSettings, a) -> int:
    from awr.bench.boardstore import compose_board, versions
    from awr.bench.submissions import SubmissionStore

    if a.version not in versions(s.data_root / "boards"):
        print(f"no board version {a.version}", file=sys.stderr)
        return 2
    try:
        w = {k: float(x) for k, x in (kv.split("=", 1) for kv in a.weights.split(","))}
        out = compose_board(s.data_root / "boards" / a.version, w, SubmissionStore(s.data_root / "submissions").board_view(), n_boot=a.n_boot)
    except (KeyError, ValueError) as e:
        print(f"weights: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def _submission_create(s: BenchSettings, a) -> int:
    from awr.agentsdk.schemas import bench_validator
    from awr.bench.boardstore import current_version, versions
    from awr.bench.submissions import SubmissionError, SubmissionStore

    env = dict(kv.split("=", 1) for kv in a.env)
    req = {"track": a.track, "image_digest": a.digest, "entry": {"cmd": a.entry_cmd.split(), **({"env": env} if env else {})}, "notes": a.notes}
    errs = [e.message for e in bench_validator("submission").iter_errors({"schema": "drone4d.bench.submission.v1", **req})]
    if errs:
        print("; ".join(errs[:3]), file=sys.stderr)
        return 2
    root = s.data_root / "boards"
    v = current_version(root, versions(root))
    if v is None:
        print("no board published (board_closed)", file=sys.stderr)
        return 2
    b = json.loads((root / v / "board.json").read_text(encoding="utf-8"))
    st = SubmissionStore(s.data_root / "submissions")
    try:
        d = st.create(req, owner=a.owner, team=a.team, board_version=v, tracks={t["track"] for t in b["tracks"]}, lanes={x["lane"] for x in b["lanes"]})
        if a.visibility == "board":
            st.set_visibility(d["submission_id"], "board")
    except SubmissionError as e:
        print(f"{e.code} {e.detail}", file=sys.stderr)
        return 1
    print(d["submission_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
