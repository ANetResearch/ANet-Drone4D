"""TargetLite32 打包核与 numpy 实现逐字节相同（M08-FR-217；D2-FX-R2-sim 第 2 次，`tap_targets._pack_fast`）。

随机列值（含速度饱和、警觉度越界、位段超宽、空洞行）下 `_pack_fast` 与 `_pack_np` 的输出逐字节相同；缺列时核以零列代替，
与 numpy 实现跳过该列的结果相同。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from awr.sim.fleet.stages import registry as R
from awr.sim.fleet.stages import tap_targets as TT

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "fake"))
import targets_stub as TS  # noqa: E402

pytestmark = pytest.mark.skipif(not TT._NB or not TT._BITS_OK, reason="numba 不可用或位段布局与核不一致")


def _fill(tab, rng: np.random.Generator) -> None:
    for name, c in tab.cols.items():
        if name in ("active", "target_no"):
            continue
        if c.dtype.kind == "f":
            c[:] = rng.normal(0.0, 3.0, c.shape) * rng.choice([1.0, 10.0, 1e3], c.shape)
        elif c.dtype.kind in "ui":
            info = np.iinfo(c.dtype)
            c[:] = rng.integers(max(info.min, -1000), min(info.max, 1000) + 1, c.shape).astype(c.dtype)
        elif c.dtype.kind == "b":
            c[:] = rng.random(c.shape) < 0.5
    if "tgt_speed" in tab.cols:
        tab.cols["tgt_speed"][:4] = (400.0, -400.0, 0.005, -0.005)[: min(4, len(tab.cols["tgt_speed"]))]
    if "tgt_alert" in tab.cols:
        tab.cols["tgt_alert"][:3] = (-0.2, 1.7, 0.5)[: min(3, len(tab.cols["tgt_alert"]))]


@pytest.mark.parametrize("drop", [None, "tgt_speed", "tgt_alert"])
def test_pack_fast_matches_numpy(drop: str | None) -> None:
    from awr.sim.core.entities import EntityTable

    rng = np.random.default_rng(5 if drop is None else len(drop))
    with R.isolated_registry() as reg:
        R.register_entity_block("target", "targets", "M18", TS.M18_FIELDS)
        R.register_entity_block("target", "perception", "M19", TS.M19_FIELDS)
        R.register_entity_block("target", "relay", "M20", TS.M20_FIELDS)
        tab = EntityTable("target", reg.entity_blocks["target"])
        ids = [tab.add(model=("person", "dog", "vehicle")[k % 3]).id for k in range(24)]
        for eid in (ids[3], ids[10]):            # 空洞行
            assert tab.remove(eid) is not None
        if drop is not None:
            tab.cols.pop(drop, None)
        for _ in range(20):
            _fill(tab, rng)
            rows = tab.rows()
            a = TT._pack_fast(tab, rows)
            assert a is not None
            assert a.tobytes() == TT._pack_np(tab, rows).tobytes()
        assert TT.pack_rows(tab).tobytes() == TT._pack_np(tab, tab.rows()).tobytes()
