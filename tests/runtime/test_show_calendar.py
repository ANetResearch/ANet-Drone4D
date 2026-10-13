"""共享展示 S7 的会话配置副本与日历（ADR-094；supervisor `_session_config_env`；D2-FX-R2-other）。

- 缺省：`calendar.start_utc` 为首次写副本时的 UTC 时刻（仿真日历与北京时间同步），时区沿用剧本（Asia/Shanghai）；
- `AWR_SHOW_CALENDAR_START`（浸泡工具日切自检）：格式正确时代替启动时刻，格式不符时忽略；
- 副本只写一次（崩溃接替沿用首次写入的日历）。
"""

from __future__ import annotations

import calendar
import json
import os
import time
from pathlib import Path

from test_supervisor import make_cfg

from awr.runtime.config import load_runtime_config
from awr.runtime.supervisor import Supervisor


def _sup(tmp: Path) -> Supervisor:
    cfg = load_runtime_config(make_cfg(tmp, []), profile="dev", env={})
    sup = Supervisor(cfg, bus_kind="local", out=open(os.devnull, "w"))  # noqa: SIM115
    cfg.run.scenario = "s7-synthcity-relay"
    sup.persist_dir = tmp / "persist"
    return sup


def _cal(env: dict[str, str]) -> dict:
    return json.loads(Path(env["AWR_SESSION_CONFIG"]).read_text(encoding="utf-8"))["calendar"]


def test_show_calendar_defaults_to_start_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AWR_SHOW_CALENDAR_START", raising=False)
    env = _sup(tmp_path)._session_config_env(["awr.sim.mission"])
    cal = _cal(env)
    t = calendar.timegm(time.strptime(cal["start_utc"], "%Y-%m-%dT%H:%M:%SZ"))
    assert cal["tz"] == "Asia/Shanghai" and abs(t - time.time()) < 120
    assert env["AWR_SCENARIO_LOAD"] == "0" and "awr.sim.tasking.plugin" in env["AWR_PLUGINS"]


def test_show_calendar_start_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AWR_SHOW_CALENDAR_START", "2026-10-11T19:57:00Z")
    sup = _sup(tmp_path)
    assert _cal(sup._session_config_env([]))["start_utc"] == "2026-10-11T19:57:00Z"
    monkeypatch.setenv("AWR_SHOW_CALENDAR_START", "2026-10-12T00:00:00Z")     # 副本已存在：不重算
    assert _cal(sup._session_config_env([]))["start_utc"] == "2026-10-11T19:57:00Z"


def test_show_calendar_start_invalid_is_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AWR_SHOW_CALENDAR_START", "tomorrow 04:00")
    cal = _cal(_sup(tmp_path)._session_config_env([]))
    assert cal["start_utc"] != "tomorrow 04:00" and cal["start_utc"].endswith("Z")
