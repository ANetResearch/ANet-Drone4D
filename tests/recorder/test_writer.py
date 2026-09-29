"""McapSegmentWriter（M12 §6.6.4；FR-034；M12-AC-034 的结构部分）：

- 队列超限时先丢 Full64（75 %）再丢块（100 %），事件与低频永不丢，丢弃计数进入 `take_dropped()`；
- 写出的 MCAP 可被 mcap 1.5.0 读取：summary、统计与消息数一致，`log_time = publish_time`，每条消息带 RecPrefix8，
  channel metadata 含 record_policy 与 backfill，段首 `awr.binding`，段尾 `awr.segment_end`；
- 谱系 metadata 的 `chunk_start` 等于新纪元第一个 chunk 的序号（FLUSH 在前）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from mcap.reader import make_reader

from awr.recorder.formats import prefix
from awr.recorder.writer import Kind, McapSegmentWriter, SegmentSpec

pytestmark = pytest.mark.ext


def _spec(tmp: Path) -> SegmentSpec:
    return SegmentSpec(k=0, path=tmp / "rec-000.mcap", ovw_path=tmp / "rec-000.ovw", evx_path=tmp / "rec-000.evx",
                       binding={"world_id": "shenzhen", "segment": "0", "ovw_tracks": ""}, tracks=[])


def test_drop_policy(tmp_path: Path) -> None:
    w = McapSegmentWriter(queue_bytes=4096, flush_wall_s=3600)
    with w.cv:  # 冻结 writer 线程：先占住条件变量，入队不被消费
        w.busy = True
        ok_full = [w.put(Kind.FULL, "/uav/a/state", i, b"x" * 1000) for i in range(5)]
        ok_block = [w.put(Kind.BLOCK, "/swarm/uav/state_block", i, b"y" * 1000) for i in range(5)]
        ok_keep = [w.put(Kind.KEEP, "/event", i, b"z" * 1000) for i in range(5)]
        w.busy = False
    assert ok_full == [True, True, True, False, False]  # 75 % = 3072 B
    assert ok_block == [True, False, False, False, False]  # 100 % = 4096 B
    assert all(ok_keep)
    assert w.take_dropped() == {"full": 2, "block": 4}
    assert w.take_dropped() == {"full": 0, "block": 0}
    w.close()


def test_mcap_structure(tmp_path: Path) -> None:
    w = McapSegmentWriter(flush_wall_s=3600)
    w.start_segment(_spec(tmp_path))
    for i in range(100):
        w.put(Kind.BLOCK, "/swarm/uav/state_block", i * 40_000_000, prefix(1) + b"\x00" * 16, i)
        if i == 49:
            w.flush()
    w.lineage({"epoch": 2, "restored_t_ns": 1_000_000_000, "last_t_ns": 1_960_000_000, "invalid_from_ns": 1_000_000_000,
               "invalid_to_ns": 1_960_000_000})
    for i in range(10):
        w.put(Kind.KEEP, "/event", 1_000_000_000 + i, prefix(2) + b"\x80", i)
    w.end_segment(4_000_000_000, "stop")
    assert w.drain(10)
    w.close()
    with open(tmp_path / "rec-000.mcap", "rb") as f:
        r = make_reader(f)
        s = r.get_summary()
        assert s is not None and s.statistics.message_count == 110
        topics = {c.topic: c for c in s.channels.values()}
        assert topics["/swarm/uav/state_block"].metadata["backfill"] == "latest"
        assert topics["/event"].metadata["backfill"] == "none"
        md = {m.name: m.metadata for m in r.iter_metadata()}
        assert md["awr.binding"]["world_id"] == "shenzhen"
        assert md["awr.segment_end"] == {"t_end_ns": "4000000000", "reason": "stop"}
        assert md["awr.lineage"]["chunk_start"] == "2" and len(s.chunk_indexes) == 3
        for _sc, _ch, m in r.iter_messages():
            assert m.log_time == m.publish_time and m.data[3] == 1  # prefix_version
    assert (tmp_path / "rec-000.evx").stat().st_size == 32 + 16 * 10


def test_writer_survives_closed_file_error(tmp_path: Path) -> None:
    """写失败被记录到 error，主线程据此停录（磁盘满等）。"""
    w = McapSegmentWriter(flush_wall_s=3600)
    spec = _spec(tmp_path / "missing" / "deep")
    spec.path = Path("/proc/awr-no-such/rec-000.mcap")
    w.start_segment(spec)
    t0 = time.monotonic()
    while w.error is None and time.monotonic() - t0 < 5:
        time.sleep(0.01)
    assert w.error is not None
    w.close()
