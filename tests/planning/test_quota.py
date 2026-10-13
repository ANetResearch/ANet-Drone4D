"""规划输入规模配额（M10-FR-212；M10-AC-209；AWR-04 §11.5）：§15.3.3 各上限的边界值受理、边界值 + 1 在 sim-core 提交前
（PlanPoolClient）与服务端入队前（PlanServer）都回复 585，且回复列出超限项。"""

from __future__ import annotations

import pytest

from awr.sim.planning.jobs import PLAN_QUOTA, PlanRequest, quota_check, validate_request
from awr.sim.planning.pool import PlanPoolClient

pytestmark = pytest.mark.usefixtures("plan_plugins")


def _rq(jid: str, kind: str, payload: dict) -> PlanRequest:
    return PlanRequest(jid, kind, None, (), payload, {}, 1, 0, 200, jid)  # type: ignore[arg-type]


def _over(v: float) -> float:
    return v + 1


@pytest.mark.parametrize("item", sorted(PLAN_QUOTA))
def test_each_quota_boundary(item: str) -> None:
    lim = PLAN_QUOTA[item]
    assert quota_check({item: lim}) is None
    code, detail, info = quota_check({item: _over(lim)})
    assert code == 585 and detail.startswith(f"{item}=") and info == {item: {"value": _over(lim), "limit": lim}}


def test_quota_lists_all_items_sorted_and_ignores_unknown() -> None:
    r = quota_check({"vertices": 65, "area_m2": 1_000_001, "stripes": 200, "foo": 1e9})
    assert r is not None and r[1] == "area_m2=1000001>1000000; vertices=65>64" and set(r[2]) == {"area_m2", "vertices"}
    assert quota_check({"x": 5}, {"x": 4})[1] == "x=5>4"
    assert quota_check({"x": 4.5}, {"x": 4})[1] == "x=4.5>4"


def test_validate_request_for_registered_kind() -> None:
    assert validate_request(_rq("a", "test.quota", {"measured": dict(PLAN_QUOTA)})) is None
    r = validate_request(_rq("b", "test.quota", {"measured": {"work_items": 51, "vehicles": 13}}))
    assert r is not None and r.code == 585 and r.status == "error" and r.detail == "vehicles=13>12; work_items=51>50"
    assert r.stats["quota"]["work_items"] == {"value": 51, "limit": 50} and r.remedy
    assert validate_request(_rq("c", "no.such", {})) is None                     # 未登记类别由 worker 判 300


def test_builtin_kinds_check_keepouts() -> None:
    ko = [{"id": f"k{i}"} for i in range(64)]
    assert validate_request(_rq("t", "safe_transit", {"keepouts": ko})) is None
    r = validate_request(_rq("t", "safe_transit", {"keepouts": [*ko, {"id": "k64"}]}))
    assert r.code == 585 and r.detail == "keepouts=65>64"


def test_validator_exception_is_bad_request() -> None:
    from awr.sim.planning.jobs import register_plan_kind, unregister_plan_kind

    def bad(payload):
        raise KeyError("polygon")

    try:
        register_plan_kind("t.badval", lambda r, w: None, budget_ms=100, validate_input=bad)
        r = validate_request(_rq("x", "t.badval", {}))
        assert r.code == 300 and r.detail.startswith("BAD_INPUT: KeyError")
    finally:
        unregister_plan_kind("t.badval")


@pytest.mark.parametrize("mode", ["inline", "thread"])
def test_client_rejects_before_execution(mode: str, monkeypatch) -> None:
    from awr.sim.planning import worker as WK

    ran: list = []
    real = WK.worker_main
    monkeypatch.setattr(WK, "worker_main", lambda req: (ran.append(req.job_id), real(req))[1])
    pool = PlanPoolClient(None, mode=mode)
    got: list = []
    try:
        pool.submit(_rq("over", "test.quota", {"measured": {"coverage_cells": 250_001}}), lambda r, t: got.append((r, t)))
        assert got == []                                                         # 拒绝也在下一次 drain 生效
        pool.drain(3)
        r, t = got[0]
        assert t == 3 and r.code == 585 and r.detail == "coverage_cells=250001>250000" and ran == []
        pool.submit(_rq("edge", "test.quota", {"measured": {"coverage_cells": 250_000}}), lambda r, t: got.append((r, t)))
        import time

        t_end = time.monotonic() + 10
        while len(got) < 2 and time.monotonic() < t_end:
            pool.drain(4)
            time.sleep(0.01)
        assert got[1][0].ok and ran == ["edge"]
        assert pool.snapshot()["rejected"] == 1
    finally:
        pool.close()
