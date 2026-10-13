"""敏感体（M18-AC-008；M18-FR-009；AWR-04 §7.3、§10.5）：四种形状的内外判定 golden（含边界、z 范围、扇形方向与方位偏移）；
每识别物 > 4 或会话启用 > 64 返回 542；禁用的体不参与；带符号距离与法向（软围栏）。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from tlib import Bench, FakeWorld, static_person

from awr.sim.targets import body_volumes, inside, signed_distance

C = (10.0, -5.0, 2.0)


def _vol(shape: str, **kw):
    b = {"kind": "visual", "shape": shape, "radius_max_m": 100.0, "z_min_m": 0.0, "z_max_m": 50.0, "half_angle_deg": 30.0,
         "azimuth_offset_deg": 0.0, **kw}
    return body_volumes([b], C, kw.pop("yaw", 0.0) if "yaw" in kw else 0.0)[0]


GOLDEN = [
    ("sphere", {}, (10 + 99.9, -5, 2), True), ("sphere", {}, (10, -5, 2 - 99.9), True), ("sphere", {}, (10 + 70.8, -5 + 70.8, 2), False),
    ("hemisphere", {}, (10, -5, 2 + 99.9), True), ("hemisphere", {}, (10 + 50, -5, 2 - 0.01), False),
    ("cylinder", {}, (10 + 99.9, -5, 2 + 50), True), ("cylinder", {}, (10 + 99.9, -5, 2 + 50.01), False),
    ("cylinder", {"z_min_m": -10}, (10, -5, 2 - 9.9), True), ("cylinder", {}, (10, -5, 1.9), False),
    ("sector", {}, (10 + 80, -5 + 80 * math.tan(math.radians(29.9)), 10), True),
    ("sector", {}, (10 + 80, -5 + 80 * math.tan(math.radians(30.1)), 10), False),
    ("sector", {}, (10 - 50, -5, 10), False),
    ("sector", {"azimuth_offset_deg": 90}, (10, -5 + 60, 10), True), ("sector", {"azimuth_offset_deg": 90}, (10 + 60, -5, 10), False),
    ("sector", {"azimuth_offset_deg": -180}, (10 - 60, -5, 10), True),
]


@pytest.mark.parametrize(("shape", "kw", "p", "exp"), GOLDEN)
def test_inside_golden(shape, kw, p, exp) -> None:
    assert inside(_vol(shape, **kw), p) is exp


def test_sector_follows_heading_and_moving_circle() -> None:
    b = [{"kind": "visual", "shape": "sector", "radius_max_m": 100.0, "half_angle_deg": 30.0, "z_min_m": 0, "z_max_m": 50}]
    v = body_volumes(b, (0, 0, 0), math.pi / 2)[0]
    assert inside(v, (0, 50, 5)) and not inside(v, (50, 0, 5))
    c = body_volumes(b, (0, 0, 0), math.pi / 2, moving_as_circle=True)[0]
    assert c.shape == "cylinder" and inside(c, (50, 0, 5)) and inside(c, (0, -50, 5))


def test_signed_distance() -> None:
    v = _vol("cylinder")
    d, n = signed_distance(v, (10 + 120, -5, 10))
    assert d == pytest.approx(20.0) and np.allclose(n, [1, 0, 0])
    d, n = signed_distance(v, (10, -5, 2 + 60))
    assert d == pytest.approx(10.0) and np.allclose(n, [0, 0, 1])
    d, _n = signed_distance(v, (10 + 50, -5, 10))
    assert d < 0
    s = _vol("sphere")
    d, n = signed_distance(s, (10, -5 + 130, 2))
    assert d == pytest.approx(30.0) and np.allclose(n, [0, 1, 0])
    sec = _vol("sector")
    d, n = signed_distance(sec, (10 - 30, -5, 10))   # 扇形背后：到两条边线的距离 = 到顶点 30 m
    assert d == pytest.approx(30.0, abs=1e-6) and n[0] < 0


def test_limits_per_target_and_session() -> None:
    four = [{"kind": "visual", "shape": "sphere", "radius_max_m": 10.0}] * 4
    with Bench(0, FakeWorld(), limit=30) as b:
        r = b.cmd("target/add", {"target": static_person(sensitivity=four + four[:1])})
        assert r["status"] == "rejected" and r["code"] == 542 and r["detail"]["why"] == "TOO_MANY_BODIES"
        for k in range(16):
            b.add(static_person(pos=[k * 5.0, 0.0], sensitivity=four))
        r = b.cmd("target/add", {"target": static_person(pos=[200.0, 0.0], sensitivity=four[:1])})
        assert r["status"] == "rejected" and r["code"] == 542 and r["detail"]["why"] == "SESSION_BODY_LIMIT"
        # 禁用的体不计入
        tid = b.add(static_person(pos=[200.0, 0.0], sensitivity=[dict(four[0], enabled=False)]))
        assert b.col("tgt_n_vol", tid) == 0
        r = b.cmd("target/update", {"id": tid, "patch": {"sensitivity": four[:1]}})
        assert r["status"] == "rejected" and r["code"] == 542


def test_disabled_body_does_not_trigger() -> None:
    with Bench(1, FakeWorld()) as b:
        tid = b.add(static_person(sensitivity=[{"kind": "visual", "shape": "sphere", "radius_max_m": 100.0, "enabled": False}]))
        b.S.fly(0, [20.0, 0.0, 10.0])
        b.run(5.0)
        assert b.T(tid).aware == 0 and b.col("tgt_occupied", tid) == 0
