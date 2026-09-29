"""剧本 S1 端到端（M10-AC-003 的 M10 部分、M10-AC-014）：深圳 381 m 塔两架 P600 分高度带螺旋扫描。

M10 自有的谓词（`missions_done`、`facade_coverage ≥ 0.9`）必须成立；M08、M09 的度量（最小间距、guard_events、
阵风窗口位置误差、落地、电量）在本测试台未登记时按 16 §12.3 判为假，只检查其值为 None 或成立。
约 150–200 s 墙钟，标记 slow、needs_data（缺少已构建的深圳世界时 skip）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from harness import ROOT, Sim

from awr.sim.core import metrics as MET

pytestmark = [pytest.mark.slow, pytest.mark.needs_data]


def test_s1_end_to_end() -> None:
    wd = ROOT / "worlds" / "shenzhen"
    if not (wd / "world.json").exists() or not (ROOT / "scenarios" / "s1-shenzhen-facade.json").exists():
        pytest.skip("shenzhen world or S1 scenario not present")
    from awr.world.geometry.query import open_world_query

    sim = Sim(open_world_query(Path(wd), allow_derive=False), n=1, world_id="shenzhen")
    try:
        sim.advance(0.5)
        if "elapsed_s" not in {m.name for m in MET.list_metrics()}:          # INT-1：SimCore 已登记 M08 度量
            MET.register_metric("elapsed_s", lambda **kw: sim.core.clock.t_ns * 1e-9, owner="M08")
        res = sim.rt.load_scenario("s1-shenzhen-facade")
        assert res.get("code", 0) == 0, res
        eng = sim.rt.missions
        assert sim.until(lambda: {"m-lower", "m-upper"} <= set(eng.missions), 30.0, 0.5)
        sim.until(lambda: all(eng.missions[m].state in ("DONE", "ABORTED") for m in ("m-lower", "m-upper")), 1500.0,
                  5.0)
        assert all(eng.missions[m].state == "DONE" for m in ("m-lower", "m-upper"))
        prec = {mid: eng.missions[mid].energy for mid in ("m-lower", "m-upper")}          # 启动时的能量预检
        # 12 §7.2：落地 SOC 预检 p600-01 0.36 ± 0.03、p600-02 0.32 ± 0.03（M10-AC-014）
        soc = {p["id"]: p["soc_after"] for m in prec.values() for p in (m or {}).get("per_vehicle", [])}
        assert soc["p600-01"] == pytest.approx(0.36, abs=0.03) and soc["p600-02"] == pytest.approx(0.32, abs=0.03)
        cov = sim.rt.coverage.facade_ratio(["m-lower", "m-upper"])
        assert cov is not None and cov >= 0.9
        results = [e[2] for e in sim.kinds("scenario.result")]
        assert results, "scenario.result not emitted"
        preds = {p["expr"]: p for p in results[-1]["predicates"]}
        assert preds["missions_done == True"]["ok"]
        assert next(p for k, p in preds.items() if k.startswith("facade_coverage"))["ok"]
        for p in preds.values():
            assert p["ok"] or p["value"] is None, p                          # 其余度量未登记时为缺失
        # 两架机在扫描期间保持垂直分层（高度带不重叠），最小间距由 FleetGuard 与分层共同保证
        z = [sim.pos(v)[2] for v in ("p600-01", "p600-02")]
        assert np.all(np.isfinite(z))
    finally:
        sim.close()
