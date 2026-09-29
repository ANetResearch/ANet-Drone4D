"""`trajectory.bin` AWTR v1 codec (M01-FR-012; layout frozen by AWR-16 §14.1, header `awr.recon.AwtrHeader.v1`).

Little-endian SoA after a 32-byte header: i64 t_rel_ns[N], f32 pos[3N] (world m), f32 q_xyzw[4N] (w >= 0),
u8 frame_type[N], u8 conf_u8[N], u16 camera_id[N]; every array starts on an 8-byte boundary (zero padding), no padding
after the last array: L(N) = 32 + 8N + pad8(12N) + 16N + 2 pad8(N) + 2N (N = 600 -> 24,032 B).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from awr.contracts.layouts import RECON_AWTR_HEADER, RECON_AWTR_HEADER_CONST

from .jsonio import atomic_write_bytes

__all__ = ["AWTR_MAGIC", "Trajectory", "decode_trajectory", "encode_trajectory", "expected_length", "read_trajectory_bin",
           "write_trajectory_bin"]

AWTR_MAGIC = b"AWTR"
HEADER = 32
assert RECON_AWTR_HEADER.itemsize == HEADER and RECON_AWTR_HEADER_CONST["magic"] == int.from_bytes(AWTR_MAGIC, "little")


def _pad8(x: int) -> int:
    return (x + 7) // 8 * 8


def _offsets(n: int) -> dict[str, int]:
    o = {"t_rel_ns": HEADER}
    o["pos"] = o["t_rel_ns"] + 8 * n
    o["q_xyzw"] = o["pos"] + _pad8(12 * n)
    o["frame_type"] = o["q_xyzw"] + 16 * n
    o["conf_u8"] = o["frame_type"] + _pad8(n)
    o["camera_id"] = o["conf_u8"] + _pad8(n)
    o["end"] = o["camera_id"] + 2 * n
    return o


def expected_length(n: int) -> int:
    return _offsets(n)["end"]


@dataclass
class Trajectory:
    t0_ns: int
    t_rel_ns: np.ndarray       # int64 [N]
    pos: np.ndarray            # float32 [N, 3]
    q_xyzw: np.ndarray         # float32 [N, 4]
    frame_type: np.ndarray     # uint8 [N]
    conf_u8: np.ndarray        # uint8 [N]
    camera_id: np.ndarray      # uint16 [N]
    body: bool = False

    @property
    def n(self) -> int:
        return len(self.t_rel_ns)


def encode_trajectory(t0_ns: int, t_rel_ns, pos_m, q_xyzw, frame_type, conf_u8, camera_id, *, body: bool = False) -> bytes:
    t = np.asarray(t_rel_ns, dtype="<i8")
    n = len(t)
    pos = np.asarray(pos_m, dtype="<f4").reshape(n, 3)
    q = np.asarray(q_xyzw, dtype=np.float64).reshape(n, 4)
    q = np.where(q[:, 3:4] < 0, -q, q).astype("<f4")
    o = _offsets(n)
    buf = bytearray(o["end"])
    hdr = np.zeros((), dtype=RECON_AWTR_HEADER)
    hdr["magic"] = RECON_AWTR_HEADER_CONST["magic"]
    hdr["version"] = RECON_AWTR_HEADER_CONST["version"]
    hdr["flags"] = 1 if body else 0
    hdr["n"] = n
    hdr["t0_ns"] = int(t0_ns)
    buf[0:HEADER] = hdr.tobytes()
    buf[o["t_rel_ns"]:o["t_rel_ns"] + 8 * n] = t.tobytes()
    buf[o["pos"]:o["pos"] + 12 * n] = pos.tobytes()
    buf[o["q_xyzw"]:o["q_xyzw"] + 16 * n] = q.tobytes()
    buf[o["frame_type"]:o["frame_type"] + n] = np.asarray(frame_type, dtype=np.uint8).tobytes()
    buf[o["conf_u8"]:o["conf_u8"] + n] = np.asarray(conf_u8, dtype=np.uint8).tobytes()
    buf[o["camera_id"]:o["camera_id"] + 2 * n] = np.asarray(camera_id, dtype="<u2").tobytes()
    return bytes(buf)


def write_trajectory_bin(path: Path, t0_ns: int, t_rel_ns, pos_m, q_xyzw, frame_type, conf_u8, camera_id, *,
                         body: bool = False) -> None:
    atomic_write_bytes(path, encode_trajectory(t0_ns, t_rel_ns, pos_m, q_xyzw, frame_type, conf_u8, camera_id, body=body))


class AwtrError(ValueError):
    pass


def decode_trajectory(data: bytes) -> Trajectory:
    if len(data) < HEADER:
        raise AwtrError(f"trajectory.bin shorter than the {HEADER}-byte header")
    hdr = np.frombuffer(data[:HEADER], dtype=RECON_AWTR_HEADER)[0]
    if bytes(data[:4]) != AWTR_MAGIC:
        raise AwtrError("bad magic (expected AWTR)")
    if int(hdr["version"]) != 1:
        raise AwtrError(f"unsupported AWTR version {int(hdr['version'])}")
    n = int(hdr["n"])
    o = _offsets(n)
    if len(data) != o["end"]:
        raise AwtrError(f"length {len(data)} != L(N={n}) = {o['end']}")
    return Trajectory(
        t0_ns=int(hdr["t0_ns"]),
        t_rel_ns=np.frombuffer(data, "<i8", n, o["t_rel_ns"]).copy(),
        pos=np.frombuffer(data, "<f4", 3 * n, o["pos"]).reshape(n, 3).copy(),
        q_xyzw=np.frombuffer(data, "<f4", 4 * n, o["q_xyzw"]).reshape(n, 4).copy(),
        frame_type=np.frombuffer(data, np.uint8, n, o["frame_type"]).copy(),
        conf_u8=np.frombuffer(data, np.uint8, n, o["conf_u8"]).copy(),
        camera_id=np.frombuffer(data, "<u2", n, o["camera_id"]).copy(),
        body=bool(int(hdr["flags"]) & 1))


def read_trajectory_bin(path: Path) -> Trajectory:
    return decode_trajectory(Path(path).read_bytes())
