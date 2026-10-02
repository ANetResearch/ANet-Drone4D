"""state_ext 直接编码（`SimCore._ext_rows_packed`，FX2-R3，ADR-070）与字典装配后整行编码等价。

M09（battery、link、gcs_loss_policy）与 M13（loc、sens）装配，部分机体起飞、环绕一段时间后，对全部机体比较：直接编码的
字节解码后与 `[agent_no, _ext_rows 的字典]` 逐键相等；另以登记的测试钩子写入基本键之外的字段，验证回退路径同样相等。"""

from __future__ import annotations

import msgpack
from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R


def _check(h: CoreHarness) -> int:
    core = h.core
    slots = core.roster.slots_in_order()
    agents, packed = core._ext_rows_packed(slots)
    rows = core._ext_rows(slots)
    assert agents == [r[0] for r in rows]
    for b, r in zip(packed, rows, strict=True):
        assert msgpack.unpackb(b, raw=False) == msgpack.unpackb(msgpack.packb(r, use_bin_type=True), raw=False)
    return len(rows)


def test_packed_rows_equal_dict_rows() -> None:
    import awr.sim.safety as SAF
    from awr.sim.sensors import plugin

    with R.isolated_registry() as reg:
        SAF.install(metrics=False)
        plugin.install()
        h = CoreHarness(n=12, reg=reg, spacing=12.0)
        try:
            h.advance(2.0)
            ids = h.ids()
            for k, v in enumerate(ids[:8]):
                h.cmd("takeoff", {"alt_m": 15.0 + k}, uav=v)
            h.advance(12.0)
            for k, v in enumerate(ids[:4]):
                h.cmd("orbit", {"center": [12.0 * k, 10.0, 16.0], "radius_m": 4.0, "turns": 0, "speed_mps": 2.0}, uav=v)
            h.advance(5.0)
            assert _check(h) == 12
            row = h.core._ext_rows(h.core.roster.slots_in_order()[:1])[0][1]
            assert "sens" in row and row["loc"].get("gnss_fix") is not None and row["battery"] is not None
        finally:
            plugin.uninstall()
            h.close()


def test_packed_rows_fallback_with_extra_hook_keys() -> None:
    with R.isolated_registry() as reg:
        def hook(slots, t_ns, objs):
            for o in objs:
                o["mission"] = {"id": "m-x"}
                o["extra_k"] = 1

        R.register_state_ext_hook(hook, owner="TEST")
        h = CoreHarness(n=3, reg=reg, spacing=12.0)
        try:
            h.advance(1.0)
            assert _check(h) == 3
        finally:
            h.close()
