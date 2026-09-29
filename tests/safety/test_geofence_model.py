"""M09-AC-013：围栏加载与对拍——`coordinate_sha256` 不一致拒绝加载（352）；`zones.active` 选择生效；`effective_max_z` 取小；
`geofence_scan`（numba）与 numpy oracle 逐位一致，并与 M04 ZoneIndex 在 FakeWorldQuery（含凹多边形禁飞区）与六城 zones 上各
10⁴ 个随机点对拍：包含判定逐位一致、距离差 ≤ 1e-9 m。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from awr.sim.safety import kernels as KN
from awr.sim.safety.geofence import GeofenceModel
from awr.sim.safety.params import FenceParams
from awr.world.geometry.types import GeoLoadError
from awr.world.geometry.zones import ZoneIndex

ROOT = Path(__file__).resolve().parents[2]
CITIES = ("shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago")


def _scan_both(g: GeofenceModel, P: np.ndarray) -> tuple:
    n = len(P)
    idx = np.arange(n)

    def run(fn):
        o = [np.full(n, np.inf), np.full(n, np.inf), np.full(n, -1, np.int64), np.full(n, -1, np.int64),
             np.full(n, -1, np.int64), np.full(n, np.inf)]
        fn(P, idx, g.bea, g.beb, g.zea, g.zeb, g.zstart, g.zcount, g.zmin, g.zmax, g.zbbox, g.zkind, g.zactive, g.cap_m, *o)
        return o

    return run(KN.geofence_scan), run(KN.geofence_scan_np)


def _check_against_zoneindex(g: GeofenceModel, zi: ZoneIndex, P: np.ndarray) -> None:
    nb, npy = _scan_both(g, P)
    if KN.HAVE_NUMBA:
        for a, b in zip(nb, npy, strict=True):
            assert np.array_equal(a, b)  # 逐位一致（含距离）
    margin, border, nofly, restr, _near, _near_d = npy
    bsd = zi.border_signed_distance(P[:, :2])
    assert np.array_equal(border >= 0, zi.border.contains_xy(P[:, :2]))
    assert np.max(np.abs(border - bsd)) <= 1e-9
    assert np.array_equal(nofly >= 0, zi.contains(P, {"nofly"}))
    assert np.array_equal(restr >= 0, zi.contains(P, {"restricted"}))
    # 外部点：到最近 nofly 的距离（cap 内）与 ZoneIndex.nearest_zone_distance 一致
    if zi.nofly:
        nz = zi.nearest_zone_distance(P[:, :2], {"nofly"}, g.cap_m)
        out = ~np.any([p.contains_xy(P[:, :2]) for p in zi.nofly], axis=0)
        fin = out & np.isfinite(nz)
        # margin 取 border 与 nofly 外距离的小者
        assert np.all(margin[fin] <= nz[fin] + 1e-9)
        k = fin & (nz < border)
        assert np.max(np.abs(margin[k] - nz[k]), initial=0.0) <= 1e-9


def test_tiny_world_oracle(tiny_world) -> None:
    rng = np.random.default_rng(13)
    P = np.c_[rng.uniform(-200, 200, 10000), rng.uniform(-150, 150, 10000), rng.uniform(0, 160, 10000)]
    g = GeofenceModel(tiny_world, FenceParams())
    assert g.zone_names == ["nofly-l", "restricted-a"]
    _check_against_zoneindex(g, tiny_world.zones, P)
    # zones.active：只生效 restricted 时 nofly 不命中
    g.set_active(["restricted-a"])
    inb, innf, _ = g.point_status(np.array([[80.0, -50.0, 20.0]]))
    assert inb[0] and not innf[0]
    g.set_active(None)
    assert g.point_status(np.array([[80.0, -50.0, 20.0]]))[1][0]


def test_effective_max_z(tiny_world) -> None:
    g = GeofenceModel(tiny_world, FenceParams())
    assert g.max_z == pytest.approx(float(tiny_world.zones.border.zmax))
    assert GeofenceModel(tiny_world, FenceParams(), profile_max_z=120.0).max_z == 120.0
    assert GeofenceModel(tiny_world, FenceParams(max_z_m=90.0), profile_max_z=120.0).max_z == 90.0


def _synthetic_fc(fc: dict, rng: np.random.Generator) -> dict:
    """在城市 border 内追加随机凹、凸多边形禁飞区与限制区（城市 zones 目前只有 border）。"""
    b = next(f for f in fc["features"] if f["properties"]["kind"] == "border")
    ring = np.asarray(b["geometry"]["coordinates"][0], np.float64)
    lo, hi = ring.min(0), ring.max(0)
    feats = list(fc["features"])
    for k in range(8):
        c = rng.uniform(lo + 100, hi - 100)
        n = int(rng.integers(4, 16))
        a = np.sort(rng.uniform(0, 2 * np.pi, n))
        r = rng.uniform(20, 80) * rng.uniform(0.4, 1.0, n)
        pts = c + np.c_[np.cos(a), np.sin(a)] * r[:, None]
        coords = [list(map(float, p)) for p in pts] + [list(map(float, pts[0]))]
        kind = "nofly" if k % 3 else "restricted"
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [coords]},
                      "properties": {"zone_id": f"z{k}", "kind": kind, "min_z_m": None if k % 2 else 10.0,
                                     "max_z_m": float(rng.uniform(60, 300)), "label": ""}})
    return {**fc, "features": feats}


@pytest.mark.needs_data
@pytest.mark.parametrize("city", CITIES)
def test_city_zones_oracle(city: str) -> None:
    path = ROOT / "worlds" / city / "semantic" / "zones.geojson"
    wj = ROOT / "worlds" / city / "world.json"
    if not path.exists() or not wj.exists():
        pytest.skip(f"worlds/{city} 未构建")
    sha = json.loads(wj.read_text(encoding="utf-8"))["coordinate"]["sha256"]
    zi = ZoneIndex.load(path, expect_coord_sha=sha)
    with pytest.raises(GeoLoadError) as e:
        ZoneIndex.load(path, expect_coord_sha="0" * 64)
    assert e.value.code == "GEO_SHA_MISMATCH"  # 剧本加载以 352 COORDINATE_MISMATCH 失败
    rng = np.random.default_rng(int.from_bytes(city.encode()[:4], "little"))
    zi2 = ZoneIndex(_synthetic_fc(zi.fc, rng))
    g = GeofenceModel(SimpleNamespace(zones=zi2, height_dsm=lambda xy: np.zeros(len(xy))), FenceParams())
    ring = np.asarray(zi.border.polygons[0][0])
    lo, hi = ring.min(0) - 40, ring.max(0) + 40
    P = np.c_[rng.uniform(lo[0], hi[0], 10000), rng.uniform(lo[1], hi[1], 10000), rng.uniform(0, 400, 10000)]
    _check_against_zoneindex(g, zi2, P)
