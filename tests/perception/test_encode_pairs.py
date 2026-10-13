"""逐对读数的直接编码与 `encode_soa(quant_frame(...))` 逐字节相同（M19-FR-060、SB-73 线格式；D2-FX-R2-sim 第 2 次，
`quant.encode_pairs`、`FieldService._quant_pairs`）。

`FieldService.tick` 发布逐对读数时不再先组装 QuantFrame 对象：`encode_pairs` 从 `quant_pairs` 的结果直接量化为列式 SoA。
本用例在多种规模（含单机、过滤单机）下逐帧比较 msgpack 编码后的字节。
"""

from __future__ import annotations

import msgpack
import pytest
from test_field_wire import Scene

from awr.sim.perception.quant import encode_pairs, encode_soa, quant_frame


@pytest.mark.parametrize(("n_uav", "n_tgt"), [(12, 30), (3, 5), (1, 2)])
def test_encode_pairs_matches_frame_encoding(n_uav: int, n_tgt: int) -> None:
    s = Scene(n_uav, n_tgt)
    try:
        svc = s.svc
        frames = 0
        for _ in range(12):
            s.tick(0.5)
            us, en = svc.uavs(s.b.ctx), svc.env(s.b.ctx)
            for tid in s.tids:
                for uf in (None, us[0].id if us else None):
                    qp = svc._quant_pairs(tid, s.b.ctx, uav=uf, uavs=us, env=en)
                    a = msgpack.packb(encode_pairs(*qp), use_bin_type=True, use_single_float=True)
                    b = msgpack.packb(encode_soa(quant_frame(*qp)), use_bin_type=True, use_single_float=True)
                    assert a == b, (tid, uf)
                    frames += 1
        assert frames == 12 * n_tgt * 2
        assert isinstance(svc.last_quant, dict)
    finally:
        s.close()
