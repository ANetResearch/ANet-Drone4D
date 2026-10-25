"""`drone4d` 打包与文档（D3-AC-58 的"`pip install drone4d` 后按文档运行""SDK 与基线源码随文档发布，文档通过 copy-lint"；
M26-AC-030、M26-FR-080、FR-085、FR-121、NFR-011）。

- wheel：纯 Python（py3-none-any），包含全部模块与参考检测器定稿权重、模型卡；版本单一来源（`drone4d.__version__`）；依赖只有
  httpx、msgpack，extras `l2` 为 numpy、pillow；Python ≥ 3.11；入口 `drone4d-agent`；
- sdist：源码树中的全部 .py（含基线策略与感知层）、权重、README 与 pyproject 原样随包，可据此重建 wheel；
- 临时 venv 安装后不依赖 `awr`，权重载入时 sha256 校验通过；
- 文档：中英文快速上手与 SDK README 通过 copy-lint（中文、英文规则集）；文档中的示例代码与 docs/sdk/examples 逐字相同，
  中英文两篇的命令与代码块一致，引用的文件与基线名存在。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import drone4d
import pytest
from drone4d.baselines import BASELINES
from drone4d.perception.ref_det import WEIGHTS_SHA256
from pkgfix import DOCS, EXAMPLES, PKG, ROOT, SDK, build_wheel, venv_probe

QUICKSTART = {"zh": DOCS / "quickstart.zh-CN.md", "en": DOCS / "quickstart.en.md"}
D3_BASELINES = ("static_guard", "coverage_scan", "track_share")


def _sources() -> set[str]:
    """源码树中随包发布的文件（相对 src/）：全部 .py 与 perception 的权重、模型卡。"""
    out = {p.relative_to(SDK).as_posix() for p in SDK.rglob("*.py") if "__pycache__" not in p.parts}
    return out | {"drone4d/perception/ref_det_v1.npz", "drone4d/perception/model_card.json"}


def test_wheel_contents_and_metadata(dist) -> None:
    assert dist.wheel.name == f"drone4d-{drone4d.__version__}-py3-none-any.whl"
    with zipfile.ZipFile(dist.wheel) as z:
        names = set(z.namelist())
        files = {n for n in names if not n.split("/")[0].endswith(".dist-info")}
        assert files == _sources()
        assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
        assert hashlib.sha256(z.read("drone4d/perception/ref_det_v1.npz")).hexdigest() == WEIGHTS_SHA256
        for rel in _sources():                                          # 与源码树逐字节相同
            assert z.read(rel) == (SDK / rel).read_bytes(), rel
        ep = z.read(f"drone4d-{drone4d.__version__}.dist-info/entry_points.txt").decode()
    assert "drone4d-agent = drone4d.serve:main" in ep
    m = dist.metadata()
    assert m["Name"] == "drone4d" and m["Version"] == drone4d.__version__
    assert m["Requires-Python"] == ">=3.11"
    assert m["License-Expression"] == "Apache-2.0"
    reqs = sorted(m.get_all("Requires-Dist"))
    assert reqs == sorted(["httpx>=0.27", "msgpack>=1.0", 'numpy>=1.26; extra == "l2"', 'pillow>=10; extra == "l2"',
                           'numpy>=1.26; extra == "images"', 'pillow>=10; extra == "images"'])
    assert {"3.11", "3.12", "3.13"} <= {c.rsplit(" ", 1)[-1] for c in m.get_all("Classifier") if c.startswith("Programming Language :: Python :: 3.")}
    assert m.get_payload().strip() == (PKG / "README.md").read_text(encoding="utf-8").strip()      # README 即包说明


def test_wheel_build_is_reproducible(dist, tmp_path: Path) -> None:
    """固定 SOURCE_DATE_EPOCH 时两次构建的 wheel 逐字节相同（快速上手第 8 节）。"""
    if not dist.built:
        pytest.skip("[optin] DRONE4D_DIST supplies a prebuilt wheel; reproducibility is checked where the wheel is built")
    again = build_wheel(tmp_path)
    assert hashlib.sha256(again.read_bytes()).hexdigest() == hashlib.sha256(dist.wheel.read_bytes()).hexdigest()


def test_sdist_ships_sdk_and_baseline_sources(dist) -> None:
    top = dist.sdist.name.removesuffix(".tar.gz")
    with tarfile.open(dist.sdist) as t:
        names = {n.removeprefix(f"{top}/") for n in t.getnames()}
        assert {"pyproject.toml", "README.md", "PKG-INFO"} <= names
        shipped = {n.removeprefix("src/") for n in names if n.startswith("src/drone4d/")}
        assert _sources() <= shipped
        for p in D3_BASELINES:                                          # 三个基线与公共部分随源码发布
            assert f"drone4d/baselines/{p}.py" in shipped
        assert t.extractfile(f"{top}/src/drone4d/baselines/common.py").read() == (SDK / "drone4d/baselines/common.py").read_bytes()
        assert t.extractfile(f"{top}/pyproject.toml").read() == (PKG / "pyproject.toml").read_bytes()


def test_clean_venv_has_no_repository_packages(clean_venv) -> None:
    info = venv_probe(clean_venv)
    assert info["version"] == drone4d.__version__
    assert Path(info["file"]).is_relative_to(clean_venv.site), info["file"]           # 来自 venv 中安装的 wheel，不是源码树
    assert info["awr"] is False
    assert Path(info["weights"]).is_relative_to(clean_venv.site)
    assert set(D3_BASELINES) <= set(info["baselines"]) and info["baselines"] == sorted(BASELINES)
    r = subprocess.run([str(clean_venv.script("drone4d-agent")), "--help"], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"})
    assert r.returncode == 0 and "--baseline" in r.stdout
    r = clean_venv.run("-m", "pip", "--version")                     # venv 不带 pip：安装经宿主 pip 的 --python 完成
    assert r.returncode != 0


# ------------------------------------------------------------------ 文档
def _blocks(md: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in re.finditer(r"^```(\w*)\n(.*?)^```$", md, re.S | re.M)]


def test_quickstart_code_matches_examples() -> None:
    blocks = {lang: _blocks(p.read_text(encoding="utf-8")) for lang, p in QUICKSTART.items()}
    assert blocks["zh"] == blocks["en"], "the Chinese and English quickstarts must carry the same commands and code"
    py = [b for k, b in blocks["zh"] if k == "python" and b.startswith('"""快速上手示例 Agent')]
    assert py == [(EXAMPLES / "plaza_watch.py").read_text(encoding="utf-8")]        # 文档中的 Agent 即示例文件全文
    text = "\n".join(b for _k, b in blocks["zh"])
    for rel in re.findall(r"(?:configs|docs)/[\w./-]+\.(?:json|py|md)", text):
        assert (ROOT / rel).exists(), rel
    for name in re.findall(r"--baseline (\w+)", "\n".join(b for k, b in blocks["zh"] if k == "bash")):
        assert name in BASELINES, name


def test_docs_name_the_three_baselines_and_their_sources() -> None:
    for p in QUICKSTART.values():
        md = p.read_text(encoding="utf-8")
        for name in D3_BASELINES:
            assert f"`{name}`" in md and f"src/drone4d/baselines/{name}.py" in md, (p.name, name)
        assert "src/drone4d/perception/ref_det.py" in md


@pytest.mark.parametrize("path", [QUICKSTART["zh"], QUICKSTART["en"], PKG / "README.md", DOCS / "README.md"], ids=lambda p: p.name)
def test_docs_pass_copy_lint(path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("[dep] copy-lint needs node")
    r = subprocess.run([node, "tools/lint/copy-lint.mjs", "--no-baseline", str(path.relative_to(ROOT))], cwd=ROOT, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
