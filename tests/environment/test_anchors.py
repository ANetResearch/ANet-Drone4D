"""锚点确定性（M07-AC-011；M07-FR-018）：服务端增量积分与参考 advance 一致、网格内部分值、1 h 夹具。"""

from __future__ import annotations

import json

import numpy as np
from envfix import OUT, _mgr, anchors_fixture

from awr.environment.anchors import H_NS, AnchorIntegrator, advance, anchors_initial, partial
from awr.environment.keyframe import EnvOp, from_wire
from awr.environment.weather.presets import presets
from awr.environment.weather.transitions import TransitionKf

SEC = 1_000_000_000


def test_incremental_equals_reference():
    P = presets()
    kf = TransitionKf("smooth", 2 * SEC, 32 * SEC, P.preset_vector("clear"), P.preset_vector("thunderstorm"), P.route("clear", "thunderstorm"))
    A_ref = anchors_initial(kf, 0)
    advance(A_ref, kf, 0, 2500)
    integ = AnchorIntegrator(anchors_initial(kf, 0))
    for k in range(1, 2501):
        integ.advance_to(kf, k, 0)
    a, b = A_ref.to_json(), integ.A.to_json()
    assert a == b  # 同一运算序列：逐位相同


def test_partial_is_between_grid_points():
    P = presets()
    kf = TransitionKf("step", 0, 0, P.preset_vector("rain"), P.preset_vector("rain"))
    A = anchors_initial(kf, 0)
    advance(A, kf, 0, 10)
    B = A.copy()
    advance(B, kf, 10, 11)
    mid = partial(A, kf, 10 * H_NS + H_NS // 2)
    assert A.s_m < mid.s_m < B.s_m
    assert abs(mid.s_m - 0.5 * (A.s_m + B.s_m)) < 1e-9
    assert partial(A, kf, 10 * H_NS).s_m == A.s_m


def test_at_matches_advance():
    m = _mgr("rain")
    integ = AnchorIntegrator(m.kf.anchors.copy())
    integ.advance_to(m.kf.transition(), 100, 0)
    got = integ.at(m.kf.transition(), 157 * H_NS + 7_000_000)
    ref = m.kf.anchors.copy()
    advance(ref, m.kf.transition(), 0, 157)
    ref = partial(ref, m.kf.transition(), 157 * H_NS + 7_000_000)
    assert got.to_json() == ref.to_json()


def test_fixture_frames_reproduce_checkpoints():
    """夹具自洽：从任一变化帧的锚点出发按同式推进，到检查点与服务端差 ≤ 1e-6 m（湿度 ≤ 1e-9）。"""
    d = json.loads((OUT / "anchors_1h.json").read_text(encoding="utf-8"))
    frames = [from_wire(f) for f in d["frames"]]
    for cp in d["checkpoints"][::3]:
        t = cp["t_ns"]
        kf = [f for f in frames if f.t_apply_ns <= t][-1]
        A = kf.anchors.copy()
        advance(A, kf.transition(), A.t_ns // H_NS, t // H_NS)
        assert abs(A.s_m - cp["s_m"]) <= 1e-6
        assert np.max(np.abs(np.asarray(A.d_enu_m) - np.asarray(cp["d_enu_m"]))) <= 1e-6
        assert abs(A.fall_rain_m - cp["fall_rain_m"]) <= 1e-6 and abs(A.fall_snow_m - cp["fall_snow_m"]) <= 1e-6
        assert abs(A.wetness - cp["wetness"]) <= 1e-9 and abs(A.puddle - cp["puddle"]) <= 1e-9


def test_fixture_up_to_date():
    cur = json.loads((OUT / "anchors_1h.json").read_text(encoding="utf-8"))
    new = json.loads(json.dumps(anchors_fixture(hours=0.05)))
    # 前 3 min 的帧序列与夹具一致（完整 1 h 由 envfix.py 生成，用时约 6 s）
    n = len(new["frames"])
    assert cur["frames"][:n] == new["frames"]


def test_ops_change_version_and_anchor_time():
    m = _mgr("clear")
    integ = AnchorIntegrator(m.kf.anchors.copy())
    integ.advance_to(m.kf.transition(), 500, 0)
    kf = m.apply(EnvOp("preset", name="rain"), 500 * H_NS, integ.A)
    assert kf.version == 2 and kf.anchors.t_ns == 500 * H_NS == kf.t_apply_ns
