"""M27 录制与帧读取（§7.5.1、§7.5.3；AWR-17 §18.8；M27-FR-051、052；API-FR-114）：写入、frames.idx、单帧、索引页与区间容器。"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")

from awr.bench.recorder.frames_idx import FramesIdx, build_frames_idx
from awr.bench.recorder.read import FrameLimitError, FrameStore
from awr.bench.recorder.writer import RunWriter

ROOT = Path(__file__).resolve().parents[3]


def _jpeg(i: int) -> bytes:
    """不可压缩的伪 JPEG（4 KB 量级），使 frames.idx 与 raw.mcap 的体积比接近真实帧。"""
    return b"\xff\xd8" + np.random.default_rng(i).bytes(4000 + i) + b"\xff\xd9"


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run-0001"
    with RunWriter(d / "raw.mcap") as w:
        w.metadata("run", {"run_id": "run-0001"})
        for seq in range(1, 41):
            t = seq * 100_000_000 - 4_000_000
            for uav in ("uav-02", "uav-01"):
                depth = np.full((4, 6), float(seq), np.float32) if seq % 10 == 0 else None
                w.write_camera(uav, "cam0", seq, t, _jpeg(seq + (uav == "uav-02") * 7), depth_m=depth, period_ns=100_000_000)
    build_frames_idx(d / "raw.mcap", d / "frames.idx")
    return d


def test_frames_idx_layout_and_order(run_dir: Path) -> None:
    b = (run_dir / "frames.idx").read_bytes()
    assert b[:8] == b"D3FIDX01" and hashlib.sha256(b[:-32]).digest() == b[-32:]
    fi = FramesIdx.open(run_dir / "frames.idx")
    assert fi.header["uavs"] == ["uav-01", "uav-02"] and fi.header["sensors"] == ["cam0", "cam0.depth"]
    assert fi.header["n_records"] == 2 * 40 + 2 * 4
    k = fi.key
    assert np.all(k[1:] > k[:-1])
    assert len(b) - 32 - 12 - struct.unpack_from("<I", b, 8)[0] == 32 * fi.header["n_records"]
    raw = run_dir / "raw.mcap"
    assert fi.header["raw_mcap_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert len(b) < 0.02 * raw.stat().st_size                                  # 4 KB 测试帧；真实 JPEG 约 200 KB 时约 0.02%
    bad = bytearray(b)
    bad[20] ^= 1
    (run_dir / "bad.idx").write_bytes(bytes(bad))
    with pytest.raises(ValueError, match="trailer"):
        FramesIdx.open(run_dir / "bad.idx")


def test_single_frame_and_index_page(run_dir: Path) -> None:
    fs = FrameStore(run_dir)
    f = fs.frame("uav-02", "cam0", 13)
    assert f.payload == _jpeg(20) and f.t_ns == 1_296_000_000 and f.content_type == "image/jpeg"
    assert fs.frame("uav-01", "cam0", 41) is None and fs.frame("uav-09", "cam0", 1) is None
    d = fs.frame("uav-01", "cam0.depth", 20)
    assert (d.width, d.height, d.codec) == (6, 4, "32FC1")
    assert np.frombuffer(d.payload, "<f4").reshape(4, 6)[0, 0] == 20.0
    page = fs.index_page("uav-01", "cam0", 5, 7)
    schema = json.loads((ROOT / "packages/contracts/bench/frame_index.v1.schema.json").read_text())
    import jsonschema

    jsonschema.Draft202012Validator(schema).validate(page)
    assert [i["seq"] for i in page["items"]] == [5, 6, 7] and page["next_seq"] is None
    assert page["items"][0]["sha256"] == hashlib.sha256(_jpeg(5)).hexdigest() and page["period_ns"] == 100_000_000
    assert fs.index_page("uav-01", "cam0", 39)["items"][-1]["seq"] == 40
    assert fs.latest_seq("uav-02", "cam0") == 40
    fs.close()


def test_range_container_layout_stride_and_limits(run_dir: Path) -> None:
    fs = FrameStore(run_dir)
    b = fs.range_container("uav-01", "cam0", 2, 11, stride=3)
    assert b[:4] == b"D3FR" and struct.unpack_from("<HH", b, 4) == (1, 0)
    (hl,) = struct.unpack_from("<I", b, 8)
    head = json.loads(b[12:12 + hl])
    assert head["n"] == 4 and head["stride"] == 3
    p = 12 + hl
    seqs = []
    while p < len(b) - 32:
        seq, _t, n = struct.unpack_from("<IqI", b, p)
        sha = b[p + 16:p + 48]
        payload = b[p + 48:p + 48 + n]
        assert hashlib.sha256(payload).digest() == sha and payload == _jpeg(seq)
        seqs.append(seq)
        p += 48 + n
    assert seqs == [2, 5, 8, 11] and hashlib.sha256(b[:-32]).digest() == b[-32:]
    with pytest.raises(FrameLimitError):
        fs.range_container("uav-01", "cam0", 0, 300)
    assert fs.range_container("uav-01", "cam0", 0, 599, stride=2) is not None
    fs.close()


def test_depth_archive_is_lossless_bshuf4_zstd(tmp_path: Path) -> None:
    """L2-depth 存档为 f32 + 字节重排 + zstd（AWR-05 §1154）：解码逐位相同（含 0、inf、NaN），平滑深度明显变小；未压缩的旧 Run 仍可读。"""
    from mcap.reader import make_reader

    from awr.bench.recorder.depthcodec import bshuf4_unzstd, bshuf4_zstd

    y, x = np.mgrid[0:180, 0:320]
    depth = (5.0 + 0.05 * x + 0.2 * y).astype(np.float32)
    depth[:20] = 0.0
    depth[20, :3] = [np.inf, np.nan, -0.0]
    raw = depth.astype("<f4").tobytes()
    assert bshuf4_unzstd(bshuf4_zstd(raw)) == raw
    d = tmp_path / "run-d"
    with RunWriter(d / "raw.mcap") as w:
        w.write_camera("uav-01", "cam0", 1, 100_000_000, _jpeg(1), depth_m=depth)
    build_frames_idx(d / "raw.mcap", d / "frames.idx")
    with open(d / "raw.mcap", "rb") as fh:
        msgs = [(c, m) for _s, c, m in make_reader(fh).iter_messages() if c.topic.endswith("/depth")]
    assert msgs[0][0].metadata["compression"] == "bshuf4-zstd" and len(msgs[0][1].data) < len(raw) / 3
    fs = FrameStore(d)
    f = fs.frame("uav-01", "cam0.depth", 1)
    assert f is not None and f.payload == raw and (f.width, f.height) == (320, 180)
    page = fs.index_page("uav-01", "cam0.depth")
    assert page["items"][0]["bytes"] == len(raw) and page["items"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    fs.close()

    plain = tmp_path / "run-p"
    with RunWriter(plain / "raw.mcap", depth_compression=None) as w:
        w.write_camera("uav-01", "cam0", 1, 100_000_000, _jpeg(1), depth_m=depth)
    build_frames_idx(plain / "raw.mcap", plain / "frames.idx")
    fs = FrameStore(plain)
    assert fs.frame("uav-01", "cam0.depth", 1).payload == raw
    fs.close()
