"""修复与 OPEN 段读取（M12 §6.6.7；FR-040、FR-041；M12-AC-040、AC-041 的 OPEN 段部分；RK-M12-09）：

- 录制文件在随机位置截断（模拟 kill -9 与断电）：未修复时 SegmentIndex 线性遍历读到最后一个完整 chunk；
- `repair` 重写 summary（原文件保留为 `.orig`），段置 CLOSED，消息序列是原序列的前缀（逐字节），丢失 ≤ 1 个 chunk
  （1 s 墙钟）；重建的 `.ovw`、`.evx` 与段内容一致；
- 无任何完整 chunk 的文件修复失败，段置 CORRUPT（461）。
"""

from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import pytest
import rechelp

from awr.recorder.mcap_index import SegmentIndex
from awr.recorder.repair import repair

pytestmark = pytest.mark.ext


def _copy_run(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


def test_truncated_segments_read_and_repair(std_run: dict, tmp_path: Path) -> None:
    src = Path(std_run["dir"])
    orig = rechelp.messages(src / "rec-000.mcap")
    size = (src / "rec-000.mcap").stat().st_size
    ix0 = SegmentIndex.build(src / "rec-000.mcap")
    chunk_ends = [c.start + c.length for c in ix0.chunks]
    per_chunk = [0] * len(ix0.chunks)
    for _t, c, _o, _w in ix0.idx.values():
        for ci in c.tolist():
            per_chunk[ci] += 1
    ix0.close()
    rng = random.Random(40)
    cuts = sorted(rng.randrange(size // 10, size - 64) for _ in range(12))
    for i, cut in enumerate(cuts):
        d = _copy_run(src, tmp_path / f"c{i}" / rechelp.RUN)
        p = d / "rec-000.mcap"
        with open(p, "r+b") as f:
            f.truncate(cut)
        # 未修复：线性遍历到最后一个完整 chunk（其后的 MessageIndex 组完整才接受）
        ix = SegmentIndex.build(p, allow_open=True)
        assert not ix.closed
        complete = [e for e in chunk_ends if e <= cut]
        assert len(ix.chunks) in (max(0, len(complete) - 1), len(complete))
        ix.close()
        r = repair(p)
        assert r["ok"] and r["state"] == "CLOSED", r
        assert (d / "rec-000.mcap.orig").stat().st_size == cut
        got = rechelp.messages(p)
        assert got == orig[: len(got)]
        # 截断点之前完整 chunk 的消息全部保留（丢失 ≤ 截断处的一个 chunk，即 ≤ 1 s 墙钟）
        assert len(got) >= sum(per_chunk[: max(0, len(complete) - 1)])
        ix = SegmentIndex.build(p)
        assert ix.closed and ix.segment_end() is not None
        assert ix.data_end_ns >= r["t_end_ns"] - 1
        ix.close()
        seg = json.loads((d / "meta.json").read_text())["segments"][0]
        assert seg["state"] == "CLOSED" and seg["t_end_ns"] == r["t_end_ns"]
        assert (d / "rec-000.evx").stat().st_size >= 32


def test_unrecoverable_is_corrupt(std_run: dict, tmp_path: Path) -> None:
    d = _copy_run(Path(std_run["dir"]), tmp_path / rechelp.RUN)
    p = d / "rec-000.mcap"
    with open(p, "r+b") as f:
        f.truncate(200)
    r = repair(p)
    assert not r["ok"] and r["state"] == "CORRUPT"
    assert json.loads((d / "meta.json").read_text())["segments"][0]["state"] == "CORRUPT"
