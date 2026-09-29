"""输入日志与重仿真（M08-AC-036；M08-FR-084；D1-AC-31；ADR-049）。

原运行（假墙钟单步驱动，带 RNG 湍流的 env 桩）把席位、命令写入 `inputs.msgpack`（awr.inputlog.v1）；`resim()` 按头重建
sim-core、在记录的 apply_tick 注入输入并逐 tick 推进：每一帧 Full64 与原运行逐字节一致；头中内核或版本与当前环境不一致时
拒绝（`355 RESIM_INCOMPATIBLE`）。
"""

from __future__ import annotations

import msgpack
import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts import LAYOUT_ID
from awr.contracts.reasons import Reason
from awr.runtime.statering import LocalRing
from awr.sim.fleet import kernels_l1 as KL
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.inputlog import SCHEMA, InputLog, inputlog_header, read_inputlog
from awr.sim.runtime.resim import ResimIncompatible, resim


def _env(S, ctx) -> None:
    idx = S.active_idx()
    if idx.size:
        S.set_wind_from_enu(np.array([2.0, -3.0, 0.0]) + ctx.rng["dryden"].normal(0, 1.0, (idx.size, 3)), idx)


def _record(tmp_path) -> dict[int, bytes]:
    frames: dict[int, bytes] = {}
    box: dict = {"on": False}

    def probe(S, ctx) -> None:
        if not box["on"]:
            return
        f = box["reader"].read_latest(0)
        if f is not None:
            frames[int(f.t_sim_ns)] = f.full

    R.register_stage("env", 5, 0, 20, owner="M07")(_env)
    R.register_stage("rs_probe", 2, 0, 147, owner="M08", budget_core=0.0)(probe)
    h = CoreHarness(n=2, reg=R.registry(), seat=False, ready=False, spacing=15.0, cfg_over={"world_seed": 11})
    try:
        il = InputLog(tmp_path / "inputs.msgpack", inputlog_header(h.core, h.run_id))
        h.core.inputlog = il
        h.core.engine.inputlog = il
        box["reader"] = LocalRing.attach(h.path, expect_layout_id=LAYOUT_ID)
        box["on"] = True
        h.seat()
        assert h.until(lambda: all(e.lifecycle == 4 for e in h.core.roster.by_slot.values()), 5.0)
        a, b = h.ids()
        assert h.cmd("takeoff", {"alt_m": 12.0}, uav="*", cid="to")["status"] == "accepted"
        h.advance(12.0)
        pa = h.pos(a)
        assert h.cmd("goto", {"pos": [pa[0] + 40.0, pa[1] + 10.0, 15.0]}, uav=a)["status"] == "accepted"
        assert h.cmd("orbit", {"center": [pa[0] + 15.0, pa[1] + 30.0, 12.0], "radius_m": 10.0, "turns": 1}, uav=b
                     )["status"] == "accepted"
        h.advance(10.0)
        assert h.cmd("rtl", {}, uav=a)["status"] == "accepted"
        h.advance(5.0)
        box["on"] = False
        il.close()
        box["end_tick"] = h.core.clock.tick
        return frames, box["end_tick"]
    finally:
        h.close()


def test_resim_bit_identical(tmp_path) -> None:
    with R.isolated_registry():
        frames, end_tick = _record(tmp_path)
        header, entries = read_inputlog(tmp_path / "inputs.msgpack")
        assert header["schema"] == SCHEMA and {e["kind"] for e in entries} >= {"lease", "cmd"}
        last = max(int(e["tick"]) for e in entries)
        raw: list[bytes] = []
        times: list[int] = []
        digests = resim(tmp_path, extra_ticks=end_tick - last, frames=raw, times=times)
    assert len(digests) == len(raw) == len(times)
    got = dict(zip(times, raw, strict=True))
    common = sorted(set(frames) & set(got))
    assert len(common) >= 0.95 * len(frames) and len(frames) > 3000
    bad = [t for t in common if frames[t] != got[t]]
    assert not bad, f"{len(bad)} 帧不一致，首个 t = {bad[0] * 1e-9:.3f} s"


def _rewrite_header(src, dst, **sim_over) -> None:
    header, entries = read_inputlog(src / "inputs.msgpack")
    header = dict(header)
    header["sim"] = dict(header["sim"], **sim_over)
    with open(dst / "inputs.msgpack", "wb") as f:
        f.write(msgpack.packb(header, use_bin_type=True))
        for e in entries:
            f.write(msgpack.packb(e, use_bin_type=True))


def test_resim_rejects_incompatible(tmp_path, monkeypatch) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with R.isolated_registry():
        h = CoreHarness(n=1, reg=R.registry(), seat=False, ready=False, kernel="numba")
        try:
            il = InputLog(src / "inputs.msgpack", inputlog_header(h.core, h.run_id))
            h.core.inputlog = il
            h.core.engine.inputlog = il
            h.seat()
            il.close()
        finally:
            h.close()
    for name, over in (("ver", {"version": "0.0.0-other"}), ("cfg", {"fleet_config": {"no_such_field": 1}})):
        d = tmp_path / name
        d.mkdir()
        _rewrite_header(src, d, **over)
        with pytest.raises(ResimIncompatible) as ei:
            resim(d)
        assert ei.value.code == int(Reason.RESIM_INCOMPATIBLE) == 355
    d = tmp_path / "kern"
    d.mkdir()
    _rewrite_header(src, d, kernel="numba")
    with pytest.raises(ResimIncompatible):
        resim(d, kernel="numpy")  # 原运行 numba、当前强制 numpy（AWR_KERNEL=numpy）
    monkeypatch.setattr(KL, "HAVE_NUMBA", False)
    with pytest.raises(ResimIncompatible):
        resim(d)  # 原运行 numba、当前 numba 不可用
