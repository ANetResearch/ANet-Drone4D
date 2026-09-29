"""Pose-direction self-check (M01-AC-002; M01-FR-004): 6 conventions x 20 seeds, declared correctly and flipped.

Correct declarations pass every time; flipped declarations always stop the session with 336 RECON_POSE_CONVENTION;
pairs without overlap abstain instead of voting wrong.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from recon_synth import orbit, raw_frames, scene

from awr.reconstruction.conventions import CONVENTIONS
from awr.reconstruction.engines.base import pose_direction_selfcheck
from awr.reconstruction.types import RECON_ENGINES, PoseConventionError

SEEDS = range(20)
_FLIP = {"c2w": "w2c", "w2c": "c2w"}


@pytest.fixture(scope="module")
def world():
    return scene(np.random.default_rng(5)), orbit(32)


@pytest.mark.parametrize("engine", RECON_ENGINES)
def test_declared_correctly_always_passes(engine, world):
    P, poses = world
    for seed in SEEDS:
        frames, _ = raw_frames(engine, n=32, seed=seed, points=P, poses=poses)
        sc = pose_direction_selfcheck(frames, CONVENTIONS[engine], np.random.default_rng(seed))
        assert sc.passed, (engine, seed, sc)
        assert len(sc.votes) >= 2 and all(v == CONVENTIONS[engine].pose_dir for v in sc.votes)
        assert min(sc.margins) >= 1.5


@pytest.mark.parametrize("engine", RECON_ENGINES)
def test_flipped_declaration_always_fails(engine, world):
    P, poses = world
    for seed in SEEDS:
        true_conv = CONVENTIONS[engine]
        frames, _ = raw_frames(engine, n=32, seed=seed, points=P, poses=poses)
        wrong = replace(true_conv, pose_dir=_FLIP[true_conv.pose_dir])
        sc = pose_direction_selfcheck(frames, wrong, np.random.default_rng(seed))
        assert not sc.passed
        assert all(v == true_conv.pose_dir for v in sc.votes)        # the vote itself is never wrong


def test_adapter_raises_336_on_wrong_declaration(world):
    """AdapterBase.infer runs the check on the first 32 frames and stops with PoseConventionError (336)."""
    from awr.reconstruction.engines.base import AdapterBase
    from awr.reconstruction.types import EngineCaps, EngineContext, SessionSpec

    P, poses = world
    frames, _ = raw_frames("vggt", n=32, seed=1, points=P, poses=poses)

    class Replay(AdapterBase):
        name = "vggt"

        def capabilities(self):
            return EngineCaps(True, None, frozenset(), "cpu", 100, None, 0.1)

        def _run_raw(self, _):
            yield from frames

    ok = Replay()
    ok.prepare(SessionSpec("rs-000000000000", 32, 10.0, 640, 360, 1), EngineContext(workdir=__import__("pathlib").Path(".")))
    out = list(ok.infer([]))
    assert len(out) == 32 and ok.self_check.passed and out[-1].self_check["pass"] is True
    bad = Replay(convention=replace(CONVENTIONS["vggt"], pose_dir="c2w"))
    bad.prepare(SessionSpec("rs-000000000000", 32, 10.0, 640, 360, 1), EngineContext(workdir=__import__("pathlib").Path(".")))
    with pytest.raises(PoseConventionError) as ei:
        list(bad.infer([]))
    assert ei.value.code == 336 and not ei.value.resumable and ei.value.detail["pass"] is False


def test_no_overlap_pairs_abstain():
    """Frames looking at disjoint patches: every pair abstains, nothing votes wrong, the check does not pass."""
    rng = np.random.default_rng(3)
    P = np.c_[rng.uniform(-2000, 2000, (60_000, 2)), np.zeros(60_000)]
    poses = []
    for k in range(32):                                  # jump 400 m between frames, looking straight down
        T = np.eye(4)
        T[:3, :3] = np.diag([1.0, -1.0, -1.0])
        T[:3, 3] = [-1800 + 400 * (k % 10), -1800 + 400 * (k // 10), 30.0]
        poses.append(T)
    frames, _ = raw_frames("mock", n=32, seed=2, points=P, poses=poses)
    sc = pose_direction_selfcheck(frames, CONVENTIONS["mock"], np.random.default_rng(1))
    assert not sc.passed and all(v == "c2w" for v in sc.votes)


def test_short_session_uses_quarter_gap(world):
    P, _ = world
    frames, _ = raw_frames("da3", n=8, seed=4, points=P, poses=orbit(8, turns=0.25))
    sc = pose_direction_selfcheck(frames, CONVENTIONS["da3"], np.random.default_rng(0))
    assert sc.passed and all(v == "w2c" for v in sc.votes)
