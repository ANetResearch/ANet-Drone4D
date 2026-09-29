"""M13-AC-018（部分）：state_ext 字段供给（全机查表部分、detail 并 marks 的白噪声部分、全部为可选字段）；M13-FR-034、FR-080。"""

from __future__ import annotations

import json

import numpy as np
from jsonschema import Draft202012Validator

from awr.contracts._paths import contracts_root


def test_fields_for_interest_and_others(bench):
    b = bench
    b.spawn(0, 1)
    b.spawn(1, 2)
    b.interest([1])
    b.run(0.6)
    out = [{"loc": {"status": "TRACKING", "gnss_fix": None, "sats": None}}, {"loc": {"status": "TRACKING"}}]
    b.rt.state_ext_fields(np.array([0, 1]), b.tick * 4_000_000, out)
    a, c = out
    assert a["loc"]["gnss_fix"] == 4 and a["loc"]["sats"] == 22 and len(a["loc"]["err_enu_m"]) == 3
    assert set(a["sens"]) == {"gimbal", "imu"} and set(a["sens"]["imu"]) == {"acc_mps2", "gyro_rad_s", "bias_acc_mps2", "bias_gyro_rad_s"}
    assert a["sens"]["gimbal"][0] == {"sensor_no": 0, "mode": "fixed", "az_rad": 0.0, "el_rad": -0.261799, "limited": False}
    assert "err_enu_m" not in c["loc"] and "imu" not in c["sens"] and c["loc"]["eph_m"] is not None


def test_loc_fields_validate_against_contract(bench):
    """loc 的新增字段在 1.0 schema 下合法（loc additionalProperties = true）；sens 待 M00 登记（M13-to-M00）。"""
    b = bench
    b.spawn(0, 1)
    b.interest([1])
    b.run(0.6)
    d = b.rt.ext_fields_for(0)
    schema = json.loads((contracts_root() / "rt" / "payloads" / "uav_state_ext.schema.json").read_text())
    loc_schema = schema["properties"]["loc"]
    assert list(Draft202012Validator(loc_schema).iter_errors(d["loc"])) == []
    json.dumps(d)  # 可 JSON 序列化（NaN 已转 null）
