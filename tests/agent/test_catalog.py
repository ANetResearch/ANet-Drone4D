"""M14-AC-007：能力 id 语法与目录（20 正例、20 反例）；目录 24 条。"""

from __future__ import annotations

import pytest

from awr.agent.capabilities.catalog import catalog
from awr.agent.runtime.provider import CapabilityError, valid_capability_id

POS = ["thermal.imaging", "rgb.zoom", "relay.communication", "lidar.mapping", "lidar.scan", "rgb.capture", "flight.takeoff",
       "flight.land", "flight.goto", "flight.hover", "flight.orbit", "flight.rtl", "flight.follow_path", "mission.coverage",
       "mission.search", "mission.insert_leg", "mission.abort", "agent.describe", "agent.state", "thermal.imaging.hd"]
NEG = ["Thermal.imaging", "thermal.Imaging", "thermal.imaging@uav/p600-02", "thermal-imaging.x", "thermal.imag-ing",
       "thermal", "a.b.c.d", "thermal." + "x" * 60, "1thermal.imaging", "thermal.1imaging", ".thermal.imaging",
       "thermal..imaging", "thermal.imaging.", "unknown.family", "sonar.scan", "thermal imaging", "thermal/imaging",
       "", "thermal.imaging\n", "\uff34\uff28\uff25\uff32\uff2d\uff21\uff2c.imaging"]


def test_positive_ids() -> None:
    fam = catalog().families
    assert len(POS) == 20
    for c in POS:
        assert valid_capability_id(c, fam), c


def test_negative_ids() -> None:
    fam = catalog().families
    assert len(NEG) == 20
    for c in NEG:
        assert not valid_capability_id(c, fam), c


def test_catalog_24_entries_and_entry_471() -> None:
    cat = catalog()
    assert len(cat.entries) == 24
    assert cat.served("thermal.imaging") and cat.served("rgb.zoom")
    assert cat.get("lidar.mapping")["d1"] == "future"
    assert "orbit" in cat.ops("thermal.imaging")
    with pytest.raises(CapabilityError):
        cat.entry("thermal.imaging.hd")  # 语法合法但不在目录
    with pytest.raises(CapabilityError):
        cat.entry("sonar.scan")
    assert cat.default_accept("thermal.imaging")["op"] == 1
    assert cat.default_accept("rgb.zoom")["children"][1]["artifact"]["path_glob"] == "rgb/**"
