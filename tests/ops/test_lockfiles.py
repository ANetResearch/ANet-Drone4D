"""锁文件与依赖一致性（M00-A）。

依据：AWR-11 TECH-FR-001（精确版本、两份锁文件）、TECH-FR-003（禁用依赖）、TECH-NFR-003（版本漂移为 0）；
AWR-19 OPS-FR-006、§7.4；AWR-03 ADR-037、ADR-038。
"""

from __future__ import annotations

import json
import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.+\-]+)$")
D1_EXTRAS = ("geo", "tools", "test", "dev", "fetch")
NOT_IN_D1 = ("geo-worker", "px4")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _lock_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==(\S+)", line)
        if m:
            pins[_norm(m.group(1))] = m.group(2)
    return pins


def _pkg_json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _npm_lock() -> dict:
    return json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))


def _installed_npm(name: str) -> str:
    return _npm_lock()["packages"][f"node_modules/{name}"]["version"]


def _minor(v: str) -> str:
    return ".".join(v.split(".")[:2])


# ---- Python ------------------------------------------------------------------


def test_pyproject_pins_are_exact() -> None:
    proj = _pyproject()["project"]
    specs = list(proj["dependencies"])
    for group in proj["optional-dependencies"].values():
        specs.extend(group)
    bad = [s for s in specs if not PIN.match(s)]
    assert not bad, f"pyproject.toml 中存在非精确版本：{bad}"


def test_lock_covers_pyproject_d1_pins() -> None:
    proj = _pyproject()["project"]
    lock = _lock_pins()
    specs = list(proj["dependencies"])
    for extra in D1_EXTRAS:
        specs.extend(proj["optional-dependencies"][extra])
    for spec in specs:
        name, ver = PIN.match(spec).groups()
        assert lock.get(_norm(name)) == ver, f"{name}: pyproject {ver}，requirements.lock {lock.get(_norm(name))}"


def test_lock_excludes_non_d1_extras_and_denied() -> None:
    proj = _pyproject()["project"]
    lock = _lock_pins()
    for extra in NOT_IN_D1:
        for spec in proj["optional-dependencies"][extra]:
            name = PIN.match(spec).group(1)
            assert _norm(name) not in lock, f"{name} 属于 {extra}，不得进入 D1 锁文件"
    # TECH-FR-003 的 Python 侧禁用清单
    for denied in ("redis", "pyzmq", "roslibpy", "open3d"):
        assert denied not in lock, denied


def test_lock_has_hashes_and_tooling() -> None:
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in text
    lock = _lock_pins()
    # requirements.in 的工具项进入锁文件，uv pip sync 不会卸载它们
    assert lock["uv"] == "0.12.19"
    assert "pip" in lock
    assert "setuptools" in lock


def test_numba_llvmlite_numpy_locked_together() -> None:
    lock = _lock_pins()
    # AWR-11 §3.5：numba 0.67.0 × llvmlite 0.49.0 × numpy 2.5.3 同步锁定（ADR-049 逐位一致要求同一版本）
    assert (lock["numba"], lock["llvmlite"], lock["numpy"]) == ("0.67.0", "0.49.0", "2.5.3")


def test_installed_python_matches_lock() -> None:
    lock = _lock_pins()
    drift = {}
    for name, ver in lock.items():
        try:
            got = metadata.version(name)
        except metadata.PackageNotFoundError:
            got = None
        if got != ver:
            drift[name] = (ver, got)
    assert not drift, f".venv 与 requirements.lock 不一致（期望, 实际）：{drift}；修复：make setup"


# ---- Node --------------------------------------------------------------------

RANGE = re.compile(r"[\^~*xX<>|]|\s-\s|^latest$")


@pytest.mark.parametrize("rel", ["package.json", "apps/web/package.json", "packages/contracts/package.json"])
def test_package_json_pins_are_exact(rel: str) -> None:
    pkg = _pkg_json(rel)
    fields = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
    bad = {name: spec for f in fields for name, spec in pkg.get(f, {}).items() if RANGE.search(spec)}
    assert not bad, f"{rel} 中存在范围版本：{bad}"


def test_workspaces_and_engines() -> None:
    root = _pkg_json("package.json")
    assert root["private"] is True
    assert root["type"] == "module"
    assert root["workspaces"] == ["apps/web", "packages/contracts"]
    assert root["engines"]["node"] == ">=22.12 <23"
    assert _pkg_json("apps/web/package.json")["engines"]["node"] == ">=22.12 <23"
    assert (ROOT / ".nvmrc").read_text(encoding="utf-8").strip() == "22.12.0"


def test_package_lock_matches_package_json() -> None:
    web = _pkg_json("apps/web/package.json")
    drift = {}
    for field in ("dependencies", "devDependencies"):
        for name, spec in web[field].items():
            if name == "@awr/contracts":
                continue
            got = _installed_npm(name)
            if got != spec:
                drift[name] = (spec, got)
    assert not drift, f"package-lock.json 与 apps/web/package.json 不一致：{drift}"
    lock = _npm_lock()["packages"]
    assert lock["node_modules/@awr/contracts"].get("link") is True
    assert lock["packages/contracts"]["version"] == "1.0.0"


def test_version_drift_rules() -> None:
    # TECH-NFR-003
    assert _minor(_installed_npm("three")) == _minor(_installed_npm("@types/three"))
    react = _minor(_installed_npm("react"))
    assert _minor(_installed_npm("@types/react")) == react
    assert _minor(_installed_npm("@types/react-dom")) == react
    assert _installed_npm("@types/node").split(".")[0] == "22"
    assert _installed_npm("@vitest/browser-playwright") == _installed_npm("vitest")
    assert _installed_npm("three") == "0.186.1"


def test_npm_denied_packages_absent() -> None:
    # TECH-FR-003：禁用依赖在锁文件中不得出现（@radix-ui/* 只作为 cmdk 的传递依赖出现，见实现报告）
    denied = [
        "lucide-react", "recharts", "echarts", "chart.js", "sonner", "framer-motion", "motion", "cesium",
        "deck.gl", "msgpackr", "comlink", "r3f-perf", "@tanstack/pacer", "typescript-eslint", "vaul",
        "embla-carousel-react", "react-day-picker", "input-otp", "next-themes",
    ]
    names = {k.rsplit("node_modules/", 1)[-1] for k in _npm_lock()["packages"] if k}
    hit = sorted(n for n in denied if n in names)
    assert not hit, f"锁文件中出现禁用依赖：{hit}"
    assert _installed_npm("@react-three/fiber").split(".")[0] == "9"
    assert _installed_npm("@react-three/drei").split(".")[0] == "10"
    web = _pkg_json("apps/web/package.json")
    direct = set(web["dependencies"]) | set(web["devDependencies"])
    assert not [n for n in direct if n.startswith("@radix-ui/")]
