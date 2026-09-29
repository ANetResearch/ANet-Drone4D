"""Map Release tile header `AWLT` v1 (M02 §6.3.4 draft; directory layer pending ADR, M02 §14 item 4).

Little-endian, 48-byte header, 8-byte aligned arrays: char[4] "AWLT", u16 version = 1, u16 flags (bit0 = intensity),
i32 key[2] (tile ij), f64 origin[3] (tile min corner, world m), f32 quant_m, u32 n; payload u16 xyz[n][3] (quantised
from the corner: 0.01 m covers 0-655.35 m), u16 oct16 normal[n] (0x0000 = none), optional u8 intensity[n].
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

__all__ = ["HEADER", "TileHeader", "decode_tile", "encode_tile"]

HEADER = struct.Struct("<4sHHii3dfI")
assert HEADER.size == 48


def _pad8(x: int) -> int:
    return (x + 7) // 8 * 8


@dataclass(frozen=True)
class TileHeader:
    key: tuple[int, int]
    origin: tuple[float, float, float]
    quant_m: float
    n: int
    intensity: bool = False


def encode_tile(h: TileHeader, xyz_q: np.ndarray, normal: np.ndarray, intensity: np.ndarray | None = None) -> bytes:
    n = h.n
    parts = [HEADER.pack(b"AWLT", 1, 1 if h.intensity else 0, h.key[0], h.key[1], *h.origin, h.quant_m, n)]
    for arr in (np.asarray(xyz_q, "<u2").reshape(n, 3), np.asarray(normal, "<u2").reshape(n)) + (
            (np.asarray(intensity, np.uint8).reshape(n),) if h.intensity else ()):
        b = arr.tobytes()
        parts.append(b + b"\0" * (_pad8(len(b)) - len(b)))
    return b"".join(parts)


def decode_tile(data: bytes) -> tuple[TileHeader, np.ndarray, np.ndarray, np.ndarray | None]:
    magic, ver, flags, k0, k1, ox, oy, oz, q, n = HEADER.unpack_from(data, 0)
    if magic != b"AWLT" or ver != 1:
        raise ValueError("not an AWLT v1 tile")
    o = HEADER.size
    xyz = np.frombuffer(data, "<u2", 3 * n, o).reshape(n, 3).copy()
    o += _pad8(6 * n)
    nrm = np.frombuffer(data, "<u2", n, o).copy()
    o += _pad8(2 * n)
    inten = np.frombuffer(data, np.uint8, n, o).copy() if flags & 1 else None
    return TileHeader((k0, k1), (ox, oy, oz), q, n, bool(flags & 1)), xyz, nrm, inten
