"""M03-AC-021：`--missing` 的 6 个条件逐一构造（删包、改一字节、降生成器版本、改主版本、改原始文件 sha256、
改 curated zones），判定结果正确；六城 `worldpkg status --json` 给出同一结论（needs_data）。"""

from __future__ import annotations

import json
import shutil

import pytest
from m03_common import WORLDS, needs_worlds

from awr.world.ingest.manifest import DataConfig, RawFileSpec
from awr.world.package.build import missing_reason
from awr.world.package.cli import main as worldpkg


@pytest.fixture
def setup(tiny_built, tmp_path, repo_env):
    worlds = tmp_path / "worlds"
    shutil.copytree(tiny_built["dir"], worlds / "tiny")
    raw = tmp_path / "raw"
    raw.mkdir()
    ply = raw / tiny_built["ply"].name
    shutil.copy(tiny_built["ply"], ply)
    spec = RawFileSpec("tiny", ply.name, ply.stat().st_size, 200_000, 0, tiny_built["sha"])
    data = DataConfig(archive={}, files=(spec,), raw={})
    return worlds, raw, data, repo_env


def _edit(p, fn):
    d = json.loads(p.read_text())
    fn(d)
    p.write_text(json.dumps(d, indent=1))


def test_up_to_date(setup):
    worlds, raw, data, _ = setup
    assert missing_reason(worlds, "tiny", raw_dir=raw, data=data)[0] is None


def test_1_missing(setup):
    worlds, raw, data, _ = setup
    shutil.rmtree(worlds / "tiny")
    assert missing_reason(worlds, "tiny", raw_dir=raw, data=data)[0] == "missing"


def test_2_one_byte_changed(setup):
    worlds, raw, data, _ = setup
    p = worlds / "tiny" / "semantic" / "anet-classes@1.json"
    b = bytearray(p.read_bytes())
    b[10] ^= 0x20
    p.write_bytes(bytes(b))
    assert missing_reason(worlds, "tiny", raw_dir=raw, data=data)[0] == "validate_failed"


def test_3_generator_outdated(setup):
    worlds, raw, data, _ = setup
    _edit(worlds / "tiny" / "world.json", lambda w: w["generator"].__setitem__("version", "0.0.1"))
    assert missing_reason(worlds, "tiny", raw_dir=raw, data=data)[0] == "generator_outdated"


def test_4_schema_major(setup):
    worlds, raw, data, _ = setup
    _edit(worlds / "tiny" / "world.json", lambda w: w.__setitem__("schemaVersion", "2.0.0"))
    assert missing_reason(worlds, "tiny", raw_dir=raw, data=data)[0] == "schema_major"


def test_5_raw_changed(setup):
    worlds, raw, data, _ = setup
    p = raw / data.files[0].name
    b = bytearray(p.read_bytes())
    b[-1] ^= 0x01
    p.write_bytes(bytes(b))
    reason, info = missing_reason(worlds, "tiny", raw_dir=raw, data=data)
    assert reason == "raw_changed" and info["raw_cache"]["sha256"] != data.files[0].sha256


def test_6_zones_changed(setup):
    worlds, raw, data, root = setup
    from awr.world.semantic.zones import example_curated

    doc = example_curated("tiny")
    doc["features"] = []
    (root / "scenarios" / "zones" / "tiny.zones.geojson").write_text(json.dumps(doc))
    try:
        assert missing_reason(worlds, "tiny", raw_dir=raw, data=data)[0] == "zones_changed"
    finally:
        (root / "scenarios" / "zones" / "tiny.zones.geojson").unlink()


def test_deep_env_checks_hashes(setup, monkeypatch):
    worlds, raw, data, _ = setup
    p = worlds / "tiny" / "geometry" / "terrain" / "dtm_10m.f32"
    b = bytearray(p.read_bytes())
    b[0] ^= 0x01
    p.write_bytes(bytes(b))
    assert missing_reason(worlds, "tiny", raw_dir=raw, data=data)[0] is None           # 浅校验不复算 sha256
    monkeypatch.setenv("WORLDPKG_VERIFY", "deep")
    assert missing_reason(worlds, "tiny", raw_dir=raw, data=data)[0] == "validate_failed"


@pytest.mark.needs_data
@needs_worlds
def test_status_json_six_cities(capsys):
    assert worldpkg(["status", "--json", "--worlds", str(WORLDS)]) == 0
    rows = json.loads(capsys.readouterr().out)["worlds"]
    assert {r["world_id"] for r in rows} == {"shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago"}
    for r in rows:
        if r["raw_present"]:
            assert r["rebuild"] is False, r
