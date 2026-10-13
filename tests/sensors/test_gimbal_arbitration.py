"""M13-AC-204：云台指向源仲裁与引导（operator > tasking > cue > default；60 s 与 5 s 到期；cue 与共享云台）；M13-FR-206–208。"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from awr.sim.sensors.commands import vehicle_command
from awr.sim.sensors.enums import GimbalMode, PointSrc

PRI = {"operator": PointSrc.OPERATOR, "tasking": PointSrc.TASKING, "cue": PointSrc.CUE, "default": PointSrc.DEFAULT}


def _cmd(b, op, vid, **args):
    return vehicle_command(b.rt, op, vid, args, b.tick + 1, b.ctx)


def _setup(b, *, nir=False, pos=(0.0, 0.0, 60.0)):
    if nir:
        b.rt.configure_mounts("p1", {"nir_ill": {"enabled": True}})
    b.spawn(0, 1, "p600_mid360", vid="p1", pos=pos)
    b.run(0.04)
    return 0


def _apply(b, src: str, s: int = 0):
    rt = b.rt
    if src == "operator":
        assert _cmd(b, "gimbal", "p1", mode="angles", yaw_rad=0.5, pitch_rad=-0.3)["status"] == "accepted"
    elif src == "tasking":
        rt.set_tasking_pointing(s, None, [100.0, -100.0, 0.0])
    elif src == "cue":
        rt.cue_point(s, [-100.0, 50.0, 0.0])
    else:
        rt.set_mode(s, None, "nadir")
    b.run(0.04)


@pytest.mark.parametrize("first,second", list(itertools.product(PRI, PRI)))
def test_priority_matrix(bench_d2, first, second):
    b = bench_d2
    s = _setup(b)
    _apply(b, first)
    _apply(b, second)
    want = max(PRI[first], PRI[second])
    if {first, second} == {"cue"} or (second == "cue" and PRI[first] > PointSrc.CUE):
        pass  # cue 在 operator 或 tasking 生效时只记录不执行
    assert int(b.rt.pblk["src"][s, 0]) == int(want)
    pv = b.rt.payload_view(s)
    assert pv.imaging[0].src == ("default", "cue", "tasking", "operator")[int(want)]


def _point_err_deg(b, s, p):
    """云台光轴与指向 p 的夹角（度）。"""
    pos, R = b.rt.sensor_world_pose(np.array([s]), "eo")
    d = np.asarray(p, float) - pos[0]
    return math.degrees(math.acos(float(np.clip(R[0][:, 0] @ d / np.linalg.norm(d), -1, 1))))


def test_operator_holds_60s_then_falls_back(bench_d2):
    b = bench_d2
    s = _setup(b)
    rt = b.rt
    rt.set_tasking_pointing(s, "g0", [200.0, 0.0, 0.0])
    b.run(0.1)
    assert int(rt.pblk["src"][s, 0]) == PointSrc.TASKING
    assert _cmd(b, "gimbal", "p1", mode="angles", yaw_rad=-1.0, pitch_rad=-0.5)["status"] == "accepted"
    b.run(2.0)
    assert int(rt.pblk["src"][s, 0]) == PointSrc.OPERATOR
    assert float(rt.blk["g_az"][s, 0]) == pytest.approx(-1.0) and float(rt.blk["g_el"][s, 0]) == pytest.approx(-0.5)
    msg = rt.payload.message(s, b.ctx.t_ns)
    assert msg["gimbal"]["src"] == "operator" and msg["gimbal"]["mode"] == "angles"
    b.run(57.5)
    assert int(rt.pblk["src"][s, 0]) == PointSrc.OPERATOR
    b.run(1.0)
    assert int(rt.pblk["src"][s, 0]) == PointSrc.TASKING
    b.run(3.0)
    assert _point_err_deg(b, s, [200.0, 0.0, 0.0]) < 1.0
    # release 立即回落；tasking 释放后回到 default（D1 缺省 FIXED(0, −15°)）
    _cmd(b, "gimbal", "p1", mode="look_at", look_at_enu_m=[0.0, 300.0, 0.0])
    b.run(0.1)
    assert int(rt.pblk["src"][s, 0]) == PointSrc.OPERATOR
    _cmd(b, "gimbal", "p1", mode="release")
    b.run(0.04)
    assert int(rt.pblk["src"][s, 0]) == PointSrc.TASKING
    rt.set_tasking_pointing(s, None, None)
    b.run(3.0)
    assert int(rt.pblk["src"][s, 0]) == PointSrc.DEFAULT
    assert int(rt.blk["g_mode"][s, 0]) == GimbalMode.FIXED and math.degrees(float(rt.blk["g_el"][s, 0])) == pytest.approx(-15.0)


def test_cue_holds_5s_and_is_ignored_under_higher_sources(bench_d2):
    b = bench_d2
    s = _setup(b)
    rt = b.rt
    assert rt.cue_point(s, [50.0, 50.0, 0.0]) is True
    b.run(4.5)
    assert int(rt.pblk["src"][s, 0]) == PointSrc.CUE
    b.run(0.6)
    assert int(rt.pblk["src"][s, 0]) == PointSrc.DEFAULT
    rt.set_tasking_pointing(s, None, [0.0, -100.0, 0.0])
    b.run(0.04)
    before = rt.pblk["tk_t"][s, 0].copy()
    assert rt.cue_point(s, [50.0, 50.0, 0.0]) is False
    assert int(rt.pblk["src"][s, 0]) == PointSrc.TASKING and np.array_equal(rt.blk["g_tgt"][s, 0], before)
    # cue = off 时只记录
    rt.set_tasking_pointing(s, None, None)
    _cmd(b, "sensor/mode", "p1", sensor="eo", cue="off")
    b.run(0.04)
    assert rt.cue(s, 45.0, -20.0, 200.0, "radar") is False


def test_radar_cue_points_within_1deg_in_time(bench_d2):
    b = bench_d2
    s = _setup(b)
    rt = b.rt
    # 目标在东北方向 200 m、俯角 20°：az_enu = 45°（东为 0、逆时针为正）
    p_sensor = rt.sensor_world_pose(np.array([s]), "eo")[0][0]
    az, el, rng = 45.0, -20.0, 200.0
    u = np.array([math.cos(math.radians(el)) * math.cos(math.radians(az)), math.cos(math.radians(el)) * math.sin(math.radians(az)),
                  math.sin(math.radians(el))])
    target = p_sensor + rng * u
    assert rt.cue(s, az, el, rng, "radar") is True
    # 需转过的角度：机头朝北（yaw 0 => ENU 北）时云台偏航 −45°、俯仰从 −15° 到 −20°
    need = max(abs(-45.0 - 0.0), abs(-20.0 + 15.0))
    t_allow = need / 90.0 + 0.1
    t = 0.0
    while _point_err_deg(b, s, target) > 1.0 and t < 5.0:
        b.run(0.02)
        t += 0.02
    assert _point_err_deg(b, s, target) <= 1.0 and t <= t_allow + 0.02
    assert rt.payload.message(s, b.ctx.t_ns)["gimbal"]["mode"] == "cue"


def test_cue_without_range_uses_ray_hit_or_300m(bench_d2):
    b = bench_d2
    s = _setup(b)
    rt = b.rt
    assert rt.cue(s, 0.0, -30.0, None, "acoustic") is True
    p = rt.pblk["cu_t"][s, 0]
    p0 = rt.sensor_world_pose(np.array([s]), "eo")[0][0]
    assert np.linalg.norm(p - p0) == pytest.approx(300.0)


def test_shared_gimbal_illuminator_axis(bench_d2):
    b = bench_d2
    s = _setup(b, nir=True)
    rt = b.rt
    _cmd(b, "gimbal", "p1", mode="angles", yaw_rad=0.7, pitch_rad=-0.6)
    b.run(1.5)
    _p, R_eo = rt.sensor_world_pose(np.array([s]), "eo")
    _q, R_ill = rt.sensor_world_pose(np.array([s]), "nir_ill")
    cosang = float(R_eo[0][:, 0] @ R_ill[0][:, 0])
    assert math.degrees(math.acos(min(1.0, cosang))) == pytest.approx(0.0, abs=1e-9)


def test_track_target_follows_entity_and_unknown_is_543(bench_d2):
    b = bench_d2
    s = _setup(b)
    rt = b.rt
    tb = {"active": np.array([1, 1], np.uint8), "target_no": np.array([7, 8], np.uint16),
          "tgt_pos": np.array([[80.0, 0.0, 0.0], [0.0, 90.0, 0.0]]), "tgt_size": np.ones((2, 3), np.float32),
          "tgt_yaw": np.zeros(2)}
    b.ctx.entities = {"target": tb}
    rt.target_resolver = lambda tid: (7, None) if tid == "person-07" else None
    assert _cmd(b, "gimbal", "p1", mode="track", target="nobody")["code"] == 543
    assert _cmd(b, "gimbal", "p1", mode="track", target="person-07")["status"] == "accepted"
    b.run(0.5)
    tb["tgt_pos"][0] = (90.0, 30.0, 0.0)
    b.run(2.0)
    assert np.allclose(rt.blk["g_tgt"][s, 0], (90.0, 30.0, 0.0))
    assert _point_err_deg(b, s, (90.0, 30.0, 0.0)) < 1.0
    assert rt.payload.message(s, b.ctx.t_ns)["gimbal"]["mode"] == "track"


def test_gimbal_command_rejections(bench_d2):
    b = bench_d2
    _setup(b)
    assert _cmd(b, "gimbal", "p1", mode="angles")["code"] == 110
    assert _cmd(b, "gimbal", "p1", mode="look_at")["code"] == 110
    assert _cmd(b, "gimbal", "p1", mode="spin")["code"] == 110
    b.spawn(1, 2, "tq9_test", vid="q9")
    b.run(0.04)
    r = _cmd(b, "gimbal", "q9", mode="angles", yaw_rad=0.2, gimbal_id="g1")
    assert r["status"] == "accepted" and r["result"]["gimbal"] == "g1"
    b.run(0.5)
    assert float(b.rt.blk["g_az"][1, 1]) == pytest.approx(0.2) and int(b.rt.pblk["src"][1, 0]) == PointSrc.DEFAULT


def test_d1_sources_only_touch_default_layer(bench_d2):
    """D1 任务项与生成器缺省写缺省层；operator 生效期间不改指向，到期后恢复为最新的缺省层目标。"""
    b = bench_d2
    s = _setup(b)
    rt = b.rt
    _cmd(b, "gimbal", "p1", mode="angles", yaw_rad=0.3, pitch_rad=-0.2)
    b.run(0.1)
    rt.set_default_for(s, "lawnmower")
    b.run(0.5)
    assert int(rt.blk["g_mode"][s, 0]) == GimbalMode.FIXED and int(rt.pblk["df_mode"][s, 0]) == GimbalMode.NADIR
    _cmd(b, "gimbal", "p1", mode="release")
    b.run(2.0)
    assert int(rt.blk["g_mode"][s, 0]) == GimbalMode.NADIR and math.degrees(float(rt.blk["g_el"][s, 0])) == pytest.approx(-90.0)
