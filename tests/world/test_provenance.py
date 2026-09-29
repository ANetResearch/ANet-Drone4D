"""M03-AC-018：六城 `dataset` 五个文本字段非空，`sourceFiles` 与 `configs/data.yaml`（真源）及本机 MANIFEST.json 一致。"""

from __future__ import annotations

import pytest
from m03_common import RAW, REPO, WORLDS, load, needs_worlds

from awr.world.ingest.manifest import load_data_config, read_manifest

pytestmark = [pytest.mark.needs_data, needs_worlds]


def test_dataset_fields_and_hashes():
    cfg = load_data_config(REPO / "configs" / "data.yaml")
    man = read_manifest(RAW)
    for spec in cfg.files:
        w = load(WORLDS / spec.world_id / "world.json")
        d = w["dataset"]
        for k in ("name", "version", "url", "citation", "license"):
            assert d[k].strip(), (spec.world_id, k)
        assert d["sourceFiles"] == [{"name": spec.name, "bytes": spec.bytes, "sha256": spec.sha256}]
        c = load(WORLDS / spec.world_id / "coordinate.json")
        assert c["source"]["files"][0]["sha256"] == spec.sha256 and c["source"]["files"][0]["points"] == spec.points
        if man is not None:
            m = next(f for f in man["files"] if f["world_id"] == spec.world_id)
            assert m["sha256"] == spec.sha256
