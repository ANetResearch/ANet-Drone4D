"""cgroup 叶子组与无委派退化（M17-FR-026、AC-019）：以临时目录模拟 cgroup v2（`AWR_CGROUP_SIMULATE=1`）建删叶子组并写入限值、
读沙盒组统计；无委派时不写任何文件、N_max 减 1、RSS 看门狗在匿名页超过阈值时终止会话。"""

from __future__ import annotations

from pathlib import Path

import pytest
from brokerkit import create, make_broker, ready
from sbharness import make_sb

from awr.sandbox.cgroups import CgroupManager


@pytest.fixture
def fakecg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "cg" / "unit"
    (root / "sandbox" / "sb").mkdir(parents=True)
    monkeypatch.setenv("AWR_CGROUP_FS", str(tmp_path / "cg"))
    monkeypatch.setenv("AWR_CGROUP_ROOT", str(root))
    monkeypatch.setenv("AWR_CGROUP_SIMULATE", "1")
    return root


def test_leaf_limits_and_removal(fakecg: Path) -> None:
    cg = CgroupManager(make_sb())
    assert cg.delegated
    p = cg.create("sb-aaaaaaaaaa", "L")
    assert p == fakecg / "sandbox" / "sb" / "sb-aaaaaaaaaa"
    assert (p / "memory.high").read_text() == "250M" and (p / "memory.max").read_text() == "320M"
    assert (p / "cpu.weight").read_text() == "100"
    assert cg.create("sb-bbbbbbbbbb", "S") is not None
    assert (fakecg / "sandbox" / "sb" / "sb-bbbbbbbbbb" / "memory.high").read_text() == "230M"
    assert cg.remove("sb-aaaaaaaaaa") and not p.exists()
    assert cg.ensure_plan_leaf() == "sandbox/plan"
    assert (fakecg / "sandbox" / "plan" / "cpu.max").read_text() == "50000 100000"
    assert (fakecg / "sandbox" / "plan" / "memory.max").read_text() == "250M"


def test_spawn_weight_and_sessions_parent(fakecg: Path) -> None:
    """D2-FX-R2：拉起期间叶子组加权（D2-AC-02 冷启动不与满载会话同权），就绪后回到 100；会话父组 cpu.max = sandbox − C_ctl。"""
    cg = CgroupManager(make_sb())
    p = cg.create("sb-cccccccccc", "S", spawning=True)
    assert p is not None and (p / "cpu.weight").read_text() == "1000"
    assert cg.settle("sb-cccccccccc") and (p / "cpu.weight").read_text() == "100"
    assert cg.ensure_sessions_parent(1.14)
    assert (fakecg / "sandbox" / "sb" / "cpu.max").read_text() == "114000 100000"
    assert cg.ensure_sessions_parent(0.05) and (fakecg / "sandbox" / "sb" / "cpu.max").read_text() == "20000 100000"


def test_group_stats(fakecg: Path) -> None:
    (fakecg / "sandbox" / "cpu.stat").write_text("usage_usec 10\nnr_periods 100\nnr_throttled 9\n")
    (fakecg / "sandbox" / "memory.stat").write_text(f"anon {200 << 20}\nshmem {10 << 20}\nfile 5\n")
    (fakecg / "sandbox" / "cpu.pressure").write_text("some avg10=12.50 avg60=1.00 avg300=0.00 total=1\n")
    (fakecg / "sandbox" / "memory.pressure").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    g = CgroupManager(make_sb()).group_stats()
    assert g["cpu_stat"]["nr_throttled"] == 9 and round(g["mem_mb"]) == 210 and g["psi_cpu"] == 12.5 and g["psi_mem"] == 0.0
    assert g["cpu_stat_sb"] is None
    (fakecg / "sandbox" / "sb" / "cpu.stat").write_text("usage_usec 5\nnr_periods 100\nnr_throttled 40\n")
    g = CgroupManager(make_sb()).group_stats()
    assert g["cpu_stat_sb"]["nr_throttled"] == 40


def test_undelegated_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWR_CGROUP_FS", str(tmp_path / "none"))
    monkeypatch.setenv("AWR_CGROUP_ROOT", str(tmp_path / "none" / "unit"))
    cg = CgroupManager(make_sb())
    assert not cg.delegated and cg.create("sb-aaaaaaaaaa", "S") is None and cg.remove("sb-aaaaaaaaaa")
    assert cg.group_stats() is None and cg.ensure_plan_leaf() is None
    sb = make_sb(max_sessions="auto")
    assert sb.n_max(2.5, delegated=False) == sb.n_max(2.5) - 1
    b, fx, _c = make_broker(delegated=False)
    sid = create(b, 1)["result"]["session"]["sid"]
    ready(b, sid)
    b.sessions[sid].anon_mb = 300.0
    b.tick()
    assert fx.killed == []
    b.sessions[sid].anon_mb = 330.0
    b.tick()
    assert fx.killed == [(sid, "rss_watchdog")]
