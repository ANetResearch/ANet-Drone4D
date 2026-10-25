"""D2 环境下 tests/d3 的收集卫生（D3-AC-01 回归门第 1 项 pytest 步骤；托管 CI pytest 分片）。

回归门的 pytest 步骤与托管 CI 在 D2 环境（.venv，requirements.lock）中运行全集，其中不含 requirements-d3.in 与
D3 镜像的依赖。tests/d3 下任一模块在模块级（直接或经 awr.* 间接）导入这些依赖而不先 ``pytest.importorskip``，
收集阶段即报错并中断整个会话。本用例在子进程中屏蔽 D3 依赖后只收集 tests/d3，要求收集错误为 0，
缺依赖的模块以跳过（dep 层）出现。屏蔽在任何环境下都生效，因此在 .venv-d3 中运行同样能发现问题。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# D3 依赖的顶层导入名：requirements-d3.in（pillow、mcap-protobuf-support 及其 protobuf、foxglove-schemas-protobuf、
# open3d-cpu、pyzmq、rerun-sdk、av、datafusion）与 D3 镜像的 requirements-*.in（moderngl、glcontext、warp-lang、
# nvidia-*、cuda-bindings）。D2 锁文件已有的包（mcap、zstandard、msgpack、jsonschema、numba 等）不在此列。
D3_ONLY = ("PIL", "mcap_protobuf", "google", "foxglove_schemas_protobuf", "open3d", "zmq", "rerun", "av", "datafusion",
           "moderngl", "glcontext", "warp", "nvidia", "cuda")

_BLOCK = """
import sys
from importlib.abc import MetaPathFinder
BLOCKED = frozenset({blocked!r})
class _D3Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named {{name!r}}", name=name)
        return None
for _m in [m for m in sys.modules if m.split(".")[0] in BLOCKED]:
    del sys.modules[_m]
sys.meta_path.insert(0, _D3Block())
import pytest
sys.exit(pytest.main(["--collect-only", "-q", "-p", "no:warnings", "-p", "no:cacheprovider", *{targets!r}]))
"""


def _collect(blocked: tuple[str, ...], targets: tuple[str, ...] = ("tests/d3",), cwd: Path = REPO) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWR_D3_")}
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "python"), *filter(None, [env.get("PYTHONPATH")])])
    return subprocess.run([sys.executable, "-c", _BLOCK.format(blocked=sorted(blocked), targets=list(targets))], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=600)


def test_tests_d3_collect_without_d3_dependencies():
    """屏蔽 D3 依赖后 tests/d3 收集无错误（每个模块级导入 D3 依赖的测试模块都先 importorskip）。"""
    r = _collect(D3_ONLY)
    out = r.stdout + r.stderr
    errors = sorted(set(re.findall(r"^ERROR (\S+)", out, re.M)))
    assert r.returncode == 0 and not errors, (
        f"tests/d3 collection fails without the D3 dependencies (rc={r.returncode}); add pytest.importorskip(<module>) "
        f"before the first awr import of: {errors}\n{out[-4000:]}")
    m = re.search(r"collect_errors (\d+)", out)
    assert m and int(m.group(1)) == 0, out[-2000:]
    assert re.search(r"^\d+ tests? collected", out, re.M), out[-2000:]


def test_blocker_reports_a_missing_import_as_collection_error(tmp_path):
    """屏蔽器自检：未加 importorskip 的模块级导入在子进程收集中确实报收集错误，加了的以跳过出现（本用例不是空转）。"""
    (tmp_path / "test_bare.py").write_text("import zmq  # noqa: F401\n\n\ndef test_probe():\n    pass\n", encoding="utf-8")
    (tmp_path / "test_guarded.py").write_text("import pytest\n\npytest.importorskip(\"zmq\")\n\n\ndef test_probe():\n    pass\n",
                                              encoding="utf-8")
    bare = _collect(("zmq",), ("test_bare.py",), tmp_path)
    assert bare.returncode != 0 and re.search(r"^ERROR test_bare\.py", bare.stdout + bare.stderr, re.M), bare.stdout[-2000:]
    guarded = _collect(("zmq",), ("test_guarded.py",), tmp_path)
    assert "ERROR" not in guarded.stdout and re.search(r"no tests collected|1 skipped|skipped", guarded.stdout), guarded.stdout[-2000:]
