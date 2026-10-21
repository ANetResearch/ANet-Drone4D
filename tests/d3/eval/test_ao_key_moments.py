"""AO 关键时刻（K07；A10 遗留 F-77、docs/05 §30.5；M27-FR-090；D3-AC-18 的 AO_real 条款）。

r6 口径：每个含人员参与者的计分事件取真值区间（缺省声明窗口）的开始、中点、结束 3 个时刻；参与者已不在场（上车、进入建筑）的
时刻改取事件前全部参与者都在场的最近 tick（不早于窗口起点前 2 s）；换算后重合的时刻只计一次。旧实现只取声明窗口中点，瞬时事件在
中点时参与者已消失而被整条跳过（crop-handoff-001 上 AO 只由 1 个时刻决定，呈 0 / 0.83 双峰）。

用例用不依赖世界包的替身（平地、全通视），只验证关键时刻的选取与 AO 的汇总口径；与评测器全流程一致性由 `test_constructed.py`、
`test_eval_timing.py` 覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from awr.bench.eval.ao import active_observation, key_moments, truth_intervals

N = 3200                                    # 320 s，tick 0.1 s


def _pres(n_ticks: int, *spans: tuple[int, int]) -> np.ndarray:
    """(n_ticks + 1, len(spans)) 在场矩阵：第 j 列在 [lo, hi] 内在场。"""
    out = np.zeros((n_ticks + 1, len(spans)), bool)
    for j, (lo, hi) in enumerate(spans):
        out[lo:hi + 1, j] = True
    return out


def _ev(eid, parts, a, b):
    return {"id": eid, "type": "t", "participants": parts, "a_s": float(a), "b_s": float(b)}


def test_truth_intervals_first_hit_per_annotation() -> None:
    rows = [{"annotation": "enc", "interval_ms": [238900, 247200]}, {"annotation": "enc", "interval_ms": [222000, 233900]},
            {"annotation": None, "interval_ms": [0, 1000]}, {"id": "spawn"}, {"annotation": "board", "interval_ms": [255000, 255000]}]
    assert truth_intervals(rows) == {"enc": (222.0, 233.9), "board": (255.0, 255.0)}
    assert truth_intervals(None) == {}


def test_three_moments_from_truth_interval() -> None:
    P = _pres(N, (0, N), (0, N))
    ks = key_moments(_ev("enc", ["a", "b"], 195, 250), P, N, (222.0, 233.9))
    assert [k for k, _m in ks] == [2220, 2280, 2339] and all(m.all() for _k, m in ks)
    # 无真值：声明窗口的开始、中点、结束
    assert [k for k, _m in key_moments(_ev("enc", ["a", "b"], 195, 250), P, N)] == [1950, 2225, 2500]


def test_instantaneous_event_takes_the_moment_before() -> None:
    """上车（真值区间 [255, 255]）：B 在 255.0 s 已在车内，取 254.9 s；声明窗口的中点与结束都换算到同一时刻，只计一次。"""
    P = _pres(N, (0, 2549))
    assert [(k, m.tolist()) for k, m in key_moments(_ev("board", ["b"], 240, 275), P, N, (255.0, 255.0))] == [(2549, [True])]
    assert [k for k, _m in key_moments(_ev("board", ["b"], 240, 275), P, N)] == [2400, 2549]
    # 进入建筑恰在窗口起点（290 s）：允许回看到窗口起点前 2 s
    P = _pres(N, (0, 2899))
    assert [k for k, _m in key_moments(_ev("enter", ["a"], 290, 320), P, N, (290.0, 290.0))] == [2899]
    assert key_moments(_ev("enter", ["a"], 290, 320), P, N, (290.0, 290.0), pad_s=0.0) == []


def test_partial_presence_and_absent() -> None:
    """窗口内从无全员在场的 tick：取该时刻在场的参与者子集；无人在场的时刻跳过。"""
    P = _pres(N, (0, 1000), (1500, 1600))
    ks = key_moments(_ev("x", ["a", "b"], 50, 160), P, N)
    assert [(k, m.tolist()) for k, m in ks] == [(500, [True, False]), (1600, [False, True])]   # 105 s 无人在场，跳过
    assert key_moments(_ev("y", ["a"], 200, 210), _pres(N, (0, 10)), N) == []


# ---------------------------------------------------------------------------------------------- 汇总口径（替身世界与运行）

class _Tape:
    def __init__(self, present: dict[str, tuple[int, int]]) -> None:
        self.ids = ["a", "b", "c"]
        self.present = present

    def actors_info(self, scripted_only: bool = False):
        cls = {"a": "person", "b": "person", "c": "vehicle"}
        return [{"id": a, "idx": i, "cls": cls[a], "size_m": [0.5, 0.5, 1.8]} for i, a in enumerate(self.ids)]

    def actor_states(self, t_ns: int, idx: list[int]):
        k = round(t_ns / 100_000_000)
        ids = [self.ids[i] for i in idx]
        st = [1 if self.present[a][0] <= k <= self.present[a][1] else 3 for a in ids]
        return SimpleNamespace(pos_enu_m=np.array([[10.0 * i, 0.0, 0.0] for i in idx]), yaw_rad=np.zeros(len(idx)), state=np.array(st))


class _World:
    def ground_dtm(self, xy):
        return np.zeros(len(xy))

    def los_batch(self, A, B):
        return np.ones(len(A), bool)


def _run(rows):
    spans = {"a": (0, N), "b": (0, 2549), "c": (0, N)}
    tape = _Tape(spans)

    def present(ids):
        return _pres(N, *(spans[a] for a in ids))

    return SimpleNamespace(tape=tape, n_ticks=N, fleet={"pos": np.tile([5.0, 0.0, 40.0], (N + 1, 1, 1))}, l1=rows, present=present)


def _rows(seq: int, actors) -> list[dict]:
    return [{"uav": "uav-01", "actor": a, "seq": seq, "h_unocc_px": 60.0, "vis": 1.0} for a in actors]


def test_ao_averages_over_all_key_moments() -> None:
    """相遇（A、B，真值 222–233.9 s）只在 222 s 有证据，上车（B，真值 255 s）在 254.9 s 有证据：4 个关键时刻中 2 个满分 →
    AO_real 0.5；不给真值时按声明窗口取 5 个时刻，其中 2 个有证据 → 0.4。旧实现（只取中点）在上车事件上被跳过。"""
    E = [_ev("enc", ["a", "b"], 195, 250), _ev("board", ["b", "c"], 240, 275)]
    run = _run(_rows(2220, "ab") + _rows(2549, "b"))
    truth = [{"annotation": "enc", "interval_ms": [222000, 233900]}, {"annotation": "board", "interval_ms": [255000, 255000]}]
    kw = {"fx": 1663.0, "envelope_agl_m": (30.0, 120.0)}
    ao = active_observation(E, ["uav-01"], run, _World(), truth_rows=truth, **kw)
    assert ao["ao_real"] == pytest.approx(0.5) and ao["oracle"]["version"] == "ao-oracle-v0-grid@1"
    assert active_observation(E, ["uav-01"], run, _World(), **kw)["ao_real"] == pytest.approx(0.4)
    pres = {"a": run.present(["a"])[:, 0], "b": run.present(["b"])[:, 0]}            # 评测器传入的在场矩阵与按磁带计算一致
    assert active_observation(E, ["uav-01"], run, _World(), truth_rows=truth, present=pres, **kw) == ao
