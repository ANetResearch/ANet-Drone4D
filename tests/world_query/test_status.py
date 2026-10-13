"""M04-AC-203（D2）：`allow_derive = false` 的可用性探测与部署开关（AWR-04 §4.4.7；M17-FR-015、M17-AC-010；M10-FR-204）。

- `cache_status`：无条目 `missing`、只有其他键（版本或参数不一致）`stale`、条目损坏 `corrupt`（文件截断；`deep` 时逐数组
  sha256），三者 `reason = derived_cache_missing`；世界包本身不可用为 `world_invalid`；可用时给出条目与 Grid25 字节数；
  探测不派生、不写缓存；
- `open_world_query`：`AWR_GEO_ALLOW_DERIVE=0` 时缓存缺失抛 `GEO_CACHE_MISS`（`reason = derived_cache_missing`）；
  缓存根缺省取 `AWR_GEO_CACHE_DIR`；
- CLI `check`：全部可用退出码 0，否则 4；`--json` 输出探测结果；`warm` 不受 `AWR_GEO_ALLOW_DERIVE` 影响。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from geo_m04_common import REPO

from awr.world.geometry import (
    DERIVED_CACHE_MISSING,
    WORLD_INVALID,
    GeoLoadError,
    GeoParams,
    cache_status,
    cache_status_all,
    open_world_query,
)
from awr.world.geometry.cache import default_cache_root


@pytest.fixture
def world(fake_dir, tmp_path):
    root = tmp_path / "worlds"
    shutil.copytree(fake_dir / "tiny", root / "tiny")
    return root


def test_missing_then_available(world):
    st = cache_status(world / "tiny")
    assert not st.available and st.reason == DERIVED_CACHE_MISSING and st.detail == "missing"
    assert not (world / ".geo-cache").exists()                          # 探测不写缓存
    wq = open_world_query(world / "tiny")
    st = cache_status(world / "tiny")
    assert st.available and st.reason is None and st.derive_sha8 == wq.derive_sha8 and st.entry == str(wq.cache_dir)
    assert st.cache_bytes > st.grid25_bytes > 0 and st.content_version == wq.content_version
    assert cache_status(world / "tiny", deep=True).available
    doc = st.to_json()
    assert {"world_id", "available", "reason", "detail", "derive_sha8", "cache_bytes", "grid25_bytes", "entry"} <= set(doc)


def test_stale_when_only_other_keys(world):
    open_world_query(world / "tiny", params=GeoParams(close_k=1))
    st = cache_status(world / "tiny")
    assert not st.available and st.detail == "stale" and len(st.other_entries) == 1
    assert cache_status(world / "tiny", params=GeoParams(close_k=1)).available


def test_corrupt_entries(world):
    wq = open_world_query(world / "tiny")
    f = wq.cache_dir / "grid25.f64"
    data = bytearray(f.read_bytes())
    data[0] ^= 0xFF
    f.write_bytes(bytes(data))                                           # 大小不变、内容变化：只有 deep 能发现
    assert cache_status(world / "tiny").available
    st = cache_status(world / "tiny", deep=True)
    assert not st.available and st.detail == "corrupt" and st.checked == "deep"
    f.write_bytes(bytes(data[:-8]))                                      # 截断：浅检查即发现
    st = cache_status(world / "tiny")
    assert not st.available and st.reason == DERIVED_CACHE_MISSING and st.detail == "corrupt"
    with pytest.raises(GeoLoadError) as ei:
        open_world_query(world / "tiny", allow_derive=False)
    assert ei.value.code == "GEO_CACHE_MISS" and ei.value.reason == DERIVED_CACHE_MISSING


def test_world_invalid(world):
    c = world / "tiny" / "coordinate.json"
    c.write_text(c.read_text() + " ")
    st = cache_status(world / "tiny")
    assert not st.available and st.reason == WORLD_INVALID and st.detail == "GEO_SHA_MISMATCH"
    assert cache_status(world / "nope").detail == "GEO_MISSING_FILE"
    with pytest.raises(GeoLoadError) as ei:
        open_world_query(world / "tiny")
    assert ei.value.reason == WORLD_INVALID


def test_status_all_lists_worlds(world):
    (world / "_shared").mkdir()
    (world / ".staging").mkdir()
    out = cache_status_all(world)
    assert [s.world_id for s in out] == ["tiny"] and not out[0].available


def test_env_allow_derive_and_cache_dir(world, tmp_path, monkeypatch):
    monkeypatch.setenv("AWR_GEO_ALLOW_DERIVE", "0")
    with pytest.raises(GeoLoadError) as ei:
        open_world_query(world / "tiny")
    assert ei.value.code == "GEO_CACHE_MISS" and ei.value.reason == DERIVED_CACHE_MISSING
    assert open_world_query(world / "tiny", allow_derive=True).cache_state == "built"      # 显式参数优先
    assert open_world_query(world / "tiny").cache_state == "hit"
    monkeypatch.setenv("AWR_GEO_CACHE_DIR", str(tmp_path / "elsewhere"))
    assert default_cache_root(world / "tiny") == tmp_path / "elsewhere"
    with pytest.raises(GeoLoadError):
        open_world_query(world / "tiny")
    assert not cache_status(world / "tiny").available
    monkeypatch.setenv("AWR_GEO_ALLOW_DERIVE", "1")
    wq = open_world_query(world / "tiny")
    assert wq.cache_state == "built" and str(wq.cache_dir).startswith(str(tmp_path / "elsewhere")) and wq.allow_derive
    assert cache_status(world / "tiny").available
    monkeypatch.delenv("AWR_GEO_CACHE_DIR")
    assert default_cache_root(world / "tiny") == world / ".geo-cache"


def _cli(*args, env=None):
    e = dict(os.environ, PYTHONPATH=str(REPO / "python"))
    e.pop("AWR_GEO_CACHE_DIR", None)
    e.update(env or {})
    return subprocess.run([sys.executable, "-m", "awr.world.geometry.cli", *args], capture_output=True, text=True, env=e)


def test_cli_check_and_warm(world):
    r = _cli("check", "--all", "--worlds", str(world))
    assert r.returncode == 4 and "derived_cache_missing" in r.stderr
    r = _cli("warm", "--all", "--worlds", str(world), env={"AWR_GEO_ALLOW_DERIVE": "0"})
    assert r.returncode == 0 and "built" in r.stdout
    r = _cli("check", "--world", "tiny", "--worlds", str(world), "--deep", "--json")
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert doc[0]["available"] and doc[0]["checked"] == "deep" and doc[0]["grid25_bytes"] > 0
    r = _cli("info", "--world", "tiny", "--worlds", str(world), "--no-derive")
    assert r.returncode == 0 and json.loads(r.stdout)["grid25"]["cell"] == 4.0
