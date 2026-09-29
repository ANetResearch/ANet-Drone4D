"""M13-NFR-010、FR-004：`import awr.sim.sensors` 无副作用；不 import numba 与 open3d；纯函数模块只依赖 numpy。"""

from __future__ import annotations

import os
import subprocess
import sys

CODE = """
import sys
from awr.sim.fleet.stages import registry as R
import awr.sim.sensors, awr.sim.sensors.detector, awr.sim.sensors.thermal_mock, awr.sim.sensors.cbrng
r = R.registry()
assert not any(s.name == 'sensors' for s in r.stages) and 'sensors' not in r.blocks, 'registration on import'
assert 'awr.sim.sensors.plugin' not in sys.modules
for m in ('numba', 'open3d', 'yaml', 'jsonschema'):
    assert m not in sys.modules, m
print('ok')
"""


def run(code: str, env: dict | None = None) -> str:
    e = {k: v for k, v in os.environ.items() if k != "AWR_PLUGINS"}
    e.update(env or {})
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e, timeout=120, check=True).stdout


def test_package_import_has_no_side_effects():
    assert run(CODE).strip().endswith("ok")


def test_plugin_env_bridge_registers():
    out = run("from awr.sim.fleet.stages import registry as R\nimport awr.sim.sensors\n"
              "print(any(s.name == 'sensors' for s in R.registry().stages))", {"AWR_PLUGINS": "awr.environment.stage,awr.sim.sensors"})
    assert out.strip() == "True"


def test_no_forbidden_imports_in_sources():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "python" / "awr" / "sim" / "sensors"
    for p in root.rglob("*.py"):
        t = p.read_text(encoding="utf-8")
        assert "import numba" not in t and "import open3d" not in t and "time.perf_counter" not in t, p
