"""禁入体（M18-AC-014；M18-FR-016；AWR-04 §8.1、§9.4；契约 target/keepout.schema.json）：

- geometric：启用的视觉、声学配置体外扩 margin；radar、rf 与禁用体不产生禁入体；
- 运动识别物（mode ≠ static 或速度 > 0.1 m/s）的扇形一律按整圆；静止识别物的扇形保留方向；
- physical 保守体 ≤ 配置体（同 margin）；
- 线上形状通过契约；Python 与 TS（`apps/web/src/engine/targets/geometry.ts`）对拍夹具 `apps/web/tests/m18/keepout_cases.json`
  由本测试生成并核对（`AWR_REGEN=1` 时重写）。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest
from ctlib import errors

from awr.sim.targets import DEFAULT_MARGIN_M, P600, TargetConfig, TargetState1, keepout_volumes
from awr.sim.targets.envview import day_night

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "apps" / "web" / "tests" / "m18" / "keepout_cases.json"


def _state(pos=(10.0, 20.0, 5.0), yaw=0.5, speed=0.0, moving=False) -> TargetState1:
    return TargetState1("t", 0, pos, yaw, (0.0, 0.0, 0.0), speed, moving, "UNAWARE", 0.5)


def test_geometric_margin_and_sector() -> None:
    c = TargetConfig.from_dict({"class": "person", "pos": [0, 0], "motion": {"mode": "static"}}, "person-01")
    vols = keepout_volumes(c, _state(), "geometric", 20.0)
    assert [v.shape for v in vols] == ["sector", "hemisphere"]
    sec, hem = vols
    assert sec.r_m == pytest.approx(320.0) and sec.z_min_m == pytest.approx(5.0 - 20.0) and sec.z_max_m == pytest.approx(5.0 + 320.0)
    assert sec.half_angle_rad > math.radians(60.0) and sec.yaw_rad == pytest.approx(0.5)
    assert hem.r_m == pytest.approx(320.0) and hem.z_min_m == pytest.approx(5.0)
    for v in vols:
        assert errors("target/keepout.schema.json", v.to_wire(f"person-01/{v.vol_idx}")) == []


def test_moving_target_full_circle() -> None:
    walk = TargetConfig.from_dict({"class": "person", "pos": [0, 0]}, "p")       # random_walk 模板
    vols = keepout_volumes(walk, _state(), "geometric", DEFAULT_MARGIN_M)
    assert vols[0].shape == "cylinder"
    still = TargetConfig.from_dict({"class": "vehicle", "pos": [0, 0]}, "v")     # static 模板，但当前在动
    assert keepout_volumes(still, _state(speed=3.0), "geometric", 0.0)[0].shape == "cylinder"
    assert keepout_volumes(still, _state(speed=3.0), "geometric", 0.0, moving_as_circle=False)[0].shape == "sector"


def test_physical_conservative_within_configured() -> None:
    for cls in ("person", "dog", "vehicle", "uav"):
        c = TargetConfig.from_dict({"class": cls, "pos": [0, 0], "criterion_mode": "physical"}, cls)
        geo = keepout_volumes(c, _state(), "geometric", 20.0)
        phy = keepout_volumes(c, _state(), "physical", 20.0, models=[P600], env=day_night("rural", 10000.0)[1])
        assert len(geo) == len(phy)
        for g, p in zip(geo, phy, strict=True):
            assert p.r_m <= g.r_m + 1e-9 and p.r_m >= 20.0


def test_radar_rf_and_disabled_skipped() -> None:
    c = TargetConfig.from_dict({"class": "object", "pos": [0, 0], "sensitivity": [
        {"kind": "radar", "shape": "sphere", "radius_max_m": 50}, {"kind": "rf", "shape": "sphere", "radius_max_m": 50},
        {"kind": "visual", "shape": "sphere", "radius_max_m": 50, "enabled": False}]}, "o")
    assert keepout_volumes(c, _state(), "geometric", 20.0) == []


def _cases() -> list[dict]:
    out = []
    specs = [("person", {"motion": {"mode": "static"}}, 0.0, 20.06), ("person", {}, 0.0, 20.06), ("dog", {"motion": {"mode": "static"}}, 1.2, 5.0),
             ("vehicle", {}, 3.0, 0.0), ("uav", {}, 0.0, 25.0),
             ("object", {"sensitivity": [{"kind": "visual", "shape": "cylinder", "radius_max_m": 40, "z_min_m": -5, "z_max_m": 60}]}, 0.0, 10.0)]
    for cls, extra, speed, margin in specs:
        c = TargetConfig.from_dict({"class": cls, "pos": [0, 0], **extra}, cls)
        st = _state(pos=(12.5, -7.0, 3.0), yaw=-2.0, speed=speed)
        vols = keepout_volumes(c, st, "geometric", margin)
        out.append({"cfg": {"class": cls, "motion": c.raw["motion"], "sensitivity": c.raw["sensitivity"]}, "pos": list(st.pos),
                    "yaw_rad": st.yaw_rad, "speed_mps": speed, "margin_m": margin,
                    "volumes": [v.to_wire(f"{cls}/{v.vol_idx}") for v in vols]})
    return out


def test_ts_fixture_up_to_date() -> None:
    cases = _cases()
    text = json.dumps({"schema": "awr.test.m18.keepout_cases.v1", "source": "tests/targets/test_keepout.py", "cases": cases},
                      ensure_ascii=False, indent=1) + "\n"
    if os.environ.get("AWR_REGEN") == "1" or not CASES.exists():
        CASES.parent.mkdir(parents=True, exist_ok=True)
        CASES.write_text(text, encoding="utf-8")
    assert json.loads(CASES.read_text(encoding="utf-8"))["cases"] == json.loads(text)["cases"]
