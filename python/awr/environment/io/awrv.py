"""AWRV v1 体数据编解码（M07-FR-051；16 §8.3；layouts.json `awr.env.AwrvHeader.v1`；g06 §7.3）。

小端，64 B 头 + payload；layout 0 = zyx C 序（x 变化最快，等价于 Data3DTexture(width=nx, height=ny, depth=nz)）；
dtype 1 f16、2 f32、3 u8；CRC-32（zlib）覆盖 payload。解码器对 magic、version、payload_bytes、CRC 不符一律拒绝
（V-E-04），错误码 443 ENV_ASSET_INVALID。`field_version` = sha256(参数串) 前 4 字节按小端解释的 u32（16 §8.4 第 3 条）。
"""

from __future__ import annotations

import hashlib
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["AWRV_MAGIC", "AwrvError", "AwrvVolume", "decode", "encode", "field_version", "read", "read_header", "write_atomic"]

AWRV_MAGIC = 0x56525741  # 'AWRV'
HEADER = struct.Struct("<IHHHHHHBBBB3f3fffIII")
assert HEADER.size == 64

KIND = {"wind_sector": 1, "turb_box": 2, "wind_snapshot": 3, "scalar_field": 4, "lbm_frame": 5, "noise_field": 6}
DTYPE = {1: np.dtype("<f2"), 2: np.dtype("<f4"), 3: np.dtype("u1")}
DTYPE_CODE = {np.dtype("<f2"): 1, np.dtype("<f4"): 2, np.dtype("u1"): 3}
ENV_ASSET_INVALID = 443


class AwrvError(ValueError):
    code = ENV_ASSET_INVALID


def field_version(params: str) -> int:
    return int.from_bytes(hashlib.sha256(params.encode("utf-8")).digest()[:4], "little")


@dataclass(slots=True)
class AwrvVolume:
    kind: int
    data: np.ndarray  # (nz, ny, nx, comp)，dtype f16 / f32 / u8
    origin_enu_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    cell_m: tuple[float, float, float] = (1.0, 1.0, 1.0)
    dir_from_deg: float = float("nan")
    value_scale: float = 1.0
    field_version: int = 0
    premultiplied: int = 0
    alpha: int = 0
    layout: int = 0

    @property
    def shape_xyz(self) -> tuple[int, int, int]:
        nz, ny, nx = self.data.shape[:3]
        return nx, ny, nz


def encode(vol: AwrvVolume) -> bytes:
    a = np.ascontiguousarray(vol.data)
    if a.ndim != 4:
        raise AwrvError("AWRV data must be (nz, ny, nx, comp)")
    dt = a.dtype.newbyteorder("<") if a.dtype.byteorder == ">" else a.dtype
    code = DTYPE_CODE.get(np.dtype(dt))
    if code is None:
        raise AwrvError(f"unsupported dtype {a.dtype}")
    if code in (1, 2) and not np.all(np.isfinite(a)):
        raise AwrvError("NaN or Inf in a filterable volume (AWR-03 §5.7)")
    payload = a.astype(dt, copy=False).tobytes()
    nz, ny, nx, comp = a.shape
    hdr = HEADER.pack(AWRV_MAGIC, 1, vol.kind, nx, ny, nz, comp, code, vol.layout, vol.premultiplied, vol.alpha,
                      *(float(x) for x in vol.origin_enu_m), *(float(x) for x in vol.cell_m), float(vol.dir_from_deg),
                      float(vol.value_scale), vol.field_version & 0xFFFFFFFF, len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
    return hdr + payload


def _parse_header(b: bytes | memoryview) -> dict:
    if len(b) < 64:
        raise AwrvError("AWRV too short")
    (magic, ver, kind, nx, ny, nz, comp, dtype, layout, premul, alpha, ox, oy, oz, cx, cy, cz, dirf, scale, fv, nbytes,
     crc) = HEADER.unpack_from(b, 0)
    if magic != AWRV_MAGIC:
        raise AwrvError("AWRV magic mismatch")
    if ver != 1:
        raise AwrvError(f"AWRV version {ver} != 1")
    if dtype not in DTYPE:
        raise AwrvError(f"AWRV dtype {dtype}")
    if nbytes != nx * ny * nz * comp * DTYPE[dtype].itemsize:
        raise AwrvError("AWRV payload_bytes does not match nx*ny*nz*comp*sizeof(dtype)")
    return {"kind": kind, "nx": nx, "ny": ny, "nz": nz, "comp": comp, "dtype": dtype, "layout": layout, "premultiplied": premul,
            "alpha": alpha, "origin_enu_m": (ox, oy, oz), "cell_m": (cx, cy, cz), "dir_from_deg": dirf, "value_scale": scale,
            "field_version": fv, "payload_bytes": nbytes, "crc32": crc}


def decode(b: bytes | memoryview, *, check_crc: bool = True) -> AwrvVolume:
    h = _parse_header(b)
    end = 64 + h["payload_bytes"]
    if len(b) < end:
        raise AwrvError("AWRV payload truncated")
    payload = memoryview(b)[64:end]
    if check_crc and (zlib.crc32(payload) & 0xFFFFFFFF) != h["crc32"]:
        raise AwrvError("AWRV CRC mismatch")
    a = np.frombuffer(payload, DTYPE[h["dtype"]]).reshape(h["nz"], h["ny"], h["nx"], h["comp"])
    return AwrvVolume(h["kind"], a, h["origin_enu_m"], h["cell_m"], h["dir_from_deg"], h["value_scale"], h["field_version"],
                      h["premultiplied"], h["alpha"], h["layout"])


def read_header(path: Path) -> dict:
    with open(path, "rb") as f:
        return _parse_header(f.read(64))


def read(path: Path, *, check_crc: bool = True) -> AwrvVolume:
    return decode(Path(path).read_bytes(), check_crc=check_crc)


def write_atomic(path: Path, vol: AwrvVolume) -> int:
    """写临时文件后原子改名（M07-FR-026）；返回字节数。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encode(vol)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return len(data)
