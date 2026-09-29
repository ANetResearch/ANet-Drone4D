"""M14-AC-006：Registry（精确优先、父级回退、冲突拒绝、d05 resolvetest 两例、`@` 拒绝 471）。"""

from __future__ import annotations

import pytest

from awr.agent.runtime.provider import CapabilityError, Registry


def test_exact_then_parent_fallback() -> None:
    r = Registry()
    r.register("drone", ["thermal.imaging", "flight"])
    assert r.resolve("thermal.imaging") == "drone"
    assert r.resolve("thermal.imaging.hd") == "drone"  # 回退到 thermal.imaging
    assert r.resolve("flight.goto") == "drone"  # 回退到 family
    assert r.resolve("flightx.goto") is None
    assert r.resolve("sensor.thermal") is None
    assert r.resolve_entry("flight.goto") == "flight"


def test_exact_wins_over_parent() -> None:
    r = Registry()
    r.register("a", ["flight"])
    r.register("b", ["flight.goto"])
    assert r.resolve("flight.goto") == "b"
    assert r.resolve("flight.orbit") == "a"


def test_conflict_rejected() -> None:
    r = Registry()
    r.register("drone", ["thermal.imaging"])
    with pytest.raises(ValueError):
        r.register("dup", ["thermal.imaging"])


def test_d05_resolvetest_cases() -> None:
    """d05 resolvetest：`flight.goto@uav/p600-02` 回退到 flight；`thermal.imaging@uav/p600-02` 解析失败。"""
    r = Registry()
    r.register("drone", ["flight"])
    assert r.resolve("flight.goto@uav/p600-02") == "drone"
    assert r.resolve("thermal.imaging@uav/p600-02") is None


def test_at_suffix_registration_rejected() -> None:
    r = Registry()
    with pytest.raises(CapabilityError) as ei:
        r.register("gw", ["thermal.imaging@uav/p600-03"])
    assert ei.value.code == 471


def test_ids_listing_and_unregister() -> None:
    from awr.agent.runtime.provider import check_capability_id

    assert check_capability_id("thermal.imaging", ["thermal"]) == "thermal.imaging"
    with pytest.raises(CapabilityError):
        check_capability_id("thermal.imaging", ["rgb"])
    r = Registry()
    r.register("a", ["thermal.imaging", "rgb"])
    assert r.listed() == ["rgb", "thermal.imaging"]
    r.unregister("a")
    assert r.resolve("thermal.imaging") is None and r.resolve_entry("rgb.zoom") is None
