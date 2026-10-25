"""D3 回归门与 I01 全量测试入口的运行环境（D3-AC-01；封板任务 K14；docs/05 F-113、F-114）。

- 回归门 pytest 步骤只覆盖 D2 范围（tests/d3 由 I01 承担），由 d2_suite.py 运行（失败用例隔离重跑）；在 AVX-512 主机上把 numpy 与 OpenBLAS 固定到 AVX2 路径，使 D1
  黄金摘要可比（C-41）；取消选择的用例逐条有说明，且由其他步骤覆盖；
- dmax 重载门容器设置打开文件数上限与 /dev/shm 大小，世界包同步保留 .status（缺少时六城判为 stale）；
- I01 入口主阶段使用 d3-test 镜像（d3-compile + rsync、git、make），gz 阶段使用按当前提交重建的 d3-gz；依赖 git 目录或
  docker 守护进程的用例在主机阶段运行、主阶段 --ignore 它们，合并计数含 -host 阶段；
- 论文大文件 m1geo_full.parquet 本地保留且与 external.json 一致（REL-01 由提交代理 git rm --cached）。

脚本渲染沿用 tools/deploy/dmax/audit_manifests.py 的记录桩（ssh、rsync、docker 不真正执行）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "tests" / "d3" / "regress_gate" / "gate.json"
DMAX = ROOT / "tools" / "deploy" / "dmax"
HOST_TESTS = ("tests/d3/deploy", "tests/d3/submit/test_redteam.py")
EXTERNAL_GIT_DIR = Path("/data/projs/.anet-drone4d.git")    # 本仓库的外置 git 目录（同 test_lint_hygiene.py）


def _audit():
    spec = importlib.util.spec_from_file_location("audit_manifests_k14", DMAX / "audit_manifests.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _step(sid: str) -> dict:
    return next(s for s in json.loads(GATE.read_text(encoding="utf-8"))["steps"] if s["id"] == sid)


def _flag_values(argv: list[str], flag: str) -> list[str]:
    out = []
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            out.append(argv[i + 1])
        elif a.startswith(flag + "="):
            out.append(a.split("=", 1)[1])
    return out


# ---------------------------------------------------------------------------------------------------------------------
# gate.json：pytest 步骤


def test_pytest_step_covers_the_d2_suite_only():
    argv = _step("pytest")["argv"]
    py = argv.index("{py}")
    assert argv[py + 1] == "tests/d3/regress_gate/d2_suite.py"     # 首轮全集 + 失败用例隔离重跑（test_d2_suite.py）
    rest = argv[argv.index("--") + 1:]
    assert rest[rest.index("-m") + 1] == "not perf"
    assert "--ignore=tests/d3" in rest and rest[-1] == "tests"
    assert not any(a.startswith("tests/d3") for a in rest), "tests/d3 runs in the I01 entry, not in the gate pytest step"
    assert "no:cacheprovider" in rest


def test_pytest_step_pins_the_avx2_paths_for_the_d1_goldens():
    argv = _step("pytest")["argv"]
    env = dict(a.split("=", 1) for a in argv[1:argv.index("{py}")])
    assert argv[0] == "env"
    assert set(env["NPY_DISABLE_CPU_FEATURES"].split()) >= {"X86_V4", "AVX512_ICL"}
    assert env["OPENBLAS_CORETYPE"] == "Haswell"
    assert "C-41" in _step("pytest")["doc"]


def test_deselected_cases_exist_and_are_explained():
    s = _step("pytest")
    sel = _flag_values(s["argv"], "--deselect")
    assert len(sel) == 4
    for nid in sel:
        path, _, func = nid.partition("::")
        assert (ROOT / path).is_file(), nid
        assert re.search(rf"^def {re.escape(func)}\(", (ROOT / path).read_text(encoding="utf-8"), re.M), nid
    doc = s["doc"]
    for word in ("node_modules", "front-end production build", "make dry-run", "contracts and lint steps", "public-build"):
        assert word in doc, word


def test_registry_golden_is_strict_outside_hosted_ci():
    # ISA 固定是为了让严格比较在 dmax 上成立；托管 CI（CI 变量）仍只告警（tests/planning/test_registry.py）
    src = (ROOT / "tests" / "planning" / "test_registry.py").read_text(encoding="utf-8")
    assert 'STRICT_DIGEST = not os.environ.get("CI")' in src


# ---------------------------------------------------------------------------------------------------------------------
# 脚本渲染：重载门与 I01 入口


@pytest.fixture(scope="module")
def audit():
    if not shutil.which("git"):
        pytest.skip("[platform] git not installed")
    return _audit()


def _runs(audit, script: str, env: dict[str, str]) -> list[list[str]]:
    code, calls, err = audit.render_shell(DMAX / script, (), env)
    assert code == 0, err
    return [c for c in calls if audit._is_run(c)]


def test_gate_container_sets_nofile_and_shm(audit):
    if not (ROOT / ".git").exists() and not EXTERNAL_GIT_DIR.is_dir():
        pytest.skip("[platform] run_d3_gate.sh resolves REF in the git directory (not available in the I01 container)")
    runs = _runs(audit, "run_d3_gate.sh", {"SYNC": "0", "WAIT": "0"})
    assert len(runs) == 1
    r = runs[0]
    assert _flag_values(r, "--ulimit") == ["nofile=1048576:1048576"]
    assert _flag_values(r, "--shm-size") == ["2g"]
    assert "d3-agent-py:dev" in r, "perf baseline host image (fingerprint) stays the same"
    code, calls, _ = audit.render_shell(DMAX / "run_d3_gate.sh", (), {"SYNC": "0", "WAIT": "0", "NOFILE": "lots"})
    assert code == 2 and not [c for c in calls if audit._is_run(c)]


def test_i01_entry_images_and_limits(audit):
    runs = _runs(audit, "run_d3_tests.sh", {"HOST": "0"})
    by_name = {r[r.index("--name") + 1]: r for r in runs}
    main, gz, summ = by_name["d3-test-main"], by_name["d3-test-main-gz"], by_name["d3-test-main-sum"]
    assert "d3-test:dev" in main and "d3-gz:dev" in gz and "d3-test:dev" in summ
    for r in (main, gz):
        assert _flag_values(r, "--ulimit") == ["nofile=1048576:1048576"]
    assert not any(a.startswith("--ignore=") for a in " ".join(main).split()), "HOST=0 keeps the host cases in the main stage"
    stages = [a for a in " ".join(summ).split() if a.startswith("--stages=")]
    assert stages == ["--stages==d3-test:dev,-gz=d3-gz:dev"]


def test_i01_host_stage_moves_host_cases_out_of_the_container(audit, tmp_path):
    fake = tmp_path / "fakepy"                               # 主机阶段的解释器桩：记录参数，不运行 pytest
    rec = tmp_path / "host-argv.txt"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {rec}\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    runs = _runs(audit, "run_d3_tests.sh", {"HOST": "1", "HOST_PY": str(fake), "PASS_ENV": "AWR_AC61_REPORT", "AWR_AC61_REPORT": "/out/ac61.json"})
    main = next(r for r in runs if "d3-test-main" in r)
    words = " ".join(main).split()
    for t in HOST_TESTS:
        assert f"--ignore={t}" in words
    assert "AWR_AC61_REPORT=/out/ac61.json" in words
    host_argv = rec.read_text(encoding="utf-8").split()
    assert host_argv[:2] == ["-m", "pytest"] and all(t in host_argv for t in HOST_TESTS)
    summ = next(r for r in runs if "d3-test-main-sum" in r)
    st = next(a for a in " ".join(summ).split() if a.startswith("--stages="))
    assert ",-host=host:" in st


def test_i01_rejects_bad_pass_env(audit):
    code, calls, _ = audit.render_shell(DMAX / "run_d3_tests.sh", (), {"HOST": "0", "PASS_ENV": "bad-name"})
    assert code == 2 and not [c for c in calls if audit._is_run(c)]


def test_world_sync_keeps_the_status_directory():
    for script in ("run_d3_gate.sh", "run_d3_tests.sh"):
        src = (DMAX / script).read_text(encoding="utf-8")
        sync = [ln for ln in src.splitlines() if "rsync" in ln and "/worlds/" in ln]
        assert sync, script
        assert all("--exclude /.status" not in ln for ln in sync), script
        # Publisher 在只读卷上 mkdir(exist_ok=True)：.locks、.staging、.trash 须作为空目录存在（只排除其内容）
        for d in (".locks", ".staging", ".trash"):
            assert all(f"--exclude '/{d}/*'" in ln and f"--exclude /{d} " not in ln for ln in sync), (script, d)


# ---------------------------------------------------------------------------------------------------------------------
# 镜像定义


def test_test_image_adds_the_entry_tools_on_top_of_d3_compile():
    df = (DMAX / "images" / "test" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG BASE=d3-compile:dev$", df, re.M) and "FROM ${BASE}" in df
    assert re.search(r"apt-get install -y --no-install-recommends rsync git make\b", df)
    assert "import datafusion" in df                     # requirements-d3.lock 的 datafusion（rerun catalog）须在基础层
    assert "datafusion==" in (ROOT / "requirements-d3.lock").read_text(encoding="utf-8")
    build = (DMAX / "images" / "test" / "build.sh").read_text(encoding="utf-8")
    order = [build.index(f'"$IMG/{n}/build.sh"') for n in ("sensorsvc", "compile")]
    assert order == sorted(order) and "d3-test:$TAG" in build
    assert "not amd64" in build                          # 共享标签 python:3.12-slim 被改指其他架构时拒绝构建
    sv = (DMAX / "images" / "sensorsvc" / "build.sh").read_text(encoding="utf-8")
    assert "${PY_BASE:+--build-context python:3.12-slim=docker-image://$PY_BASE}" in sv


def test_gz_stage_image_is_built_from_the_current_commit():
    build = (DMAX / "images" / "gz" / "build.sh").read_text(encoding="utf-8")
    assert "D4D_PLUGIN_BUILD=$BUILD" in build and "rev-parse --short HEAD" in build


# ---------------------------------------------------------------------------------------------------------------------
# REL-01：本地保留的论文大文件与清单一致


def test_local_external_file_matches_the_manifest():
    data = ROOT / "research" / "ccf-tpci" / "data"
    for e in json.loads((data / "external.json").read_text(encoding="utf-8"))["files"]:
        p = data / e["name"]
        if not p.exists():
            continue                                     # 新检出：由 fetch_external.py 获取（test_lint_hygiene.py 覆盖）
        assert p.stat().st_size == e["bytes"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == e["sha256"]


def test_i01_dirty_tag_hashes_regular_files_only():
    # 工作树副本以符号链接接入 node_modules 等目录时，未跟踪项摘要不得把目录交给 sha256sum（K14 实测 CM-00 渲染失败）
    src = (DMAX / "run_d3_tests.sh").read_text(encoding="utf-8")
    tag = src[src.index("TAG=$SHA-dirty-"):]
    tag = tag[:tag.index("cut -c1-8)")]
    assert "git ls-files -o --exclude-standard -z" in tag
    assert 'if [ -f "$f" ]; then printf' in tag and tag.index('[ -f "$f" ]') < tag.index("xargs -0 -r sha256sum")
    # set -e 与 pipefail：`[ -f ] && printf` 在最后一项不是普通文件时使循环以 1 退出、脚本中止，循环体须用 if
    assert '[ -f "$f" ] &&' not in src
