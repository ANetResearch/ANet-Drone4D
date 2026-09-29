"""M13-AC-020：Mock 目标表（≤ 64、重复 id 拒绝、checkpoint 与 restore 字节一致）；M13-FR-040。"""

from __future__ import annotations

import pytest

from awr.sim.sensors.detector import MAX_TARGETS, TargetError, TargetTable


def test_full_and_dup():
    t = TargetTable()
    for i in range(MAX_TARGETS):
        assert t.spawn(f"t{i}", [i, 0, 0], "person") == i
    with pytest.raises(TargetError) as e:
        t.spawn("t64", [0, 0, 0])
    assert e.value.code == 110 and e.value.detail == "TARGET_TABLE_FULL"
    t2 = TargetTable()
    t2.spawn("x", [0, 0, 0])
    with pytest.raises(TargetError) as e:
        t2.spawn("x", [1, 1, 1])
    assert e.value.detail == "TARGET_ID_DUP"
    with pytest.raises(TargetError):
        t2.spawn("y" * 17, [0, 0, 0])
    with pytest.raises(TargetError):
        t2.spawn("z", [0, 0, 0], "tank")


def test_checkpoint_restore_bytes():
    t = TargetTable()
    t.spawn("t1", [1.5, 2.5, 0.0], "vessel", conf_first=0.42)
    t.spawn("t2", [3, 4, 5], "person", conf_confirm=0.9)
    t.rows["state"][1] = 2
    b = t.checkpoint()
    u = TargetTable()
    u.restore(b)
    assert u.checkpoint() == b and u.n == 2 and u.items()[0]["target_id"] == "t1" and u.items()[1]["state"] == "confirmed"


def test_table_lives_in_checkpointed_state_block(bench):
    b = bench
    b.spawn(0, 1)
    b.run(0.02)
    b.rt.spawn_target("t1", [1, 2, 3], "person", conf_first=0.42)
    b.rt.spawn_target("t2", [4, 5, 6], "vehicle")
    snap = b.S.checkpoint_arrays()
    assert "blk.sensor_targets.tg_target_id" in snap and "blk.sensors.gn_z" in snap
    before = b.rt.detector.targets.items()
    b.rt.detector.targets.clear()
    assert b.rt.detector.targets.n == 0
    b.S.restore_arrays(snap)
    assert b.rt.detector.targets.items() == before
