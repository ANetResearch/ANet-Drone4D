"""M14-AC-001 至 004 的 M14 侧起草（目录归 M00：tests/contracts；本文件在合入前于 tests/agent 运行，见请求 M14-to-M00）。"""

from __future__ import annotations

import json

import pytest
from fakes.schemas import CONTRACTS, errors, load

from awr.agent.runtime.core import EVENT_LEVELS
from awr.agent.runtime.types import Effect, EffectStatus

AGENT_SCHEMAS = ["agent/predicate.schema.json", "agent/effect.schema.json", "agent/capability.schema.json",
                 "agent/capability_catalog.schema.json", "agent/manifest.schema.json", "agent/task.schema.json",
                 "agent/agent_status.schema.json", "agent/agent_tasks.schema.json"]


@pytest.mark.parametrize("rel", AGENT_SCHEMAS)
def test_schema_compiles(rel: str) -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(load(rel))


def test_catalog_24_valid() -> None:
    doc = load("agent/capability_catalog.json")
    assert errors("agent/capability_catalog.schema.json", doc, pending=False) == []
    assert len(doc["capabilities"]) == 24
    for e in doc["capabilities"]:
        assert errors("agent/capability.schema.json", e, pending=False) == [], e["id"]


def test_reasons_470_486() -> None:
    from awr.contracts.reasons import info

    for c in range(470, 487):
        assert info(c) is not None
    assert info(486).name == "RECEIPT_INVALID" and info(470).name == "AGENT_GUARD_INTERNAL"


def test_rng_stream_5_and_mcap_and_bus_keys() -> None:
    from awr.contracts import bus_keys

    rng = load("rt/rng_streams.json")
    s5 = next(s for s in rng["streams"] if s["stream_id"] == 5)
    assert s5["name"] == "anet_mock_latency" and "keyed" in rng["doc"]
    ch = json.dumps(load("rec/mcap_channels.json"))
    assert "/agent/**" in ch and "awr.agent.v1" in ch
    assert bus_keys.ctl_agent("task") == "ctl/agent-runtime/task" and bus_keys.state_agent("tasks") == "state/agent-runtime/tasks"
    cmds = load("rt/commands.json")
    assert any(o["op"] == "scenario/metric" for o in cmds["bus_ops"])


def test_agent_event_kinds_match_pattern() -> None:
    """agent.* 事件 kind 满足 event.schema.json 的 kind 语法；known_kinds 的登记见请求（M14-to-M00 第 1 条）。"""
    import re

    ev = load("bus/event.schema.json")
    pat = re.compile(ev["properties"]["kind"]["pattern"])
    kinds = [*EVENT_LEVELS, "agent.health", "agent.runtime.state", "agent.task.escalated", "agent.board.incidental"]
    assert all(pat.fullmatch(k) for k in kinds)


def test_effect_examples_from_17() -> None:
    """17 §7.8 的命令回执效果（扁平结构）与协作任务效果共用同一 schema（M14-AC-002）。"""
    samples = [{"status": "OK", "verify_trust": 4, "auth_trust": 1, "simulated": True, "native_ack": True, "protocol": "inproc",
                "requested": "goto", "observed_state": "FLYING/POSCTL", "latency_ms": 12, "quirk": "", "message": "",
                "metrics": {"dist_err_m": 0.4, "t_exec_s": 18.2}},
               {"status": "UNVERIFIED", "verify_trust": 1, "native_ack": True},
               Effect(EffectStatus.FAILED, verify_trust=2, message="code=204").to_dict()]
    for s in samples:
        assert errors("agent/effect.schema.json", s, pending=False) == []
    assert (CONTRACTS / "agent" / "effect.schema.json").exists()
