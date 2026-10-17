"""`python -m awr.sim.events compile|preflight|verify|inspect`（M23 §7.2）。

compile、preflight 的退出码：0 通过，2 门 1 失败，3 门 2 不可行，4 其他编译错误；verify、inspect 失败为 2。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from awr.sim.events.errors import EventError
from awr.sim.events.tape import TapeReader


def _reader(p: Path) -> tuple[TapeReader, str | None]:
    if p.is_dir():
        m = json.loads((p / "tape.json").read_text())
        return TapeReader(p / "tape.bin"), m["content_sha256"]
    return TapeReader(p), None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.sim.events")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("verify", "inspect"):
        sub.add_parser(c).add_argument("path", type=Path)
    for c in ("compile", "preflight"):
        p = sub.add_parser(c)
        p.add_argument("event", type=Path)
        p.add_argument("--world", type=Path, required=True, help="world package directory")
        p.add_argument("--anchors", type=Path)
        p.add_argument("--retime", choices=("minimal", "median"), default="minimal")
        p.add_argument("--lenient", action="store_true")
        p.add_argument("--out", type=Path)
        p.add_argument("--cache", type=Path)
    a = ap.parse_args(argv)
    if a.cmd in ("compile", "preflight"):
        from awr.sim.events.compile import CompileOptions, compile_event

        r = compile_event(a.event, a.world, opts=CompileOptions(strict=not a.lenient, retime_mode=a.retime, anchors_overlay=a.anchors,
                                                                out=a.out, cache_dir=a.cache, preflight_only=a.cmd == "preflight"))
        print(json.dumps(r.as_dict(), ensure_ascii=False, indent=2))
        return {"compiled": 0, "preflight": 0, "infeasible": 3}.get(r.status, 2 if r.gates and r.gates[0]["status"] == "fail" else 4)
    try:
        r, expect = _reader(a.path)
        if a.cmd == "verify":
            h = r.verify(expect)
            print(json.dumps({"ok": True, **h, "checked_against_manifest": expect is not None}, indent=2))
        else:
            hd = r.header
            raw = sum(b.raw_s + b.raw_b for b in r.blocks)
            comp = sum(b.comp_s + b.comp_b for b in r.blocks)
            print(json.dumps({"event_id": hd["event_id"], "event_version": hd["event_version"], "mode": hd["mode"], "n_ticks": r.n_ticks,
                              "n_actors": r.n_actors, "n_scripted": r.n_scripted, "blocks": len(r.blocks), "raw_bytes": raw,
                              "compressed_bytes": comp, "bytes_per_actor_second": round(comp / max(1, r.n_actors) / (r.n_ticks / 10), 2),
                              "world": hd["world"]}, indent=2))
    except EventError as e:
        print(json.dumps({"ok": False, "code": e.code, "reason": e.reason, "message": str(e), "detail": e.detail}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
