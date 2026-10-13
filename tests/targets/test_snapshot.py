"""会话快照（M18-AC-018；M18-FR-021；ADR-112；契约 sandbox/snapshot.schema.json）：经真实 sim-core 导出 `targets` 段（create_request
形状，配置的放置位置，不含运行状态）后在同一世界的新会话装配：配置逐字段一致；装配问题按条目返回（码与 detail）。"""

from __future__ import annotations

from ctlib import errors
from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R

ITEMS = [
    {"class": "person", "pos": [12.0, -3.0], "label": "门卫", "alertness": 0.6, "criterion_mode": "physical",
     "sensitivity": [{"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 150.0}]},
    {"class": "dog", "pos": [-40.0, 25.0], "motion": {"mode": "random_walk", "region": [[-60, 0], [-20, 0], [-20, 50], [-60, 50]]},
     "sound": {"waveform": "pulse_train", "f0_hz": 650, "harmonics_db": [0, -3], "lw_dba": 98, "period_s": 5, "duty": 0.2}},
    {"class": "vehicle", "pos": [0.0, 60.0], "motion": {"mode": "path", "path": [[0, 60, None], [120, 60, None]], "loop": "pingpong",
                                                         "speed_mps": 8}},
]


def _core(reg):
    from awr.sim.targets import plugin

    plugin.install()
    return CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": "sandbox100"})


def test_export_import_roundtrip() -> None:
    with R.isolated_registry() as reg:
        h = _core(reg)
        try:
            probs = h.core.session.apply({"world_id": h.core.cfg.world_id, "targets": ITEMS})
            assert probs == []
            h.advance(3.0)
            snap = h.core.session.export()
            assert errors("sandbox/snapshot.schema.json", snap) == [], errors("sandbox/snapshot.schema.json", snap)[:3]
            exported = snap["targets"]
            assert [t["class"] for t in exported] == ["person", "dog", "vehicle"]
            assert exported[0]["label"] == "门卫" and exported[0]["pos"] == [12.0, -3.0]   # 配置的放置位置
        finally:
            h.close()
    with R.isolated_registry() as reg:
        h2 = _core(reg)
        try:
            assert h2.core.session.apply({"world_id": h2.core.cfg.world_id, "targets": exported}) == []
            again = h2.core.session.export()["targets"]
            assert again == exported
        finally:
            h2.close()


def test_import_problems_reported() -> None:
    with R.isolated_registry() as reg:
        h = _core(reg)
        try:
            probs = h.core.session.apply({"world_id": h.core.cfg.world_id, "targets": [
                {"class": "person", "pos": [0, 0]}, {"class": "person", "pos": [0, 0], "alertness": 3}, {"class": "ghost", "pos": [0, 0]}]})
            assert [(p["idx"], p["code"]) for p in probs] == [(1, 110), (2, 110)]
            assert len(h.core.entities["target"]) == 1
        finally:
            h.close()
