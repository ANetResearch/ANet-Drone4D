"""Dependency isolation (M01-AC-022; M01-NFR-010, NFR-011; AWR-03 §4.2 rule 1).

The api never imports `awr.reconstruction` (import lint over `awr/api`), `import awr.reconstruction` costs <= 200 ms
cumulative (engines, numpy and scipy are loaded lazily) and the main venv has no torch.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys

from recon_common import ROOT


def test_api_does_not_import_reconstruction():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "lint" / "check_py_imports.py"),
                        str(ROOT / "python" / "awr" / "api"), str(ROOT / "python" / "awr" / "reconstruction")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:]
    src = (ROOT / "python" / "awr" / "api" / "rest" / "recon.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+awr\.reconstruction", src, re.M)


def test_import_time_and_no_heavy_modules():
    code = ("import sys, time; t = time.perf_counter(); import awr.reconstruction; dt = time.perf_counter() - t; "
            "print(dt, 'numpy' in sys.modules, 'scipy' in sys.modules, 'torch' in sys.modules)")
    best = None
    for _ in range(3):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True).stdout.split()
        best = out if best is None or float(out[0]) < float(best[0]) else best
    assert float(best[0]) <= 0.200, best
    assert best[1:] == ["False", "False", "False"]
    assert importlib.util.find_spec("torch") is None


def test_importtime_cumulative():
    r = subprocess.run([sys.executable, "-X", "importtime", "-c", "import awr.reconstruction"], capture_output=True, text=True,
                       timeout=60, check=True)
    last = [ln for ln in r.stderr.splitlines() if ln.rstrip().endswith("| awr.reconstruction")][-1]
    cumulative_us = int(last.split("|")[1])
    assert cumulative_us <= 200_000, last
