"""tools/bench/geo 的功能自检（`--smoke`：合成小世界、极少重复，不取性能锁、不作为验收数据）：输出 `awr.bench.geo.v1`，
含派生缓存与 Grid25 字节数、热打开、Grid25 匿名页增量、numba 与 numpy 的 los_batch 计时与逐位对拍（不一致 0）。"""

from __future__ import annotations

import json
import subprocess
import sys

from geo_m04_common import REPO


def test_bench_geo_smoke(tmp_path):
    r = subprocess.run([sys.executable, str(REPO / "tools" / "bench" / "geo" / "run.py"), "--smoke", "--out", str(tmp_path)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads((tmp_path / "bench-geo.json").read_text())
    assert doc["schema"] == "awr.bench.geo.v1" and doc["smoke"] and doc["ok"]
    rec = doc["records"][0]
    assert rec["world"] == "tiny" and rec["cache"]["available"] and rec["ws_mb_upper"] > rec["grid25_mb"] > 0
    assert rec["grid25"]["source"] == "cache" and rec["grid25"]["inproc_equal"] and rec["grid25"]["anon_delta_mib"] <= 1.0
    assert rec["los"]["mismatch"] == 0 and rec["los"]["step1"]["numba_ray_us"]["p50"] > 0
    assert {g["metric"] for g in rec["gates"]} == {"los_numba_ray_p50_us_step1", "grid25_anon_delta_mib", "open_hot_p95_ms",
                                                   "los_mismatch"}
    assert doc["world_costs_hint"]["tiny"]["ws_mb_upper"] == rec["ws_mb_upper"]
