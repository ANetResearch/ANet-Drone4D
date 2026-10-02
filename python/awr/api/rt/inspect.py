"""R57 `GET /api/rt/topics`、R58 `GET /api/rt/inspect`（D1-ext；M11-FR-030、FR-086；AWR-17 §4.3.14）。

- topics：当前 channel 表（与 `advertise` 同结构）+ `topics.json` 的 topic 模式表；viewer；
- inspect：`{tick{hz, age_p50_ms, age_p99_ms}, clients[{conn_id, principal_id, role, subs[], window, acked, frame_seq,
  credit_skips, srtt_ms, kbps, ctrl_queue_len}], channels[{id, topic, seq, encodes, last_t_sim_ns}], event_ring{oldest_seq,
  newest_seq, count, bytes}}` 与兴趣集、RPC 统计；生产（demo）profile 需要 admin，dev/test/ci 为 viewer；`?dump=<conn_id>` 返回该连接最近
  一帧 BATCH 的十六进制与逐记录解码（仅 dev/test/ci）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from awr.contracts import frame as F
from awr.contracts import topics as T

from ..deps import Viewer, app_ctx
from ..problem import ApiProblem

__all__ = ["router"]

router = APIRouter(prefix="/api/rt", tags=["rt"])

DEBUG_PROFILES = ("dev", "ci", "test", "perf")


@router.get("/topics")
async def topics(request: Request, _p: Viewer) -> dict[str, Any]:
    gw = app_ctx(request).gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    return {"channels": gw.registry.adverts(),
            "topics": [{"pattern": t.pattern, "kind": t.kind, "encoding": t.encoding, "schemaName": t.schema_name,
                        "nativeHz": t.native_hz, "defaultRate": t.default_rate, "mode": t.mode, "priority": t.priority,
                        "selfContained": t.self_contained, "d1": t.d1, "aliases": list(t.aliases)} for t in T.TOPICS],
            "rate_classes": list(T.RATE_CLASSES), "tick_hz": T.TICK_HZ}


@router.get("/inspect")
async def inspect(request: Request, p: Viewer, dump: str | None = None) -> dict[str, Any]:
    ctx = app_ctx(request)
    gw = ctx.gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    debug = ctx.settings.profile in DEBUG_PROFILES
    if not debug and not p.at_least("admin"):
        raise ApiProblem(115, status=403, detail={"need": "admin"})
    out = gw.inspect()
    if dump is not None:
        if not debug:
            raise ApiProblem(115, status=403, detail={"dump": "dev/test only"})
        s = next((x for x in gw.sessions if x.conn_id == dump), None)
        if s is None:
            raise ApiProblem(305, status=404, detail={"conn_id": dump})
        out["dump"] = _dump(s.last_frame)
    return out


def _dump(frame: bytes) -> dict[str, Any]:
    if not frame:
        return {"hex": "", "records": []}
    h = F.decode_batch_header(frame)
    recs = [{"channel_id": r.channel_id, "encoding": r.encoding, "rflags": r.rflags, "length": r.length, "seq": r.seq,
             "dt_us": r.dt_us, "payload_hex": frame[r.payload_off:r.payload_off + min(r.length, 256)].hex()}
            for r in F.iter_records(frame)]
    return {"header": {"flags": h.flags, "epoch": h.epoch, "frame_seq": h.frame_seq, "t_sim_ns": h.t_sim_ns},
            "bytes": len(frame), "hex": frame[:512].hex(), "records": recs}
