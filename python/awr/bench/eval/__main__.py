"""`python -m awr.bench.eval run <run_dir> --event <dir> --world <dir>` 与 `board <evaluation.json...> --out board.json`。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.bench.eval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="evaluate one run directory (bench/evaluation.v1)")
    p.add_argument("run_dir", type=Path)
    p.add_argument("--event", type=Path, required=True, help="event version directory (plan.json, tape.bin)")
    p.add_argument("--world", type=Path, required=True, help="world package directory")
    p.add_argument("--out", type=Path, help="default <run_dir>/evaluation.json")
    p = sub.add_parser("board", help="aggregate evaluations into a prototype board (bench/board.v1)")
    p.add_argument("index", type=Path, help="JSON list of {entry_id, team, display_name, baseline, track, lane, event, evaluation}")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--board-version", default="proto-0")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        from awr.agentsdk.env.core import _open_world
        from awr.bench.eval.evaluator import evaluate_run

        diag: dict = {}
        doc = evaluate_run(a.run_dir, a.event, _open_world(a.world), diag=diag)
        out = a.out or a.run_dir / "evaluation.json"
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        out.with_name(out.stem + ".diag.json").write_text(json.dumps(diag, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        c = doc["components"]
        print(f"{doc['run_id']}: EC {c['ec']:.3f}  EU {c['eu']:.3f}  AO {c['ao']:.3f}  CE {c['ce'] if c['ce'] is None else round(c['ce'], 3)}"
              f"  coverage {doc['ec']['coverage']:.3f}  -> {out}")
        return 0
    from awr.bench.eval.aggregate import build_board

    rows = json.loads(a.index.read_text(encoding="utf-8"))
    for r in rows:
        r["evaluation"] = json.loads(Path(r["evaluation"]).read_text(encoding="utf-8"))
    board = build_board(rows, board_version=a.board_version)
    a.out.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(board['entries'])} entries -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
