"""coverage stage 只处理覆盖类与编队任务（ADR-074 第 5 条）：orbit、follow_path 等任务不逐轨道筛选，覆盖类任务照常戳记。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from awr.sim.mission.coverage import _STAMP_GENS, CoverageTracker


class _NoIter(dict):
    """被逐轨道筛选即失败的轨道表。"""

    def values(self):
        raise AssertionError("tracks of a non-coverage mission must not be scanned")


class _Seen(dict):
    def __init__(self, *a) -> None:
        super().__init__(*a)
        self.scanned = 0

    def values(self):
        self.scanned += 1
        return super().values()


def _rt(missions: dict) -> SimpleNamespace:
    return SimpleNamespace(missions=SimpleNamespace(order=list(missions), missions=missions), world=None,
                           camera={}, emit=lambda *a, **k: None)


def test_orbit_and_follow_path_missions_are_not_scanned() -> None:
    ms = {f"m{k}": SimpleNamespace(mid=f"m{k}", state="RUNNING", spec={"generator": g}, tracks=_NoIter(), extra={})
          for k, g in enumerate(("orbit", "follow_path", "goto_route"))}
    cov = CoverageTracker(_rt(ms))
    S = SimpleNamespace(enu=SimpleNamespace(pos=np.zeros((4, 3))), t_ns=0)
    cov.stage(S, None)  # 不抛出：没有逐轨道筛选


def test_coverage_generators_are_still_scanned() -> None:
    assert {"helix_scan", "lawnmower", "corridor", "terrain_follow", "formation"} == set(_STAMP_GENS)
    tracks = _Seen({0: SimpleNamespace(state="DONE", slot=0)})
    ms = {"m0": SimpleNamespace(mid="m0", state="RUNNING", spec={"generator": "lawnmower"}, tracks=tracks, extra={})}
    cov = CoverageTracker(_rt(ms))
    S = SimpleNamespace(enu=SimpleNamespace(pos=np.zeros((4, 3))), t_ns=0)
    cov.stage(S, None)
    assert tracks.scanned == 1
