"""RGB JPEG 编码（M25-FR-060、183；M25 §6.7、§15.5；docs/05 §5.4.3；D3-AC-60 编码条款、D3-AC-11 编码前提）。

CPU 部分本机可跑：4:2:0、q90 量化表、在线路径禁止 CPU 编码、编码器选择规则、交付字节即存档字节。GPU 部分需要 nvImageCodec
与 CUDA（d3-sensorsvc 镜像，dmax 容器内运行；本机跳过）：nvJPEG 字节确定性与参数、两条 CUDA 流、3 机 × 1080p 在线路径零 CPU
编码调用、GL 渲染与 CUDA 编码在时间上重叠。
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from awr.sensorsvc.camera import CameraModel
from awr.sensorsvc.camera import encode as E
from awr.sensorsvc.poses import Mount

sys.path.insert(0, str(Path(__file__).resolve().parent))

# IJG 标准亮度 / 色度量化表（ITU-T T.81 附录 K，之字形之前的行序；Pillow 的 quantization 同为行序）
_STD_LUMA = [16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
             18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92, 49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100,
             103, 99]
_STD_CHROMA = [17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99, 24, 26, 56, 99, 99, 99, 99, 99, 47, 66, 99, 99, 99, 99, 99, 99,
               *([99] * 32)]


def _ijg_table(base: list[int], q: int) -> list[int]:
    s = 5000 // q if q < 50 else 200 - 2 * q
    return [min(max((b * s + 50) // 100, 1), 255) for b in base]


def _qtables(b: bytes) -> dict[int, list[int]]:
    from PIL import Image

    with Image.open(io.BytesIO(b)) as im:
        return {k: list(v) for k, v in im.quantization.items()}


def _smooth(h: int, w: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = 128 + 100 * np.sin(xx / 97.0) * np.cos(yy / 61.0)
    g = 255 * xx / w
    b = 255 * yy / h
    return np.clip(np.stack([r, g, b], -1), 0, 255).astype(np.uint8)


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    return float(10 * np.log10(255.0 ** 2 / np.mean((a.astype(np.float64) - b) ** 2)))


def _decode(b: bytes) -> np.ndarray:
    from PIL import Image

    with Image.open(io.BytesIO(b)) as im:
        return np.asarray(im.convert("RGB"))


def _frame_kw(cm: CameraModel, out, seq: int = 1, uav: str = "u1") -> dict:
    return {"uav": uav, "sensor_id": "cam0", "seq": seq, "t_mid_ns": 100_000_000 * seq, "exposure_ns": 1_000_000,
            "readout_ns": round(cm.readout_s * 1e9), "cm": cm, "mount": Mount(), "gimbal_reading": (0.0, -0.5, 0.0),
            "gimbal_true": (0.0, -0.5, 0.0), "pos_enu_m": np.array([0.0, 0.0, 60.0]), "R_ws": np.eye(3), "out": out,
            "outputs": {"rgb"}, "ref_prefix": f"{uav}/cam0/{seq}/"}


def _render_out(rgb: np.ndarray):
    from awr.sensorsvc.camera.raster import RenderOut

    h, w = rgb.shape[:2]
    return RenderOut(rgb, np.full((h, w), 50.0, np.float32), np.zeros((h, w), np.uint16), np.zeros((h, w), np.uint8), 0.0, 0.0, 1)


# ---------------------------------------------------------------- CPU（本机）
def test_cpu_encoder_is_q90_420_with_standard_tables() -> None:
    pytest.importorskip("PIL")
    rgb = _smooth(120, 160)
    e = E.CpuJpegEncoder()
    b = e.encode(rgb)
    assert b[:2] == b"\xff\xd8" and E.jpeg_subsampling(b) == "420"
    assert re.fullmatch(r"pillow-[\d.]+/jpeg/q90/420", e.identity)
    q = _qtables(b)
    assert q[0] == _ijg_table(_STD_LUMA, 90) and q[1] == _ijg_table(_STD_CHROMA, 90)
    assert _psnr(_decode(b), rgb) > 38.0
    assert e.encode(rgb) == b                                       # 同一像素数组 → 同一字节


def test_encode_jpeg_legacy_wrapper_is_420_and_guarded() -> None:
    pytest.importorskip("PIL")
    from awr.sensorsvc.frame import encode_jpeg

    rgb = _smooth(48, 64)
    assert E.jpeg_subsampling(encode_jpeg(rgb, 90)) == "420"
    n0 = E.cpu_encode_calls()
    with E.online_path(), pytest.raises(E.CpuEncodeForbidden):
        encode_jpeg(rgb, 90)
    assert E.cpu_encode_calls() == n0 + 1                           # 被拒绝的调用也计数
    assert not E.online_active()
    encode_jpeg(rgb, 90)                                            # 作用域外恢复


def test_online_guard_is_process_wide_and_nests() -> None:
    pytest.importorskip("PIL")
    rgb = _smooth(16, 16)
    e = E.CpuJpegEncoder()
    err: list[BaseException] = []

    def other() -> None:
        try:
            e.encode(rgb)
        except BaseException as x:                                 # 收集到主线程断言
            err.append(x)

    with E.online_path():
        with E.online_path():
            pass
        assert E.online_active()                                    # 内层退出不解除外层
        t = threading.Thread(target=other)
        t.start()
        t.join()
        with pytest.raises(E.CpuEncodeForbidden):
            e.submit(rgb)
    assert len(err) == 1 and isinstance(err[0], E.CpuEncodeForbidden) and isinstance(err[0], AssertionError)
    assert not E.online_active()


def test_encoder_selection_policy(monkeypatch) -> None:
    pytest.importorskip("PIL")

    class _NoGpu:
        def __init__(self, *a, **k) -> None:
            raise E.GpuEncoderUnavailable("no GPU in this test")

    monkeypatch.setattr(E, "GpuJpegEncoder", _NoGpu)
    monkeypatch.setattr(E, "_gpu_ok", False)
    monkeypatch.delenv(E.ENV_ENCODER, raising=False)
    assert isinstance(E.make_encoder("rgb720-gs", 1280, 720), E.CpuJpegEncoder)        # 离线 + 无 GPU：CPU 回退
    with pytest.raises(E.GpuEncoderUnavailable):
        E.make_encoder("rgb1080-rs", 1920, 1080)                                       # 1080p 只允许 GPU，不静默回退
    with pytest.raises(E.GpuEncoderUnavailable):
        E.make_encoder("rgb720-gs", 1280, 720, online=True)                            # 在线路径只允许 GPU
    with pytest.raises(E.CpuEncodeForbidden):
        E.make_encoder("rgb1080-rs", 1920, 1080, prefer="cpu")
    with E.online_path(), pytest.raises(E.CpuEncodeForbidden):
        E.make_encoder(None, 64, 48, prefer="cpu")
    monkeypatch.setenv(E.ENV_ENCODER, "cpu")
    assert isinstance(E.make_encoder("rgb720-gs", 1280, 720), E.CpuJpegEncoder)
    monkeypatch.setenv(E.ENV_ENCODER, "gpu")
    with pytest.raises(E.GpuEncoderUnavailable):
        E.make_encoder("rgb720-gs", 1280, 720)
    monkeypatch.setenv(E.ENV_ENCODER, "fast")
    with pytest.raises(ValueError):
        E.make_encoder("rgb720-gs", 1280, 720)


def test_camera_profile_of() -> None:
    from awr.sensorsvc.frame import camera_profile_of

    assert camera_profile_of(CameraModel.from_hfov(1920, 1080, 84.0, readout_s=0.02)) == "rgb1080-rs"
    assert camera_profile_of(CameraModel.from_hfov(1280, 720, 84.0)) == "rgb720-gs"
    assert camera_profile_of(CameraModel.from_hfov(1920, 1080, 84.0)) is None
    assert camera_profile_of(CameraModel.from_hfov(64, 48, 84.0)) is None


def test_camera_frame_delivered_bytes_are_archived_bytes(monkeypatch) -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("zstandard")
    from awr.sensorsvc.frame import FrameContext, camera_frame, camera_frame_deferred

    monkeypatch.setenv(E.ENV_ENCODER, "cpu")
    cm = CameraModel.from_hfov(64, 48, 84.0)
    out = _render_out(_smooth(48, 64))
    ctx = FrameContext("run-1", "sess-1", "v", "gpu", "drv")
    enc = E.CpuJpegEncoder()
    rec, blocks = camera_frame(ctx, encoder=enc, **_frame_kw(cm, out))
    ref = rec["agent_view"]["rgb"]["ref"]
    assert next(iter(blocks)) == ref and hashlib.sha256(blocks[ref]).hexdigest() == rec["agent_view"]["rgb"]["sha256"]
    assert rec["internal"]["encoder"] == enc.identity and E.jpeg_subsampling(blocks[ref]) == "420"
    rec0, blocks0 = camera_frame(ctx, **_frame_kw(cm, out))                          # 缺省：进程常驻编码器（此处强制 CPU）
    assert rec0["internal"]["encoder"] == enc.identity and blocks0[ref] == blocks[ref]
    tk = enc.submit(out.rgb8)                                                        # 预先提交的票据
    pend = camera_frame_deferred(ctx, rgb=tk, **_frame_kw(cm, out))
    assert pend.rec["agent_view"]["rgb"]["sha256"] == ""                             # 编码完成前不写摘要
    rec2, blocks2 = pend.result()
    assert blocks2[ref] is tk.result() and rec2["agent_view"]["rgb"]["sha256"] == rec["agent_view"]["rgb"]["sha256"]
    with pytest.raises(ValueError):
        camera_frame(ctx, rgb=blocks[ref], **_frame_kw(cm, out))                     # 只给字节而不给编码器身份
    rec3, _ = camera_frame(FrameContext("run-1", "sess-1", "v", "gpu", "drv", encoder="x-ext/jpeg/q90/420"), rgb=blocks[ref],
                           **_frame_kw(cm, out))
    assert rec3["internal"]["encoder"] == "x-ext/jpeg/q90/420"


def test_pipeline_yields_each_item_after_next_render() -> None:
    order: list[str] = []

    class _Rec(E.JpegEncoder):
        identity, kind = "rec/jpeg/q90/420", "cpu"

        def submit(self, rgb8):
            order.append(f"submit{int(rgb8[0, 0, 0])}")
            return E.EncodeTicket(_bytes=b"\xff\xd8", identity=self.identity)

    def render(i: int) -> np.ndarray:
        order.append(f"render{i}")
        return np.full((2, 2, 3), i, np.uint8)

    for it, _out, _tk in E.render_encode_pipeline(range(3), render, _Rec(), rgb_of=lambda o: o):
        order.append(f"yield{it}")
    assert order == ["render0", "submit0", "render1", "submit1", "yield0", "render2", "submit2", "yield1", "yield2"]


# ---------------------------------------------------------------- GPU（dmax 容器）
@pytest.fixture(scope="module")
def gpu():
    pytest.importorskip("PIL")
    try:
        import cuda.bindings.driver  # noqa: F401
        from nvidia import nvimgcodec  # noqa: F401
    except ImportError as e:
        pytest.skip(f"[dep] No module named '{getattr(e, 'name', None) or 'nvidia.nvimgcodec'}'")
    if not E.gpu_encoder_available():
        pytest.skip("[gpu] nvImageCodec / CUDA GPU encoder unavailable")
    e = E.GpuJpegEncoder(1920, 1080)
    yield e
    e.close()


def test_gpu_nvjpeg_q90_420_deterministic_and_whitelisted(gpu) -> None:
    from awr.agentsdk.env.obs_audit import jpeg_segments_ok

    rgb = _smooth(1080, 1920)
    n0 = E.cpu_encode_calls()
    b1 = gpu.encode(rgb)
    b2 = gpu.encode(rgb.copy())
    assert b1 == b2                                                  # 同镜像、同型号 GPU、同像素 → 同字节
    assert re.fullmatch(r"nvimgcodec-[\d.]+/jpeg/q90/420", gpu.identity)
    assert E.jpeg_subsampling(b1) == "420" and jpeg_segments_ok(b1)  # 基线 SOF0、APP0 JFIF，段白名单（M26-FR-026）
    q = _qtables(b1)
    assert q[0] == _ijg_table(_STD_LUMA, 90) and q[1] == _ijg_table(_STD_CHROMA, 90)
    assert _psnr(_decode(b1), rgb) > 38.0
    cpu = E.CpuJpegEncoder().encode(rgb)
    assert np.abs(_decode(b1).astype(int) - _decode(cpu)).mean() < 2.0   # 与 CPU 回退同参数：解码像素接近（色彩变换舍入不同）
    assert E.cpu_encode_calls() == n0 + 1                            # GPU 编码不经 CPU 编码器
    up, en = gpu.streams
    assert up and en and up != en                                    # 两条 CUDA 流
    assert E.nvimgcodec_cpu_codecs_absent()                          # 镜像不含 nvImageCodec 的 CPU 编码扩展


def test_gpu_three_uav_1080p_online_no_cpu_encode(gpu) -> None:
    """3 机 × 1080p @ 10 Hz（1 s，30 帧）在线路径：rgb1080-rs 选中 GPU，零 CPU 编码调用，交付字节与记录摘要一致。"""
    pytest.importorskip("zstandard")
    from awr.sensorsvc.frame import FrameContext, camera_frame_deferred

    cm = CameraModel.from_hfov(1920, 1080, 84.0, readout_s=0.020, exposure_s=0.001)
    ctx = FrameContext("run-1", "sess-1", "v", gpu.gpu_name, "drv")
    base = _smooth(1080, 1920)
    n0 = E.cpu_encode_calls()
    sizes, pend = [], []
    with E.online_path():
        enc = E.shared_encoder("rgb1080-rs", 1920, 1080)
        assert enc.kind == "gpu"
        for seq in range(1, 11):
            for u in ("u1", "u2", "u3"):
                rgb = np.roll(base, 37 * seq + 11 * int(u[1]), axis=1)
                pend.append((rgb, camera_frame_deferred(ctx, **_frame_kw(cm, _render_out(rgb), seq, u))))
        for _rgb, p in pend:
            rec, blocks = p.result()
            b = blocks[rec["agent_view"]["rgb"]["ref"]]
            assert hashlib.sha256(b).hexdigest() == rec["agent_view"]["rgb"]["sha256"]
            assert rec["internal"]["encoder"] == enc.identity and rec["agent_view"]["readout_ns"] == 20_000_000
            sizes.append(len(b))
        assert pend[0][1].result()[1][pend[0][1].rgb_ref] == enc.encode(pend[0][0])      # 重编码同字节
    assert E.cpu_encode_calls() == n0
    assert len(sizes) == 30 and min(sizes) > 50_000


def test_gl_render_overlaps_cuda_encode(gpu, tmp_path) -> None:
    """GL 渲染（1080p，逐顶点卷帘）与 nvJPEG 编码在时间上重叠：帧 k 的编码区间与帧 k+1 的渲染区间相交。"""
    moderngl = pytest.importorskip("moderngl")
    try:
        ctx = moderngl.create_standalone_context(backend="egl", require=450)
    except Exception as e:                                           # 无 EGL 设备
        pytest.skip(f"[gpu] EGL unavailable: {e}")
    from appfix import write_world

    from awr.sensorsvc.camera import look_at_R
    from awr.sensorsvc.camera.raster import RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance

    cam = RasterCamera(load_appearance(write_world(tmp_path), tile_m=64.0), ctx=ctx)
    with cam.ctx:
        cm = CameraModel.from_hfov(1920, 1080, 84.0, readout_s=0.020)
        spans: list[tuple[float, float]] = []

        def render(k: int):
            p = np.array([float(k), 0.0, 80.0])
            R = look_at_R(p, p - [0, 0, 1])
            nodes = [(R, p + np.array([0.02 * (j - 4), 0.0, 0.0])) for j in range(9)]       # 8 m/s × 2.5 ms 节点间隔
            t0 = time.perf_counter()
            out = cam.render(cm, R, p, rs_nodes=nodes)
            spans.append((t0, time.perf_counter()))
            return out

        render(0)
        spans.clear()
        n = 12
        res = list(E.render_encode_pipeline(range(n), render, gpu))
        overlaps = 0
        for k, (_, _, tk) in enumerate(res[:-1]):
            tk.result()
            e0, e1 = tk.timing["t0"], tk.timing["t1"]
            r0, r1 = spans[k + 1]
            overlaps += min(e1, r1) > max(e0, r0)
        assert all(tk.result()[:2] == b"\xff\xd8" for _, _, tk in res)
        assert overlaps >= (n - 1) // 2, f"encode/render overlap in {overlaps}/{n - 1} frames"
    cam.release()
