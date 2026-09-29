"""Unique implementation guard (M02-FR-012, M02-AC-013; AWR-03 §5.1 rule 8)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {"python/awr/world/georef/frames.py", "python/awr/world/georef/types.py", "apps/web/src/engine/geo/frames.ts"}
PATTERN = re.compile(r"6378137|298\.257223563|298\.257222101|6371000|\blla_to_ecef\b|\becef_to_lla\b|\bllaToEcef|\becefToLla")


def _sources():
    for base, exts in (("python", {".py"}), ("apps/web/src", {".ts", ".tsx"})):
        for p in sorted((ROOT / base).rglob("*")):
            if p.suffix in exts and "node_modules" not in p.parts and "__pycache__" not in p.parts:
                yield p


def test_ellipsoid_constants_only_in_frames():
    bad = []
    for p in _sources():
        rel = p.relative_to(ROOT).as_posix()
        if rel in ALLOWED or "/conventions." in rel:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if PATTERN.search(line) and "frames" not in line.split("import")[-1]:
                bad.append(f"{rel}:{i}: {line.strip()[:80]}")
    assert not bad, bad


def test_frames_imports_are_light():
    src = (ROOT / "python/awr/world/georef/frames.py").read_text() + (ROOT / "python/awr/world/georef/time.py").read_text()
    for heavy in ("open3d", "small_gicp", "gtsam", "scipy", "pyproj"):
        assert not re.search(rf"^\s*(import|from)\s+{heavy}\b", src, re.M), heavy
    code = ("import time, numpy; t0 = time.perf_counter(); import awr.world.georef.frames, awr.world.georef.time; "
            "print(time.perf_counter() - t0)")
    best = min(float(subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True).stdout) for _ in range(3))
    assert best <= 0.050, f"import took {best * 1e3:.1f} ms (numpy preloaded)"
