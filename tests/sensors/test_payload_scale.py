"""`m13.payload_pub` 的规模口径（D2-FX-R2-other；D2 验收第 1 轮 D2-AC-26 → D1-AC-07、08、27）。

- D2 机体不超过 `PAYLOAD_ALL_MAX`（12，17 §17.7.2 "全部机体 ≤ 12"）时为全部 D2 机体并兴趣集（行为不变）；
- 超过时只为兴趣集生成（共享展示与 D1 阶梯的 P600 自 D2 起为对象形式，N = 1000 时此前每 500 ms 在同一轮内构建 1000 条消息）；
- 每轮至多构建 `PAYLOAD_SLICE` 条消息，周期构建完成后一次发布全集消息，头部 `t_sim_ns` 为周期开始时刻（2 Hz 不变）；
- 仿真时间回退（重置）后继续按 2 Hz 发布。
"""

from __future__ import annotations

import msgpack
import numpy as np

from awr.sim.sensors import nav_pub as NP


def _spawn(b, n: int) -> None:
    for i in range(n):
        b.spawn(i, 10 + i, "p600_mid360", vid=f"u{i}", pos=(10.0 * i, 0.0, 50.0))
    b.run(0.04)


def test_large_fleet_is_interest_controlled(bench_d2_factory):
    b = bench_d2_factory(sandbox=False, capacity=64)
    got: list[dict] = []
    b.rt.payload_pub.sink = lambda raw: got.append(msgpack.unpackb(raw, raw=False))
    _spawn(b, NP.PAYLOAD_ALL_MAX + 8)
    got.clear()
    b.run(2.0)
    assert got == []  # 20 架 D2 机体、无兴趣集：不生成
    assert b.rt.payload_pub.stats["interest_only"] >= 3
    b.interest([12, 15])
    b.run(2.0)
    assert got and all(sorted(m["items"]) == ["u2", "u5"] for m in got)


def test_small_fleet_still_publishes_every_d2_vehicle(bench_d2_factory):
    b = bench_d2_factory(sandbox=True, capacity=64)
    got: list[dict] = []
    b.rt.payload_pub.sink = lambda raw: got.append(msgpack.unpackb(raw, raw=False))
    _spawn(b, NP.PAYLOAD_ALL_MAX)
    got.clear()
    b.run(5.0)
    assert 9 <= len(got) <= 11
    want = sorted(f"u{i}" for i in range(NP.PAYLOAD_ALL_MAX))
    assert all(sorted(m["items"]) == want for m in got)
    assert all(d == NP.PAYLOAD_PERIOD_NS for d in np.diff([m["t_sim_ns"] for m in got]))


def test_build_is_sliced_per_round(bench_d2_factory, monkeypatch):
    """每轮至多 PAYLOAD_SLICE 次逐机构建；全集消息在最后一片之后发布一次。"""
    b = bench_d2_factory(sandbox=True, capacity=64)
    _spawn(b, NP.PAYLOAD_ALL_MAX)
    pp = b.rt.payload_pub
    calls: list[int] = []
    orig = b.rt.payload.message

    def counted(s, t):
        calls.append(int(t))
        return orig(s, t)

    monkeypatch.setattr(b.rt.payload, "message", counted)
    sent: list[bytes] = []
    pp.sink = sent.append
    b.slow_d2 = False  # 手动驱动慢任务
    pp.next_due = 0
    pp._queue = None
    rounds = 0
    while not sent:
        n0 = len(calls)
        pp.run_slow(b.ctx)
        assert len(calls) - n0 <= NP.PAYLOAD_SLICE
        rounds += 1
        b.step()
        assert rounds < 50
    assert rounds == -(-NP.PAYLOAD_ALL_MAX // NP.PAYLOAD_SLICE)
    m = msgpack.unpackb(sent[0], raw=False)
    assert len(m["items"]) == NP.PAYLOAD_ALL_MAX
    assert m["t_sim_ns"] == min(calls)  # 头部为周期开始时刻
    assert {it["t_sim_ns"] for it in m["items"].values()} <= set(calls)


def test_publishes_again_after_time_goes_back(bench_d2_factory):
    b = bench_d2_factory(sandbox=True, capacity=16)
    got: list[dict] = []
    b.rt.payload_pub.sink = lambda raw: got.append(msgpack.unpackb(raw, raw=False))
    _spawn(b, 3)
    b.run(3.0)
    assert got
    got.clear()
    b.tick = 0  # sim/reset：仿真时间回到 0
    b.run(1.2)
    assert len(got) >= 2 and got[0]["t_sim_ns"] == 0


def test_pose_and_nav_publish_again_after_time_goes_back(bench_d2_factory):
    """SensorPose48（10 Hz）与 `uav/{id}/nav`（10 Hz）同样在仿真时间回退后立即重新对齐（D2-INT，d2-FX-R2-other-to-M13 第 5 条：
    此前 `next_due` 不回退，循环剧本 S0 每轮重置后视锥位姿停发一整轮）。"""
    b = bench_d2_factory(sandbox=True, capacity=16)
    _spawn(b, 3)
    b.interest([10, 11, 12])
    poses: list[int] = []
    b.rt.packer.sink = lambda raw: poses.append(b.tick)
    b.rt.nav.override = [10, 11, 12]
    b.run(3.0)
    assert poses
    r0 = b.rt.nav.stats["rounds"]
    assert r0 >= 20
    poses.clear()
    b.tick = 0  # sim/reset：仿真时间回到 0
    b.run(1.0)
    assert len(poses) >= 5                      # 1 s 内照常 10 Hz（此前要等到 3 s 才恢复）
    assert b.rt.nav.stats["rounds"] - r0 >= 8
