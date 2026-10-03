"""合成演示城市剧本 S0（DEMO-W；ADR-077；M16 §6.4.9、M16-AC-043、M16-AC-044）。

静态部分（G1，无需世界）：剧本形状（4–8 架、三类任务、晴 → 小雨 → 雾的过渡顺序、ci profile ×5、时长窗口）。
端到端（`needs_data`：需要已构建的 `worlds/synthcity`，`make demo-world` 生成，无需 UrbanScene3D 数据）：ci profile 的 supervisor
（sim-core + api）以 ×5 运行 S0，`scenario.result = SUCCEEDED`、全部成功谓词为真、两次天气事件与收尾标记出现、无间距告警；
仿真时长 3–5 min，墙钟 ≤ 120 s（含启动）。
"""

from __future__ import annotations

import json

import pytest
from e2ehelp import SCENARIOS

from awr.datasets.scenarios import authoring as A
from awr.datasets.synthcity import SHOWCASE, SYNTHCITY

SID = "s0-synthcity-showcase"
GUARD_PREFIXES = ("SAF.SEP.CONFLICT", "SAF.SEP.AVOIDING", "fleet_guard.conflict", "fleet_guard.avoiding")


def _doc() -> dict:
    return json.loads((SCENARIOS / f"{SID}.json").read_text(encoding="utf-8"))


def test_s0_shape() -> None:
    d = _doc()
    assert d["world_id"] == SYNTHCITY.world_id == "synthcity" and A.world_of(SID) == "synthcity"
    assert 4 <= len(d["vehicles"]) <= 8
    gens = {m["mission_id"]: m["generator"] for m in d["missions"]}
    assert gens == {"m-helix-upper": "helix_scan", "m-helix-lower": "helix_scan", "m-blocks": "lawnmower",
                    "m-formation": "formation"}
    # 立面螺旋覆盖 ANet Tower 的同一立面段，两段相接、自上而下
    up, lo = (next(m["params"] for m in d["missions"] if m["mission_id"] == k) for k in ("m-helix-upper", "m-helix-lower"))
    assert up["center_enu_m"] == lo["center_enu_m"] == list(SHOWCASE["tower_center"])
    assert up["z_range_m"][1] == lo["z_range_m"][0] and up["z_range_m"][0] > up["z_range_m"][1] > lo["z_range_m"][1]
    assert up["facade_z_range_m"] == [lo["z_range_m"][1], up["z_range_m"][0]]
    # 天气：晴开局，随后小雨、雾，事件参数即 env/preset 命令参数 {name, duration_s}
    assert d["env"]["preset"] == "clear"
    wx = [e for e in d["events"] if e["action"] == "env.preset"]
    assert [e["args"]["name"] for e in wx] == ["lightRain", "fog"]
    assert wx[0]["at_s"] < wx[1]["at_s"] < d["time_limit_s"] and all(0 < e["args"]["duration_s"] <= 600 for e in wx)
    assert d["profiles"]["ci"]["rate"] == 5 and d["profiles"]["demo"]["on_complete"] == "continue"
    assert 180 <= d["time_limit_s"] <= 600
    homes = [v["home_enu_m"][:2] for v in d["vehicles"]]
    for i, a in enumerate(homes):
        for b in homes[i + 1:]:
            assert ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 >= 12.0, (a, b)


def test_s0_catalog_default() -> None:
    cat = json.loads((SCENARIOS / "catalog.json").read_text(encoding="utf-8"))
    assert cat["worlds"]["synthcity"]["default"] == SID
    assert "free-synthcity" in cat["worlds"]["synthcity"]["demo"] and SID in cat["themes"]["showcase"]
    assert not cat["worlds"]["synthcity"].get("gate")                      # 门禁城市仍是深圳（V-SC-13）


@pytest.mark.needs_data
@pytest.mark.slow
def test_s0_showcase_x5(scenario_run) -> None:
    """S0 ×5（ci profile）：成功、全部谓词为真、天气两次切换与收尾标记、无 FleetGuard 冲突；仿真 3–5 min，墙钟 ≤ 120 s。"""
    out = scenario_run(SID, "ci", timeout_s=240)
    bad = out.failed_predicates()
    assert out.status == "SUCCEEDED" and not bad, f"{out.status} {bad}"
    assert len(out.predicates) == len(_doc()["success"]["all"])
    fired = {(e.get("data") or {}).get("event_id") for e in out.kinds("scenario.event")}
    assert {"wx-lightrain", "wx-fog"} <= fired, fired
    assert any((e.get("data") or {}).get("label") == "showcase complete" for e in out.kinds("scenario.mark"))
    assert not [e for e in out.events if str(e.get("type")).startswith(GUARD_PREFIXES)]
    t_end = max(int(e.get("t_sim_ns") or 0) for e in out.events) * 1e-9
    assert 180.0 <= t_end <= 300.0, t_end
    assert out.wall_s <= 120.0, out.wall_s
