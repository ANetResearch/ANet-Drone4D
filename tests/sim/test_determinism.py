"""确定性（M08-AC-019；M08-FR-082；ADR-049）。

S1 剧本（M16）与世界尚未装配时，以等价的缩减剧本验证同一机制：3 架机、固定 tick 注入的命令序列（takeoff、goto、orbit、
follow_path、rtl），M07 env 桩 stage 从 `ctx.rng["dryden"]`（`PCG64(SeedSequence([world_seed, stream]))`）抽样湍流并经
`set_wind_from_enu` 写风。前 60 s【仿真】：
- 同一种子、同一输入运行两次，StateRing 发布的 Full64 全部帧逐字节一致（numba 与 numpy 各测一次）；
- 改变 `world_seed` 时湍流相关字段（位置、速度、姿态）不同。
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts import LAYOUT_ID
from awr.runtime.statering import LocalRing
from awr.sim.fleet import kernels_l1 as KL
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R

T_END_S = 60.0


def _env(S, ctx) -> None:
    idx = S.active_idx()
    if idx.size == 0:
        return
    g = ctx.rng["dryden"]
    w = np.column_stack([np.full(idx.size, 4.0), np.full(idx.size, -2.0), np.zeros(idx.size)])
    w += g.normal(0.0, 1.2, (idx.size, 3))
    S.set_wind_from_enu(w, idx)


def _run(kernel: str, seed: int) -> tuple[str, int]:
    with R.isolated_registry() as reg:
        R.register_stage("env", 5, 0, 20, owner="M07")(_env)
        frames: list[bytes] = []
        box: dict = {}

        def probe(S, ctx) -> None:
            if "reader" not in box:  # 构造期（生命周期到 READY）的帧不计
                return
            f = box["reader"].read_latest(box.get("seq", 0))
            if f is not None:
                box["seq"] = f.frame_seq
                frames.append(f.full)

        R.register_stage("det_probe", 2, 0, 145, owner="M08", budget_core=0.0)(probe)
        h = CoreHarness(n=3, reg=reg, kernel=kernel, spacing=20.0, cfg_over={"world_seed": seed})
        box["reader"] = LocalRing.attach(h.path, expect_layout_id=LAYOUT_ID)
        try:
            ids = h.ids()
            script = {
                1.0: [("takeoff", {"alt_m": 15.0}, "*")],
                15.0: [("goto", {"pos": [60.0, 30.0, 15.0]}, ids[0]),
                       ("orbit", {"center": [20.0, 40.0, 15.0], "radius_m": 15.0, "turns": 1}, ids[1]),
                       ("follow_path", {"waypoints": [[60.0, 0.0, 18.0], [60.0, 40.0, 18.0], [20.0, 40.0, 15.0]]}, ids[2])],
                45.0: [("rtl", {}, ids[0])],
            }
            done: set[float] = set()
            while h.t < T_END_S:
                for ts, cmds in script.items():
                    if ts not in done and h.t >= ts:
                        done.add(ts)
                        for op, args, uav in cmds:
                            adm = h.cmd(op, args, uav=uav)
                            assert adm["status"] == "accepted", (op, adm)
                h.W[0] += 2 * TICK_NS
                h.core.iterate()
            dig = hashlib.sha256(b"".join(frames)).hexdigest()
            return dig, len(frames)
        finally:
            h.close()


KERNELS = ["numba", "numpy"] if KL.HAVE_NUMBA else ["numpy"]


@pytest.mark.parametrize("kernel", KERNELS)
def test_same_seed_same_frames(kernel: str) -> None:
    a = _run(kernel, 7)
    b = _run(kernel, 7)
    assert a[1] >= int(T_END_S * 125 * 0.95)
    assert a == b


def test_world_seed_changes_turbulent_fields() -> None:
    a = _run(KERNELS[0], 7)
    c = _run(KERNELS[0], 8)
    assert a[0] != c[0]
