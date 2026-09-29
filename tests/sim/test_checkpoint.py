"""checkpoint 接线（M08-AC-035；M08-FR-083；D1-AC-11b；M11 CheckpointStore）。

运行 10 s【仿真】（起飞、导航、PATH、带随机湍流的 env 桩）后 checkpoint，经 M11 `CheckpointStore` 序列化写盘、读回后装入
新启动的 SimCore，再运行 10 s；与不中断运行的同一段 Full64 帧逐字节一致（同一内核），在途调用、租约、幂等表与 RNG 状态
随之恢复。主循环内拷贝 ≤ 1 ms 属 perf 用例（并行阶段不跑）。
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts import LAYOUT_ID
from awr.runtime.checkpoint import CheckpointStore
from awr.runtime.statering import LocalRing
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.ckpt import SimCheckpointer, capture, restore


def _env(S, ctx) -> None:
    idx = S.active_idx()
    if idx.size:
        S.set_wind_from_enu(np.array([3.0, 1.0, 0.0]) + ctx.rng["dryden"].normal(0, 1.0, (idx.size, 3)), idx)


class Rec:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.reader = None

    def __call__(self, S, ctx) -> None:
        if self.reader is None:
            return
        f = self.reader.read_latest(0)
        if f is not None and (not self.frames or f.t_sim_ns != self.last_t):
            self.last_t = f.t_sim_ns
            self.frames.append(f.full)


def _core(reg, rec: Rec) -> CoreHarness:
    h = CoreHarness(n=2, reg=reg, spacing=20.0)
    rec.reader = LocalRing.attach(h.path, expect_layout_id=LAYOUT_ID)
    return h


def _run_until(h: CoreHarness, t_end_tick: int) -> None:
    while h.core.clock.tick < t_end_tick:
        h.W[0] += 2 * TICK_NS
        h.core.iterate()


def _script(h: CoreHarness) -> None:
    a, b = h.ids()
    h.takeoff(10.0, a)
    pa, pb = h.pos(a), h.pos(b)
    assert h.cmd("takeoff", {"alt_m": 12.0}, uav=b, cid="to-b")["status"] == "accepted"
    assert h.cmd("goto", {"pos": [pa[0] + 80.0, pa[1] + 20.0, 15.0]}, uav=a, cid="g-a")["status"] == "accepted"
    c = h.call("to-b")
    assert h.until(lambda: c.final, 30.0) and c.status == "succeeded"
    wp = [[pb[0] + 10.0, pb[1] + 30.0, 12.0], [pb[0] + 40.0, pb[1] + 30.0, 14.0], [pb[0] + 40.0, pb[1] - 10.0, 12.0]]
    assert h.cmd("follow_path", {"waypoints": wp}, uav=b, cid="fp-b")["status"] == "accepted"


def test_checkpoint_restore_matches_uninterrupted(tmp_path) -> None:
    with R.isolated_registry() as reg:
        R.register_stage("env", 5, 0, 20, owner="M07")(_env)
        rec = Rec()
        R.register_stage("ck_probe", 2, 0, 146, owner="M08", budget_core=0.0)(rec)
        h1 = _core(reg, rec)
        try:
            _script(h1)
            _run_until(h1, h1.core.clock.tick + 2500)  # 再 10 s
            store = CheckpointStore(tmp_path / "ckpt", layout_id=LAYOUT_ID)
            ck = SimCheckpointer(store)
            t0 = time.perf_counter()
            assert ck.save(h1.core, force=True)
            assert (time.perf_counter() - t0) < 0.5
            assert store.flush(5.0)
            tick_ck = h1.core.clock.tick
            n_before = len(rec.frames)
            _run_until(h1, tick_ck + 2500)
            ref = rec.frames[n_before:]
            store.close(final=True)
            ref_calls = {c.cid: c.status for c in h1.core.engine.table.calls()}
        finally:
            h1.close()
        rec2 = Rec()
        with R.isolated_registry() as reg2:
            R.register_stage("env", 5, 0, 20, owner="M07")(_env)
            R.register_stage("ck_probe", 2, 0, 146, owner="M08", budget_core=0.0)(rec2)
            h2 = _core(reg2, rec2)
            try:
                loaded = CheckpointStore(tmp_path / "ckpt", layout_id=LAYOUT_ID).load_latest()
                assert loaded is not None and loaded.t_sim_ns == tick_ck * TICK_NS
                restore(h2.core, loaded.arrays, loaded.meta)
                assert h2.core.clock.tick == tick_ck
                assert h2.core.lease.seat.holder == "p-op"
                assert {"g-a", "fp-b"} <= set(h2.core.engine.idem)
                rec2.frames.clear()
                _run_until(h2, tick_ck + 2500)
                got = rec2.frames
                assert len(got) == len(ref) >= 1200
                bad = [k for k, (x, y) in enumerate(zip(got, ref, strict=True)) if x != y]
                assert not bad, f"首个不一致帧 {bad[0]} / {len(ref)}"
                assert {c.cid: c.status for c in h2.core.engine.table.calls()} == ref_calls
            finally:
                h2.close()


def test_capture_is_copy_safe() -> None:
    """capture 的数组经 CheckpointStore.save 拷贝；capture 本身返回可序列化的元数据。"""
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            h.takeoff(5.0)
            arrays, meta = capture(h.core)
            assert "p" in arrays and meta["clock"]["tick"] == h.core.clock.tick
            assert all(isinstance(v, np.ndarray) for v in arrays.values())
            import json

            json.dumps(meta, default=str)
        finally:
            h.close()


@pytest.mark.perf
def test_checkpoint_copy_under_1ms() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=200, reg=reg, spacing=12.0, ready=False)
        try:
            import tempfile

            from awr.runtime.checkpoint import CheckpointStore as CS

            with tempfile.TemporaryDirectory() as d:
                ck = SimCheckpointer(CS(__import__("pathlib").Path(d), layout_id=LAYOUT_ID))
                for _ in range(20):
                    ck.save(h.core, force=True)
                    ck.store.flush(5.0)
                assert float(np.percentile(ck.copy_ms, 99)) <= 1.0
                ck.close()
        finally:
            h.close()
