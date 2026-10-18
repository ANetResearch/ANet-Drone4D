"""相机帧记录（`drone4d.frame.v1`，M25 §6.7、§7.2.1；AWR-05 §8.1）：由光栅输出与请求上下文组装三个分区与载荷块。

- agent_view：交付图像（JPEG，sha256 取交付字节）、L2-depth（f32 平面深度，`depth` 在输出集合中时）、内参与标称挂载、云台读数；
- ground_truth：真值外参（ENU←RDF）、真实云台角、实例与无遮挡实例（u16）、语义（u8，`d3-semantic@1`）、`invalid_mask`（u8），
  均为 zstd 压缩；`gt_depth`（f32，bshuf4-zstd 无损）；可见度归约 `objects`（无演员时为空表）；真值时刻；
- internal：版本与来源（`encoder` 为实际编码器身份，如 `nvimgcodec-0.9.0/jpeg/q90/420`）。载荷块以 `ref` 命名（同一 MCAP
  消息的附加段或文件导出中的相对路径）。

RGB 编码（M25-FR-060、183；`camera.encode`）：缺省取进程常驻编码器（GPU nvJPEG 可用即用，`rgb1080-rs` 与在线路径只允许 GPU）；
`camera_frame_deferred` 先把 RGB 提交给编码流再在 CPU 上组装真值分区，返回 `PendingFrame`，调用方可先渲染下一帧再取结果，
使 GL 渲染与 CUDA 编码重叠。交付字节即存档字节：`blocks[rgb ref]` 与 `agent_view.rgb.sha256` 取自同一 `bytes` 对象。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from awr.bench.recorder.depthcodec import DEPTH_COMPRESSION, bshuf4_zstd
from awr.sensorsvc.camera.actors import visibility_objects
from awr.sensorsvc.camera.encode import CpuJpegEncoder, EncodeTicket, JpegEncoder, shared_encoder
from awr.sensorsvc.camera.model import CAMERA_PROFILES, CameraModel
from awr.sensorsvc.camera.proj import ray_elevation
from awr.sensorsvc.camera.raster import RENDERER_VERSION, RS_NODES, RenderOut
from awr.sensorsvc.poses import Mount, q_world_opt

__all__ = ["INVALID_BITS", "FrameContext", "PendingFrame", "camera_frame", "camera_frame_deferred", "camera_profile_of", "encode_jpeg"]

INVALID_BITS = ("void_below_horizon", "stretched", "outside_coverage", "hole_edge", "water")
ZSTD_LEVEL = 3


@dataclass(frozen=True)
class FrameContext:
    run_id: str
    session_id: str
    world_content_version: str
    gpu_model: str
    driver: str
    dynamic_source_id: str = "none"
    backend: str = "raster-gl"
    renderer_version: str = RENDERER_VERSION
    fx_version: str = "fx-none@0"
    appearance: str = "mesh"
    noise_tier: str = "clean"
    noise_table_version: str = "none"
    encoder: str = "auto"                 # 实际编码器身份在 camera_frame 中写入；只在调用方直接给出 JPEG 字节时取本值
    derived: bool = False

    def internal(self) -> dict:
        return {k: getattr(self, k) for k in ("run_id", "session_id", "world_content_version", "dynamic_source_id", "backend",
                                              "renderer_version", "fx_version", "appearance", "noise_tier", "noise_table_version",
                                              "encoder", "gpu_model", "driver", "derived")}


@lru_cache(maxsize=8)
def _cpu_encoder(quality: int) -> CpuJpegEncoder:
    return CpuJpegEncoder(quality)


def encode_jpeg(rgb8: np.ndarray, quality: int = 90) -> bytes:
    """CPU JPEG 编码（Pillow，4:2:0，与 nvJPEG 同量化表；CI、离线工具与样张）。在线路径（`camera.encode.online_path()`）
    内调用即抛 `CpuEncodeForbidden`。"""
    return _cpu_encoder(int(quality)).encode(rgb8)


def camera_profile_of(cm: CameraModel) -> str | None:
    """按交付分辨率与卷帘读出匹配相机档位（`CAMERA_PROFILES`）；不匹配时为 None。"""
    for name, p in CAMERA_PROFILES.items():
        if (cm.width, cm.height) == tuple(p["size"]) and (cm.readout_s > 0) == (p["readout_s"] > 0):
            return name
    return None


def _zstd(a: np.ndarray) -> bytes:
    import zstandard

    return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(np.ascontiguousarray(a).tobytes())


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _gimbal(g: tuple[float, float, float]) -> dict:
    d = {"az_deg": round(math.degrees(g[0]), 6), "el_deg": round(math.degrees(g[1]), 6)}
    if g[2] > 0:
        d["zoom_f_mm"] = g[2]
    return d


@dataclass
class PendingFrame:
    """RGB 仍在编码流上的帧：`result()` 等待编码完成，填入 RGB 载荷与交付字节 sha256 后返回 (frame.v1 记录, {ref: 字节})。"""
    rec: dict
    blocks: dict[str, bytes]
    ticket: EncodeTicket
    rgb_ref: str
    _done: bool = False

    def result(self, timeout: float | None = None) -> tuple[dict, dict[str, bytes]]:
        if not self._done:
            jpg = self.ticket.result(timeout)
            self.blocks[self.rgb_ref] = jpg                        # 交付与存档使用同一 bytes 对象
            self.rec["agent_view"]["rgb"]["sha256"] = _sha(jpg)
            self._done = True
        return self.rec, self.blocks


def camera_frame(ctx: FrameContext, **kw) -> tuple[dict, dict[str, bytes]]:
    """返回 (frame.v1 记录, {ref: 字节})；参数同 `camera_frame_deferred`。"""
    return camera_frame_deferred(ctx, **kw).result()


def camera_frame_deferred(ctx: FrameContext, *, uav: str, sensor_id: str, seq: int, t_mid_ns: int, exposure_ns: int, readout_ns: int,
                          cm: CameraModel, mount: Mount, gimbal_reading: tuple[float, float, float],
                          gimbal_true: tuple[float, float, float], pos_enu_m: np.ndarray, R_ws: np.ndarray, out: RenderOut,
                          outputs: frozenset[str] | set[str], ref_prefix: str = "", depth_obs: np.ndarray | None = None,
                          depth_range_m: tuple[float, float] | None = None, actors: list[dict] | None = None,
                          encoder: JpegEncoder | None = None, rgb: EncodeTicket | bytes | None = None) -> PendingFrame:
    """组装一帧并返回 `PendingFrame`。`outputs` 取 render_req.v1 的取值；真值分区总是写出（只进入评测库与真值文件，ADR-161）。
    `depth_obs` 为经 L2-depth 传感器模型的观测深度（缺省即渲染深度），`gt_depth` 总是渲染深度。`actors` 为磁带演员表（按 idx），
    给出且渲染带有 `instance_unocc` 时做可见度归约（M25 §6.6.1）。

    RGB：`rgb` 为已提交的编码票据或已编码的 JPEG 字节（身份取票据、`encoder` 或 `ctx.encoder`）；缺省由 `encoder`（缺省为
    按档位选择的进程常驻编码器，`camera.encode.shared_encoder`）在函数入口提交，随后的真值压缩与可见度归约在 CPU 上与 GPU
    编码并行。"""
    blocks: dict[str, bytes] = {}

    def put(name: str, b: bytes) -> str:
        ref = f"{ref_prefix}{name}"
        blocks[ref] = b
        return ref

    if rgb is None:
        enc = encoder or shared_encoder(camera_profile_of(cm), cm.width, cm.height, quality=cm.isp.jpeg_quality)
        tk = enc.submit(out.rgb8)
    elif isinstance(rgb, EncodeTicket):
        tk = rgb
    else:
        tk = EncodeTicket(_bytes=bytes(rgb), identity=encoder.identity if encoder is not None else ctx.encoder)
    enc_id = tk.identity or (encoder.identity if encoder is not None else ctx.encoder)
    if not enc_id or enc_id == "auto":
        raise ValueError("JPEG encoder identity unknown: pass encoder= or set FrameContext.encoder")
    rgb_ref = put("rgb.jpg", b"")                                  # 占位，保持载荷块顺序；result() 填入交付字节
    av = {"sensor_id": sensor_id, "seq": int(seq), "t_mid_ns": int(t_mid_ns), "exposure_ns": int(exposure_ns), "readout_ns": int(readout_ns),
          "width": cm.width, "height": cm.height, "K": [cm.fx, 0.0, cm.cx, 0.0, cm.fy, cm.cy, 0.0, 0.0, 1.0], "dist": list(cm.dist),
          "pixel_convention": "colmap_center_0.5", "mount_nominal": {"xyz_m": list(mount.xyz_m), "rpy_deg": list(mount.rpy_deg)},
          "gimbal_reading": _gimbal(gimbal_reading),
          "rgb": {"codec": "jpeg", "quality": cm.isp.jpeg_quality, "ref": rgb_ref, "sha256": ""}}
    depth = np.ascontiguousarray(out.depth_m, "<f4")
    if "depth" in outputs:
        b = (depth if depth_obs is None else np.ascontiguousarray(depth_obs, "<f4")).tobytes()
        rng = list(depth_range_m) if depth_range_m else [cm.near_m, cm.far_m]
        av["depth"] = {"dtype": "f32", "unit": "m", "type": "planar_z", "range_m": rng, "ref": put("depth.f32", b), "sha256": _sha(b)}
    void = (depth <= 0) & (ray_elevation(cm, R_ws, cm.width, cm.height) < 0)
    mask = void.astype(np.uint8)                                   # 第 0 位；其余位随质量位附件与效果链落地
    unocc = out.instance_unocc if out.instance_unocc is not None else np.zeros_like(out.instance)
    objects = visibility_objects(out.instance, unocc, out.semantic, mask, actors) if actors and out.instance_unocc is not None else []
    gt = {"T_world_sensor": {"pos_enu_m": [float(v) for v in pos_enu_m], "q_ws_xyzw": [float(v) for v in q_world_opt(R_ws)], "frame": "ENU<-RDF"},
          "gimbal_true": _gimbal(gimbal_true),
          "instance": {"dtype": "u16", "codec": "zstd", "ref": put("instance.u16.zst", _zstd(out.instance.astype("<u2")))},
          "instance_unocc": {"dtype": "u16", "codec": "zstd", "ref": put("instance_unocc.u16.zst", _zstd(unocc.astype("<u2")))},
          "semantic": {"dtype": "u8", "codec": "zstd", "code_table": "d3-semantic@1", "ref": put("semantic.u8.zst", _zstd(out.semantic))},
          "invalid_mask": {"dtype": "u8", "codec": "zstd", "bits": list(INVALID_BITS), "ref": put("invalid_mask.u8.zst", _zstd(mask))},
          "objects": objects,
          "timing": {"t_truth_ns": [int(t_mid_ns)], "blur_subframes": cm.blur_subframes, "rs_bands": cm.rs_bands, "rs_method": "vertex"}}
    if readout_ns > 0:                                             # 卷帘：节点数与读出窗口（M25 §15.4）
        gt["timing"]["rs_nodes"] = RS_NODES
        gt["timing"]["readout_window_ns"] = [int(t_mid_ns) - int(readout_ns) // 2, int(t_mid_ns) + int(readout_ns) // 2]
    if "gt_depth" in outputs:
        gt["gt_depth"] = {"dtype": "f32", "unit": "m", "codec": DEPTH_COMPRESSION,
                          "ref": put("gt_depth.f32.bshuf.zst", bshuf4_zstd(depth.tobytes()))}
    rec = {"schema": "drone4d.frame.v1", "frame_id": f"{ctx.run_id}/{uav}/{sensor_id}/{int(seq)}", "kind": "camera", "agent_view": av,
           "ground_truth": gt, "internal": {**ctx.internal(), "encoder": enc_id}}
    return PendingFrame(rec, blocks, tk, rgb_ref)
