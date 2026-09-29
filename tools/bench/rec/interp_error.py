"""M12 插值误差 oracle（M12 §5.3、§6.4 误差表、M12-AC-010；迁自 `.cache/research/m12/interp_error.py`）。

对无人机典型运动（15 m/s、r = 30 m 转弯；12 m/s、a = 3 m/s² 梯形 goto）按间隔 h 采样（速度按 Lite32 量化到 1 cm/s），
以 1 ms 真值比较三次 Hermite（端点速度为切线）、线性插值与"按速度外推 ≤ h"的最大位置误差。

用法：
  python tools/bench/rec/interp_error.py                 # 打印误差表（§6.4）
  python tools/bench/rec/interp_error.py --golden OUT    # 写 TS 对拍 golden（apps/web/tests/time/fixtures/hermite_golden.json）
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def circle(t: np.ndarray, v: float = 15.0, r: float = 30.0) -> tuple[np.ndarray, np.ndarray]:
    w = v / r
    return (np.stack([r * np.cos(w * t), r * np.sin(w * t)], -1),
            np.stack([-v * np.sin(w * t), v * np.cos(w * t)], -1))


def goto(t: np.ndarray, vmax: float = 12.0, amax: float = 3.0, d: float = 400.0) -> tuple[np.ndarray, np.ndarray]:
    ta = vmax / amax
    da = 0.5 * amax * ta ** 2
    tc = (d - 2 * da) / vmax
    T = 2 * ta + tc
    t = np.clip(t, 0, T)
    x = np.where(t < ta, 0.5 * amax * t ** 2, np.where(t < ta + tc, da + vmax * (t - ta), d - 0.5 * amax * (T - t) ** 2))
    v = np.where(t < ta, amax * t, np.where(t < ta + tc, vmax, amax * (T - t)))
    return np.stack([x, 0 * x], -1), np.stack([v, 0 * v], -1)


def hermite(P: np.ndarray, V: np.ndarray, k: np.ndarray, h: float, s: np.ndarray) -> np.ndarray:
    s = s[:, None]
    h00, h10, h01, h11 = 2 * s ** 3 - 3 * s ** 2 + 1, s ** 3 - 2 * s ** 2 + s, -2 * s ** 3 + 3 * s ** 2, s ** 3 - s ** 2
    return h00 * P[k] + h10 * h * V[k] + h01 * P[k + 1] + h11 * h * V[k + 1]


def run(fn, h: float, T: float = 60.0, quant_v: float = 0.01) -> tuple[float, float, float]:
    ts = np.arange(0, T, h)
    P, V = fn(ts)
    V = np.round(V / quant_v) * quant_v
    tq = np.arange(h, T - h, 0.001)
    Pt, _ = fn(tq)
    k = np.clip(np.searchsorted(ts, tq, "right") - 1, 0, len(ts) - 2)
    s = (tq - ts[k]) / h
    Ph = hermite(P, V, k, h, s)
    Pl = (1 - s)[:, None] * P[k] + s[:, None] * P[k + 1]
    Pe = P[k] + V[k] * (tq - ts[k])[:, None]

    def e(X: np.ndarray) -> float:
        return float(np.linalg.norm(X - Pt, axis=-1).max())

    return e(Ph), e(Pl), e(Pe)


def golden(out: Path, h: float = 0.1, n_samples: int = 40, n_query: int = 60) -> None:
    cases = []
    for name, fn in (("turn", circle), ("goto", goto)):
        ts = np.arange(n_samples) * h + 1.0
        P, V = fn(ts)
        V = np.round(V / 0.01) * 0.01
        tq = np.linspace(ts[1], ts[-2], n_query)
        k = np.clip(np.searchsorted(ts, tq, "right") - 1, 0, len(ts) - 2)
        s = (tq - ts[k]) / h
        Ph = hermite(P, V, k, h, s)
        Pt, _ = fn(tq)
        cases.append({"name": name, "h": h, "t": ts.tolist(), "p": P.tolist(), "v": V.tolist(), "tq": tq.tolist(),
                      "hermite": Ph.tolist(), "truth": Pt.tolist()})
    out.write_text(json.dumps({"schema": "awr.golden.hermite.v1", "generator": "tools/bench/rec/interp_error.py",
                               "quant_v_mps": 0.01, "cases": cases}, separators=(",", ":")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--golden", type=Path, default=None)
    a = ap.parse_args()
    if a.golden is not None:
        golden(a.golden)
        print(f"wrote {a.golden}")
        return
    for name, fn in (("turn 15 m/s r 30 m", circle), ("goto 12 m/s a 3 m/s2", goto)):
        for h in (0.008, 0.04, 0.1, 0.2, 1.0, 2.0):
            eh, el, ee = run(fn, h)
            print(f"{name:22s} h={h:5.3f}s  max err: hermite {eh * 1000:9.3f} mm  linear {el * 1000:10.3f} mm  "
                  f"v-extrap(<=h) {ee * 1000:10.3f} mm")


if __name__ == "__main__":
    main()
