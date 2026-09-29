"""commands.json admission must be generated from AWR-12 §5.2 (BIZ-AC-002): parse the document table and compare."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import ROOT, load

SUP = {"¹": "sub_ready_to_arm", "²": "agl_ge_2m", "³": "pause_active_nav", "⁴": "track_suspended", "⁵": "cause_cleared", "⁶": "escalation_levels_failsafe"}


def _doc_table() -> dict[str, list[tuple[str, str | None]]]:
    text = (ROOT / "docs" / "12-业务逻辑设计说明书.md").read_text(encoding="utf-8")
    sec = text.split("### 5.2 ", 1)[1].split("### 5.3", 1)[0]
    rows = {}
    for line in sec.splitlines():
        if not line.startswith("| ") or line.startswith("| 命令") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        ops = [re.sub(r"（.*?）", "", o).strip() for o in cells[0].split("、")]
        vals = []
        for c in cells[1:]:
            cond = next((v for k, v in SUP.items() if k in c), None)
            vals.append((re.sub("[¹²³⁴⁵⁶]", "", c), cond))
        for op in ops:
            rows[op] = vals
    return rows


def test_doc_table_parsed():
    t = _doc_table()
    assert len(t) == 16 and all(len(v) == 16 for v in t.values())


def test_commands_json_equals_doc_table():
    c = load("rt/commands.json")
    cols = [x["id"] for x in c["admission_columns"]]
    by_op = {s["op"]: s for s in c["services"] if s.get("admission")}
    bad = []
    for op, vals in _doc_table().items():
        s = by_op.get(op)
        if s is None:
            bad.append(f"{op}: missing in commands.json")
            continue
        for col, (sym, cond) in zip(cols, vals, strict=True):
            if s["admission"][col] != sym:
                bad.append(f"{op}/{col}: {s['admission'][col]} != doc {sym}")
            if (s.get("admission_cond") or {}).get(col) != cond:
                bad.append(f"{op}/{col}: condition {(s.get('admission_cond') or {}).get(col)} != doc {cond}")
    assert not bad, bad


def test_admission_golden_matches_generated():
    from awr.contracts import commands
    from awr.contracts.enums import FlightFlags

    for op, fs, failsafe, sym, cond in load("golden/commands/admission.json")["rows"]:
        assert commands.admit_symbol(op, fs, FlightFlags.FAILSAFE if failsafe else 0) == (sym, cond)
