"""回放倍速上限（M12 §6.7.7；FR-046；M12-AC-046）：

speed_max = min(20, 64 MB/s ÷ 每仿真秒字节, 5000 条/s ÷ 每仿真秒事件)：普通录制为 20；570 条/s 的事件风暴录制为
5000 ÷ 570 ≈ 8.77；超过上限的请求被钳制并返回 SPEED_CLAMPED，越出 [0.1, 20] 返回 110；meta.json 记录同一值。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import rechelp

from awr.recorder.config import ReplayCfg
from awr.recorder.mcap_source import speed_max_of

pytestmark = pytest.mark.ext


def test_formula() -> None:
    c = ReplayCfg()
    assert speed_max_of(0.538e6, 5.0, c) == 20.0
    assert speed_max_of(8e6, 5.0, c) == round(64 * 2 ** 20 / 8e6, 3)
    assert speed_max_of(0.5e6, 570.0, c) == round(5000 / 570, 3)


def test_storm_recording_is_clamped(tmp_path: Path) -> None:
    from awr.recorder.synth import synthesize

    runs = tmp_path / "runs"
    out = synthesize(runs / rechelp.RUN2, n=4, sim_s=5, events_per_s=570)
    meta = json.loads((Path(out["dir"]) / "meta.json").read_text())
    side = meta["sidecars"]["000"]
    assert abs(side["events_per_sim_s"] - 570) < 15
    assert 8.5 < side["speed_max"] < 9.1 and meta["speed_max"] == side["speed_max"]
    h, bus = rechelp.host(runs, tmp_path / "shm")
    rep = h.handle("open", {"run": rechelp.RUN2, "segment": 0})
    assert 8.5 < rep["speed_max"] < 9.1
    r = h.handle("speed", {"speed": 20})
    assert r["speed"] == rep["speed_max"] and r["warnings"] == ["SPEED_CLAMPED"]
    assert h.handle("speed", {"speed": 0.05})["code"] == 110
    assert h.handle("speed", {"speed": 5})["warnings"] == []
    h.close()
    bus.close()
