"""M19-AC-001：传感器参数。六类型号（GX40、nir_ill_l、LWIR 640、mmw77、mic8、MID-360）由型号库加载且通过契约 schema；GX40 FOV 与
规格误差 ≤ 0.2°；越界 561（分辨率、4K 算力、LWIR 镜头），型号不存在或非成像 560；M13 成像状态（eo_zoom、LWIR、D1 虚拟像元）的换算。"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from awr.sim.perception import PerceptionError, gx40, gx40_nir, hfov_rad, lwir640, mic8, mid360, mmw77, vfov_rad
from awr.sim.perception.specs import imaging_state_spec, load_model_doc, sensor_for, with_resolution


def _validate(doc: dict) -> list[str]:
    from awr.sim.sensors.spec import schema_problems

    return schema_problems(doc)


@pytest.mark.parametrize("mid", ["eo_gx40", "nir_ill_l", "lwir640", "mmw77", "mic8", "mid360"])
def test_models_load_and_pass_schema(mid):
    doc = load_model_doc(mid)
    assert doc["model_id"] == mid
    assert _validate(doc) == []


def test_gx40_fov_within_02_deg():
    s = gx40("4k")
    for f, h, v in ((4.8, 60.2, 36.1), (48.0, 6.6, 3.7)):
        assert abs(math.degrees(hfov_rad(s, f)) - h) <= 0.2
        assert abs(math.degrees(vfov_rad(s, f)) - v) <= 0.2
    # 裁切输出的视场按 2·atan(W_crop·1.45 µm/2f)
    assert math.degrees(hfov_rad(s, 4.8, "sxga")) == pytest.approx(math.degrees(2 * math.atan(2700 * 1.45e-3 / 9.6)), abs=1e-9)


def test_reference_factories_follow_the_library():
    s = gx40()
    assert (s.pixel_um, s.w_native_px, s.f_min_mm, s.f_max_mm, s.f_number_min, s.f_number_max) == (1.45, 3840, 4.8, 48.0, 1.7, 3.2)
    assert s.analysis == "1080p" and s.k4_requires == "compute.orin_nx" and s.illuminators[0].kind == "builtin"
    n = gx40_nir(True)
    assert n.kind == "nir" and [i.kind for i in n.illuminators] == ["builtin", "nir_ill_l"]
    assert n.illuminators[1].r0_m(math.radians(5.5), 0.3) == pytest.approx(464.76, abs=0.05)   # AWR-20 §6.3 表
    lw = lwir640(75.0)
    assert lw.kind == "lwir" and lw.netd_k == pytest.approx(0.05) and lw.lens_mm == (13.0, 25.0, 75.0, 100.0)
    assert lw.jitter_short_urad == 20.0 and lw.t_int_s == pytest.approx(0.01)
    r = mmw77()
    assert (r.fov_az_deg, r.fov_el_deg, r.r_ref_m, r.snr_d_db, r.r_max_m) == (60.0, 15.0, 180.0, 13.2, 400.0)
    a = mic8()
    assert a.array_gain_db == 9.0 and a.nr_db == {"multirotor": 25.0, "fixed_wing": 35.0} and a.snr_d_db == 6.0
    m = mid360()
    assert m.hard_max_m == 100.0 and m.r_ref_m == 40.0 and m.rho_exp == pytest.approx(0.269)


def test_rejections():
    with pytest.raises(PerceptionError) as e:
        with_resolution(gx40(), "8k")
    assert e.value.code == 561 and e.value.detail["why"] == "RESOLUTION_UNKNOWN"
    with pytest.raises(PerceptionError) as e:
        with_resolution(gx40(), "4k", capabilities=["nav.rtk"])
    assert e.value.code == 561 and e.value.detail["why"] == "K4_REQUIRES"
    assert with_resolution(gx40(), "4k", capabilities=["compute.orin_nx"]).analysis == "4k"
    with pytest.raises(PerceptionError) as e:
        lwir640(50.0)
    assert e.value.code == 561
    with pytest.raises(PerceptionError) as e:
        load_model_doc("no_such_sensor")
    assert e.value.code == 560
    with pytest.raises(PerceptionError) as e:
        sensor_for("mmw77")
    assert e.value.code == 560
    assert sensor_for("eo_gx40", light="night").kind == "nir" and sensor_for("lwir640", lens_mm=25.0).f_max_mm == 25.0


def _zoom_spec():
    from awr.sim.sensors.spec import build_spec

    doc = {"schema": "awr.sensor.eo_zoom.v1", "sensor_id": "eo", "spec_ref": "eo_gx40@1.0.0", "mount": {"xyz_m": [0, 0, 0], "rpy_deg": [0, 0, 0]}}
    return build_spec("eo", doc, 3)


def test_imaging_state_eo_zoom_and_d1_virtual():
    sp = _zoom_spec()
    st = SimpleNamespace(sensor="eo", sensor_no=3, kind="eo", col=0, f_mm=24.0, f_cmd_mm=24.0, f_number=2.45, hfov_rad=0.2, vfov_rad=0.1,
                         p_eff_um=2.9, analysis="1080p", width_px=1920, height_px=1080, mode="nir", mode_cmd="auto", active=True, t_exp_s=1 / 30,
                         jitter_urad=30.0, illuminators=({"kind": "builtin", "on": True, "spec": sp.illum},), az_rad=0.0, el_rad=0.0,
                         src="default", spec=sp)
    spec, sst = imaging_state_spec(st)
    assert spec.kind == "nir" and spec.f_max_mm == 48.0 and spec.outputs["1080p"] == (1920, 3840)
    assert sst.f_mm == 24.0 and sst.mode == "auto" and sst.illum_on == (True,)
    assert spec.illuminators[0].divergence_deg is None and spec.illuminators[0].r_ref_m == 200.0
    d1 = SimpleNamespace(sensor="camera", sensor_no=0, kind="eo", col=0, f_mm=float("nan"), f_cmd_mm=float("nan"), f_number=float("nan"),
                         hfov_rad=math.radians(90.0), vfov_rad=math.radians(60.0), p_eff_um=float("nan"), analysis="native", width_px=1280,
                         height_px=720, mode="day", mode_cmd="auto", active=True, t_exp_s=0.001, jitter_urad=10.0, illuminators=(), az_rad=0.0,
                         el_rad=0.0, src="default", spec=SimpleNamespace(zoom=None, doc={}, model_id=""))
    spec2, sst2 = imaging_state_spec(d1)
    fx = 640.0 / math.tan(math.radians(45.0))
    assert spec2.virtual and sst2.f_mm == pytest.approx(fx * 1e-3) and spec2.pixel_um == 1.0
    # 虚拟像元：N = d·fx/R、GSD = R/fx
    from awr.sim.perception import gsd_m, p_prime_um

    assert gsd_m(100.0, p_prime_um(spec2, sst2.f_mm), sst2.f_mm) == pytest.approx(100.0 / fx)
