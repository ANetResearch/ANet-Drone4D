"""计数口径（M18-AC-013；M18-FR-015；AWR-04 §9.5）：两架机先后进入同一识别物配置体：同一轮次计 2 次、各发一条事件（episode
相同）；同一架机在同一轮次内不重复计数；轮次结束（回到 UNAWARE）后重新计数（新 episode）。TargetLite32 的 discovered_n 与
detail 的 discovered_count 一致。"""

from __future__ import annotations

from tlib import Bench, FakeWorld, static_person

SPHERE = [{"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 100.0}]
CALM = {"onset": {"kind": "none"}, "response": [{"kind": "ignore"}], "alert_others": False}


def test_two_uavs_same_episode_then_new_episode() -> None:
    with Bench(2, FakeWorld()) as b:
        tid = b.add(static_person(alertness=0.5, sensitivity=SPHERE, reaction=CALM, calm_s=10.0))
        b.S.fly(0, [40.0, 0.0, 30.0])
        b.S.fly(1, [400.0, 0.0, 30.0])
        b.run(3.0)
        b.S.pos[1] = [0.0, 40.0, 30.0]   # 第二架后进入
        b.run(3.0)
        b.S.pos[0] = [40.0, 5.0, 30.0]   # 第一架仍在体内：不重复计数
        b.run(3.0)
        ev = b.events.of("target.discovered")
        assert [(e["data"]["uav"], e["data"]["episode"], e["data"]["count"]) for e in ev] == [("uav-01", 1, 1), ("uav-02", 1, 2)]
        T = b.T(tid)
        assert T.disc_n == 2 and b.col("tgt_disc_n", tid) == 2 and b.col("tgt_episode", tid) == 1
        b.S.pos[0] = [500.0, 0.0, 30.0]
        b.S.pos[1] = [500.0, 50.0, 30.0]
        b.run(11.0)
        assert T.aware == 0 and T.disc_set == set()
        b.S.pos[0] = [40.0, 0.0, 30.0]
        b.run(3.0)
        ev = b.events.of("target.discovered")
        assert (ev[-1]["data"]["uav"], ev[-1]["data"]["episode"], ev[-1]["data"]["count"]) == ("uav-01", 2, 3)
        det = b.rt.detail(T, b.ctx.t_ns, b.rt.env(b.ctx))
        assert det["discovered_count"] == 3 and det["episode"] == 2 and det["last_discovered"]["uav"] == "uav-01"
