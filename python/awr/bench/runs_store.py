"""Run 目录查找（M27 §7.6）：`<data_root>/dataset/<event_id>/runs/<run_id>/{run_manifest.json, raw.mcap, frames.idx}`。

Env 基准运行（M26 recorder，`manifest.json`，schema `drone4d.bench.env_run.v1`）同处一目录，列表项另带策略、物理档、排名标记与
评测摘要（`evaluation.json` 的四项分量与 `ranked_valid`）。
"""

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
        item = {"run_id": d.name, "event_id": d.parent.parent.name, "state": doc.get("state", "UNKNOWN"),
                "has_frames": (d / "frames.idx").exists(), "created_unix_ns": doc.get("created_unix_ns"),
                "kind": doc.get("kind"), "world_id": doc.get("world_id")}
        env = d / "manifest.json"
        if not doc and env.exists():
            item.update(env_run_item(d, json.loads(env.read_text(encoding="utf-8"))))
        out.append(item)
    return out


def env_run_item(d: Path, man: dict) -> dict:
    ev = d / "evaluation.json"
    e = json.loads(ev.read_text(encoding="utf-8")) if ev.exists() else None
    pol = man.get("policy") or {}
    return {"state": "EVALUATED" if e else "SUCCEEDED", "kind": "bench", "world_id": man.get("world"),
            "created_unix_ns": int((d / "manifest.json").stat().st_mtime_ns), "policy": pol.get("name") or pol.get("kind"),
            "physics": man.get("tier"), "obs_level": man.get("obs_level"), "ranked": bool(man.get("ranked")), "n_uav": len(man.get("fleet", [])),
            "duration_s": man.get("duration_s"), "evaluation": None if e is None else {"components": e["components"], "ranked_valid": e["ranked_valid"],
                                                                                      "invalid_reasons": e["invalid_reasons"]}}


def truth_paths(run_dir: Path) -> tuple[Path, Path]:
    """dev 划分的真值文件与索引：`<event>/truth/run_<id>.mcap`、`<event>/truth/run_<id>.frames.idx`（test 划分在 hidden/，P0a 不涉及）。"""
    t = Path(run_dir).parent.parent / "truth"
    return t / f"run_{Path(run_dir).name}.mcap", t / f"run_{Path(run_dir).name}.frames.idx"
