"""D3 测试统一入口的跳过分层（tests/d3/conftest.py；D3 封板 I01）：原因归层、严格层改判失败、摘要计数与逐用例结果。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _load():
    spec = importlib.util.spec_from_file_location("_d3_root_conftest", CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load()


@pytest.mark.parametrize(("reason", "tier", "detail"), [
    ("could not import 'open3d': No module named 'open3d'", "dep", "open3d"),
    ("could not import 'mcap_protobuf': No module named 'mcap_protobuf'", "dep", "mcap_protobuf"),
    ("No module named 'google.protobuf'", "dep", "google"),
    ("EGL unavailable: cannot create context", "gpu", None),
    ("set AWR_GEOSVC_CROP_WORLD=<world dir> (crop)", "optin", None),
    ("perf bound disabled (AWR_D3_PERF=0)", "optin", None),
    ("formal timing runs in the dmax CPU pool (AWR_D3_EVAL_TIMING_* unset)", "optin", None),
    ("needs worlds/oryahovo-crop1", "data", None),
    ("六城 World Package 未构建（make worlds）", "data", None),
    ("sanfrancisco 未构建", "data", None),
    ("needs gz-sim with libDrone4DGz.so (d3-gz image)", "platform", None),
    ("kernel dsm-v2 unavailable; the D2 baseline was recorded with it", "platform", None),
    ("[gpu] warp kernels need a CUDA device", "gpu", None),
    ("[data] could not import 'x'", "data", None),                         # 显式层名优先于关键字推断
    ("something else entirely", "other", None),
])
def test_classify_skip(reason: str, tier: str, detail: str | None) -> None:
    assert C.classify_skip(reason) == (tier, detail)


def test_skip_reason_strips_prefix_and_handles_tuple() -> None:
    assert C.skip_reason(("/x.py", 3, "Skipped: could not import 'a'")) == "could not import 'a'"
    assert C.skip_reason("Skipped: EGL unavailable") == "EGL unavailable"
    assert C.skip_reason(None) == ""


def test_required_tiers_parse() -> None:
    assert C.required_tiers(None) == frozenset()
    assert C.required_tiers(" dep, gpu ,") == frozenset({"dep", "gpu"})
    assert C.required_tiers("all") == frozenset(C.TIERS)
    with pytest.raises(pytest.UsageError, match="unknown tier"):
        C.required_tiers("dep,gpus")


SAMPLE = {
    "test_mod_skip.py": """
        import pytest
        pytest.importorskip("no_such_module_d3_i01")

        def test_never():
            pass
    """,
    "test_mixed.py": """
        import pytest

        @pytest.fixture
        def needs_dep():
            pytest.importorskip("no_such_module_d3_i01_fx")

        def test_pass():
            pass

        def test_fail():
            assert 0

        def test_dep_fixture(needs_dep):
            pass

        def test_gpu():
            pytest.skip("EGL unavailable: no display")

        @pytest.mark.skipif(True, reason="needs worlds/oryahovo-crop1")
        def test_data():
            pass

        @pytest.mark.xfail(reason="known")
        def test_xfail():
            assert 0

        @pytest.fixture
        def broken():
            raise RuntimeError("setup boom")

        def test_error(broken):
            pass
    """,
}


def _run(tmp_path: Path, require: str | None, *args: str) -> tuple[int, dict, str]:
    d = tmp_path / "suite"
    d.mkdir(parents=True)
    (d / "conftest.py").write_text(CONFTEST.read_text())
    for name, body in SAMPLE.items():
        (d / name).write_text(textwrap.dedent(body))
    (d / "pytest.ini").write_text("[pytest]\n")
    out = tmp_path / "summary.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWR_D3_")}
    env["AWR_D3_SUMMARY"] = str(out)
    if require is not None:
        env["AWR_D3_REQUIRE"] = require
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(d / "pytest.ini"),
                        "--rootdir", str(d), *args, str(d)], capture_output=True, text=True, env=env, cwd=d, timeout=120)
    return p.returncode, json.loads(out.read_text()), p.stdout


def test_summary_counts_and_tiers(tmp_path: Path) -> None:
    rc, s, stdout = _run(tmp_path, None)
    assert rc == 1                                                            # test_fail 失败
    assert s["counts"] == {"passed": 1, "failed": 1, "error": 1, "skipped": 4, "xfailed": 1, "xpassed": 0,
                           "collect_errors": 0, "skipped_modules": 1}
    assert s["skipped_by_tier"] == {"dep": 2, "gpu": 1, "optin": 0, "data": 1, "platform": 0, "other": 0}
    assert s["missing_deps"] == {"no_such_module_d3_i01": 1, "no_such_module_d3_i01_fx": 1}
    assert s["required_tiers"] == [] and s["required_skip_failures"] == 0
    t = s["tests"]
    assert t["test_mixed.py::test_pass"] == "passed" and t["test_mixed.py::test_fail"] == "failed"
    assert t["test_mixed.py::test_error"] == "error" and t["test_mixed.py::test_xfail"] == "xfailed"
    assert t["test_mod_skip.py"] == "skipped" and t["test_mixed.py::test_dep_fixture"] == "skipped"
    kinds = {x["nodeid"]: (x["kind"], x["tier"]) for x in s["skips"]}
    assert kinds["test_mod_skip.py"] == ("collect", "dep") and kinds["test_mixed.py::test_dep_fixture"] == ("setup", "dep")
    assert kinds["test_mixed.py::test_gpu"] == ("call", "gpu") and kinds["test_mixed.py::test_data"] == ("setup", "data")
    assert "D3 skip tiers" in stdout and "missing deps: no_such_module_d3_i01 1" in stdout


def test_required_tiers_turn_skips_into_failures(tmp_path: Path) -> None:
    rc, s, stdout = _run(tmp_path, "dep,gpu")
    assert rc == 2 and s["counts"]["collect_errors"] == 1 and not s["tests"].get("test_mixed.py::test_pass")   # 收集错误中断会话
    rc, s, stdout = _run(tmp_path / "b", "dep,gpu", "--continue-on-collection-errors")             # 统一入口带此选项
    assert rc == 1
    c = s["counts"]
    assert c["collect_errors"] == 1 and c["skipped_modules"] == 0                  # 模块级依赖跳过改判为收集错误
    assert s["tests"]["test_mixed.py::test_dep_fixture"] == "error"              # setup 阶段依赖跳过改判为 error
    assert s["tests"]["test_mixed.py::test_gpu"] == "failed"                     # call 阶段 GPU 跳过改判为 failed
    assert s["tests"]["test_mixed.py::test_data"] == "skipped"                   # 非严格层仍为跳过
    assert s["required_skip_failures"] == 3 and s["skipped_by_tier"]["dep"] == 0 and s["skipped_by_tier"]["data"] == 1
    assert "required-tier skips counted as failures: 3" in stdout
    assert "[AWR_D3_REQUIRE] skip in required tier 'gpu'" in stdout and "required tier 'dep'" in stdout


def test_unknown_required_tier_is_usage_error(tmp_path: Path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    (d / "conftest.py").write_text(CONFTEST.read_text())
    (d / "test_a.py").write_text("def test_a():\n    pass\n")
    (d / "pytest.ini").write_text("[pytest]\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWR_D3_")} | {"AWR_D3_REQUIRE": "gpus"}
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(d / "pytest.ini"),
                        "--rootdir", str(d), str(d)], capture_output=True, text=True, env=env, cwd=d, timeout=120)
    assert p.returncode != 0 and "unknown tier" in (p.stdout + p.stderr)


# ==================================================================== 统一入口的阶段合并（tools/deploy/dmax/d3_tests_summary.py）

SUMMARY_TOOL = Path(__file__).resolve().parents[2] / "tools" / "deploy" / "dmax" / "d3_tests_summary.py"


def _stage(out: Path, sfx: str, rc: int, tests: dict[str, str], skips: list[dict], collect_errors=()) -> None:
    (out / f"rc{sfx}.txt").write_text(f"{rc}\n")
    (out / f"summary{sfx}.json").write_text(json.dumps({
        "counts": {}, "skipped_by_tier": {}, "missing_deps": {}, "required_tiers": ["dep", "gpu"], "required_skip_failures": 0,
        "duration_s": 1.0, "python": "3.12", "tests": tests, "collect_errors": list(collect_errors),
        "skips": [{"required": False, "detail": None, **s} for s in skips]}))


def _tool(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if not k.startswith("D3T_")} | (env or {})
    return subprocess.run([sys.executable, str(SUMMARY_TOOL), *args], capture_output=True, text=True, env=e, timeout=60)


def test_merge_takes_run_outcome_over_skip_and_worst_of_runs(tmp_path: Path) -> None:
    gz = "tests/d3/phys/test_gz_lockstep.py::test_hover"
    _stage(tmp_path, "", 0, {"a::p": "passed", gz: "skipped", "a::both": "passed", "a::data": "skipped", "a::dep": "skipped"},
           [{"nodeid": gz, "tier": "platform", "reason": "needs gz-sim"}, {"nodeid": "a::data", "tier": "data", "reason": "needs worlds/x"},
            {"nodeid": "a::dep", "tier": "dep", "detail": "rerun", "reason": "could not import 'rerun'"}])
    _stage(tmp_path, "-gz", 1, {gz: "passed", "a::both": "failed"}, [])
    p = _tool("--out", str(tmp_path), "--stages", "=d3-sensorsvc:dev,-gz=d3-gz:dev", env={"D3T_TAG": "abc-dirty-1", "D3T_GPU": "2"})
    assert p.returncode == 1, p.stderr                                          # gz 阶段退出码非零
    c = json.loads((tmp_path / "counts.json").read_text())
    assert c["tag"] == "abc-dirty-1" and c["gpu"] == "2" and c["schema"] == "awr.d3.test_counts.v1" and not c["ok"]
    assert c["counts"] == {"passed": 2, "failed": 1, "error": 0, "skipped": 2, "xfailed": 0, "xpassed": 0, "total": 5, "collect_errors": 0}
    assert c["skipped_by_tier"]["platform"] == 0 and c["skipped_by_tier"]["data"] == 1 and c["missing_deps"] == {"rerun": 1}
    assert c["not_passed"] == ["a::both"] and set(c["stages"]) == {"main", "gz"}
    assert json.loads((tmp_path / "meta.json").read_text())["tag"] == "abc-dirty-1"
    assert "not passed: a::both" in p.stdout


def test_merge_ok_only_when_every_stage_clean_and_present(tmp_path: Path) -> None:
    _stage(tmp_path, "", 0, {"a::p": "passed"}, [])
    assert _tool("--out", str(tmp_path), "--stages", "=d3-sensorsvc:dev").returncode == 0
    assert json.loads((tmp_path / "counts.json").read_text())["ok"]
    p = _tool("--out", str(tmp_path), "--stages", "=d3-sensorsvc:dev,-gz=d3-gz:dev")         # gz 阶段无摘要：不算通过
    assert p.returncode == 1 and json.loads((tmp_path / "counts.json").read_text())["stages"]["gz"]["summary"] is None
    _stage(tmp_path, "", 0, {"a::p": "passed"}, [], collect_errors=["tests/d3/rerun/test_export.py"])
    p = _tool("--out", str(tmp_path), "--stages", "=d3-sensorsvc:dev")
    c = json.loads((tmp_path / "counts.json").read_text())
    assert c["counts"]["collect_errors"] == 1 and c["collect_errors"] == ["main:tests/d3/rerun/test_export.py"]
    assert _tool("--out", str(tmp_path), "--stages", "-gz").returncode == 2                  # 缺镜像名为参数错误
