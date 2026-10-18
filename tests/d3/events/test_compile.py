"""事件编译（M23 §6.3–§6.6、§6.11；AWR-05 §5.2）：门 1 错误目录、门 2 预检与排时建议、生成的状态与几何、贴地对拍与确定性。

合成世界：300 m × 300 m 平地，盒子（20 × 20 × 30 m）位于 world (30..50, −50..−30)。
"""

from __future__ import annotations

import copy
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")
pytest.importorskip("PIL")
pytest.importorskip("zstandard")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world"))
from awr.sim.events import tape as tp
from awr.sim.events.compile import CompileOptions, compile_event

ROOT = Path(__file__).resolve().parents[3]


def _sq(cx: float, cy: float, r: float) -> list[list[float]]:
    return [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]]


OVERLAY = {"schema": "awr.anchors.v1", "extends": "world", "world_id": "synth-glb", "anchors": [
    {"id": "ev_west", "kind": "point", "profile": "person", "xy": [-60.0, -40.0], "seed": True},
    {"id": "ev_east", "kind": "point", "profile": "person", "xy": [100.0, -40.0]},
    {"id": "ev_south", "kind": "point", "profile": "person", "xy": [0.0, -70.0]},
    {"id": "ev_north", "kind": "point", "profile": "person", "xy": [0.0, -10.0]},
    {"id": "ev_door", "kind": "door", "profile": "person", "xy": [28.0, -40.0], "building": "ev_box"},
    {"id": "ev_plaza", "kind": "zone", "profile": "person", "polygon": _sq(0.0, 60.0, 10.0)},
    {"id": "ev_curb", "kind": "curb", "profile": "person", "xy": [60.0, 60.0], "pair": "ev_road"},
    {"id": "ev_road", "kind": "point", "profile": "vehicle", "xy": [64.0, 60.0]},
    {"id": "ev_road_w", "kind": "point", "profile": "vehicle", "xy": [-100.0, 100.0]},
    {"id": "ev_road_e", "kind": "point", "profile": "vehicle", "xy": [120.0, 100.0]},
    {"id": "ev_kiosk", "kind": "point", "profile": "person", "xy": [-20.0, 20.0]},
    {"id": "ev_town", "kind": "zone", "profile": "person", "polygon": _sq(0.0, 0.0, 130.0)},
]}

EVENT = {
    "schema": "drone4d.event.v1", "event_id": "synth-handoff", "version": 1, "world": {"id": "synth-glb"}, "anchors_ref": "event:anchors.json",
    "seed": 11, "duration_s": 150,
    "actors": [
        {"id": "person_a", "class": "person", "appearance": {"top_rgb": [42, 93, 176], "height_m": 1.76},
         "route": [{"t": 0, "at": "ev_west", "do": "spawn"}, {"t": 140, "at": "ev_east"}]},
        {"id": "person_b", "class": "person", "route": [{"t": 0, "at": "ev_plaza", "do": "spawn", "dwell_s": 20},
                                                       {"t": 80, "at": "ev_curb", "do": "board", "target": "vehicle_c"}]},
        {"id": "person_c", "class": "person", "route": [{"t": 0, "at": "ev_south", "do": "spawn"}, {"t": 40, "at": "ev_door", "do": "enter_building"},
                                                       {"t": 70, "at": "ev_door", "do": "exit_building"}, {"t": 110, "at": "ev_north", "do": "despawn"}]},
        {"id": "vehicle_c", "class": "vehicle", "route": [{"t": 0, "at": "ev_road_w", "do": "spawn"}, {"t": 60, "at": "ev_road", "do": "wait", "dwell_s": 40},
                                                         {"t": 130, "at": "ev_road_e", "do": "despawn"}]},
        {"id": "kiosk", "class": "machine", "route": [{"t": 0, "at": "ev_kiosk", "do": "spawn"}]},
    ],
    "background": [{"class": "person", "count": 12, "zone": "ev_town", "pause_s": [0, 5]}],
    "environment": {"keyframes": [{"t": 0, "wind_mps": 2.0}, {"t": 100, "wind_mps": 6.0, "wind_dir_deg": 300, "rain_mm_h": 2.0}]},
    "comms": {"zones": [{"id": "jam", "polygon": [[-10, -10], [10, -10], [10, 10], [-10, 10]], "t": [30, 60], "extra_loss": 0.4}]},
}


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory):
    from synthworld import build_synth_world

    tmp = tmp_path_factory.mktemp("w")
    mp = pytest.MonkeyPatch()
    yield build_synth_world(tmp, mp), tmp
    mp.undo()


def _compile(world, tmp: Path, ev: dict | None = None, name: str = "out", **kw):
    w, _ = world
    d = tmp / name
    d.mkdir(exist_ok=True)
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev or EVENT))
    return compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache", **kw))


def _codes(r) -> list[str]:
    return [e["code"] for g in r.gates for e in g["errors"]]


def test_gate1_catalogue(world, tmp_path: Path) -> None:
    ev = copy.deepcopy(EVENT)
    ev["actors"][0]["route"][1]["at"] = "nowhere"
    ev["actors"][1]["route"][1]["target"] = "person_a"
    ev["actors"][2]["route"][0]["do"] = "pass"
    ev["actors"][3]["route"][2]["t"] = 50
    ev["actors"].append(dict(ev["actors"][4]))
    r = _compile(world, tmp_path, ev)
    assert r.status == "failed" and r.reason == 611
    assert set(_codes(r)) >= {"E-ANCHOR-UNKNOWN", "E-BOARD-TARGET", "E-FIRST-WAYPOINT", "E-TIME-ORDER", "E-DUP-ID"}
    assert {e["path"] for e in r.gates[0]["errors"] if e["code"] == "E-ANCHOR-UNKNOWN"} == {"/actors/0/route/1/at"}
    ev = copy.deepcopy(EVENT)
    ev["mode"] = "interactive"
    r = _compile(world, tmp_path, ev)
    assert r.reason == 619 and _codes(r) == ["E-MODE"]
    ev = copy.deepcopy(EVENT)
    ev["actors"][0]["speed"] = 3
    r = _compile(world, tmp_path, ev)
    assert _codes(r) == ["E-SCHEMA"] and r.gates[0]["errors"][0]["path"] == "/actors/0"


def test_gate2_infeasible_with_retime_suggestions(world, tmp_path: Path) -> None:
    ev = copy.deepcopy(EVENT)
    ev["actors"][0]["route"][1]["t"] = 60                                         # 约 162 m / 60 s > 1.8 m/s
    r = _compile(world, tmp_path, ev, preflight_only=True)
    assert r.status == "infeasible" and r.reason == 612 and "I-SPEED" in _codes(r)
    seg = next(s for s in r.segments if s["actor"] == "person_a")
    assert seg["feasible"] is False and seg["why"] == "speed" and 160.0 < seg["len_m"] < 175.0
    sug = r.retime["actors"]["person_a"]
    assert sug[0] == 0.0 and sug[1] > 60.0 and abs(sug[1] * 10 - round(sug[1] * 10)) < 1e-9          # 100 ms 网格
    from awr.sim.events.gen import t_min

    assert sug[1] >= t_min(seg["len_m"], 1.0, 1.8) - 1e-9
    ev["actors"][0]["route"][1]["t"] = sug[1]
    assert _compile(world, tmp_path, ev, preflight_only=True).status == "preflight"     # 按建议时刻可行
    ev = copy.deepcopy(EVENT)
    ev["actors"][1]["route"][1]["t"] = 110                                        # 车辆窗口 [60, 100] 之后
    r = _compile(world, tmp_path, ev, preflight_only=True)
    assert "I-BOARD" in _codes(r) and r.retime["vehicle_dwell_ext_s"]["vehicle_c"] == pytest.approx(15.0)


@pytest.fixture(scope="module")
def compiled(world):
    _w, tmp = world
    r = _compile(world, tmp, name="main")
    assert r.status == "compiled", json.dumps(r.as_dict(), ensure_ascii=False)[:3000]
    return r, tp.open_tape(r.out)


def test_compiled_tape_states_and_geometry(compiled) -> None:
    r, t = compiled
    hd = t.header
    ids = [a["id"] for a in hd["actors"]]
    assert ids[:5] == ["vehicle_c", "kiosk", "person_a", "person_b", "person_c"] and t.n_scripted == 5 and t.n_actors == 17
    assert hd["n_ticks"] == 1500 and hd["world"]["navmesh_sha256"]["person"] != hd["world"]["navmesh_sha256"]["vehicle"]
    f = np.concatenate([t.block_frames(i) for i in range(len(t.blocks))])
    a, b, c, v, k = (ids.index(n) for n in ("person_a", "person_b", "person_c", "vehicle_c", "kiosk"))
    xy = np.stack([f["x_mm"], f["y_mm"]], -1) / 1000.0
    # person_a 绕开盒子并按时到达
    pa = xy[:, a]
    inside_box = (pa[:, 0] > 30.2) & (pa[:, 0] < 49.8) & (pa[:, 1] > -49.8) & (pa[:, 1] < -30.2)
    assert not inside_box.any()
    assert np.allclose(pa[0], [-60.0, -40.0], atol=1e-3) and np.allclose(pa[1400], [100.0, -40.0], atol=1e-3)
    sp = np.hypot(f["vx_cmps"][:, a], f["vy_cmps"][:, a]) / 100.0
    assert sp.max() <= 1.8 + 0.01 and (f["state"][1:1400, a] == 1).all() and (f["state"][1400:, a] == 2).all()
    # person_b：zone 站位停留 → 移动 → 上车（容器 = 车辆 idx）→ 车辆离场后 GONE
    sb = f["state"][:, b]
    assert (sb[:200] == 2).all() and (sb[200:800] == 1).all() and (sb[800:1300] == 3).all() and (sb[1300:] == 5).all()
    assert (f["container"][800:1300, b] == v).all() and np.array_equal(f["x_mm"][800:1300, b], f["x_mm"][800:1300, v])
    assert (f["container"][:800, b] == tp.NO_CONTAINER).all()
    # person_c：进入建筑 INSIDE，40–70 s；随后出门、离场
    sc = f["state"][:, c]
    assert (sc[400:700] == 4).all() and (sc[700:1100] == 1).all() and (sc[1100:] == 5).all()
    assert np.allclose(xy[400, c], xy[700, c]) and np.hypot(*(xy[400, c] - [28.0, -40.0])) < 3.0
    # 车辆：等待窗口与可行速度（0.7 × 11 m/s）
    assert (f["state"][600:1000, v] == 2).all() and (f["state"][1300:, v] == 5).all()
    assert (np.hypot(f["vx_cmps"][:, v], f["vy_cmps"][:, v]) / 100.0).max() <= 7.7 + 0.01
    # 静止类别、贴地与竖直速度
    assert (f["state"][:, k] == 2).all() and len(set(f["x_mm"][:, k])) == 1
    z = f["z_mm"] / 1000.0
    vis = np.isin(f["state"], (1, 2))
    assert np.ptp(z[vis]) < 0.02 and (f["vz_cmps"][vis] == 0).all() and (f["vz_cmps"][~vis] == 0).all()
    # 背景在 zone 内移动、不与脚本列耦合
    bg = xy[:, 5:][vis[:, 5:]]
    assert (np.abs(bg) <= 130.5).all() and (f["state"][:, 5:] == 1).mean() > 0.5
    rep = json.loads((r.out / "tape.json").read_text())["report"]
    assert rep["min_dist_m"]["scripted_scripted"] is not None and rep["boards"][0]["t_board_s"] == 80.0
    assert "W-ENV-CLOUD" in rep["warnings"]


def test_header_env_track_and_contracts(compiled) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource

    r, t = compiled
    hd = t.header
    trk = hd["env_track"]
    assert trk[0] == {"t_ms": 0, "op": {"kind": "set", "mode": "step", "duration_s": 0, "patch": trk[0]["op"]["patch"]}}
    assert trk[0]["op"]["patch"]["wind"]["speed_ref_mps"] == 2.0 and trk[1]["op"]["mode"] == "smooth" and trk[1]["op"]["duration_s"] == 100.0
    assert trk[1]["op"]["patch"]["cloud"]["cover"] == 0.9 and trk[1]["op"]["patch"]["wind"]["dir_from_deg"] == 300.0
    ev = ROOT / "packages/contracts/event"
    th = json.loads((ev / "tape.v1.schema.json").read_text())
    tm = json.loads((ev / "tape_manifest.v1.schema.json").read_text())
    jsonschema.Draft202012Validator(th).validate(hd)
    reg = Registry().with_resources([(th["$id"], Resource.from_contents(th))])
    jsonschema.Draft202012Validator(tm, registry=reg).validate(json.loads((r.out / "tape.json").read_text()))
    assert {p.name for p in r.out.iterdir()} == {"event.json", "plan.json", "anchors.json", "tape.bin", "tape.json", "compile_report.json"}
    assert r.tape["bytes_per_actor_s"] <= 25.0
    assert t.comm_zones_at(29_999_999_999) == [] and t.comm_zones_at(70_000_000_000) == []
    (z,) = t.comm_zones_at(30_000_000_000)
    assert z["id"] == "jam" and z["extra_loss"] == 0.4 and z["polygon_m"].shape == (4, 2) and z["t_to_ns"] == 60_000_000_000
    info = {a["id"]: a for a in t.actors_info()}
    assert info["person_a"]["size_m"] == (0.5, 0.3, 1.76) and info["person_a"]["pos0_enu_m"][0] == pytest.approx(-60.0)
    assert [a["id"] for a in t.actors_info(scripted_only=True)] == [a["id"] for a in hd["actors"][:5]]


def test_env_at_follows_m07_transitions(compiled) -> None:
    """环境轨经 M07 求值：首帧阶跃生效，平滑过渡在 100 s 内单调到达终值，风矢量为来向的反向。"""
    _r, t = compiled
    e0, e1, e_end = t.env_at(0), t.env_at(100_000_000_000), t.env_at(150_000_000_000)
    assert e0.wind_speed_mps == 2.0 and e0.rain_mm_h == 0.0 and np.allclose(e0.wind_enu_mps[:2], [2.0, 0.0], atol=1e-12)    # 来自正西
    assert e1.wind_speed_mps == pytest.approx(6.0) and e1.wind_dir_from_deg == pytest.approx(300.0) and e1.rain_mm_h == pytest.approx(2.0)
    assert e1.cloud == pytest.approx(0.9) and e_end.wind_speed_mps == pytest.approx(6.0)
    sp = [t.env_at(k * 10_000_000_000).wind_speed_mps for k in range(11)]
    assert all(b >= a for a, b in itertools.pairwise(sp)) and 2.0 < sp[5] < 6.0
    assert t.env_at(-5).wind_speed_mps == 2.0 and t.env_at(50_000_000_000).scalars.shape == (21,)


def test_determinism_and_background_decoupling(world, compiled) -> None:
    r0, t0 = compiled
    _w, tmp = world
    r1 = _compile(world, tmp, name="again")
    assert r1.tape["content_sha256"] == r0.tape["content_sha256"]
    ev = copy.deepcopy(EVENT)
    ev["background"][0]["count"] = 4
    r2 = _compile(world, tmp, ev, name="fewer")
    t2 = tp.open_tape(r2.out)
    f0 = np.concatenate([t0.block_frames(i, background=False) for i in range(len(t0.blocks))])
    f2 = np.concatenate([t2.block_frames(i, background=False) for i in range(len(t2.blocks))])
    assert np.array_equal(f0, f2) and r2.tape["content_sha256"] != r0.tape["content_sha256"]


def test_ground_z_reference_matches_batch_bitwise(world) -> None:
    from awr.sim.events.ground import build_ground_index
    from awr.world.mesh.query import open_mesh_world_query

    w, tmp = world
    q = open_mesh_world_query(w, tmp / "cache")
    gi = build_ground_index(q, (-140.0, -140.0, 140.0, 140.0))
    rng = np.random.default_rng(3)
    xy = rng.uniform(-145, 145, (20000, 2))
    zh = np.where(rng.random(20000) < 0.8, rng.normal(0.0, 0.6, 20000), rng.uniform(28.0, 31.0, 20000))   # 地面层与盒顶层
    z, found = gi.ground_z_batch(xy, zh)
    for i in range(0, 20000, 7):
        ref = gi.ground_z_ref(float(xy[i, 0]), float(xy[i, 1]), float(zh[i]))
        assert (ref is None) == (not found[i]) and (ref is None or ref == z[i])
    assert found.mean() > 0.7 and np.abs(z[found & (zh < 5)]).max() < 1e-9
