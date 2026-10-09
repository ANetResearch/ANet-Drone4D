"""Results layout shared by every paper experiment.

`runs/paper/<exp>/<run_id>/` holds `meta.json` (git SHA, host, hardware, config grid) and `rows.jsonl` (one JSON object
per measured unit, appended by workers). `tools/paper/collect.py` converts rows to Parquet and aggregated CSV under
`paper/data/<exp>/`.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUNS = Path(os.environ.get("AWR_PAPER_RUNS") or ROOT / "runs" / "paper")
WORLDS = ("shenzhen", "shanghai", "newyork", "chicago", "sanfrancisco", "suzhou", "synthcity")
PRESETS = ("clear", "partlyCloudy", "overcast", "lightRain", "rain", "heavyRain", "thunderstorm", "fog", "haze", "snow",
           "blizzard", "sandstorm")


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def hardware() -> dict[str, Any]:
    cpu = platform.processor() or ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem = None
    with contextlib.suppress(OSError, IndexError, ValueError):
        mem = int(Path("/proc/meminfo").read_text().split()[1]) // 1024 // 1024
    return {"host": platform.node(), "cpu": cpu, "cores": os.cpu_count(), "mem_gb": mem, "python": platform.python_version(),
            "os": platform.platform()}


def new_run(exp: str, config: dict[str, Any], run_id: str | None = None) -> Path:
    rid = run_id or time.strftime("%Y%m%d-%H%M%S")
    d = RUNS / exp / rid
    d.mkdir(parents=True, exist_ok=True)
    meta = {"exp": exp, "run_id": rid, "git_sha": git_sha(), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "hardware": hardware(), "config": config}
    (d / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    return d


def append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    data = "".join(json.dumps(r, default=_default) + "\n" for r in rows)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)


def _default(o: Any) -> Any:
    try:
        import numpy as np

        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    return str(o)


def read_rows(path: Path) -> list[dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
