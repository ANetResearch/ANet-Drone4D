"""M04-AC-201（D2）：Grid25 并入派生缓存（AWR-04 ADR-113 第 5 条；M10-FR-205、M10-AC-204）。

- 缺省高度的 `grid25()` 与 M10 `Grid25.from_world` 逐位相同（a、base、原点与格宽），来源为派生缓存的只读 memmap；
- 非缺省高度由缓存的地板与阻塞掩码在进程内合成，同样与 M10 逐位相同，最近一个高度复用；
- 写时复制映射可写且不影响共享映射；高度越界 110；
- 可分离地板与逐点参考实现逐位一致；manifest 记录 Grid25 参数，缺数组的条目视为损坏；
- 七个世界（needs_data）逐位对拍；Grid25 全量读取后匿名页不增长（D2-AC-28 的"Grid25 不在 sim-core 匿名页中"）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import numpy as np
import pytest
from geo_m04_common import WORLDS

from awr.world.geometry import DERIVE_VERSION, GeoError, Grid25View, open_world_query
from awr.world.geometry import grid25 as G25
from awr.world.geometry.cache import verify_entry

SEVEN = ["synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco"]
needs_seven = pytest.mark.skipif(not all((WORLDS / c / "world.json").exists() for c in SEVEN),
                                 reason="七个世界未构建（make worlds）")


def _m10_grid(wq, alt):
    from awr.sim.planning.grid25 import Grid25

    return Grid25.from_world(wq, alt)


def _same(g, v: Grid25View) -> bool:
    if (g.x0, g.y0, g.cell, g.alt_min_agl_m) != (v.x0_m, v.y0_m, v.cell_m, v.alt_min_agl_m):
        return False
    if not np.array_equal(g.a, v.a):
        return False
    if g.base is None or v.base is None:
        return g.base is None and v.base is None
    return np.array_equal(g.base, v.base)


def test_default_grid_matches_m10_and_is_cached_memmap(wq):
    v = wq.grid25()
    assert v.source == "cache" and isinstance(v.a, np.memmap) and v.a.dtype == np.float64
    assert not v.a.flags.writeable and v.base is not None and not v.base.flags.writeable
    assert _same(_m10_grid(wq, 20.0), v)
    assert np.isinf(v.a).sum() == wq.grid25_info()["blocked_cells"] > 0
    assert (v.h, v.w) == tuple(wq.grid25_info()["shape"]) and v.nbytes == 2 * v.a.size * 8
    with pytest.raises(ValueError):
        v.a[0, 0] = 1.0
    assert wq.grid25().a is v.a                                    # 同一共享映射


def test_non_default_altitude_composed_in_process(wq):
    v = wq.grid25(35.0)
    assert v.source == "process" and v.a.flags.writeable
    assert _same(_m10_grid(wq, 35.0), v)
    assert wq.grid25(35.0) is v                                      # 最近一个高度复用
    v5 = wq.grid25(5.0)
    assert _same(_m10_grid(wq, 5.0), v5) and wq.grid25(35.0) is not v
    assert wq.grid25(20.0).source == "cache"


def test_copy_on_write_is_private(wq):
    c = wq.grid25(copy_on_write=True)
    assert c.source == "cache" and c.a.flags.writeable
    r, k = np.argwhere(np.isinf(c.a))[0]
    c.a[r, k] = c.base[r, k]                                         # A* 恢复起终点格的用法
    assert np.isfinite(c.a[r, k]) and np.isinf(wq.grid25().a[r, k])
    assert np.isinf(wq.grid25(copy_on_write=True).a[r, k])            # 新映射不受影响


@pytest.mark.parametrize("alt", [-1.0, 1001.0, float("nan"), float("inf")])
def test_altitude_out_of_range(wq, alt):
    with pytest.raises(GeoError) as ei:
        wq.grid25(alt)
    assert ei.value.code == 110


def test_floor_separable_equals_pointwise(wq):
    m = wq.grid25_info()
    H, W = m["shape"]
    f = G25.floor_at_centres(wq.dtm, m["x0"], m["y0"], m["cell"], H, W)
    assert f.dtype == np.float32 and np.array_equal(f, G25.floor_at_centres_ref(wq.dtm, m["x0"], m["y0"], m["cell"], H, W))
    assert np.array_equal(f, wq._g25_arrays["grid25_floor"])


def test_pyramid_level_rules():
    assert G25.pyramid_level(2.0, 4.0, 3) == 1 and G25.pyramid_level(2.0, 8.0, 3) == 2
    assert G25.pyramid_level(2.0, 8.0, 2) is None and G25.pyramid_level(2.0, 6.0, 5) is None
    assert G25.pyramid_level(2.0, 1.0, 5) is None and G25.pyramid_level(2.0, float("nan"), 5) is None


def test_manifest_and_incomplete_entry(fake_dir, tmp_path):
    d = tmp_path / "w" / "tiny"
    shutil.copytree(fake_dir / "tiny", d)
    w = open_world_query(d, tmp_path / "cache")
    m = json.loads((w.cache_dir / "manifest.json").read_text())
    assert m["derive_version"] == DERIVE_VERSION == "m04-derive@2"
    assert m["grid25"]["cell"] == 4.0 and m["grid25"]["alt_min_agl_m"] == 20.0 and m["grid25"]["has_base"]
    names = {a["name"]: a for a in m["arrays"]}
    assert names["grid25"]["dtype"] == "float64" and names["grid25"]["href"] == "grid25.f64"
    assert names["grid25_floor"]["dtype"] == "float32" and names["grid25_blocked"]["dtype"] == "uint8"
    assert verify_entry(w.cache_dir, deep=True)
    m["arrays"] = [a for a in m["arrays"] if a["name"] != "grid25_base"]
    (w.cache_dir / "manifest.json").write_text(json.dumps(m))
    assert not verify_entry(w.cache_dir)                               # has_base 但缺数组：损坏
    again = open_world_query(d, tmp_path / "cache")                    # 同键损坏条目被移开后重新发布
    assert again.cache_state == "built" and again.cache_dir == w.cache_dir and verify_entry(again.cache_dir)
    assert _same(_m10_grid(again, 20.0), again.grid25())


def test_no_grid25_for_sub_level_world(wq, monkeypatch):
    monkeypatch.setattr(wq, "_g25_meta", None)
    with pytest.raises(GeoError) as ei:
        wq.grid25()
    assert ei.value.code == 110 and wq.grid25_info() is None


@pytest.mark.needs_data
@needs_seven
@pytest.mark.parametrize("city", SEVEN)
def test_seven_worlds_match_m10(city):
    wq = open_world_query(WORLDS / city, allow_derive=False)
    v = wq.grid25()
    assert v.source == "cache" and _same(_m10_grid(wq, 20.0), v)
    assert _same(_m10_grid(wq, 60.0), wq.grid25(60.0))


_ANON_PROBE = r"""
import re, sys
from pathlib import Path
import numpy as np
from awr.world.geometry import open_world_query

def smaps():
    t = Path("/proc/self/smaps_rollup").read_text()
    return {k: int(re.search(rf"^{k}:\s+(\d+) kB", t, re.M).group(1)) / 1024 for k in ("Rss", "Anonymous")}

wq = open_world_query(Path(sys.argv[1]), Path(sys.argv[2]) if sys.argv[2] != "-" else None, allow_derive=False)
s0 = smaps()
v = wq.grid25()
n = 0
for r0 in range(0, v.h, 64):
    n += int(np.count_nonzero(np.isinf(v.a[r0:r0 + 64])))
    if v.base is not None:
        n += int(np.count_nonzero(np.isinf(v.base[r0:r0 + 64])))
s1 = smaps()
print(s1["Anonymous"] - s0["Anonymous"], (s1["Rss"] - s1["Anonymous"]) - (s0["Rss"] - s0["Anonymous"]), v.nbytes / 2**20, n)
"""


def _anon_probe(world_dir, cache) -> tuple[float, float, float]:
    out = subprocess.run([sys.executable, "-c", _ANON_PROBE, str(world_dir), str(cache)], capture_output=True, text=True,
                         check=True).stdout.split()
    return float(out[0]), float(out[1]), float(out[2])


def test_grid25_pages_are_file_backed_tiny(fake_dir):
    anon, _file, _nb = _anon_probe(fake_dir / "tiny", fake_dir / ".geo-cache")
    assert anon <= 1.0


@pytest.mark.needs_data
@pytest.mark.skipif(not (WORLDS / "sanfrancisco" / "world.json").exists(), reason="sanfrancisco 未构建")
def test_grid25_pages_are_file_backed_sanfrancisco():
    """全量读取旧金山 Grid25（a 与 base，约 52 MiB）后匿名页增量 ≤ 4 MiB，文件页增量 ≥ 90%（M10-AC-204 的口径）。"""
    anon, file_mib, nbytes_mib = _anon_probe(WORLDS / "sanfrancisco", "-")
    assert anon <= 4.0 and file_mib >= 0.9 * nbytes_mib, (anon, file_mib, nbytes_mib)
