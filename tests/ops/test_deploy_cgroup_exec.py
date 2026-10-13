"""`tools/deploy/emax/cgroup-exec.sh`：单元 cgroup 两组布局与限值（M00，D2 WP-16；AWR-04 §4.4.4；M17-FR-063、M17-AC-019）。

用普通目录模拟 cgroupfs（`AWR_CGROUP_SIMULATE=1`，与 `awr.runtime.cgroup` 的测试口径相同），以 Q = 2.5（CPUQuota 250%）
与 MemoryHigh 1.8G、Q = 2.0 与 1.6G 两组单元配额执行包装脚本，检查：子组目录、cgroup.subtree_control、各组限值、自身移入
core/、导出的 AWR_SANDBOX_MEM_MB、最后 exec 了给定命令；再用池的代码（`awr.sandbox.config.PoolSettings`）读同一个模拟单元，
确认 Q、委派判定与 `sandbox/cpu.max` 的公式值和脚本写入的一致。无委派（单元根只读）、单元根不存在与 AWR_CGROUP_DISABLE=1
时脚本跳过布局、给出一行 JSON 告警并照常 exec。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "deploy" / "emax" / "cgroup-exec.sh"
PROBE = [sys.executable, "-c", "import json, os; print(json.dumps({k: v for k, v in os.environ.items() if k.startswith('AWR_')}))"]


def _unit(tmp: Path, cpu_max: str = "250000 100000", mem_high: int = int(1.8 * 2**30)) -> Path:
    u = tmp / "system.slice" / "anet-drone4d.service"
    u.mkdir(parents=True)
    (u / "cpu.max").write_text(cpu_max + "\n")
    (u / "memory.high").write_text(f"{mem_high}\n")
    (u / "memory.max").write_text(f"{2 * 2**30}\n")
    (u / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (u / "cgroup.procs").write_text("")
    (u / "cgroup.subtree_control").write_text("")
    return u


def _run(tmp: Path, unit: Path, **env: str) -> tuple[subprocess.CompletedProcess, dict]:
    e = {k: v for k, v in os.environ.items() if not k.startswith("AWR_")}
    e.update({"AWR_CGROUP_SIMULATE": "1", "AWR_CGROUP_FS": str(tmp), "AWR_CGROUP_ROOT": str(unit), **env})
    p = subprocess.run(["bash", str(SCRIPT), *PROBE], env=e, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return p, json.loads(p.stdout.strip().splitlines()[-1])


def _log(p: subprocess.CompletedProcess) -> dict:
    line = [x for x in p.stderr.splitlines() if x.startswith("{")][-1]
    doc = json.loads(line)
    assert doc["proc"] == "cgroup-exec" and doc["logger"] == "awr.deploy.cgroup"
    return doc


def _r(p: Path) -> str:
    return p.read_text().strip()


def test_script_is_executable_and_valid_bash():
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_layout_q25_memoryhigh_18g(tmp_path):
    u = _unit(tmp_path)
    p, env = _run(tmp_path, u)
    for d in ("core", "sandbox", "sandbox/ctl", "sandbox/plan", "sandbox/sb"):
        assert (u / d).is_dir(), d
    assert _r(u / "cgroup.subtree_control") == "+cpu +memory"
    assert _r(u / "sandbox" / "cgroup.subtree_control") == "+cpu +memory"
    assert _r(u / "sandbox" / "sb" / "cgroup.subtree_control") == "+cpu +memory"
    assert _r(u / "core" / "cpu.weight") == "400"
    assert int(_r(u / "core" / "memory.min")) == 450 * 2**20
    assert _r(u / "sandbox" / "cpu.max") == "136000 100000"            # 0.9·2.5 − 0.89 = 1.36 核
    assert _r(u / "sandbox" / "cpu.weight") == "100"
    assert int(_r(u / "sandbox" / "memory.high")) == 1300 * 2**20
    assert int(_r(u / "sandbox" / "memory.max")) == 1450 * 2**20
    assert _r(u / "sandbox" / "ctl" / "cpu.weight") == "300"
    assert int(_r(u / "sandbox" / "ctl" / "memory.min")) == 150 * 2**20
    assert _r(u / "sandbox" / "plan" / "cpu.max") == "50000 100000"
    assert int(_r(u / "sandbox" / "plan" / "memory.max")) == 250 * 2**20
    assert _r(u / "sandbox" / "sb" / "cpu.max") == "114000 100000"      # 会话父组：1.36 − C_ctl 0.22（D2-FX-R2）
    pids = _r(u / "core" / "cgroup.procs").split()
    assert len(pids) == 1 and pids[0].isdigit()                        # 包装脚本自身（随后 exec 成 supervisor）
    assert env["AWR_SANDBOX_MEM_MB"] == "1100"                         # M_sb = 1300 − 控制面 200（AWR-04 §4.4.2）
    log = _log(p)
    assert log["msg"] == "cgroup.layout" and log["lvl"] == "INFO"
    assert log["kv"]["delegated"] is True and log["kv"]["q_cores"] == 2.5 and log["kv"]["failed_writes"] == []


def test_layout_q20_memoryhigh_16g(tmp_path):
    u = _unit(tmp_path, cpu_max="200000 100000", mem_high=int(1.6 * 2**30))
    _, env = _run(tmp_path, u)
    assert _r(u / "sandbox" / "cpu.max") == "91000 100000"             # 0.9·2.0 − 0.89 = 0.91 核
    assert int(_r(u / "sandbox" / "memory.high")) == 1150 * 2**20       # M17 PRD §8.3
    assert int(_r(u / "sandbox" / "memory.max")) == 1300 * 2**20
    assert _r(u / "sandbox" / "sb" / "cpu.max") == "69000 100000"       # 0.91 − 0.22
    assert env["AWR_SANDBOX_MEM_MB"] == "950"


@pytest.mark.parametrize(("mem_high", "sb_high", "m_sb"), [(1800 * 2**20, 1300, "1100"), (int(1.8 * 2**30), 1300, "1100"),
                                                           (1600 * 2**20, 1150, "950"), (1599 * 2**20, 1099, "899")])
def test_memory_tier_boundaries(tmp_path, mem_high, sb_high, m_sb):
    """MemoryHigh "1800M"（cgroup 模拟 `_sbbench.Stack` 与运维手写）与 systemd 的 1.8G 同属 1.8 GB 档（D2-ACC-1 变体 B 中 1800M 被归入
    1.6G 档，M_sb 950，sanfrancisco + shanghai 即 508；D2-FX-R2）。"""
    u = _unit(tmp_path, mem_high=mem_high)
    _, env = _run(tmp_path, u)
    assert int(_r(u / "sandbox" / "memory.high")) == sb_high * 2**20
    assert env["AWR_SANDBOX_MEM_MB"] == m_sb


def test_env_overrides_and_existing_mem_budget(tmp_path):
    u = _unit(tmp_path, cpu_max="max 100000")
    _, env = _run(tmp_path, u, AWR_SANDBOX_CPU_QUOTA="3.0", AWR_CG_SANDBOX_MEM_HIGH="1000M", AWR_SANDBOX_MEM_MB="777")
    assert _r(u / "sandbox" / "cpu.max") == "181000 100000"            # cpu.max 为 max 时取 AWR_SANDBOX_CPU_QUOTA
    assert int(_r(u / "sandbox" / "memory.high")) == 1000 * 2**20
    assert env["AWR_SANDBOX_MEM_MB"] == "777"                          # 环境文件已设置时不覆盖


def test_pool_reads_the_same_layout(tmp_path):
    """池的 Q、委派判定与 sandbox/cpu.max 公式值与脚本写入的一致（awr.sandbox.config）。"""
    u = _unit(tmp_path)
    _run(tmp_path, u)
    from awr.runtime import cgroup as cg
    from awr.runtime.config import SandboxCfg
    from awr.sandbox.config import PoolSettings, read_unit_quota

    env = {"AWR_CGROUP_FS": str(tmp_path), "AWR_CGROUP_ROOT": str(u)}
    assert read_unit_quota(env) == 2.5
    s = PoolSettings(sb=SandboxCfg(), env=env)
    assert s.is_delegated()
    quota = int(_r(u / "sandbox" / "cpu.max").split()[0])
    assert abs(s.cpu_max_sandbox() - quota / 100_000) < 0.006
    assert cg.leaf_path("sandbox/sb/sb-abcdefghij", env) == u / "sandbox" / "sb" / "sb-abcdefghij"


def test_rerun_is_idempotent(tmp_path):
    u = _unit(tmp_path)
    _run(tmp_path, u)
    p, _ = _run(tmp_path, u)
    assert _log(p)["kv"]["failed_writes"] == []
    assert _r(u / "sandbox" / "cpu.max") == "136000 100000"


@pytest.mark.parametrize("case", ["readonly", "missing", "disabled"])
def test_undelegated_still_execs(tmp_path, case):
    u = _unit(tmp_path)
    extra: dict[str, str] = {}
    if case == "readonly":
        os.chmod(u / "cgroup.subtree_control", 0o444)
        os.chmod(u, 0o555)
    elif case == "missing":
        u = tmp_path / "nonexistent.service"
    else:
        extra["AWR_CGROUP_DISABLE"] = "1"
    try:
        p, env = _run(tmp_path, u, **extra)
    finally:
        if case == "readonly":
            os.chmod(tmp_path / "system.slice" / "anet-drone4d.service", 0o755)
    log = _log(p)
    assert log["msg"] == "cgroup.undelegated" and log["lvl"] == "WARN"
    assert not (tmp_path / "system.slice" / "anet-drone4d.service" / "core").exists()
    assert "AWR_CGROUP_SIMULATE" in env                                # 命令照常执行（exec）
    if case == "readonly" and os.geteuid() == 0:
        pytest.skip("root 忽略只读权限")
