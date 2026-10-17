"""sim-core perf 的机巢数与活动任务数（issue #7；M08-FR-224 追加字段）：装配 M20 时给出 `n_nest`、`n_task_active`，未装配时省略。"""

from __future__ import annotations

from types import SimpleNamespace

from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R


def test_perf_reports_nest_and_active_task_counts() -> None:
    from awr.sim.tasking import plugin

    with R.isolated_registry() as reg:
        h = CoreHarness(n=0, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, ready=False)
        try:
            assert "n_nest" not in h.core._tasking_counts()
            rt = plugin.install()
            try:
                rt.nests["nest-a"] = SimpleNamespace()
                rt.tasks["t-1"] = SimpleNamespace(active=True)
                rt.tasks["t-2"] = SimpleNamespace(active=False)
                assert h.core._tasking_counts() == {"n_nest": 1, "n_task_active": 1}
            finally:
                rt.nests.pop("nest-a", None)
                rt.tasks.pop("t-1", None)
                rt.tasks.pop("t-2", None)
                plugin.uninstall()
        finally:
            h.close()
