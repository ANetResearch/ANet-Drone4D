"""SegmentIndex（M12 §6.7.2；FR-042；M12-AC-042 的功能部分；RK-M12-02）：

- 已关闭段读 summary、OPEN 段线性遍历，两者得到相同的逐 channel 索引（时刻、chunk、偏移、写入序号）；
- `last_le` 取不晚于 t 的最后一条（同刻取最后写入），`exact`、`range(t0, t1]` 语义；`/event` 的 mseq 映射；
- OPEN 段追读（`refresh_tail`）只接受 MessageIndex 组完整的 chunk，随文件增长单调增加；
- 源码中不得出现逆序 `iter_messages(..., reverse=True)`（backfill 必须走逐 channel 索引）。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import numpy as np
import pytest
import rechelp

from awr.recorder.mcap_index import SegmentIndex

pytestmark = pytest.mark.ext
REC = Path(__file__).resolve().parents[2] / "python" / "awr" / "recorder"


def test_summary_and_scan_agree(std_run: dict, tmp_path: Path) -> None:
    p = Path(std_run["dir"]) / "rec-000.mcap"
    a = SegmentIndex.build(p)
    assert a.closed
    # 截断在最后一个 chunk 内部，模拟未关闭段：线性遍历，只接受 MessageIndex 组完整的 chunk
    q = tmp_path / "rec-000.mcap"
    shutil.copy(p, q)
    last = max(c.start + c.length for c in a.chunks)
    size = p.stat().st_size
    with open(q, "r+b") as f:
        f.truncate(min(size, last - 1))
    b = SegmentIndex.build(q, allow_open=True)
    assert not b.closed and len(b.chunks) == len(a.chunks) - 1
    for cid, (t, c, o, w) in b.idx.items():
        ta, ca, oa, wa = a.idx[a.topic_ch[b.channels[cid].topic]]
        n = len(t)
        assert np.array_equal(t, ta[:n]) and np.array_equal(c, ca[:n]) and np.array_equal(o, oa[:n]) and np.array_equal(w, wa[:n])
    st = a.stats()
    assert st["entries"] == sum(len(v[0]) for v in a.idx.values()) and st["memory_bytes"] > 0
    a.close()
    b.close()


def test_lookup_semantics(std_run: dict) -> None:
    ix = SegmentIndex.build(Path(std_run["dir"]) / "rec-000.mcap")
    blk = ix.ch("/swarm/uav/state_block")
    assert ix.last_le(blk, -1) is None
    assert ix.last_le(blk, 0).t == 0
    assert ix.last_le(blk, 39_999_999).t == 0 and ix.last_le(blk, 40_000_000).t == 40_000_000
    assert ix.exact(blk, 40_000_000) is not None and ix.exact(blk, 40_000_001) is None
    ts = [r.t for r in ix.range(blk, 1_000_000_000, 1_200_000_000)]
    assert ts == [1_040_000_000, 1_080_000_000, 1_120_000_000, 1_160_000_000, 1_200_000_000]
    ev = ix.ch("/event")
    t, _c, _o, w = ix.idx[ev]
    assert np.all(t[1:] >= t[:-1])
    assert np.array_equal(ix.by_ord[ev][w], np.arange(len(w)))
    assert ix.last_le(ix.ch("/no/such"), 5) is None and ix.count(None) == 0
    ix.close()


def test_refresh_tail_grows(std_run: dict, tmp_path: Path) -> None:
    src = (Path(std_run["dir"]) / "rec-000.mcap").read_bytes()
    q = tmp_path / "rec-000.mcap"
    q.write_bytes(src[: len(src) // 4])
    ix = SegmentIndex.build(q, allow_open=True)
    n0 = len(ix.chunks)
    e0 = ix.data_end_ns
    with open(q, "ab") as f:
        f.write(src[len(src) // 4: len(src) // 2])
    added = ix.refresh_tail()
    assert added > 0 and len(ix.chunks) > n0 and ix.data_end_ns > e0
    blk = ix.ch("/swarm/uav/state_block")
    t = ix.idx[blk][0]
    assert np.all(t[1:] > t[:-1])
    with open(q, "ab") as f:
        f.write(src[len(src) // 2:])
    ix.refresh_tail()
    assert ix.closed and ix.data_end_ns == 30_000_000_000
    ix.close()


def test_no_reverse_iter_messages_in_recorder() -> None:
    pat = re.compile(r"iter_messages\([^)]*reverse\s*=\s*True")
    for f in REC.rglob("*.py"):
        assert not pat.search(f.read_text(encoding="utf-8")), f
    assert rechelp.RUN
