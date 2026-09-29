"""g04 state model: the seven assertion groups on the contract enums (AWR-12 §4, §5.2; AWR-17 §6.5; D1-AC-13).

The oracle is tools/contracts/state_model_ref.py (port of .cache/research/g04/state_model.py). When M08's
python/awr/sim/core/state_model.py is importable, its admission matrix and packing are checked against the same data.
"""

from __future__ import annotations

import importlib
import itertools
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctlib  # also puts tools/contracts on sys.path
import state_model_ref as S

from awr.contracts import commands, layouts
from awr.contracts.enums import FlightState as FS
from awr.contracts.enums import Native, Owner, PoseSrc

A = S.F_ARMED | S.F_IN_AIR


def test_group1_custom_mode_bijection():
    assert S.custom_mode(S.NAV.AUTO_LOITER) == 0x03040000 and S.custom_mode(S.NAV.ORBIT) == 0x01030000
    assert S.custom_mode(S.NAV.DESCEND) == 0x14040000 and S.custom_mode(S.NAV.OFFBOARD) == 0x00060000
    assert all(S.nav_from_custom_mode(S.custom_mode(n)) == n for n in S.NAV_TO_CM)
    assert len(S.NAV_TO_CM) == 29


def test_group2_derive_px4_total():
    n = 0
    for armed, nav, st, ld, air, lock in itertools.product((False, True), list(S.NAV_TO_CM), (S.MS_UNINIT, S.MS_STANDBY, S.MS_ACTIVE, S.MS_CRITICAL, S.MS_TERMINATION),
                                                           range(5), (False, True), (False, True)):
        fs, s, _ = S.derive_px4(S.Px4Raw(armed, S.custom_mode(nav), st, ld), S.Intent(cmd="goto", airborne_since_arm=air, lock=lock))
        assert isinstance(fs, FS) and 0 <= s < len(S.SUB[fs]) and S.SUB[fs][s] is not None, (armed, nav, st, ld, fs, s)
        if not armed and st != S.MS_TERMINATION:
            assert fs == FS.DISARMED
        n += 1
    assert n == 2 * 29 * 5 * 5 * 2 * 2


def test_group3_derive_prometheus_total():
    for conn, armed, cs, fsafe, odom, air, ac, mm, cmd in itertools.product((False, True), (False, True), range(5), (False, True), (False, True), (False, True),
                                                                           (1, 2, 3, 4, 5), range(9), (None, "goto", "rtl", "takeoff", "orbit")):
        fs, s, _, nat = S.derive_prometheus(S.PromRaw(conn, armed, cs, fsafe, odom, air, ac, mm), S.Intent(cmd=cmd))
        assert 0 <= s < len(S.SUB[fs]) and S.SUB[fs][s] is not None and nat in Native
    fs, s, _, nat = S.derive_prometheus(S.PromRaw(True, True, 2, False, True, True, S.A_CUR_HOVER), S.Intent())
    assert (fs, s, nat) == (FS.FLYING, 0, Native.COMMAND)


def test_group4_mock_emulation_round_trip():
    lossy = set()
    for fs in FS:
        for s, name in enumerate(S.SUB[fs]):
            if name is None or fs == FS.UNKNOWN:
                continue
            g = S.Intent(cmd="land" if fs == FS.LANDING else {1: "goto", 2: "follow_path", 3: "orbit", 4: "velocity", 5: "swarm"}.get(s if fs == FS.FLYING else -1),
                         airborne_since_arm=fs == FS.LANDED, rtl_phase=s if fs == FS.RTL else 7, hold_reason=s if fs == FS.HOLD and s != 0 else None,
                         lock=fs == FS.HOLD and s == 0, supervisor=(fs, s) if fs == FS.CORRECTING else None, killed=fs == FS.DISARMED and s == 2)
            fs2, s2, _ = S.derive_px4(S.mock_emulate_px4(fs, s, g), g)
            if (fs2, s2) != (fs, s):
                lossy.add((fs.name, name, fs2.name, S.SUB[fs2][s2]))
    assert lossy == {("PREFLIGHT", "CHECKING", "DISARMED", "NOT_READY"), ("PREFLIGHT", "COUNTDOWN", "DISARMED", "NOT_READY"),
                     ("ELAND", "CONTROLLED", "LANDING", "DESCEND"), ("ELAND", "NO_POSITION", "LANDING", "DESCEND"), ("FAILSAFE", "DESCENT", "ELAND", "NO_POSITION"),
                     ("CRASHED", "TILT", "DISARMED", "NOT_READY"), ("CRASHED", "COLLISION_WORLD", "DISARMED", "NOT_READY"),
                     ("CRASHED", "COLLISION_UAV", "DISARMED", "NOT_READY"), ("CRASHED", "IMPACT", "DISARMED", "NOT_READY")}


def test_group5_golden_bytes():
    fs, s, _ = S.derive_px4(S.Px4Raw(True, 0x03040000, S.MS_ACTIVE, S.L_AIR), S.Intent(cmd="goto", arrived=True))
    assert layouts.pack_fs(fs, s) == 0x05
    assert S.F_ARMED | S.F_IN_AIR | S.F_LOC_OK | S.F_GCS | S.F_FCU == 0x37
    assert layouts.pack_ctrl(Owner.OPERATOR, False, Native.COMMAND, PoseSrc.TRUTH) == 0x61
    fs, s, fsafe = S.derive_px4(S.Px4Raw(True, 0x03040000, S.MS_CRITICAL, S.L_AIR), S.Intent(cmd="goto"))
    assert layouts.pack_fs(fs, s) == 0xA7 and fsafe
    assert S.F_ARMED | S.F_IN_AIR | S.F_LOC_OK | S.F_FAILSAFE | S.F_FCU | S.F_ALERT == 0xAF
    assert layouts.pack_ctrl(Owner.SAFETY, False, Native.COMMAND, PoseSrc.ESTIMATE) == 0x25
    fs, s, fsafe, nat = S.derive_prometheus(S.PromRaw(True, True, 3, True, False, True, last_error="Odom invalid, swtich to land control mode!"), S.Intent())
    assert layouts.pack_fs(fs, s) == 0x2A and nat == Native.LAND
    assert S.F_ARMED | S.F_IN_AIR | S.F_FAILSAFE | S.F_GCS | S.F_FCU | S.F_ALERT == 0xBB
    assert layouts.pack_ctrl(Owner.SAFETY, False, Native.LAND, PoseSrc.ESTIMATE) == 0x35
    assert layouts.pack_fs(FS.HOLD, 0) == 0x07 and layouts.pack_ctrl(Owner.SAFETY, True, Native.COMMAND, PoseSrc.TRUTH) == 0x6D
    assert layouts.pack_fs(FS.UNKNOWN, 2) == 0x40


def test_group6_full64_bytes_round_trip():
    a = np.zeros(3, layouts.DRONE_STATE64)
    a["flight_state"] = [0x05, 0xA7, 0x2A]
    a["ctrl"] = [0x61, 0x25, 0x35]
    b = np.frombuffer(a.tobytes(), layouts.DRONE_STATE64)
    assert [layouts.unpack_fs(int(x)) for x in b["flight_state"]] == [(5, 0), (7, 5), (10, 1)]
    assert layouts.unpack_ctrl(0x6D) == (Owner.SAFETY, True, Native.COMMAND, PoseSrc.TRUTH)


def test_group7_admission_spot_checks_and_matrix():
    assert S.admit("goto", FS.HOLD, 0, A) == "SAFETY_ACTIVE"
    assert S.admit("goto", FS.RTL, 0, A) is None
    assert S.admit("goto", FS.RTL, 0, A | S.F_FAILSAFE) == "SAFETY_ACTIVE"
    assert S.admit("land", FS.ELAND, 0, A) == "SAFETY_ACTIVE"
    assert S.admit("pause", FS.FLYING, 0, A, has_task=True) == "STATE"
    assert S.admit("goto", FS.LANDING, 0, A) is None
    assert S.admit("hover", FS.TAKING_OFF, 1, A) is None
    for op, row in S.admission_matrix().items():
        assert list(commands.SERVICE_BY_OP[op].admission) == row, op


def test_m08_state_model_if_present():
    try:
        m = importlib.import_module("awr.sim.core.state_model")
    except ImportError:
        pytest.skip("awr.sim.core.state_model (M08) not implemented yet")
    if hasattr(m, "admission_matrix"):
        for op, row in m.admission_matrix().items():
            assert list(commands.SERVICE_BY_OP[op].admission) == list(row), op
    for v in ctlib.load("golden/layouts/vectors.json")["vectors"]:
        if hasattr(m, "pack_fs"):
            assert m.pack_fs(v["state"], v["sub"]) == v["flight_state"], v["name"]
        if hasattr(m, "pack_ctrl") and "ctrl" in v:
            assert m.pack_ctrl(v["owner"], v["locked"], v["native"], v["pose_src"]) == v["ctrl"], v["name"]
