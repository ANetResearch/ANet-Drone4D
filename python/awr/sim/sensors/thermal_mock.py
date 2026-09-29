"""Mock 热成像帧纯函数（M13-FR-044；M13 §6.5.9；D1-ext）。

`render_thermal_frame(params) -> bytes`：PGM P5，160×120，u8，共 19215 B（≥ 1024 B，满足 TSIR `ARTIFACT("thermal/**",
min_size = 1024)`）。内容：背景温度（大气混合 `tau·t_bg + (1 − tau)·t_env`）、目标热斑（二维高斯，sigma = size_px/2，像素中心 0.5）、
NETD 噪声（计数器 RNG：seed、流 4、通道 2^20 + 像素下标）；量化 `round((T − (t_bg − 5))/30·255)` 钳到 [0, 255]。
参数随 `sensor.detect` 事件的 `artifact.params` 下发（seed 为十进制字符串），任何进程可逐字节重建；本模块无 I/O、只依赖 numpy，
sim-core 不落盘（产物形态由 M14 决定）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from . import cbrng

__all__ = ["FRAME_H", "FRAME_W", "HEADER", "background_temps", "render_thermal_frame"]

FRAME_W = 160
FRAME_H = 120
HEADER = b"P5\n160 120\n255\n"
PIXEL_CH0 = cbrng.CHANNELS[(4, "thermal_pixel")][0]
SPAN_C = 30.0

# 类别 -> (背景相对环境温度、目标温度或相对量)（M13 设定：落水人员体表 34 °C，水面比气温低 3 °C）
_BIAS = {"person": (-3.0, ("abs", 34.0)), "vessel": (-3.0, ("rel", 15.0)), "vehicle": (2.0, ("rel", 25.0)),
         "generic": (0.0, ("rel", 10.0))}


def background_temps(kind: str, t_env_c: float) -> tuple[float, float]:
    bg, (how, v) = _BIAS.get(kind, _BIAS["generic"])
    return float(t_env_c) + bg, (v if how == "abs" else float(t_env_c) + v)


def render_thermal_frame(p: Mapping[str, Any] | Any) -> bytes:
    """params：w、h（须为 160、120）、u、v（目标像素，已缩放到 160×120）、size_px、t_bg_c、t_tgt_c、t_env_c（缺省 t_bg_c）、
    tau、netd_k、seed（u64，整数或十进制字符串）。"""
    g = (lambda k, d=None: p.get(k, d)) if isinstance(p, Mapping) else (lambda k, d=None: getattr(p, k, d))
    w, h = int(g("w", FRAME_W)), int(g("h", FRAME_H))
    if (w, h) != (FRAME_W, FRAME_H):
        raise ValueError("thermal frame is fixed at 160x120")
    u, v = float(g("u")), float(g("v"))
    size = max(1.0, float(g("size_px", 1.0)))
    t_bg = float(g("t_bg_c"))
    t_tgt = float(g("t_tgt_c"))
    t_env = float(g("t_env_c", t_bg))
    tau = float(g("tau", 1.0))
    netd = float(g("netd_k", 0.05))
    seed = int(str(g("seed", "0")))
    x = np.arange(w, dtype=np.float64) + 0.5
    y = np.arange(h, dtype=np.float64) + 0.5
    s2 = 2.0 * (size / 2.0) ** 2
    gx = np.exp(-((x - u) ** 2) / s2)
    gy = np.exp(-((y - v) ** 2) / s2)
    T = np.full((h, w), tau * t_bg + (1.0 - tau) * t_env)
    T += tau * (t_tgt - t_bg) * (gy[:, None] * gx[None, :])
    T += netd * cbrng.normal_elem(seed, 4, 0, 0, PIXEL_CH0 + np.arange(h * w, dtype=np.int64)).reshape(h, w)
    q = np.floor((T - (t_bg - 5.0)) / SPAN_C * 255.0 + 0.5)
    img = np.clip(q, 0, 255).astype(np.uint8)
    return HEADER + img.tobytes()
