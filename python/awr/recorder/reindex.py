"""从 MCAP 重建派生索引 `.ovw`、`.evx`（M12 §6.6.6、FR-037；M12-AC-037：重建结果与在线写出逐字节一致）。

按文件顺序遍历记录：chunk 内的 Channel 与 Message、chunk 之间的 Metadata（`awr.binding` 给出段号与概览轨迹机、
`awr.lineage` 给出回滚区间），逐条喂给与 writer 线程相同的 `SidecarBuilder`。

用法：`python -m awr.recorder.reindex runs/<run>/rec-000.mcap [--out-dir DIR]`
"""

from __future__ import annotations

import argparse
import io
import struct
import sys
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import zstandard
from mcap.data_stream import ReadDataStream
from mcap.records import Attachment, Channel, Metadata, Schema

from .mcap_index import (
    OP_ATTACH,
    OP_CHANNEL,
    OP_CHUNK,
    OP_DATAEND,
    OP_FOOTER,
    OP_MESSAGE,
    OP_META,
    OP_SCHEMA,
    _chunk_header,
)
from .sidecar import SidecarBuilder

__all__ = ["iter_records", "reindex"]

_REC = struct.Struct("<BQ")
_MSG = struct.Struct("<HIQQ")


def iter_records(path: Path, *, verify_crc: bool = True) -> Iterator[tuple[str, Any]]:
    """文件顺序的逻辑记录：("schema", Schema)、("channel", Channel)、("message", (ch, seq, log_time, publish_time, data))、
    ("chunk_end", index)、("metadata", Metadata)、("attachment", Attachment)、("end", offset)。遇到截断或损坏记录即停止，
    最后一条为 ("end", 最后一个完整记录的结束偏移)。"""
    data = Path(path).read_bytes()
    size = len(data)
    pos = 8
    ok_end = pos
    ci = 0
    while pos + _REC.size <= size:
        op, ln = _REC.unpack_from(data, pos)
        body_at = pos + _REC.size
        end = body_at + ln
        if end > size or op == 0 or op > 0x0F:
            break
        body = memoryview(data)[body_at:end]
        if op == OP_CHUNK:
            try:
                _t0, _t1, usize, crc, comp, ro, rl = _chunk_header(body)
                raw = bytes(body[ro:ro + rl])
                inner = zstandard.ZstdDecompressor().decompress(raw, max_output_size=usize) if comp == "zstd" else raw
            except Exception:
                break
            if verify_crc and crc and zlib.crc32(inner) != crc:
                break
            p = 0
            while p + _REC.size <= len(inner):
                iop, iln = _REC.unpack_from(inner, p)
                ib = inner[p + _REC.size:p + _REC.size + iln]
                if iop == OP_SCHEMA:
                    yield "schema", Schema.read(ReadDataStream(io.BytesIO(ib)))
                elif iop == OP_CHANNEL:
                    yield "channel", Channel.read(ReadDataStream(io.BytesIO(ib)))
                elif iop == OP_MESSAGE:
                    ch, seq, lt, pt = _MSG.unpack_from(ib, 0)
                    yield "message", (ch, seq, lt, pt, ib[_MSG.size:])
                p += _REC.size + iln
            yield "chunk_end", ci
            ci += 1
        elif op == OP_META:
            yield "metadata", Metadata.read(ReadDataStream(io.BytesIO(bytes(body))))
        elif op == OP_ATTACH:
            try:
                yield "attachment", Attachment.read(ReadDataStream(io.BytesIO(bytes(body))))
            except Exception:
                break
        elif op in (OP_DATAEND, OP_FOOTER):
            ok_end = end
            break
        ok_end = end
        pos = end
    yield "end", ok_end


def reindex(mcap_path: Path, out_dir: Path | None = None) -> tuple[Path, Path]:
    mcap_path = Path(mcap_path)
    d = out_dir or mcap_path.parent
    stem = mcap_path.stem  # rec-000
    ovw, evx = d / f"{stem}.ovw", d / f"{stem}.evx"
    topics: dict[int, str] = {}
    b: SidecarBuilder | None = None
    for kind, rec in iter_records(mcap_path, verify_crc=False):
        if kind == "metadata":
            if rec.name == "awr.binding" and b is None:
                m = rec.metadata
                tracks = [int(x) for x in m.get("ovw_tracks", "").split(",") if x.strip()]
                b = SidecarBuilder(ovw, evx, segment=int(m.get("segment", stem.split("-")[-1])), tracks=tracks)
            elif rec.name == "awr.lineage" and b is not None:
                m = rec.metadata
                b.on_lineage(int(m["epoch"]), int(m["restored_t_ns"]), int(m["last_t_ns"]))
        elif kind == "channel":
            topics[rec.id] = rec.topic
        elif kind == "message":
            if b is None:
                b = SidecarBuilder(ovw, evx, segment=int(stem.split("-")[-1]), tracks=[])
            ch, _seq, lt, _pt, data = rec
            t = topics.get(ch)
            if t is not None:
                b.on_message(t, lt, bytes(data))
    if b is None:
        b = SidecarBuilder(ovw, evx, segment=int(stem.split("-")[-1]), tracks=[])
    b.close()
    return ovw, evx


def main() -> int:
    ap = argparse.ArgumentParser(description="重建 .ovw 与 .evx（M12 reindex）")
    ap.add_argument("mcap", type=Path)
    ap.add_argument("--out-dir", type=Path, default=None)
    a = ap.parse_args()
    o, e = reindex(a.mcap, a.out_dir)
    print(f"{o}\n{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
