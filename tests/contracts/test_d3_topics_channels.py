"""D3 扩展点 10（AWR-05 §12.3；17 §18.11、§18.13；M27 §7.5.1）：lab-gateway 的 5 个 topic 与 D3 录制通道只做加法。
D3 topic 声明 `apis = ["lab"]` 与作用域 `scope`，生成物把它们放在独立的 LAB_TOPICS 中，D1/D2 网关的 `match_topic` 不匹配；
D3 通道登记在 `d3_channels`，D2 录制器只读 `channels`，真值通道只写真值文件。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import load

from awr.contracts import topics as T

LAB = {"bench/run/{rid}/status", "uav/{id}/sensor/{name}/preview", "uav/{id}/lidar/{name}/v2", "event/status", "actor/state"}


def test_lab_topics_are_declared_with_scope_and_lab_api_only() -> None:
    rows = [t for t in load("rt/topics.json")["topics"] if t.get("apis") == ["lab"]]
    assert {t["pattern"] for t in rows} == LAB
    for t in rows:
        assert t["d1"] == "V0.3" and t["scope"] in ("lab_researcher", "lab_evaluator"), t["pattern"]
    assert T.LAB_SCOPES["actor/state"] == "lab_evaluator"


def test_d2_gateways_never_match_lab_topics() -> None:
    assert {t.pattern for t in T.TOPICS}.isdisjoint(LAB)
    for topic in ("bench/run/r-1/status", "uav/p600-01/sensor/cam0/preview", "uav/p600-01/lidar/mid360/v2", "event/status", "actor/state"):
        assert T.match_topic(topic) is None, topic
        assert T.match_lab_topic(topic) is not None, topic
    assert T.match_topic("uav/p600-01/sensor/cam0/pose").pattern == "uav/{id}/sensor/{name}/pose"   # D2 不变
    assert T.match_lab_topic("uav/p600-01/sensor/cam0/pose") is None


def test_d3_channels_are_separate_and_truth_stays_in_truth_files() -> None:
    d = load("rec/mcap_channels.json")
    d2_topics = {c["topic"] for c in d["channels"]}
    d3 = d["d3_channels"]
    assert len(d3) == len({c["topic"] for c in d3})
    assert all(c["file"] == "truth" for c in d3 if c["layer"] == "truth")
    assert all(c["file"] == "raw" for c in d3 if c["layer"] in ("observation", "run"))
    hidden = [c for c in d3 if c["topic"].startswith("/eval/hidden/")]
    assert hidden and all(c["file"] == "truth" for c in hidden)
    assert "/uav/{id}/state" in d2_topics       # D2 录制器的同名通道不受 D3 真值通道影响
    from awr.recorder import formats

    assert formats.channel_spec("/uav/p600-01/state").schema == "awr.DroneState64.v1"
    for c in d3:
        if c["topic"] not in d2_topics and not c["topic"].startswith("/uav/{id}/"):
            try:
                formats.channel_spec(c["topic"].replace("{uav}", "p600-01").replace("{name}", "cam0"))
            except KeyError:
                continue
            raise AssertionError(f"D2 recorder resolves the D3 channel {c['topic']}")
