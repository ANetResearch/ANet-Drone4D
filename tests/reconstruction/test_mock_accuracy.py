"""Mock georeferencing accuracy (M01-AC-009; M01-NFR-013; M01-FR-019, FR-020, FR-022, FR-027, FR-028, FR-048).

helix and lawnmower x 3 seeds in GNSS mode (1 % outliers, 2 % dropouts, 0.5 % depth noise): scale error <= 0.2 %,
rotation error <= 0.5 deg, registration point error p95 <= 1.5 m, inlier ratio >= 0.80, status accepted, C2C to the full
source cloud p50 <= 1.5 m and p95 <= 4.0 m. The functional run uses 300 frames at source_keep 0.05 and seeds 1 and 3;
the acceptance size (600 frames, seeds 1-3; source_keep 0.10 to keep the slow run near 4 min) is the slow variant.
"""

from __future__ import annotations

import pytest
from recon_common import job_params, make_worlds, needs_world
from recon_stages import accuracy, run_until

CASES = [(path, seed) for path in ("helix", "lawnmower") for seed in (1, 2, 3)]
CASES_FUNCTIONAL = [(path, seed) for path in ("helix", "lawnmower") for seed in (1, 3)]


def _check(run):
    a = accuracy(run)
    assert a["status"] == "accepted", a
    assert a["scale_err"] <= 2e-3, a
    assert a["rot_err_deg"] <= 0.5, a
    assert a["reg_p95_m"] <= 1.5, a
    assert a["inlier_ratio"] >= 0.80, a
    c2c = run.info["c2c"]
    assert c2c["p50"] <= 1.5 and c2c["p95"] <= 4.0, c2c
    return a


@needs_world
@pytest.mark.parametrize(("path", "seed"), CASES_FUNCTIONAL)
def test_accuracy_functional(path, seed, tmp_path_factory):
    base = tmp_path_factory.mktemp(f"acc-{path}-{seed}")
    wd = make_worlds(base)
    run = run_until(job_params(f"acc-{path}-{seed}", frames=300, keep=0.05, seed=seed, path=path), wd, base / "job")
    _check(run)


@pytest.mark.slow
@needs_world
@pytest.mark.parametrize(("path", "seed"), CASES)
def test_accuracy_acceptance_size(path, seed, tmp_path_factory):
    base = tmp_path_factory.mktemp(f"accfull-{path}-{seed}")
    wd = make_worlds(base)
    run = run_until(job_params(f"acc-{path}-{seed}", frames=600, keep=0.10, seed=seed, path=path), wd, base / "job")
    _check(run)
