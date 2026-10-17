"""D3 扩展点 6（AWR-05 §12.3）：`RawCloud.rgb` 经 ingest 的非有限点筛选与 `.work` 交换后与点逐一对应；
没有 rgb 时 NormalizedCloud.rgb 为 None，D2 世界的构建结果不变。"""

from __future__ import annotations

import numpy as np
import pytest
from tinyworld_m03 import TinyAdapter

from awr.world.ingest.pipeline import ingest
from awr.world.ingest.types import ConfigError
from awr.world.package.build import load_work, save_work


class _ColorAdapter(TinyAdapter):
    """tiny world 加逐点颜色（颜色由源点下标给出），并把前 10 个点改为 NaN 以覆盖筛选路径。"""

    def __init__(self, *a, rgb_shape_ok: bool = True, **kw):
        super().__init__(*a, **kw)
        self.rgb_shape_ok = rgb_shape_ok

    def load(self):
        raw = super().load()
        xyz = np.array(raw.xyz, np.float64)
        xyz[:10] = np.nan
        k = np.arange(len(xyz))
        rgb = np.c_[k % 251, (k // 251) % 241, (k // 60491) % 239].astype(np.uint8)
        raw.xyz = xyz
        raw.rgb = rgb if self.rgb_shape_ok else rgb[:-1]
        return raw


def test_rgb_follows_the_kept_points_through_ingest_and_work(tiny_raw, tmp_path):
    ply, sha = tiny_raw
    nc = ingest(_ColorAdapter(ply, sha))
    assert nc.stats.n_nonfinite == 10
    k = np.arange(10, nc.stats.n_raw)
    assert nc.rgb is not None and nc.rgb.shape == (len(nc.xyz), 3)
    assert np.array_equal(nc.rgb, np.c_[k % 251, (k // 251) % 241, (k // 60491) % 239].astype(np.uint8))
    save_work(tmp_path, nc, {}, "2026-10-17T00:00:00Z")
    back, _, _ = load_work(tmp_path)
    assert np.array_equal(back.rgb, nc.rgb)


def test_no_rgb_stays_none_and_bad_shape_is_refused(tiny_raw, tmp_path):
    ply, sha = tiny_raw
    nc = ingest(TinyAdapter(ply, sha))
    assert nc.rgb is None
    save_work(tmp_path, nc, {}, "2026-10-17T00:00:00Z")
    assert load_work(tmp_path)[0].rgb is None
    with pytest.raises(ConfigError):
        ingest(_ColorAdapter(ply, sha, rgb_shape_ok=False))
