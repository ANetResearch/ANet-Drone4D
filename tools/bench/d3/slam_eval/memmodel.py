"""`slam_eval` 内存模型与资源份额（M27-FR-104、§6.7.7 `mem_limit_gb`；AWR-05 §5.6.2；RV-04；D3-AC-47 的 r_map 项）。

    mem_limit = safety × (base + bytes_per_point × r_map × T)          （GB = 1e9 B）

- r_map：按型号标定的地图点累积率（点/s）。FAST-LIVO2 每个体素地图点保存 `pointWithVar`（约 430 B），内存随扫过的表面积线性增长
  （r1 §2.4）。MID-360 倒装 30 m AGL 按 ADR-215 决策 (5) 冻结为 4.5 万点/s（SL3 斜率口径上限 4.3 万点/s 加余量；360 s 作业限额
  约 1.5 × (2 GB + 430 B × 45,000 × 360) ≈ 13.4 GB）；Avia 维持暂定 8 万点/s（r1 HKisland03 320 s 峰值 12.7 GB），冻结移入 D3.x
  （独占核复测或在模型中加入积压项）。
- 份额：MID-360 LIVO 作业 `--cpus 4`（ADR-215 (5)：2 核时丢弃约 25% 的 LiDAR 帧，4 核时处理帧比例 ≥ 99%）；长量程型号（Avia 等）
  维持暂定下限 ≥ 20 GB、4 核；HKisland03 真机回归作业 ≥ 32 GB、8 核（ADR-215 (4)，`slam_eval regress` 缺省值）。
- 标定：容器内每 2 s 采样 FastLIVO2 进程 RSS（`res.txt`）。回放墙钟乘以倍速即航时；对回放窗口内的 RSS(航时) 做最小二乘直线，
  斜率 ÷ 430 B 为 `r_map_slope`；峰值口径 `r_map_peak = (RSS_peak − base) / (430 B × T)`（base 取模型的 2 GB，偏保守时取 0）。
  两者都报告，冻结取较大者。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

__all__ = ["MEM", "MemModel", "calibrate_r_map", "job_resources", "mem_limit_gb"]

GB = 1e9


@dataclass(frozen=True)
class MemModel:
    """`slam.mem{base_gb, bytes_per_point, safety, r_map}`（M27 §11 参数表；MID-360 项按 ADR-215 冻结，Avia 项暂定）。"""

    base_gb: float = 2.0
    bytes_per_point: float = 430.0
    safety: float = 1.5
    r_map: dict = field(default_factory=lambda: {"mid360": 45_000.0, "avia": 80_000.0})
    long_range: tuple = ("avia",)
    long_range_min_gb: float = 20.0
    cpus: dict = field(default_factory=lambda: {"mid360": 4.0, "avia": 4.0})
    status: dict = field(default_factory=lambda: {"mid360": "frozen", "avia": "provisional"})   # 逐型号；冻结项见 adr
    adr: str = "ADR-215"

    def as_dict(self) -> dict:
        return asdict(self)


MEM = MemModel()


def mem_limit_gb(model: str, T_s: float, r_map: float | None = None, mm: MemModel = MEM) -> float:
    """作业内存限额（GB）；长量程型号不低于 20 GB。"""
    if model not in mm.r_map and r_map is None:
        raise KeyError(f"no r_map for lidar model {model!r}")
    r = float(mm.r_map[model] if r_map is None else r_map)
    v = mm.safety * (mm.base_gb + mm.bytes_per_point * r * float(T_s) / GB)
    if model in mm.long_range:
        v = max(v, mm.long_range_min_gb)
    return v


def job_resources(model: str, T_s: float, *, r_map: float | None = None, min_gb: float = 0.0, min_cpus: float = 0.0,
                  mm: MemModel = MEM) -> dict:
    """单个 FAST-LIVO2 容器的 `--memory`（向上取整到 GB）与 `--cpus`。"""
    mem = max(mem_limit_gb(model, T_s, r_map, mm), float(min_gb))
    cpus = max(float(mm.cpus.get(model, 2.0)), float(min_cpus))
    return {"model": model, "flight_s": float(T_s), "r_map": float(mm.r_map.get(model, 0.0) if r_map is None else r_map),
            "mem_model_gb": mem_limit_gb(model, T_s, r_map, mm), "mem_gb": int(np.ceil(mem)), "cpus": cpus}


def calibrate_r_map(t_wall_s, rss_bytes, *, rate: float, play_t0_s: float, play_t1_s: float, flight_s: float | None = None,
                    mm: MemModel = MEM) -> dict:
    """由 RSS 采样标定 r_map（点/s）。`t_wall_s` 与 `play_t0_s/play_t1_s` 为同一墙钟（容器内 `date +%s.%N`）。"""
    t = np.asarray(t_wall_s, np.float64).reshape(-1)
    r = np.asarray(rss_bytes, np.float64).reshape(-1)
    if len(t) != len(r) or len(t) < 3:
        raise ValueError("need >= 3 RSS samples")
    w = (t >= play_t0_s) & (t <= play_t1_s)
    out: dict = {"samples": len(t), "samples_in_play": int(w.sum()), "rate": float(rate), "rss_peak_gb": float(r.max() / GB)}
    T = float(flight_s) if flight_s is not None else float((play_t1_s - play_t0_s) * rate)
    out["flight_s"] = T
    if w.sum() >= 3:
        x = (t[w] - play_t0_s) * rate                                        # 航时（s）
        A = np.stack([x, np.ones_like(x)], 1)
        (k, b), *_ = np.linalg.lstsq(A, r[w], rcond=None)
        out["rss_slope_bytes_per_s"] = float(k)
        out["rss_intercept_gb"] = float(b / GB)
        out["r_map_slope"] = float(k / mm.bytes_per_point)
    if T > 0:
        out["r_map_peak"] = float(max(r.max() - mm.base_gb * GB, 0.0) / (mm.bytes_per_point * T))
        out["r_map_peak_nobase"] = float(r.max() / (mm.bytes_per_point * T))
    cands = [out.get(k) for k in ("r_map_slope", "r_map_peak") if out.get(k) is not None]
    out["r_map_recommended"] = float(max(cands)) if cands else None
    return out
