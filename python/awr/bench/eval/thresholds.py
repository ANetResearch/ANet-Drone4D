"""评测阈值与版本（M27 §6.8；`eval_thresholds.v1`）。"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["EVALUATOR_VERSION", "ORACLE_VERSION", "SCORE_VERSION", "TH", "THRESHOLDS_VERSION", "Thresholds"]

EVALUATOR_VERSION = "0.1.0"
THRESHOLDS_VERSION = "eval_thresholds.v1"
ORACLE_VERSION = "ao-oracle-v0-grid@1"
SCORE_VERSION = "d3-score-0"


@dataclass(frozen=True)
class Thresholds:
    h_px: float = 16.0               # 识别：投影高度（无遮挡）≥ 16 px
    vis: float = 0.5                 # 且身体 5 点 LOS 可见比例 ≥ 0.5
    pad_s: float = 2.0               # EC 窗口前后各 2 s
    eu_dt_s: float = 15.0            # EU：report 区间与事件区间的间隔 ≤ 15 s（重叠为 0）
    eu_iou: float = 0.3
    ao_q_px: float = 30.0            # AO 质量 min(1, h / 30 px) · 可见比例
    ao_window_s: float = 1.0
    oracle_grid_m: float = 10.0
    oracle_alt_step_m: float = 10.0
    person_r_m: float = 0.4
    person_h_m: float = 1.8
    uav_r_m: float = 0.5


TH = Thresholds()
