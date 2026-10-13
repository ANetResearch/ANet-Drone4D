"""模板与字段校验（M18-AC-002；M18-FR-002、FR-003、FR-026；契约 target/*.schema.json、class_defaults.json）。

- 六类模板通过契约 schema；合并后的完整配置（含缺省补齐）通过 `target.schema.json`；
- 9 组字段的越界值各被拒并给出字段级 detail（码 110、540、541、542、544）；未知字段被拒；
- 名称含 emoji 与禁用字形被净化（≤ 32 字符）；radar、rf 敏感体只入 schema 并给出 warning；`rf` 字段只入配置。
"""

from __future__ import annotations

import copy

import pytest
from ctlib import errors, load
from tlib import Bench, FakeWorld

from awr.sim.targets.model import CLASSES, TargetError, class_defaults, merged_config, sanitize_label, validate_config


def test_templates_pass_schema() -> None:
    cd = load("target/class_defaults.json")
    assert errors("target/class_defaults.schema.json", cd) == []
    assert tuple(cd["classes"]) == CLASSES
    for c in CLASSES:
        raw = merged_config({"class": c, "pos": [0.0, 0.0]})
        assert validate_config(raw) == []
        assert errors("target/target.schema.json", raw) == [], c


BAD = [
    ({"size_m": {"l": 0.01, "w": 0.3, "h": 1.7}}, 110, "size_m.l"),                       # 几何
    ({"motion": {"mode": "path", "path": [[0, 0, None]] * 65}}, 544, "motion.path"),          # 运动
    ({"motion": {"mode": "random_walk", "speed_mps": 50}}, 544, "motion.speed_mps"),
    ({"contrast0": 5.0}, 110, "contrast0"),                                                   # 外观
    ({"reflect_850": 0.0}, 110, "reflect_850"),                                               # 反射率
    ({"thermal": {"mode": "hot"}}, 110, "thermal.mode"),                                      # 热
    ({"rcs_m2": 1e4}, 110, "rcs_m2"),                                                         # 雷达
    ({"sound": {"waveform": "tone", "f0_hz": 12000, "lw_dba": 60, "period_s": 1, "duty": 1}}, 541, "sound.f0_hz"),  # 声源
    ({"sound": {"waveform": "harmonic", "f0_hz": 200, "lw_dba": 60, "period_s": 0.05, "duty": 1}}, 541, "sound.period_s"),
    ({"rf": {"freq_mhz": -1}}, 110, "rf.freq_mhz"),                                           # 射频
    ({"alertness": 1.5}, 110, "alertness"),                                                   # 感知与行为
    ({"sensitivity": [{"kind": "visual", "shape": "cone", "radius_max_m": 10}]}, 542, "sensitivity[0].shape"),
    ({"sensitivity": [{"kind": "visual", "shape": "sphere", "radius_max_m": 5000}]}, 542, "sensitivity[0].radius_max_m"),
    ({"sensitivity": [{"kind": "visual", "shape": "sphere", "radius_max_m": 10}] * 5}, 542, "sensitivity"),
    ({"reaction": {"onset": {"kind": "dance"}}}, 110, "reaction.onset.kind"),
    ({"bogus": 1}, 110, "bogus"),
]


@pytest.mark.parametrize(("patch", "code", "field"), BAD)
def test_out_of_range_rejected_with_field_detail(patch, code, field) -> None:
    with pytest.raises(TargetError) as ei:
        validate_config(merged_config({"class": "person", "pos": [0.0, 0.0], **copy.deepcopy(patch)}))
    assert ei.value.code == code and ei.value.detail["field"] == field, ei.value.detail


def test_commands_report_codes_and_warnings() -> None:
    with Bench(0, FakeWorld()) as b:
        r = b.cmd("target/add", {"target": {"class": "person", "pos": [0, 0], "alertness": 2}})
        assert r["status"] == "rejected" and r["code"] == 110 and r["detail"]["field"] == "alertness"
        r = b.cmd("target/add", {"target": {"class": "robot", "pos": [0, 0]}})
        assert r["status"] == "rejected" and r["code"] == 110 and r["detail"]["why"] == "UNKNOWN_CLASS"
        r = b.cmd("target/add", {"target": {"class": "object", "pos": [0, 0],
                                            "sensitivity": [{"kind": "radar", "shape": "sphere", "radius_max_m": 50}],
                                            "rf": {"freq_mhz": 2400.0, "eirp_dbm": 20.0, "duty": 0.1}}})
        assert r["status"] == "accepted" and r["warnings"] == ["SENSITIVITY_KIND_STORED_ONLY"]
        cfg = r["result"]["cfg"]
        assert cfg["rf"] == {"freq_mhz": 2400.0, "eirp_dbm": 20.0, "duty": 0.1}
        assert errors("target/target.schema.json", cfg) == []
        # radar 体不参与判定、不产生禁入体
        assert b.rt.keepout(r["result"]["id"]) == []


def test_label_sanitized() -> None:
    assert sanitize_label("门口" + chr(0x1F600) + "的人" + chr(0x2605) + "x" * 40) == "门口的人" + "x" * 28
    with Bench(0, FakeWorld()) as b:
        tid = b.add({"class": "person", "pos": [0, 0], "label": "  巡逻\U0001F680 人员  "})
        assert b.ents["target"].by_id[tid].label == "巡逻 人员"
        tid2 = b.add({"class": "dog", "pos": [5, 0]})
        assert b.ents["target"].by_id[tid2].label == "狗-01" and tid2 == "dog-01"


def test_template_values_are_reference_values() -> None:
    cd = class_defaults()
    assert "模拟参考值" in cd["doc"] or "reference values" in cd["doc"]
    p = cd["classes"]["person"]
    assert p["size_m"] == {"l": 0.5, "w": 0.3, "h": 1.75} and p["sound"]["lw_dba"] == 70
