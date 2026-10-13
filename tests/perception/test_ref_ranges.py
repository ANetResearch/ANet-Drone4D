"""CAT-AC-016（P1，d2-WP-08-to-M19 第 2 条）：参考作用距离表与 AWR-20 §6.9 一致（±1%；NIR 与表的取整 ±1 m）。"""

from __future__ import annotations

import pytest

from awr.sim.perception import gx40, gx40_nir, lwir640, mid360, mmw77, ref_ranges

EO = {  # (sensor, target, MOR): (D, R, I)
    ("1080p", "person", 10000.0): (2422, 845, 527), ("1080p", "person", 3000.0): (1196, 661, 505),
    ("1080p", "vehicle", 10000.0): (3951, 1913, 1196), ("1080p", "vehicle", 3000.0): (1673, 1107, 908),
    ("4k", "person", 10000.0): (3029, 1359, 850), ("4k", "person", 3000.0): (1379, 838, 663),
    ("4k", "vehicle", 10000.0): (4627, 2681, 1928), ("4k", "vehicle", 3000.0): (1838, 1311, 1109),
}
NIR = {
    (False, "person", 10000.0): (230, 220, 213), (False, "person", 3000.0): (209, 200, 195),
    (False, "vehicle", 10000.0): (244, 239, 235), (False, "vehicle", 3000.0): (220, 216, 213),
    (True, "person", 10000.0): (504, 458, 427), (True, "person", 3000.0): (417, 386, 364),
    (True, "vehicle", 10000.0): (538, 514, 497), (True, "vehicle", 3000.0): (441, 425, 413),
}


def _ok(got: float, want: float, rel: float = 0.01, abs_: float = 1.5) -> bool:
    return abs(got - want) <= max(rel * want, abs_)


@pytest.mark.parametrize("res", ["1080p", "4k"])
def test_eo(res):
    for r in ref_ranges(gx40(res)):
        want = EO[(res, r["target"], r["mor_m"])]
        assert all(_ok(r[f"{lv}_m"], w) for lv, w in zip("DRI", want, strict=True)), (r, want)


@pytest.mark.parametrize("ext", [False, True])
def test_nir(ext):
    for r in ref_ranges(gx40_nir(ext)):
        want = NIR[(ext, r["target"], r["mor_m"])]
        assert all(_ok(r[f"{lv}_m"], w) for lv, w in zip("DRI", want, strict=True)), (r, want)


def test_lwir_and_pixels_only():
    rows = {(lens, r["target"]): r for lens in (13.0, 25.0, 75.0, 100.0) for r in ref_ranges(lwir640(lens), mors=(10000.0,))}
    for lens, rr in zip((13.0, 25.0, 75.0, 100.0), (56, 108, 321, 425), strict=True):
        assert _ok(rows[(lens, "person")]["R_m"], rr)
    for lens, dd in zip((13.0, 25.0, 75.0, 100.0), (224, 431, 1283, 1700), strict=True):
        assert _ok(rows[(lens, "person")]["D_m"], dd)
    for lens, rr in zip((13.0, 25.0, 75.0, 100.0), (64, 122, 364, 482), strict=True):
        assert _ok(rows[(lens, "vehicle")]["R_m"], rr)
    px = ref_ranges(gx40("1080p"), pixels_only=True)
    assert _ok(px[0]["D_m"], 3427) and _ok(px[0]["R_m"], 857) and _ok(px[0]["I_m"], 535)


def test_radar_and_lidar():
    rd = {r["target"]: r for r in ref_ranges(mmw77())}
    for cls, d, r in (("person", 151, 102), ("dog", 101, 68), ("uav_small", 57, 38), ("vehicle", 320, 216), ("machine", 214, 145), ("object", 133, 90)):
        assert _ok(rd[cls]["D_m"], d) and _ok(rd[cls]["R_m"], r)
    li = {r["target"]: r for r in ref_ranges(mid360())}
    assert li["person"]["D_m"] == pytest.approx(42.9, abs=0.1) and li["person"]["R_m"] == pytest.approx(13.6, abs=0.1)
    assert li["vehicle"]["D_m"] == pytest.approx(61.7, abs=0.1) and li["vehicle"]["R_m"] == pytest.approx(48.6, abs=0.1)
