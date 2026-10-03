"""state_ext 分片的预算控制（M08-FR-004，AWR-03 ADR-073 第 3 条）。

- 逐机耗时估计取上包络：实测更大时取实测（每次至多翻倍），更小时只向实测靠拢 10%；
- 末片之后剩余预算放不下发布时顺延到下一次调用，载荷与不顺延时逐字节相同；
- 交给空闲窗口发布线程时主线程只拼接，载荷经 `GatedPublisher.put` 发出。
"""

from __future__ import annotations

import msgpack
import pytest
from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R


class _Pub:
    def __init__(self) -> None:
        self.got: list[bytes] = []

    def put(self, payload: bytes) -> None:
        self.got.append(payload)


@pytest.fixture
def h():
    with R.isolated_registry() as reg:
        hh = CoreHarness(n=20, reg=reg)
        yield hh
        hh.close()


def _start_period(core) -> None:
    core._ext_last = -(1 << 62)
    core._ext_pos = -1


def test_publish_deferred_when_budget_short_and_payload_identical(h: CoreHarness) -> None:
    core = h.core
    pub = _Pub()
    core._pub_ext = pub
    _start_period(core)
    core._ext_us_per = 15.0
    core._ext_pub_us = 1e9  # 发布永远放不下：末片之后顺延
    assert core._slow_state_ext(10_000.0) is None
    assert core._ext_pos == len(core._ext_order) and pub.got == []
    assert core._slow_state_ext(5.0) is False  # 预算不足且未饿死：不发布
    core._ext_slice_wall -= 10**9  # 超过 EXT_STARVE_NS：兜底发布
    assert core._slow_state_ext(5.0) is None
    assert core._ext_pos == -1 and len(pub.got) == 1
    rows = msgpack.unpackb(pub.got[0], raw=False)
    assert len(rows) == len(core.roster.by_slot)
    ref = msgpack.unpackb(msgpack.packb(core.state_ext_items(), use_bin_type=True), raw=False)
    assert [r[0] for r in rows] == [r[0] for r in ref]


def test_per_row_estimate_is_upper_envelope(h: CoreHarness, monkeypatch) -> None:
    core = h.core
    core._pub_ext = _Pub()
    seq = iter([100_000, 4_000_000, 100_000])  # 每次 _ext_rows_packed 前后两次读计时：差值依次为 0.1、4、0.1 ms 起
    clock = [0]

    def perf() -> int:
        return clock[0]

    orig = core._ext_rows_packed

    def rows(chunk):
        clock[0] += next(seq)
        return orig(chunk)

    monkeypatch.setattr(core, "perf_ns", perf)
    monkeypatch.setattr(core, "_ext_rows_packed", rows)
    core._ext_pub_us = 0.0
    _start_period(core)
    core._ext_us_per = 50.0
    core._slow_state_ext(8 * 50.0)  # 8 架，实测 100 µs / 8 = 12.5 µs/架：只靠拢 10%
    assert core._ext_us_per == pytest.approx(50.0 + (12.5 - 50.0) * 0.1)
    per0 = core._ext_us_per
    core._slow_state_ext(8 * per0 + 1)  # 8 架，实测 4 ms / 8 = 500 µs/架：取实测但至多翻倍
    assert core._ext_us_per == pytest.approx(2.0 * per0)


def test_forced_slice_waits_for_a_light_round(h: CoreHarness) -> None:
    """饿死兜底的最小片只落在轻轮（ADR-074 第 4 条）：饿死但本轮剩余预算 < EXT_FORCE_MIN_US 时让出，轻轮到来即强制；
    连续超过 EXT_STARVE_HARD_NS 时不再挑轮。"""
    from awr.sim.runtime import main as M

    core = h.core
    core._pub_ext = _Pub()
    core._ext_pub_us = 0.0
    _start_period(core)
    core._ext_us_per = 1e6  # 预算永远放不下 EXT_SLICE_MIN 架：只能由饿死兜底
    assert core._slow_state_ext(5_000.0) is False  # 周期开始：未饿死
    pos0 = core._ext_pos
    core._ext_slice_wall = h.W[0] - M.EXT_STARVE_NS - 1  # 饿死
    assert core._slow_state_ext(M.EXT_FORCE_MIN_US - 1.0) is False and core._ext_pos == pos0  # 重轮：让出
    assert core._slow_state_ext(M.EXT_FORCE_MIN_US) is None and core._ext_pos == pos0 + M.EXT_SLICE_MIN  # 轻轮：强制
    core._ext_slice_wall = h.W[0] - M.EXT_STARVE_HARD_NS - 1  # 超过硬上界：不再挑轮
    assert core._slow_state_ext(150.0) is None and core._ext_pos == pos0 + 2 * M.EXT_SLICE_MIN
