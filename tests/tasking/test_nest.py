"""M20-AC-001（纯函数部分）、M20-AC-002：充电时间（AWR-20 §4.5，与 M21 实现对拍）、起降位时间线、稳态检查。"""

from __future__ import annotations

import itertools

import pytest

from awr.tasking.nestmodel import PadTimeline, charge_time_s, soc_after, steady_state


def test_charge_time_matches_awr20_and_m21() -> None:
    assert charge_time_s(10.0, 1.0, 10.0, 0.0, 0.95) == pytest.approx(4178.0, rel=0.01)    # P600 LPB610HV + C1-XR
    assert charge_time_s(22.0, 0.5, 10.0, 0.0, 0.95) == pytest.approx(8832.0, rel=0.01)    # AWR-V1 12S 22 Ah
    from awr.sim.catalog import charge as M21

    for q, c, i, s0, s1 in ((10, 1, 10, 0, 0.95), (22, 0.5, 10, 0.2, 0.95), (5, 1, 10, 0.5, 0.9), (5.9, 1, 10, 0.9, 0.95)):
        assert charge_time_s(q, c, i, s0, s1) == pytest.approx(M21.charge_time_s(q, c, i, s0, s1), rel=1e-12)


def test_soc_after_is_inverse() -> None:
    for s0 in (0.0, 0.3, 0.85):
        for s1 in (0.5, 0.8, 0.9, 0.95):
            if s1 <= s0:
                continue
            t = charge_time_s(10.0, 1.0, 10.0, s0, s1)
            assert soc_after(10.0, 1.0, 10.0, s0, t) == pytest.approx(s1, abs=1e-9)
    assert soc_after(10.0, 1.0, 10.0, 0.2, 100.0) == pytest.approx(0.2)   # 降温等待期间不变


def test_pad_timeline_interval_and_recover_priority() -> None:
    pad = PadTimeline(30.0)
    t0 = pad.book(0.0, "launch", "a")
    t1 = pad.book(0.0, "launch", "b")
    t2 = pad.book(5.0, "launch", "c")
    assert (t0, t1, t2) == (0.0, 30.0, 60.0)
    # 回收在 t = 31 插队：与之冲突的起飞顺延，相邻事件间隔仍 ≥ 30 s
    tr = pad.book(31.0, "recover", "d", recover=True)
    assert tr == 31.0
    ts = sorted(e[0] for e in pad.events)
    assert all(b - a >= 30.0 - 1e-9 for a, b in itertools.pairwise(ts))
    assert any(e[2] == "d" and e[1] == "recover" for e in pad.events)


@pytest.mark.parametrize(("t_on", "n_ch", "n_pack"), [(1192.0, 4, 5), (1090.0, 4, 6)])
def test_steady_state_p600(t_on: float, n_ch: int, n_pack: int) -> None:
    t_ch = charge_time_s(10.0, 1.0, 10.0, 0.0, 0.95)
    ss = steady_state(t_on, 100.0, 60 + 180 + 60, 180.0, t_ch)
    assert (ss["n_ch"], ss["n_pack"]) == (n_ch, n_pack)
    assert ss["n_air"] == 2 and ss["n_air_recommended"] == 3


def test_steady_state_v1() -> None:
    t_ch = charge_time_s(22.0, 0.5, 10.0, 0.0, 0.95)
    ss = steady_state(4700.0, 100.0, 300.0, 180.0, t_ch)
    assert (ss["n_ch"], ss["n_pack"]) == (2, 3)
