"""录制策略与确定性（M12 §6.6.2；FR-032、FR-033；NFR-019；M12-AC-032、AC-033 的策略部分）：

- 桶规则：×1 块落在 40 ms 网格（25 Hz）、Full64 8 ms（125 Hz）；×10 块 200 ms、Full64 80 ms；块帧强制附带 Full64；
- 标记机：N ≤ 50 全部；N > 50 时剧本标记优先、其余按最近选中，总数 ≤ 16；
- KeyDelta：段首关键块、之后只写变化机体、每 5 s 关键块，合并结果等于最新值；
- 同一参数两次合成录制的消息序列（topic、log_time、data）逐字节相同（chunk 边界除外）。
"""

from __future__ import annotations

import itertools
from pathlib import Path

import msgpack
import pytest
import rechelp

from awr.recorder.config import PolicyCfg
from awr.recorder.keydelta import KeyDeltaBlocker, merge_blocks, parse_batch
from awr.recorder.policy import MarkedSet, RecordingPolicy
from awr.recorder.selector import FrameSelector

pytestmark = pytest.mark.ext


def test_buckets_x1_and_x10() -> None:
    pol = RecordingPolicy.from_cfg(PolicyCfg())
    assert (pol.swarm_bucket_ns(1), pol.full_bucket_ns(1)) == (40_000_000, 8_000_000)
    assert (pol.swarm_bucket_ns(10), pol.full_bucket_ns(10)) == (200_000_000, 80_000_000)
    sel = FrameSelector(pol)
    blocks, fulls = [], []
    for t in range(0, 1_000_000_000, 8_000_000):
        b, f = sel.select(t, 1.0)
        if b:
            blocks.append(t)
        if f:
            fulls.append(t)
    assert blocks == list(range(0, 1_000_000_000, 40_000_000))
    assert len(fulls) == 125
    sel.reset()
    blocks, fulls = [], []
    for t in range(0, 4_000_000_000, 40_000_000):  # ×10：tap 每 40 ms 仿真
        b, f = sel.select(t, 10.0)
        blocks += [t] if b else []
        fulls += [t] if f else []
    assert all(t % 200_000_000 == 0 for t in blocks) and len(blocks) == 20
    grid = set(range(0, 4_000_000_000, 80_000_000))
    assert set(blocks) <= set(fulls) and set(fulls) == grid | set(blocks)


def test_marked_set_priority_and_cap() -> None:
    ms = MarkedSet(16, 50)
    agents = list(range(100))
    ms.set_scenario_ids([f"v{i}" for i in range(16)])
    ms.resolve_scenario([{"id": f"v{i}", "agent_no": i} for i in range(100)])
    assert ms.current(agents) == frozenset(range(16))
    assert ms.on_interest_marks([40])
    assert ms.current(agents) == frozenset(range(16))  # 剧本 16 架已满，第 17 架不进
    ms.set_scenario_ids([f"v{i}" for i in range(10)])
    ms.resolve_scenario([{"id": f"v{i}", "agent_no": i} for i in range(100)])
    ms.on_interest_marks([41, 42])
    cur = ms.current(agents)
    assert len(cur) == 13 and {40, 41, 42} <= cur
    for k in range(50, 70):
        ms.on_interest_marks([k])
    cur = ms.current(agents)
    assert len(cur) == 16 and set(range(10)) <= cur and 69 in cur and 40 not in cur
    assert MarkedSet(16, 50).current(list(range(30))) == frozenset(range(30))


def test_keydelta_blocks() -> None:
    kd = KeyDeltaBlocker(2.0, 5_000_000_000)
    kd.offer({1: {"a": 1}, 2: {"a": 2}}, 7)
    p0, key0 = kd.poll(0)
    assert key0 and msgpack.unpackb(p0)["items"] == {"1": {"a": 1}, "2": {"a": 2}}
    assert kd.poll(100_000_000) is None  # 桶未到期
    kd.offer({2: {"a": 3}})
    p1, key1 = kd.poll(500_000_000)
    assert not key1 and msgpack.unpackb(p1)["items"] == {"2": {"a": 3}}
    assert kd.poll(1_000_000_000) is None  # 无变化不写
    kd.offer({1: {"a": 9}})
    p2, key2 = kd.poll(5_000_000_000)
    assert key2
    assert merge_blocks([p0, p1]) == {1: {"a": 1}, 2: {"a": 3}}
    assert merge_blocks([p0, p1, p2]) == {1: {"a": 9}, 2: {"a": 3}}
    assert parse_batch(msgpack.packb([[3, {"x": 1}], [4, b"raw"]])) == {3: {"x": 1}, 4: b"raw"}


def test_selection_is_deterministic(tmp_path: Path) -> None:
    from awr.recorder.synth import synthesize

    kw = {"n": 8, "sim_s": 6, "marked": ["sim-0001"], "events_per_s": 3,
          "rate": lambda t: 1.0 if t < 3_000_000_000 else 4.0}
    a = synthesize(tmp_path / "r20260929-000001-aaaa", **kw)
    b = synthesize(tmp_path / "r20260929-000002-bbbb", **kw)
    ma = rechelp.messages(Path(a["dir"]) / "rec-000.mcap")
    mb = rechelp.messages(Path(b["dir"]) / "rec-000.mcap")

    def strip(ms):  # 事件与 roster 中含墙钟与运行 id 的字段之外逐字节比较
        return [(t, lt, d) for t, lt, d in ms if t not in ("/sim/roster",)]

    assert len(ma) == len(mb) > 100
    assert strip(ma) == strip(mb)
    blocks = [lt for t, lt, _ in ma if t == "/swarm/uav/state_block"]
    assert blocks[:3] == [0, 40_000_000, 80_000_000]
    late = [lt for lt in blocks if lt > 3_100_000_000]
    assert all(y - x >= 80_000_000 for x, y in itertools.pairwise(late))  # ×4：块桶宽 80 ms
