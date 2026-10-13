"""配置快照校验与会话配置组装（M17-FR-009、AC-016 的校验部分；17 §17.4.5；512 的五种原因）。"""

from __future__ import annotations

import json
from pathlib import Path

from sbharness import make_sb

from awr.sandbox.config import limits_obj
from awr.sandbox.snapshot import check_snapshot
from awr.sandbox.templates import build_session_config, default_calendar

ROOT = Path(__file__).resolve().parents[2]
LIM = limits_obj(make_sb())


def _snap() -> dict:
    return json.loads((ROOT / "packages/contracts/fixtures/d2/rest/snapshot.json").read_text())


def test_fixture_snapshot_valid_and_reasons() -> None:
    s = _snap()
    assert check_snapshot(s, world=s["world_id"], limits=LIM, max_kib=512) is None
    assert check_snapshot(s, world="atlantis", limits=LIM, max_kib=512)[0] == "world_mismatch"
    assert check_snapshot(s, world=None, limits=LIM, max_kib=0.01)[0] == "size"
    assert check_snapshot(dict(s, contracts="2.0.0"), world=None, limits=LIM, max_kib=512)[0] == "version"
    bad = dict(s, vehicles="nope")
    why, detail = check_snapshot(bad, world=None, limits=LIM, max_kib=512)
    assert why == "schema" and detail["path"].startswith("vehicles")
    assert check_snapshot([], world=None, limits=LIM, max_kib=512)[0] == "schema"
    lim = dict(LIM, targets_max=0)
    if s.get("targets"):
        assert check_snapshot(s, world=None, limits=lim, max_kib=512)[0] == "limits"


def test_session_config_from_snapshot_and_template() -> None:
    s = _snap()
    doc = build_session_config(s["world_id"], template_id=None, template=None, snapshot=s, calendar=None, acoustic_bg=None,
                               seed=9, limits=LIM, tz="Asia/Shanghai", now=0)
    assert doc["template_id"] is None and doc["calendar"] == s["calendar"] and doc["seed"] == 9
    assert doc["limits"] == {"vehicles": 12, "targets": 30, "clones": 8, "nests": 4, "tasks_active": 6}
    tpl = {"vehicles": [{"model_id": "p600"}], "calendar_default": {"start_local": "08:30", "tz": "America/New_York"},
           "acoustic_bg": "suburban"}
    doc = build_session_config("newyork", template_id="t1_point_watch", template=tpl, snapshot=None, calendar=None,
                               acoustic_bg=None, seed=1, limits=LIM, tz="America/New_York", now=1_791_000_000)
    assert doc["calendar"]["tz"] == "America/New_York" and doc["calendar"]["start_utc"].endswith(":30:00Z")
    assert doc["acoustic_bg"] == "suburban" and doc["nests"] == [] and len(doc["vehicles"]) == 1
    cal = {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"}
    doc = build_session_config("synthcity", template_id=None, template=None, snapshot=None, calendar=cal, acoustic_bg="rural",
                               seed=2, limits=LIM, tz=None)
    assert doc["calendar"] == cal and doc["acoustic_bg"] == "rural"


def test_default_calendar_local_ten() -> None:
    c = default_calendar("Asia/Shanghai", now=1_791_000_000)
    assert c["tz"] == "Asia/Shanghai" and c["start_utc"].endswith("T02:00:00Z")
    assert default_calendar("Nope/Zone", now=0)["tz"] == "Asia/Shanghai"
