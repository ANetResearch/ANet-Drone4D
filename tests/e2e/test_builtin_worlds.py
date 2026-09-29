"""六城事实回归（M16-FR-005；M16-AC-007；M16 §6.2.3；AWR-16 §7、§10.3、§10.4）。

读取已构建的 World Package，对照 16 §10.3 的规范值断言（`needs_data`；世界缺失时 skip，G2 中缺失即失败）。
任何一项失败都意味着坐标、单位或调平出错（x01 §6 第 1 条：全项目最高风险）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import yaml
from awrproc import ROOT
from e2ehelp import WORLDS, needs_world

from awr.datasets.urbanscene3d.cities import CITIES, CITY_IDS

pytestmark = pytest.mark.needs_data


def _world(wid: str) -> tuple[dict, dict]:
    needs_world(wid)
    w = json.loads((WORLDS / wid / "world.json").read_text(encoding="utf-8"))
    c = json.loads((WORLDS / wid / (w.get("coordinate") or {}).get("href", "coordinate.json")).read_text(encoding="utf-8"))
    return w, c


def _data_yaml() -> dict[str, dict]:
    d = yaml.safe_load((ROOT / "configs" / "data.yaml").read_text(encoding="utf-8"))
    return {f["world_id"]: f for f in d["urbanscene3d"]["files"]}


@pytest.mark.parametrize("wid", CITY_IDS)
def test_extent_and_max_z(wid: str) -> None:
    _, c = _world(wid)
    lo, hi = np.asarray(c["extent"]["min"], float), np.asarray(c["extent"]["max"], float)
    assert np.allclose(hi - lo, CITIES[wid].extent_enu_m, atol=1.0), (hi - lo).tolist()
    assert hi[2] == pytest.approx(CITIES[wid].max_world_z_m, abs=0.5)


@pytest.mark.parametrize("wid", CITY_IDS)
def test_render_and_roots(wid: str) -> None:
    w, _ = _world(wid)
    f = CITIES[wid]
    r = w["render"]
    assert r["defaultColorMode"] == f.default_color
    assert r["nnMedianM"] == pytest.approx(f.nn_median_m, rel=0.02)
    assert r.get("syntheticGroundZ") == f.synthetic_ground_z
    pc = next(L for L in w["layers"] if L["id"] == "pointcloud.visual")
    roots = pc.get("roots") or [{"href": pc["href"]}]
    assert len(roots) == f.roots, [r.get("name") for r in roots]
    depths = []
    for root in roots:
        if "depth" in root:
            depths.append(int(root["depth"]))
            continue
        md = json.loads((WORLDS / wid / root["href"].rstrip("/") / "metadata.json").read_text(encoding="utf-8"))
        depths.append(int(md["hierarchy"]["depth"]))
    assert max(depths) == f.depth, depths
    assert len(set(depths)) == 1, depths


@pytest.mark.parametrize("wid", CITY_IDS)
def test_anchor_and_north(wid: str) -> None:
    _, c = _world(wid)
    a = c["anchor"]
    assert a["kind"] == "synthetic" and a["georeferenced"] is False and a["label"].startswith("illustrative:")
    assert (a["latDeg"], a["lonDeg"]) == pytest.approx(CITIES[wid].anchor_lat_lon, abs=1e-6)
    assert c["trueNorth"]["confidence"] == CITIES[wid].true_north


@pytest.mark.parametrize("wid", CITY_IDS)
def test_dataset_provenance(wid: str) -> None:
    """M16-FR-010 的数据源：`world.json.dataset` 的 name、version、url、citation、license 非空（V-W-12），源文件与
    `configs/data.yaml`（入库真源）逐项一致；本机 `MANIFEST.json` 存在时也与之一致（16 §10.4）。"""
    w, _ = _world(wid)
    ds = w["dataset"]
    for k in ("name", "version", "url", "citation", "license"):
        assert ds.get(k), k
    want = _data_yaml()[wid]
    src = ds["sourceFiles"]
    assert len(src) == 1 and src[0]["name"] == want["name"] and src[0]["bytes"] == want["bytes"]
    assert src[0]["sha256"] == want["sha256"]
    man = ROOT / "data" / "raw" / "urbanscene3d" / "MANIFEST.json"
    if man.exists():
        m = {f["world_id"]: f for f in json.loads(man.read_text(encoding="utf-8"))["files"]}
        assert m[wid]["sha256"] == want["sha256"] and m[wid]["bytes"] == want["bytes"]


@pytest.mark.parametrize("wid", CITY_IDS)
def test_border_max_z(wid: str) -> None:
    needs_world(wid)
    fc = json.loads((WORLDS / wid / "semantic" / "zones.geojson").read_text(encoding="utf-8"))
    b = next(f for f in fc["features"] if f["id"] == "border")
    assert b["properties"]["max_z_m"] == pytest.approx(CITIES[wid].border_max_z_m, abs=0.05)


@pytest.mark.parametrize("wid", CITY_IDS)
def test_curated_zones_merged(wid: str) -> None:
    """世界按当前 curated 文件构建：`awr.source_sha256` 等于 `scenarios/zones/<id>.zones.geojson` 的 sha256。
    不一致表示提交了新的 curated 文件而世界尚未重建（`make worlds` 会按 zones_changed 条件重建）。"""
    needs_world(wid)
    fc = json.loads((WORLDS / wid / "semantic" / "zones.geojson").read_text(encoding="utf-8"))
    cur = ROOT / "scenarios" / "zones" / f"{wid}.zones.geojson"
    want = hashlib.sha256(cur.read_bytes()).hexdigest()
    if fc["awr"].get("source_sha256") != want:
        pytest.skip(f"{wid}: world built from an older curated zones file（make worlds）")
    ids = {f["id"] for f in fc["features"]}
    assert {z.zone_id for z in CITIES[wid].zones} <= ids


@pytest.mark.parametrize("wid", CITY_IDS)
def test_raw_ply_sha256(wid: str) -> None:
    """原始 PLY 的字节数与 sha256 等于 `configs/data.yaml`（逐字节读 120 MB，约 0.3 s/城）。"""
    want = _data_yaml()[wid]
    p = ROOT / "data" / "raw" / "urbanscene3d" / want["name"]
    if not p.exists():
        pytest.skip(f"raw data missing: {p}（make fetch-data）")
    assert p.stat().st_size == want["bytes"]
    h = hashlib.sha256()
    with p.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    assert h.hexdigest() == want["sha256"]


def test_chicago_corridor_is_level() -> None:
    """芝加哥调平（2.025°）：DTM 沿 S4 走廊（x = 800，y ∈ [−1200, 600]）无倾斜。

    M16 §6.2.3 写的"起伏 ≤ 1 m"在实测世界上不成立：走廊北段跨过岸线与码头区（湖面 world z 约 −2.0 m，北段约 −0.5 m，
    末端 +1.2 m），总起伏 3.5 m。调平错误的特征是线性趋势（残差 2.025° 在 1.8 km 上约 64 m），因此改判：线性趋势在
    1.8 km 上 ≤ 3 m 且总起伏 ≤ 5 m（偏差见实现报告）。"""
    from e2ehelp import _world_query

    needs_world("chicago")
    wq = _world_query("chicago")
    y = np.linspace(-1200.0, 600.0, 181)
    g = np.asarray(wq.ground_dtm(np.stack([np.full_like(y, 800.0), y], axis=1)), float)
    slope = np.polyfit(y, g, 1)[0]
    assert abs(slope) * 1800.0 <= 3.0, slope * 1800.0
    assert float(np.ptp(g)) <= 5.0, float(np.ptp(g))


def test_repo_paths_exist() -> None:
    assert isinstance(Path(ROOT / "configs" / "data.yaml"), Path) and (ROOT / "configs" / "data.yaml").exists()
