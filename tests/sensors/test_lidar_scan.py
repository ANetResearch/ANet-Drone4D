"""M13-AC-206、AC-209（确定性部分）：MID-360 限频扫描（订阅时、≤ 2 Hz、每帧 ≤ 20 000 点与 ≤ 160 KB、DSM 面偏差、识别物
包围盒、每会话 1 路与 paused、逐字节确定）；M13-FR-213、NFR-202。慢任务成本（每轮 ≤ 300 µs、2 Hz ≤ 0.012 核）属性能用例，
由 `fleet_ladder --clock sandbox100` 在 MS5 测量（并行阶段不跑基准）。"""

from __future__ import annotations

import math

import msgpack
import numpy as np
import pytest

from awr.sim.sensors.frames import quat_to_R
from awr.sim.sensors.lidar.pattern import NPF
from awr.sim.sensors.lidar.scan_rt import SLICE, dda_dsm

HEAD = 48
BODY = np.dtype([("x", "<i2"), ("y", "<i2"), ("z", "<i2"), ("refl", "u1"), ("tl", "u1")])


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from awr.world.geometry.fake import fake_world_query

    return fake_world_query(tmp_path_factory.mktemp("tinyw"))


def _decode(blob: bytes):
    h = np.frombuffer(blob[:HEAD], [("t", "<i8"), ("seq", "<u4"), ("n", "<u4"), ("pos", "<f4", (3,)), ("q", "<f4", (4,)),
                                    ("decim", "<u2"), ("flags", "<u2")])[0]
    body = np.frombuffer(blob[HEAD:], BODY)
    xyz = np.stack([body["x"], body["y"], body["z"]], axis=1).astype(np.float64) / 100.0
    R = quat_to_R(np.asarray(h["q"], np.float64)[None])[0]
    pw = np.asarray(h["pos"], np.float64)[None] + xyz @ R.T
    return h, body, pw


def _frame(raw: bytes) -> dict:
    """总线消息 `{v: 1, t_sim_ns, items: {<uav id>: LidarScan8}}` -> {uav, t_sim_ns, scan}（每会话 1 路，只有一项）。"""
    m = msgpack.unpackb(raw, raw=False)
    assert m["v"] == 1 and len(m["items"]) == 1
    (uav, blob), = m["items"].items()
    return {"uav": uav, "t_sim_ns": m["t_sim_ns"], "scan": blob}


def _bench(factory, world, *, targets=True):
    b = factory(world=world)
    frames: list[dict] = []
    b.rt.scanner.sink = lambda raw: frames.append(_frame(raw))
    b.spawn(0, 21, "p600_mid360", vid="a", pos=(-80.0, -20.0, 12.0))
    b.spawn(1, 22, "p600_mid360", vid="b", pos=(-100.0, 40.0, 30.0))
    if targets:
        b.ctx.entities = {"target": {"active": np.array([1], np.uint8), "target_no": np.array([3], np.uint16),
                                     "tgt_pos": np.array([[-50.0, -38.0, 0.0]]), "tgt_size": np.array([[4.5, 1.8, 1.5]], np.float32),
                                     "tgt_yaw": np.array([0.3])}}
    b.run(0.04)
    return b, frames


def test_scan_only_when_subscribed_and_2hz(bench_d2_factory, world):
    b, frames = _bench(bench_d2_factory, world)
    b.run(1.0)
    assert frames == [] and int(b.rt.pblk["scan"][0]) == 0
    b.rt.stream_interest("lidar", [21])
    b.run(3.0)
    assert 5 <= len(frames) <= 7
    t = [f["t_sim_ns"] for f in frames]
    assert min(np.diff(t)) >= 500_000_000  # ≤ 2 Hz
    for f in frames:
        h, body, _pw = _decode(f["scan"])
        assert f["uav"] == "a" and h["n"] == body.size <= NPF and int(h["t"]) == f["t_sim_ns"]
        assert len(f["scan"]) <= 160 * 1024
    assert int(b.rt.pblk["scan"][0]) == 1
    m = b.rt.payload.message(0, b.ctx.t_ns)
    assert {e["sensor"]: e["mode"] for e in m["sensors"]}["mid360"] == "active"
    b.rt.stream_interest("lidar", [])
    n = len(frames)
    b.run(1.0)
    assert len(frames) == n and int(b.rt.pblk["scan"][0]) == 0


def test_points_lie_on_dsm_and_target_box(bench_d2_factory, world):
    b, frames = _bench(bench_d2_factory, world)
    b.rt.stream_interest("lidar", [21])
    b.run(0.6)
    _h, body, pw = _decode(frames[-1]["scan"])
    g = world.dsm_grid()
    a = np.asarray(g.a)

    def cell(x, y):
        c = np.clip(np.floor((x - g.x0_m) / g.cell_m).astype(int), 0, a.shape[1] - 1)
        r = np.clip(np.floor((y - g.y0_m) / g.cell_m).astype(int), 0, a.shape[0] - 1)
        return r, c

    tgt = (body["tl"] & 1) == 1
    pd = pw[~tgt]
    r, c = cell(pd[:, 0], pd[:, 1])
    top = np.abs(pd[:, 2] - a[r, c]) <= 0.5
    # 侧面命中：所在格或相邻格的格顶高于该点（柱体语义）
    nb = np.max(np.stack([a[np.clip(r + dr, 0, a.shape[0] - 1), np.clip(c + dc, 0, a.shape[1] - 1)]
                          for dr in (-1, 0, 1) for dc in (-1, 0, 1)]), axis=0)
    side = pd[:, 2] <= nb + 0.5
    assert pd.shape[0] > 1000
    assert np.mean(top | side) >= 0.997
    assert top.sum() > 200  # 地面点（其余为塔与薄墙立面的侧面命中）
    # 平整区域（3 × 3 邻格等高：地面、屋顶内部）的点必为顶面命中：与 DSM 高度偏差 ≤ 0.5 m（噪声 sigma 0.02–0.03 m）
    mn = np.min(np.stack([a[np.clip(r + dr, 0, a.shape[0] - 1), np.clip(c + dc, 0, a.shape[1] - 1)]
                          for dr in (-1, 0, 1) for dc in (-1, 0, 1)]), axis=0)
    flat = (nb - mn) < 0.01
    assert flat.sum() > 200 and np.all(np.abs(pd[flat, 2] - a[r[flat], c[flat]]) <= 0.5)
    # 识别物包围盒（车 4.5 × 1.8 × 1.5 m，偏航 0.3 rad 的 AABB）出现在点云中
    pt = pw[tgt]
    assert pt.shape[0] >= 10
    assert np.all(np.abs(pt[:, 0] + 50.0) <= 2.9) and np.all(np.abs(pt[:, 1] + 38.0) <= 1.9)
    assert np.all((pt[:, 2] >= -0.2) & (pt[:, 2] <= 1.7))
    # 反射率：地物 0.2 -> 51，识别物 0.3 -> 76；量程按 R_max(rho) 截断
    assert set(np.unique(body["refl"][~tgt]).tolist()) == {51} and set(np.unique(body["refl"][tgt]).tolist()) == {76}
    rng = np.linalg.norm(np.stack([body["x"], body["y"], body["z"]], axis=1) / 100.0, axis=1)
    assert rng[~tgt].max() <= 40.0 * 2.0 ** 0.269 + 0.2 and rng.min() >= 0.05


def test_second_subscription_pauses_first(bench_d2_factory, world):
    b, frames = _bench(bench_d2_factory, world, targets=False)
    b.rt.stream_interest("lidar", [21, 22])  # 最后一个为最新订阅者
    b.run(1.2)
    assert {f["uav"] for f in frames} == {"b"}
    assert int(b.rt.pblk["scan"][0]) == 2 and int(b.rt.pblk["scan"][1]) == 1
    m = b.rt.payload.message(0, b.ctx.t_ns)
    assert {e["sensor"]: e["mode"] for e in m["sensors"]}["mid360"] == "paused"


def test_scan_respects_sensor_off(bench_d2_factory, world):
    from awr.sim.sensors.commands import vehicle_command

    b, frames = _bench(bench_d2_factory, world, targets=False)
    assert vehicle_command(b.rt, "sensor/mode", "a", {"sensor": "mid360", "active": False}, b.tick + 1, b.ctx)["status"] == "accepted"
    b.run(0.04)  # 命令在 apply_tick 之后的首次 sensors 调用生效
    b.rt.stream_interest("lidar", [21])
    b.run(1.2)
    assert frames == []


def test_scan_frames_are_byte_identical(bench_d2_factory, world):
    out = []
    for _ in range(2):
        b, frames = _bench(bench_d2_factory, world)
        b.rt.stream_interest("lidar", [21])
        b.run(1.2)
        out.append([f["scan"] for f in frames])
    assert out[0] and out[0] == out[1]


def test_frame_is_sliced(bench_d2_factory, world):
    """一帧分 ⌈20 000 / SLICE⌉ 片在慢任务中完成（每轮一片）。"""
    b, _frames = _bench(bench_d2_factory, world, targets=False)
    b.slow_d2 = False
    sc = b.rt.scanner
    sc.override = [21]
    n = 0
    while True:
        sc.run_slow(b.ctx)
        n += 1
        if sc.frame is None:
            break
    assert n == math.ceil(NPF / SLICE) and sc.stats["frames"] == 1 and sc.last["n"] > 0


def test_dda_kernel_against_brute_force(world):
    g = world.dsm_grid()
    a = np.ascontiguousarray(g.a)
    rng = np.random.default_rng(5)
    o = np.array([-60.0, 0.0, 45.0])
    d = rng.normal(size=(500, 3))
    d[:, 2] = -np.abs(d[:, 2])
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    t = np.full(500, np.nan)
    dda_dsm(a, float(g.x0_m), float(g.y0_m), float(g.cell_m), float(np.nanmax(a)), *o, d, 150.0, t)
    for i in range(0, 500, 7):  # 0.01 m 步进的暴力求交
        s = np.arange(0.0, 150.0, 0.01)
        p = o[None] + s[:, None] * d[i][None]
        c = np.floor((p[:, 0] - g.x0_m) / g.cell_m).astype(int)
        r = np.floor((p[:, 1] - g.y0_m) / g.cell_m).astype(int)
        inside = (c >= 0) & (c < a.shape[1]) & (r >= 0) & (r < a.shape[0])
        hit = inside & (p[:, 2] <= a[np.clip(r, 0, a.shape[0] - 1), np.clip(c, 0, a.shape[1] - 1)])
        if not hit.any():
            assert math.isnan(t[i]) or t[i] > 149.9
            continue
        assert t[i] == pytest.approx(s[np.argmax(hit)], abs=0.02)


def test_lidar_bus_value_is_scan_blob_for_gateway(bench_d2_factory, world):
    """`state/sim-core/lidar` 的 items 值为 LidarScan8 字节，M11 `StreamDemux` 按字节区间切出（不重新打包）。"""
    from types import SimpleNamespace

    from awr.api.rt.detail import StreamDemux

    b, _frames = _bench(bench_d2_factory, world, targets=False)
    raws: list[bytes] = []
    b.rt.scanner.sink = raws.append
    b.rt.stream_interest("lidar", [21])
    b.run(0.6)
    _t, items = StreamDemux(SimpleNamespace(frame_t_sim_ns=0))._items("lidar", raws[-1])
    assert [k for k, _ in items] == ["a"] and items[0][1] == b.rt.scanner.last["blob"]
