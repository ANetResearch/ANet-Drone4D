"""M27 录制（§6.7.4、§7.5；M27-FR-040、049；M27-AC-006、008；D3-AC-19）：Env 运行写 `raw.mcap` 与真值 MCAP、写入背压阻塞生产者且丢帧 0。

- 写入器单元：异步队列满时阻塞生产者并记录阻塞次数与时长（每条延迟注入与 20 MB/s 量级限速两种慢下游），消息不丢、顺序不变；
  写线程异常在调用方抛出；入队后调用方修改数组不影响存档；
- Env L2 / L2-depth 运行：Agent 收到的 RGB、深度、LiDAR、IMU、GNSS 字节与存档逐字节一致（sha256），帧数据只在 `raw.mcap` 中保存一份
  （`frames.idx` 只含偏移），磁带与世界清单、`run_spec.json` 为附件，真值通道只在 `truth/run_<id>.mcap`；
- 有背压与无背压两次运行的 `(uav, sensor, seq)` 集合与帧字节相同，清单记录 `writer_block_s` 与阻塞次数，`dropped = 0`；
- 排名运行的 `run_spec.json` 附件与 MCAP 元数据不含种子。

渲染以 CPU 替身代替 EGL 光栅相机（`tests/d3/env/test_l2.FakeRaster`；真实渲染在 dmax 上验证）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")

from awr.bench.recorder.frames_idx import FramesIdx, build_frames_idx
from awr.bench.recorder.read import FrameStore
from awr.bench.recorder.writer import RunWriter, WriterError, channel_counts

ROOT = Path(__file__).resolve().parents[3]
ENV_TESTS = ROOT / "tests" / "d3" / "env"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _jpeg(i: int, n: int = 4000) -> bytes:
    return b"\xff\xd8" + np.random.default_rng(i).bytes(n + i) + b"\xff\xd9"


def _messages(path: Path) -> list[tuple[str, int, int, bytes]]:
    from mcap.reader import make_reader

    with path.open("rb") as f:
        return [(c.topic, m.sequence, m.log_time, bytes(m.data)) for _s, c, m in make_reader(f).iter_messages(log_time_order=False)]


def _attachments(path: Path) -> dict[str, bytes]:
    from mcap.reader import make_reader

    with path.open("rb") as f:
        return {a.name: bytes(a.data) for a in make_reader(f).iter_attachments()}


def _metadata(path: Path) -> dict[str, dict]:
    from mcap.reader import make_reader

    with path.open("rb") as f:
        return {m.name: dict(m.metadata) for m in make_reader(f).iter_metadata()}


# ---------------------------------------------------------------------- 写入器单元


def test_async_writer_blocks_producer_and_loses_nothing(tmp_path: Path) -> None:
    """在途上限 2 条、写线程每条注入 20 ms：生产者被阻塞（墙钟 ≥ 注入总量的 80%），阻塞次数与时长有记录；40 帧全部按序写出。"""
    n, delay = 40, 0.02
    d = tmp_path / "run-bp"
    w = RunWriter(d / "raw.mcap", queue_msgs=2, inject_delay_s=delay)
    t0 = time.perf_counter()
    for seq in range(1, n + 1):
        w.write_camera("uav-01", "eo", seq, seq * 100_000_000, _jpeg(seq), period_ns=100_000_000)
    t_produce = time.perf_counter() - t0
    w.close()
    st = w.summary()
    assert st["mode"] == "async" and st["enqueued"] == st["written"] == n and st["dropped"] == 0
    assert st["blocked"] >= n - 3 and st["block_s"] >= 0.8 * (n - 3) * delay and t_produce >= 0.8 * (n - 3) * delay
    assert st["max_depth"] <= 2 and len(st["blocks"]) == st["blocked"] and st["blocks"][0][1] > 0
    assert channel_counts(d / "raw.mcap") == {"/uav/uav-01/camera/eo/image": n}
    msgs = _messages(d / "raw.mcap")
    assert [m[1] for m in msgs] == list(range(1, n + 1))                       # 入队顺序即写出顺序
    build_frames_idx(d / "raw.mcap", d / "frames.idx")
    fs = FrameStore(d)
    assert all(fs.frame("uav-01", "eo", s).payload == _jpeg(s) for s in range(1, n + 1))
    fs.close()


def test_rate_limited_writer_backpressure(tmp_path: Path) -> None:
    """写入速率上限 5 MB/s（慢磁盘）× 1 MB 载荷：总墙钟 ≥ 0.18 s，生产者阻塞，消息数与字节不变。"""
    p = tmp_path / "r.mcap"
    w = RunWriter(p, queue_msgs=4, queue_bytes=200_000, rate_Bps=5e6)
    t0 = time.perf_counter()
    for seq in range(20):
        w.write_lidar("uav-01", "lidar0", seq, seq * 100_000_000, {"point_count": 2500}, np.zeros(2500, _lidar_dtype()))
    w.close()
    wall = time.perf_counter() - t0
    st = w.summary()
    assert wall >= 0.18 and st["blocked"] > 0 and st["written"] == 20 and st["dropped"] == 0
    assert st["max_depth_bytes"] <= 200_000 and st["bytes"] >= 20 * 2500 * 20
    assert channel_counts(p) == {"/uav/uav-01/lidar/lidar0": 20}


def _lidar_dtype():
    from awr.bench.recorder.lidar import LIDAR_RECORD

    return LIDAR_RECORD


def test_async_writer_snapshots_arrays_and_surfaces_errors(tmp_path: Path) -> None:
    d = tmp_path / "run-s"
    depth = np.full((4, 6), 3.0, np.float32)
    w = RunWriter(d / "raw.mcap", queue_msgs=8, inject_delay_s=0.01)
    w.write_camera("uav-01", "eo", 1, 100_000_000, _jpeg(1), depth_m=depth)
    depth[:] = -1.0                                                            # 入队后修改：存档仍为入队时的值
    w.close()
    build_frames_idx(d / "raw.mcap", d / "frames.idx")
    fs = FrameStore(d)
    assert np.all(np.frombuffer(fs.frame("uav-01", "eo.depth", 1).payload, "<f4") == 3.0)
    fs.close()

    w = RunWriter(tmp_path / "bad.mcap", queue_msgs=4)
    calls = {"n": 0}
    real = w._w.add_message

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        return real(**kw)

    w._w.add_message = flaky
    errs = []
    for seq in range(50):                                                      # 第 3 条写失败后，后续写入在调用方线程抛出
        try:
            w.write_json("/net/records", "awr.bench.net_record.v1", seq, seq, b"{}")
        except WriterError as e:
            errs.append(e)
            break
        time.sleep(0.002)
    assert errs and "No space left" in str(errs[0]) and isinstance(errs[0].__cause__, OSError)
    with pytest.raises(WriterError):
        w.close()
    assert w.closed


# ---------------------------------------------------------------------- Env 运行

_conf = _load("d3_env_conftest_for_recorder", ENV_TESTS / "conftest.py")
synth_event = _conf.synth_event
RAW_OK = ("/uav/{u}/camera/eo/image", "/uav/{u}/camera/eo/depth", "/uav/{u}/lidar/lidar0", "/uav/{u}/imu/lidar0_imu", "/uav/{u}/gnss/gnss0",
          "/uav/{u}/obs/nav", "/uav/{u}/action", "/uav/{u}/report")


def _episode(synth_event, out: Path, obs_level: str = "L2", duration_s: float = 2.0, *, ranked: bool = False, seed: int = 3):
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.spec import EnvSpec

    t_l2 = _load("d3_env_test_l2_for_recorder", ENV_TESTS / "test_l2.py")
    w, ev = synth_event
    spec = EnvSpec("t-rec", seed, w, ev, t_l2.FLEET, obs_level=obs_level, physics="kinematic", duration_s=duration_s, out_dir=out,
                   ranked=ranked, seed_commit="hmac:0123abcd" if ranked else None)
    env = BenchEnv(spec, l2=lambda e: t_l2._l2(e, False))
    obs, _ = env.reset()
    seen = list(obs.items())
    while env.agents:
        acts = {u: {"nav": {"kind": "goto", "target_enu": [0.0, 60.0, 30.0], "speed_mps": 4.0}, "gimbal": {"pitch_deg": -45.0}} for u in obs}
        obs, *_ = env.step(acts)
        seen += list(obs.items())
    man = env.close()
    return seen, man, env


def _delivered(seen) -> dict[tuple[str, str, int], dict]:
    out = {}
    for u, msg in seen:
        for sid, fs in msg["obs"]["sensors"].items():
            for f in fs:
                key = (u, sid, int(f["seq"]))
                assert key not in out                                            # 每帧只交付一次
                out[key] = f
    return out


def _archive_digest(run: Path) -> dict[tuple[str, str, int], str]:
    """存档中每帧交付字节的 sha256（相机经 FrameStore 解封装；LiDAR、IMU、GNSS 取消息字节）。"""
    out = {}
    fs = FrameStore(run)
    for u, s in fs.idx.channels():
        for r in fs.idx.span(u, s, 0, 2 ** 32 - 1):
            out[(u, s, int(r["seq"]))] = fs.frame(u, s, int(r["seq"])).sha256
    fs.close()
    for topic, seq, _t, data in _messages(run / "raw.mcap"):
        m = re.match(r"^/uav/([^/]+)/(imu|gnss)/([A-Za-z0-9_]+)$", topic)
        if m:
            out[(m.group(1), m.group(3), seq)] = hashlib.sha256(data).hexdigest()
    return out


def test_env_l2_writes_raw_mcap_with_delivered_bytes(synth_event, tmp_path: Path) -> None:
    import msgpack

    from awr.bench.recorder.lidar import unpack_lidar_frame
    from awr.sim.events.tape import TapeReader

    run = tmp_path / "run-a"
    seen, man, env = _episode(synth_event, run)
    got = _delivered(seen)
    rec = man["recorder"]
    assert rec["dropped"] == 0 and rec["raw"]["mismatch"] == {} and rec["truth"]["mismatch"] == {}
    assert json.loads((run / "manifest.json").read_text())["recorder"]["dropped"] == 0
    assert sum(rec["frames"].values()) == len(got)                             # 各通道帧数等于 Env 交付计数

    # Agent 收到的字节与存档逐字节一致（sha256）
    fs = FrameStore(run)
    n_cam = n_lid = 0
    for (u, sid, seq), f in got.items():
        if "rgb" in f:
            a = fs.frame(u, sid, seq)
            assert a.sha256 == hashlib.sha256(f["rgb"]["data"]).hexdigest() and a.t_ns == f["t_mid_ns"]
            n_cam += 1
        elif "xyz_m" in f:
            h, pts = unpack_lidar_frame(fs.frame(u, sid, seq).payload)
            assert h["point_count"] == f["n"] and h["timebase_ns"] == f["t_start_ns"]
            assert np.ascontiguousarray(np.stack([pts["x"], pts["y"], pts["z"]], 1), "<f4").tobytes() == f["xyz_m"]
            assert pts["offset_time"].astype("<u4").tobytes() == f["offset_time_ns"]
            assert pts["reflectivity"].tobytes() == f["reflectivity"] and pts["tag"].tobytes() == f["tag"]
            n_lid += 1
    page = fs.index_page("uav-01", "eo")
    assert all(i["sha256"] == hashlib.sha256(got[("uav-01", "eo", i["seq"])]["rgb"]["data"]).hexdigest() for i in page["items"])
    fs.close()
    assert n_cam == 2 * 21 and n_lid == 2 * 20
    msgs = _messages(run / "raw.mcap")
    for topic, seq, _t, data in msgs:
        m = re.match(r"^/uav/([^/]+)/(imu|gnss)/([A-Za-z0-9_]+)$", topic)
        if m:
            assert msgpack.unpackb(data, raw=False) == got[(m.group(1), m.group(3), seq)]

    # 帧数据只在 raw.mcap 中保存一份：frames.idx 只含偏移，其余文件不含任何帧载荷
    fi = FramesIdx.open(run / "frames.idx")
    assert fi.header["n_records"] == n_cam + n_lid and (run / "frames.idx").stat().st_size == 12 + len(json.dumps(
        fi.header, separators=(",", ":"), sort_keys=True).encode()) + 32 * fi.header["n_records"] + 32
    blobs = [f["rgb"]["data"][100:164] for f in got.values() if "rgb" in f] + [f["xyz_m"][:64] for f in got.values() if "xyz_m" in f and f["n"] > 8]
    for p in run.rglob("*"):
        if p.is_file() and p.name not in ("raw.mcap",) and not p.is_relative_to(run / "wq"):
            b = p.read_bytes()
            assert not any(x in b for x in blobs), p

    # 通道白名单：raw.mcap 只含观测与运行层；真值只在 truth/run_<id>.mcap
    from awr.agentsdk.env.recorder import RAW_TOPIC_PATTERNS, TRUTH_TOPIC_PATTERNS

    raw_topics = set(channel_counts(run / "raw.mcap"))
    assert all(any(re.match(p, t) for p in RAW_TOPIC_PATTERNS) for t in raw_topics), raw_topics
    assert not any(re.match(p, t) for t in raw_topics for p in TRUTH_TOPIC_PATTERNS)
    assert {"/uav/uav-01/camera/eo/image", "/uav/uav-01/lidar/lidar0", "/uav/uav-01/imu/lidar0_imu", "/uav/uav-01/gnss/gnss0",
            "/uav/uav-01/obs/nav", "/uav/uav-01/action"} <= raw_topics
    tc = channel_counts(run / "truth" / "run_t-rec.mcap")
    assert all(any(re.match(p, t) for p in TRUTH_TOPIC_PATTERNS) for t in tc)
    n_tick = man["n_ticks"] + 1
    assert tc["/uav/uav-01/state"] == n_tick == tc["/uav/uav-02/state"]        # 真值位姿（每 tick 末）
    st = [msgpack.unpackb(d) for t, _s, _l, d in _messages(run / "truth" / "run_t-rec.mcap") if t == "/uav/uav-01/state"]
    fleet = np.load(run / "truth" / "fleet.npz")
    assert np.allclose([s["pos_enu_m"] for s in st], fleet["pos"][:, 0]) and st[-1]["t_ns"] == int(fleet["t_ns"][-1])

    # 附件：磁带原始字节（content_sha256 与 tape.json 一致）、世界清单、run_spec；元数据
    att = _attachments(run / "raw.mcap")
    assert set(att) == {"tape.bin", "tape.json", "world_manifest.json", "run_spec.json"}
    assert att["tape.bin"] == Path(env.tape.path).read_bytes()
    tp = tmp_path / "tape-from-mcap.bin"
    tp.write_bytes(att["tape.bin"])
    assert TapeReader(tp).hashes()["content_sha256"] == json.loads(att["tape.json"])["content_sha256"] == man["event"]["content_sha256"]
    wm = json.loads(att["world_manifest.json"])
    assert wm["schema"] == "awr.world_manifest.v1" and wm["world_id"] == Path(env.spec.world_dir).name and wm["public"] is True
    assert json.loads(att["run_spec.json"]) == json.loads(json.dumps(env.spec.resolved()))
    md = _metadata(run / "raw.mcap")
    ident = env.spec.identity()
    assert md["run"]["run_id"] == "t-rec" and md["run"]["run_hash"] == ident["run_hash"] and md["run"]["config_sha256"] == ident["config_sha256"]
    assert "seed_commit" not in md["run"] and int(md["run.wall"]["ended_unix_ns"]) >= int(md["run.wall"]["started_unix_ns"])
    assert "channels.units" in md


def test_env_l2_depth_archives_delivered_depth(synth_event, tmp_path: Path) -> None:
    run = tmp_path / "run-d"
    seen, man, _env = _episode(synth_event, run, obs_level="L2-depth", duration_s=0.5)
    got = _delivered(seen)
    fs = FrameStore(run)
    n = 0
    for (u, sid, seq), f in got.items():
        if "depth" in f:
            a = fs.frame(u, f"{sid}.depth", seq)
            assert a.payload == f["depth"]["data"] and (a.width, a.height) == (f["width"], f["height"])
            n += 1
    fs.close()
    assert n == 2 * 6 and man["recorder"]["dropped"] == 0
    assert (run / "raw.mcap").stat().st_size < sum(len(f["depth"]["data"]) for f in got.values() if "depth" in f)   # 深度经无损压缩存档


def test_env_backpressure_blocks_and_keeps_frame_set(synth_event, tmp_path: Path, monkeypatch) -> None:
    """写入器背压（在途 2 条、每条注入 15 ms）：Env 主循环被阻塞并记录，`(uav, sensor, seq)` 集合与帧字节与无背压运行相同，丢帧 0。"""
    seen_a, man_a, _ = _episode(synth_event, tmp_path / "free", duration_s=1.0)
    monkeypatch.setenv("AWR_REC_QUEUE_MSGS", "2")
    monkeypatch.setenv("AWR_REC_INJECT_DELAY_MS", "15")
    seen_b, man_b, _ = _episode(synth_event, tmp_path / "bp", duration_s=1.0)
    ra, rb = man_a["recorder"], man_b["recorder"]
    assert ra["raw"]["blocked"] == 0 and ra["writer_block_s"] == 0.0
    assert rb["options"]["inject_delay_ms"] == 15.0 and rb["options"]["queue_msgs"] == 2
    assert rb["raw"]["blocked"] > 0 and rb["writer_block_s"] > 0.0 and man_b["writer_block_s"] == rb["writer_block_s"]
    assert rb["raw"]["max_depth"] <= 2 and rb["raw"]["blocks_head"]
    assert ra["dropped"] == rb["dropped"] == 0 and ra["frames"] == rb["frames"]
    da, db = _archive_digest(tmp_path / "free"), _archive_digest(tmp_path / "bp")
    assert da == db and len(da) > 0
    assert set(da) == set(_delivered(seen_a)) == set(_delivered(seen_b))
    assert channel_counts(tmp_path / "free" / "raw.mcap") == channel_counts(tmp_path / "bp" / "raw.mcap")


def test_ranked_run_spec_attachment_has_no_seed(synth_event, tmp_path: Path) -> None:
    """排名运行：`run_spec.json` 附件与 MCAP 元数据不含种子（十进制、十六进制与 8 字节小端），`seed_commit` 写入 `run` 元数据。"""
    from awr.agentsdk.env.recorder import EnvRecorder, RecorderOptions
    from awr.agentsdk.env.spec import EnvSpec, FleetEntry

    w, ev = synth_event
    seed = 0x5EED_1234_ABCD
    spec = EnvSpec("t-rank", seed, w, ev, (FleetEntry("uav-01", (0.0, 50.0, 30.0)),), obs_level="L2", ranked=True,
                   seed_commit="hmac:feedface", run_spec={"world": {"id": Path(w).name}, "seeds": [seed], "seed": seed, "obs_level": "L2",
                                                         "ranked": True})
    run = tmp_path / "run-r"
    rec = EnvRecorder(run, spec=spec, options=RecorderOptions(queue_msgs=4))
    rec.write_json("manifest.json", {"run_id": "t-rank"})
    rec.close()
    att = _attachments(run / "raw.mcap")
    rs = json.loads(att["run_spec.json"])
    assert "seed" not in rs and "seeds" not in rs and rs["ranked"] is True
    md = _metadata(run / "raw.mcap")
    assert md["run"]["seed_commit"] == "hmac:feedface" and md["run"]["ranked"] == "true"
    blob = (run / "raw.mcap").read_bytes()
    for needle in (str(seed).encode(), f"{seed:x}".encode(), seed.to_bytes(8, "little")):
        assert needle not in blob
