"""pytest fixtures for tests/reconstruction (M01). Helper modules in this directory are importable by name
(`recon_common`, `recon_synth`, `recon_stages`, `georef_fixture`).

D1-ext fixtures run the Mock chain against the built Shenzhen world through a temporary worlds directory (the source
world is symlinked read-only, products are written only there); they skip when `worlds/shenzhen` is not built.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from recon_common import WORLD_BUILT, job_params, make_worlds  # noqa: E402


@pytest.fixture(scope="session")
def golden_session(tmp_path_factory):
    from awr.reconstruction.ir.golden import write_golden_session

    return write_golden_session(tmp_path_factory.mktemp("golden"))


@pytest.fixture(scope="session")
def recon_base(tmp_path_factory):
    if not WORLD_BUILT:
        pytest.skip("worlds/shenzhen not built (make worlds)")
    base = tmp_path_factory.mktemp("recon")
    make_worlds(base)
    return base


@pytest.fixture(scope="session")
def chain_run(recon_base):
    """One complete small Mock job shared by the chain, manifest, event and storage tests."""
    from awr.reconstruction.jobs.recon_job import run_local

    res = run_local(job_params("shenzhen-recon-01"), worlds_dir=recon_base / "worlds", runs_dir=recon_base / "runs")
    res["worlds"] = recon_base / "worlds"
    res["world_dir"] = recon_base / "worlds" / "shenzhen-recon-01"
    return res
