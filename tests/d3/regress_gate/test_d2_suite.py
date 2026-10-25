"""回归门第 1 条 D2 测试全集运行器（tests/d3/regress_gate/d2_suite.py；D3-AC-01，封板任务 K14）的判定规则。

在临时目录里造小型用例集（通过、首轮失败而重跑通过、每次都失败、收集错误），以真实 pytest 子进程运行，核对报告与退出码。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SUITE = ROOT / "tests" / "d3" / "regress_gate" / "d2_suite.py"
GATE = ROOT / "tests" / "d3" / "regress_gate" / "gate.json"


@pytest.fixture(scope="module")
def ds():
    spec = importlib.util.spec_from_file_location("d2_suite_k14", SUITE)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _write(d: Path, name: str, body: str) -> None:
    (d / name).write_text(body, encoding="utf-8")


FLAKY = """
from pathlib import Path

def test_flaky():
    p = Path(__file__).with_suffix(".count")
    n = int(p.read_text()) if p.exists() else 0
    p.write_text(str(n + 1))
    assert n >= 1, "first run fails (wall-clock sensitive case)"
"""


def _run(ds, d: Path, *args: str, **kw) -> dict:
    return ds.run_suite(["-q", "-p", "no:cacheprovider", *args], cwd=d, **kw)


def test_first_run_pass_needs_no_rerun(ds, tmp_path):
    _write(tmp_path, "test_a.py", "def test_ok():\n    assert True\n")
    rep = _run(ds, tmp_path, "test_a.py")
    assert rep["schema"] == "awr.d3.regress_gate.d2_suite.v1"
    assert rep["pass"] and rep["first"]["exit"] == 0 and rep["reruns"] == [] and rep["load_sensitive"] == []


def test_case_that_passes_in_isolation_is_load_sensitive(ds, tmp_path):
    _write(tmp_path, "test_a.py", "def test_ok():\n    assert True\n")
    _write(tmp_path, "test_flaky.py", FLAKY)
    rep = _run(ds, tmp_path, ".")
    assert rep["first"]["exit"] == 1 and rep["first"]["failed"] == ["test_flaky.py::test_flaky"]
    assert rep["pass"] and rep["load_sensitive"] == ["test_flaky.py::test_flaky"]
    assert [t["exit"] for t in rep["reruns"][0]["tries"]] == [0]           # 一次通过即停止重跑


def test_case_failing_every_rerun_fails_the_step(ds, tmp_path):
    _write(tmp_path, "test_b.py", "def test_bad():\n    assert False\n\ndef test_ok():\n    assert True\n")
    rep = _run(ds, tmp_path, ".", reruns=2)
    assert not rep["pass"] and rep["load_sensitive"] == []
    assert rep["reruns"][0]["nodeid"] == "test_b.py::test_bad" and [t["exit"] for t in rep["reruns"][0]["tries"]] == [1, 1]
    assert "failed again in every isolated rerun" in rep["reason"]


def test_setup_errors_are_rerun_like_failures(ds, tmp_path):
    _write(tmp_path, "test_c.py", "import pytest\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError('x')\n\n"
                                  "def test_needs(broken):\n    pass\n")
    rep = _run(ds, tmp_path, ".", reruns=1)
    assert rep["first"]["failed"] == ["test_c.py::test_needs"] and not rep["pass"]


def test_collection_errors_are_not_rerun(ds, tmp_path):
    _write(tmp_path, "test_d.py", "import does_not_exist_k14\n")
    _write(tmp_path, "test_flaky.py", FLAKY)
    rep = _run(ds, tmp_path, ".", "--continue-on-collection-errors")
    assert not rep["pass"] and rep["reruns"] == [] and rep["first"]["collect_errors"] == ["test_d.py"]
    assert rep["reason"].startswith("collection errors")


def test_wide_failure_is_a_regression_without_reruns(ds, tmp_path):
    _write(tmp_path, "test_e.py", "import pytest\n\n@pytest.mark.parametrize('i', range(4))\ndef test_many(i):\n    assert False\n")
    rep = _run(ds, tmp_path, ".", max_cases=3)
    assert not rep["pass"] and rep["reruns"] == [] and "treated as a regression" in rep["reason"]


def test_session_error_without_failed_cases_is_not_rerun(ds, tmp_path):
    _write(tmp_path, "conftest.py", "def pytest_sessionfinish(session, exitstatus):\n    raise OSError(24, 'Too many open files')\n")
    _write(tmp_path, "test_a.py", "def test_ok():\n    assert True\n")
    rep = _run(ds, tmp_path, ".")
    # pytest 以非 0 退出却没有失败用例（会话收尾崩溃，例如 F-114 的 Too many open files）：不重跑，直接不通过
    assert not rep["pass"] and rep["first"]["exit"] != 0 and rep["first"]["failed"] == [] and rep["reruns"] == []
    assert "session ended" in rep["reason"]


def test_cli_report_and_exit_code(ds, tmp_path, capsys, monkeypatch):
    _write(tmp_path, "test_flaky.py", FLAKY)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "rep" / "r.json"
    assert ds.main(["--report", str(out), "--", "-q", "-p", "no:cacheprovider", "."]) == 0
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("D2-SUITE-REPORT ")]
    assert len(line) == 1 and json.loads(line[0].split(" ", 1)[1]) == json.loads(out.read_text(encoding="utf-8"))
    with pytest.raises(SystemExit):
        ds.main([])


def test_plugin_flags_are_kept_for_reruns(ds):
    assert ds.plugin_flags(["-q", "-p", "no:cacheprovider", "-pno:warnings", "-m", "not perf", "tests"]) == \
        ["-p", "no:cacheprovider", "-pno:warnings"]


def test_gate_pytest_step_runs_the_suite_runner():
    s = next(x for x in json.loads(GATE.read_text(encoding="utf-8"))["steps"] if x["id"] == "pytest")
    argv = s["argv"]
    py = argv.index("{py}")
    assert argv[py + 1] == "tests/d3/regress_gate/d2_suite.py"
    own, rest = argv[py + 2:argv.index("--")], argv[argv.index("--") + 1:]
    assert own == ["--reruns", "3", "--max-reruns-cases", "8"]
    assert rest[:2] == ["-q", "-m"] and rest[2] == "not perf"
    assert "isolated rerun" in s["doc"] and "load_sensitive" in s["doc"]
