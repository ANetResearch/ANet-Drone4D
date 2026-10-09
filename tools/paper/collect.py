"""Collect sweep rows into paper/data: `python tools/paper/collect.py <exp>/<run_id> [...] [--out paper/data]`.

Each `runs/paper/<exp>/<run_id>/rows.jsonl` becomes `<out>/<exp>_<run_id>.parquet` (all columns) plus `meta.json` copied
as `<exp>_<run_id>.meta.json`, so figure scripts never read the raw run directories.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd

from common import RUNS


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "paper" / "data"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for r in a.runs:
        d = RUNS / r
        rows = [json.loads(x) for x in (d / "rows.jsonl").read_text().splitlines() if x.strip()]
        df = pd.DataFrame(rows).drop(columns=["_task_key"], errors="ignore")
        name = r.replace("/", "_")
        df.to_parquet(out / f"{name}.parquet", index=False)
        if (d / "meta.json").exists():
            shutil.copy(d / "meta.json", out / f"{name}.meta.json")
        n_err = len((d / "errors.jsonl").read_text().splitlines()) if (d / "errors.jsonl").exists() else 0
        print(f"{r}: {len(df)} rows, {n_err} errors -> {out / name}.parquet")


if __name__ == "__main__":
    main()
