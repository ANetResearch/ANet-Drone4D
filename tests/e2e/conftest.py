"""tests/e2e 公共夹具（M16 §9.1、§9.2）。

- `scenario_run(sid, profile)`：以 ci profile 的真实 supervisor 运行剧本，轮询 `/api/events` 直到 `scenario.result`，
  返回 `ScenarioOutcome`（状态、谓词、事件、墙钟）；结束后停止并删除临时 runs 目录。
- `world_query(wid)`：已构建世界的 `WorldQuery`（M04），zones 换成"世界 border + 本仓库 curated 文件"的合并结果，
  使 V-SC-05、V-SC-09 在世界尚未按新的 curated 文件重建时也能对剧本文件做离线校验（needs_data）。
- 长时剧本运行（S1 ×1、S2–S6、恶劣天气 profile）只在 `AWR_E2E_FULL=1` 时执行（harness 的 `e2e.scenarios` 用例设置），
  避免并行开发期间的 `make test` 占满 CPU；S1 ×10、free、ladder 冒烟在 G1 中运行（`slow`）。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from awrproc import Backend  # noqa: E402
from e2ehelp import SCENARIOS, _world_query, needs_world  # noqa: E402


@pytest.fixture
def world_query():
    def get(wid: str) -> Any:
        needs_world(wid)
        return _world_query(wid)

    return get


@pytest.fixture(scope="session")
def profile_table():
    from awr.sim.fleet.profiles import ProfileTable

    return ProfileTable()


@dataclass
class ScenarioOutcome:
    scenario_id: str
    profile: str | None
    status: str
    predicates: list[dict]
    events: list[dict]
    wall_s: float
    load_s: float | None
    result: dict = field(default_factory=dict)

    def kinds(self, prefix: str) -> list[dict]:
        return [e for e in self.events if str(e.get("type", "")).startswith(prefix)]

    def failed_predicates(self) -> list[dict]:
        return [p for p in self.predicates if not p.get("ok")]


@pytest.fixture
def scenario_run(tmp_path):
    """启动 ci profile 的 supervisor（world 取剧本 world_id，profile 经 AWR_SCENARIO_PROFILE），轮询到 `scenario.result`。"""
    started: list[Backend] = []

    def run(sid: str, profile: str | None = None, *, timeout_s: float = 300.0, scenarios_dir: Path | None = None,
            world: str | None = None, until: Any = None, only: str | None = "sim-core,api") -> ScenarioOutcome:
        src = (scenarios_dir or SCENARIOS) / f"{sid}.json"
        doc = json.loads(src.read_text(encoding="utf-8"))
        wid = world or doc["world_id"]
        needs_world(wid)
        b = Backend(world=wid, scenario=sid, scenario_profile=profile, scenarios_dir=scenarios_dir, only=only)
        started.append(b)
        t0 = time.monotonic()
        b.start()
        events: list[dict] = []
        pred = until or (lambda e: e.get("type") == "scenario.result")
        last = b.wait_event(pred, timeout_s, collect=events)
        wall = time.monotonic() - t0
        loaded = next((e for e in events if e.get("type") == "scenario.loaded"), None)
        invalid = next((e for e in events if e.get("type") == "scenario.invalid"), None)
        if invalid is not None:
            pytest.fail(f"{sid}: scenario.invalid {invalid.get('data')}")
        data = dict(last.get("data") or {})
        return ScenarioOutcome(sid, profile, str(data.get("status", "")), list(data.get("predicates") or []), events,
                               wall, (loaded or {}).get("t_sim_ns", None) and loaded["t_sim_ns"] * 1e-9, data)

    yield run
    for b in started:
        b.stop()
