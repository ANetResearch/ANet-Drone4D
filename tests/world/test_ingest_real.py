"""M03-AC-003、AC-006：六城坐标清单与规范化派生值对照 AWR-16 §10.2、§10.3（读取已构建的 worlds/，needs_data）。"""

from __future__ import annotations

import numpy as np
import pytest
from m03_common import WORLDS, load, needs_worlds

pytestmark = [pytest.mark.needs_data, needs_worlds]

T_WORLD_SOURCE = {  # 16 §10.2（行主序前三行）
    "shenzhen": [[1, 0, 0, -318.431854], [0, 1, 0, -250.993927], [0, 0, 1, 31.908033]],
    "shanghai": [[1, 0, 0, -3184.087891], [0, 1, 0, 1308.724976], [0, 0, 1, 6.317375]],
    "newyork": [[1, 0, 0, -17.047424], [0, 1, 0, 24.386719], [0, 0, 1, 16.502184]],
    "sanfrancisco": [[0, -10.15, 0, 0.370465], [10.15, 0, 0, 0.425911], [0, 0, 10.15, 164.727487]],
    "suzhou": [[1, 0, 0, -315.931274], [0, 0, -1, -689.211945], [0, 1, 0, -0.280154]],
    "chicago": [[999.483188, -0.235836, -32.14499, 9009.183787], [-0.235836, 999.892381, -14.668689, -3489.145297],
                [32.14499, 14.668689, 999.37557, 209.316774]],
}
ANCHOR = {  # 16 §10.2 示意锚点
    "shenzhen": (22.5160584, 113.9432472, 12.2, 50), "shanghai": (31.2281892, 121.5316942, 4.0, 50),
    "newyork": (40.7130611, -74.0023445, 6.3, 50), "sanfrancisco": (37.7791216, -122.4212116, 39.5, 50),
    "suzhou": (31.30, 120.62, 3.0, 5000), "chicago": (41.8841302, -87.6225307, 178.9, 50),
}
DERIVED = {  # 16 §10.3：范围 E×N×U、maxRadius、ULP、翻正、零法线、地面占比、nnMedian、着色、最高 world z
    "shenzhen": ((1848, 1999, 391), 1361.2, 0.061, 0.164, 0.0, 0.357, 0.526, "height", 374.05),
    "shanghai": ((7736, 6211, 645), 4960.4, 0.244, 0.209, 0.0015, 0.255, 1.845, "height", 636.73),
    "newyork": ((2928, 3167, 292), 2156.5, 0.122, 0.114, 0.0, 0.152, 1.016, "height", 287.00),
    "sanfrancisco": ((7278, 7512, 558), 5229.6, 0.244, 0.036, 0.0, 0.385, 1.887, "hag", 443.20),
    "suzhou": ((4407, 686, 155), 2230.0, 0.244, 0.211, 0.0, 0.004, 0.348, "height", 152.37),
    "chicago": ((4176, 8038, 451), 4528.8, 0.244, 0.027, 0.0, 0.560, 1.587, "height", 445.24),
}
PEAK_HAG = {"shenzhen": 381.3, "shanghai": 636.7, "newyork": 287.4, "chicago": 443.1}


@pytest.mark.parametrize("city", list(T_WORLD_SOURCE))
def test_coordinate(city):
    c = load(WORLDS / city / "coordinate.json")
    T = np.asarray(c["source"]["T_world_source"])[:3]
    assert np.abs(T - np.asarray(T_WORLD_SOURCE[city])).max() <= 1e-6
    lat, lon, h, unc = ANCHOR[city]
    a = c["anchor"]
    assert abs(a["latDeg"] - lat) <= 1e-6 and abs(a["lonDeg"] - lon) <= 1e-6 and abs(a["hMslM"] - h) <= 0.05
    assert a["uncertaintyM"]["horizontal"] == unc and a["label"].startswith("illustrative:") and a["kind"] == "synthetic"
    assert c["conventions"]["px4Boundary"] == "NED/FRD at PX4 adapter boundary only"


@pytest.mark.parametrize("city", list(DERIVED))
def test_derived(city):
    ext, rmax, ulp, flip, zero, ground, nn, mode, zmax = DERIVED[city]
    c = load(WORLDS / city / "coordinate.json")
    w = load(WORLDS / city / "world.json")
    lo, hi = np.asarray(c["extent"]["min"]), np.asarray(c["extent"]["max"])
    assert np.abs((hi - lo) - np.asarray(ext)).max() <= 1.0
    assert abs(c["precision"]["maxRadiusM"] - rmax) <= 0.1 and abs(c["precision"]["float32UlpMm"] - ulp) <= 0.001
    q = c["qa"]
    assert abs(q["normalsFlippedFrac"] - flip) <= 0.001 + 1e-9
    assert abs(q["zeroNormalsFrac"] - zero) <= 0.0001 + 1e-9
    assert abs(q["groundFrac"] - ground) <= 0.001 + 1e-9
    assert abs(w["render"]["nnMedianM"] - nn) <= 0.005
    assert w["render"]["defaultColorMode"] == mode
    assert abs(hi[2] - zmax) <= 0.05
    if city in PEAK_HAG:
        g01 = next(g for g in q["gates"] if g["name"].startswith("G-01"))
        assert abs(g01["value"] - PEAK_HAG[city]) <= 2.0


def test_newyork_class_histogram_equals_g03():
    md = load(WORLDS / "newyork" / "visual/pointcloud/metadata.json")
    assert md["anet"]["stats"]["classHistogram"] == {"0": 91064, "1": 726199, "5": 1528938, "6": 2603626, "7": 50238}


def test_levelling_and_synthetic_ground():
    ch = load(WORLDS / "chicago" / "coordinate.json")
    assert abs(ch["source"]["leveledDeg"] - 2.025) <= 0.01
    sz = load(WORLDS / "suzhou" / "coordinate.json")
    assert sz["ground"]["type"] == "synthetic" and load(WORLDS / "suzhou" / "world.json")["render"]["syntheticGroundZ"] == 0.0


def test_all_gates_pass():
    for city in DERIVED:
        r = load(WORLDS / city / "qa" / "report.json")
        assert r["status"] == "pass" and all(g["pass"] or g["severity"] == "info" for g in r["gates"])
