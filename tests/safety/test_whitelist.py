"""M09-AC-001：14×14 白名单与 12 §4.4.4、M09 §6.4.2 的表逐项相等（196 个组合穷举）；准入矩阵与 commands.json 一致；
Mock SafetyStop 黄金向量 `0x07/0x37/0x6D`（g04 §5.5）逐字节命中。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from awr.contracts.enums import FlightState
from awr.sim.core.fuser import ctrl_bytes, flags_bytes, fs_bytes
from awr.sim.core.state_model import admission_matrix
from awr.sim.safety.flight_fsm import RANK, WHITELIST

ROOT = Path(__file__).resolve().parents[2]
FS = FlightState
ABBR = ("UNK", "DIS", "PRE", "RDY", "TKO", "FLY", "COR", "HLD", "RTL", "LND", "ELD", "FSF", "LDD", "CRS")
NAME = {"UNKNOWN": 0, "DISARMED": 1, "PREFLIGHT": 2, "READY": 3, "TAKING_OFF": 4, "FLYING": 5, "CORRECTING": 6,
        "HOLD": 7, "RTL": 8, "LANDING": 9, "ELAND": 10, "FAILSAFE": 11, "LANDED": 12, "CRASHED": 13}


def _prd_table() -> np.ndarray:
    text = (ROOT / "docs/modules/M09-安全与健康PRD.md").read_text(encoding="utf-8")
    sec = text.split("#### 6.4.2", 1)[1].split("####", 1)[0]
    wl = np.zeros((14, 14), bool)
    for ln in sec.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if cells and cells[0] in ABBR and len(cells) == 15:
            i = ABBR.index(cells[0])
            for j, c in enumerate(cells[1:]):
                wl[i, j] = c == "Y"
    return wl


def _doc12_table() -> np.ndarray:
    """12 §4.4.4：目标状态 -> 允许的源状态（"任意"表示全部源；同状态不算转移）。"""
    text = (ROOT / "docs/12-业务逻辑设计说明书.md").read_text(encoding="utf-8")
    sec = text.split("#### 4.4.4", 1)[1].split("####", 1)[0]
    wl = np.zeros((14, 14), bool)
    for ln in sec.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] not in NAME:
            continue
        dst = NAME[cells[0]]
        if cells[1].startswith("任意"):
            srcs = list(range(14))
        else:
            srcs = [NAME[x] for x in re.findall(r"[A-Z_]+", cells[1]) if x in NAME]
        for s in srcs:
            if s != dst:
                wl[s, dst] = True
    return wl


def test_whitelist_equals_prd_and_doc12() -> None:
    prd = _prd_table()
    doc = _doc12_table()
    assert prd.sum() > 40
    mismatch_prd = [(ABBR[i], ABBR[j]) for i in range(14) for j in range(14) if bool(WHITELIST[i, j]) != bool(prd[i, j])]
    mismatch_doc = [(ABBR[i], ABBR[j]) for i in range(14) for j in range(14) if bool(WHITELIST[i, j]) != bool(doc[i, j])]
    assert mismatch_prd == [], mismatch_prd
    assert mismatch_doc == [], mismatch_doc
    assert not WHITELIST.flags.writeable


def test_rank_matches_severity() -> None:
    from awr.contracts.enums import SEVERITY

    for fs, sev in SEVERITY.items():
        assert RANK[int(fs)] == sev
    assert RANK[FS.DISARMED] == -1 and RANK[FS.LANDED] == -1


def test_admission_matrix_equals_commands_json() -> None:
    doc = json.loads((ROOT / "packages/contracts/rt/commands.json").read_text(encoding="utf-8"))
    cols = [c["id"] for c in doc["admission_columns"]]
    mat = admission_matrix()
    for svc in doc["services"]:
        op = svc.get("op")
        if op in mat and isinstance(svc.get("admission"), dict):
            got = dict(zip(cols, mat[op], strict=True))
            for c in cols:
                assert svc["admission"][c] == got[c], (op, c)


def test_safety_stop_golden_vector() -> None:
    """HOLD/SAFETY_STOP、在空中、加锁、租约 OPERATOR：flight_state 0x07、flags 0x37、ctrl 0x6D。"""
    from safelib import UnitRig

    r = UnitRig(n=1)
    S, sb = r.S, r.sb
    r.set(0, int(FS.FLYING), 0)
    r.tick(1)
    r.rt.fsm.propose_operator(0, int(FS.HOLD), 0, "SAF.OP.SAFETY_STOP", lock=True)
    r.tick(1)
    assert r.fs(0) == (int(FS.HOLD), 0) and bool(sb["locked"][0])
    idx = np.array([0], np.int32)
    owner = np.zeros(S.capacity, np.uint8)
    owner[0] = 1  # OPERATOR
    assert int(fs_bytes(S, idx)[0]) == 0x07
    assert int(flags_bytes(S, idx)[0]) == 0x37
    assert int(ctrl_bytes(S, idx, owner)[0]) == 0x6D
