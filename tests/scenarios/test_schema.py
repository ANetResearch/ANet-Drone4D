"""剧本 schema 契约（AWR-18 §8.3 "剧本 schema" 行；D1-AC-15 前置）：S1、ladder、S2–S6、free、soak 与 catalog 全部通过
`packages/contracts/scenario/*.schema.json`（Python jsonschema；Ajv strict 由 M00 的契约测试覆盖同一 schema）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awr.datasets.scenarios.catalog import scenario_files, schema_errors

ROOT = Path(__file__).resolve().parents[2]
FILES = scenario_files(ROOT / "scenarios")


def test_expected_files_present() -> None:
    need = {"s1-shenzhen-facade", "ladder-shenzhen", "s2-shanghai-formation", "s3-newyork-sar", "s4-chicago-lakeshore",
            "s5-sanfrancisco-terrain", "s6-suzhou-corridor"}
    assert need <= set(FILES)


@pytest.mark.parametrize("sid", sorted(FILES))
def test_scenario_schema(sid: str) -> None:
    d = json.loads(FILES[sid].read_text(encoding="utf-8"))
    assert schema_errors(d, "scenario/scenario.schema.json") == []


def test_catalog_schema() -> None:
    d = json.loads((ROOT / "scenarios" / "catalog.json").read_text(encoding="utf-8"))
    assert schema_errors(d, "scenario/catalog.schema.json") == []


def test_contract_fixture_is_consistent_with_s1() -> None:
    """契约夹具（16 §12.4 样例）与内置 S1 的业务参数一致（夹具只有 ci profile）。"""
    fx = json.loads((ROOT / "packages/contracts/fixtures/scenario/s1-shenzhen-facade.json").read_text(encoding="utf-8"))
    s1 = json.loads(FILES["s1-shenzhen-facade"].read_text(encoding="utf-8"))
    for k in ("vehicles", "events", "success", "zones", "transit", "env", "gcs_loss_policy", "time_limit_s"):
        assert fx[k] == s1[k], k
    # 内置 S1 另写了 facade_z_range_m（12 §7.1.3，M10-to-M16 第 2 条），其余任务字段与夹具一致
    strip = [{**m, "params": {k: v for k, v in m["params"].items() if k != "facade_z_range_m"}} for m in s1["missions"]]
    assert fx["missions"] == strip
    assert fx["profiles"]["ci"] == s1["profiles"]["ci"]
