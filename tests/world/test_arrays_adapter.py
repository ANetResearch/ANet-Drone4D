"""`IngestFromArrays` / `ArraysAdapter` 的输入校验与适配器字段（M03-FR-021、M03-NFR-017；FX-SIM2）。端到端（Mock 会话 →
`PhasedBuild` → `--deep` → 发布）由 `tests/reconstruction/test_mock_chain.py` 覆盖（M03-AC-026）。"""

from __future__ import annotations

import numpy as np
import pytest

from awr.world.ingest import ArraysAdapter, IngestFromArrays
from awr.world.ingest.types import ConfigError


def _spec(tmp_path, **kw) -> IngestFromArrays:
    base = dict(world_id="shenzhen-recon-09", name="x", name_zh=None, xyz_world=np.zeros((10, 3)), normals=None,
                class_index=None, anchor=None, true_north={"yawOffsetDeg": 0.0, "confidence": "assumed", "evidence": []},
                scale_status="gnss", source_kind="reconstruction",
                registration={"method": "sim3", "T_world_map": {"s": 1.0, "q": [0, 0, 0, 1], "t": [0, 0, 0]}},
                source_origin_engine=None, dataset={"name": "d"}, generator_params={"job_id": "j-1", "session_id": "s1"},
                session_id="s1")
    base.update(kw)
    return IngestFromArrays(**base)


def test_adapter_fields(tmp_path) -> None:
    a = ArraysAdapter(_spec(tmp_path, camera_home={"position": [1, 2, 3], "target": [0, 0, 0], "fovDeg": 50}))
    cfg = a.config()
    assert cfg.world_id == "shenzhen-recon-09" and cfg.source_kind == "reconstruction" and cfg.units_to_m == 1.0
    assert a.staging_nonce == "j-1"
    ex = a.manifest_extras()
    assert ex["tags"] == ["recon", "synthetic"] and ex["camera_home"]["fovDeg"] == 50
    assert a.load().xyz.shape == (10, 3)
    from awr.world.package.arrays_build import ArraysPipeline, ReconParams
    from awr.world.package.params import BuildParams

    assert a.pipeline_cls is ArraysPipeline
    p = a.build_params(BuildParams.from_config())
    assert isinstance(p, ReconParams) and p.generator_params()["recon"]["job_id"] == "j-1"


@pytest.mark.parametrize("kw", [{"world_id": "Bad_Id"}, {"xyz_world": np.zeros((0, 3))}, {"xyz_world": np.zeros((5, 2))},
                                {"normals": np.zeros((3, 3), np.float32)}, {"class_index": np.zeros(3, np.uint8)},
                                {"source_kind": "dataset"}, {"registration": {"method": "none"}}])
def test_spec_rejected(tmp_path, kw) -> None:
    with pytest.raises(ConfigError):
        ArraysAdapter(_spec(tmp_path, **kw))


def test_session_dir_symlink_rejected(tmp_path) -> None:
    real = tmp_path / "sess"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    ArraysAdapter(_spec(tmp_path, recon_session_dir=real))
    with pytest.raises(ConfigError):
        ArraysAdapter(_spec(tmp_path, recon_session_dir=link))
