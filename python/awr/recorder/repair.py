"""修复未正常关闭的录制段（M12 §6.6.7、FR-040；19 §13.3；M12-AC-040）。

`python -m awr.recorder.repair runs/<run>/rec-000.mcap`
1. 线性遍历记录（`reindex.iter_records`）：长度越过文件尾或 opcode 非法即为截断点；chunk 解压失败或 CRC 不符同样截止；
2. 以 mcap Writer（awr profile）按原顺序重写：chunk 内的 Schema、Channel、Message 原样写入（每个源 chunk 之后 flush，
   chunk 序号不变，`awr.lineage.chunk_start` 仍然有效），chunk 之间的 Metadata 与 Attachment 原样写入；缺少
   `awr.segment_end` 时补写 `{t_end_ns: 最后一条消息时刻, reason: "repaired"}`；写出新的 summary 与 footer；
3. 原文件改名为 `.orig`，临时文件原子改名为原名；`meta.json` 段状态置 CLOSED（失败置 CORRUPT，461）；
4. 调用 reindex 重建 `.ovw`、`.evx`。
supervisor 启动扫描 UNFINALIZED 运行中 `state = OPEN` 且无 recorder 在写的段时调用（19 §13.3）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcap.writer import CompressionType, IndexType, Writer

from .meta import atomic_write_json
from .reindex import iter_records, reindex

__all__ = ["repair"]


def _update_meta(path: Path, *, state: str, t_end_ns: int | None, nbytes: int | None) -> None:
    mp = path.parent / "meta.json"
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    k = int(path.stem.split("-")[-1])
    for s in m.get("segments", []):
        if int(s.get("segment", -1)) == k:
            s["state"] = state
            if t_end_ns is not None:
                s["t_end_ns"] = int(t_end_ns)
            if nbytes is not None:
                s["bytes"] = int(nbytes)
            dur = ((s.get("t_end_ns") or s.get("t_start_ns") or 0) - (s.get("t_start_ns") or 0)) / 1e9
            if dur > 0 and nbytes:
                s["bytes_per_sim_s"] = round(nbytes / dur, 3)
    atomic_write_json(mp, m)


def repair(path: Path, *, update_meta: bool = True) -> dict[str, Any]:
    """返回 {ok, state, messages, chunks, t_end_ns, truncated_at, orig}。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".repair")
    schemas: dict[int, int] = {}
    channels: dict[int, int] = {}
    n_msg = 0
    n_chunk = 0
    t_last = 0
    has_end = False
    end_at = 0
    try:
        with open(tmp, "wb") as f:
            w = Writer(f, chunk_size=4 << 20, compression=CompressionType.ZSTD, index_types=IndexType.ALL, repeat_channels=True,
                       repeat_schemas=True, use_chunking=True, use_statistics=True, use_summary_offsets=True, enable_crcs=True)
            w.start(profile="awr", library="awr.recorder.repair")
            pending_msgs = 0
            for kind, rec in iter_records(path):
                if kind == "schema":
                    if rec.id not in schemas:
                        schemas[rec.id] = w.register_schema(name=rec.name, encoding=rec.encoding, data=rec.data)
                elif kind == "channel":
                    if rec.id not in channels:
                        channels[rec.id] = w.register_channel(topic=rec.topic, message_encoding=rec.message_encoding,
                                                              schema_id=schemas.get(rec.schema_id, 0), metadata=dict(rec.metadata))
                elif kind == "message":
                    ch, seq, lt, pt, data = rec
                    if ch not in channels:
                        continue
                    w.add_message(channel_id=channels[ch], log_time=lt, data=bytes(data), publish_time=pt, sequence=seq)
                    pending_msgs += 1
                    t_last = max(t_last, lt)
                elif kind == "chunk_end":
                    if pending_msgs:
                        w.flush()
                        n_chunk += 1
                        n_msg += pending_msgs
                        pending_msgs = 0
                elif kind == "metadata":
                    if rec.name == "awr.segment_end":
                        has_end = True
                    w.add_metadata(rec.name, dict(rec.metadata))
                elif kind == "attachment":
                    w.add_attachment(create_time=rec.create_time, log_time=rec.log_time, name=rec.name, media_type=rec.media_type,
                                     data=rec.data)
                elif kind == "end":
                    end_at = int(rec)
            if not has_end:
                w.add_metadata("awr.segment_end", {"t_end_ns": str(t_last), "reason": "repaired"})
            w.finish()
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        if update_meta:
            _update_meta(path, state="CORRUPT", t_end_ns=None, nbytes=None)
        return {"ok": False, "state": "CORRUPT", "messages": n_msg, "chunks": n_chunk, "t_end_ns": None, "truncated_at": end_at,
                "orig": None}
    if n_chunk == 0:
        with contextlib.suppress(OSError):
            tmp.unlink()
        if update_meta:
            _update_meta(path, state="CORRUPT", t_end_ns=None, nbytes=None)
        return {"ok": False, "state": "CORRUPT", "messages": 0, "chunks": 0, "t_end_ns": None, "truncated_at": end_at, "orig": None}
    orig = path.with_name(path.name + ".orig")
    os.replace(path, orig)
    os.replace(tmp, path)
    nbytes = path.stat().st_size
    if update_meta:
        _update_meta(path, state="CLOSED", t_end_ns=t_last, nbytes=nbytes)
    reindex(path)
    return {"ok": True, "state": "CLOSED", "messages": n_msg, "chunks": n_chunk, "t_end_ns": t_last, "truncated_at": end_at,
            "orig": str(orig)}


def main() -> int:
    ap = argparse.ArgumentParser(description="修复录制段（M12 repair）")
    ap.add_argument("mcap", type=Path)
    ap.add_argument("--no-meta", action="store_true")
    a = ap.parse_args()
    r = repair(a.mcap, update_meta=not a.no_meta)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
