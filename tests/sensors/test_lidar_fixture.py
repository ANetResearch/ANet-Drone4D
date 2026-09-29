"""M13-AC-025（P2）：LiDAR 帧夹具通过 16 §14.3 schema 与 M02 规则 ①②③④⑦，0 告警；每帧点数、line、offset、100 ms 网格。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from awr.sim.sensors.frames import mount_R
from awr.sim.sensors.lidar.frame import decode_lidar_frame, encode_lidar_frame, pack_scan8, validate_lidar_frame

# 按文件路径加载生成器（tests/sensors/fixtures 不是包，避免与其他测试目录的同名模块冲突）
_spec = importlib.util.spec_from_file_location("m13_gen_lidar_fixture", Path(__file__).parent / "fixtures" / "gen_lidar_fixture.py")
assert _spec is not None and _spec.loader is not None
_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen)
make_frame = _gen.make_frame


def synthetic_grid() -> SimpleNamespace:
    a = np.zeros((200, 200), np.float32)
    a[80:120, 110:140] = 25.0  # 一栋 25 m 楼
    return SimpleNamespace(a=a, x0_m=-200.0, y0_m=-200.0, cell_m=2.0)


def test_fixture_frame_valid():
    g = synthetic_grid()
    for preset, rpy in (("upright", [0, 20, 0]), ("inverted", [180, 0, 0])):
        hdr, pts = make_frame(g, np.array([0.0, 0.0, 30.0]), mount_R(rpy), 3, t_frame_ns=1_234_567_890)
        buf = encode_lidar_frame(hdr, pts)
        h2, p2 = decode_lidar_frame(buf)
        err, warn = validate_lidar_frame(h2, p2)
        assert err == [] and warn == [], (preset, err, warn)
        assert h2["timebase_ns"] % 100_000_000 == 0 and h2["sync_type"] == "sim" and h2["T_world_sensor"] is not None
        assert 0 < h2["point_count"] <= 20000 and int(p2["line"].max()) < 4 and np.all(np.diff(p2["offset_time"].astype(np.int64)) >= 0)
        assert len(buf) - buf.index(b"\n") - 1 == 20 * h2["point_count"]
    # 倒装看到的地面多于正装（r04 §3.1.6 的趋势）
    up = make_frame(g, np.array([0.0, 0.0, 30.0]), mount_R([0, 20, 0]), 3)[0]["point_count"]
    inv = make_frame(g, np.array([0.0, 0.0, 30.0]), mount_R([180, 0, 0]), 3)[0]["point_count"]
    assert inv > up


def test_validator_catches_rule_violations():
    g = synthetic_grid()
    hdr, pts = make_frame(g, np.array([0.0, 0.0, 30.0]), mount_R([180, 0, 0]), 0)
    bad = dict(pts, line=pts["line"].copy(), offset_time=pts["offset_time"].copy())
    bad["line"][0] = 5
    bad["offset_time"][-1] = 0
    err, _ = validate_lidar_frame(dict(hdr, timebase_ns=50_000_000, raw_timebase_ns=50_000_000), bad)
    assert any("rule 1" in e for e in err) and any("rule 2" in e for e in err) and any("rule 3" in e for e in err)
    _, warn = validate_lidar_frame(dict(hdr, sync_type="none"), pts)
    assert any("rule 4" in w for w in warn)
    err, _ = validate_lidar_frame(dict(hdr, sync_type="gps"), pts)
    assert any("schema" in e for e in err)


def test_scan8_layout():
    b = pack_scan8(100_000_000, 7, np.zeros(3), np.array([0, 0, 0, 1.0]), np.array([[1.0, -2.0, 0.5]]), np.array([100]),
                   np.array([1]), np.array([3]))
    assert len(b) == 48 + 8
    assert np.frombuffer(b[48:54], "<i2").tolist() == [100, -200, 50] and b[55] == (1 | (3 << 6))
