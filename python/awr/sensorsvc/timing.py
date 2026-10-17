"""触发网格与曝光时刻（M25 §6.2.2、§6.2.3；M25-FR-022、023）：帧率为有理数，全部时刻为整数纳秒，不以 round(1e9/fps) 累加。"""

from __future__ import annotations

from fractions import Fraction

from awr.sensorsvc.errors import SensorSvcError

__all__ = ["T_TICK_NS", "check_exposure", "deliver_tick", "frame_period_ns", "parse_rate", "row_mid_ns", "t_end_ns", "t_mid_ns"]

T_TICK_NS = 100_000_000                     # 10 Hz 控制步
NS = 1_000_000_000


def parse_rate(fps: str | int | float) -> Fraction:
    """十进制字符串到有理数（"12.5" → 25/2）；float 输入按最短十进制表示转换。"""
    f = Fraction(repr(fps) if isinstance(fps, float) else str(fps))
    if f <= 0:
        raise SensorSvcError(649, f"frame rate must be positive: {fps!r}")
    return f


def frame_period_ns(rate: Fraction) -> Fraction:
    return NS / rate


def t_end_ns(t0_ns: int, rate: Fraction, k: int) -> int:
    """第 k 帧采集窗口结束时刻 `t0 + ⌊k · 10⁹ · q / p⌋`（k ≥ 1）。"""
    return t0_ns + (k * NS * rate.denominator) // rate.numerator


def deliver_tick(t0_ns: int, t_end: int, tick_ns: int = T_TICK_NS) -> int:
    """交付观测步 b = ⌈(t_end − t0) / T_tick⌉。"""
    return -((t0_ns - t_end) // tick_ns)


def t_mid_ns(t_end: int, exposure_ns: int, readout_ns: int = 0) -> int:
    """帧时间戳：中间行曝光中点 `t_end − ⌊(e + T_ro) / 2⌋`。"""
    return t_end - (exposure_ns + readout_ns) // 2


def row_mid_ns(t_mid: int, readout_ns: int, height: int, row: int) -> int:
    """第 i 行曝光中点 `t_mid + (i − (H − 1)/2) · T_ro / H`，以 float64 计算后取整到纳秒。"""
    return t_mid + round((row - (height - 1) / 2) * readout_ns / height)


def check_exposure(rate: Fraction, exposure_ns: int, readout_ns: int) -> None:
    if exposure_ns < 0 or readout_ns < 0 or exposure_ns + readout_ns > frame_period_ns(rate):
        raise SensorSvcError(649, f"exposure {exposure_ns} ns + readout {readout_ns} ns exceeds the frame period at {rate} Hz")
