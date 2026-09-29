"""M09-AC-017：估价一致——`EnergyModel.estimate` 与 60 s 实飞的 soc 差 ≤ 0.01；返航落地后 < 20% 时返回 119；`path_wh` 沿同一
实飞轨迹积分与运行期电量模型一致（≤ 3%）；`rtl_plan` 只读缓存。单次耗时（p99 ≤ 0.5 ms）为 perf，不在此运行。"""

from __future__ import annotations

import numpy as np
from safelib import Harness

from awr.contracts.reasons import Reason
from awr.sim.core.estimate import vehicle_profile
from awr.sim.core.interfaces import EstimatePath


def test_estimate_vs_flight_and_path_wh() -> None:
    h = Harness(limits="px4_default")
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(20.0)
        h.advance(1.0)
        s = h.slot()
        bb = h.S.blocks["battery"]
        p0 = h.pos()
        goal = p0 + np.array([300.0, 0.0, 0.0])
        soc0 = float(bb["soc"][s])
        wh0 = float(bb["wh_used"][s])
        prof = vehicle_profile(h.core.T.get("p600_mid360"))
        est = h.svc.energy.estimate(prof, soc0, EstimatePath(np.array([p0, goal]), np.array([5.0])), None)
        samples = []
        rep, cid = h.goto(goal, wait=False, speed_mps=5.0)
        assert rep["status"] == "accepted"
        t0 = h.core.clock.t_ns
        call = h.call(cid)  # 幂等表只保留 60 s【墙钟】，持有调用对象
        while not call.final:
            h.advance(0.1)
            samples.append([(h.core.clock.t_ns - t0) * 1e-9, *h.pos(), *h.S.enu.vel[s]])
        assert call.status == "succeeded"
        flown = (h.core.clock.t_ns - t0) * 1e-9
        d_soc_run = soc0 - float(bb["soc"][s])
        d_soc_est = soc0 - est.soc_after_pct / 100.0
        assert 55.0 <= flown <= 75.0 and est.eta_s > 55.0
        assert abs(d_soc_est * est.eta_s / flown - d_soc_run) <= 0.01, (d_soc_est, d_soc_run)  # 同一时长口径
        assert abs(d_soc_est - d_soc_run) <= 0.01, (d_soc_est, d_soc_run)
        wh_run = float(bb["wh_used"][s]) - wh0
        wh_path = h.svc.energy.path_wh("p600_mid360", np.asarray(samples), None)
        assert abs(wh_path - wh_run) <= 0.03 * wh_run, (wh_path, wh_run)
        # 不可行：返航落地后 < 20%
        far = EstimatePath(np.array([p0, p0 + np.array([3000.0, 0.0, 0.0]), p0]), np.array([5.0, 5.0]))
        r = h.svc.energy.estimate(prof, 0.35, far, None)
        assert not r.feasible and r.code == int(Reason.ENERGY_INFEASIBLE) and r.soc_after_return_pct < 20.0
        # x500：battery = null，恒可行、不耗电
        rx = h.svc.energy.estimate(vehicle_profile(h.core.T.get("x500")), 1.0, far, None)
        assert rx.feasible and rx.energy_wh == 0.0
        # rtl_plan：缓存（z_rtl ≥ home + 30）
        plan = h.svc.energy.rtl_plan(s)
        assert plan.z_rtl_m >= h.S.enu.home[s][2] + 30.0 - 1e-6 and plan.t_rtl_s > 0 and plan.v_c_mps >= 1.0
    finally:
        h.close()
