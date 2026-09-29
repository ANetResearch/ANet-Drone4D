"""冻结湍流盒与共享资产（M07-AC-004、M07-AC-005；M07-FR-012、FR-026、FR-051；16 §8.4）。

耗时类判据（生成 ≤ 1 s、已存在时 ≤ 50 ms）标 perf，只在验收阶段执行。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from awr.environment.conventions import e
from awr.environment.io import awrv
from awr.environment.io.assets import ensure_turb_box, ensure_weather_map, turb_name, turb_params, weather_name
from awr.environment.noise import weather_map, weather_params
from awr.environment.wind.turbulence import TurbBox, box_to_rgba, vk_box


def cdiv(u: np.ndarray, dx: float) -> tuple[np.ndarray, np.ndarray]:
    def d(f, ax):
        return (np.roll(f, -1, ax) - np.roll(f, 1, ax)) / (2 * dx)
    return d(u[0], 0) + d(u[1], 1) + d(u[2], 2), d(u[0], 0)


@pytest.fixture(scope="module")
def box7() -> np.ndarray:
    return vk_box(64, 4.0, 30.0, seed=7)


def test_rms_and_divergence(box7: np.ndarray):
    u = box7
    assert abs(u.std() - 1.0) < 1e-6
    for c in range(3):
        assert 0.94 <= u[c].std() <= 1.06
    dv, g = cdiv(u, 4.0)
    assert dv.std() / g.std() <= 1e-6
    q = np.stack([np.transpose(box_to_rgba(u)[..., c].astype(np.float64), (2, 1, 0)) for c in range(3)])
    dq, gq = cdiv(q, 4.0)
    assert dq.std() / gq.std() <= 2e-3


def test_asset_file_layout(tmp_path):
    r = ensure_turb_box(tmp_path, 7)
    assert r.error is None and r.built
    assert r.path.name == turb_name(7) == "vk_s7_n64_dx4_L30.awrv"
    assert r.path.stat().st_size == 2_097_216
    h = awrv.read_header(r.path)
    assert h["kind"] == 2 and (h["nx"], h["ny"], h["nz"], h["comp"], h["dtype"]) == (64, 64, 64, 4, 1)
    assert h["field_version"] == awrv.field_version("vk|seed=7|n=64|dx=4|L=30|v=1") == awrv.field_version(turb_params(7))
    assert h["cell_m"] == (4.0, 4.0, 4.0) and np.isnan(h["dir_from_deg"]) and h["value_scale"] == 1.0
    vol = awrv.read(r.path)
    assert np.all(vol.data[..., 3] == 0)
    again = ensure_turb_box(tmp_path, 7)
    assert not again.built and again.error is None


def test_asset_rebuilt_when_field_version_differs(tmp_path):
    p = tmp_path / "env" / "turb" / turb_name(9)
    p.parent.mkdir(parents=True)
    small = awrv.AwrvVolume(2, np.zeros((2, 2, 2, 4), np.float16), field_version=1)
    awrv.write_atomic(p, small)
    r = ensure_turb_box(tmp_path, 9)
    assert r.built and r.path.stat().st_size == 2_097_216


def test_weather_map_asset(tmp_path):
    r = ensure_weather_map(tmp_path, 11, load=True)
    assert r.error is None and r.path.name == weather_name(11)
    v = r.volume
    assert v.kind == 6 and v.data.shape == (1, 512, 512, 4) and v.data.dtype == np.uint8
    assert v.field_version == awrv.field_version(weather_params(11))
    assert np.all(v.data[..., 3] == 255)
    cov = v.data[0, ..., 0].astype(float) / 255
    assert 0.3 < cov.mean() < 0.7 and cov.std() > 0.1
    # 可平铺：首末行列的差不比相邻行列大很多
    assert np.abs(cov[0] - cov[-1]).mean() < 3 * np.abs(cov[0] - cov[1]).mean() + 0.02
    assert np.array_equal(weather_map(11, 64), weather_map(11, 64))


def test_asset_failure_disables_turbulence(tmp_path):
    from awr.environment.field import EnvironmentServiceImpl

    blocker = tmp_path / "env"
    blocker.write_text("not a directory")
    env = EnvironmentServiceImpl(world_id="t", shared_dir=tmp_path, world_seed=1)
    assert env.kf.config["wind"]["turbulence"]["model"] == "off"
    codes = [w["code"] for w in env.warnings]
    assert codes.count("asset_unavailable") == 2


def test_sampling_cell_centre_and_periodic(box7: np.ndarray):
    rgba = box_to_rgba(box7)
    box = TurbBox(rgba, 4.0)
    # 格心 (i + 0.5)*dx 处恰为格值；周期 256 m
    i, j, k = 5, 17, 40
    q = np.array([[(i + 0.5) * 4, (j + 0.5) * 4, (k + 0.5) * 4]])
    assert np.allclose(box.sample(q)[0], rgba[k, j, i, :3].astype(float))
    assert np.allclose(box.sample(q + 256.0), box.sample(q)) and np.allclose(box.sample(q - 512.0), box.sample(q))


def test_direction_change_is_continuous(box7: np.ndarray):
    """M07-AC-005：风向 3 s 内 0° -> 90°，距原点 2 km 处相邻 20 ms 湍流矢量差有线性界；旋转采样坐标的写法会跳变。"""
    box = TurbBox(box_to_rgba(box7), 4.0)
    p = np.array([[2000.0, 0.0, 50.0]])
    dt = 0.02
    s = 10.0
    D = np.zeros(3)
    prev = prev_rot = None
    worst = worst_rot = 0.0
    for kstep in range(int(3.0 / dt) + 1):
        th = 90.0 * min(kstep * dt / 3.0, 1.0)
        ex, ey = e(th)
        step = np.array([s * ex, s * ey, 0.0]) * dt
        D = D + step
        b = box.sample(p - np.mod(1.463 * D, 256.0))[0]  # 平移坐标
        ang = np.radians(th)
        R = np.array([[np.cos(ang), -np.sin(ang), 0], [np.sin(ang), np.cos(ang), 0], [0, 0, 1]])
        b_rot = box.sample((R.T @ p[0])[None] - np.mod(np.array([1.463 * s * kstep * dt, 0, 0]), 256.0))[0]  # 旋转采样坐标（反例）
        if prev is not None:
            worst = max(worst, float(np.linalg.norm(b - prev)))
            worst_rot = max(worst_rot, float(np.linalg.norm(b_rot - prev_rot)))
        prev, prev_rot = b, b_rot
    bound = 4.0 * (1.463 * s * dt / 4.0)  # 4*sigma*(平移量 / 格距)，sigma = 1
    assert worst <= bound
    assert worst_rot > bound


@pytest.mark.perf
def test_generation_time(tmp_path):
    t = time.perf_counter()
    ensure_turb_box(tmp_path, 3, load=False)
    assert time.perf_counter() - t <= 1.0
    t = time.perf_counter()
    ensure_turb_box(tmp_path, 3, load=False)
    assert time.perf_counter() - t <= 0.05
    t = time.perf_counter()
    ensure_weather_map(tmp_path, 3)
    assert time.perf_counter() - t <= 0.5
