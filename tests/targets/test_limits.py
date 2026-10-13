"""上限与实体层（M18-AC-001；M18-FR-001、FR-019、FR-027；ADR-098）：经真实 sim-core（CoreHarness，sandbox100 与 d1_250 两档）：

- 实体块列与冻结字段表（`tools/fake/targets_stub.M18_FIELDS`）逐项一致；stage `targets` 在两档时钟均为 10 Hz、order 170；
- 30 个识别物全部接受，第 31 个经准入返回 505；roster 出现 `kind = target` 条目（agent_no 在识别物内从 0 计，含 model 与 label）；
- 识别物不进入机群、租约与 FleetGuard（`fleet/vehicles` 与机体 roster 不变）；删除后 roster 移除且 target_no 不复用。
"""

from __future__ import annotations

import pytest
import targets_stub as TS
from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R
from awr.sim.targets.block import FIELDS


def test_block_fields_match_frozen_table() -> None:
    assert {k: (str(v[0]), tuple(v[1])) for k, v in FIELDS.items()} == {k: (str(v[0]), tuple(v[1])) for k, v in TS.M18_FIELDS.items()}


@pytest.mark.parametrize("profile", ["sandbox100", "d1_250"])
def test_limit_roster_and_isolation(profile: str) -> None:
    with R.isolated_registry() as reg:
        from awr.sim.targets import plugin

        plugin.install()
        h = CoreHarness(n=2, reg=reg, fleet_cfg={"clock_profile": profile})
        try:
            spec = h.core.fleet.pipeline.spec("targets")
            assert spec is not None and spec.order == 170 and spec.owner == "M18"
            assert h.core.profile.tick_hz // spec.every == 10
            n_uav = len(h.core.roster.by_slot)
            ids = []
            for k in range(30):
                adm = h.cmd("target/add", {"target": {"class": ("person", "dog", "vehicle", "object", "machine", "uav")[k % 6],
                                                      "pos": [10.0 * k - 150.0, 40.0]}}, uav=None)
                assert adm["status"] == "accepted", adm
                ids.append(adm["detail"]["result"]["id"])
            adm = h.cmd("target/add", {"target": {"class": "person", "pos": [0.0, 0.0]}}, uav=None)
            assert adm["status"] == "rejected" and adm["code"] == 505
            snap = h.core.roster.snapshot()
            tg = [e for e in snap["entries"] if e["kind"] == "target"]
            assert [e["agent_no"] for e in tg] == list(range(30))
            assert tg[0]["id"] == "person-01" and tg[0]["model"] == "person" and tg[0]["label"] == "人-01"
            assert len(h.core.roster.by_slot) == n_uav and len(h.core.vehicle_items(None)) == n_uav
            h.advance(1.0)
            assert h.cmd("target/remove", {"id": ids[0]}, uav=None)["status"] == "accepted"
            rej = h.cmd("target/remove", {"id": ids[0]}, uav=None)
            assert rej["status"] == "rejected" and rej["code"] == 543
            adm = h.cmd("target/add", {"target": {"class": "person", "pos": [0.0, 0.0]}}, uav=None)
            assert adm["status"] == "accepted"
            assert h.core.entities["target"].by_id[adm["detail"]["result"]["id"]].target_no == 30
            assert "targets" in h.core.features()
        finally:
            h.close()


def test_viewer_cannot_write() -> None:
    with R.isolated_registry() as reg:
        from awr.sim.targets import plugin

        plugin.install()
        h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": "sandbox100"})
        try:
            adm = h.cmd("target/add", {"target": {"class": "person", "pos": [0.0, 0.0]}}, uav=None, role="viewer")
            assert adm["status"] == "rejected"
            assert len(h.core.entities["target"]) == 0
        finally:
            h.close()
