"""M13-AC-205：数据产品 `uav/{id}/payload`（2 Hz）与 `uav/{id}/sensor/nav`（10 Hz，订阅时，每会话 ≤ 4 架），schema 与频率；
定位档（P1）；`GET .../vehicles/{vid}/sensors`；M13-FR-209–211。"""

from __future__ import annotations

import json

import msgpack
import numpy as np
import pytest
from jsonschema import Draft202012Validator

from awr.contracts._paths import contracts_root
from awr.sim.core import metrics as MET
from awr.sim.schemas import contracts_registry


def _validator(rel: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((contracts_root() / rel).read_text(encoding="utf-8")), registry=contracts_registry())


PAYLOAD = _validator("sensor/payload.schema.json")
NAV = _validator("sensor/nav.schema.json")


def _fleet(b, n=2):
    for i in range(n):
        b.spawn(i, 10 + i, "p600_mid360" if i % 2 == 0 else "tq9_test", vid=f"u{i}", pos=(10.0 * i, 0.0, 50.0))
    b.run(0.04)


def test_payload_2hz_schema_and_every_d2_vehicle(bench_d2):
    b = bench_d2
    got: list[dict] = []
    b.rt.payload_pub.sink = lambda raw: got.append(msgpack.unpackb(raw, raw=False))
    _fleet(b, 3)
    got.clear()
    b.run(10.0)
    assert 19 <= len(got) <= 21  # 2 Hz ± 5%（10 s）
    for m in got:
        assert m["v"] == 1 and sorted(m["items"]) == ["u0", "u1", "u2"]  # 全集，按机体 id 为键（M11 网关拆分）
        for k, it in m["items"].items():
            assert it["uav"] == k
            errs = [e.message for e in PAYLOAD.iter_errors(it)]
            assert not errs, errs
    t = [m["t_sim_ns"] for m in got]
    assert all(d == 500_000_000 for d in np.diff(t))
    kinds = {e["sensor"]: e["kind"] for e in got[-1]["items"]["u1"]["sensors"]}
    assert kinds == {"eo": "eo", "lwir": "lwir", "radar": "radar", "mic": "acoustic"}


def test_d1_vehicles_only_in_interest(bench_factory):
    b = bench_factory()
    b.slow_d2 = True
    got: list[dict] = []
    b.rt.payload_pub.sink = lambda raw: got.append(msgpack.unpackb(raw, raw=False))
    for i in range(3):
        b.spawn(i, 10 + i, "x500")  # 纯 D1 映射的机型（P600 自 2.0.0 起为对象形式，D2-INT）
    b.run(1.0)
    assert got == []  # D1 机体不在兴趣集时不生成（大机群成本）
    b.interest([11])
    b.run(1.0)
    items = [it for m in got for it in m["items"].values()]
    assert items and {it["uav"] for it in items} == {"v11"}
    for it in items:
        assert not [e.message for e in PAYLOAD.iter_errors(it)]
    assert {e["sensor"] for e in items[-1]["sensors"]} == {"camera"}


def test_nav_10hz_only_when_subscribed_max_4(bench_d2):
    b = bench_d2
    got: list[dict] = []
    b.rt.nav.sink = lambda raw: got.append(msgpack.unpackb(raw, raw=False))
    _fleet(b, 6)
    b.run(1.0)
    assert got == []
    b.rt.stream_interest("nav", np.array([10, 11, 12, 13, 14], np.uint16))  # M11 兴趣下推（streams.nav）
    b.run(10.0)
    assert 99 <= len(got) <= 101  # 10 Hz ± 5%
    for m in got:
        assert list(m["items"]) == ["u0", "u1", "u2", "u3"]  # 第 5 架不生成
        for it in m["items"].values():
            errs = [e.message for e in NAV.iter_errors(it)]
            assert not errs, errs
    assert b.rt.nav.stats["nav_dropped"] > 0
    it = got[-1]["items"]["u0"]
    assert it["imu"]["f_mps2"][2] == pytest.approx(9.81, abs=0.3)  # 悬停比力（FLU，含噪声与偏置）
    assert it["gnss"]["fix"] == "rtk_fixed" and it["gnss"]["pos_enu_m"][2] == pytest.approx(50.0, abs=0.5)
    assert it["baro"]["alt_m"] == pytest.approx(50.0, abs=5.0)
    assert it["loc_mode"] == "rtk"


def test_nav_values_deterministic(bench_d2_factory):
    out = []
    for _ in range(2):
        b = bench_d2_factory()
        got: list[bytes] = []
        b.rt.nav.sink = got.append
        _fleet(b, 2)
        b.rt.stream_interest("nav", [10, 11])
        b.run(1.0)
        out.append(got)
    assert out[0] == out[1]


def test_loc_mode_follows_rtk_cover_metric(bench_d2):
    """P1：度量 `tasking.rtk_cover`（M20 登记）覆盖时 rtk 且 GNSS 为 RTK_FIXED，移出覆盖后装 MID-360 的为 slam、单点档。"""
    b = bench_d2
    got: list[dict] = []
    b.rt.nav.sink = lambda raw: got.append(msgpack.unpackb(raw, raw=False))
    _fleet(b, 2)
    b.rt.stream_interest("nav", [10, 11])
    with MET.isolated_metrics():
        MET.register_metric("tasking.rtk_cover", lambda x_m, y_m: 1.0 if x_m < 5.0 else 0.0, owner="M20")
        b.run(0.3)
        by = got[-1]["items"]
        assert by["u0"]["loc_mode"] == "rtk" and by["u0"]["gnss"]["fix"] == "rtk_fixed"
        assert by["u1"]["loc_mode"] == "single" and by["u1"]["gnss"]["fix"] == "3d"  # tq9 无 MID-360
        b.set_pose(0, (20.0, 0.0, 50.0))
        b.run(0.3)
        by = got[-1]["items"]
        assert by["u0"]["loc_mode"] == "slam" and by["u0"]["gnss"]["fix"] == "3d"
        assert by["u0"]["gnss"]["hacc_m"] > 0.5


def test_sensors_rest_body(bench_d2):
    b = bench_d2
    b.rt.configure_mounts("u0", {"nir_ill": {"enabled": True}})
    _fleet(b, 1)
    r = b.rt.sensors_rest("u0")
    assert not [e.message for e in PAYLOAD.iter_errors(r["payload"])]
    names = [x["name"] for x in r["sensors"]]
    assert names == ["mid360", "gnss", "imu", "eo", "nir_ill"]
    eo = r["sensors"][3]
    assert eo["spec_ref"] == "eo_gx40@1.0.0" and eo["kind"] == "eo" and eo["state"] == "ACTIVE" and eo["mode"] == "day"
    assert len(eo["mount"]["q_xyzw"]) == 4 and eo["mount"]["pos_m"] == [0.12, 0.0, -0.08]
    assert b.rt.sensors_rest("nobody") is None
    ms = b.rt.mount_summary(0)
    assert ms["mass_kg"] == pytest.approx(0.4236 + 0.265 + 0.18)


def test_configure_mounts_after_spawn_remounts(bench_d2):
    b = bench_d2
    _fleet(b, 1)
    rt = b.rt
    assert rt.rig_of(0).by_name("nir_ill") is None
    summ = rt.configure_mounts("u0", {"nir_ill": {"enabled": True}, "mid360": {"enabled": False}})
    assert summ["mass_kg"] == pytest.approx(0.4236 + 0.18)
    b.run(0.04)
    rig = rt.rig_of(0)
    assert rig.by_name("nir_ill") is not None and rig.by_name("mid360") is None and not (rt.blk["has"][0] & 16)
    assert rt.rig_of(0).col_specs[0].name == "eo"
    from awr.sim.sensors.gimbal import SensorError

    with pytest.raises(SensorError) as e:
        rt.configure_mounts("u0", {"camera": {"enabled": True}})
    assert e.value.code == 560


def test_registered_command_and_query_routes(bench_d2):
    """M08-FR-218 的机体命令分派（`register_command_handler`，msg 带 `uav`）与查询 `payload/state`。"""
    b = bench_d2
    _fleet(b, 1)
    cmds, qs = b.reg.commands, b.reg.queries
    assert {"gimbal", "zoom", "sensor/mode"} <= set(cmds) and all(cmds[o].owner == "M13" for o in ("gimbal", "zoom", "sensor/mode"))
    r = cmds["zoom"].fn({"op": "zoom", "uav": "u0", "_slot": 0, "args": {"sensor": "eo", "f_mm": 24.0}}, b.tick + 1, b.ctx)
    assert r["status"] == "accepted"
    r = cmds["gimbal"].fn({"op": "gimbal", "service": "uav/u0/cmd/gimbal", "args": {"mode": "stow"}}, b.tick + 1, b.ctx)
    assert r["status"] == "accepted"
    assert cmds["zoom"].fn({"op": "zoom", "args": {"sensor": "eo"}}, b.tick + 1, b.ctx)["code"] == 110  # 无机体
    b.run(3.0)
    q = qs["payload/state"].fn({"v": 1, "op": "payload/state", "args": {"id": "u0"}}, b.ctx)
    assert q["code"] == 0 and {e["sensor"]: e["f_mm"] for e in q["item"]["sensors"]}["eo"] == 24.0
    assert q["item"]["gimbal"]["mode"] == "stow" and q["item"]["gimbal"]["pitch_rad"] == 0.0
    assert qs["payload/state"].fn({"args": {"id": "nope"}}, b.ctx)["code"] == 107


def test_streams_from_stage_ctx(bench_d2):
    """M08 把兴趣下推的 `streams{}` 放在 `StageCtx.streams`（WP-10 -> M08 第 6 条）：nav 与 lidar 按其生成。"""
    b = bench_d2
    got: list[dict] = []
    b.rt.nav.sink = lambda raw: got.append(msgpack.unpackb(raw, raw=False))
    _fleet(b, 2)
    if not hasattr(b.ctx, "streams"):
        pytest.skip("StageCtx.streams not available")
    b.ctx.streams = {"nav": np.array([11], np.uint16), "lidar": np.array([10], np.uint16)}
    b.run(0.5)
    assert got and {k for m in got for k in m["items"]} == {"u1"}
    assert int(b.rt.pblk["scan"][0]) == 1


def test_bus_shape_splits_in_m11_gateway(bench_d2):
    """总线载荷 `{v, t_sim_ns, items: {<uav id>: ...}}` 经 M11 `StreamDemux._items` 拆成逐机负载（d2-WP-03-to-M13 第 1 条）。"""
    from types import SimpleNamespace

    from awr.api.rt.detail import StreamDemux

    b = bench_d2
    pay: list[bytes] = []
    nav: list[bytes] = []
    b.rt.payload_pub.sink = pay.append
    b.rt.nav.sink = nav.append
    _fleet(b, 2)
    b.rt.stream_interest("nav", [11])
    b.run(0.6)
    dm = StreamDemux(SimpleNamespace(frame_t_sim_ns=0))
    t, items = dm._items("payload", pay[-1])
    assert t == msgpack.unpackb(pay[-1], raw=False)["t_sim_ns"] and [k for k, _ in items] == ["u0", "u1"]
    for _k, raw in items:
        assert not [e.message for e in PAYLOAD.iter_errors(msgpack.unpackb(raw, raw=False))]
    _t, items = dm._items("nav", nav[-1])
    assert [k for k, _ in items] == ["u1"] and not [e.message for e in NAV.iter_errors(msgpack.unpackb(items[0][1], raw=False))]


def test_instance_without_sensors_gets_instance_defaults(bench_d2_factory):
    """M08 对每个沙盒机队实例调用 `configure_mounts(vid, None, profile_id=...)`：即使未判定为沙盒时钟档，也按实例缺省挂载。"""
    b = bench_d2_factory(sandbox=False)
    b.run(0.04)  # sim-core 已在运行（ctx 已绑定）
    summ = b.rt.configure_mounts("i1", None, profile_id="p600_mid360")
    assert [x["slot"] for x in summ["slots"] if x["mounted"]] == ["mid360", "gnss", "imu", "eo"]
    b.spawn(0, 1, "p600_mid360", vid="i1")
    b.spawn(1, 2, "p600_mid360", vid="s1")  # 剧本机体（未调用 configure_mounts）：D1 槽位 + 缺省槽位
    b.run(0.04)
    assert b.rt.mounted_names("i1") == ["mid360", "gnss", "imu", "eo"]
    assert b.rt.mounted_names("s1") == ["camera", "thermal", "mid360", "gnss", "imu", "eo"]


def test_configure_mounts_on_d1_form_rig_reports_full_rig(bench):
    """纯 D1 映射的机型（x500；P600 自 2.0.0 起为对象形式，D2-INT）：机队实例为完整组，汇总列出全部槽位为已挂载。"""
    b = bench
    b.run(0.04)
    summ = b.rt.configure_mounts("i1", None, profile_id="x500")
    assert [x["slot"] for x in summ["slots"] if x["mounted"]] == ["camera"]
    from awr.sim.sensors.gimbal import SensorError

    with pytest.raises(SensorError):
        b.rt.configure_mounts("i1", {"radar": {"enabled": True}}, profile_id="x500")
