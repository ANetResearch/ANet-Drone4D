"""确定性（M18-AC-006，P1；M18-FR-007、NFR-003；ADR-049；D2-AC-32）：同一配置与种子两次运行，识别物轨迹（位置、航向的位模式）
与事件序列逐字节一致；改变订阅（detail 发布与否）与观察者（多一个不进入任何敏感体的机体）不改变结果；换种子则轨迹不同。
台架运行 5 min【仿真】（3000 次 stage）；1 h 版本属 MS5 的批处理确定性用例（M16）。"""

from __future__ import annotations

import hashlib

import numpy as np
from tlib import Bench, FakeWorld

REGION = [[-150.0, -150.0], [150.0, -150.0], [150.0, 150.0], [-150.0, 150.0]]
W = FakeWorld(boxes=[(30.0, 40.0, -20.0, 20.0, 8.0)])


def _run(seed: int, *, detail: bool, extra_observer: bool, minutes: float = 5.0) -> tuple[str, list]:
    with Bench(3 if extra_observer else 2, W, seed=seed) as b:
        b.add({"class": "person", "pos": [0.0, 0.0], "motion": {"mode": "random_walk", "region": REGION}})
        b.add({"class": "dog", "pos": [-50.0, 40.0], "motion": {"mode": "random_walk", "region": REGION}})
        b.add({"class": "vehicle", "pos": [-100.0, -100.0],
               "motion": {"mode": "path", "path": [[-100, -100, None], [100, -100, None], [100, 100, None]], "loop": "pingpong"}})
        b.S.fly(0, [0.0, 0.0, 60.0])
        b.S.fly(1, [400.0, 0.0, 60.0])
        if extra_observer:
            b.S.fly(2, [1900.0, 1900.0, 60.0])
        h = hashlib.sha256()
        steps = round(minutes * 600)
        for k in range(steps):
            # 两架机按固定脚本往返进出敏感体
            b.S.pos[0] = [250.0 * np.sin(k * 0.002), 30.0, 60.0]
            b.S.pos[1] = [400.0 - 0.3 * (k % 2000), -20.0, 45.0]
            b.step(1)
            if detail and k % 5 == 0:
                b.rt.publish_detail(b.ctx)
            for T in b.rt.ordered():
                h.update(np.asarray(T.ms.pos, np.float64).tobytes())
                h.update(np.float64(T.ms.yaw).tobytes())
                h.update(bytes([T.aware]))
        ev = [(e["kind"], e["t_sim_ns"], tuple(sorted((k, str(v)) for k, v in e["data"].items()))) for e in b.events.items
              if e["kind"].startswith("target.")]
        return h.hexdigest(), ev


def test_same_seed_same_bytes_and_events() -> None:
    a = _run(7, detail=False, extra_observer=False)
    b = _run(7, detail=True, extra_observer=True)
    assert a[0] == b[0]
    assert a[1] == b[1] and len(a[1]) > 4
    c = _run(8, detail=False, extra_observer=False, minutes=2.0)
    d = _run(7, detail=False, extra_observer=False, minutes=2.0)
    assert c[0] != d[0]
