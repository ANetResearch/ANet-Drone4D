"""Pipeline 与 stage 注册表、状态块、numba 退回（M08-AC-003、AC-008；M08-FR-010 至 FR-012、FR-040）。

AC-003（与 test_skeleton 的登记校验互补）：every = 3、phase ≥ every、重名、重复 order、越区段、同一字段两个写者、
Σbudget > 0.40 均构建失败；状态块按容量分配、重复登记失败、进入 checkpoint 数组；默认 pipeline 顺序与内置 stage 集合。
AC-008：`AWR_KERNEL=numpy` 与模拟 numba import 失败两种方式：内核为 numpy、第 301 架添加返回 110 KERNEL_LIMIT、
`meta.json` 的 `sim.kernel = numpy`、发 `sim.kernel.fallback`。
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.reasons import Reason
from awr.sim.fleet import kernels_l1 as KL
from awr.sim.fleet.fleet import FleetConfig, FleetSim, resolve_kernel
from awr.sim.fleet.pipeline import Pipeline, PipelineError
from awr.sim.fleet.stages import registry as R
from awr.sim.fleet.state import FleetState


def _noop(S, ctx) -> None:
    return None


def test_registry_rejections() -> None:
    with R.isolated_registry():
        R.register_stage("guard", 5, 1, 110, owner="M09")(_noop)
        for kw in ({"name": "a", "every": 3, "phase": 0, "order": 111},  # 250 % 3 ≠ 0
                   {"name": "b", "every": 5, "phase": 5, "order": 111},  # phase ≥ every
                   {"name": "guard", "every": 5, "phase": 0, "order": 112},  # 重名
                   {"name": "c", "every": 5, "phase": 0, "order": 40}):  # 越区段（M08 区段）
            with pytest.raises(ValueError):
                R.register_stage(kw["name"], kw["every"], kw["phase"], kw["order"], owner="M09", budget_core=0.001)(_noop)
    a = R.make_stage("x.a", 1, 0, 120, _noop, owner="M09", budget_core=0.001)
    b = R.make_stage("x.b", 1, 0, 120, _noop, owner="M09", budget_core=0.001)
    with pytest.raises(PipelineError, match="order"):
        Pipeline.build([*a, *b], None)  # 重复 order
    with pytest.raises(ValueError):
        R.make_stage("x.c", 1, 0, 60, _noop, owner="M09", budget_core=0.001)  # 越区段（make_stage 同样校验）
    (c,) = R.make_stage("x.c", 1, 0, 124, _noop, owner="M09", budget_core=0.001)
    c = dataclasses.replace(c, order=60)
    with pytest.raises(PipelineError, match="区段"):
        Pipeline.build([c], None)  # 构建时再次校验
    w1 = R.make_stage("x.w1", 1, 0, 121, _noop, owner="M09", budget_core=0.001, writes=("soc",))
    w2 = R.make_stage("x.w2", 1, 0, 122, _noop, owner="M09", budget_core=0.001, writes=("soc",))
    with pytest.raises(PipelineError, match="写者"):
        Pipeline.build([*w1, *w2], None)
    heavy = R.make_stage("x.h", 1, 0, 123, _noop, owner="M09", budget_core=0.41)
    with pytest.raises(PipelineError, match="budget"):
        Pipeline.build(heavy, None)


def test_state_block_allocation_and_checkpoint() -> None:
    with R.isolated_registry() as reg:
        R.register_state_block("m09x", "M09", {"soc": (np.float32, ()), "cells": (np.float64, (3,))})
        with pytest.raises(ValueError):
            R.register_state_block("m09x", "M09", {"soc": (np.float32, ())})
        S = FleetState(16, blocks=reg.blocks)
        assert S.blocks["m09x"]["soc"].shape == (16,) and S.blocks["m09x"]["cells"].shape == (16, 3)
        S.blocks["m09x"]["soc"][3] = 0.5
        arr = S.checkpoint_arrays()
        assert any("m09x" in k and "soc" in k for k in arr)


def test_default_pipeline_order() -> None:
    with R.isolated_registry() as reg:
        # collide：机间碰撞检查自 contact 拆出（25 Hz、奇数 tick，ADR-070）
        for kernel, names in (("numba", ["clock", "ingest", "l1", "kinematic", "contact", "collide", "fsm_min", "cmd_watch", "tap"]),
                              ("numpy", ["clock", "ingest", "refgen", "pos_ctrl", "att_ctrl", "motor", "aero", "integrate",
                                         "kinematic", "contact", "collide", "fsm_min", "cmd_watch", "tap"])):
            if kernel == "numba" and not KL.HAVE_NUMBA:
                continue
            f = FleetSim(FleetConfig(kernel=kernel, path_capacity=1024), reg=reg)
            pl = f.build_pipeline()
            got = [s.name for s in pl.stages]
            assert got == names, got
            assert [s.order for s in pl.stages] == sorted(s.order for s in pl.stages)
            assert pl.budget_total() <= 0.40


@pytest.mark.parametrize("how", ["env", "import"])
def test_numpy_fallback(how: str, monkeypatch, tmp_path) -> None:
    if how == "env":
        cfg = FleetConfig.from_env({"AWR_KERNEL": "numpy"})
        assert cfg.kernel == "numpy"
        kernel = "numpy"
    else:
        monkeypatch.setattr(KL, "HAVE_NUMBA", False)
        monkeypatch.setattr(KL, "NUMBA_ERROR", "ImportError: simulated")
        assert resolve_kernel("numba") == ("numpy", "numba import failed: ImportError: simulated")
        kernel = "numba"
    (tmp_path / "meta.json").write_text(json.dumps({"schema": "awr.meta.v1", "sim": {}}), encoding="utf-8")
    with R.isolated_registry() as reg:
        ev: list[tuple[str, dict]] = []
        h = CoreHarness(n=300, reg=reg, kernel=kernel, ready=False, spacing=12.0, persist_dir=tmp_path)
        try:
            assert h.core.fleet.kernel == "numpy" and h.core.fleet.max_vehicles == 300
            meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
            assert meta["sim"]["kernel"] == "numpy" and meta["sim"]["fastmath"] is False
            assert {"numpy_version", "python_version", "world_seed"} <= set(meta["sim"])
            side = json.loads((tmp_path / "sim-core.json").read_text(encoding="utf-8"))
            assert side["sim_kernel"] == "numpy" and side["kernel_fallback"]
            h.core.events.emit = lambda kind, **kw: ev.append((kind, kw))
            h.n += 1
            adm = h.cmd("fleet/add", {"profile_id": "x500", "home_enu_m": [5000.0, 0.0, 0.0]}, uav=None)
            assert adm["code"] == int(Reason.PARAM_OUT_OF_RANGE) and adm["detail"]["why"] == "KERNEL_LIMIT"
        finally:
            h.close()


def test_fallback_event_emitted(monkeypatch) -> None:
    with R.isolated_registry() as reg:
        seen: list[tuple[str, dict]] = []
        import awr.runtime.events as E

        orig = E.EventPublisher.emit

        def rec(self, kind, **kw):
            seen.append((kind, kw))
            return orig(self, kind, **kw)

        monkeypatch.setattr(E.EventPublisher, "emit", rec)
        h = CoreHarness(n=1, reg=reg, kernel="numpy", ready=False)
        try:
            fb = [kw for k, kw in seen if k == "sim.kernel.fallback"]
            assert fb and fb[0]["max_vehicles"] == 300 and fb[0]["reason"] == "AWR_KERNEL=numpy"
        finally:
            h.close()
