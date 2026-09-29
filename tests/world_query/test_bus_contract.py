"""集成验证（MS1/MS2）：探针服务的请求与回复符合契约 `packages/contracts/bus/geo.schema.json`（M04 §7.3；AWR-17 §9.3）。

请求的 op 取 M04 §7.3 的名字（height_dsm 等），失败回复带可选 `detail`（原因名）；schema 按 M03-to-M00 请求第 2 条修订。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from awr.world.geometry import GeoProbeServer
from awr.world.geometry.fake import TOWER
from awr.world.geometry.probe import KIND_OPS

SCHEMA = json.loads((Path(__file__).resolve().parents[2] / "packages/contracts/bus/geo.schema.json").read_text())


def _validator(part: str) -> Draft202012Validator:
    return Draft202012Validator({**SCHEMA["$defs"][part], "$defs": SCHEMA["$defs"]})


REQ = _validator("req")
REP = _validator("rep")


class Q:
    def __init__(self, qid: str, op: str, args: dict, world_id: str | None = None):
        self.id, self.op, self.args, self.world_id = qid, op, args, world_id
        self.replies: list[dict] = []

    def reply(self, payload: dict) -> None:
        self.replies.append(payload)


def _drain(srv: GeoProbeServer) -> None:
    for _ in range(10_000):
        if not srv.q:
            return
        srv.run(500)


@pytest.mark.parametrize("op", sorted({op for ops in KIND_OPS.values() for op in ops}))
def test_every_served_op_is_in_the_contract(op):
    REQ.validate({"v": 1, "id": "x", "world_id": "shenzhen", "op": op, "args": {}})


def test_replies_validate(wq):
    srv = GeoProbeServer(wq)
    ok = [Q("a", "height_dsm", {"points": [[TOWER[0], TOWER[1]], [-1000.0, 0.0]]}),
          Q("b", "path_coarse_check", {"polyline": [[-150, 0, 150], [-150, 50, 150]]}),
          Q("c", "segment_los", {"pairs": [[[-150, -20, 50], [20, -20, 50]]]})]
    ray = Q("d", "ray_hit", {"origin_enu_m": [TOWER[0], TOWER[1], 300.0], "dir": [0, 0, -1], "max_range_m": 1000})
    bad_op = Q("e", "no_such_op", {})
    other_world = Q("f", "height_dsm", {"points": [[0.0, 0.0]]}, world_id="not-" + str(getattr(wq, "world_id", "w")))
    for q in [*ok, bad_op, other_world]:
        srv.enqueue("height", q)
    srv.enqueue("ray_hit", ray)
    _drain(srv)
    for q in [*ok, ray]:
        assert q.replies and q.replies[0]["ok"], q.op
        REP.validate(q.replies[0])
    for q, detail in ((bad_op, "UNKNOWN_OP"), (other_world, "WORLD_NOT_IN_SESSION")):
        r = q.replies[0]
        assert not r["ok"] and r["detail"] == detail
        REP.validate(r)
