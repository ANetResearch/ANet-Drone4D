"""过渡连续（M07-AC-012；M07-FR-004、FR-021）：过渡中新操作在 t_apply 处全字段连续、风向最短弧、MOR 对数插值、
路由只在稳态且 to_preset 为起点时启用、一阶差分有界。"""

from __future__ import annotations

import numpy as np
from envfix import _anch, _mgr

from awr.environment.anchors import H_NS
from awr.environment.keyframe import EnvOp
from awr.environment.weather.presets import DIR, MOR_BG, NF, presets
from awr.environment.weather.transitions import TransitionKf, eval_env, exp_t1_ns

SEC = 1_000_000_000


def test_new_op_mid_transition_is_continuous():
    m = _mgr("clear")
    t0 = 100 * H_NS
    m.apply(EnvOp("preset", name="thunderstorm"), t0, _anch(t0))
    assert m.kf.via == ["partlyCloudy", "overcast", "rain", "heavyRain"]
    for dt_s in (0.02, 3.0, 11.34, 17.0, 29.98):
        t = t0 + int(dt_s * SEC) // H_NS * H_NS
        before = np.asarray(eval_env(m.kf.transition(), t, np.empty(NF)))
        kf = m.apply(EnvOp("preset", name="fog"), t, _anch(t))
        assert kf.via == []  # 过渡中改选：直达
        after = np.asarray(eval_env(kf.transition(), t, np.empty(NF)))
        assert np.array_equal(before, after)
        kf = m.apply(EnvOp("set", patch={"wind": {"speed_ref_mps": 12.5}}, mode="exp"), t, _anch(t))
        after2 = np.asarray(eval_env(kf.transition(), t, np.empty(NF)))
        assert np.array_equal(before, after2)


def test_route_only_when_steady_and_from_preset():
    m = _mgr("clear")
    t0 = 10 * H_NS
    kf = m.apply(EnvOp("set", patch={"cloud": {"cover": 0.2}}, duration_s=0), t0, _anch(t0))
    assert kf.to_preset is None and kf.mode == "step"
    kf = m.apply(EnvOp("preset", name="thunderstorm"), t0 + H_NS, _anch(t0))
    assert kf.via == []
    m2 = _mgr("clear")
    kf = m2.apply(EnvOp("set", patch={"wind": {"dir_from_deg": 90}}, duration_s=0), t0, _anch(t0))
    assert kf.to_preset == "clear"  # 只改用户轴：仍在预设上
    kf = m2.apply(EnvOp("preset", name="thunderstorm"), t0 + H_NS, _anch(t0))
    assert kf.via == ["partlyCloudy", "overcast", "rain", "heavyRain"]


def test_shortest_arc_359_to_1():
    P = presets()
    a = P.default_vector()
    a[DIR] = 359.0
    b = a.copy()
    b[DIR] = 1.0
    kf = TransitionKf("smooth", 0, 3 * SEC, a, b)
    seen = [float(eval_env(kf, int(t), np.empty(NF))[DIR]) for t in np.linspace(0, 3 * SEC, 301)]
    for v in seen:
        assert v >= 359.0 - 1e-9 or v <= 1.0 + 1e-9
    steps = np.diff(np.unwrap(np.radians(seen)))
    assert np.degrees(np.abs(steps)).sum() <= 2.0 + 1e-9


def test_mor_log_space():
    P = presets()
    a = P.preset_vector("clear")
    b = P.preset_vector("fog")
    kf = TransitionKf("exp", 0, exp_t1_ns(0), a, b)
    mid = float(eval_env(kf, 2 * SEC, np.empty(NF))[MOR_BG])
    k = 1.0 - np.exp(-1.2 * 2.0)
    assert abs(np.log(mid) - (np.log(a[MOR_BG]) + (np.log(b[MOR_BG]) - np.log(a[MOR_BG])) * k)) < 1e-12


def test_preset_sequence_first_difference_bounded():
    P = presets()
    a = P.preset_vector("clear")
    b = P.preset_vector("thunderstorm")
    kf = TransitionKf("smooth", 0, 30 * SEC, a, b, P.route("clear", "thunderstorm"))
    lo = np.array([f.lo for f in P.fields])
    hi = np.array([f.hi for f in P.fields])
    prev = None
    worst = 0.0
    for t in range(0, 30 * SEC + 1, H_NS):
        s = np.asarray(eval_env(kf, t, np.empty(NF)))
        assert np.all(s >= lo - 1e-9) and np.all(s <= hi + 1e-9)
        if prev is not None:
            d = np.abs(s - prev)
            d[DIR] = min(d[DIR], 360 - d[DIR])
            d[MOR_BG] = abs(np.log(s[MOR_BG]) - np.log(prev[MOR_BG]))
            worst = max(worst, float(np.max(d / np.maximum(hi - lo, 1e-9))))
        prev = s
    # 20 ms 一步内任何字段的变化不超过其量程的 1%（smoothstep 峰值斜率 1.5 × 段速率）
    assert worst < 0.01
