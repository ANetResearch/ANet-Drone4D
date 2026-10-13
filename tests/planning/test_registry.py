"""规划作业注册表（M10-FR-201、NFR-202；M10-AC-201；ADR-113 第 ② 条）。

- 内置 11 种作业经 `kinds_builtin` 登记；同名重复登记在导入时失败；未登记类别 300（UNKNOWN_KIND）；
- 插件按 `AWR_PLAN_PLUGINS` 导入（缺失模块记 missing，导入出错记 error，strict 时抛出），进程模式 worker 导入同一清单；
- 等价：合成小世界上覆盖 11 种作业、六城 240 组转场与 S1、S4 生成作业在注册表分派下的 `(status, code, result_sha256)`
  与 if/elif 分派（D1）记录的黄金数据逐字节相同（`golden/registry_d1.json`，`gen_registry_golden.py` 在改造前生成）。
"""

from __future__ import annotations

import json
import os
import sys
import time
import types
import warnings

import pytest
from geo import tiny
from registry_cases import CITIES, GOLDEN, ROOT, city_cases, scenario_cases, summary, tiny_cases

from awr.sim.planning import jobs as J
from awr.sim.planning.jobs import (
    BUDGET_MS,
    BUILTIN_KINDS,
    PRIO_BACKGROUND,
    PRIO_INTERACTIVE,
    PlanKindError,
    PlanRequest,
    PlanResult,
    load_plan_plugins,
    plan_kinds,
    register_plan_kind,
    unregister_plan_kind,
)
from awr.sim.planning.worker import set_world, worker_main

UNKNOWN_CASE = "tiny:unknown"        # D1 detail 为 "UNKNOWN_KIND:<kind>"，D2 改为 "UNKNOWN_KIND"（FR-201），单独断言


def _ok(req: PlanRequest, world) -> PlanResult:
    return PlanResult(req.job_id, "ok", (), None, {}, None, None, J.result_digest((), None, {"w": world is not None}))


def test_builtin_kinds_registered_with_d1_budgets() -> None:
    J.ensure_plan_kinds()
    ks = plan_kinds()
    assert set(BUILTIN_KINDS) <= set(ks) and len(BUILTIN_KINDS) == 11
    for k in BUILTIN_KINDS:
        assert ks[k].owner == "M10" and ks[k].budget_ms == BUDGET_MS[k] and ks[k].module == "awr.sim.planning.kinds_builtin"
    assert ks["warm"].priority_default == PRIO_BACKGROUND and ks["path_valid"].priority_default == PRIO_INTERACTIVE
    assert J.budget_for("generator") == 2000 and J.budget_for("no.such", 77) == 77


def test_duplicate_registration_fails_and_identical_is_idempotent() -> None:
    try:
        s1 = register_plan_kind("t.dup", _ok, budget_ms=50, owner="M20")
        assert register_plan_kind("t.dup", _ok, budget_ms=50, owner="M20") is s1        # 同一函数、同一参数：幂等
        with pytest.raises(PlanKindError):
            register_plan_kind("t.dup", lambda r, w: _ok(r, w), budget_ms=50, owner="M20")
        with pytest.raises(PlanKindError):
            register_plan_kind("t.dup", _ok, budget_ms=60, owner="M20")
        with pytest.raises(PlanKindError):
            register_plan_kind("safe_transit", _ok, budget_ms=50)                     # 内置类别不可被覆盖
    finally:
        unregister_plan_kind("t.dup")


@pytest.mark.parametrize("bad", [dict(kind="Bad"), dict(kind="a..b"), dict(kind="x" * 65), dict(budget_ms=0),
                                 dict(budget_ms=60_001), dict(budget_ms=1.5), dict(priority_default=4),
                                 dict(validate_input=3), dict(fn=None)])
def test_registration_argument_checks(bad: dict) -> None:
    kw = {"kind": "t.args", "fn": _ok, "budget_ms": 100} | bad
    try:
        with pytest.raises(PlanKindError):
            register_plan_kind(kw.pop("kind"), kw.pop("fn"), **kw)
    finally:
        unregister_plan_kind("t.args")


def test_priority_alias_and_owner_inference() -> None:
    mod = types.ModuleType("awr.tasking._probe")
    exec("def run(req, world):\n    return None\n", mod.__dict__)
    try:
        s = register_plan_kind("t.prio", mod.run, budget_ms=200, priority=PRIO_INTERACTIVE)   # M20 PRD §7.4 的写法
        assert s.priority_default == PRIO_INTERACTIVE and s.owner == "M20"
    finally:
        unregister_plan_kind("t.prio")


def test_unknown_kind_is_300_and_registered_plugin_dispatches(tmp_path) -> None:
    w = tiny(str(tmp_path))
    key = (w.world_id, w.content_version, w.coordinate_sha256)
    set_world(key, w)
    r = worker_main(PlanRequest("u", "no.such", key, (), {}, {}, 1, 0, 100, "u"))
    assert r.status == "error" and r.code == 300 and r.detail == "UNKNOWN_KIND" and r.stats["kind"] == "no.such"
    try:
        register_plan_kind("t.ok", _ok, budget_ms=100)
        r = worker_main(PlanRequest("k", "t.ok", key, (), {}, {}, 1, 0, 100, "k"))
        assert r.ok and r.result_sha256 == J.result_digest((), None, {"w": True}) and "t_ms" in r.stats
    finally:
        unregister_plan_kind("t.ok")


def test_handler_errors_map_to_320_and_bad_result(tmp_path) -> None:
    w = tiny(str(tmp_path))
    key = (w.world_id, w.content_version, w.coordinate_sha256)
    set_world(key, w)

    def boom(req, world):
        raise RuntimeError("x" * 500)

    try:
        register_plan_kind("t.boom", boom, budget_ms=100)
        register_plan_kind("t.none", lambda r, w: None, budget_ms=100)
        r = worker_main(PlanRequest("b", "t.boom", key, (), {}, {}, 1, 0, 100, "b"))
        assert r.code == 320 and r.detail.startswith("RuntimeError") and len(r.detail) < 240
        r = worker_main(PlanRequest("n", "t.none", key, (), {}, {}, 1, 0, 100, "n"))
        assert r.code == 320 and r.detail == "BAD_RESULT: NoneType"
    finally:
        unregister_plan_kind("t.boom")
        unregister_plan_kind("t.none")


def test_plugin_loading_missing_error_and_strict(tmp_path, monkeypatch) -> None:
    (tmp_path / "plug_dup_m10.py").write_text(
        "from awr.sim.planning.jobs import register_plan_kind\n"
        "register_plan_kind('safe_transit', lambda r, w: None, budget_ms=10)\n")
    (tmp_path / "plug_good_m10.py").write_text(
        "from awr.sim.planning.jobs import register_plan_kind\n"
        "register_plan_kind('t.plug', lambda r, w: None, budget_ms=10)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        st = load_plan_plugins("plug_missing_m10,plug_good_m10,plug_dup_m10")
        assert st["plug_missing_m10"] == "missing" and st["plug_good_m10"] == "ok" and st["plug_dup_m10"] == "error"
        assert st["awr.sim.planning.kinds_builtin"] == "ok"                       # 内置类别恒登记
        assert "t.plug" in plan_kinds()
        sys.modules.pop("plug_dup_m10", None)
        with pytest.raises(PlanKindError):                                       # 重复登记在导入时失败
            load_plan_plugins("plug_dup_m10", strict=True)
    finally:
        unregister_plan_kind("t.plug")
        for m in ("plug_good_m10", "plug_dup_m10"):
            sys.modules.pop(m, None)
            J._LOADED.pop(m, None)
        J._LOADED.pop("plug_missing_m10", None)


@pytest.mark.slow
def test_process_mode_worker_imports_plugin_list(plan_plugins) -> None:
    """D1 进程模式（共享展示 S7 的 plan-pool）：spawn 出的 worker 按同一插件清单登记，插件类别经注册表分派（FR-213）。"""
    from awr.sim.planning.pool import PlanPoolClient

    pool = PlanPoolClient(None, mode="process")
    got: list = []
    try:
        pool.submit(PlanRequest("e", "test.echo", None, (), {"x": 41}, {}, 0, 0, 200, "e"),  # type: ignore[arg-type]
                    lambda r, t: got.append(r))
        t_end = time.monotonic() + 60
        tick = 0
        while not got and time.monotonic() < t_end:
            tick += 1
            pool.drain(tick)
            time.sleep(0.01)
        assert got and got[0].ok and got[0].extra["echo"] == 41
    finally:
        pool.close()


# ------------------------------------------------------------------------------------------------ 等价（AC-201）
def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


# 结果摘要是规划输出的逐字节哈希，依赖 CPU 的浮点细节（numba 向量化路径在不同指令集上末位可能不同），golden 在开发机录制。
# 托管 CI（GitHub Actions 设置环境变量 CI）的 runner CPU 不固定：状态与原因码逐项严格比较，摘要不一致只告警不判失败；
# 逐字节等价由本地 `make ci`（G1）把关。
STRICT_DIGEST = not os.environ.get("CI")


def test_registry_matches_d1_golden_on_tiny_world(tmp_path) -> None:
    ref = _golden()
    w = tiny(str(tmp_path))
    set_world((w.world_id, w.content_version, w.coordinate_sha256), w)
    n = 0
    drift: list[str] = []
    for req in tiny_cases(w):
        got = summary(worker_main(req))
        if req.job_id == UNKNOWN_CASE:
            assert got[:2] == ref[UNKNOWN_CASE][:2] == ["error", 300]
            continue
        if STRICT_DIGEST:
            assert got == ref[req.job_id], req.job_id
        else:
            assert got[:2] == ref[req.job_id][:2], req.job_id
            if got[2] != ref[req.job_id][2]:
                drift.append(req.job_id)
        n += 1
    assert n >= 30
    if drift:
        warnings.warn(f"registry golden: result digest differs on this CPU for {drift} (status and code match)", stacklevel=1)


def _open_city_or_skip(name: str):
    from awr.world.geometry.query import open_world_query
    from awr.world.geometry.types import GeoLoadError

    d = ROOT / "worlds" / name
    if not (d / "world.json").exists():
        pytest.skip(f"world {name} not built")
    try:
        return open_world_query(d, allow_derive=False)
    except GeoLoadError as e:
        pytest.skip(f"world {name} derived cache unavailable: {e.code}")


@pytest.mark.needs_data
@pytest.mark.parametrize("city", CITIES)
def test_registry_matches_d1_golden_on_cities(city: str) -> None:
    """六城各 40 组转场（合计 240 组），深圳另含 S1、芝加哥另含 S4 的生成作业。"""
    ref = _golden()
    w = _open_city_or_skip(city)
    set_world((w.world_id, w.content_version, w.coordinate_sha256), w)
    reqs = city_cases(city, w)
    sc = {"shenzhen": "s1-shenzhen-facade", "chicago": "s4-chicago-lakeshore"}.get(city)
    if sc:
        reqs += scenario_cases(sc, w)
    bad = {}
    for req in reqs:
        got = summary(worker_main(req))
        if got != ref[req.job_id]:
            bad[req.job_id] = (ref[req.job_id], got)
    assert not bad, bad
