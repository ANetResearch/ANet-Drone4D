"""D2 contract freeze (WP-01; AWR-04 §11.6; AWR-17 §17.7-§17.11; API-AC-055, API-AC-058): catalogs, layouts, enums, reasons, topics,
commands, keys, events, schemas and the closed-form reference data."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import CONTRACTS, ROOT, errors, load

from awr.contracts import CONTRACTS_VERSION, bus_keys, commands, enums, layouts, reasons, rng_streams, topics, units

sys.path.insert(0, str(ROOT / "tools" / "fake"))
import d2_synth
import targets_stub as TS

D2 = "V0.2-demo"


def test_contracts_version_1_1_0():
    # D2.1 契约 1.2.0（只增，17 §17.20.8；D2-INT 合入）：layouts 未变，仍为 1.1.0
    assert load("package.json")["version"] == "1.2.0" == CONTRACTS_VERSION
    assert load("rt/layouts.json")["version"] == "1.1.0"


# ---------------------------------------------------------------- awr.TargetLite32.v1 (AWR-17 §17.7.3, API-AC-055)
def test_target_lite32_layout_matches_awr17():
    s = next(x for x in load("rt/layouts.json")["schemas"] if x["schemaName"] == "awr.TargetLite32.v1")
    assert s["size"] == 32 and s["kind"] == "record"
    off = {f["n"]: (f["t"], f["o"], f.get("c", 1)) for f in s["fields"]}
    assert off == {"agent_no": ("u16", 0, 1), "class": ("u8", 2, 1), "state": ("u8", 3, 1), "pos": ("f32", 4, 3), "heading_rad": ("f32", 16, 1),
                   "speed_cms": ("i16", 20, 1), "best": ("u8", 22, 1), "alertness_u8": ("u8", 23, 1), "discovered_n": ("u16", 24, 1),
                   "n_vol": ("u8", 26, 1), "flags": ("u8", 27, 1), "keepout_r_m": ("f32", 28, 1)}
    b = layouts.TARGET_LITE32_BITS
    assert b["state"] == {"awareness": (0, 3, "Awareness"), "criterion": (3, 1, "CriterionMode"), "moving": (4, 1), "occupied": (5, 1),
                          "sound_on": (6, 1), "enabled": (7, 1)}
    assert b["best"] == {"level": (0, 2, "PerceptionLevel"), "captured": (2, 1), "relay": (3, 1), "sensor": (4, 4, "PerceptionSensor")}
    assert b["flags"] == {"hidden": (0, 1), "discovered_recent": (1, 1), "cfg_rev": (2, 6)}
    assert layouts.SIZES["awr.TargetLite32.v1"] == layouts.TARGET_LITE32.itemsize == 32


def test_target_lite32_golden_vectors():
    doc = load("golden/layouts/target_vectors.json")
    assert doc["schema_hash"] == layouts.SCHEMA_HASH["awr.TargetLite32.v1"]
    exp = {"person_static_unaware": (0x01, 0x80, 0x00, 0x00), "person_walk_eo_captured_relay": (0x01, 0x90, 0x1E, 0x00),
           "dog_reacting_physical_barking_lwir": (0x04, 0xFA, 0x31, 0x02)}
    assert {v["name"] for v in doc["vectors"]} == set(exp)
    for v in doc["vectors"]:
        raw = bytes.fromhex(v["hex"])
        assert len(raw) == 32
        assert (raw[2], raw[3], raw[22], raw[27]) == exp[v["name"]]
        a = np.frombuffer(raw, layouts.TARGET_LITE32)
        assert a.tobytes() == raw and int(a["class"][0]) == v["class"]
    dog = layouts.unpack_bits(layouts.TARGET_LITE32_BITS["state"], 0xFA)
    assert dog == {"awareness": enums.Awareness.REACTING, "criterion": enums.CriterionMode.PHYSICAL, "moving": 1, "occupied": 1, "sound_on": 1, "enabled": 1}


def test_fake_targets_rows_decode():
    ft = TS.FakeTargets(TS.TEMPLATE_T5)
    ft.step(2_500_000_000)
    ft.set_perception(0, 2, True, 1, relay=True)
    rows = np.frombuffer(ft.lite_rows().tobytes(), layouts.TARGET_LITE32)
    assert list(rows["agent_no"]) == [0, 1] and list(rows["class"]) == [enums.TargetClass.PERSON, enums.TargetClass.DOG]
    best = layouts.unpack_bits(layouts.TARGET_LITE32_BITS["best"], int(rows["best"][0]))
    assert best == {"level": 2, "captured": 1, "relay": 1, "sensor": enums.PerceptionSensor.EO}
    assert layouts.unpack_bits(layouts.TARGET_LITE32_BITS["flags"], int(rows["flags"][0]))["cfg_rev"] == 1
    assert abs(rows["speed_cms"][0] * layouts.TARGET_LITE32_SCALE["speed_cms"] - 1.4) < 0.01


# ---------------------------------------------------------------- enums (AWR-04 §11.6 rt/enums.json; AWR-17 §17.11)
def test_enums_append_only_and_d2_values():
    e = load("rt/enums.json")
    assert e["SensorKind"][:7] == ["camera", "lidar", "thermal", "radar", "gnss", "imu", "baro"]
    assert enums.SensorKind.ILLUMINATOR == 7 and enums.SensorKind.ACOUSTIC == 8
    assert e["Kind"][:6] == ["uav", "ugv", "robot", "vehicle", "sensor", "human"] and e["Kind"][-1] == "target"
    assert enums.SetpointFrame.FW == 2
    assert [m.name for m in enums.TargetClass] == ["UNKNOWN", "PERSON", "UAV", "MACHINE", "DOG", "VEHICLE", "OBJECT"]
    assert [m.name for m in enums.PerceptionSensor] == ["NONE", "EO", "NIR", "LWIR", "RADAR", "ACOUSTIC", "LIDAR"]
    assert enums.TARGETCLASSWIRE_BY_NAME["dog"] == enums.TargetClass.DOG
    for name in ("Awareness", "CriterionMode", "PerceptionLevel", "NestKind", "TaskType", "TaskState", "SandboxState", "FrameType", "LightBand"):
        assert name in e
    assert set(TS.CLASS_DEFAULTS["classes"]) <= {m.name.lower() for m in enums.TargetClass}


# ---------------------------------------------------------------- reasons 500-599 (AWR-17 §17.9, API-AC-058)
REASONS_D2 = {500: "SANDBOX_FULL", 501: "SANDBOX_RATE_LIMITED", 502: "SANDBOX_SPAWN_FAILED", 503: "SANDBOX_SCOPE", 504: "SANDBOX_QUOTA",
              505: "SANDBOX_ENTITY_LIMIT", 506: "SANDBOX_EXPIRED", 507: "SANDBOX_WORLD_FORBIDDEN", 508: "SANDBOX_WORLD_CAPACITY",
              509: "SANDBOX_CHALLENGE_REQUIRED", 510: "SANDBOX_PREEMPTED", 511: "SANDBOX_TICKET_INVALID", 512: "SANDBOX_SNAPSHOT_INVALID",
              513: "SANDBOX_STATE", 514: "SANDBOX_BATCH_BUSY", 520: "MODEL_UNKNOWN", 521: "VEHICLE_ENV_LIMIT", 522: "VEHICLE_CLONE_INVALID",
              523: "FW_AIRSPEED", 524: "VTOL_HOVER_LIMIT", 525: "NEST_INCOMPATIBLE", 540: "TARGET_PLACEMENT_INVALID", 541: "SOUND_PARAM_INVALID",
              542: "SENSITIVITY_PARAM_INVALID", 543: "TARGET_NOT_FOUND", 544: "MOTION_PARAM_INVALID", 560: "SENSOR_UNSUPPORTED",
              561: "SENSOR_PARAM_INVALID", 562: "LEVEL_UNREACHABLE", 563: "ILLUMINATOR_UNAVAILABLE", 580: "TASK_INFEASIBLE",
              581: "NO_FEASIBLE_STATION", 582: "NEST_EMPTY", 583: "KEEPOUT_VIOLATION", 584: "PLAN_REJECTED", 585: "PLAN_INPUT_TOO_LARGE",
              586: "PLAN_THROTTLED",
              # D2.1（17 §17.20.7）
              515: "SANDBOX_PARKED", 516: "IDENTITY_LOCKED", 517: "VIEWER_SEATS_FULL", 518: "IDLE_RECLAIMED",
              519: "VIEWER_TICKET_INVALID", 564: "FIELD_QUERY_INVALID"}


def test_reasons_500_599():
    got = {int(r): r.name for r in reasons.Reason if 500 <= int(r) <= 599}
    assert got == REASONS_D2
    rng = {(g["from"], g["to"]): g["category"] for g in load("rt/reasons.json")["ranges"]}
    assert {k: v for k, v in rng.items() if 500 <= k[0] <= 599} == {(500, 519): "sandbox", (520, 539): "catalog", (540, 559): "targets",
                                                            (560, 579): "perception", (580, 599): "tasking"}
    http = {c: reasons.REASONS[c].http for c in (501, 503, 506, 510, 511, 512, 520, 523, 586)}
    assert http == {501: None, 503: 403, 506: 410, 510: 410, 511: 410, 512: 422, 520: 404, 523: None, 586: 429}
    assert reasons.REASONS[120].name == "ENV_LIMIT" and reasons.REASONS[353].name == "VEHICLE_PROFILE_INVALID"  # 521/522 renamed, D1 kept
    assert {"SANDBOX_RATE_LIMITED", "KEEPOUT_CLAMPED", "VEHICLE_ENV_LIMIT", "PREVIEW_RECOMPUTED"} <= set(reasons.WARNINGS)
    assert reasons.WS_CLOSE["SANDBOX_GONE"] == 4410
    for r in load("rt/reasons.json")["reasons"]:
        if 500 <= r["code"] <= 599:
            assert r["d1"] == D2


# ---------------------------------------------------------------- topics (AWR-17 §17.7.2)
TOPICS_D2 = {"swarm/target/state": ("raw", "awr.TargetLite32.v1", 10, "M18"), "target/{id}/detail": ("msgpack", "awr.target.detail.v1", 2, "M18"),
             "uav/{id}/perception": ("msgpack", "awr.uav.perception.v1", 5, "M19"), "uav/{id}/payload": ("msgpack", "awr.uav.payload.v1", 2, "M13"),
             "uav/{id}/sensor/nav": ("msgpack", "awr.sensor.nav.v1", 10, "M13"),
             "uav/{id}/sensor/radar/tracks": ("msgpack", "awr.sensor.radar_tracks.v1", 5, "M19"),
             "uav/{id}/sensor/acoustic/bearings": ("msgpack", "awr.sensor.acoustic_bearings.v1", 2, "M19"),
             "uav/{id}/sensor/lidar/scan": ("blob", "awr.LidarScan8.v1", 2, "M13"), "nest/{id}/status": ("msgpack", "awr.nest.status.v1", 1, "M20"),
             "task/{tid}/status": ("msgpack", "awr.task.status.v1", 1, "M20"), "relay/{tid}/stats": ("msgpack", "awr.relay.stats.v1", 1, "M20"),
             "sandbox/session": ("msgpack", "awr.sandbox.session_state.v1", 1, "M17")}


def _schema_by_title() -> dict[str, str]:
    out = {}
    for p in sorted(CONTRACTS.rglob("*.schema.json")):
        if {"gen", "fixtures"} & set(p.relative_to(CONTRACTS).parts):
            continue
        title = json.loads(p.read_text(encoding="utf-8")).get("title", "")
        if title.startswith("awr."):
            out[title.split()[0]] = str(p.relative_to(CONTRACTS))
    return out


def test_topics_d2_and_schemas():
    by_pat = {t["pattern"]: t for t in load("rt/topics.json")["topics"]}
    titles = _schema_by_title()
    for pat, (enc, sn, hz, owner) in TOPICS_D2.items():
        t = by_pat[pat]
        assert (t["encoding"], t["schemaName"], t["native_hz"], t["owner"]) == (enc, sn, hz, owner), pat
        if enc == "raw":
            assert sn in layouts.DTYPES
        if enc == "msgpack":
            assert sn in titles, f"{pat}: no schema titled {sn}"
        assert topics.match_topic(pat.replace("{id}", "x1").replace("{tid}", "task-01")) is not None
    assert by_pat["sandbox/session"]["apis"] == ["sandbox"]
    for pat in ("uav/{id}/perception", "uav/{id}/sensor/nav", "uav/{id}/sensor/radar/tracks"):
        assert by_pat[pat]["interest"] is True


# ---------------------------------------------------------------- commands (AWR-17 §17.7.6, API-AC-057 contract part)
CMDS_D2 = {"loiter", "gimbal", "zoom", "sensor/mode", "fleet/update", "target/add", "target/update", "target/remove", "nest/add", "nest/update",
           "nest/remove", "task/create", "task/start", "task/pause", "task/resume", "task/cancel", "tasking/plan", "env/calendar"}


def test_commands_d2():
    svc = {s.op: s for s in commands.SERVICES}
    assert set(svc) >= CMDS_D2 and len(CMDS_D2) == 18
    for op in CMDS_D2:
        assert svc[op].d1 == D2
        Draft202012Validator.check_schema(svc[op].args_schema)
        assert "$ref" not in json.dumps(svc[op].args_schema), op       # api/rt/rpc.py validates without a registry
    assert commands.match_service("uav/p600-02/cmd/sensor/mode")[0].op == "sensor/mode"
    assert commands.match_service("task/task-01/start")[0].op == "task/start"
    assert commands.match_service("fleet/update")[0].op == "fleet/update"
    assert commands.match_service("fleet/cmd/rtl")[0].op == "fleet/cmd"
    vel = svc["velocity"].args_schema["properties"]
    assert vel["frame"]["enum"] == ["world", "body", "fw"] and vel["keepout_guard"]["enum"] == ["clamp", "warn", "off"]
    v = Draft202012Validator(svc["sensor/mode"].args_schema)
    assert not list(v.iter_errors({"sensor": "eo_gx40", "mode": "nir", "illum": {"ext": "on"}, "resolution": "4k"}))
    assert list(v.iter_errors({"sensor": "eo_gx40", "mode": "color"}))
    assert commands.admit_symbol("loiter", enums.FlightState.FLYING, 0)[0] == "Y"
    assert commands.admit_symbol("gimbal", enums.FlightState.LANDED, 0)[0] == "Y"


# ---------------------------------------------------------------- bus keys, events, rng, units
def test_bus_keys_d2():
    names = {k["name"] for k in load("bus/keys.json")["keys"]}
    assert {"SYS_SESSION", "EVT_SANDBOX_POOL", "STATE_SANDBOX_POOL", "CTL_PLAN", "CTL_TARGET", "CTL_TASKING", "CTL_SNAPSHOT", "STATE_TARGETS",
            "STATE_TARGET_DETAIL", "STATE_PERCEPTION", "STATE_NAV", "STATE_RADAR", "STATE_ACOUSTIC", "STATE_LIDAR", "STATE_PAYLOAD", "STATE_NESTS",
            "STATE_TASKS", "STATE_RELAY"} <= names
    assert {"SYS_SESSION", "EVT_SANDBOX_POOL", "STATE_SANDBOX_POOL"} == bus_keys.POOL_KEYS
    assert bus_keys.pool_key(bus_keys.sys_session("create")) == "awr/_sandbox/pool/sys/session/create"
    assert bus_keys.ctl_plan("submit") == "ctl/plan/submit" and bus_keys.STATE_TARGETS == "state/sim-core/targets"
    assert {"target", "perception", "nest", "task", "relay", "sandbox"} <= set(bus_keys.EVENT_CATEGORIES)


EVENTS_D2 = {"target.discovered", "target.state", "perception.level", "perception.capture", "perception.capture_lost", "perception.detect",
             "uav.keepout_guard", "fw.wind_exceeds", "nest.launch", "nest.recover", "nest.swap_done", "nest.dispatch_delayed", "task.created",
             "task.state", "task.allocated", "task.reallocated", "relay.dispatch", "relay.handover", "relay.gap", "sandbox.state",
             "sandbox.expiring", "sandbox.rate", "sandbox.slot_ready"}


def test_events_d2_registered_and_fake_covers_them():
    known = set(load("bus/event.schema.json")["$defs"]["known_kinds"]["enum"])
    assert len(EVENTS_D2) == 23 and known >= EVENTS_D2 and "session.switched" in known
    assert set(d2_synth.event_types()) | {d2_synth.SANDBOX_ONLY_EVENT} == EVENTS_D2


def test_rng_and_units_d2():
    assert rng_streams.Stream.TARGETS_MOTION == 17 and rng_streams.Stream.PERCEPTION == 18
    own = {s["name"]: s["owner"] for s in load("rt/rng_streams.json")["streams"]}
    assert own["targets_motion"] == "M18" and own["perception"] == "M19"
    for suf, unit in {"_db": "dB", "_dba": "dB(A)", "_dbm": "dBm", "_lx": "lx", "_px": "px", "_urad": "urad"}.items():
        assert units.SUFFIXES[suf][0] == unit


# ---------------------------------------------------------------- schemas: examples, negatives, vehicle 1.1
FIX = "fixtures/d2/vehicle/"


def test_vehicle_profile_schema_1_1():
    for p in sorted((ROOT / "vehicles").glob("*/params.yaml")):            # D1 files stay valid (ADR-115 relaxation)
        assert errors("vehicle/vehicle_profile.schema.json", yaml.safe_load(p.read_text(encoding="utf-8"))) == [], p
    f1 = yaml.safe_load((CONTRACTS / FIX / "awr_f1.params.yaml").read_text(encoding="utf-8"))
    assert errors("vehicle/vehicle_profile.schema.json", f1) == []
    bad = json.loads(json.dumps(f1))
    bad.pop("airframe")
    assert errors("vehicle/vehicle_profile.schema.json", bad)              # fixed wing needs airframe
    bad = dict(f1, motor={"value": 1})
    assert errors("vehicle/vehicle_profile.schema.json", bad)              # fixed wing must not carry multirotor fields
    bad = json.loads(json.dumps(f1))
    bad.pop("acoustic")
    assert errors("vehicle/vehicle_profile.schema.json", bad)              # catalog models carry signatures
    p600 = yaml.safe_load((ROOT / "vehicles/p600/params.yaml").read_text(encoding="utf-8"))
    bad = dict(p600)
    bad.pop("motor")
    assert errors("vehicle/vehicle_profile.schema.json", bad)              # multirotor needs motor
    for f in ("sensor_model_eo_gx40.yaml", "mount_awr_f1_eo_pod.yaml"):
        assert errors("sensor/eo_zoom.schema.json", yaml.safe_load((CONTRACTS / FIX / f).read_text(encoding="utf-8"))) == [], f


def test_class_defaults_and_critical_dim_anchors():
    cd = load("target/class_defaults.json")
    assert set(cd["classes"]) == {"person", "uav", "machine", "dog", "vehicle", "object"}
    for name, c in cd["classes"].items():
        assert errors("target/target.schema.json", c) == [], name
    s = cd["classes"]["person"]["size_m"]
    person = [s["l"], s["w"], s["h"]]
    assert abs(TS.critical_dim(person, 0.0, 0.0, worst=True) - 0.7246) < 0.0072
    assert abs(TS.critical_dim(person, math.pi / 2, 0.0) - 0.3873) < 0.0039
    s = cd["classes"]["vehicle"]["size_m"]
    car = [s["l"], s["w"], s["h"]]
    for beta, exp in ((0.0, 1.643), (math.pi / 2, 2.598)):
        assert abs(TS.critical_dim(car, 0.0, beta) - exp) / exp < 0.01
    assert abs(TS.critical_dim(car, math.pi / 2, 0.0) - 2.846) / 2.846 < 0.01


def test_target_create_requires_class_and_pos():
    from ctlib import registry

    v = Draft202012Validator({"$ref": "https://schemas.anet-drone.dev/target/1/target.schema.json#/$defs/create_request"}, registry=registry())
    assert not list(v.iter_errors({"class": "person", "pos": [1.0, 2.0, None]}))
    assert list(v.iter_errors({"class": "person"}))
    assert list(v.iter_errors({"class": "cat", "pos": [0, 0]}))
    assert list(v.iter_errors({"class": "person", "pos": [0, 0], "sensitivity": [{"kind": "visual", "shape": "sector", "radius_max_m": 1}] * 5}))


def test_perception_levels():
    lv = load("sensor/perception_levels.json")
    for k, v in lv["levels"].items():
        assert abs(v["n_p90_px"] - lv["ttpf"]["ratio_at_p_definite"] * v["n50_px"]) < 1e-9, k
        assert v["n50_px"] == lv["pixels_per_cycle"] * v["n50_cycles"]
    assert abs(lv["k_mor"] - math.log(20.0)) < 1e-12


def test_keepout_and_state_ext_and_env_examples():
    ft = TS.FakeTargets(TS.TEMPLATE_T5)
    st = ft.states()
    for i, c in enumerate(ft.cfgs):
        for k, vol in enumerate(TS.keepout_volumes(c, st[i], "geometric", 20.06)):
            assert errors("target/keepout.schema.json", vol.to_wire(f"{c.id}/{k}")) == []
    moving = [v.shape for v in TS.keepout_volumes(ft.cfgs[0], st[0], "geometric", 20.0)]
    assert "sector" not in moving                                           # moving targets: sectors become cylinders
    ext = {"lifecycle": "READY", "lease": {"owner": "MISSION"}, "loc": {"status": "TRACKING"}, "battery": None, "mission": None,
           "home_enu_m": None, "link": {}, "gcs_loss_policy": "hold_rtl", "model_id": "awr_v1", "label": "V1-01", "nest_id": "nest-01",
           "work": {"tid": "task-02", "wid": "task-02-w1", "kind": "station"},
           "fw": {"mode": "FW", "airspeed_mps": 15.0, "bank_rad": 0.3, "gamma_rad": 0.0, "hover_used_s": 40.0},
           "manual": {"active": False, "keepout_guard": "clamp"}, "env_limit_warn": False}
    assert errors("rt/payloads/uav_state_ext.schema.json", ext) == []
    import fake_gw

    kf = fake_gw.Synth(1, "synthcity").keyframe(1, 0, None, "clear")
    kf["config"]["sun"].update({"mode": "clock", "illum_lx": 3.0, "band": "low"})
    kf.update({"calendar": {"start_utc": "2026-10-05T10:00:00Z", "tz": "Asia/Shanghai", "t_cal_unix_ns": "1791194400000000000"},
               "humidity_pct": 70.0, "surface": {"t_c": 18.0, "emissivity": 0.95}, "acoustic_bg": {"profile": "rural", "l_bg_dba": 30.0}})
    assert errors("env/env_state.schema.json", kf) == []
    kf["sun"] = {}
    assert errors("env/env_state.schema.json", kf)                          # no top-level sun: the D2 fields extend config.sun


def test_roster_target_entries():
    d1 = {"agent_no": 0, "id": "uav0001", "kind": "uav", "model": "p600", "profile_id": "p600_mid360", "backend": "mock", "simulated": True,
          "producer": "sim-core", "lifecycle": "READY", "sensors": [], "t_world_local": None, "caps_ref": "mock"}
    tgt = {"agent_no": 0, "id": "person-01", "kind": "target", "model": "person", "label": "人-01"}
    assert errors("rt/payloads/fleet_roster.schema.json", {"roster_version": 1, "entries": [d1, tgt]}) == []
    assert errors("rt/payloads/fleet_roster.schema.json", {"roster_version": 1, "entries": [{k: v for k, v in d1.items() if k != "backend"}]})
    assert errors("rt/payloads/fleet_roster.schema.json", {"roster_version": 1, "entries": [{k: v for k, v in tgt.items() if k != "model"}]})


def test_interest_streams():
    msg = {"v": 1, "seq": 3, "detail": [0, 1], "marks": [], "topics": ["perception", "nav", "lidar"], "streams": {"lidar": [1], "nav": [0, 1]}}
    assert errors("bus/interest.schema.json", msg) == []
    assert errors("bus/interest.schema.json", {**msg, "streams": {"lidar": [0, 1]}})   # one lidar stream per session


REST = {"meta": "sandbox/session.schema.json#meta", "worlds": "#worlds", "templates": "#templates", "session_created": "#created",
        "session": "sandbox/session.schema.json", "snapshot": "sandbox/snapshot.schema.json", "targets": "target/target.schema.json#list",
        "nests": "tasking/nest.schema.json#list", "target_detail": "rt/payloads/target_detail.schema.json", "perception": "sensor/perception.schema.json",
        "relay_stats": "tasking/relay_stats.schema.json"}


@pytest.mark.parametrize("name", sorted(REST))
def test_rest_examples_validate(name: str):
    from ctlib import registry

    ref = REST[name]
    file, _, frag = ref.partition("#")
    file = file or "sandbox/session.schema.json"
    sid = load(file)["$id"]
    target = {"$ref": sid + (f"#/$defs/{frag}" if frag else "")}
    v = Draft202012Validator(target, registry=registry())
    inst = load(f"fixtures/d2/rest/{name}.json")
    errs = [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:160]}" for e in v.iter_errors(inst)]
    assert errs == []
