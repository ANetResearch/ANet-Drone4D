"""F13 夹具生成器：以 `awr.phys.tier_compare.CaseRun.save`（`np.savez`，ZIP 存储方式 + force_zip64）写出两档轨迹 `.npz`，
并以 `compare_case` 同式（numpy）计算每机位置 RMS 与若干逐样本位置差，供 apps/web/tests/lab/tierCompare.test.ts 逐值对拍。

用法（仓库根目录）：
  PYTHONPATH=python .venv-d3/bin/python apps/web/tests/lab/fixtures/tier_compare/gen.py
另写一份 `np.savez_compressed` 的 `.npz`（ZIP 压缩方式 8）用于解压路径。输出确定（固定输入，无随机数）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from awr.phys.tier_compare import CaseRun

OUT = Path(__file__).resolve().parent
STEP_NS = 4_000_000                                   # 250 Hz
N = 101


def state(n: int, phase: float, off: tuple[float, float, float]) -> np.ndarray:
    k = np.arange(n, dtype=np.float64)
    s = np.zeros((n, 16), np.float64)
    s[:, 0] = 10.0 * np.cos(0.05 * k + phase) + off[0]
    s[:, 1] = 10.0 * np.sin(0.05 * k + phase) + off[1]
    s[:, 2] = 5.0 + 0.01 * k + off[2]
    s[:, 6] = 1.0                                     # qw
    s[:, 7] = -0.5 * np.sin(0.05 * k + phase)
    s[:, 15] = 9.80665
    return s


def run(case: str, tier: str, uavs: dict[str, np.ndarray]) -> CaseRun:
    t_ns = np.arange(N, dtype=np.int64) * STEP_NS
    return CaseRun(case, tier, t_ns, uavs, {u: ["AIRBORNE"] for u in uavs}, [], [], {}, 0.0)


def main() -> None:
    k = np.arange(N, dtype=np.float64)
    fa = {"u1": state(N, 0.0, (0.0, 0.0, 0.0))}
    re_ = {"u1": state(N, 0.0, (0.02, -0.01, 0.0)) + np.outer(0.001 * k, np.r_[1.0, 0.5, -0.25, np.zeros(13)])}
    cases = {"hover": (fa, re_),
             "contact_crash": ({"wall": state(N, 0.3, (30.0, -40.0, 0.0)), "drop": state(N, 1.1, (-40.0, 40.0, 0.0))},
                               {"wall": state(N, 0.3, (30.05, -39.9, 0.1)), "drop": state(N, 1.1, (-40.1, 40.13, -0.06))})}
    expect: dict = {"n": N, "step_ns": STEP_NS, "cases": {}}
    for cid, (a, b) in cases.items():
        run(cid, "fast", a).save(OUT)
        run(cid, "realistic", b).save(OUT)
        (OUT / f"{cid}.fast.json").unlink()
        (OUT / f"{cid}.realistic.json").unlink()
        pos_rms = {u: float(np.sqrt(np.mean(np.sum((a[u][:, 0:3] - b[u][:, 0:3]) ** 2, axis=1)))) for u in a}
        d = {u: np.linalg.norm(a[u][:, 0:3] - b[u][:, 0:3], axis=1) for u in a}
        expect["cases"][cid] = {"uavs": list(a), "pos_rms_m": pos_rms,
                                "pos_diff_m": {u: {str(i): float(d[u][i]) for i in (0, 37, N - 1)} for u in a},
                                "row37": {u: [float(x) for x in a[u][37]] for u in a}}
    np.savez_compressed(OUT / "hover.fast.deflate.npz", t_ns=np.arange(N, dtype=np.int64) * STEP_NS, s_u1=fa["u1"])
    (OUT / "expect.json").write_text(json.dumps(expect, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
