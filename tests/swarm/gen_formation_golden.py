"""生成 TS 对拍用的编队槽位与 CAPT golden（M10-FR-042；M10-AC-018；AWR-18 §8.3）。

输出 `apps/web/tests/mission/golden/formation.json`：六种队形 × 若干规模与间距的槽位（虚拟锚点），以及随机起终点的
CAPT 分配与代价（scipy `linear_sum_assignment`）。用法：`.venv/bin/python tests/swarm/gen_formation_golden.py`。
`tests/swarm/test_golden.py` 校验文件与当前 Python 实现一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from awr.swarm.formation import SHAPES, capt_assign, formation_slots

OUT = Path(__file__).resolve().parents[2] / "apps" / "web" / "tests" / "mission" / "golden" / "formation.json"


def build() -> dict:
    slots = []
    for shape in SHAPES:
        for n, s, ha, cols in ((2, 12.0, 35.0, None), (5, 12.0, 35.0, None), (7, 6.0, 35.0, None), (9, 6.0, 30.0, 3),
                               (10, 8.0, 40.0, 4)):
            off = formation_slots(shape, n, s, ha, cols)
            slots.append({"shape": shape, "n": n, "spacing_m": s, "half_angle_deg": ha, "cols": cols,
                          "offsets": off.round(12).ravel().tolist()})
    rng = np.random.default_rng(20260929)
    capt = []
    for n in (2, 3, 5, 9, 16, 50):
        for _ in range(3):
            P = rng.uniform(-100, 100, (n, 3))
            G = rng.uniform(-100, 100, (n, 3))
            a = capt_assign(P, G)
            cost = float(((P - G[a]) ** 2).sum())
            capt.append({"P": P.round(9).ravel().tolist(), "G": G.round(9).ravel().tolist(), "assign": a.tolist(),
                         "cost": cost})
    return {"schema": "awr.test.formation_golden.v1", "slots": slots, "capt": capt}


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), separators=(",", ":")) + "\n", encoding="utf-8")
    print(OUT)
