"""M03-AC-005：原始读取。截断、改一字节、属性顺序不符分别得到退出码 4、4、3；非有限点剔除计数正确。"""

from __future__ import annotations

import numpy as np
import pytest
from tinyworld_m03 import TinyAdapter, to_source, true_cloud, write_ply

from awr.world.ingest import ConfigError, RawDataError, ingest
from awr.world.ingest.rawply import read_header, read_ply


@pytest.fixture
def small(tmp_path):
    P, N = true_cloud(30_000)
    Ps, Ns = to_source(P, N)
    p = tmp_path / "s.ply"
    return p, write_ply(p, Ps, Ns), Ps, Ns


def test_header_and_memmap(small):
    p, sha, Ps, _ = small
    info = read_header(p)
    assert info.n == len(Ps) and info.file_size == info.expected == info.header_bytes + 24 * info.n
    xyz, _nrm, _info, got = read_ply(p, expect_sha256=sha)
    assert got == sha and np.allclose(xyz, Ps.astype(np.float32))


def test_truncated_is_exit_4(small):
    p, _sha, *_ = small
    data = p.read_bytes()
    p.write_bytes(data[:-5])
    with pytest.raises(RawDataError) as ei:
        read_ply(p)
    assert ei.value.exit_code == 4


def test_one_byte_changed_is_exit_4(small):
    p, sha, *_ = small
    b = bytearray(p.read_bytes())
    b[-100] ^= 0x01
    p.write_bytes(bytes(b))
    with pytest.raises(RawDataError) as ei:
        read_ply(p, expect_sha256=sha)
    assert ei.value.exit_code == 4


def test_property_order_is_exit_3(tmp_path):
    P, N = true_cloud(1000)
    p = tmp_path / "bad.ply"
    write_ply(p, P, N, props=("y", "x", "z", "nx", "ny", "nz"))
    with pytest.raises(ConfigError) as ei:
        read_ply(p)
    assert ei.value.exit_code == 3


def test_missing_file_is_exit_4(tmp_path):
    with pytest.raises(RawDataError) as ei:
        read_ply(tmp_path / "none.ply")
    assert ei.value.exit_code == 4 and ei.value.error_code == "RAW_MISSING"


def test_nonfinite_points_dropped_and_counted(tmp_path):
    P, N = true_cloud(60_000)
    Ps, Ns = to_source(P, N)
    Ps[10] = np.nan
    Ps[20, 1] = np.inf
    Ns[30] = np.nan                                  # 非有限法线按零法线处理
    p = tmp_path / "nan.ply"
    sha = write_ply(p, Ps, Ns)
    nc = ingest(TinyAdapter(p, sha))
    assert nc.stats.n_nonfinite == 2 and len(nc.xyz) == len(P) - 2
    g09 = next(g for g in nc.gates if g.id == "G-09")
    assert g09.passed and g09.value == len(P) - 2
    assert nc.stats.zero_normals_frac > 0
