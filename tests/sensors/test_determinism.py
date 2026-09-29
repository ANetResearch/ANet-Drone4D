"""M13-AC-013（后半）、G-M13-2：S3 在"无人订阅"与"全部订阅"两种兴趣集下，sensors 块与 sensor.detect 逐字节一致；
同一输入两次运行一致（重仿真的前提）；剧本事件桥（scenario.event target.spawn）与直接调用结果一致。"""

from __future__ import annotations

import json

from m13_s3lib import run_s3, setup_s3


def run(b, interest):
    b.interest(interest)
    setup_s3(b)
    ev = run_s3(b, 30.0)
    return b.block_bytes(), json.dumps([(e["t_sim_ns"], e["data"]) for e in ev], sort_keys=True), b.rt.detector.targets.checkpoint()


def test_interest_set_does_not_change_results(bench_factory):
    a = run(bench_factory(seed=9), [])
    b = run(bench_factory(seed=9), [1, 2, 3])
    c = run(bench_factory(seed=9), [2])
    assert a == b == c
    assert '"state": "confirmed"' in a[1]


def test_scenario_event_bridge(bench_factory):
    b = bench_factory(seed=9)
    setup_s3(b, targets=False)
    b.events.emit("scenario.event", t_sim_ns=b.tick * 4_000_000, severity=1,
                  fields={"event_id": "e1", "action": "target.spawn",
                          "args": {"target_id": "t1", "pos_enu_m": [40.0, -20.0, 0.0], "kind": "person", "conf_first": 0.42,
                                   "conf_confirm": 0.9}})
    b.run(0.25)
    assert b.rt.detector.targets.items()[0]["target_id"] == "t1" and b.rt.stats["bridged_targets"] == 1
    b.events.emit("scenario.event", t_sim_ns=b.tick * 4_000_000, severity=1,
                  fields={"event_id": "e2", "action": "target.spawn", "args": {"target_id": "t1", "pos_enu_m": [0, 0, 0]}})
    b.run(0.25)
    assert b.rt.detector.targets.n == 1  # 重复 id 忽略
    ev = run_s3(b, 20.0)
    assert ev and ev[0]["data"]["conf"] == 0.42
