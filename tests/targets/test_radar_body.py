"""M18-AC-024（P1）：配置 `radar_rx` 的识别物 radar 敏感体（M18-FR-030；AWR-04 §15.10.1、§15.10.3）。

- physical：位于配置体内、接收视场内且 SNR ≥ snr_req 时满足判据；被发现的距离与解析半径 `r_ref·10^((snr_ref + 10·lg sigma − snr_req)/40)`
  一致（≤ 1 m）；视场外不满足；geometric 只看配置体；
- 有效半径（M18 `effective_radius`）与禁入体（physical 时为有效半径外扩）；detail 的 `radius_eff_m` 含雷达体；
- 未配置 `radar_rx` 时 radar 体只入 schema（不参与判据、不产生禁入体）。
"""

from __future__ import annotations

import math

import pytest
from tlib import Bench, FakeWorld

from awr.sim.targets import effective_radius
from awr.sim.targets.envview import fallback_env
from awr.sim.targets.signature import P600

RX = {"snr_ref_db": 13.2, "r_ref_m": 400.0, "snr_req_db": 13.2, "fov_az_deg": 45.0, "fov_el_deg": 30.0}
R_EFF = 400.0 * 0.05 ** 0.25          # P600 RCS 0.05 m²：189.15 m


def _radar_target(mode: str, rx: dict | None = RX, **over) -> dict:
    d = {"class": "machine", "pos": [0.0, 0.0, None], "motion": {"mode": "static"}, "criterion_mode": mode, "alertness": 0.5,
         "sensitivity": [{"kind": "radar", "shape": "sphere", "radius_max_m": 600}], **over}
    if rx is not None:
        d["radar_rx"] = rx
    return d


def _first_discovery_range(mode: str, az_deg: float = 0.0, rx: dict | None = RX) -> float | None:
    with Bench(1, FakeWorld()) as b:
        b.add(_radar_target(mode, rx))
        a = math.radians(az_deg)
        for k in range(280):
            r = 700.0 - 2.5 * k
            b.S.fly(0, (r * math.cos(a), r * math.sin(a), 20.0))
            b.step(1)
            ev = b.events.of("target.state")
            if ev:
                return math.hypot(r, 20.0)
    return None


def test_physical_radar_criterion_matches_analytic_radius():
    r = _first_discovery_range("physical")
    assert r is not None and r <= R_EFF + 2.5 and r >= R_EFF - 2.5     # 首次满足（SUSPICIOUS）落在解析半径的一个步长内
    assert effective_radius(_cfg("physical"), 0, P600, fallback_env(), conservative=False, truncate=False) == pytest.approx(R_EFF, abs=1e-6)


def _cfg(mode: str):
    from awr.sim.targets import TargetConfig

    return TargetConfig.from_dict(_radar_target(mode), "machine-01")


def test_outside_receiver_fov_never_discovers():
    assert _first_discovery_range("physical", az_deg=120.0) is None


def test_geometric_uses_configured_body_only():
    r = _first_discovery_range("geometric", az_deg=120.0)
    assert r is not None and r == pytest.approx(600.0, abs=3.0)


def test_without_rx_stored_only():
    assert _first_discovery_range("physical", rx=None) is None
    assert _first_discovery_range("geometric", rx=None) is None


def test_keepout_and_detail():
    with Bench(1, FakeWorld()) as b:
        tid = b.add(_radar_target("physical"))
        b.S.fly(0, (1000.0, 0.0, 50.0))
        b.step(1)
        vols = b.rt.keepout(tid)
        assert len(vols) == 1 and vols[0].kind == "radar"
        assert vols[0].r_m == pytest.approx(R_EFF + 20.06, abs=1.0)          # 保守口径 a + 0.2 不影响雷达；外扩 20.06 m
        d = b.rt.detail(b.T(tid), b.ctx.t_ns, b.rt.env(b.ctx))
        assert d["sensitivity"][0]["kind"] == "radar"
        assert d["sensitivity"][0]["radius_eff_m"]["p600_mid360"] == pytest.approx(R_EFF, abs=0.1)
        t2 = b.add(_radar_target("physical", rx=None, pos=[50.0, 0.0, None]))
        assert b.rt.keepout(t2) == []
