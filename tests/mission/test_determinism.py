"""确定性（M10-NFR-010；M10-AC-009 的功能部分）：同一剧情在全新的 SimCore 上跑两次，每个规划结果的
`result_sha256` 与生效步号（apply_tick）相同，轨迹控制点逐字节相同，Track 事件序列相同。
（S1 全程两次属 M16 e2e 的范围，本文件以合成小世界上的双机任务覆盖同一机制。）"""

from __future__ import annotations

import hashlib

import numpy as np
from harness import Sim

from awr.world.geometry.fake import fake_world_query

COR = {"polyline_enu_m": [[-150.0, -40.0], [-60.0, -40.0]], "offset_m": 15.0, "agl_m": 40.0, "terrain_follow": False,
       "speed_mps": 5.0}


def _run(tmp, seconds: float = 90.0) -> dict:
    sim = Sim(fake_world_query(tmp), n=2, spacing=20.0)
    try:
        sim.advance(1.0)                                  # 首步绑定时创建 plan-pool
        log: list[tuple] = []
        pool = sim.rt.pool
        orig = pool.submit

        def submit(req, cb):
            def wrap(res, tick, _cb=cb, _jid=req.job_id):
                log.append((_jid, res.status, res.result_sha256, int(tick)))
                return _cb(res, tick)
            return orig(req, wrap)

        pool.submit = submit
        eng = sim.rt.missions
        eng.create({"mission_id": "m-det", "generator": "corridor", "vehicle_ids": sim.ids[:2], "params": COR}, "operator")
        assert sim.until(lambda: eng.missions["m-det"].gen == "ok", 5.0, 0.1)
        assert eng.start("m-det")["code"] == 0
        sim.advance(seconds)
        h = hashlib.sha256()
        for k in sorted(sim.rt.tracker.cache._d):
            h.update(k.encode())
            h.update(np.ascontiguousarray(sim.rt.tracker.cache._d[k].Q).tobytes())
        tracks = [(round(t, 3), d["vehicle_id"], d["to"], d.get("reason")) for t, _, d in sim.kinds("track.state")]
        pos = {v: sim.pos(v).round(6).tolist() for v in sim.ids[:2]}
        return {"plans": log, "traj": h.hexdigest(), "tracks": tracks, "pos": pos}
    finally:
        sim.close()


def test_same_inputs_same_plans(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _run(tmp_path / "a")
    b = _run(tmp_path / "b")
    assert len(a["plans"]) >= 3 and all(len(x[2]) == 64 for x in a["plans"] if x[1] in ("ok", "degraded"))
    assert a["plans"] == b["plans"]                       # result_sha256 与 apply_tick 相同
    assert a["traj"] == b["traj"]                         # 轨迹控制点逐字节相同
    assert a["tracks"] == b["tracks"]
    assert a["pos"] == b["pos"]

