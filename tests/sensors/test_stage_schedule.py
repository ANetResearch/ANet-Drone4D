"""M13-AC-005：sensors stage 相位调度（tick 0–199 执行日志 golden）；M13-FR-010。"""

from __future__ import annotations


def test_stage_registration(bench):
    st = bench.stage
    assert (st.every, st.phase, st.order, st.owner, st.budget_core) == (5, 2, 100, "M13", 0.010)
    assert "sensors" in bench.reg.blocks and {t.name for t in bench.reg.slow} == {"m13.sensor_pose", "m13.noisy_obs"}


def test_schedule_ticks_0_to_199(bench):
    b = bench
    b.rt.trace = []
    for s in range(10):
        b.spawn(s, s + 1)
    b.run(200 / 250)
    tr = b.rt.trace
    assert [t for t, *_ in tr] == [t for t in range(200) if t % 5 == 2]  # 只在 tick ≡ 2 (mod 5)
    for tick, c, gp, ip, det in tr:
        assert c == (tick - 2) // 5 and gp == c % 5
        assert (ip is not None) == (c % 5 == 2)
        if ip is not None:
            assert ip == (c // 5) % 10
        assert det is False  # 目标表为空时检测不运行
    # GNSS 按 slot % 5 轮转：每个 slot 每 5 次调用推进一次（10 Hz）
    assert b.rt.gnss.stats["steps"] == len(tr)
    b.rt.detector.spawn("t1", [0, 0, 0], "person")
    b.rt.trace = []
    b.run(0.4)
    dets = [c for _t, c, _g, _i, d in b.rt.trace if d]
    assert dets and all(c % 10 == 4 for c in dets)
