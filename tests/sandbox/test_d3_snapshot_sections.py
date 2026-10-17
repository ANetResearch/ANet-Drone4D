"""D3 扩展点 9（AWR-05 §12.3；M23 §7.5、M26 §7.12）：快照可选段 `event`、`agents` 通过 schema；D2 快照逐字节不变地通过；
D2 会话装配忽略两个段（没有登记对应的会话段加载器）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from awr.sandbox.snapshot import check_snapshot

ROOT = Path(__file__).resolve().parents[2]
D2 = json.loads((ROOT / "packages/contracts/fixtures/d2/rest/snapshot.json").read_text(encoding="utf-8"))
LIMITS = {"vehicles_max": 12, "targets_max": 30, "nests_max": 4, "tasks_max": 8}
EVENT = {"event_id": "crop-handoff-001", "version": "1", "tape_uri": "events/crop-handoff-001/tape.awtp",
         "tape_content_sha256": "b" * 64, "mode": "exogenous"}
AGENTS = [{"uav_id": "p600-01", "policy": {"kind": "reference", "ref": "baseline/frontier"}, "obs_level": "L2"}]


def _check(s: dict):
    return check_snapshot(s, world=s.get("world_id"), limits=LIMITS, max_kib=512.0)


def test_d2_snapshot_still_valid() -> None:
    assert _check(copy.deepcopy(D2)) is None


def test_event_and_agents_sections_validate() -> None:
    s = {**copy.deepcopy(D2), "event": dict(EVENT, truth_rev=2), "agents": AGENTS}
    assert _check(s) is None


def test_sections_reject_unknown_values() -> None:
    for patch in ({"event": {**EVENT, "mode": "replay"}}, {"event": {k: v for k, v in EVENT.items() if k != "tape_uri"}},
                  {"agents": [{**AGENTS[0], "obs_level": "L0"}]}, {"agents": [{**AGENTS[0], "policy": {"kind": "inproc", "ref": "x"}}]},
                  {"event": {**EVENT, "extra": 1}}):
        why = _check({**copy.deepcopy(D2), **patch})
        assert why is not None and why[0] == "schema", patch


def test_d2_session_assembly_ignores_the_sections() -> None:
    from awr.sim.fleet.stages import registry as R
    from awr.sim.runtime.session_cfg import SECTION_ORDER

    with R.isolated_registry() as reg:
        assert "event" not in SECTION_ORDER and "agents" not in SECTION_ORDER
        assert "event" not in reg.session_loaders and "agents" not in reg.session_loaders
