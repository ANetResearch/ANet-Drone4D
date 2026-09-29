"""M13-AC-023：Mock 热成像帧（PGM 头、19215 B、逐字节可重建、热斑峰值位于 (u, v) ± 1 px）；M13-FR-044。"""

from __future__ import annotations

import numpy as np

from awr.sim.sensors.thermal_mock import HEADER, background_temps, render_thermal_frame

P = {"w": 160, "h": 120, "u": 71.3, "v": 44.8, "size_px": 3.2, "t_bg_c": 17.0, "t_tgt_c": 34.0, "t_env_c": 20.0, "tau": 0.95,
     "netd_k": 0.05, "seed": "18446744073709551557"}


def test_pgm_header_size_and_determinism():
    a = render_thermal_frame(P)
    assert a.startswith(b"P5\n160 120\n255\n") and a[: len(HEADER)] == HEADER
    assert len(a) == 19215 >= 1024
    assert render_thermal_frame(dict(P)) == a
    assert render_thermal_frame(dict(P, seed="1")) != a


def test_hot_spot_peak():
    img = np.frombuffer(render_thermal_frame(P)[len(HEADER):], np.uint8).reshape(120, 160)
    r, c = np.unravel_index(int(np.argmax(img)), img.shape)
    assert abs((c + 0.5) - P["u"]) <= 1.0 + 0.5 and abs((r + 0.5) - P["v"]) <= 1.0 + 0.5
    assert img.mean() < img.max() - 50


def test_background_by_kind():
    assert background_temps("person", 20.0) == (17.0, 34.0)
    assert background_temps("vehicle", 20.0) == (22.0, 45.0)


def test_event_artifact_rebuilds(bench):
    from m13_s3lib import run_s3, setup_s3

    setup_s3(bench)
    ev = [e for e in run_s3(bench, 30.0) if e["data"]["artifact"]]
    assert ev
    p = ev[0]["data"]["artifact"]["params"]
    img = render_thermal_frame(p)
    assert len(img) == 19215 and render_thermal_frame(dict(p)) == img and isinstance(p["seed"], str)
    assert 0 <= p["u"] <= 160 and 0 <= p["v"] <= 120
