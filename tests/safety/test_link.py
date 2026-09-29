"""M09-AC-018：GCS 链路阶梯（DEGRADED 1.5 ± 0.1 s、HOLD 3.0 ± 0.1 s、RTL 13 ± 0.1 s），停发信标（api 不可达）同样按此时刻；
断线 1 s 后暂停 60 s（信标照常到达且 ping_age_ms 持续增长）→ 恢复后年龄从 1 s 继续（第 0.5 s 进入 DEGRADED，不立即 RTL）；
×10 下 2 Hz ping 零误判；恢复后 FLYING/HOVER（auto_resume）。"""

from __future__ import annotations

import numpy as np
from safelib import Harness

from awr.sim.fleet.pipeline import TICK_NS


def _flying(h: Harness) -> None:
    h.ready()
    h.gcs_age_ms = 0
    h.takeoff(10.0)
    h.advance(0.3)


def _first(h: Harness, pred, timeout_s: float) -> float:
    t0 = h.core.clock.t_ns
    assert h.until(pred, timeout_s, step_s=0.02), h.state()
    return (h.core.clock.t_ns - t0) * 1e-9


def test_ladder_growing_ping_age_and_restore() -> None:
    h = Harness()
    try:
        _flying(h)
        # 持有者断线：信标照常 5 Hz，ping_age_ms 按墙钟增长
        t_lost = h.W[0]

        def age() -> None:
            h.gcs_age_ms = int((h.W[0] - t_lost) // 1_000_000)

        orig = h._beacon

        def beacon() -> None:
            age()
            orig()

        h._beacon = beacon  # type: ignore[method-assign]
        h._last_gcs = 0
        sb = h.S.blocks["safety"]
        s = h.slot()
        t1 = _first(h, lambda: not sb["flag_gcs"][s], 3.0)
        assert abs(t1 - 1.5) <= 0.1 + 0.2, t1  # 信标 5 Hz：判据以最近一次信标为准（≤ 200 ms 量化）
        t2 = t1 + _first(h, lambda: h.fs()[0] == "HOLD", 3.0)
        assert h.fs() == ("HOLD", "LINK_LOSS")
        assert abs(t2 - 3.0) <= 0.1 + 0.2, t2
        # 恢复：新 ping（年龄 0）→ < 1.5 s → FLYING/HOVER
        h._beacon = orig  # type: ignore[method-assign]
        h.gcs_age_ms = 0
        h._last_gcs = 0
        assert h.until(lambda: h.fs() == ("FLYING", "HOVER"), 1.0)
        codes = h.codes()
        assert codes[:3] == ["SAF.LINK.DEGRADED", "SAF.LINK.LOST_HOLD", "SAF.LINK.RESTORED"], codes
    finally:
        h.close()


def test_ladder_no_beacon_to_rtl() -> None:
    """api 不可达：不再收到信标，年龄随链路时钟自然增长（精度 ±0.1 s）。"""
    h = Harness()
    try:
        _flying(h)
        h.gcs_age_ms = None
        t_last = h._last_gcs
        sb = h.S.blocks["safety"]
        s = h.slot()

        def since() -> float:
            return (h.W[0] - t_last) * 1e-9

        assert h.until(lambda: not sb["flag_gcs"][s], 3.0, step_s=0.004)
        assert abs(since() - 1.5) <= 0.1, since()
        assert h.until(lambda: h.fs()[0] == "HOLD", 3.0, step_s=0.004)
        assert abs(since() - 3.0) <= 0.1, since()
        assert h.until(lambda: h.fs()[0] == "RTL", 12.0, step_s=0.004)
        assert abs(since() - 13.0) <= 0.1, since()
        assert bool(sb["fs_auto"][s]) and "SAF.LINK.LOST_RTL" in h.codes()
    finally:
        h.close()


def test_pause_freezes_age() -> None:
    h = Harness()
    try:
        _flying(h)
        clk = h.core.clock
        t_lost = h.W[0]
        h.gcs_age_ms = None
        sb = h.S.blocks["safety"]
        s = h.slot()
        h.advance(1.0)
        clk.apply("pause")
        # 暂停 60 s【墙钟】：信标照常到达，ping 年龄持续增长
        for _ in range(60 * 5):
            h.W[0] += 200_000_000
            h.svc.on_gcs_beacon("p-op", "HELD", int((h.W[0] - t_lost) // 1_000_000), clk.wall_mono_ns(),
                                clk.paused_total_ns())
            h.core.iterate()
        clk.apply("play")
        age = int(h.rt.link.age_ms_of(np.array([s]))[0])
        assert 1000 <= age <= 1250, age  # 断线前最后一次信标距断线 ≤ 200 ms；暂停 60 s 不计入
        t0 = h.W[0]
        assert h.until(lambda: not sb["flag_gcs"][s], 2.0, step_s=0.004)
        assert abs((h.W[0] - t0) * 1e-9 - (1.5 - age / 1000.0)) <= 0.1
        assert h.fs() == ("FLYING", "HOVER")  # 不立即 HOLD 或 RTL
    finally:
        h.close()


def test_rate_x10_no_false_trigger() -> None:
    h = Harness()
    try:
        _flying(h)
        h.core.clock.apply("speed", {"rate": 10.0})
        sb = h.S.blocks["safety"]
        s = h.slot()
        bad = 0
        t_end = h.core.clock.t_ns + int(60e9)
        while h.core.clock.t_ns < t_end:
            h.W[0] += 5 * TICK_NS
            # 客户端 2 Hz ping：年龄在 0–500 ms 间锯齿变化（墙钟）
            h.svc.on_gcs_beacon("p-op", "HELD", int((h.W[0] // 1_000_000) % 500), h.core.clock.wall_mono_ns(),
                                h.core.clock.paused_total_ns())
            h.core.iterate()
            bad += int(not sb["flag_gcs"][s])
        assert bad == 0 and h.fs()[0] == "FLYING"
    finally:
        h.close()
