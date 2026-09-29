"""Product world manifest (M01-AC-019; M01-FR-031, §6.8; AWR-16 §14.1 derived worlds)."""

from __future__ import annotations

import numpy as np
from recon_common import WORLDS, needs_world, read_json

from awr.world.georef.frames import Sim3


@needs_world
def test_world_manifest(chain_run):
    w = chain_run["world_dir"]
    sid = chain_run["extra"]["recon_session"]
    wj, c = read_json(w / "world.json"), read_json(w / "coordinate.json")
    src_w, src_c = read_json(WORLDS / "shenzhen" / "world.json"), read_json(WORLDS / "shenzhen" / "coordinate.json")
    al = read_json(w / "reconstruction" / sid / "alignment.json")
    assert c["source"]["kind"] == "reconstruction"
    assert c["registration"]["method"] == "gnss-sim3" == al["method"]
    assert (w / c["registration"]["alignmentRef"]).is_file()
    assert c["anchor"] == src_c["anchor"] and c["T_ecef_world"] == src_c["T_ecef_world"] and c["trueNorth"] == src_c["trueNorth"]
    assert wj["scaleStatus"] == c["scaleStatus"] == al["scale_status"] == "gnss"
    T = np.asarray(c["source"]["T_world_source"])
    assert abs(abs(np.linalg.det(T[:3, :3])) ** (1 / 3) / c["source"]["unitsToMeters"] - 1.0) <= 1e-9
    assert Sim3.from_json(c["registration"]["T_world_map"]).s == al["T_world_engine"]["s"]
    layer = next(L for L in wj["layers"] if L["id"] == f"reconstruction.{sid}")
    assert layer["format"] == "recon-ir@1" and layer["type"] == "reconstruction" and layer["role"] == "reconstruction"
    assert wj["tags"] == ["recon", "synthetic"]
    assert wj["generator"]["params"]["recon"]["engine"] == "mock" and wj["generator"]["params"]["recon"]["session_id"] == sid
    assert wj["dataset"]["sourceFiles"] == src_w["dataset"]["sourceFiles"] and "MockEngine" in wj["dataset"]["notice"]
    assert not any(p.name == "mock_truth.json" for p in w.rglob("*"))
    assert all(f["path"] != "reconstruction/" for f in wj["files"])
    assert any(f["path"] == f"reconstruction/{sid}/session.json" for f in wj["files"])
    home = wj["camera"]["home"]
    assert home["fovDeg"] == 60.0 and len(home["position"]) == 3


@needs_world
def test_session_files_have_no_context_fields(chain_run):
    sd = chain_run["world_dir"] / "reconstruction" / chain_run["extra"]["recon_session"]
    for p in sd.glob("*.json"):
        doc = read_json(p)
        assert not {"created_at", "created_wall_ns", "host", "duration_s", "job_id", "world_id"} & set(doc), p.name
    ses = read_json(sd / "session.json")
    assert ses["session_id"] == sd.name and ses["source_world"]["id"] == "shenzhen"
