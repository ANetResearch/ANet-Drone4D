"""D3 原因码 600–699 与 Agent WS 关闭码（AWR-05 §8.4、AWR-17 §18.12；WP-D3-01）：全表登记、子段归属、保留码位、名称全局唯一，
D2 前端的原因码文案不包含 D3 码。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from awr.contracts import reasons as R

ROOT = Path(__file__).resolve().parents[2]
DOC = json.loads((ROOT / "packages/contracts/rt/reasons.json").read_text(encoding="utf-8"))
SUBRANGES = {"world": (600, 609), "event": (610, 629), "physics": (630, 639), "sensor": (640, 659), "agent": (660, 679),
             "bench": (680, 699)}
RESERVED = {*range(606, 611), *range(622, 630), *range(653, 661), *range(668, 680), 699}


def _d3() -> list[dict]:
    return [r for r in DOC["reasons"] if 600 <= r["code"] <= 699]


def test_d3_codes_match_awr17_full_table() -> None:
    doc17 = next((ROOT / "docs").glob("17-*.md")).read_text(encoding="utf-8")
    seg = doc17[doc17.index("#### 18.12.2"):doc17.index("#### 18.12.3")]
    table = {int(c): (n, None if h == "null" else int(h)) for c, n, h in re.findall(r"^\| (\d{3}) \| ([A-Z_0-9]+) \| (null|\d{3}) \|", seg, re.M)}
    got = {r["code"]: (r["name"], r["http"]) for r in _d3()}
    assert got == table and len(got) == 66
    assert set(range(600, 700)) - set(got) == RESERVED


def test_d3_codes_sit_in_their_subrange_and_are_v03() -> None:
    ranges = {(g["from"], g["to"]): g["category"] for g in DOC["ranges"] if g["from"] >= 600}
    assert {v: k for k, v in ranges.items()} == SUBRANGES
    for r in _d3():
        lo, hi = SUBRANGES[r["category"]]
        assert lo <= r["code"] <= hi and r["d1"] == "V0.3", r
        assert "`" not in r["message_zh"] + r["remedy_zh"] and "detail" not in r["message_zh"], r


def test_names_are_globally_unique() -> None:
    names = [r["name"] for r in DOC["reasons"]]
    assert len(names) == len(set(names))
    other = set(DOC.get("detail_only", [])) | set(DOC.get("warnings", [])) | {w["name"] for w in DOC["ws_close_codes"]} - {"AGENT_PROTOCOL_ERROR"}
    assert not {r["name"] for r in _d3()} & other  # 4462 shares its name with reason 666 on purpose
    assert R.Reason.BENCH_EXPORT_FAILED == 689 and R.Reason.EXPORT_FAILED == 356


def test_agent_ws_close_codes() -> None:
    ws = {w["code"]: w["name"] for w in DOC["ws_close_codes"]}
    assert {c: ws.get(c) for c in (4460, 4462, 4463, 4464)} == {4460: "RUN_ENDED", 4462: "AGENT_PROTOCOL_ERROR",
                                                                4463: "AGENT_SUPERSEDED", 4464: "AGENT_NOT_ACCEPTED"}
    assert 4461 not in ws


def test_d2_front_end_texts_exclude_d3_codes() -> None:
    texts = json.loads((ROOT / "apps/web/src/app/i18n/reasons.zh-CN.gen.json").read_text(encoding="utf-8"))
    assert not [k for k in texts if re.match(r"reason\.6\d\d\.", k)]
    assert "reason.586.short" in texts


def test_d3_bus_event_kinds() -> None:
    ev = json.loads((ROOT / "packages/contracts/bus/event.schema.json").read_text(encoding="utf-8"))["$defs"]
    d3 = {"phys.contact.mesh", "phys.coverage.exit", "phys.instance", "phys.sitl.vehicle"}
    assert d3 <= set(ev["known_kinds"]["enum"]) and set(ev["phys_payloads"]["properties"]) == d3
