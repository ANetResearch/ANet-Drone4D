"""D3 回归门合入前强制（D3-AC-01；AWR-05 §4.8；封板任务 G05）：mk/d3.mk 的档位与证据检查、托管 CI 的 d3-gate 任务、
dmax 重载部分启动脚本的登记。

- 档位：轻量门（GATE_D3_LIGHT）与重载部分（GATE_D3_HEAVY）只含 gate.json 登记的缺省步骤；轻量门不含需要数据、浏览器或
  独占主机的步骤；重载部分与其余缺省步骤合起来覆盖完整门（夜间作业的两份报告）。
- 证据检查（make gate-d3-check）：以被检查内容的 tree id 为键，每步取最近一次结果；缺步、失败、运行期间树发生变化的报告
  均不通过；完整门可由多份报告合并覆盖。
- 导出副本运行（GATE_REF）：实际执行一个轻量门步骤，报告与 meta.json 记录提交、tree id 与未登记步骤，副本与临时索引在结束后删除。
- pre-push 钩子只拦截 refs/heads/d3-dev，并对被推送的提交执行检查。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MK = ROOT / "mk" / "d3.mk"
GATE = ROOT / "tests" / "d3" / "regress_gate" / "gate.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
DMAX = ROOT / "tools" / "deploy" / "dmax" / "run_d3_gate.sh"
EXTERNAL_GIT_DIR = Path("/data/projs/.anet-drone4d.git")


def _mk_var(name: str) -> list[str]:
    m = re.search(rf"^{name}\s*\?=\s*(\S+)\s*$", MK.read_text(encoding="utf-8"), re.M)
    assert m, f"{name} not defined in mk/d3.mk"
    return m.group(1).split(",")


def _gate_steps() -> dict[str, dict]:
    return {s["id"]: s for s in json.loads(GATE.read_text(encoding="utf-8"))["steps"]}


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    if not (ROOT / ".git").exists() and "GIT_DIR" not in env and EXTERNAL_GIT_DIR.is_dir():
        env.update(GIT_DIR=str(EXTERNAL_GIT_DIR), GIT_WORK_TREE=str(ROOT))
    return env


def _need_tools() -> dict[str, str]:
    if not (shutil.which("make") and shutil.which("bash") and shutil.which("git")):
        pytest.skip("[platform] make, bash or git not available")
    env = _git_env()
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        pytest.skip("[platform] git repository not reachable (export GIT_DIR for the external git directory)")
    if not (ROOT / ".venv" / "bin" / "python").exists():
        pytest.skip("[platform] .venv not set up (make setup)")
    return env


def _make(*args: str, env: dict[str, str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["make", "--no-print-directory", "-s", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout, check=False)


# ==================================================================== 档位与 gate.json


def test_tiers_use_registered_default_steps() -> None:
    steps = _gate_steps()
    light, heavy = _mk_var("GATE_D3_LIGHT"), _mk_var("GATE_D3_HEAVY")
    for sid in light + heavy:
        assert sid in steps, f"{sid} not registered in gate.json"
        assert steps[sid].get("default", True), f"{sid} is not a default step"
    # 轻量门在托管 CI 上运行：不含 D2 测试全集（需要城市世界包）、1 h 零影响对拍、独占主机性能子集与浏览器 e2e
    assert not set(light) & {"pytest", "zero-impact", "perf", "public-e2e"}
    assert {"contracts", "web-unit", "lint", "imports", "public-build", "k9-d3", "demo-parity", "lab-build"} <= set(light)
    assert {"pytest", "zero-impact", "resim"} <= set(heavy)
    # G02 遗留 6：扩展点 15 构建对拍与 /lab/ 静态构建登记为门步骤（第 6 条），排在 public-build 之后
    order = list(steps)
    assert steps["demo-parity"]["argv"] == ["make", "demo-parity"] and steps["lab-build"]["argv"] == ["make", "lab-build"]
    assert {steps["demo-parity"]["item"], steps["lab-build"]["item"]} == {6}
    assert order.index("public-build") < order.index("demo-parity") and order.index("public-build") < order.index("lab-build")
    # dmax 脚本的缺省步骤与 GATE_D3_HEAVY 一致
    m = re.search(r"^STEPS=\$\{STEPS:-(\S+)\}$", DMAX.read_text(encoding="utf-8"), re.M)
    assert m and m.group(1).split(",") == heavy


# ==================================================================== 证据检查


def _write_report(root: Path, tag: str, tier: str, steps: dict[str, bool], *, utc: str, tree_changed: bool = False,
                  meta: bool = True) -> None:
    d = root / tag / tier
    d.mkdir(parents=True, exist_ok=True)
    rep = {"schema": "awr.d3.regress_gate.report.v1", "utc": utc, "pass": all(steps.values()),
           "steps": {k: {"pass": v, "item": 1} for k, v in steps.items()}}
    (d / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    (d / "meta.json").unlink(missing_ok=True)
    if meta:
        m = {"schema": "awr.d3.regress_gate.meta.v1", "tier": tier, "tag": tag, "tree_changed": tree_changed}
        (d / "meta.json").write_text(json.dumps(m), encoding="utf-8")


def test_gate_check_joins_reports_by_tree(tmp_path: Path) -> None:
    env = _need_tools()
    r = _make("gate-d3-tag", "GATE_REF=HEAD", env=env)
    assert r.returncode == 0, r.stderr
    tag = r.stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout
    assert tag == tree.strip()[:12]
    env = {**env, "GATE_D3_ROOT": str(tmp_path)}
    check = lambda tier="light": _make("gate-d3-check", "GATE_REF=HEAD", f"GATE_TIER={tier}", env=env)  # noqa: E731
    light, heavy = _mk_var("GATE_D3_LIGHT"), _mk_var("GATE_D3_HEAVY")
    default = [k for k, s in _gate_steps().items() if s.get("default", True)]

    r = check()
    assert r.returncode != 0 and "FAIL" in r.stdout and "missing" in r.stdout            # 没有证据（make 以 2 退出）

    _write_report(tmp_path, tag, "light", {s: True for s in light}, utc="20261019T010000Z")
    r = check()
    assert r.returncode == 0 and "PASS" in r.stdout, r.stdout
    assert check("full").returncode != 0                                                  # 完整门缺重载部分

    _write_report(tmp_path, tag, "heavy", {s: True for s in heavy}, utc="20261019T020000Z")
    rest = [s for s in default if s not in set(light) | set(heavy)]
    _write_report(tmp_path, tag, "full", {s: True for s in rest}, utc="20261019T030000Z")
    r = check("full")
    assert r.returncode == 0, r.stdout                                                    # 三份报告合并覆盖完整门

    _write_report(tmp_path, tag, "light", {**{s: True for s in light}, "lint": False}, utc="20261019T040000Z")
    r = check()
    assert r.returncode != 0 and "lint (light" in r.stdout                                 # 每步取最近一次结果

    _write_report(tmp_path, tag, "light", {s: True for s in light}, utc="20261019T050000Z", tree_changed=True)
    r = check()
    assert r.returncode != 0 and "tree changed during the run" in r.stdout                # 运行中树变化的报告不采信

    _write_report(tmp_path, tag, "light", {s: True for s in light}, utc="20261019T060000Z", meta=False)
    assert "no meta.json" in check().stdout

    r = check("nope")
    assert r.returncode != 0 and "unknown GATE_TIER" in r.stderr
    r = _make("gate-d3-check", "GATE_REF=no-such-ref-g05", env=env)
    assert r.returncode != 0 and "cannot resolve" in r.stderr


# ==================================================================== 导出副本中实际运行一个轻量门步骤


def test_gate_runs_a_step_on_an_exported_commit(tmp_path: Path) -> None:
    env = {**_need_tools(), "GATE_D3_ROOT": str(tmp_path)}
    tag = _make("gate-d3-tag", "GATE_REF=HEAD", env=env).stdout.strip()
    r = _make("gate-d3", "GATE_REF=HEAD", "GATE_STEPS=imports,no-such-step-g05", env=env, timeout=900)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    d = tmp_path / tag / "full"
    rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert rep["pass"] and list(rep["steps"]) == ["imports"]
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, env=_git_env(), capture_output=True, text=True, check=True).stdout.strip()
    assert meta["commit"] == sha and meta["tag"] == tag and meta["ref"] == "HEAD" and not meta["dirty"] and not meta["tree_changed"]
    assert meta["not_registered"] == ["no-such-step-g05"] and meta["steps"] == ["imports"] and meta["exit"] == 0
    assert Path(meta["src"]).name.startswith(f"src-{tag}.") and not Path(meta["src"]).exists()   # 副本已删除
    assert ROOT not in Path(meta["src"]).parents                                          # 副本在本检出之外
    assert not Path(meta["src"] + ".index").exists()                                     # 临时索引已删除
    assert (d / "gate.log").read_text(encoding="utf-8").count("[d3-gate] imports: PASS") == 1
    # 只登记了一步：轻量门证据不完整
    r = _make("gate-d3-check", "GATE_REF=HEAD", env=env)
    assert r.returncode != 0 and "missing" in r.stdout


# ==================================================================== pre-push 钩子


def test_pre_push_hook_only_guards_d3_dev(tmp_path: Path) -> None:
    env = _need_tools()
    bare = tmp_path / "hooks-only.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    r = _make("gate-d3-hook", env={**env, "GIT_DIR": str(bare)})
    assert r.returncode == 0, r.stderr
    hook = bare / "hooks" / "pre-push"
    text = hook.read_text(encoding="utf-8")
    assert os.access(hook, os.X_OK) and "refs/heads/d3-dev" in text and 'GATE_REF="$lsha"' in text and f"-C {ROOT} " in text
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout.strip()
    zero = "0" * 40
    run = lambda line: subprocess.run(["bash", str(hook), "origin", "x"], input=line, cwd=ROOT, text=True, capture_output=True,  # noqa: E731
                                      env={**env, "GATE_D3_ROOT": str(tmp_path / "evidence")}, timeout=300, check=False)
    assert run(f"refs/heads/main {sha} refs/heads/main {zero}\n").returncode == 0           # 其他分支不拦截
    assert run(f"(delete) {zero} refs/heads/d3-dev {sha}\n").returncode == 0                # 删除分支不检查
    r = run(f"refs/heads/d3-dev {sha} refs/heads/d3-dev {zero}\n")
    assert r.returncode != 0 and "FAIL" in r.stdout                                       # 无证据：拒绝推送


# ==================================================================== 托管 CI 与 dmax 启动脚本


def test_ci_runs_the_light_gate_on_d3_dev() -> None:
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))
    assert "d3-dev" in on["push"]["branches"] and "main" in on["push"]["branches"] and "pull_request" in on
    job = wf["jobs"]["d3-gate"]
    cond = job["if"]
    assert "refs/heads/d3-dev" in cond and "github.base_ref == 'd3-dev'" in cond and "workflow_dispatch" in cond
    steps = job["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@") and steps[0]["with"]["fetch-depth"] == 0   # demo-parity 需要 v0.2.1
    runs = [s.get("run", "") for s in steps]
    idx = lambda needle: next(i for i, x in enumerate(runs) if needle in x)  # noqa: E731
    assert idx("make setup") < idx("make demo-world") < idx("make gate-d3-light") < idx("make gate-d3-check GATE_TIER=light")
    assert steps[-1].get("if") == "always()" and "GITHUB_STEP_SUMMARY" in runs[-1]
    # 原有 G1h 任务保持不变（main 与 d3-dev 的 push、全部 PR 上运行）
    assert {"checks", "pytest"} <= set(wf["jobs"]) and "if" not in wf["jobs"]["checks"] and "if" not in wf["jobs"]["pytest"]


def test_dmax_heavy_runner_is_registered_and_parses() -> None:
    text = (ROOT / "tools" / "deploy" / "dmax" / "audit_manifests.py").read_text(encoding="utf-8")
    assert '"{SCRIPT_ROOT}/run_d3_gate.sh"' in text.replace("f\"", "\"")
    src = DMAX.read_text(encoding="utf-8")
    for needle in ("--network none", "--cgroup-parent d3.slice", "--cpus $CPUS", "--memory $MEM", "--name $N", "N=d3-test-gate-"):
        assert needle in src, needle
    assert "--gpus" not in src                                                           # CPU 池，不挂 GPU
    assert "git archive" in src and "--require-hashes" in src and "--no-index" in src     # 只取提交；离线、逐个校验哈希
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n", str(DMAX)], check=False).returncode == 0
