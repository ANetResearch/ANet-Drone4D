"""Shared helpers for the frame-conversion tests (M02)."""

from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "packages" / "contracts" / "golden" / "frames"


def load_group(name: str) -> dict:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def close(a: float, b: float, kind: str, tol: dict) -> bool:
    """Mixed tolerance |a - b| <= atol + rtol |b| (AWR-03 §5.1 rule 8); angle_deg compares in radians; exact for integers."""
    if kind == "exact":
        return a == b
    if kind == "angle_deg":
        a, b, kind = math.radians(a), math.radians(b), "angle_rad"
    return abs(a - b) <= tol["atol"][kind] + tol["rtol"] * abs(b)


def flat(x):
    if isinstance(x, dict):
        return [v for k in sorted(x) for v in flat(x[k])]
    if isinstance(x, (list, tuple)):
        return [v for i in x for v in flat(i)]
    return [x]


