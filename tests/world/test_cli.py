"""M03-AC-024：CLI。分阶段 ingest → grid → tile → package（经 .work 交换中间数组）与一次 build 产出相同
`contentVersion`；各子命令的退出码与 §7.1 一致；export 返回 3 与说明。"""

from __future__ import annotations

import json

import pytest
from tinyworld_m03 import TinyAdapter

from awr.world.ingest.types import StageContext
from awr.world.package.build import BuildPipeline
from awr.world.package.cli import main as worldpkg
from awr.world.package.params import BuildParams
from awr.world.package.schemas import schema_errors


def test_staged_equals_single_build(tiny_built, tmp_path, repo_env):
    stage = tmp_path / "S"
    stage.mkdir()
    p = BuildParams()
    a = BuildPipeline(stage, p, StageContext(quiet=True))
    a.run_ingest(TinyAdapter(tiny_built["ply"], tiny_built["sha"]))
    a.save_after("ingest")
    b = BuildPipeline(stage, p, StageContext(quiet=True))
    b.restore("grid")
    b.run_grid()
    b.save_after("grid")
    c = BuildPipeline(stage, p, StageContext(quiet=True))
    c.restore("tile")
    c.run_tile()
    c.save_after("tile")
    d = BuildPipeline(stage, p, StageContext(quiet=True))
    d.restore("package")
    d.run_derive()
    w = d.run_package()
    d.run_validate(deep=True)
    d.run_report()
    assert w["contentVersion"] == tiny_built["result"].content_version
    assert (stage / "qa" / "report.json").exists()
    assert [s["name"] for s in json.loads((stage / "qa" / "report.json").read_text())["stages"]][:5] == \
        ["read", "ingest", "dtm", "normals", "classify"]


def test_export_is_v05(capsys):
    assert worldpkg(["export", "shenzhen", "--tiles3d"]) == 3
    assert "V0.5" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["ingest", "recon", "--out", "x"], ["ingest", "generic", "--out", "x"],
                                  ["build", "--config", "a.yaml"], ["build", "atlantis"],
                                  ["ingest", "urbanscene3d", "--city", "shenzhen", "--out", "x", "--semantic", "csf"]])
def test_unsupported_is_exit_3(argv, capsys):
    assert worldpkg(argv) == 3
    capsys.readouterr()


def test_zones_example_is_valid_draft(capsys):
    assert worldpkg(["zones", "--example", "shenzhen"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [f["id"] for f in doc["features"]] == ["nofly-sz-t2", "restricted-sz-t3"]
    doc["features"].insert(0, {"type": "Feature", "id": "border", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
                               "properties": {"zone_id": "border", "kind": "border", "min_z_m": None, "max_z_m": 1.0,
                                              "label": "b", "origin": "derived"}})
    assert schema_errors(doc, "zones.schema.json") == []
    ring = doc["features"][1]["geometry"]["coordinates"][0]
    assert len(ring) == 33 and ring[0] == ring[-1]


def test_zones_example_refuses_scenarios_dir(capsys):
    from awr.world.ingest.manifest import repo_root

    assert worldpkg(["zones", "--example", "shenzhen", "--out", str(repo_root() / "scenarios" / "zones" / "x.geojson")]) == 3
    capsys.readouterr()


def test_qa_command(tiny_built, capsys):
    assert worldpkg(["qa", str(tiny_built["dir"])]) == 0
    out = capsys.readouterr().out
    assert "G-01" in out and "通过" in out
