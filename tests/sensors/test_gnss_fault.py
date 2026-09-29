"""M13-AC-016：gnss_denied 故障联动（≤ 0.1 s 进 NO_FIX；解除后 +1 s、+11 s、+41 s；每次变化 1 条事件；不写 LOC 标志）。"""

from __future__ import annotations

import numpy as np

from awr.sim.fleet.stages.registry import StateBlockSpec


def test_denied_and_convergence(bench_factory):
    b = bench_factory()
    # M09 未装配时自带一个只含 fault_mask 的 safety 块，模拟注入状态（只读）
    b.S.add_block(StateBlockSpec("safety", "M09", {"fault_mask": (np.dtype(np.uint8), ()), "flag_loc_ok": (np.dtype(bool), ())}))
    sb = b.S.blocks["safety"]
    b.spawn(0, 1)
    b.run(1.0)
    blk = b.S.blocks["sensors"]
    assert blk["gn_fix"][0] == 4
    t_inj = b.tick
    sb["fault_mask"][0] |= 16
    b.run(0.1)
    assert blk["gn_fix"][0] == 0 and blk["gn_sats"][0] == 0
    ev = b.events.of("sensor.gnss_fix")
    assert len(ev) == 1 and ev[0]["data"]["to"] == "NO_FIX" and ev[0]["severity"] == 2 and ev[0]["data"]["reason"] == "fault"
    assert (ev[0]["t_sim_ns"] - t_inj * 4_000_000) <= 100_000_000
    b.run(5.0)  # 注入期间保持 NO_FIX
    assert blk["gn_fix"][0] == 0
    sb["fault_mask"][0] &= ~np.uint8(16)
    t_clr = b.tick * 4_000_000
    b.run(45.0)
    ev = b.events.of("sensor.gnss_fix")
    seq = [(e["data"]["from"], e["data"]["to"], (e["t_sim_ns"] - t_clr) / 1e9) for e in ev[1:]]
    assert [s[:2] for s in seq] == [("NO_FIX", "SINGLE"), ("SINGLE", "RTK_FLOAT"), ("RTK_FLOAT", "RTK_FIXED")]
    for (_f, _t, dt), want in zip(seq, (1.0, 11.0, 41.0), strict=True):
        assert abs(dt - want) <= 0.1 + 1e-9
    assert blk["gn_fix"][0] == 4 and not sb["flag_loc_ok"].any()  # LOC 标志只由 M09 写
    st = b.events.of("sensor.state")
    assert [e["data"]["to"] for e in st] == ["DEGRADED", "ACTIVE"]
