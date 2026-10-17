"""Run 目录查找（M27 §7.6）：`<data_root>/dataset/<event_id>/runs/<run_id>/{run_manifest.json, raw.mcap, frames.idx}`。"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["RUN_ID", "find_run", "list_runs", "truth_paths"]

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def find_run(dataset_dir: Path, run_id: str) -> Path | None:
    if not RUN_ID.match(run_id):
        return None
    hits = sorted(Path(dataset_dir).glob(f"*/runs/{run_id}"))
    return hits[0] if hits else None


def list_runs(dataset_dir: Path) -> list[dict]:
    out = []
    for d in sorted(Path(dataset_dir).glob("*/runs/*")):
        if not d.is_dir():
            continue
        m = d / "run_manifest.json"
        doc = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
        out.append({"run_id": d.name, "event_id": d.parent.parent.name, "state": doc.get("state", "UNKNOWN"),
                    "has_frames": (d / "frames.idx").exists(), "created_unix_ns": doc.get("created_unix_ns"),
                    "kind": doc.get("kind"), "world_id": doc.get("world_id")})
    return out


def truth_paths(run_dir: Path) -> tuple[Path, Path]:
    """dev 划分的真值文件与索引：`<event>/truth/run_<id>.mcap`、`<event>/truth/run_<id>.frames.idx`（test 划分在 hidden/，P0a 不涉及）。"""
    t = Path(run_dir).parent.parent / "truth"
    return t / f"run_{Path(run_dir).name}.mcap", t / f"run_{Path(run_dir).name}.frames.idx"
