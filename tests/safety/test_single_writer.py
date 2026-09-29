"""代码审查用例（M09 §9.5；FR-023、FR-006）：候选到转移只允许在 `flight_fsm.FlightFSM` 中发生，任何其他模块直接写
safety 块的 `fs`、`sub` 都视为缺陷；M09 代码不读写 FleetState 的 NED/FRD 数组（AWR-03 §5.3 第 3 条），不直接调用墙钟。"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "python" / "awr" / "sim" / "safety"
FS_WRITE = re.compile(r"""\[\s*["'](fs|sub)["']\s*\]\s*\[[^\]]*\]\s*(=|\|=|&=)(?!=)""")
NED = re.compile(r"\bS\.(p|v|q|q_sp|omega|pos_ref|home|tr_x|tr_v|tr_a|target|vel_cmd|wind|p_prev|a_meas)\[")
CLOCK = re.compile(r"\btime\.(time|monotonic|perf_counter)(_ns)?\(|datetime\.now\(")


def test_only_fsm_writes_flight_state() -> None:
    bad = []
    for p in sorted(PKG.glob("*.py")):
        for k, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if FS_WRITE.search(ln) and p.name != "flight_fsm.py":
                bad.append(f"{p.name}:{k}: {ln.strip()}")
    assert bad == [], bad


def test_no_ned_arrays_and_no_wall_clock() -> None:
    bad = []
    for p in sorted(PKG.glob("*.py")):
        for k, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = ln.split("#", 1)[0]
            if NED.search(code) or CLOCK.search(code):
                bad.append(f"{p.name}:{k}: {ln.strip()}")
    assert bad == [], bad
