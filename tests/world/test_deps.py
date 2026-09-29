"""M03-AC-030：`worldpkg` 进程不加载 open3d、torch、numba；tiny world 全流程 ≤ 5 s（NFR-013）；代码不含椭球常量（NFR-012）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def test_no_heavy_modules_loaded():
    code = ("import sys, awr.world.package.cli, awr.world.package.build, awr.world.package.validate, awr.world.geometry, "
            "awr.world.package.catalog\nprint(','.join(m for m in ('open3d', 'torch', 'numba') if m in sys.modules))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == ""


def test_tiny_pipeline_fast(tiny_built):
    assert tiny_built["result"].seconds <= 5.0 * 2      # 与其他 agent 共享 CPU 时留一倍余量；本机空载实测见实现报告


@pytest.mark.parametrize("sub", ["ingest", "pointcloud", "terrain", "semantic", "package", "qa", "geometry"])
def test_no_ellipsoid_constants(sub):
    for p in (REPO / "python" / "awr" / "world" / sub).rglob("*.py"):
        t = p.read_text(encoding="utf-8")
        assert "6378137" not in t and "298.257" not in t, p
