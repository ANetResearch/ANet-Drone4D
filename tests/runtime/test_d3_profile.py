"""D3 扩展点 12（AWR-05 §12.3）：`d3` profile 启用 D3 进程并置 `AWR_D3_ENABLED = 1`；公开与缺省 profile 的进程集合与环境不变。"""

from __future__ import annotations

import pytest

from awr.runtime.config import load_runtime_config

D3_PROCS = {"bench-api", "bench-scheduler", "geosvc", "lab-gateway", "rerun-proxy"}


ENV = {"AWR_ORIGINS": "https://drone4d.example.org"}     # public 模式要求 origins


def _enabled(profile: str) -> set[str]:
    return {p.name for p in load_runtime_config(profile=profile, env=ENV).enabled_procs()}


def test_d3_profile_enables_d3_processes_and_the_flag() -> None:
    c = load_runtime_config(profile="d3", env={})
    names = {p.name for p in c.enabled_procs()}
    assert names >= D3_PROCS and {"sim-core", "api"} <= names
    assert c.defaults.env["AWR_D3_ENABLED"] == "1" and c.defaults.env["OMP_NUM_THREADS"] == "1"
    assert {p.name for p in c.procs if p.layer == "d3"} == D3_PROCS


@pytest.mark.parametrize("profile", ["dev", "demo", "ci", "perf", "public", "sandbox"])
def test_other_profiles_have_no_d3_process_or_flag(profile: str) -> None:
    c = load_runtime_config(profile=profile, env=ENV)
    assert not _enabled(profile) & D3_PROCS
    assert "AWR_D3_ENABLED" not in c.defaults.env
