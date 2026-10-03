"""Shared helpers of tests/reconstruction (M01): paths, the temporary worlds directory and job parameters.

A uniquely named module (not `conftest`) so imports stay unambiguous when the whole tests/ tree is collected.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORLDS = ROOT / "worlds"
SOURCE = "shenzhen"
SMALL = {"frames": 60, "keep": 0.05}           # functional chain size (the full 600-frame run is marked slow)
WORLD_BUILT = (WORLDS / SOURCE / "geometry" / "pointcloud" / "source" / "source.json").exists() and \
    (WORLDS / ".status" / f"{SOURCE}.json").exists()
_skip_no_world = pytest.mark.skipif(not WORLD_BUILT, reason="worlds/shenzhen not built (make worlds)")


def needs_world(fn):
    """Needs the built Shenzhen world: the needs_data marker plus a skip when it is missing (AWR-18 §8.2, SHOW-CI)."""
    return pytest.mark.needs_data(_skip_no_world(fn))


def make_worlds(base: Path) -> Path:
    """Temporary worlds dir: the real source world symlinked, its status copied; products go only here."""
    wd = base / "worlds"
    (wd / ".status").mkdir(parents=True, exist_ok=True)
    if not (wd / SOURCE).exists():
        os.symlink(WORLDS / SOURCE, wd / SOURCE)
    shutil.copy(WORLDS / ".status" / f"{SOURCE}.json", wd / ".status" / f"{SOURCE}.json")
    return wd


def job_params(target: str, *, frames: int = SMALL["frames"], keep: float = SMALL["keep"], seed: int = 1, path: str = "helix",
               **extra: dict) -> dict:
    p = {"engine": "mock", "source": {"kind": "world_sample", "world_id": SOURCE, "path": path, "frames": frames},
         "target_world_id": target, "seed": seed, "params": {"mock": {"source_keep": keep}}}
    for k, v in extra.items():
        p["params"].setdefault(k, {}).update(v)
    return p


def read_json(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))
