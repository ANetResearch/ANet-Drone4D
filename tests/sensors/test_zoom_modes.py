"""M13-AC-202、AC-203：变焦与 FOV、成像模式与照明器（payload 块）；M13-FR-203–205。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.sim.sensors.commands import vehicle_command
from awr.sim.sensors.enums import ImagingMode, LightBand

STAGE_S = 0.02


def _cmd(b, op, vid, **args):
    return vehicle_command(b.rt, op, vid, args, b.tick + 1, b.ctx)


def _p600(b, slot=0, agent=1, vid="p1"):
    b.spawn(slot, agent, "p600_mid360", vid=vid)
    b.run(0.04)
    return slot


def test_spawn_defaults(bench_d2):
    b = bench_d2
    s = _p600(b)
    rt = b.rt
    P = rt.pblk
    rig = rt.rig_of(s)
    assert rig.col_specs[0].name == "eo" and int(P["mnt"][s]) == rig.mask
    assert P["f"][s, 0] == pytest.approx(4.8) and P["f_cmd"][s, 0] == pytest.approx(4.8) and np.isnan(P["f"][s, 1])
    st = rt.imaging_specs(s)[0]
    assert st.sensor == "eo" and st.mode == "day" and st.mode_cmd == "auto" and st.analysis == "1080p"
    assert math.degrees(st.hfov_rad) == pytest.approx(60.2, abs=0.2) and st.f_number == pytest.approx(1.7)
    assert st.width_px == 1920 and st.p_eff_um == pytest.approx(2.9) and st.t_exp_s == pytest.approx(0.001)
    # 云台缺省 FIXED(0, −15°)，源 default
    assert math.degrees(float(rt.blk["g_el"][s, 0])) == pytest.approx(-15.0) and st.src == "default"


def test_zoom_full_range_takes_4s_and_is_log_linear(bench_d2):
    b = bench_d2
    s = _p600(b)
    rt = b.rt
    r = _cmd(b, "zoom", "p1", sensor="eo", f_mm=48.0)
    assert r["status"] == "accepted" and r["result"]["eta_s"] == pytest.approx(4.0)
    trace = []
    for _ in range(int(5.0 * 250)):
        b.step()
        trace.append((b.ctx.t_ns, float(rt.pblk["f"][s, 0])))
    moving = [(t, f) for t, f in trace if f > 4.8]
    t_start = next(t for t, f in trace if f > 4.8) - int(STAGE_S * 1e9)  # 第一次变焦步之前的那次调用
    t_end = next(t for t, f in trace if f == 48.0)
    assert (t_end - t_start) / 1e9 == pytest.approx(4.0, abs=STAGE_S + 1e-9)
    # ln f 随时间线性（每次调用步长 ln10·0.02/4）
    lf = np.log([f for _t, f in moving if f < 48.0])
    d = np.diff(lf)
    assert np.allclose(d[d > 0], math.log(10.0) * STAGE_S / 4.0, rtol=1e-9)
    st = rt.imaging_specs(s)[0]
    assert math.degrees(st.hfov_rad) == pytest.approx(6.6, abs=0.2) and math.degrees(st.vfov_rad) == pytest.approx(3.7, abs=0.2)
    assert st.f_number == pytest.approx(3.2)
    # 机内照明器发散角随变焦取 HFOV（AWR-04 §6.1）
    ill = st.illuminators[0]
    assert ill["kind"] == "builtin" and ill["theta_rad"] == pytest.approx(st.hfov_rad) and ill["range_m"] == pytest.approx(198.8, abs=0.1)


def test_zoom_x_and_reverse(bench_d2):
    b = bench_d2
    s = _p600(b)
    assert _cmd(b, "zoom", "p1", sensor="eo", zoom_x=5.0)["status"] == "accepted"
    b.run(3.0)
    assert float(b.rt.pblk["f"][s, 0]) == pytest.approx(24.0)
    assert _cmd(b, "zoom", "p1", sensor="eo", f_mm=4.8)["status"] == "accepted"
    b.run(3.0)
    assert float(b.rt.pblk["f"][s, 0]) == pytest.approx(4.8)


def test_zoom_rejections(bench_d2):
    b = bench_d2
    _p600(b)
    b.spawn(1, 2, "tq9_test", vid="q9")
    b.run(0.04)
    r = _cmd(b, "zoom", "p1", sensor="eo", f_mm=60.0)
    assert r["code"] == 561 and r["detail"]["detail"] == "F_OUT_OF_RANGE" and r["detail"]["max"] == 48.0
    assert _cmd(b, "zoom", "p1", sensor="eo", f_mm=3.0)["code"] == 561
    assert _cmd(b, "zoom", "q9", sensor="lwir", f_mm=25.0)["code"] == 560     # LWIR 没有光学变焦（数字变焦只在前端）
    assert _cmd(b, "zoom", "q9", sensor="radar", f_mm=25.0)["code"] == 560
    assert _cmd(b, "zoom", "p1", sensor="camera", f_mm=10.0)["code"] == 560   # D1 槽位未挂载
    assert _cmd(b, "zoom", "nope", sensor="eo", f_mm=10.0)["code"] == 107


def test_auto_modes_follow_light_band(bench_d2):
    b = bench_d2
    s = _p600(b)
    rt = b.rt
    P = rt.pblk
    rt.set_light_band("day")
    b.run(0.1)
    assert int(P["im_mode"][s, 0]) == ImagingMode.DAY and P["ill_on"][s, 0, 0] == 0
    seen = []

    def at(bb, t):
        if t == 400:
            rt.set_light_band("low")
        if t == 800:
            rt.set_light_band("night")
        if t == 1200:
            rt.set_light_band("day")

    b.tick = 300
    for _ in range(1000):
        b.step(at)
        seen.append((b.tick - 1, int(P["im_mode"][s, 0]), int(P["ill_on"][s, 0, 0])))
    ev = b.events.of("sensor.mode")
    assert [(e["data"]["from"], e["data"]["to"], e["data"]["reason"]) for e in ev] == [
        ("day", "lowlight", "light_band"), ("lowlight", "nir", "light_band"), ("nir", "day", "light_band")]
    # 档位变化后的第一次 sensors 调用即切换（stage 50 Hz，≤ 1 个 stage 周期）
    first_low = next(t for t, m, _ in seen if m == ImagingMode.LOWLIGHT)
    assert 400 <= first_low < 405
    # 夜间：同云台全部照明器打开（机内）；回到日间关闭
    assert any(m == ImagingMode.NIR and on == 1 for _t, m, on in seen)
    assert P["ill_on"][s, 0, 0] == 0
    st = rt.imaging_specs(s)[0]
    assert st.mode == "day"


def test_nir_turns_on_external_illuminator(bench_d2):
    b = bench_d2
    rt = b.rt
    rt.configure_mounts("p1", {"nir_ill": {"enabled": True}})
    s = _p600(b)
    P = rt.pblk
    assert rt.rig_of(s).col_of("nir_ill") == 0
    rt.set_light_band("night")
    b.run(0.1)
    assert int(P["im_mode"][s, 0]) == ImagingMode.NIR and list(P["ill_on"][s, 0]) == [1, 1]
    ills = {x["sensor"]: x for x in rt.imaging_specs(s)[0].illuminators}
    assert ills["nir_ill"]["on"] and ills["nir_ill"]["range_m"] == pytest.approx(464.8, abs=0.1)
    # 外挂照明器锁定关闭
    assert _cmd(b, "sensor/mode", "p1", sensor="eo", illum={"ext": "off"})["status"] == "accepted"
    b.run(0.04)
    assert list(P["ill_on"][s, 0]) == [1, 0]
    msg = rt.payload.message(s, b.ctx.t_ns)
    by = {e["sensor"]: e for e in msg["sensors"]}
    assert by["nir_ill"]["kind"] == "nir_illuminator" and by["nir_ill"]["illuminator"]["on"] is False
    assert by["eo"]["mode"] == "nir" and by["eo"]["illuminator"]["on"] is True


def test_manual_lock_ignores_light_band(bench_d2):
    b = bench_d2
    s = _p600(b)
    rt = b.rt
    rt.set_light_band("day")
    b.run(0.04)
    assert _cmd(b, "sensor/mode", "p1", sensor="eo", mode="lowlight")["status"] == "accepted"
    b.run(0.04)
    assert int(rt.pblk["im_mode"][s, 0]) == ImagingMode.LOWLIGHT
    rt.set_light_band("night")
    b.run(0.2)
    assert int(rt.pblk["im_mode"][s, 0]) == ImagingMode.LOWLIGHT
    assert [e["data"]["reason"] for e in b.events.of("sensor.mode")] == ["command"]
    # 回到 auto：按当前档位（夜间）立即切换
    assert _cmd(b, "sensor/mode", "p1", sensor="eo", mode="auto")["status"] == "accepted"
    b.run(0.04)
    assert int(rt.pblk["im_mode"][s, 0]) == ImagingMode.NIR


def test_mode_rejections(bench_d2):
    b = bench_d2
    _p600(b)
    b.spawn(1, 2, "tq1_test", vid="q1")
    b.spawn(2, 3, "tq9_test", vid="q9")
    b.run(0.04)
    r = _cmd(b, "sensor/mode", "q1", sensor="eo", mode="nir")          # EO-Z8 没有照明器
    assert r["code"] == 560 and r["detail"]["detail"] == "NIR_NEEDS_ILLUMINATOR"
    assert _cmd(b, "sensor/mode", "p1", sensor="eo", illum={"ext": "on"})["code"] == 563  # 未挂 nir_ill_l
    assert _cmd(b, "sensor/mode", "p1", sensor="eo", resolution="4k")["status"] == "accepted"  # P600 测试机型有 orin_nx
    assert _cmd(b, "sensor/mode", "q9", sensor="eo", resolution="4k")["code"] == 561   # tq9 无机载算力能力
    assert _cmd(b, "sensor/mode", "p1", sensor="eo", palette="black_hot")["code"] == 560
    assert _cmd(b, "sensor/mode", "q9", sensor="lwir", palette="black_hot")["status"] == "accepted"
    assert _cmd(b, "sensor/mode", "q9", sensor="lwir", mode="day")["code"] == 560
    assert _cmd(b, "sensor/mode", "p1", sensor="eo")["code"] == 110
    b.run(0.04)
    s = 0
    assert b.rt.imaging_specs(s)[0].analysis == "4k" and b.rt.imaging_specs(s)[0].p_eff_um == pytest.approx(1.45)
    m = b.rt.payload.message(2, b.ctx.t_ns)
    assert {e["sensor"]: e["palette"] for e in m["sensors"]}["lwir"] == "black_hot"


def test_active_switch_and_cue_setting(bench_d2):
    b = bench_d2
    b.spawn(0, 1, "tq9_test", vid="q9")
    b.run(0.04)
    rt = b.rt
    assert _cmd(b, "sensor/mode", "q9", sensor="radar", active=False, cue="off")["status"] == "accepted"
    b.run(0.04)
    m = rt.payload.message(0, b.ctx.t_ns)
    by = {e["sensor"]: e for e in m["sensors"]}
    assert by["radar"]["active"] is False and by["eo"]["cue"] == "off" and by["lwir"]["cue"] == "off"
    assert _cmd(b, "sensor/mode", "q9", sensor="eo", active=False)["status"] == "accepted"
    b.run(0.04)
    assert rt.imaging_specs(0)[0].active is False and not (rt.blk["act"][0] & 1)


def test_zoom_and_modes_are_deterministic(bench_d2_factory):
    out = []
    for _ in range(2):
        b = bench_d2_factory()
        _p600(b)
        b.rt.set_light_band("low")
        _cmd(b, "zoom", "p1", sensor="eo", f_mm=31.7)
        b.run(1.3)
        b.rt.set_light_band("night")
        b.run(0.7)
        P = b.S.blocks["payload"]
        out.append(b"".join(np.ascontiguousarray(P[k]).tobytes() for k in sorted(P)) + b.block_bytes())
    assert out[0] == out[1]


def test_light_band_from_env(bench_d2):
    b = bench_d2
    s = _p600(b)

    class Env:
        band = "day"

        def light_band(self, t_ns):
            return self.band

    b.ctx.env = Env()
    b.run(0.04)
    b.ctx.env.band = "night"
    b.run(0.04)
    assert int(b.rt.pblk["im_mode"][s, 0]) == ImagingMode.NIR
    assert b.rt.payload.light_band(b.ctx) == LightBand.NIGHT


def test_pose48_rows_follow_zoom_and_internal_set_zoom(bench_d2):
    """SensorPose48（M13-FR-013）：沙盒 P600 的 eo（sensor_no 5、kind camera）占云台列 0，hfov、vfov 随当前焦距（M13-FR-217）。"""
    import msgpack

    from awr.sim.sensors.pose_pack import decode_rows

    b = bench_d2
    s = _p600(b)
    got: list[bytes] = []
    b.rt.packer.sink = got.append
    b.interest([1])
    b.run(0.3)
    rows = decode_rows(msgpack.unpackb(got[-1], raw=False)["rows"])
    assert rows["sensor_no"].tolist() == [5] and int(rows["kind"][0]) == 0
    assert math.degrees(float(rows["hfov_rad"][0])) == pytest.approx(60.23, abs=0.01)
    assert b.rt.set_zoom(s, "eo", 99.0) is True and b.rt.set_zoom(s, "mid360", 10.0) is False
    b.run(4.5)
    rows = decode_rows(msgpack.unpackb(got[-1], raw=False)["rows"])
    assert math.degrees(float(rows["hfov_rad"][0])) == pytest.approx(6.64, abs=0.01)
    assert math.degrees(float(rows["vfov_rad"][0])) == pytest.approx(3.74, abs=0.01)
    assert b.rt.mounted_names("p1") == ["mid360", "gnss", "imu", "eo"]


def test_light_band_from_m07_sky_sample(bench_d2):
    """M07（WP-09）的 `EnvironmentService.sky_sample(t_ns).band_code`（0 day、1 low、2 night）驱动 auto 模式。"""
    from types import SimpleNamespace

    b = bench_d2
    s = _p600(b)

    class Env:
        code = 0

        def sky_sample(self, t_ns):
            return SimpleNamespace(band_code=self.code)

    b.ctx.env = Env()
    b.run(0.04)
    assert int(b.rt.pblk["im_mode"][s, 0]) == ImagingMode.DAY
    b.ctx.env.code = 1
    b.run(0.04)
    assert int(b.rt.pblk["im_mode"][s, 0]) == ImagingMode.LOWLIGHT
    b.ctx.env.code = 2
    b.run(0.04)
    assert int(b.rt.pblk["im_mode"][s, 0]) == ImagingMode.NIR and b.rt.pblk["ill_on"][s, 0, 0] == 1
