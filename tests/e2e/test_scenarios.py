"""剧本端到端（M16-FR-026；D1-AC-15 `test_s1`（P0）、D1-AC-16 `test_s3`（P1）、D1-AC-17；M16-AC-005、010、011、012、013、014）。

真实进程：ci profile 的 supervisor（sim-core + api），剧本经 `AWR_SCENARIO` / `AWR_SCENARIO_PROFILE` 加载，
轮询 `/api/events` 读 `scenario.loaded`、`scenario.result` 与途经事件（M10-FR-063）。

G1 中运行：S1 ×10（约 80 s 仿真墙钟加启动）、六城 free、ladder n10 冒烟（×10 的临时变体）。
`AWR_E2E_FULL=1` 时另跑：S1 ×1（与 ×10 的谓词一致性，约 13 min）、S2–S6、S3、恶劣天气 profile（harness `e2e.scenarios`）。
"""

from __future__ import annotations

import copy
import json

import pytest
from awrproc import Backend, write_variant
from e2ehelp import FULL, SCENARIOS

from awr.datasets.urbanscene3d.cities import CITY_IDS

pytestmark = [pytest.mark.needs_data, pytest.mark.slow]
full_only = pytest.mark.skipif(not FULL, reason="long scenario run: set AWR_E2E_FULL=1 (harness case e2e.scenarios)")

GUARD_PREFIXES = ("SAF.SEP.CONFLICT", "SAF.SEP.AVOIDING", "fleet_guard.conflict", "fleet_guard.avoiding")


def _pred(out, metric: str) -> list[dict]:
    return [p for p in out.predicates if str(p.get("expr", "")).startswith(metric)]


def _assert_success(out) -> None:
    bad = out.failed_predicates()
    assert out.status == "SUCCEEDED" and not bad, f"{out.scenario_id}/{out.profile}: {out.status} {bad}"


def _signature(out) -> list[tuple]:
    """同一种子下 ×1 与 ×10 应一致的事件序列摘要（类型、机体、剧本事件 id；不含时间戳）。"""
    keep = ("scenario.", "mission.", "cmd.succeeded", "cmd.failed", "SAF.")
    return [(e.get("type"), e.get("uav"), (e.get("data") or {}).get("event_id"))
            for e in out.events if str(e.get("type", "")).startswith(keep)]


# ---------------------------------------------------------------- S1（D1-AC-15，P0）
def test_s1(scenario_run) -> None:
    """S1 ×10（ci profile）：`scenario.result = SUCCEEDED`，§7.3.2 成功谓词全部为真；能量 RTL 0、间距 ≥ 10 m、
    guard 事件 0、阵风期间 pos_err < 3.0 m；×10 墙钟 ≤ 3 min（M16-NFR-008）。"""
    doc = json.loads((SCENARIOS / "s1-shenzhen-facade.json").read_text(encoding="utf-8"))
    assert doc["gcs_loss_policy"] == "ignore" and doc["profiles"]["ci"]["rate"] == 10
    out = scenario_run("s1-shenzhen-facade", "ci", timeout_s=420)
    _assert_success(out)
    for m in ("missions_done", "min_separation_m", "guard_events", "pos_err_max_m{window=gust}", "energy_rtl_count"):
        assert _pred(out, m) and all(p["ok"] for p in _pred(out, m)), m
    assert not [e for e in out.events if str(e.get("type")).startswith(GUARD_PREFIXES)]
    assert any((e.get("data") or {}).get("label") == "scan complete" for e in out.kinds("scenario."))
    assert out.wall_s <= 180.0 + 30.0, out.wall_s                                               # 3 min + 启动


@full_only
def test_s1_rate1_matches_rate10(scenario_run) -> None:
    """同一种子 ×1 与 ×10 的谓词结果与事件序列一致（M16-AC-010）。"""
    a = scenario_run("s1-shenzhen-facade", "ci", timeout_s=420)
    b = scenario_run("s1-shenzhen-facade", "perf", timeout_s=1800)
    assert [p["ok"] for p in a.predicates] == [p["ok"] for p in b.predicates]
    assert _signature(a) == _signature(b)


@full_only
@pytest.mark.ext
@pytest.mark.parametrize("profile", ["wx-fog", "wx-rain", "wx-storm"])
def test_s1_weather_profiles(scenario_run, tmp_path, profile: str) -> None:
    """M16-AC-011：恶劣天气 profile 以 ×10 运行（临时变体加 rate 10），wx-fog 满足基础谓词，rain、storm 满足安全终止谓词。"""
    doc = json.loads((SCENARIOS / "s1-shenzhen-facade.json").read_text(encoding="utf-8"))
    v = copy.deepcopy(doc)
    v["profiles"][profile] = {**v["profiles"][profile], "rate": 10, "record": False}
    d = write_variant(tmp_path, v)
    _assert_success(scenario_run("s1-shenzhen-facade", profile, timeout_s=420, scenarios_dir=d))


# ---------------------------------------------------------------- free（M16-AC-005）
@pytest.mark.parametrize("wid", CITY_IDS)
def test_free(wid: str) -> None:
    """六城各加载 free 剧本：2 架机出生（骨架机体被替换），加载 ≤ 2 s（墙钟，自 READY 起算；另留 8 s 进程与握手余量）。
    剧本加载事件早于 api 连接，因此以实时通道的机体频道（roster）判定，而不是 `scenario.loaded` 事件。"""
    import asyncio
    import time

    from e2ehelp import needs_world

    needs_world(wid)
    sys_path_chaos()
    from rtprobe import Probe

    b = Backend(world=wid, scenario=f"free-{wid}").start()
    try:
        t0 = time.monotonic()

        async def roster() -> list[str]:
            end = time.monotonic() + 20
            while time.monotonic() < end:
                p = await Probe.open(b.base, b.token("viewer", "m16free"))
                ids = p.roster_ids()
                await p.close()
                if sorted(ids) == ["p600-01", "p600-02"]:
                    return ids
                await asyncio.sleep(0.5)
            return ids

        ids = asyncio.run(roster())
        assert sorted(ids) == ["p600-01", "p600-02"], ids
        assert time.monotonic() - t0 <= 10.0
        assert b.scenario_invalid() is None
    finally:
        b.stop()


def sys_path_chaos() -> None:
    import sys
    from pathlib import Path

    p = str(Path(__file__).resolve().parents[1] / "chaos")
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------- ladder（M16-AC-012 冒烟）
def test_ladder_smoke(scenario_run, tmp_path) -> None:
    """ladder n10 以 ×5 运行（临时变体；×10 时 45 s 的稳态标记早于 api 连接，事件环读不到）：`ladder.steady` 标记出现、
    FleetGuard CONFLICT 与 AVOIDING 为 0、结果成功。"""
    doc = json.loads((SCENARIOS / "ladder-shenzhen.json").read_text(encoding="utf-8"))
    v = copy.deepcopy(doc)
    v["vehicle_sets"] = v["profiles"]["n10"]["vehicle_sets"]
    v["rate"] = 5
    d = write_variant(tmp_path, v)
    out = scenario_run("ladder-shenzhen", None, timeout_s=300, scenarios_dir=d)
    marks = [e for e in out.kinds("scenario.mark") if (e.get("data") or {}).get("label") == "ladder.steady"]
    assert marks, f"ladder.steady mark missing; result {out.status} {out.failed_predicates()}"
    assert abs(marks[0]["t_sim_ns"] * 1e-9 - 45.0) <= 0.5
    assert not [e for e in out.events if str(e.get("type")).startswith(GUARD_PREFIXES)]
    _assert_success(out)


# ---------------------------------------------------------------- S2–S6（D1-ext）
@full_only
@pytest.mark.ext
@pytest.mark.parametrize("sid", ["s2-shanghai-formation", "s4-chicago-lakeshore", "s5-sanfrancisco-terrain",
                                 "s6-suzhou-corridor"])
def test_sx(scenario_run, sid: str) -> None:
    _assert_success(scenario_run(sid, "ci", timeout_s=600))


# ---------------------------------------------------------------- S3（D1-AC-16，P1；M14-to-M16 第 2 条）
S3_PROCS = "sim-core,api,agent-runtime"
S3_CHAIN = ("agent.task.submitted", "agent.task.find", "agent.task.quote", "agent.task.awarded", "agent.task.effect",
            "agent.task.accepted")


def _s3_decisions(out) -> tuple[dict, int]:
    """M14 锁步用例 `_decisions` 的多进程版（tests/agent/test_s3_lockstep.py）：中标机、候选集、可行性与原因码、
    证据类型序列、谓词结论、最终置信度；另返回 accepted 的仿真时刻。AID 每次运行不同，按 agent.registered 映射到机体。"""
    veh = {str((e.get("data") or {}).get("aid")): (e.get("data") or {}).get("vehicle_id") or e.get("uav")
           for e in out.events if e.get("type") == "agent.registered"}
    ev = [e for e in out.kinds("agent.task.") if (e.get("data") or {}).get("task_id") in (None, "T-0001")]
    quote = next(e for e in ev if e.get("type") == "agent.task.quote")
    rows = list((quote.get("data") or {}).get("rows") or [])
    who = {str(r.get("aid")): veh.get(str(r.get("aid"))) or f"agent#{r.get('agent_no')}" for r in rows}
    award = next(e for e in ev if e.get("type") == "agent.task.awarded")
    acc = next(e for e in ev if e.get("type") == "agent.task.accepted")
    ad = acc.get("data") or {}
    dec = {"winner": who.get(str((award.get("data") or {}).get("aid"))),
           "cands": sorted(who.values()),
           "feasible": {who[str(r.get("aid"))]: (bool(r.get("feasible")), int(r.get("code") or 0)) for r in rows},
           "types": [e.get("type") for e in ev if e.get("type") in S3_CHAIN],
           "pred": (ad.get("predicate_ok"), ad.get("scope_ok")),
           "conf": round(float(ad.get("confidence") or 0.0), 3),
           "status": out.status}
    return dec, int(acc.get("t_sim_ns") or 0)


@full_only
@pytest.mark.ext
def test_s3(scenario_run) -> None:
    """D1-AC-16（M14-AC-023、026 的多进程判定）：t1 置信度 300 s 内升到 ≥ 0.9；b3 因 119 不可行；委派 effect = OK 且
    simulated；证据链 submitted、find、quote、awarded、effect、accepted 齐全；无委派外确认（incidental）。"""
    out = scenario_run("s3-newyork-sar", "ci", timeout_s=600, only=S3_PROCS)
    _assert_success(out)
    dec, t_acc = _s3_decisions(out)
    for k in S3_CHAIN:
        assert k in dec["types"], k
    assert dec["winner"] == "p600-b1", dec
    infeasible = [v for v, (ok, code) in dec["feasible"].items() if not ok]
    assert infeasible and all(dec["feasible"][v][1] == 119 for v in infeasible), dec["feasible"]
    eff = next(e for e in out.kinds("agent.task.effect"))
    assert (eff.get("data") or {}).get("status") == "OK" and (eff.get("data") or {}).get("simulated") is True
    assert dec["conf"] >= 0.9 and 0 < t_acc <= 300 * 10**9, (dec, t_acc)
    assert not out.kinds("agent.board.incidental")


@full_only
@pytest.mark.ext
def test_s3_rate_equivalence(scenario_run, tmp_path) -> None:
    """M14 锁步 `test_rate_equivalence_x1_x10` 的多进程版：×1 与 ×10 决策一致，accepted 仿真时刻差 ≤ 1.0 s。"""
    doc = json.loads((SCENARIOS / "s3-newyork-sar.json").read_text(encoding="utf-8"))
    v = copy.deepcopy(doc)
    v["profiles"]["x1"] = {"rate": 1, "record": False}
    d = write_variant(tmp_path, v)
    a = scenario_run("s3-newyork-sar", "ci", timeout_s=600, only=S3_PROCS)
    b = scenario_run("s3-newyork-sar", "x1", timeout_s=2400, scenarios_dir=d, only=S3_PROCS)
    da, ta = _s3_decisions(a)
    db, tb = _s3_decisions(b)
    assert da == db
    assert abs(ta - tb) <= 1_000_000_000, (ta, tb)
