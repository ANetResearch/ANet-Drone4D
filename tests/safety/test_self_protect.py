"""M09-AC-030：向 guard 注入异常 → 发 `HLT.SAFETY.STAGE_ERROR`（critical）；连续 3 次后空中机体 HOLD、置 SAFETY_DEGRADED；
sim-core 不退出（主循环继续推进）。"""

from __future__ import annotations

from safelib import Harness


def test_stage_exception_self_protection() -> None:
    h = Harness()
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(8.0)
        calls = {"n": 0}

        def boom(ctx) -> None:
            calls["n"] += 1
            raise RuntimeError("injected")

        h.rt.guards["guard"].fn = boom
        t0 = h.core.clock.t_ns
        h.advance(0.2)
        assert h.core.clock.t_ns > t0 and calls["n"] >= 3
        codes = h.codes(None)
        assert codes.count("HLT.SAFETY.STAGE_ERROR") >= 3
        assert h.fs() == ("HOLD", "OTHER")
        cond = int(h.S.blocks["safety"]["cond"][h.slot()])
        assert cond & (1 << 20)  # SAFETY_DEGRADED
        assert bool(h.S.blocks["safety"]["flag_alert"][h.slot()])
        ev = next(e for e in h.events if e.get("code") == "HLT.SAFETY.STAGE_ERROR")
        assert ev["kind"] == "safety.health" and ev["cls"] == "critical"
    finally:
        h.close()
