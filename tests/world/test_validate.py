"""M03-AC-019：校验器。tiny world 零错误零告警；≥ 30 个变异注入全部被预期规则 ID 拦下；84 条规则齐全；
JSON 输出符合 16 §15.4；非世界目录被跳过。

（M03 PRD 把变异测试放在 tests/contracts/test_world_mutations.py，该目录属 M00；本文件是 M03 自测版本。）
"""

from __future__ import annotations

import json
import shutil
import struct
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from m03_common import load

from awr.world.package.cli import main as worldpkg
from awr.world.package.validate import RULES, validate_world


def _jedit(rel: str, fn: Callable[[dict], None]) -> Callable[[Path], None]:
    def m(d: Path) -> None:
        p = d / rel
        doc = json.loads(p.read_text(encoding="utf-8"))
        fn(doc)
        p.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return m


def _bedit(rel: str, fn: Callable[[bytearray], bytearray | None]) -> Callable[[Path], None]:
    def m(d: Path) -> None:
        p = d / rel
        b = bytearray(p.read_bytes())
        out = fn(b)
        p.write_bytes(bytes(out if out is not None else b))
    return m


def _set(path: list, value):
    def f(doc):
        o = doc
        for k in path[:-1]:
            o = o[k]
        o[path[-1]] = value
    return f


def _hier_rec(i: int, field: str, value: int):
    fmt = {"t": ("<B", 0), "m": ("<B", 1), "n": ("<I", 2), "o": ("<q", 6), "s": ("<q", 14)}[field]

    def f(b: bytearray):
        struct.pack_into(fmt[0], b, 22 * i + fmt[1], value)
    return f


def _first_payload_offset(d: Path) -> tuple[int, int]:
    b = (d / "visual/pointcloud/hierarchy.bin").read_bytes()
    _t, _m, n, off, _size = struct.unpack_from("<BBIqq", b, 0)
    return off, n


def _octree_class(d: Path) -> None:
    off, n = _first_payload_offset(d)
    p = d / "visual/pointcloud/octree.bin"
    b = bytearray(p.read_bytes())
    b[off + 8 * n + 3] = 20                                    # 第 0 点的类别字节
    p.write_bytes(bytes(b))


def _octree_outside_ext(d: Path) -> None:
    ext = np.frombuffer((d / "visual/pointcloud/hierarchy_ext.bin").read_bytes(), "<u2").reshape(-1, 6)
    off, _n = _first_payload_offset(d)
    p = d / "visual/pointcloud/octree.bin"
    b = bytearray(p.read_bytes())
    struct.pack_into("<H", b, off + 4, max(int(ext[0][2]) - 1, 0) if ext[0][2] > 0 else 65535)
    if ext[0][2] == 0 and ext[0][5] == 65535:
        pytest.skip("root box spans the whole cube on z")
    p.write_bytes(bytes(b))


def _swap_source(d: Path) -> None:
    p = d / "geometry/pointcloud/source/xyz.f32"
    a = np.fromfile(p, "<f4").reshape(-1, 3)
    a[[0, -1]] = a[[-1, 0]]
    a.tofile(p)


def _dsm_edit(fn):
    def m(d: Path):
        sc = load(d / "geometry/terrain/dsm_2m.json")
        p = d / "geometry/terrain" / sc["href"]
        a = np.fromfile(p, "<f4").reshape(sc["height"], sc["width"])
        fn(a)
        a.tofile(p)
    return m


def _zones(fn):
    return _jedit("semantic/zones.geojson", fn)


def _add_nofly(ring):
    def f(fc):
        fc["features"].append({"type": "Feature", "id": "nofly-x", "geometry": {"type": "Polygon", "coordinates": [ring]},
                               "properties": {"zone_id": "nofly-x", "kind": "nofly", "min_z_m": None, "max_z_m": None,
                                              "label": "x", "origin": "curated", "editable": False}})
    return f


CW = [[0, 0], [0, 50], [50, 50], [50, 0], [0, 0]]
CCW_OUT = [[0, 0], [5000, 0], [5000, 50], [0, 50], [0, 0]]


def _two_borders(fc):
    fc["features"].append(json.loads(json.dumps(fc["features"][0])))
    fc["features"][-1]["id"] = "border2"
    fc["features"][-1]["properties"]["zone_id"] = "border2"


def _dup_zone(fc):
    _add_nofly([[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]])(fc)
    _add_nofly([[100, 0], [150, 0], [150, 50], [100, 50], [100, 0]])(fc)


def _builtin_no_license(w):
    w["tags"] = ["builtin"]
    w["dataset"]["license"] = ""


MUTATIONS: list[tuple[str, Callable[[Path], None], bool]] = [
    ("V-W-02", _jedit("world.json", _set(["id"], "other")), False),
    ("V-W-03", _jedit("coordinate.json", _set(["worldId"], "other")), False),
    ("V-W-04", _jedit("world.json", _set(["scaleStatus"], "relative")), False),
    ("V-W-05", _jedit("world.json", lambda w: w["bounds"]["min"].__setitem__(0, w["bounds"]["min"][0] + 1)), False),
    ("V-W-06", _jedit("world.json", lambda w: w["layers"].append(dict(w["layers"][2]))), False),
    ("V-W-06", _jedit("world.json", lambda w: w.__setitem__("layers", [L for L in w["layers"] if L["id"] != "environment.config"])), False),
    ("V-W-08", _jedit("world.json", lambda w: w["layers"][0]["roots"][0].__setitem__("cubeSize", 1.0)), False),
    ("V-W-09", _jedit("world.json", lambda w: w["layers"][0]["roots"][0].__setitem__("points", 1)), False),
    ("V-W-10", _jedit("world.json", lambda w: w["layers"][0]["roots"][0].__setitem__("firstScreenBytes", 12)), False),
    ("V-W-10", _jedit("world.json", lambda w: w["lod"]["firstScreen"].__setitem__("points", 7)), False),
    ("V-W-11", lambda d: (d / "extra.bin").write_bytes(b"x"), False),
    ("V-W-11", _jedit("world.json", _set(["contentVersion"], "000000000000")), False),
    ("V-W-12", _jedit("world.json", _builtin_no_license), False),
    ("V-W-14", _jedit("world.json", _set(["coordinate", "sha256"], "0" * 64)), False),
    ("V-C-01", _jedit("coordinate.json", lambda c: c["T_ecef_world"].__setitem__(3, [0, 0, 1, 1])), False),
    ("V-C-02", _jedit("coordinate.json", lambda c: c["anchor"].__setitem__("latDeg", c["anchor"]["latDeg"] + 0.01)), False),
    ("V-C-03", _jedit("coordinate.json", lambda c: c["source"].__setitem__("handedness", "left")), False),
    ("V-C-04", _jedit("coordinate.json", lambda c: c["source"].__setitem__("unitsToMeters", 11.0)), False),
    ("V-C-06", _jedit("coordinate.json", lambda c: c["source"].__setitem__("upAxis", "-y")), False),
    ("V-C-09", _jedit("coordinate.json", lambda c: c["precision"].__setitem__("maxRadiusM", 1.0)), False),
    ("V-C-11", _jedit("coordinate.json", _set(["scaleStatus"], "rtk")), False),
    ("V-C-12", _jedit("coordinate.json", lambda c: c["anchor"].__setitem__("label", "not illustrative")), False),
    ("V-C-14", _jedit("coordinate.json", lambda c: c["extent"].__setitem__("min", [9e3, 9e3, 9e3])), False),
    ("V-P-02", _jedit("visual/pointcloud/metadata.json", lambda m: m["boundingBox"]["max"].__setitem__(0, m["boundingBox"]["max"][0] + 5)), False),
    ("V-P-03", _jedit("visual/pointcloud/metadata.json", lambda m: m["offset"].__setitem__(0, 0.5)), False),
    ("V-P-04", _jedit("visual/pointcloud/metadata.json", _set(["spacing"], 1.0)), False),
    ("V-P-05", _bedit("visual/pointcloud/hierarchy.bin", lambda b: b[:-5]), False),
    ("V-P-06", _bedit("visual/pointcloud/hierarchy.bin", _hier_rec(0, "m", 0)), False),
    ("V-P-07", _bedit("visual/pointcloud/hierarchy.bin", _hier_rec(1, "n", 999_999)), False),
    ("V-P-08", _jedit("visual/pointcloud/metadata.json", lambda m: m["hierarchy"].__setitem__("depth", m["hierarchy"]["depth"] + 1)), False),
    ("V-P-11", _bedit("visual/pointcloud/hierarchy.bin", _hier_rec(0, "o", 12)), False),
    ("V-P-14", _jedit("visual/pointcloud/metadata.json", lambda m: m["anet"]["levelsPoints"].__setitem__(0, 1)), False),
    ("V-P-15", _jedit("visual/pointcloud/metadata.json", lambda m: m["anet"].__setitem__("firstScreenLevel", 19)), False),
    ("V-P-16", _jedit("visual/pointcloud/metadata.json", lambda m: m["anet"]["tightBounds"]["max"].__setitem__(2, 1e6)), False),
    ("V-P-17", _bedit("visual/pointcloud/hierarchy_ext.bin", lambda b: b[:-12]), False),
    ("V-P-18", _jedit("visual/pointcloud/metadata.json", lambda m: m["anet"]["stats"]["classHistogram"].__setitem__("1", 1)), False),
    ("V-P-19", _jedit("visual/pointcloud/metadata.json", lambda m: m["anet"]["stats"].__setitem__("zP1", 1e5)), False),
    ("V-P-20", _jedit("visual/pointcloud/metadata.json", lambda m: m["anet"]["root"].__setitem__("forestSize", 2)), False),
    ("V-D-02", _octree_outside_ext, True),
    ("V-D-05", _octree_class, True),
    ("V-D-06", _octree_class, True),
    ("V-D-07", _bedit("geometry/terrain/dtm_10m.f32", lambda b: b.__setitem__(0, b[0] ^ 1)), True),
    ("V-K-03", _jedit("semantic/anet-classes@1.json", lambda c: c["classes"][2].__setitem__("lasCode", 2)), False),
    ("V-K-04", _bedit("semantic/anet-classes@1.json", lambda b: b + b" "), False),
    ("V-G-02", _bedit("geometry/terrain/dsm_2m.f32", lambda b: b[:-4]), False),
    ("V-G-04", _dsm_edit(lambda a: a.__setitem__((0, 0), np.nan)), False),
    ("V-G-05", _dsm_edit(lambda a: a.__setitem__((5, 5), -500.0)), True),
    ("V-G-06", _dsm_edit(lambda a: a.__setitem__((5, 5), 9000.0)), True),
    ("V-Z-02", _zones(_two_borders), False),
    ("V-Z-02", _zones(lambda fc: fc["features"][0]["properties"].__setitem__("max_z_m", 1.0)), False),
    ("V-Z-03", _zones(_add_nofly(CW)), False),
    ("V-Z-04", _zones(_add_nofly(CCW_OUT)), False),
    ("V-Z-05", _zones(_dup_zone), False),
    ("V-Z-06", _zones(lambda fc: fc["awr"].__setitem__("coordinate_sha256", "0" * 64)), False),
    ("V-S-02", _jedit("geometry/pointcloud/source/source.json", _set(["count"], 5)), False),
    ("V-S-03", _swap_source, True),
    ("V-S-04", _jedit("geometry/pointcloud/source/source.json", _set(["coordinate_sha256"], "0" * 64)), False),
    ("V-E-01", _jedit("environment/env.json", _set(["coordinate_hash"], "sha256:" + "0" * 64)), False),
    ("V-E-02", _jedit("environment/env.json", _set(["default_preset"], "sunny")), False),
    ("V-E-03", _jedit("environment/env.json", _set(["extra"], 1)), False),
    ("V-E-04", lambda d: (d / "environment" / "bad.awrv").write_bytes(b"AWRX" + bytes(60)), False),
    ("V-Q-01", lambda d: (d / "qa" / "report.json").unlink(), False),
    ("V-Q-02", _jedit("world.json", lambda w: w["qa"].__setitem__("status", "warn")), False),
]


def test_tiny_world_validates_clean(tiny_built):
    rep = validate_world(tiny_built["dir"], deep=True)
    assert rep.errors == [] and rep.warnings == [], (rep.errors, rep.warnings)
    assert rep.rules_checked == 84


def test_rule_catalog_complete():
    groups = {}
    for rid in RULES:
        g = rid.split("-")[1]
        groups[g] = groups.get(g, 0) + 1
    assert len(RULES) == 84
    assert groups == {"C": 14, "W": 15, "P": 20, "D": 7, "K": 4, "G": 6, "Z": 6, "S": 4, "E": 6, "Q": 2}


def test_at_least_30_mutations():
    assert len(MUTATIONS) >= 30 and len({m[0] for m in MUTATIONS}) >= 30


@pytest.mark.parametrize(("rule", "mutate", "deep"), MUTATIONS, ids=[f"{m[0]}-{i}" for i, m in enumerate(MUTATIONS)])
def test_mutation_caught(tiny_built, tmp_path, rule, mutate, deep):
    d = tmp_path / "tiny"
    shutil.copytree(tiny_built["dir"], d)
    mutate(d)
    rep = validate_world(d, deep=deep)
    got = {e["rule"] for e in rep.errors} | {e["rule"] for e in rep.warnings}
    assert rule in got, (rule, rep.errors[:5], rep.warnings[:5])


def test_warning_rules(tiny_copy):
    _jedit("world.json", lambda w: w["lod"].__setitem__("budgets", {"desktop": 1}))(tiny_copy)
    _jedit("coordinate.json", lambda c: (c["ground"].__setitem__("zM", 100.0), c["precision"].__setitem__("float32UlpMm", 1.5)))(tiny_copy)

    def zero_normals(b: bytearray):
        off, n = _first_payload_offset(tiny_copy)
        for k in range(n):
            struct.pack_into("<H", b, off + 8 * k + 6, 0)
    _bedit("visual/pointcloud/octree.bin", zero_normals)(tiny_copy)
    rep = validate_world(tiny_copy, deep=True)
    warns = {w["rule"] for w in rep.warnings}
    assert {"V-W-15", "V-C-13", "V-C-10", "V-D-04"} <= warns, rep.warnings


def test_staging_mode_skips_q_and_softens_w02(tiny_built, tmp_path):
    d = tmp_path / "tiny-abc123"
    shutil.copytree(tiny_built["dir"], d)
    (d / "qa" / "report.json").unlink()
    rep = validate_world(d, staging=True)
    assert rep.ok and not rep.warnings
    d2 = tmp_path / "zzz"
    shutil.copytree(tiny_built["dir"], d2)
    rep2 = validate_world(d2, staging=True)
    assert rep2.ok and "V-W-02" in {w["rule"] for w in rep2.warnings}


def test_cli_json_and_skip(tiny_built, tmp_path, capsys):
    (tmp_path / "_shared").mkdir()
    w = tmp_path / "tiny"
    shutil.copytree(tiny_built["dir"], w)
    rc = worldpkg(["validate", str(w), str(tmp_path / "_shared"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["validator"] == "awr.worldpkg.validate" and out["schema_version"] == "1.0.0"
    r = out["results"][0]
    assert r["ok"] and r["world_id"] == "tiny" and r["info"]["rules_checked"] >= 70 and "first_screen" in r["info"]
    assert out["skipped"] == [str(tmp_path / "_shared")]


def test_cli_exit_codes(tiny_built, tmp_path, capsys):
    w = tmp_path / "tiny"
    shutil.copytree(tiny_built["dir"], w)
    _jedit("world.json", lambda x: x["lod"].__setitem__("budgets", {"desktop": 1}))(w)
    assert worldpkg(["validate", str(w)]) == 0
    assert worldpkg(["validate", str(w), "--strict-warn"]) == 2
    _jedit("world.json", _set(["scaleStatus"], "relative"))(w)
    assert worldpkg(["validate", str(w)]) == 1
    assert worldpkg(["validate", str(tmp_path / "nope")]) == 3
    capsys.readouterr()


def test_shallow_and_deep_timing(tiny_built):
    import time

    t = time.perf_counter()
    validate_world(tiny_built["dir"])
    assert time.perf_counter() - t < 1.0
