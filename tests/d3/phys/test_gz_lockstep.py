"""Realistic 档锁步端到端（M24-AC-005、AC-017 子集；dmax，d3-gz 容器内以 `launcher = local` 运行宿主与 gz）。

运行：tools/deploy/dmax/run_gz_tests.sh（或在 d3-gz 容器内 `pytest tests/d3/phys/test_gz_lockstep.py`）。本机无 gz 时跳过。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from awr.phys.adapter import TierCommand, VehicleStatus

pytestmark = pytest.mark.skipif(
    shutil.which("gz") is None
    or not any((Path(p) / "libDrone4DGz.so").exists() for p in os.environ.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "").split(":") if p),
    reason="needs gz-sim with libDrone4DGz.so (d3-gz image)")

G = 9.80665


def _fly(tmp: Path, spawns, plan: dict[int, list[TierCommand]], n_ticks: int, *, grounded=None, keep=None):
    pytest.importorskip("zmq")
    from awr.phys.gz.backend import GzSimBackend
    from awr.phys.gz.compose import compose_run
    from awr.phys.gz.instance import GzInstance

    inst = GzInstance(tmp, "local", launcher="local")
    inst.start(compose_run(spawns, ground_plane=True, endpoint=inst.gz_endpoint))
    trace: dict[str, list] = {s.uav: [] for s in spawns}
    events, statuses = [], []
    try:
        inst.wait_socket(60)
        be = GzSimBackend([s.uav for s in spawns], inst.endpoint, grounded=grounded)
        be.open()
        for k in range(n_ticks):
            be.submit(plan.get(k, []))
            r = be.advance(k)
            events += r.contacts
            statuses.append(dict(r.status))
            for u in trace:
                sl = be.history.slice(u, r.t_start_ns + 1, r.t_end_ns)
                trace[u].append((sl.t_ns[1:], sl.state[1:]))      # 去掉左侧邻样本：每 tick 25 个新样本
        info = be.close()
    finally:
        inst.stop()
    out = {u: (np.concatenate([t for t, _ in v]), np.concatenate([s for _, s in v])) for u, v in trace.items()}
    return out, events, statuses, info


def test_takeoff_hover_grid(tmp_path: Path) -> None:
    from awr.phys.gz.compose import UavSpawn

    tr, _ev, st, info = _fly(tmp_path, [UavSpawn("uav-01", (0.0, 0.0, 0.3))], {10: [TierCommand("uav-01", "takeoff", 10, alt_m=10.0)]}, 300)
    t, s = tr["uav-01"]
    assert np.array_equal(t, 4_000_000 * np.arange(1, 300 * 25 + 1))             # 网格无缺口无重复
    rest = s[:200]                                                               # 起飞前 0.8 s：地面静止
    assert np.all(np.abs(rest[100:, 15] - G) < 0.05)
    hover = s[-2500:]                                                            # 最后 10 s 悬停
    z0 = s[0, 2]
    assert np.max(np.abs(hover[:, 2] - (z0 + 10.0))) < 0.15
    assert np.max(np.linalg.norm(hover[:, :2], axis=1)) < 0.1
    assert abs(float(np.mean(hover[:, 15])) - G) < 0.02 and np.max(np.abs(np.mean(hover[:, 13:15], axis=0))) < 0.02
    assert st[5]["uav-01"] == VehicleStatus.GROUNDED and st[-1]["uav-01"] == VehicleStatus.AIRBORNE
    assert info["iterations"] == 300 * 25


def test_goto_land_and_determinism(tmp_path: Path) -> None:
    from awr.phys.gz.compose import UavSpawn

    spawns = [UavSpawn("uav-01", (0.0, 0.0, 0.3)), UavSpawn("uav-02", (6.0, 0.0, 0.3), yaw_rad=1.2)]
    plan = {5: [TierCommand("uav-01", "takeoff", 5, alt_m=6.0), TierCommand("uav-02", "takeoff", 5, alt_m=4.0)],
            80: [TierCommand("uav-01", "goto", 80, target_enu_m=(20.0, 10.0, 8.0), speed_mps=5.0)],
            200: [TierCommand("uav-01", "land", 200), TierCommand("uav-02", "land", 200)]}
    runs = [_fly(tmp_path / f"r{i}", spawns, plan, 330) for i in range(2)]
    tr, ev, st, info = runs[0]
    assert info["pose_sha256"] == runs[1][3]["pose_sha256"]                       # 同一输入、同一镜像：逐位相同
    p1 = tr["uav-01"][1][-1, :3]
    assert np.hypot(p1[0] - 20.0, p1[1] - 10.0) < 0.3 and p1[2] < 0.3
    assert st[-1] == {"uav-01": VehicleStatus.GROUNDED, "uav-02": VehicleStatus.GROUNDED}
    kinds = [(e.uav, e.kind) for e in ev]
    assert kinds.count(("uav-01", "LIFTOFF")) == 1 and kinds.count(("uav-01", "TOUCHDOWN")) == 1
    assert ("uav-02", "TOUCHDOWN") in kinds and not any(k in ("IMPACT", "COLLISION_WORLD", "COLLISION_UAV") for _u, k in kinds)
    assert any(s["uav-01"] == VehicleStatus.LANDING for s in st)
    yaw2 = tr["uav-02"][1][-1, 3:7]
    assert abs(2.0 * np.arctan2(yaw2[2], yaw2[3]) - 1.2) < 0.05                    # 偏航角速度为零时保持出生航向


def test_kill_in_air_is_impact(tmp_path: Path) -> None:
    from awr.phys.gz.compose import UavSpawn

    plan = {5: [TierCommand("uav-01", "takeoff", 5, alt_m=8.0)], 90: [TierCommand("uav-01", "kill", 90)]}
    _tr, ev, st, _info = _fly(tmp_path, [UavSpawn("uav-01", (0.0, 0.0, 0.3))], plan, 140)
    assert [e.kind for e in ev if e.kind == "IMPACT"] == ["IMPACT"]
    assert st[-1]["uav-01"] == VehicleStatus.CRASHED
