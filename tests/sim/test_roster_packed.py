"""Roster 快照回复的预编码（ADR-074 第 1 条）：`snapshot_packed()` 与 `msgpack.packb(snapshot())` 逐字节相同，且在加入、
生命周期转移、直接改写后 touch、移除、条目对象替换（checkpoint 恢复）与清空之后仍然相同；N = 1000 时命中缓存的回复
不重新编码。"""

from __future__ import annotations

import time

import msgpack

from awr.contracts.enums import Lifecycle
from awr.sim.core.roster import Roster, RosterEntry


def _packed_ref(r: Roster) -> bytes:
    return msgpack.packb(r.snapshot(), use_bin_type=True)


def _add(r: Roster, k: int, t_ns: int = 0) -> RosterEntry:
    return r.add(vehicle_id=f"sim-{k:04d}", model="p600", profile_id="p600_mid360", limits_profile=None,
                 home_enu_m=(float(k), 0.0, 0.0), yaw_rad=0.0, initial_soc=1.0, t_ns=t_ns)


def test_packed_matches_snapshot_through_mutations() -> None:
    r = Roster(64, id_base=100, id_count=64)
    assert r.snapshot_packed() == _packed_ref(r)  # 空 roster
    for k in range(1, 11):
        _add(r, k)
    assert r.snapshot_packed() == _packed_ref(r)
    r.advance(1)  # PENDING -> STARTING（经 _set）
    assert r.snapshot_packed() == _packed_ref(r)
    r.advance(int(0.3e9))
    r.advance(int(1.0e9))
    assert all(e.lifecycle == int(Lifecycle.READY) for e in r.by_slot.values())
    assert r.snapshot_packed() == _packed_ref(r)
    r.set_lifecycle(3, Lifecycle.DRAINING, int(2e9))
    assert r.snapshot_packed() == _packed_ref(r)
    r.by_slot[4].lifecycle = int(Lifecycle.STARTING)  # 直接改写（checkpoint 恢复路径）后 touch
    r.touch()
    assert r.snapshot_packed() == _packed_ref(r)
    r.remove(5, int(3e9))
    assert r.snapshot_packed() == _packed_ref(r)
    _add(r, 99)  # 复用最小空闲 slot
    assert r.snapshot_packed() == _packed_ref(r)
    old = r.by_slot[2]  # 条目对象替换（checkpoint 恢复重建 RosterEntry）
    r.by_slot[2] = RosterEntry(old.slot, old.id, old.agent_no, old.kind, old.model, "p600_other", old.limits_profile,
                               old.home_enu_m, old.yaw_rad, old.initial_soc, backend="sih", lifecycle=old.lifecycle)
    r.by_id[old.id] = r.by_slot[2]
    r.touch()
    assert r.snapshot_packed() == _packed_ref(r)
    old = r.by_slot[3]  # checkpoint 恢复的形态：直接改写 by_slot 并推进 roster_version，不经 Roster 的方法、不调 touch()
    r.by_slot[3] = RosterEntry(old.slot, old.id, old.agent_no, old.kind, old.model, "p600_restored", old.limits_profile,
                               old.home_enu_m, old.yaw_rad, old.initial_soc, lifecycle=int(Lifecycle.READY))
    r.by_id[old.id] = r.by_slot[3]
    r.roster_version += 1
    assert r.snapshot_packed() == _packed_ref(r)
    r.clear()
    assert r.snapshot_packed() == _packed_ref(r)


def test_packed_reply_is_cheap_at_n1000() -> None:
    r = Roster(1024, id_base=0, id_count=1024)
    for k in range(1, 1001):
        _add(r, k)
    r.advance(1)
    first = r.snapshot_packed()
    assert first == _packed_ref(r)
    t0 = time.perf_counter()
    for _ in range(20):
        assert r.snapshot_packed() is first  # 键不变：复用同一份字节
    assert (time.perf_counter() - t0) / 20 < 1e-3
    r.set_lifecycle(7, Lifecycle.DRAINING, 5)  # 一条转移：只重编该条目，其余拼接
    t0 = time.perf_counter()
    b = r.snapshot_packed()
    assert time.perf_counter() - t0 < 5e-3  # 全量重建并编码约 3–6 ms；拼接约 0.2 ms（宽松上界，防 CI 抖动）
    assert b == _packed_ref(r)
