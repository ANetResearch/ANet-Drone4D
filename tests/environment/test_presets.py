"""预设（M07-AC-003；M07-FR-002、FR-008）：12 预设回算总 MOR、sigma_bg 为正、帧内哈希、补丁校验与错误码。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from envfix import _mgr

from awr.contracts.presets import PRESETS_SHA256
from awr.environment.field import world_config
from awr.environment.weather.derive import K_MOR, derive
from awr.environment.weather.presets import BASE, DIR, TOP, EnvError, presets

MOR_TOTAL = {"clear": 30000, "partlyCloudy": 20000, "overcast": 12000, "lightRain": 6000, "rain": 3000, "heavyRain": 1200, "thunderstorm": 800,
             "fog": 150, "haze": 3000, "snow": 1500, "blizzard": 150, "sandstorm": 400}


@pytest.mark.parametrize("pid", sorted(MOR_TOTAL))
def test_preset_mor_within_0_1_m(pid: str):
    d = derive(presets().preset_vector(pid))
    assert abs(d.mor_m - MOR_TOTAL[pid]) <= 0.1, d.mor_m
    assert d.sigma_bg > 0


def test_k_mor_is_ln20():
    assert math.log(20.0) == K_MOR


def test_frame_carries_presets_sha():
    m = _mgr("clear")
    assert m.kf.to_wire()["config"]["presets_sha256"] == PRESETS_SHA256 == world_config(None)["presets_sha256"]


def test_overlay_keeps_user_axes():
    P = presets()
    base = P.default_vector()
    base[DIR] = 123.0
    v = P.overlay(base, "thunderstorm")
    assert v[DIR] == 123.0
    assert v[TOP] >= v[BASE] + 100


def test_routes_and_ids():
    P = presets()
    assert len(P.ids) == 12
    assert P.route("clear", "thunderstorm") == ["partlyCloudy", "overcast", "rain", "heavyRain"]
    assert P.route("rain", "fog") == []
    assert P.route(None, "fog") == []


@pytest.mark.parametrize("patch,code", [
    ({"wind": {"speed_ref_mps": 41}}, 110),
    ({"wind": {"speed_ref_mps": float("nan")}}, 110),
    ({"wind": {"speed_ref_mps": float("inf")}}, 110),
    ({"wind": {"speed_mps": 3}}, 441),
    ({"bogus": 3}, 441),
    ({"wind": {"speed_ref_mps": "3"}}, 300),
    ({"config": {"wind": {"level": 2}}}, 442),
    ({"config": {"wind": {"turbulence": {"model": "lbm"}}}}, 110),
    ({"config": {"bogus": 1}}, 441),
    ({}, 300),
])
def test_validate_patch_codes(patch: dict, code: int):
    with pytest.raises(EnvError) as ei:
        presets().validate_patch(patch)
    assert ei.value.code == code


def test_validate_patch_ok_and_arc_360():
    items, cfg = presets().validate_patch({"wind": {"dir_from_deg": 360, "speed_ref_mps": 8}, "config": {"wind": {"level": 0}}})
    assert dict(items) == {1: 0.0, 0: 8.0}
    assert cfg == {"wind": {"level": 0}}


def test_check_state_top_base():
    P = presets()
    s = P.default_vector()
    s[TOP] = s[BASE] + 50
    with pytest.raises(EnvError) as ei:
        P.check_state(s)
    assert ei.value.code == 110


def test_matches():
    P = presets()
    v = P.preset_vector("fog")
    assert P.matches(v, "fog") and not P.matches(v, "clear")
    v2 = v.copy()
    v2[DIR] = 12.0
    assert P.matches(v2, "fog")
    assert np.isnan(P.snapshot("fog")[DIR])
