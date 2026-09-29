"""Deterministic JSON and atomic file writes for the Recon IR (M01 §9.6 rule 3; P-M01-4, P-M01-7).

Canonical form: keys sorted, compact separators, floats as Python `repr` (shortest round-trip), no NaN; every write goes
to `<name>.tmp.<pid>` first and is renamed into place, so a file is either complete or absent.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["atomic_write_bytes", "canonical_json", "dumps_line", "file_sha256", "to_jsonable", "write_json"]


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return to_jsonable(obj.tolist())
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, Path):
        return obj.as_posix()
    return obj


def canonical_json(obj: Any) -> bytes:
    return json.dumps(to_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def dumps_line(obj: Any) -> bytes:
    return canonical_json(obj) + b"\n"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_json(path: Path, obj: Any) -> bytes:
    data = canonical_json(obj) + b"\n"
    atomic_write_bytes(path, data)
    return data


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
