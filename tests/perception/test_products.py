"""M19-AC-014：数据产品与查询。`uav/{id}/perception`、`radar_tracks`、`acoustic_bearings` 与检测记录逐项通过契约 schema（AWR-04
§11.7 的 EO、夜视、LWIR、雷达、声阵列、检测历史各行字段可得）；帧 ≤ 2 KB；无订阅不编码（总线只为兴趣下推的机体发布）；
SB-38、SB-39、SB-66 的查询处理函数（检测环分页、过滤、since_seq 早于环 319）；事件 ≤ 50 条/s 合并（M19-FR-016）。"""

from __future__ import annotations

import msgpack
import numpy as np
import pytest
from ctlib import errors, registry
from plib import FakeEnv, FakeWorld, PBench, look_R, make_imaging, make_nonimg

from awr.sim.perception import gx40, gx40_nir, lwir640, mic8, mid360, mmw77


def _errs_def(rel: str, ptr: str, inst) -> list[str]:
    import json

    from jsonschema import Draft202012Validator

    from awr.contracts._paths import contracts_root

    sid = json.loads((contracts_root() / rel).read_text(encoding="utf-8"))["$id"]
    v = Draft202012Validator({"$ref": f"{sid}#{ptr}"}, registry=registry())
    return [e.message[:200] for e in v.iter_errors(inst)]


def _bench(env=None):
    b = PBench(2, FakeWorld(), env=env or FakeEnv())
    ids = [b.add({"class": "person", "pos": [300.0, 0.0, 0.0], "motion": {"mode": "static"}}),
           b.add({"class": "vehicle", "pos": [120.0, 10.0, 0.0], "motion": {"mode": "static"}}),
           b.add({"class": "dog", "pos": [40.0, -5.0, 0.0], "motion": {"mode": "static"},
                  "sound": {"waveform": "pulse_train", "f0_hz": 600.0, "lw_dba": 100.0, "period_s": 1.0, "duty": 1.0}})]
    b.step(1)
    p = (0.0, 0.0, 30.0)
    b.platform(0, pos=p, imaging=[make_imaging(gx40(), p, (200.0, 0.0, 0.0), f_mm=10.0, name="eo", sensor_no=3),
                                  make_imaging(lwir640(25.0), p, (200.0, 0.0, 0.0), name="lwir", sensor_no=4)],
               nonimg=[make_nonimg("radar", mmw77(), p, name="radar", sensor_no=5), make_nonimg("acoustic", mic8(), p, name="mic", sensor_no=6),
                       make_nonimg("lidar", mid360(), p, R=look_R(p, (40.0, -5.0, 0.0)), name="mid360", sensor_no=7)], lw_bands=np.full(24, 50.0))
    q = (0.0, 50.0, 40.0)
    b.platform(1, pos=q, imaging=[make_imaging(gx40_nir(True), q, (300.0, 0.0, 0.9), f_mm=48.0, name="eo", sensor_no=3, mode="nir")])
    return b, ids


def test_frames_pass_schema_and_size():
    b, _ids = _bench()
    with b:
        b.run(3.0)
        for uav in ("uav-01", "uav-02"):
            f = b.prt.frame_for(uav)
            assert f is not None
            assert errors("sensor/perception.schema.json", f) == [], f
            assert len(msgpack.packb(f, use_bin_type=True)) <= 2048
        f1 = b.prt.frame_for("uav-01")
        kinds = {s["kind"] for s in f1["sensors"]}
        assert kinds == {"eo", "lwir"}
        lw = next(s for s in f1["sensors"] if s["kind"] == "lwir")
        assert any("delta_t_app_k" in t for t in lw["targets"])
        assert any("meas_enu_m" in t and "sigma_m" in t for s in f1["sensors"] for t in s["targets"])
        assert b.prt.frame_for("uav-02")["sensors"][0]["kind"] == "nir"
        full = b.prt.vehicle("uav-01")
        assert errors("sensor/perception.schema.json", full) == [], full
        assert full["radar"]["tracks"] and full["acoustic"]["bearings"] and full["lidar"]["n_points_target"]
        assert errors("sensor/radar_tracks.schema.json", full["radar"]) == []
        assert errors("sensor/acoustic_bearings.schema.json", full["acoustic"]) == []


def test_detection_ring_schema_and_paging():
    b, ids = _bench()
    with b:
        b.run(3.0)
        q = b.reg.queries["perception/detections"].fn
        rep = q({"args": {"since_seq": 0}}, b.ctx)
        assert rep["code"] == 0
        page = rep["item"]
        assert _errs_def("sensor/detection.schema.json", "/$defs/page", page) == []
        kinds = {r["kind"] for r in page["items"]}
        assert {"level", "capture", "detect"} <= kinds
        for r in page["items"]:
            assert errors("sensor/detection.schema.json", r) == [], r
            if r["sensor_kind"] in ("radar", "acoustic", "lidar"):
                assert r["target"] is None
        seqs = [r["seq"] for r in page["items"]]
        assert seqs == sorted(seqs) and page["next_since_seq"] == seqs[-1]
        only = q({"args": {"since_seq": 0, "target": ids[0], "uav": "uav-02"}}, b.ctx)["item"]["items"]
        assert only and all(r["target"] == ids[0] and r["uav"] == "uav-02" for r in only)
        lim = q({"args": {"since_seq": 0, "limit": 2}}, b.ctx)["item"]
        assert len(lim["items"]) == 2 and lim["next_since_seq"] == lim["items"][-1]["seq"]
        assert q({"args": {"since_seq": 0, "level_min": "I"}}, b.ctx)["item"]["items"] == [] or all(
            r["level_to"] == "I" for r in q({"args": {"since_seq": 0, "level_min": "I"}}, b.ctx)["item"]["items"])
        # 环溢出：早于最早一条的 since_seq → 319
        ring = b.prt.ring
        for _ in range(ring.cap + 5):
            ring.push({"t_sim_ns": 0, "kind": "detect", "uav": "x", "sensor": "s", "sensor_kind": "radar", "target": None, "level_from": "none",
                       "level_to": "D"})
        assert q({"args": {"since_seq": 1}}, b.ctx)["code"] == 319
        assert q({"args": {"since_seq": 0}}, b.ctx)["code"] == 0


def test_vehicle_and_tracks_queries():
    b, ids = _bench()
    with b:
        b.run(3.0)
        qv = b.reg.queries["perception/vehicle"].fn
        assert qv({"args": {"id": "uav-01"}}, b.ctx)["code"] == 0
        assert qv({"args": {"id": "nope"}}, b.ctx)["code"] == 107
        qt = b.reg.queries["perception/tracks"].fn
        items = qt({"args": {}}, b.ctx)["item"]["items"]
        assert {t["target"] for t in items} >= {ids[0]}
        assert _errs_def("sensor/perception.schema.json", "/$defs/tracks", {"items": items}) == []
        one = qt({"args": {"target": ids[0]}}, b.ctx)["item"]["items"]
        assert len(one) == 1 and one[0]["state"] in ("TENTATIVE", "CONFIRMED", "COASTING")


def test_bus_payloads_only_for_subscribed():
    b, _ids = _bench()
    with b:
        b.run(1.0)
        assert b.payloads == []                                   # 无订阅：不编码
        b.ctx.streams = {"perception": np.array([2], np.uint16), "radar": np.array([1], np.uint16), "acoustic": np.array([1], np.uint16)}
        b.run(1.0)
        keys = {k for k, _ in b.payloads}
        assert keys == {"state/sim-core/perception", "state/sim-core/radar", "state/sim-core/acoustic"}
        for k, msg in b.payloads:
            assert msg["v"] == 1 and isinstance(msg["items"], dict)
            if k.endswith("perception"):
                assert set(msg["items"]) == {"uav-02"}
                assert errors("sensor/perception.schema.json", msg["items"]["uav-02"]) == []
            else:
                assert set(msg["items"]) == {"uav-01"}


def test_event_rate_limit_merges_level_events():
    b, _ids = _bench()
    with b:
        prt = b.prt
        for k in range(200):
            prt._emit(b.ctx, "perception.level", 0, 1_000_000_000, {"uav": "x", "k": k}, "x", critical=False)
        assert prt.stats["events_merged"] >= 140
        prt._emit(b.ctx, "perception.capture", 1, 1_000_000_000, {"uav": "x"}, "x", critical=True)
        assert b.events.items[-1]["kind"] == "perception.capture"
        prt._emit(b.ctx, "perception.level", 0, 3_000_000_000, {"uav": "x"}, "x", critical=False)
        assert b.events.items[-1]["data"]["merged"] >= 140


@pytest.mark.parametrize("night", [False, True])
def test_hud_and_limiting(night):
    env = FakeEnv.night() if night else FakeEnv()
    b, ids = _bench(env)
    with b:
        b.run(2.0)
        f = b.prt.frame_for("uav-01")
        assert f["hud"] is None or f["hud"]["target"] in ids
        eo = next(s for s in f["sensors"] if s["sensor"] == "eo")
        if night:
            assert all(t["limiting"] in ("light", "pixels", None) for t in eo["targets"])
