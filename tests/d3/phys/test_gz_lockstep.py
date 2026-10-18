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


WORLD = Path(__file__).resolve().parents[3] / "worlds" / "oryahovo-crop1"
GEAR_Z = 0.227                                                                    # x500_d4d 静止时 base_link 离地高度（地面平面实测）


def _flat_spot(world: Path) -> tuple[float, float, float]:
    """DSM 上 4 m 窗口起伏 < 0.05 m（严于 A5 的 3.5 m 窗口 < 0.4 m）、距世界原点 150 m 内的最低处。"""
    import json

    meta = json.loads((world / "geometry/terrain/dsm_2m.json").read_text())
    z = np.fromfile(world / "geometry/terrain/dsm_2m.f32", "<f4").reshape(meta["height"], meta["width"]).astype(np.float64)
    w = np.lib.stride_tricks.sliding_window_view(z, (2, 2))
    relief = w.max(axis=(2, 3)) - w.min(axis=(2, 3))
    h, wd = relief.shape
    yy, xx = np.mgrid[0:h, 0:wd]
    c = meta["cellM"]
    ex, ny = meta["originXY"][0] + (xx + 1) * c, meta["originXY"][1] + (yy + 1) * c
    ok = (relief < 0.05) & (np.hypot(ex, ny) < 150.0)
    zc = w.max(axis=(2, 3))
    i = np.argmin(np.where(ok, zc, np.inf))
    return float(ex.flat[i]), float(ny.flat[i]), float(zc.flat[i])


@pytest.mark.skipif(not (WORLD / "geometry/mesh/raycast/index.json").exists(), reason="needs worlds/oryahovo-crop1")
def test_land_on_world_mesh(tmp_path: Path) -> None:
    """A5：平坦处上方 15 m 以 2 m/s 下降，静止高度与 DSM + 起落架高度之差 ≤ 0.3 m，不穿透。"""
    pytest.importorskip("zmq")
    from awr.phys.gz.backend import GzSimBackend
    from awr.phys.gz.compose import UavSpawn, compose_run
    from awr.phys.gz.instance import GzInstance
    from awr.world.export.gz import export_world

    exp = export_world(WORLD, out_root=tmp_path / "exp", check_sdf=True)
    x, y, zg = _flat_spot(WORLD)
    inst = GzInstance(tmp_path / "run", "local", launcher="local", world_export=exp.root)
    inst.start(compose_run([UavSpawn("uav-01", (x, y, zg + 15.0))], world_static_uri=exp.uri, endpoint=inst.gz_endpoint,
                           spherical=exp.spherical))
    ev, zs = [], []
    try:
        inst.wait_socket(120)
        be = GzSimBackend(["uav-01"], inst.endpoint, grounded=[])
        be.open()
        be.submit([TierCommand("uav-01", "velocity", 5, vel_enu_mps=(0.0, 0.0, -2.0))])
        for k in range(160):
            r = be.advance(k)
            ev += r.contacts
            zs.append(float(be.history.last_sample("uav-01").pos_enu_m[2]))
        st = be.status()["uav-01"]
        be.close()
    finally:
        inst.stop()
    kinds = [e.kind for e in ev]
    assert "TOUCHDOWN" in kinds and "IMPACT" not in kinds and "COLLISION_WORLD" not in kinds
    assert st == VehicleStatus.GROUNDED
    assert abs(zs[-1] - (zg + GEAR_Z)) <= 0.3 and min(zs) > zg                    # 不穿透



@pytest.mark.skipif(not (WORLD / "geometry/mesh/raycast/index.json").exists(), reason="needs worlds/oryahovo-crop1")
def test_takeoff_from_rest_on_world_mesh(tmp_path: Path) -> None:
    """静止起飞（原型榜起飞点）：按网格面放在静止高度、第 1 个 tick 起飞；网格面上推力爬升可超过 1 s，不能被判为落地（起飞宽限 3 s）。"""
    pytest.importorskip("zmq")
    pytest.importorskip("open3d")
    from awr.agentsdk.env.core import SPAWN_FOOT, _open_world
    from awr.agentsdk.env.geom import surface_z
    from awr.phys.gz.backend import GzSimBackend
    from awr.phys.gz.compose import UavSpawn, compose_run
    from awr.phys.gz.instance import GzInstance
    from awr.phys.kinematic import GEAR_Z_M
    from awr.world.export.gz import export_world

    world = _open_world(WORLD)
    pads = [(76.0, -78.0), (76.0, -82.0), (76.0, -86.0)]
    spawns = [UavSpawn(f"uav-0{i + 1}", (x, y, float(surface_z(world, SPAWN_FOOT + np.array([x, y])).max()) + GEAR_Z_M + 0.01)) for i, (x, y) in enumerate(pads)]
    exp = export_world(WORLD, out_root=tmp_path / "exp", check_sdf=True)
    inst = GzInstance(tmp_path / "run", "local", launcher="local", world_export=exp.root)
    inst.start(compose_run(spawns, world_static_uri=exp.uri, endpoint=inst.gz_endpoint, spherical=exp.spherical))
    ev = []
    uavs = [s.uav for s in spawns]
    try:
        inst.wait_socket(120)
        be = GzSimBackend(uavs, inst.endpoint, grounded=uavs)
        be.open()
        for k in range(60):
            be.submit([TierCommand(u, "takeoff", 1, alt_m=10.0) for u in uavs] if k == 1 else [])
            ev += be.advance(k).contacts
        st = be.status()
        be.close()
    finally:
        inst.stop()
    for u in uavs:
        kinds = [e.kind for e in ev if e.uav == u]
        assert "LIFTOFF" in kinds and "TOUCHDOWN" not in kinds and "COLLISION_WORLD" not in kinds, (u, kinds)
        assert st[u] == VehicleStatus.AIRBORNE
