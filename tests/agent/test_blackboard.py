"""M14-AC-019：黑板（HLC 单调、重复去重、conclude 后拒写、retraction、快照全序、签名）；4096 单元快照 ≤ 5 ms 为 perf。"""

from __future__ import annotations

import time

import pytest

from awr.agent.anet_mock import identity as ID
from awr.agent.runtime.blackboard import HLC, Blackboard, BoardError, HlcClock, TaskNotActive, unit_id

SECRET, RUN = b"k", "r"


def _board(t: list[int]) -> Blackboard:
    return Blackboard(now_ms=lambda: t[0], sign=lambda a, u: ID.sign(ID.agent_key(SECRET, RUN, a), u),
                      verify=lambda a, u, s: ID.verify(ID.agent_key(SECRET, RUN, a), u, s))


def test_hlc_monotonic() -> None:
    c = HlcClock()
    a = c.now(100, "n1")
    b = c.now(100, "n1")
    d = c.now(50, "n1")
    e = c.merge(HLC(500, 3, "x"), 200, "n1")
    assert a < b < d < e and e == HLC(500, 4, "n1")


def test_add_dedup_conclude_retract_snapshot() -> None:
    t = [1000]
    bb = _board(t)
    a = ID.aid("w", "a1")
    u1 = bb.add(a, "T-0001", "claim", {"conf": 0.42})
    unit = dict(bb.units[u1])
    assert bb.merge_unit(unit) == u1 and bb.stats["dup"] == 1
    assert u1.startswith("sha256:") and len(u1) == 71 and unit_id(unit) == u1
    t[0] = 900  # 墙钟回退：logical 递增
    u2 = bb.add(a, "T-0001", "intent", {"capability": "thermal.imaging"})
    u3 = bb.add(a, "T-0001", "retraction", {"ref": u2})
    snap = bb.snapshot("T-0001")
    assert [u["id"] for u in snap] == [u1, u2, u3]
    assert [u["type"] for u in bb.effective("T-0001")] == ["claim"]
    bb.conclude("T-0001")
    with pytest.raises(TaskNotActive):
        bb.add(a, "T-0001", "evidence", {})
    bb.archive("T-0001")
    with pytest.raises(BoardError):
        bb.conclude("T-0001")
    with pytest.raises(BoardError):
        bb.archive("T-0002")  # active 直接 archive 非法


def test_bad_signature_rejected() -> None:
    t = [1]
    bb = _board(t)
    u = bb.make(ID.aid("w", "a1"), "T-0003", "claim", {})
    u["sig"] = "00" * 32
    with pytest.raises(BoardError):
        bb.merge_unit(u)


def test_snapshot_total_order_stable() -> None:
    t = [0]
    bb = _board(t)
    for i in range(200):
        t[0] = i // 3
        bb.add(ID.aid("w", f"a{i % 5}"), "T-9", "claim", {"i": i})
    s1 = [u["id"] for u in bb.snapshot("T-9")]
    keys = [(u["stamp"]["wall"], u["stamp"]["logical"], u["stamp"]["node"], u["id"]) for u in bb.snapshot("T-9")]
    assert keys == sorted(keys) and s1 == [u["id"] for u in bb.snapshot("T-9")]


@pytest.mark.perf
def test_snapshot_4096_under_5ms() -> None:
    t = [0]
    bb = Blackboard(now_ms=lambda: t[0])
    for i in range(4096):
        t[0] = i
        bb.add("bafyreia", "T-1", "evidence", {"i": i})
    t0 = time.perf_counter()
    bb.snapshot("T-1")
    assert (time.perf_counter() - t0) * 1000 <= 5.0
