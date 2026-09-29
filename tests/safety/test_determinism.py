"""M09-AC-028（P0 部分）：同一输入（同一种子、同一脚本、注入的假墙钟）运行两次，SafetyEvent 与 uav.state 序列逐条一致。
按输入日志重仿真（ext，D1-AC-31）由 M08 `tests/sim/test_resim.py` 与链路判定日志（`LinkMonitor.logged`）覆盖。"""

from __future__ import annotations

import numpy as np
from safelib import Harness


def _script() -> tuple[list[dict], list[tuple]]:
    h = Harness(n=3, spacing=15.0)
    try:
        h.ready()
        h.gcs_age_ms = 0
        a, b, c = h.takeoff_all(8.0)
        h.goto([h.pos(a)[0], h.pos(a)[1] + 40.0, 12.0], uav=a, wait=False)
        h.goto([h.pos(b)[0] - 16.0, h.pos(b)[1], 8.0], uav=b, wait=False)  # 向 a 靠近：间距告警
        h.advance(3.0)
        h.S.est_age_s[h.slot(c)] = 0.12
        h.advance(2.0)
        h.gcs_age_ms = None  # 断链
        h.advance(4.0)
        return [dict(e) for e in h.events], list(h.rt.link.logged)
    finally:
        h.close()


def test_same_input_same_events() -> None:
    e1, l1 = _script()
    e2, l2 = _script()
    assert len(e1) > 10
    assert e1 == e2
    assert l1 == l2 and len(l1) >= 2
    codes = [e.get("code") for e in e1 if "code" in e]
    assert "SAF.EST.TIMEOUT" in codes and "SAF.LINK.LOST_HOLD" in codes
    assert np.all(np.diff([e["t_sim_ns"] for e in e1]) >= 0)
