"""M03-AC-013、AC-009：六城根数、深度、节点数、`hierarchy.bin` 字节与首屏两条规则的层、点、字节与 AWR-16 §4.11 表一致；
每根取 `[0, levelsByteEnd[L])` 恰好 `levelsPoints[L]` 个点；octree.bin = 12 × 点数；每城包 ≤ 220 MB、六城 ≤ 1.2 GB；
DTM、DSM 尺寸与 16 §6.3 表一致（needs_data）。"""

from __future__ import annotations

import os

import pytest
from m03_common import WORLDS, load, needs_worlds

pytestmark = [pytest.mark.needs_data, needs_worlds]

TABLE = {  # 根 / 深度 / 节点、hierarchy 字节、Tier S (L, 点, 字节)、Tier B/A (L, 点, 字节)、规则 G (点, 字节)
    "shenzhen": (1, 5, 696, 15312, (1, 26782, 321384), (2, 124673, 1496076), (124673, 1496076)),
    "shanghai": (1, 5, 676, 14872, (2, 75154, 901848), (3, 418818, 5025816), (418818, 5025816)),
    "newyork": (1, 5, 852, 18744, (1, 35351, 424212), (2, 181771, 2181252), (181771, 2181252)),
    "sanfrancisco": (1, 5, 658, 14476, (1, 25559, 306708), (2, 108268, 1299216), (108268, 1299216)),
    "suzhou": (6, 4, 879, 19338, (0, 20327, 243924), (1, 108263, 1299156), (166529, 1998348)),
    "chicago": (1, 6, 787, 17314, (2, 76541, 918492), (3, 352915, 4234980), (352915, 4234980)),
}
GRIDS = {"shenzhen": ((185, 200), (925, 1000)), "shanghai": ((774, 622), (3869, 3106)), "newyork": ((293, 317), (1465, 1584)),
         "sanfrancisco": ((728, 752), (3639, 3757)), "suzhou": ((441, 69), (2204, 344)), "chicago": ((418, 804), (2088, 4019))}


@pytest.mark.parametrize("city", list(TABLE))
def test_first_screen_table(city):
    roots_n, depth, nodes, hier, tier_s, tier_ba, rule_g = TABLE[city]
    w = load(WORLDS / city / "world.json")
    roots = w["layers"][0]["roots"]
    mds = [load(WORLDS / city / r["href"] / "metadata.json") for r in roots]
    assert len(roots) == roots_n and max(r["depth"] for r in roots) == depth
    assert sum(m["anet"]["nodeCount"] for m in mds) == nodes
    assert sum(os.path.getsize(WORLDS / city / r["href"] / "hierarchy.bin") for r in roots) == hier
    rep = load(WORLDS / city / "qa" / "report.json")["first_screen"]
    assert (rep["tier_s"]["level"], rep["tier_s"]["points"], rep["tier_s"]["bytes"]) == tier_s
    assert (rep["tier_ba"]["level"], rep["tier_ba"]["points"], rep["tier_ba"]["bytes"]) == tier_ba
    assert (w["lod"]["firstScreen"]["points"], w["lod"]["firstScreen"]["bytes"]) == rule_g
    for r, m in zip(roots, mds, strict=True):
        a = m["anet"]
        size = os.path.getsize(WORLDS / city / r["href"] / "octree.bin")
        assert size == 12 * m["points"] and a["levelsByteEnd"][-1] == size
        assert all(e == 12 * p for e, p in zip(a["levelsByteEnd"], a["levelsPoints"], strict=True))


@pytest.mark.parametrize("city", list(GRIDS))
def test_grid_sizes(city):
    (dw, dh), (sw, sh) = GRIDS[city]
    dtm = load(WORLDS / city / "geometry/terrain/dtm_10m.json")
    dsm = load(WORLDS / city / "geometry/terrain/dsm_2m.json")
    n = load(WORLDS / city / "geometry/terrain/dsm_2m_n.json")
    assert (dtm["width"], dtm["height"]) == (dw, dh) and (dsm["width"], dsm["height"]) == (sw, sh) == (n["width"], n["height"])


def test_package_sizes():
    total = 0
    for city in TABLE:
        d = WORLDS / city
        size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(d) for f in fs if "export" not in dp)
        assert size <= 220e6, (city, size)
        total += size
    assert total <= 1.2e9


def test_prefix_decodes_exact_points():
    import numpy as np

    for city in ("shenzhen", "suzhou"):
        w = load(WORLDS / city / "world.json")
        for r in w["layers"][0]["roots"]:
            a = load(WORLDS / city / r["href"] / "metadata.json")["anet"]
            L = a["firstScreenLevel"]
            with open(WORLDS / city / r["href"] / "octree.bin", "rb") as f:
                buf = f.read(a["levelsByteEnd"][L])
            assert len(buf) == 12 * a["levelsPoints"][L]
            assert np.frombuffer(buf, np.uint8).size % 12 == 0
