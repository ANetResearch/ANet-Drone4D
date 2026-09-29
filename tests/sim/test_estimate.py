"""估价 `ctl/sim-core/estimate`（M08-AC-029 的功能部分；M08-FR-064；ADR-036；AWR-12 §5.8.3–§5.8.4）。

- 随机 50 组起终点（同高度，20–200 m）：`eta_s` 与实际 goto 到达（距目标 < 0.5 m）用时相差 ≤ 10%；
- 路径各腿与 `EnergyModel.estimate` 收到的参数一致（假实现记录）：起点 → 转场高度 → 目标上方 → 目标 → 返航高度 → home 上方 → home；
- 不可行时 `feasible = false`、code 119；未登记 EnergyModel 时用兜底模型；
- 限流：同一秒内第 21 次得 111（令牌桶，SimCore 慢任务路径）；
- 执行耗时 p99 ≤ 1 ms 与并发下单步 p99 属 perf 用例（并行阶段不跑）。
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest
from simlib import CoreHarness, Rig

from awr.contracts.reasons import Reason
from awr.sim.core.estimate import EstimateService, TokenBucket, fallback_estimate, vehicle_profile
from awr.sim.core.interfaces import EstimatePath, EstimateResult, RtlPlan
from awr.sim.fleet import actions as ACT
from awr.sim.fleet import params_px4 as P
from awr.sim.fleet.stages import registry as R


def test_eta_matches_goto_within_10pct() -> None:
    n = 50
    rng = np.random.default_rng(4)
    rig = Rig("p600_mid360", n, spacing=400.0)
    try:
        S = rig.S
        rig.air(np.arange(n), 20.0, 3.0)
        est = EstimateService(S, rig.T, None)
        d = rng.uniform(20, 200, n)
        a = rng.uniform(0, 2 * math.pi, n)
        tg = S.p[:n].copy()
        tg[:, 0] += d * np.cos(a)
        tg[:, 1] += d * np.sin(a)
        etas = np.array([est.build_path(i, np.array([tg[i, 1], tg[i, 0], -tg[i, 2]]), None, 0.0)[1] for i in range(n)])
        ACT.begin_goto(S, np.arange(n), tg, np.nan, None, rig.t)
        t0 = rig.t
        arr = np.full(n, np.nan)
        for _ in range(90 * 125):
            rig.step(2)
            m = np.isnan(arr) & (np.linalg.norm(S.p[:n] - tg, axis=1) < 0.5)
            arr[m] = rig.t - t0
            if not np.isnan(arr).any():
                break
        assert not np.isnan(arr).any()
        err = np.abs(arr - etas) / etas
        assert err.max() <= 0.10, (err.max(), d[np.argmax(err)])
    finally:
        rig.close()


class RecEnergy:
    def __init__(self, feasible: bool = True) -> None:
        self.feasible = feasible
        self.paths: list[EstimatePath] = []
        self.profiles: list = []

    def estimate(self, profile, soc, path, env) -> EstimateResult:
        self.paths.append(path)
        self.profiles.append((profile, soc))
        return EstimateResult(123.0, 42.0, 55.0 if self.feasible else 5.0, self.feasible,
                              0 if self.feasible else int(Reason.ENERGY_INFEASIBLE))

    def rtl_plan(self, slot: int) -> RtlPlan:
        return RtlPlan(30.0, 5.0, 60.0)

    def path_wh(self, profile_id, samples, env=None) -> float:
        return 0.0


def _harness(em) -> tuple[CoreHarness, object]:
    ctx = R.isolated_registry()
    reg = ctx.__enter__()
    if em is not None:
        R.register_energy_model(em)
    return CoreHarness(n=1, reg=reg), ctx


def test_path_legs_forwarded_to_energy_model() -> None:
    em = RecEnergy()
    h, ctx = _harness(em)
    try:
        h.takeoff(10.0)
        vid = h.ids()[0]
        p = h.pos()
        home = h.S.enu.home[h.slot()].copy()
        tgt = [p[0] + 80.0, p[1] + 60.0, p[2] + 5.0]
        rep = h.core.estimator.estimate({"v": 1, "vehicle_id": vid, "target_enu_m": tgt, "speed_mps": 2.0, "dwell_s": 10.0})
        assert rep["feasible"] is True and rep["code"] == 0
        assert rep["energy_wh"] == 42.0 and rep["soc_after_pct"] == 55.0
        path = em.paths[-1]
        z_c = max(p[2], tgt[2])
        z_r = max(tgt[2], home[2] + P.RTL_RETURN_ALT)
        exp = np.array([p, [p[0], p[1], z_c], [tgt[0], tgt[1], z_c], tgt, [tgt[0], tgt[1], z_r], [home[0], home[1], z_r],
                        home])
        assert np.allclose(path.points_enu_m, exp, atol=1e-9)
        assert path.speeds_mps.tolist()[1] == 2.0 and path.dwell_s == 10.0
        assert em.profiles[-1][0].profile_id == "p600_mid360"
        em.feasible = False
        rep = h.core.estimator.estimate({"v": 1, "vehicle_id": vid, "target_enu_m": tgt})
        assert rep["feasible"] is False and rep["code"] == int(Reason.ENERGY_INFEASIBLE)
        assert h.core.estimator.estimate({"v": 1, "vehicle_id": "nope", "target_enu_m": tgt})["code"] == int(Reason.NO_VEHICLE)
    finally:
        h.close()
        ctx.__exit__(None, None, None)


def test_fallback_model_infeasible_at_low_soc() -> None:
    h, ctx = _harness(None)
    try:
        prof = vehicle_profile(h.core.T.get("p600_mid360"))
        path = EstimatePath(np.array([[0.0, 0, 20], [3000.0, 0, 20], [0.0, 0, 20]]), np.array([5.0, 5.0]), 0.0)
        ok = fallback_estimate(prof, 1.0, path)
        low = fallback_estimate(prof, 0.25, path)
        assert ok.eta_s > 1000 and ok.energy_wh > 0
        assert low.feasible is False and low.code == int(Reason.ENERGY_INFEASIBLE)
    finally:
        h.close()
        ctx.__exit__(None, None, None)


class _Req:
    def __init__(self, msg: dict) -> None:
        self._m = msg
        self.reply = None

    def msg(self) -> dict:
        return self._m

    def reply_msg(self, rep: dict) -> None:
        self.reply = rep


def test_rate_limit_21st_request_111() -> None:
    b = TokenBucket(20.0, 20.0, 20.0)
    assert all(b.take(1_000_000_000) for _ in range(20)) and not b.take(1_000_000_000)
    assert b.take(1_000_000_000 + 60_000_000)  # 60 ms 后补回 1 个
    h, ctx = _harness(RecEnergy())
    try:
        vid = h.ids()[0]
        p = h.pos()
        reqs = [_Req({"v": 1, "vehicle_id": vid, "target_enu_m": [p[0] + 10.0, p[1], p[2] + 10.0]}) for _ in range(21)]
        for r in reqs:
            h.core._est_q.append((r, h.core.clock.wall_mono_ns()))
        for _ in range(200):  # 墙钟不前进：21 次都落在同一秒内
            h.core.iterate()
            if all(r.reply is not None for r in reqs):
                break
        codes = [r.reply["code"] for r in reqs]
        assert codes.count(int(Reason.RATE_LIMITED)) == 1 and codes[-1] == int(Reason.RATE_LIMITED)
    finally:
        h.close()
        ctx.__exit__(None, None, None)


@pytest.mark.perf
def test_estimate_exec_p99_under_1ms() -> None:
    h, ctx = _harness(None)
    try:
        vid = h.ids()[0]
        ts = []
        for k in range(500):
            t0 = time.perf_counter()
            h.core.estimator.estimate({"v": 1, "vehicle_id": vid, "target_enu_m": [k % 50, 30.0, 20.0]})
            ts.append(time.perf_counter() - t0)
        assert float(np.percentile(ts, 99)) <= 1e-3
    finally:
        h.close()
        ctx.__exit__(None, None, None)
