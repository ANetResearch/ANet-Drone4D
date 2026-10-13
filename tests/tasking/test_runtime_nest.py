"""M20-AC-001（运行时部分）：机巢实体、库存生成机队实例、上限与兼容（505、525）、只读库存、删除冲突、起降位间隔与换电、状态载荷契约。"""

from __future__ import annotations

import numpy as np
import pytest
from ctlib import errors
from m20core import Core


@pytest.fixture
def core():
    c = Core()
    yield c
    c.close()


def test_nest_add_inventory_and_home_slots(core: Core) -> None:
    nid, vids = core.nest(pos=(10.0, 20.0), n=3)
    assert nid.startswith("n-") and len(vids) == 3
    rt = core.rt
    assert all(rt.nest_of(v) == nid for v in vids)
    homes = np.array([rt.pos(v)[:2] for v in vids])
    d = np.hypot(*(homes[:, None, :] - homes[None, :, :]).transpose(2, 0, 1))
    assert d[np.triu_indices(3, 1)].min() >= 4.9           # 槽位网格间距 5 m（M08 出生点间距下限 4.24 m）
    assert np.hypot(*(homes.mean(axis=0) - np.array([10.0, 20.0]))) < 6.0
    q = core.query("tasking/nests")
    assert q["code"] == 0 and q["items"][0]["inventory"] == [{"model_id": "p600_mid360", "count": 3, "in_nest": 3, "airborne": 0}]
    item = {k: v for k, v in q["items"][0].items() if k != "status"}
    assert errors("tasking/nest.schema.json", item) == []
    st = q["items"][0]["status"]
    assert errors("rt/payloads/nest_status.schema.json", st) == []
    assert st["slots"] == {"total": 6, "occupied": 3}
    assert len(st["chargers"]) == 4                         # 缺省 4 × C1-XR


def test_limits_and_compatibility(core: Core) -> None:
    for k in range(4):
        core.ok("nest/add", {"nest": {"pos": [100.0 * k, 0.0, None]}})
    r = core.cmd("nest/add", {"nest": {"pos": [900.0, 0.0, None]}})
    assert r["status"] == "rejected" and r["code"] == 505
    core.ok("nest/remove", {"id": "n-4"})
    r = core.cmd("nest/add", {"nest": {"pos": [900.0, 0.0, None], "kind": "launcher", "inventory": [{"model_id": "p600_mid360", "count": 1}]}})
    assert r["status"] == "rejected" and r["code"] == 525 and r["detail"]["why"] == "kind"
    r = core.cmd("nest/add", {"nest": {"pos": [900.0, 0.0, None], "slots": 2, "inventory": [{"model_id": "p600_mid360", "count": 3}]}})
    assert r["status"] == "rejected" and r["code"] == 525 and r["detail"]["why"] == "slots"
    r = core.cmd("nest/add", {"nest": {"pos": [900.0, 0.0, None], "kind": "vtol_pad", "inventory": [{"model_id": "awr_v1", "count": 1}],
                                       "chargers": [{"model": "c1_xr", "count": 2, "channels": 1, "i_max_a": 10, "cells_max": 6}]}})
    assert r["status"] == "rejected" and r["code"] == 525 and r["detail"]["why"] == "cells"   # 12S 电池不进 6S 充电器
    r = core.cmd("nest/add", {"nest": {"pos": [900.0, 0.0, None], "swap_s": 5}})
    assert r["status"] == "rejected" and r["code"] == 110


def test_update_inventory_readonly_and_remove_conflict(core: Core) -> None:
    nid, _vids = core.nest(n=1)
    r = core.cmd("nest/update", {"id": nid, "patch": {"inventory": [{"model_id": "p600_mid360", "count": 2}]}})
    assert r["status"] == "rejected" and r["code"] == 110 and r["detail"]["why"] == "INVENTORY_READONLY"
    res = core.ok("nest/update", {"id": nid, "patch": {"name": "北巢\u0007", "launch_interval_s": 45}})
    assert res["nest"]["name"] == "北巢" and res["nest"]["launch_interval_s"] == 45.0
    r = core.cmd("nest/remove", {"id": nid})
    assert r["status"] == "rejected" and r["code"] == 105


def test_turnaround_swap_and_charging_queue(core: Core) -> None:
    """换电：检查 → 卸下电池入队充电 → 装入 SOC 最高的 READY 备用电池；起降位相邻事件 ≥ 起降间隔。"""
    nid, vids = core.nest(n=1, energy={"mode": "swap", "swap_s": 30, "packs": [{"cells": 6, "count": 1}]}, turnaround_s=10,
                          launch_interval_s=20)
    rt = core.rt
    n = rt.nests[nid]
    uav = vids[0]
    rt.set_soc(uav, 0.4)
    n.recover(uav, core.t, 0.4)
    core.advance(12.0)
    assert n.air[uav].state in ("swapping", "ready")
    old = f"{uav}-p0"
    assert n.packs[old].state in ("queued", "charging")
    core.until(lambda: n.air[uav].state == "ready", 60.0)
    assert rt.soc(uav) == pytest.approx(0.95, abs=1e-6)
    assert any(e[0] == "nest.swap_done" for e in rt.event_log)
    core.advance(5.0)
    assert n.packs[old].state == "charging" and n.packs[old].t_ready_s is not None
    # 0.4 → 0.95 的 10 Ah 包按 AWR-20 §4.5（含 300 s 降温）
    from awr.tasking.nestmodel import charge_time_s

    assert n.packs[old].t_ready_s - n.packs[old].t_start_s == pytest.approx(charge_time_s(10.0, 1.0, 10.0, 0.4, 0.95), rel=1e-6)
    t0 = n.pad.book(core.t, "launch", "a")
    t1 = n.pad.book(core.t, "launch", "b")
    assert t1 - t0 >= 20.0 - 1e-9


def test_rtk_cover_metric(core: Core) -> None:
    """M13-FR-211（P1，d2-WP-10-to-M20 第 3 条）：度量 `tasking.rtk_cover(x_m, y_m)` 在带 rtk_base 的机巢链路距离内为 1，否则 0。"""
    from awr.sim.core.metrics import metric

    with pytest.raises(LookupError):                     # 没有机巢：度量不可用（M13 保持机型 GNSS 配置）
        metric("tasking.rtk_cover", x_m=0.0, y_m=0.0)
    core.ok("nest/add", {"nest": {"pos": [5000.0, 0.0, None], "rtk_base": False}})
    assert metric("tasking.rtk_cover", x_m=5000.0, y_m=0.0) == 0.0
    core.ok("nest/add", {"nest": {"pos": [0.0, 0.0, None], "rtk_base": True, "link_range_m": 2000.0}})
    assert metric("tasking.rtk_cover", x_m=1500.0, y_m=0.0) == 1.0
    assert metric("tasking.rtk_cover", x_m=2500.0, y_m=0.0) == 0.0
