"""会话快照（M18-AC-018；M18-FR-021；ADR-112；契约 sandbox/snapshot.schema.json）：经真实 sim-core 导出 `targets` 段（create_request
形状，配置的放置位置，不含运行状态）后在同一世界的新会话装配：配置逐字段一致；装配问题按条目返回（码与 detail）。"""

from __future__ import annotations

from ctlib import errors
from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R
from awr.sim.targets.model import appearance_catalog

ITEMS = [
    {
        "class": "person",
        "pos": [12.0, -3.0],
        "label": "门卫",
        "alertness": 0.6,
        "criterion_mode": "physical",
        "appearance": {"asset_id": "human-1", "geometry_sha256": appearance_catalog()["human-1"]["geometry_sha256"]},
        "sensitivity": [{"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 150.0}],
    },
    {
        "class": "dog",
        "pos": [-40.0, 25.0],
        "motion": {"mode": "random_walk", "region": [[-60, 0], [-20, 0], [-20, 50], [-60, 50]]},
        "sound": {
            "waveform": "pulse_train",
            "f0_hz": 650,
            "harmonics_db": [0, -3],
            "lw_dba": 98,
            "period_s": 5,
            "duty": 0.2,
        },
    },
    {
        "class": "vehicle",
        "pos": [0.0, 60.0],
        "motion": {"mode": "path", "path": [[0, 60, None], [120, 60, None]], "loop": "pingpong", "speed_mps": 8},
    },
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
            assert exported[0]["label"] == "门卫" and exported[0]["pos"] == [12.0, -3.0]  # 配置的放置位置
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
            probs = h.core.session.apply(
                {
                    "world_id": h.core.cfg.world_id,
                    "targets": [
                        {"class": "person", "pos": [0, 0]},
                        {"class": "person", "pos": [0, 0], "alertness": 3},
                        {"class": "ghost", "pos": [0, 0]},
                    ],
                }
            )
            assert [(p["idx"], p["code"]) for p in probs] == [(1, 110), (2, 110)]
            assert len(h.core.entities["target"]) == 1
        finally:
            h.close()


def test_target_detail_resumes_immediately_after_template_clock_reset(monkeypatch) -> None:
    """慢任务仿真时钟重置（m23 修复）只在 AWR_TARGET_LIFE_ENABLE=1 时启用（D2 缺省行为不变，见 SimCore.reset）。"""
    import msgpack

    from awr.sim.targets import plugin

    monkeypatch.setenv("AWR_TARGET_LIFE_ENABLE", "1")
    with R.isolated_registry() as reg:
        h = _core(reg)
        try:
            rt = plugin.runtime()
            received = []
            rt.sink = lambda raw: received.append(msgpack.unpackb(raw, raw=False))
            h.core.session.apply({"world_id": h.core.cfg.world_id, "targets": ITEMS[:1]})
            h.advance(5)
            assert received and received[-1]["t_sim_ns"] > 1_000_000_000
            received.clear()
            h.core.reset(args={"sandbox_config": {"world_id": h.core.cfg.world_id, "targets": ITEMS[:1]}})
            h.advance(1)
            assert received, "target detail must not wait for the old 5-second timestamp"
            assert received[-1]["t_sim_ns"] <= 1_000_000_000
            assert received[-1]["items"]
        finally:
            h.close()
