"""AWRV v1 编解码（M07-FR-051；16 §8.3；V-E-04）：往返、magic、version、payload_bytes、CRC、NaN 拒绝。"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from awr.contracts.layouts import ENV_AWRV_HEADER, ENV_AWRV_HEADER_OFFSETS
from awr.environment.io import awrv


def vol() -> awrv.AwrvVolume:
    a = (np.arange(3 * 4 * 5 * 4, dtype=np.float32).reshape(3, 4, 5, 4) / 7).astype(np.float16)
    return awrv.AwrvVolume(2, a, (1.0, 2.0, 3.0), (4.0, 4.0, 4.0), float("nan"), 1.0, 0xDEADBEEF)


def test_roundtrip_and_header_matches_contract_layout():
    b = awrv.encode(vol())
    h = np.frombuffer(b[:64], ENV_AWRV_HEADER)[0]
    assert int(h["magic"]) == awrv.AWRV_MAGIC and int(h["version"]) == 1
    assert (int(h["nx"]), int(h["ny"]), int(h["nz"]), int(h["comp"])) == (5, 4, 3, 4)
    assert int(h["field_version"]) == 0xDEADBEEF and int(h["payload_bytes"]) == 3 * 4 * 5 * 4 * 2
    assert ENV_AWRV_HEADER_OFFSETS["crc32"] == 60
    v2 = awrv.decode(b)
    assert np.array_equal(v2.data, vol().data) and v2.origin_enu_m == (1.0, 2.0, 3.0)


@pytest.mark.parametrize("off,val", [(0, b"XWRV"), (4, struct.pack("<H", 2)), (56, struct.pack("<I", 1))])
def test_header_corruption_rejected(off: int, val: bytes):
    b = bytearray(awrv.encode(vol()))
    b[off:off + len(val)] = val
    with pytest.raises(awrv.AwrvError):
        awrv.decode(bytes(b))


def test_crc_rejected():
    b = bytearray(awrv.encode(vol()))
    b[70] ^= 0xFF
    with pytest.raises(awrv.AwrvError):
        awrv.decode(bytes(b))
    assert awrv.decode(bytes(b), check_crc=False) is not None


def test_nan_rejected_in_filterable_volume():
    v = vol()
    v.data[0, 0, 0, 0] = np.nan
    with pytest.raises(awrv.AwrvError):
        awrv.encode(v)


def test_error_code_is_443():
    assert awrv.AwrvError.code == 443
