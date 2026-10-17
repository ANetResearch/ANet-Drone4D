"""`python -m awr.sim.events verify|inspect <tape.bin | 事件版本目录>`：校验内容哈希、打印头与块统计。"""

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
    a = ap.parse_args(argv)
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
