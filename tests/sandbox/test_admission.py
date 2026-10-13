"""N_max 公式与世界加权准入（M17-FR-015、FR-019、AC-003 的公式部分、AC-010；AWR-04 §4.4.1、§4.4.2 的组合示例表）。"""

from __future__ import annotations

from pathlib import Path

from sbharness import COSTS, make_sb

from awr.sandbox.admission import memory_mb, world_admit
from awr.sandbox.config import PoolSettings, limits_obj


def test_n_max_formula() -> None:
    sb = make_sb(max_sessions="auto")
    assert sb.n_max(2.5) == 4 and sb.n_max(2.0) == 2
    assert sb.n_max(2.5, delegated=False) == 3 and sb.n_max(2.0, delegated=False) == 1
    assert sb.n_max(10.0) == 6        # 硬上限
    s = PoolSettings(sb=sb, state_dir=Path("/x"), q_cores=2.5, delegated=True)
    assert s.n_max() == 4 and round(s.cpu_max_sandbox(), 2) == 1.36
    s.max_sessions_override = 1
    assert s.n_max() == 1
    assert PoolSettings(sb=sb, q_cores=2.0, delegated=True).per_prefix_cap(2) == 1
    assert PoolSettings(sb=sb, q_cores=2.5, delegated=True).per_prefix_cap(4) == 2


def test_world_weighted_examples() -> None:
    m = 1100.0
    assert world_admit(COSTS, ["synthcity"] * 3, "synthcity", n_max=4, m_sb=m)                     # 4 × synthcity 585
    assert world_admit(COSTS, ["sanfrancisco"] * 3, "sanfrancisco", n_max=4, m_sb=m)               # 4 × SF 970
    assert world_admit(COSTS, ["synthcity", "synthcity", "newyork"], "newyork", n_max=4, m_sb=m)   # 650
    three = ["sanfrancisco", "chicago", "synthcity"]
    assert round(memory_mb(COSTS, three)) == 1045
    assert not any(world_admit(COSTS, three, w, n_max=4, m_sb=m) for w in COSTS)                   # 第 4 个任何世界都超额
    assert world_admit(COSTS, ["sanfrancisco"], "shanghai", n_max=4, m_sb=m)                       # 970
    assert not any(world_admit(COSTS, ["sanfrancisco", "shanghai"], w, n_max=4, m_sb=m) for w in ("chicago", "shanghai"))
    assert not world_admit(COSTS, ["synthcity"] * 4, "synthcity", n_max=4, m_sb=m)                 # 槽位满


def test_switch_counts_the_difference() -> None:
    m = 1100.0
    live = ["sanfrancisco", "chicago", "synthcity"]
    assert not world_admit(COSTS, live, "shanghai", n_max=3, m_sb=m, replace="synthcity")
    assert world_admit(COSTS, live, "shenzhen", n_max=3, m_sb=m, replace="synthcity")
    assert world_admit(COSTS, ["synthcity", "synthcity"], "suzhou", n_max=2, m_sb=m, replace="synthcity")


def test_limits_object_matches_contract() -> None:
    import json

    from jsonschema import Draft202012Validator

    lim = limits_obj(make_sb())
    schema = json.loads((Path(__file__).resolve().parents[2] / "packages/contracts/sandbox/limits.schema.json").read_text())
    assert list(Draft202012Validator(schema).iter_errors(lim)) == []
    assert lim["vehicles_max"] == 12 and lim["targets_max"] == 30 and lim["setpoint_hz_max"] == 50


def test_world_costs_file_valid_and_loaded() -> None:
    import json

    import rtc

    from awr.sandbox.worlds import load_costs

    p = Path(__file__).resolve().parents[2] / "configs" / "sandbox" / "world_costs.json"
    doc = json.loads(p.read_text())
    assert rtc.schema_errors("sandbox/world_costs.schema.json", doc) == []
    costs = load_costs(p)
    assert set(costs) == {"synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco"}
    assert costs["sanfrancisco"].world_class == "L" and costs["synthcity"].world_class == "S"
    # D2-AC-28：成本表由 harness（tools/bench/sandbox/world_costs.py）生成并入库（D2-FX-R2 入库 D2-ACC-1 的实测运行）
    assert all(w.get("measured") is True and "world_costs.py" in str(w.get("src")) for w in doc["worlds"])
    assert "world_costs.py" in doc["generator"] and all(c.anon_mb > 0 and c.ws_mb >= 0 for c in costs.values())
    assert load_costs(p.with_name("missing.json")) == {}
