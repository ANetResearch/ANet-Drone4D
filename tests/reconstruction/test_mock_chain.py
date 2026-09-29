"""Mock reconstruction chain end to end (D1-AC-22; M01-AC-008; M01-FR-016 to FR-031, FR-038, FR-045).

Functional size: 60 frames, source_keep 0.05 (about 15 s). The acceptance size (helix, 600 frames, source_keep 0.25) is
the `slow` variant below. Checked: the job walks the seven working stages (SEGMENTING skipped) to SUCCEEDED, the Recon IR
validates with zero errors (deep), the published World Package passes `validate --deep` and Ajv strict, and the product
world carries `scale_status = gnss`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from recon_common import ROOT, job_params, needs_world, read_json

from awr.reconstruction.ir.validate import validate_session
from awr.reconstruction.jobs.markers import read_marker
from awr.reconstruction.types import RECON_STAGES
from awr.world.package.validate import validate_world

NODE = shutil.which("node") or str(Path.home() / ".local" / "node" / "bin" / "node")


def _states(events):
    return [d["state"] for _, k, d in events if k == "job.state"]


@needs_world
def test_chain_succeeds(chain_run):
    r = chain_run
    assert r["exit_code"] == 0, r.get("error")
    assert _states(r["events"]) == [*RECON_STAGES, "SUCCEEDED"]
    assert r["extra"]["scale_status"] == "gnss"
    wd = Path(r["workdir"])
    seg = read_marker(wd, "SEGMENTING")
    assert seg["skipped"] is True and seg["outputs"] == []
    assert all(read_marker(wd, s) is not None for s in RECON_STAGES)
    info = r["info"]
    assert info["georef"]["status"] == "accepted" and info["qa_status"] in ("pass", "warn")


@needs_world
def test_ir_and_world_validate_deep(chain_run):
    w = chain_run["world_dir"]
    sd = w / "reconstruction" / chain_run["extra"]["recon_session"]
    rep = validate_session(sd, deep=True)
    assert rep.ok, rep.errors
    wrep = validate_world(w, deep=True)
    assert wrep.ok, wrep.errors[:5]
    assert read_json(w / "world.json")["scaleStatus"] == "gnss"


@needs_world
@pytest.mark.skipif(not (Path(NODE).exists() and (ROOT / "node_modules" / "ajv").exists()), reason="node or ajv missing")
def test_world_ajv_strict(chain_run):
    r = subprocess.run([NODE, str(ROOT / "tools" / "contracts" / "check-world.mjs"), str(chain_run["world_dir"])],
                       capture_output=True, text=True, cwd=ROOT, env=dict(os.environ), timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    sd = chain_run["world_dir"] / "reconstruction" / chain_run["extra"]["recon_session"]
    r2 = subprocess.run([NODE, str(Path(__file__).with_name("ajv_check_recon.mjs")), str(sd)], capture_output=True, text=True,
                        cwd=ROOT, timeout=120)
    assert r2.returncode == 0 and '"session.json":true' in r2.stdout and "false" not in r2.stdout, r2.stdout + r2.stderr


@needs_world
def test_cli_validate_and_engines(chain_run):
    import sys

    sd = chain_run["world_dir"] / "reconstruction" / chain_run["extra"]["recon_session"]
    r = subprocess.run([sys.executable, "-m", "awr.reconstruction", "validate", str(sd), "--deep"], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0 and "OK 0 errors" in r.stdout, r.stdout + r.stderr
    r = subprocess.run([sys.executable, "-m", "awr.reconstruction", "engines", "--json"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and '"GPU_REQUIRED"' in r.stdout and '"mock"' in r.stdout


@pytest.mark.slow
@needs_world
def test_chain_acceptance_size(tmp_path):
    """D1-AC-22 at the specified size: Shenzhen, helix, 600 frames, 128x72, source_keep 0.25 (single-thread ~2-3 min)."""
    from recon_common import make_worlds

    from awr.reconstruction.jobs.recon_job import run_local

    wd = make_worlds(tmp_path)
    r = run_local(job_params("shenzhen-recon-01", frames=600, keep=0.25), worlds_dir=wd, runs_dir=tmp_path / "runs")
    assert r["exit_code"] == 0, r.get("error")
    assert _states(r["events"]) == [*RECON_STAGES, "SUCCEEDED"]
    assert validate_world(wd / "shenzhen-recon-01", deep=True).ok
    assert r["extra"]["scale_status"] == "gnss"
