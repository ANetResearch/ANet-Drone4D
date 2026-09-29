"""分段与谱系（M12 §6.6.5；FR-035、FR-036；M12-AC-035、AC-036）：

- 剧本中途重置两次得到 3 个文件段；每段首条消息依次为 roster、（时钟）、最近 EnvKeyframe，每段 CLOSED、可独立打开与 seek；
- checkpoint 恢复：`awr.lineage` 键齐全（epoch、restored_t_ns、last_t_ns、invalid_from_ns、invalid_to_ns、chunk_start），
  seek 到回滚重跑区间只得到新纪元数据，`meta.json` 的 lineage 记录作废区间，`.evx` 旧纪元记录置 superseded。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rechelp

from awr.contracts.layouts import SWARM_LITE32
from awr.recorder.formats import BLOCK_HDR, split_prefix
from awr.recorder.mcap_index import SegmentIndex

pytestmark = pytest.mark.ext


def test_three_segments_after_two_resets(tmp_path: Path) -> None:
    from awr.recorder.synth import synthesize

    d = tmp_path / rechelp.RUN
    out = synthesize(d, n=6, sim_s=4, resets=2, events_per_s=2)
    segs = out["segments"]
    assert [s["segment"] for s in segs] == [0, 1, 2]
    assert all(s["state"] == "CLOSED" for s in segs)
    meta = json.loads((d / "meta.json").read_text())
    assert [meta["sidecars"][f"{k:03d}"]["sim_segment"] for k in range(3)] == [0, 1, 2]
    for k in range(3):
        p = d / f"rec-{k:03d}.mcap"
        msgs = rechelp.messages(p)
        firsts = [t for t, _l, _d in msgs[:2]]
        assert firsts[0] == "/sim/roster"
        assert "/env/state" in [t for t, _l, _d in msgs[:3]]
        ix = SegmentIndex.build(p)
        assert ix.closed and ix.segment_end() is not None
        assert ix.data_start_ns == 0 and ix.data_end_ns >= 3_900_000_000
        r = ix.last_le(ix.ch("/swarm/uav/state_block"), 2_345_000_000)
        assert r is not None and r.t == 2_320_000_000
        ix.close()
    ends = [m.get("reason") for m in (SegmentIndex.build(d / f"rec-{k:03d}.mcap").segment_end() for k in range(3))]
    assert ends == ["scenario_reset", "scenario_reset", "stop"]


def test_lineage_prunes_rolled_back_data(tmp_path: Path) -> None:
    from awr.recorder.synth import synthesize

    d = tmp_path / rechelp.RUN2
    synthesize(d, n=6, sim_s=20, lineage=(12_000_000_000, 8_000_000_000), events_per_s=4)
    ix = SegmentIndex.build(d / "rec-000.mcap")
    lin = ix.lineage()
    assert len(lin) == 1
    e = lin[0]
    assert set(e) == {"epoch", "restored_t_ns", "last_t_ns", "invalid_from_ns", "invalid_to_ns", "chunk_start"}
    assert e["epoch"] == 2 and e["restored_t_ns"] == 8_000_000_000 and 11_900_000_000 <= e["last_t_ns"] <= 12_000_000_000
    blk = ix.ch("/swarm/uav/state_block")
    t = ix.idx[blk][0]
    assert np.all(t[1:] >= t[:-1])  # 裁剪后单调
    # 回滚区间内的每个块都来自新纪元（RecPrefix8 epoch = 2）
    from awr.recorder.chunk_cache import ChunkCache

    cache = ChunkCache(ix)
    for tq in (8_500_000_000, 10_000_000_000, 11_990_000_000):
        r = ix.last_le(blk, tq)
        ep, _rf, _dt, payload = split_prefix(cache.message(r).data)
        assert ep == 2
        n = BLOCK_HDR.unpack_from(payload, 0)[0]
        assert len(np.frombuffer(bytes(payload), SWARM_LITE32, count=n, offset=16)) == 6
    r = ix.last_le(blk, 5_000_000_000)
    assert split_prefix(cache.message(r).data)[0] == 1
    ev = ix.ch("/event")
    import msgpack

    for ref in ix.range(ev, 8_000_000_000, 12_000_000_000):
        e2 = msgpack.unpackb(split_prefix(cache.message(ref).data)[3], raw=False)
        assert e2["epoch"] == 2  # 旧纪元在回滚区间内的事件已被谱系裁剪
    meta = json.loads((d / "meta.json").read_text())
    seg = meta["segments"][0]
    assert seg["lineage"][0]["invalid"] == [[8_000_000_000, e["last_t_ns"]]]
    assert seg["lineage"][1]["epoch"] == 2
    ix.close()
