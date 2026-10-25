"""SLAM 导出 bag（§5.4.6；D3-AC-13 的 CustomMsg 条款、D3-AC-42 的 bag 导出）：md5、全槽位零点占位、offset_time 网格、ROS1 bag 结构与读回校验。"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
import pytest

from awr.bench.recorder.lidar import LIDAR_RECORD
from awr.sensorsvc.errors import SensorSvcError
from awr.sensorsvc.slam.bag import (
    BAG_HEADER_LEN,
    BAG_MAGIC,
    COMPRESSED_IMAGE_MD5,
    CUSTOM_POINT,
    IMAGE_TOPIC,
    IMU_MD5,
    IMU_TOPIC,
    LIDAR_TOPIC,
    LIVOX_CUSTOM_MSG_MD5,
    Bag1Reader,
    Bag1Writer,
    BagFormatError,
    SlamBagWriter,
    check_custom_msg,
    full_slots,
    msg_full_text,
    msg_md5,
    offset_time_ns,
    pack_custom_msg,
    pack_imu,
    unpack_compressed_image,
    unpack_custom_msg,
    unpack_imu,
    verify_slam_bag,
)

N = 20_000
NS = 1_000_000_000
G = 9.80665
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"


def scan_points(frame: int, n_slots: int = N, every: int = 3) -> np.ndarray:
    """模拟 MeshLidar 输出：每 `every` 个槽位有一个回波（槽位升序，offset_time = i·10⁸/N）。"""
    i = np.arange(frame % every, n_slots, every)
    p = np.zeros(len(i), LIDAR_RECORD)
    p["offset_time"] = offset_time_ns(n_slots)[i]
    r = 5.0 + (i % 97) * 0.25
    p["x"], p["y"], p["z"] = r * np.cos(i * 1e-3), r * np.sin(i * 1e-3), -30.0
    p["reflectivity"] = 10 + (i % 140)
    p["tag"] = (i % 50 == 0).astype(np.uint8)
    p["line"] = i & 3
    return p


def write_bag(path: Path, *, frames: int = 10, t0: int = 2 * NS, **kw) -> dict:
    with SlamBagWriter(path, **kw) as w:
        for k in range(frames * 20 + 1):                                 # IMU 200 Hz 覆盖全部帧
            t = t0 + k * 5_000_000
            w.imu(t, (0.01, -0.02, 0.03), (0.1, -0.2, G))
        rets = [w.lidar_frame(t0 + k * 100_000_000, scan_points(k)) for k in range(frames)]
        for k in range(frames):
            w.image(t0 + k * 100_000_000 + 50_000_000, JPEG)
    return {"returns": rets}


# ---------------------------------------------------------------- md5 与定义
def test_md5_matches_ros_and_livox_driver_definitions() -> None:
    assert msg_md5("std_msgs/Header") == "2176decaecbce78abc3b96ef049fabed"
    assert msg_md5("livox_ros_driver/CustomMsg") == LIVOX_CUSTOM_MSG_MD5 == "e4d6829bdfe657cb6c21a746c86b21a6"
    assert msg_md5("livox_ros_driver2/CustomMsg") == LIVOX_CUSTOM_MSG_MD5             # driver2 定义逐字相同（r1 §4.6）
    assert msg_md5("sensor_msgs/Imu") == IMU_MD5 == "6a62c6daae103f4ff57a132d6f95cec2"
    assert msg_md5("sensor_msgs/CompressedImage") == COMPRESSED_IMAGE_MD5 == "8f7a12909da2c9d3332d540a0977563f"
    assert msg_md5("geometry_msgs/Vector3") == "4a842b65f413084dc2b10fb484ea7f17"
    assert msg_md5("geometry_msgs/Quaternion") == "a779879fadf0160734f906b8c19c7004"


def test_full_text_lists_dependencies_in_gendeps_format() -> None:
    t = msg_full_text("livox_ros_driver/CustomMsg")
    parts = t.split("=" * 80 + "\n")
    assert len(parts) == 3 and parts[1].startswith("MSG: std_msgs/Header\n") and parts[2].startswith("MSG: livox_ros_driver/CustomPoint\n")
    imu = msg_full_text("sensor_msgs/Imu")
    assert [p.split("\n", 1)[0] for p in imu.split("=" * 80 + "\n")[1:]] == ["MSG: std_msgs/Header", "MSG: geometry_msgs/Quaternion",
                                                                             "MSG: geometry_msgs/Vector3"]


# ---------------------------------------------------------------- 槽位网格与 CustomMsg
def test_offset_time_grid_is_floor_and_strictly_monotonic() -> None:
    g = offset_time_ns(N)
    assert g.dtype == np.uint32 and np.array_equal(g, np.arange(N) * 5000)
    for n in (24_000, 7, 33_333):                                         # 不整除时取 ⌊i·10⁸/N⌋
        g = offset_time_ns(n)
        assert np.array_equal(g.astype(np.int64), np.arange(n) * 100_000_000 // n) and (np.diff(g.astype(np.int64)) > 0).all()
        assert int(g[-1]) < 100_000_000
    with pytest.raises(ValueError):
        offset_time_ns(0)


def test_full_slots_zero_placeholders_and_slot_mapping() -> None:
    p = scan_points(1)
    full = full_slots(p, N)
    assert full.dtype == CUSTOM_POINT and len(full) == N and CUSTOM_POINT.itemsize == 19
    idx = p["offset_time"].astype(np.int64) // 5000
    assert np.array_equal(full["offset_time"], offset_time_ns(N))
    assert np.array_equal(full["x"][idx], p["x"]) and np.array_equal(full["reflectivity"][idx], p["reflectivity"])
    hole = np.ones(N, bool)
    hole[idx] = False
    assert (full["x"][hole] == 0).all() and (full["y"][hole] == 0).all() and (full["z"][hole] == 0).all()
    assert (full["reflectivity"][hole] == 0).all() and (full["tag"][hole] == 0).all()
    assert np.array_equal(full["line"], np.arange(N) & 3)                 # 无回波槽位保留光束号（FAST-LIVO2 只按 line 过滤）
    assert check_custom_msg(unpack_custom_msg(pack_custom_msg(0, NS, full)), N) == len(p)
    # 非整除槽位数：反解 i = ⌈ot·N/10⁸⌉ 与网格逐点一致
    q = scan_points(0, n_slots=24_000, every=7)
    f2 = full_slots(q, 24_000)
    assert np.array_equal(f2["x"][np.arange(0, 24_000, 7)], q["x"])
    assert len(full_slots(np.zeros(0, LIDAR_RECORD), N)) == N


@pytest.mark.parametrize("mutate", ["off_grid", "unsorted", "duplicate", "out_of_range", "nan"])
def test_full_slots_rejects_points_off_the_slot_grid(mutate: str) -> None:
    p = scan_points(0)
    if mutate == "off_grid":
        p["offset_time"][5] += 1
    elif mutate == "unsorted":
        p[[3, 4]] = p[[4, 3]]
    elif mutate == "duplicate":
        p["offset_time"][4] = p["offset_time"][3]
    elif mutate == "out_of_range":
        p["offset_time"][-1] = 100_000_000
    else:
        p["x"][2] = np.nan
    with pytest.raises(SensorSvcError) as e:
        full_slots(p, N)
    assert e.value.code == 642


def test_custom_msg_wire_layout() -> None:
    full = full_slots(scan_points(2), N)
    t = 1_234_567_890_123
    b = pack_custom_msg(7, t, full)
    fid = b"livox_frame"
    head = 16 + len(fid)
    assert len(b) == head + 8 + 4 + 1 + 3 + 4 + 19 * N
    assert struct.unpack_from("<IIII", b, 0) == (7, 1234, 567_890_123, len(fid)) and b[16:head] == fid
    assert struct.unpack_from("<QIB3BI", b, head) == (t, N, 0, 0, 0, 0, N)
    ot, x, y, z, r, tag, line = struct.unpack_from("<I3f3B", b, head + 20 + 19 * 4)
    assert (ot, x, y, z, r, tag, line) == (20_000, 0.0, 0.0, 0.0, 0, 0, 0)        # 槽位 4 无回波
    ot, x, *_ = struct.unpack_from("<I3f3B", b, head + 20 + 19 * 5)
    assert ot == 25_000 and x == np.float32(full["x"][5]) != 0.0                   # 槽位 5 有回波
    m = unpack_custom_msg(b)
    assert m["header"] == {"seq": 7, "stamp_ns": t, "frame_id": "livox_frame"} and m["timebase"] == t and m["point_num"] == N
    assert np.array_equal(m["points"], full)


def test_imu_message_in_g_and_unknown_orientation(tmp_path: Path) -> None:
    b = pack_imu(3, 5 * NS, (0.1, 0.2, 0.3), (0.0, 0.0, 1.0))
    m = unpack_imu(b)
    assert m["header"]["stamp_ns"] == 5 * NS and m["orientation"] == (0.0, 0.0, 0.0, 1.0) and m["orientation_covariance"][0] == -1.0
    assert m["angular_velocity"] == (0.1, 0.2, 0.3) and m["linear_acceleration"] == (0.0, 0.0, 1.0)
    with SlamBagWriter(tmp_path / "i.bag", camera=False) as w:              # m/s² → g
        w.imu(0, (0, 0, 0), (0.0, 0.0, G))
    with Bag1Reader(tmp_path / "i.bag") as rd:
        (msg,) = list(rd.messages())
    assert unpack_imu(msg.data)["linear_acceleration"] == (0.0, 0.0, 1.0)


def test_imu_samples_from_synth(tmp_path: Path) -> None:
    pytest.importorskip("scipy")
    from awr.sensorsvc.imu import ImuModel, synth_imu

    t = np.arange(0, 2 * NS + 1, NS // 250, dtype=np.int64)
    w = np.zeros((len(t), 3))
    f = np.tile([0.0, 0.0, G], (len(t), 1))
    s = synth_imu(t, w, f, R_bi=np.eye(3), r_imu_b=np.zeros(3), model=ImuModel(), t_from_ns=0, t_to_ns=2 * NS, tier="clean")
    with SlamBagWriter(tmp_path / "s.bag", camera=False) as bw:
        assert bw.imu_samples(s) == 400
        bw.lidar_frame(0, scan_points(0))
    rep = verify_slam_bag(tmp_path / "s.bag")
    assert rep.imu_samples == 400 and rep.imu_t1_ns - rep.imu_t0_ns == 399 * 5_000_000 and rep.acc_max_g == pytest.approx(1.0, abs=1e-12)


# ---------------------------------------------------------------- bag 结构与读回
@pytest.mark.parametrize("compression,threshold", [("none", 768 * 1024), ("none", 4096), ("bz2", 200_000)])
def test_slam_bag_roundtrip_and_verify(tmp_path: Path, compression: str, threshold: int) -> None:
    p = tmp_path / "sim.bag"
    info = write_bag(p, compression=compression, chunk_threshold=threshold)
    raw = p.read_bytes()
    assert raw.startswith(BAG_MAGIC)
    (hl,) = struct.unpack_from("<I", raw, len(BAG_MAGIC))
    (dl,) = struct.unpack_from("<I", raw, len(BAG_MAGIC) + 4 + hl)
    assert 4 + hl + 4 + dl == BAG_HEADER_LEN                              # 定长文件头记录
    with Bag1Reader(p) as rd:
        tops = rd.topics()
        assert {t: c.md5sum for t, c in tops.items()} == {LIDAR_TOPIC: LIVOX_CUSTOM_MSG_MD5, IMU_TOPIC: IMU_MD5,
                                                          IMAGE_TOPIC: COMPRESSED_IMAGE_MD5}
        assert tops[LIDAR_TOPIC].msgtype == "livox_ros_driver/CustomMsg"
        assert rd.message_counts() == {IMU_TOPIC: 201, LIDAR_TOPIC: 10, IMAGE_TOPIC: 10}
        assert len(rd.chunks) >= (10 if threshold == 4096 else 2)         # 每帧 380 KB，10 帧必然跨多个 chunk
        lid = [unpack_custom_msg(m.data) for m in rd.messages([LIDAR_TOPIC])]
        assert [m["header"]["seq"] for m in lid] == list(range(10))
        assert all(np.array_equal(m["points"], full_slots(scan_points(k), N)) for k, m in enumerate(lid))
        img = [unpack_compressed_image(m.data) for m in rd.messages([IMAGE_TOPIC])]
        assert all(m["format"] == "jpeg" and m["data"] == JPEG for m in img)
    rep = verify_slam_bag(p)
    assert (rep.lidar_frames, rep.imu_samples, rep.images) == (10, 201, 10)
    assert rep.returns_mean == pytest.approx(np.mean(info["returns"]))
    assert rep.lidar_t0_ns == 2 * NS and rep.lidar_t1_ns == 2 * NS + 9 * 100_000_000 and rep.imu_t0_ns == 2 * NS


def test_driver2_type_name_has_same_md5(tmp_path: Path) -> None:
    write_bag(tmp_path / "d2.bag", frames=2, lidar_type="livox_ros_driver2/CustomMsg")
    rep = verify_slam_bag(tmp_path / "d2.bag")
    assert rep.types[LIDAR_TOPIC] == "livox_ros_driver2/CustomMsg" and rep.md5[LIDAR_TOPIC] == LIVOX_CUSTOM_MSG_MD5


def test_out_of_order_writes_are_indexed_by_time(tmp_path: Path) -> None:
    p = tmp_path / "o.bag"
    with Bag1Writer(p) as w:
        c = w.add_connection(IMU_TOPIC, "sensor_msgs/Imu")
        for t in (30, 10, 20, 10):
            w.write(c, t, pack_imu(t, t, (0, 0, 0), (0, 0, 1)))
    with Bag1Reader(p) as rd:
        assert [m.t_ns for m in rd.messages()] == [30, 10, 20, 10]          # 文件顺序；索引按时刻稳定排序（已在读回中核对）
        assert (rd.chunks[0].start_ns, rd.chunks[0].end_ns) == (10, 30)


# ---------------------------------------------------------------- 负向用例（642）
def _bag_with_frames(path: Path, frames: list[bytes], *, lidar_type: str = "livox_ros_driver/CustomMsg", md5: str | None = None,
                     images: tuple[int, ...] = ()) -> Path:
    with Bag1Writer(path) as w:
        c_l = w.add_connection(LIDAR_TOPIC, lidar_type, md5sum=md5)
        c_i = w.add_connection(IMU_TOPIC, "sensor_msgs/Imu")
        for k in range(len(frames) * 20 + 1):
            w.write(c_i, k * 5_000_000, pack_imu(k, k * 5_000_000, (0, 0, 0), (0, 0, 1)))
        for k, b in enumerate(frames):
            w.write(c_l, k * 100_000_000, b)
        if images:
            from awr.sensorsvc.slam.bag import pack_compressed_image

            c_c = w.add_connection(IMAGE_TOPIC, "sensor_msgs/CompressedImage")
            for t in images:
                w.write(c_c, t, pack_compressed_image(0, t, JPEG))
    return path


def _reject(path: Path, needle: str) -> None:
    with pytest.raises(SensorSvcError) as e:
        verify_slam_bag(path)
    assert e.value.code == 642 and needle in e.value.detail, e.value.detail


def test_bag_with_dropped_no_return_points_is_rejected(tmp_path: Path) -> None:
    """r1 §2.10-1 的缺陷形态：只写有回波点（相邻 offset 间隙 ≥ 1 ms 会触发 preprocess.cpp:183 的级联时间修正）。"""
    p = scan_points(0, every=3)
    dropped = np.zeros(len(p), CUSTOM_POINT)
    for k in ("offset_time", "x", "y", "z", "reflectivity", "tag", "line"):
        dropped[k] = p[k]
    _reject(_bag_with_frames(tmp_path / "drop.bag", [pack_custom_msg(0, 0, dropped)]), "expected all 20000 slots")


def test_contract_violations_are_rejected(tmp_path: Path) -> None:
    good = full_slots(scan_points(0), N)
    _reject(_bag_with_frames(tmp_path / "md5.bag", [pack_custom_msg(0, 0, good)], md5="0" * 32), "md5")
    _reject(_bag_with_frames(tmp_path / "type.bag", [pack_custom_msg(0, 0, good)], lidar_type="sensor_msgs/Imu"), "type")
    bad = good.copy()
    bad["reflectivity"][1] = 7                                              # 槽位 1 为零点占位
    _reject(_bag_with_frames(tmp_path / "ph.bag", [pack_custom_msg(0, 0, bad)]), "zero placeholder")
    bad = good.copy()
    bad["offset_time"][10:] += 1
    _reject(_bag_with_frames(tmp_path / "ot.bag", [pack_custom_msg(0, 0, bad)]), "offset_time")
    bad = good.copy()
    bad["line"][0] = 4
    _reject(_bag_with_frames(tmp_path / "line.bag", [pack_custom_msg(0, 0, bad)]), "line")
    b = bytearray(pack_custom_msg(0, 0, good))
    struct.pack_into("<Q", b, 16 + len(b"livox_frame"), 1)                 # timebase ≠ 帧首
    _reject(_bag_with_frames(tmp_path / "tb.bag", [bytes(b)]), "timebase")
    _reject(_bag_with_frames(tmp_path / "per.bag", [pack_custom_msg(0, 0, good), pack_custom_msg(1, 200_000_000, good)]), "header.stamp")
    _reject(_bag_with_frames(tmp_path / "cam.bag", [pack_custom_msg(0, 0, good)], images=(250_000_000,)), "camera")


def test_frame_and_imu_period_checks(tmp_path: Path) -> None:
    good = full_slots(scan_points(0), N)
    p = tmp_path / "gap.bag"
    with Bag1Writer(p) as w:
        c_l = w.add_connection(LIDAR_TOPIC, "livox_ros_driver/CustomMsg")
        c_i = w.add_connection(IMU_TOPIC, "sensor_msgs/Imu")
        for t in (0, 5_000_000, 15_000_000):
            w.write(c_i, t, pack_imu(0, t, (0, 0, 0), (0, 0, 1)))
        for t in (0, 200_000_000):
            w.write(c_l, t, pack_custom_msg(0, t, good))
    _reject(p, "period")
    rep = verify_slam_bag(p, frame_period_ns=None, imu_period_ns=None)
    assert rep.lidar_frames == 2 and rep.imu_samples == 3


def test_corrupted_or_unclosed_bag_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.bag"
    write_bag(p, frames=3)
    raw = bytearray(p.read_bytes())
    with Bag1Reader(p) as rd:
        ci = rd.chunks[0]
    # 改写 chunk 内首条 IMU 消息的时刻 → 与索引不一致
    off = raw.find(b"time=", ci.pos)
    raw[off + 5] ^= 0x01
    (tmp_path / "t.bag").write_bytes(raw)
    with pytest.raises(BagFormatError), Bag1Reader(tmp_path / "t.bag") as rd:
        list(rd.messages())
    _reject(tmp_path / "t.bag", "bag structure")
    (tmp_path / "trunc.bag").write_bytes(p.read_bytes()[:-10])
    _reject(tmp_path / "trunc.bag", "bag structure")
    w = Bag1Writer(tmp_path / "open.bag")
    w.add_connection(IMU_TOPIC, "sensor_msgs/Imu")
    w._f.flush()                                                            # 未 close：index_pos = 0
    with pytest.raises(BagFormatError, match="not closed"):
        Bag1Reader(tmp_path / "open.bag")
    w.close()
    with pytest.raises(BagFormatError):
        (tmp_path / "x.bag").write_bytes(b"#ROSBAG V1.2\n")
        Bag1Reader(tmp_path / "x.bag")


def test_cli_reports_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from awr.sensorsvc.slam.bag import main

    write_bag(tmp_path / "ok.bag", frames=2)
    assert main([str(tmp_path / "ok.bag")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["lidar_frames"] == 2 and out["md5"][LIDAR_TOPIC] == LIVOX_CUSTOM_MSG_MD5
    p = scan_points(0)
    d = np.zeros(len(p), CUSTOM_POINT)
    d["offset_time"] = p["offset_time"]
    assert main([str(_bag_with_frames(tmp_path / "bad.bag", [pack_custom_msg(0, 0, d)]))]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == 642


# ---------------------------------------------------------------- 与 MeshLidar 的集成（D3 环境）
def test_mesh_lidar_scan_exports_full_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("open3d")
    pytest.importorskip("PIL")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world"))
    from synthworld import build_synth_world

    from awr.sensorsvc.lidar.mesh import MeshLidar
    from awr.sensorsvc.lidar.model import load_lidar_model
    from awr.sim.sensors.frames import mount_R
    from awr.world.mesh.query import open_mesh_world_query

    monkeypatch.setenv("AWR_D3_ENABLED", "1")
    q = open_mesh_world_query(build_synth_world(tmp_path / "w", monkeypatch), tmp_path / "cache")
    m = load_lidar_model()
    R = mount_R((180.0, 0.0, 0.0))                                          # 倒装 MID-360
    lid = MeshLidar(q, m)
    with SlamBagWriter(tmp_path / "m.bag", slots=m.slots_per_frame, camera=False) as w:
        for k in range(21):
            w.imu(k * 5_000_000, (0, 0, 0), (0, 0, G))
        for k in range(2):
            t0 = k * m.frame_ns
            nd = [(R, np.array([20.0 + 0.6 * k + 0.006 * j, -40.0, 30.0])) for j in range(m.pose_nodes)]
            sc = lid.scan(k, t0, nd, tier="realistic", seed=5)
            assert 0 < len(sc.points) < m.slots_per_frame
            w.lidar_frame(t0, sc.points)
    rep = verify_slam_bag(tmp_path / "m.bag")
    assert rep.lidar_frames == 2 and 0 < rep.returns_mean < m.slots_per_frame
    with Bag1Reader(tmp_path / "m.bag") as rd:
        msg = unpack_custom_msg(next(rd.messages([LIDAR_TOPIC])).data)
    gaps = np.diff(msg["points"]["offset_time"].astype(np.int64))
    assert gaps.max() == 5000                                               # 整帧相邻点只差 5 µs，不会触发 ≥ 1 ms 的时间修正
