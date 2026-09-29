"""工程骨架自检（M00-A）：包结构、pyproject 冻结片段、测试目录归属、Make 扩展点。

依据：AWR-03 §4.1（目录树与 pyproject 冻结片段）、§4.3（测试目录归属表）、ADR-050；AWR-19 §7.2。
"""

from __future__ import annotations

import importlib
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# AWR-03 §4.1 目录树中的全部 Python 子包
PACKAGES = [
    "awr",
    "awr.runtime",
    "awr.contracts",
    "awr.api",
    "awr.api.rest",
    "awr.api.rt",
    "awr.api.rt.sources",
    "awr.recorder",
    "awr.sim",
    "awr.sim.runtime",
    "awr.sim.fleet",
    "awr.sim.fleet.stages",
    "awr.sim.core",
    "awr.sim.safety",
    "awr.sim.mission",
    "awr.sim.planning",
    "awr.sim.sensors",
    "awr.sim.backends",
    "awr.sim.orchestrator",
    "awr.environment",
    "awr.environment.wind",
    "awr.environment.weather",
    "awr.environment.atmosphere",
    "awr.environment.io",
    "awr.world",
    "awr.world.ingest",
    "awr.world.pointcloud",
    "awr.world.georef",
    "awr.world.terrain",
    "awr.world.geometry",
    "awr.world.semantic",
    "awr.world.package",
    "awr.world.qa",
    "awr.world.export",
    "awr.reconstruction",
    "awr.reconstruction.ir",
    "awr.reconstruction.engines",
    "awr.reconstruction.jobs",
    "awr.agent",
    "awr.agent.runtime",
    "awr.agent.capabilities",
    "awr.agent.anet_mock",
    "awr.agent.anet_bridge",
    "awr.agent.guard",
    "awr.swarm",
    "awr.swarm.formation",
    "awr.swarm.coverage",
    "awr.swarm.allocation",
    "awr.jobs",
    "awr.datasets",
]

# AWR-03 §4.3 测试目录归属表（tests/ 下 pytest、apps/web/tests/ 下 Vitest、apps/web/perf/ 下 Playwright）
PY_TEST_DIRS = [
    "contracts", "ops", "fixtures", "reconstruction", "georef", "world", "jobs", "world_query",
    "pointcloud", "m06", "environment", "sim", "golden", "safety", "mission", "planning", "swarm",
    "rt", "runtime", "recorder", "sensors", "agent", "m15", "e2e", "chaos", "scenarios",
]
WEB_TEST_DIRS = [
    "contracts", "fixtures", "geo", "pointcloud", "m06", "perf", "environment", "mission",
    "m11", "net", "time", "sensors", "m15", "m16",
]
WEB_PERF_DIRS = ["m01", "m02", "m03", "m05", "m06", "m07", "m11", "m12", "m14", "m15", "harness", "report", "baselines"]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_importable_from_python_dir(name: str) -> None:
    mod = importlib.import_module(name)
    path = Path(mod.__file__).resolve()
    # 可编辑安装指向 python/awr；禁止依赖工作目录的相对 import（AWR-03 §4.1）
    assert path.is_relative_to(ROOT / "python" / "awr"), path


def test_awr_version_matches_pyproject() -> None:
    import awr

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert awr.__version__ == meta["project"]["version"] == "0.1.0"


def test_pyproject_frozen_snippet() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["build-system"] == {"requires": ["setuptools>=80"], "build-backend": "setuptools.build_meta"}
    proj = meta["project"]
    assert proj["name"] == "awr"
    assert proj["requires-python"] == ">=3.12,<3.13"
    assert proj["scripts"] == {"worldpkg": "awr.world.package.cli:main", "awr": "awr.runtime.cli:main"}
    assert meta["tool"]["setuptools"]["packages"]["find"] == {"where": ["python"], "include": ["awr*"]}
    ini = meta["tool"]["pytest"]["ini_options"]
    assert ini["testpaths"] == ["tests"]
    assert ini["addopts"] == "--import-mode=importlib"
    # AWR-18 §8.2 标记登记
    marks = {m.split(":", 1)[0] for m in ini["markers"]}
    assert marks == {"slow", "needs_data", "perf", "chaos", "ext"}


def test_tools_is_not_a_package() -> None:
    # tools/ 是不可 import 的脚本目录（AWR-03 §4.1）
    assert not (ROOT / "tools" / "__init__.py").exists()


@pytest.mark.parametrize(
    ("base", "names"),
    [("tests", PY_TEST_DIRS), ("apps/web/tests", WEB_TEST_DIRS), ("apps/web/perf", WEB_PERF_DIRS)],
)
def test_test_directories_exist(base: str, names: list[str]) -> None:
    missing = [n for n in names if not (ROOT / base / n).is_dir()]
    assert not missing, f"{base} 缺少目录：{missing}"


def test_makefile_has_only_total_targets_and_includes() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "include mk/common.mk" in text
    assert "$(wildcard mk/*.mk)" in text
    for target in ("setup", "worlds", "run", "dev", "build", "test", "perf", "chaos", "lint", "ci"):
        assert f"\n{target}:" in text, target


def test_make_dry_run_parses() -> None:
    # Makefile 与 mk/*.mk 可被 make 解析（-n 不执行任何命令）
    res = subprocess.run(["make", "-n", "-C", str(ROOT), "help"], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
