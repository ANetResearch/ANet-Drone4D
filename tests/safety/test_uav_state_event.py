"""M09-AC-035：`uav.state` 事件——一次完整飞行（arm → takeoff → goto → rtl → 上锁）中每次 fs/sub 变化恰好一条事件，`from`、`to`、
level 符合 §6.13；批量 RTL 时同一 tick 的全部转移只 put 一次（`evt/sim-core/sim` 与 `evt/sim-core/safety` 各至多一次/迭代）。"""

from __future__ import annotations

from safelib import Harness

from awr.contracts.enums import FLIGHTSTATE_NAMES, sub_name
from awr.runtime.events import category_of

LEVEL = {"RTL": 2, "HOLD": 2, "CORRECTING": 2, "ELAND": 3, "FAILSAFE": 3, "CRASHED": 3}


def test_full_flight_state_events() -> None:
    h = Harness(limits="px4_default")
    try:
        h.ready()
        h.gcs_age_ms = 0
        sb = h.S.blocks["safety"]
        s = h.slot()
        seen: list[str] = []
        n0 = len(h.states())

        def sample() -> None:
            f, u = int(sb["fs"][s]), int(sb["sub"][s])
            lab = f"{FLIGHTSTATE_NAMES[f]}/{sub_name(f, u)}"
            if not seen or seen[-1] != lab:
                seen.append(lab)

        sample()
        steps = [("arm", {}, 3.0), ("takeoff", {"alt_m": 4.0}, 15.0), ("goto", {"pos": [5.0, 3.0, 6.0], "route": "direct"}, 10.0),
                 ("rtl", {"alt_m": 10.0}, 60.0)]
        for op, args, dur in steps:
            assert h.cmd(op, args, cid=f"c-{op}")["status"] == "accepted", op
            t_end = h.core.clock.t_ns + int(dur * 1e9)
            while h.core.clock.t_ns < t_end:
                h.step(1)
                sample()
                c = h.call(f"c-{op}")
                if c is not None and c.final and op != "arm":
                    break
            assert h.call(f"c-{op}").status == "succeeded", (op, h.state())
        evs = [e for e in h.events if e.get("kind") == "uav.state"][n0:]
        tos = [e["to"] for e in evs]
        assert tos == seen[1:], (tos, seen)  # 每次变化恰好一条
        for prev, e in zip([seen[0], *tos[:-1]], evs, strict=True):
            assert e["from"] == prev
            assert e["level"] == LEVEL.get(e["to"].split("/")[0], 0) or (e["to"].startswith("LANDING") and e["level"] == 0)
        assert seen[-1] == "DISARMED/READY_TO_ARM"
        assert "RTL/CLIMB" in seen and "LANDED/SETTLING" in seen and "PREFLIGHT/CHECKING" in seen
    finally:
        h.close()


def test_batch_rtl_single_put_per_category() -> None:
    h = Harness(n=12, spacing=12.0)
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff_all(6.0)
        puts: list[dict[str, int]] = []
        pub = h.core.events
        orig = pub.flush

        def flush() -> int:
            per: dict[str, int] = {}
            for cat, evs in pub._batches.items():
                per[cat] = len(evs)
            puts.append(per)
            return orig()

        pub.flush = flush  # type: ignore[method-assign]
        rep = h.cmd("rtl", {}, uav="*", cid="batch-rtl")
        assert rep["status"] == "accepted" and len(rep["per_uav"]["accepted"]) == 12, rep
        h.step(3)
        sims = [p.get("sim", 0) for p in puts]
        # 12 架同一 tick 进入 RTL：12 条 uav.state 在同一次 flush（每个 category 每轮至多一次 put）
        assert max(sims) >= 12, sims
        assert category_of("uav.state") == "sim" and category_of("safety.battery") == "safety"
    finally:
        h.close()
