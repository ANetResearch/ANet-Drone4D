"""M04-AC-013（单元部分）、AC-022：探针服务。结果与直接调用相同；片预算内分片执行、跨迭代续算；队列满 213；同 id 重试替换；
入队 1 s 未执行丢弃；会话切换回复 123；未知 op 300、越界 110；geo.ready 字段齐全。"""

from __future__ import annotations

import numpy as np

from awr.world.geometry import GeoProbeServer
from awr.world.geometry.fake import TOWER


class Q:
    def __init__(self, qid, op, args):
        self.id, self.op, self.args = qid, op, args
        self.replies = []

    def reply(self, payload):
        self.replies.append(payload)


def _drain(srv, budget=500, n=1000):
    for _ in range(n):
        if not srv.q:
            return
        srv.run(budget)


def test_results_equal_direct(wq):
    srv = GeoProbeServer(wq)
    pts = [[TOWER[0], TOWER[1]], [0.0, 0.0], [-1000.0, 0.0]]
    q1 = Q("a", "height_dsm", {"points": pts})
    q2 = Q("b", "ray_hit", {"origin_enu_m": [TOWER[0], TOWER[1], 300.0], "dir": [0, 0, -1], "max_range_m": 1000})
    q3 = Q("c", "path_coarse_check", {"polyline": [[-150, 0, 150], [-150, 50, 150]]})
    q4 = Q("d", "segment_los", {"pairs": [[[-150, -20, 50], [20, -20, 50]]]})
    srv.enqueue("height", q1)
    srv.enqueue("ray_hit", q2)
    srv.enqueue("height", q3)
    srv.enqueue("height", q4)
    _drain(srv)
    r1 = q1.replies[0]
    assert r1["ok"] and r1["code"] == 0 and r1["source"] == "dsm_2m" and r1["content_version"] == wq.content_version
    assert r1["result"]["z_m"][:2] == [float(v) for v in wq.height_dsm(np.array(pts[:2]))] and r1["result"]["z_m"][2] is None
    assert q2.replies[0]["result"] == wq.ray_hit([TOWER[0], TOWER[1], 300.0], [0, 0, -1], 1000).to_json()
    assert q3.replies[0]["result"]["ok"] is True and q4.replies[0]["result"]["visible"] == [False]
    assert all(q.replies[0]["t_proc_us"] >= 0 for q in (q1, q2, q3, q4))


def test_slicing_and_budget(wq):
    srv = GeoProbeServer(wq)
    q = Q("r", "ray_hit", {"origin_enu_m": [-199.0, -149.0, 140.0], "dir": list(np.array([400, 300, -3.0]) / np.linalg.norm([400, 300, -3.0])),
                           "max_range_m": 5000})
    srv.enqueue("ray_hit", q)
    calls = 0
    while srv.q and calls < 10_000:
        srv.run(1)                    # 极小片预算：每次调用至多推进一个块
        calls += 1
    assert q.replies and calls >= 2
    assert srv.metrics()["slice_us_p99"] < 5_000


def test_queue_full_and_retry_replace(wq):
    srv = GeoProbeServer(wq, queue_max=2)
    a, b, c = Q("1", "height_dsm", {"points": [[0, 0]]}), Q("2", "height_dsm", {"points": [[0, 0]]}), Q("3", "height_dsm", {"points": [[0, 0]]})
    srv.enqueue("height", a)
    srv.enqueue("height", b)
    srv.enqueue("height", c)
    assert c.replies[0]["code"] == 213 and c.replies[0]["detail"] == "GEO_QUEUE_FULL"
    a2 = Q("1", "ground_dtm", {"points": [[0, 0]]})
    srv.enqueue("height", a2)                               # 同 id 重试：替换尚未开始的旧请求
    _drain(srv)
    assert not a.replies and a2.replies[0]["source"] == "dtm_10m" and len(srv.q) == 0


def test_deadline_and_close(wq):
    now = [0]
    srv = GeoProbeServer(wq, clock_ns=lambda: now[0])
    old = Q("x", "height_dsm", {"points": [[0, 0]]})
    srv.enqueue("height", old)
    now[0] += 1_100_000_000
    srv.run(500)
    assert not old.replies and srv.stats.expired == 1
    q = Q("y", "height_dsm", {"points": [[0, 0]]})
    srv.enqueue("height", q)
    srv.close()
    assert q.replies[0]["code"] == 123
    late = Q("z", "height_dsm", {"points": [[0, 0]]})
    srv.enqueue("height", late)
    assert late.replies[0]["code"] == 123


def test_errors(wq):
    srv = GeoProbeServer(wq)
    bad = Q("u", "teleport", {})
    wrong_kind = Q("k", "ray_hit", {"origin_enu_m": [0, 0, 10], "dir": [0, 0, -1]})
    big = Q("b", "height_dsm", {"points": [[0, 0]] * 65})
    notunit = Q("n", "ray_hit", {"origin_enu_m": [0, 0, 10], "dir": [0, 0, -2]})
    srv.enqueue("height", bad)
    srv.enqueue("height", wrong_kind)
    srv.enqueue("height", big)
    srv.enqueue("ray_hit", notunit)
    _drain(srv)
    assert bad.replies[0]["code"] == 300 and wrong_kind.replies[0]["code"] == 300
    assert big.replies[0]["code"] == 110 and notunit.replies[0]["code"] == 300


def test_iter_op_matches_probe(wq):
    gen = wq.iter_op("probe", {"points": [[TOWER[0], TOWER[1]]]})
    try:
        while True:
            next(gen)
    except StopIteration as done:
        assert done.value["items"] == wq.probe(np.array([[TOWER[0], TOWER[1]]]))


def test_geo_ready_fields(wq):
    ev = wq.ready_event()
    assert set(ev) == {"world_id", "content_version", "derive_sha8", "cache", "load_ms", "qa"}
    assert set(ev["qa"]) == {"empty_frac", "pits_filled_frac", "raised_frac", "dsm_max_m", "mem_mib"}
    assert ev["cache"] in ("hit", "built")


class BusReq:
    """`awr.runtime.bus.Request` 的形状：`msg()` 返回解包后的请求体，`reply_msg()` 打包回复。"""

    def __init__(self, body):
        self.body, self.sent = body, []

    def msg(self):
        return self.body

    def reply_msg(self, obj):
        self.sent.append(obj)
        return True


def test_runtime_request_adapter(wq):
    srv = GeoProbeServer(wq)
    ok = BusReq({"v": 1, "id": "q1", "world_id": wq.world_id, "op": "height_dsm", "args": {"points": [[TOWER[0], TOWER[1]]]}})
    other = BusReq({"v": 1, "id": "q2", "world_id": "not-this-world", "op": "height_dsm", "args": {"points": [[0, 0]]}})
    junk = BusReq(b"not a dict")
    for r in (ok, other, junk):
        srv.enqueue("height", r)
    _drain(srv)
    assert ok.sent[0]["ok"] and ok.sent[0]["id"] == "q1" and ok.sent[0]["result"]["z_m"] == [100.0]
    assert other.sent[0]["code"] == 123 and other.sent[0]["detail"] == "WORLD_NOT_IN_SESSION"
    assert junk.sent[0]["code"] == 300
