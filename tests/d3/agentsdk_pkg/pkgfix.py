"""tests/d3/agentsdk_pkg 的夹具与工具（conftest.py 导出夹具；D3-AC-58、M26-AC-030）：构建 `anet4d` 的 wheel 与 sdist，在临时 venv 中安装（不含 `awr`），
供快速上手示例与基线策略经线协议运行。

- `dist`：缺省把 tools/sdk/anet4d 复制到临时目录后构建（不在源码树留下 build/ 与 egg-info；需要 setuptools ≥ 77，按
  pyproject 的 build-system）；设置 `ANET4D_DIST=<目录>`（旧名 `DRONE4D_DIST` 仍接受）时直接使用该目录中已构建的 wheel 与 sdist（dmax 容器内复用本机构建物）。
- `clean_venv`：`python -m venv --without-pip` 新建环境，以宿主 pip 的 `--python` 安装 wheel（extras `l2`）。依赖来源：设置
  `ANET4D_WHEELHOUSE=<目录>`（旧名 `DRONE4D_WHEELHOUSE` 仍接受）时为该离线 wheel 目录（`--no-index --find-links`，即 ADR-202 的干净主机口径）；否则把当前解释器中
  已安装的依赖闭包（只含 anet4d 声明的依赖及其传递依赖）以符号链接放入一个目录并经 .pth 挂到 venv，pip 仍按元数据核对依赖。
  两种方式下 venv 中都没有 `awr`，`PYTHONPATH` 不传入子进程。
"""

from __future__ import annotations

import email.parser
import importlib.metadata as md
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PKG = ROOT / "tools" / "sdk" / "anet4d"


def _env(name: str) -> str | None:
    """ANET4D_* 选项；旧名 DRONE4D_*（ANet4D V0.3.2 更名前）仍接受。"""
    return os.environ.get(name) or os.environ.get(name.replace("ANET4D_", "DRONE4D_", 1))
SDK = PKG / "src"
DOCS = ROOT / "docs" / "sdk"
EXAMPLES = DOCS / "examples"
SOURCE_DATE_EPOCH = "1760745600"             # 固定构建时刻（2025-10-18T00:00:00Z），wheel 可逐字节复现
sys.path.insert(0, str(SDK))


def _load_env_conftest():
    """复用 tests/d3/env 的合成世界与事件夹具 `synth_event`（同一份构造，不复制）。"""
    spec = importlib.util.spec_from_file_location("_d3_env_conftest", ROOT / "tests" / "d3" / "env" / "conftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


synth_event = _load_env_conftest().synth_event


def clean_env(**extra: str) -> dict[str, str]:
    """子进程环境：只保留 PATH、HOME 与区域设置，不含 PYTHONPATH 与仓库变量；BLAS 单线程（与 `std` 资源档一致）。"""
    keep = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    return {**keep, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1", **extra}


@dataclass
class Dist:
    wheel: Path
    sdist: Path
    built: bool
    build_s: float

    def metadata(self) -> email.message.Message:
        with zipfile.ZipFile(self.wheel) as z:
            name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
            return email.parser.Parser().parsestr(z.read(name).decode("utf-8"))


@dataclass
class Venv:
    root: Path
    python: Path
    t0: float                                 # venv 创建开始时刻（time.monotonic），上手计时的起点
    install_s: float
    deps: str                                 # wheelhouse | linked
    site: Path
    log: list[str] = field(default_factory=list)

    def run(self, *args: str, cwd: Path | None = None, timeout: float = 120.0, **kw) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.python), *args], cwd=cwd, env=clean_env(), capture_output=True, text=True, timeout=timeout, **kw)

    def script(self, name: str) -> Path:
        return self.root / "bin" / name


def _setuptools_ok() -> bool:
    try:
        return int(md.version("setuptools").split(".")[0]) >= 77
    except md.PackageNotFoundError:
        return False


@pytest.fixture(scope="session")
def dist(tmp_path_factory) -> Dist:
    pre = _env("ANET4D_DIST")
    if pre:
        d = Path(pre)
        wheels, sdists = sorted(d.glob("anet4d-*-py3-none-any.whl")), sorted(d.glob("anet4d-*.tar.gz"))
        assert len(wheels) == 1 and len(sdists) == 1, f"ANET4D_DIST={d} must hold exactly one wheel and one sdist"
        return Dist(wheels[0], sdists[0], False, 0.0)
    if not _setuptools_ok():
        pytest.skip("[dep] building the anet4d wheel needs setuptools>=77 (or set ANET4D_DIST to a built wheel and sdist)")
    tmp = tmp_path_factory.mktemp("dist")
    t = time.monotonic()
    wheel = build_wheel(tmp)
    src, out = tmp / "src", wheel.parent
    r = subprocess.run([sys.executable, "-c", f"import setuptools.build_meta as b; b.build_sdist({str(out)!r})"], cwd=src,
                       env={**os.environ, "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-4000:]
    (sdist,) = sorted(out.glob("*.tar.gz"))
    return Dist(wheel, sdist, True, time.monotonic() - t)


def build_wheel(tmp: Path) -> Path:
    """tools/sdk/anet4d 复制到 tmp/src 后构建 wheel 到 tmp/out（与快速上手第 8 节的命令相同）。"""
    src, out = tmp / "src", tmp / "out"
    shutil.copytree(PKG, src, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "*.egg-info"))
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index", "-q", "-w", str(out), str(src)],
                       env={**os.environ, "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-4000:]
    (wheel,) = sorted(out.glob("*.whl"))
    return wheel


def _requirement_closure(dist: Dist, extras: set[str]) -> list[str]:
    """wheel 的 Requires-Dist（含给定 extras）在当前解释器中的传递闭包 → 发行包名列表（按名排序）。"""
    try:
        from packaging.requirements import Requirement
    except ImportError:                                                  # pragma: no cover
        from pip._vendor.packaging.requirements import Requirement
    todo = [(Requirement(r), extras) for r in dist.metadata().get_all("Requires-Dist") or []]
    seen: dict[str, md.Distribution] = {}
    while todo:
        req, ex = todo.pop()
        if req.marker is not None and not any(req.marker.evaluate({"extra": e}) for e in (ex or {""})):
            continue
        key = req.name.lower().replace("_", "-")
        if key in seen:
            continue
        try:
            d = md.distribution(req.name)
        except md.PackageNotFoundError:
            pytest.skip(f"[dep] could not import '{req.name}' (dependency of anet4d; or set ANET4D_WHEELHOUSE)")
        seen[key] = d
        todo += [(Requirement(r), set(req.extras)) for r in d.requires or []]
    return sorted(seen)


def _link_deps(names: list[str], target: Path) -> None:
    """把各发行包在 site-packages 中的顶层条目（包目录、模块、.libs、.dist-info）以符号链接放入 target。"""
    target.mkdir(parents=True)
    for n in names:
        d = md.distribution(n)
        tops = {Path(str(f)).parts[0] for f in d.files or []}
        for top in sorted(tops):
            if top in ("..", "__pycache__") or top.endswith(".pth"):
                continue
            src = Path(str(d.locate_file(top)))
            if src.exists() and not (target / top).exists():
                (target / top).symlink_to(src)


@pytest.fixture(scope="session")
def clean_venv(dist: Dist, tmp_path_factory) -> Venv:
    pytest.importorskip("pip")
    root = tmp_path_factory.mktemp("venv") / "d4d"
    t0 = time.monotonic()
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True, timeout=120)
    py = root / "bin" / "python"
    site = Path(subprocess.run([str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], env=clean_env(),
                               capture_output=True, text=True, check=True).stdout.strip())
    house = _env("ANET4D_WHEELHOUSE")
    pip = [sys.executable, "-m", "pip", "--python", str(py), "install", "--no-index", "--disable-pip-version-check", "-q"]
    if house:
        pip += ["--find-links", house]
        deps = "wheelhouse"
    else:
        linked = root.parent / "deps"
        _link_deps(_requirement_closure(dist, {"l2"}), linked)
        (site / "_anet4d_test_deps.pth").write_text(f"{linked}\n", encoding="utf-8")
        deps = "linked"
    r = subprocess.run([*pip, f"anet4d[l2] @ {dist.wheel.as_uri()}"], env=clean_env(), capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-4000:]
    return Venv(root, py, t0, time.monotonic() - t0, deps, site)


def venv_probe(v: Venv) -> dict:
    """venv 内的自检：anet4d 版本与安装位置、awr 不可导入、参考检测器权重随包并通过 sha256 校验；已弃用的别名包 drone4d 导入时发出
    DeprecationWarning，子模块与 anet4d 为同一对象。"""
    code = ("import importlib.util, json, warnings, anet4d, anet4d.baselines, anet4d.serve\n"
            "from anet4d.perception import ref_det\n"
            "m = ref_det.RefDet()\n"
            "with warnings.catch_warnings(record=True) as w:\n"
            "    warnings.simplefilter('always')\n"
            "    import drone4d, drone4d.baselines, drone4d.perception.ref_det\n"
            "alias = {'same': drone4d.baselines is anet4d.baselines and drone4d.perception.ref_det is ref_det,"
            " 'version': drone4d.__version__, 'deprecated': any(issubclass(x.category, DeprecationWarning) for x in w)}\n"
            "print(json.dumps({'version': anet4d.__version__, 'file': anet4d.__file__, 'awr': importlib.util.find_spec('awr') is not None,"
            " 'weights': str(ref_det.WEIGHTS), 'baselines': sorted(anet4d.baselines.BASELINES), 'alias': alias}))\n")
    r = v.run("-c", code)
    assert r.returncode == 0, r.stderr[-4000:]
    return json.loads(r.stdout.strip().splitlines()[-1])
