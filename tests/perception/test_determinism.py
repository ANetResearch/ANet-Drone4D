"""M19-AC-020（确定性，P1）与 M19-FR-027：同一配置与种子两次运行，感知事件序列、检测环与航迹逐字节一致；换种子后量测噪声不同；
M19 看不到 M13 Mock 检测器的目标（M19 只读 M18 实体表 `targets`）。"""

from __future__ import annotations

import json

import numpy as np
from plib import FakeWorld, PBench, make_imaging, make_nonimg

from awr.sim.perception import gx40, mmw77


def _run(seed: int) -> tuple[str, str, str]:
    b = PBench(2, FakeWorld(boxes=[(150.0, 160.0, 20.0, 40.0, 30.0)]), seed=seed)
    with b:
        for k in range(6):
            b.add({"class": "person" if k % 2 == 0 else "dog", "pos": [200.0 + 20 * k, -30.0 + 12 * k, 0.0],
                   "motion": {"mode": "random_walk", "speed_mps": 1.2}})
        b.step(1)
        for s, p in ((0, (0.0, 0.0, 60.0)), (1, (0.0, 60.0, 80.0))):
            b.platform(s, pos=p, imaging=[make_imaging(gx40(), p, (260.0, 0.0, 0.0), f_mm=12.0)], nonimg=[make_nonimg("radar", mmw77(), p)])
        b.run(20.0)
        ev = json.dumps([(e["kind"], e["t_sim_ns"], e["data"]) for e in b.events.items if e["kind"].startswith("perception.")], sort_keys=True)
        ring = json.dumps(list(b.prt.ring.items), sort_keys=True)
        trk = json.dumps(b.prt.tracks_wire(b.t), sort_keys=True)
        return ev, ring, trk


def test_same_seed_bytewise_equal():
    a = _run(7)
    b = _run(7)
    assert a == b
    assert len(json.loads(a[0])) > 5


def test_different_seed_changes_measurements():
    a = _run(7)
    c = _run(8)
    assert a[2] != c[2]


def test_mock_targets_invisible():
    b = PBench(1, FakeWorld())
    with b:
        b.step(1)
        b.platform(0, pos=(0.0, 0.0, 60.0), imaging=[make_imaging(gx40(), (0.0, 0.0, 60.0), (300.0, 0.0, 0.0), f_mm=48.0)])
        b.run(2.0)
        assert b.prt.frame_for("uav-01")["sensors"][0]["targets"] == [] and not b.ev("perception.level")
        assert np.all(b.ents["target"].cols["tgt_best_level"] == 0)
