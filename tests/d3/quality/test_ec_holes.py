"""EC 分母剔除洞内事件（M27-FR-089、M27-AC-020；M23 §6.9.3、M23-FR-038、M23-AC-018；ADR-131；D3-AC-04 "构造发生在洞内的事件，
EC 分母剔除该事件并在报告中单列"）。

单元用例核对剔除规则（`awr.bench.eval.ec.exclusions`）与 EC、覆盖的分母；端到端用例在带洞世界上走完整链路：
GlbPipeline 构建 → quality.geojson 的 hole 多边形 → 事件编译门 3 标记 `in_incomplete_region` → Env 运行（运动学开发档）→
`evaluate_run` 的 `excluded[]` 与 EC 分母。

带洞世界沿用 `test_quality_layer` 的夹具（300 m 平地、缺角、20 m 内洞、封闭盒子；不带 OSM 水面），另加：
- 洞内补片：洞内 19.8 m × 19.8 m 的地面片，与洞缘留 0.1 m 缝，不与地面焊接，是独立连通分量（扫描网格常见的未缝合补片）。
  quality 的 hole 位按地面的开放边界环内部置位，补片在 hole 多边形内；1 m 可走面栅格在格中心采样，缝不落在格中心上，
  行人可以走进洞内；
- 洞内小屋：补片上的 7 m × 10 m × 6 m 封闭盒子，西墙外 2 m 为门 `ev_door_h`。

事件：person_c 在洞内经 `ev_door_h` 进入小屋再出来（进入、离开两条注释均在洞内）；person_d 经大盒子西侧的 `ev_door_m` 进入后
留在室内到运行结束（进入为洞外事件，另一条室内时段的注释用于 `inside_building`）。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from awr.bench.eval.ec import EXCLUDE_REASONS, event_capture, event_truth_rows, exclusions

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "world"))
sys.path.insert(0, str(HERE.parents[0] / "events"))
sys.path.insert(0, str(HERE.parents[0] / "env"))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "sdk" / "drone4d" / "src"))

GAP = 0.1                                                  # 补片与洞缘的缝（m）
HUT = ((-55.0, -48.0), (45.0, 55.0), 6.0)                  # 小屋：世界 E 区间、N 区间、高度
DOOR_H = (-57.0, 50.0)                                     # 洞内门（小屋西墙外 2 m）
DOOR_M = (28.0, 0.0)                                       # 洞外门（大盒子西墙外 2 m）


# ---------------------------------------------------------------- 单元：剔除规则与分母


def _ev(eid: str, a: float, b: float, parts=("a",), typ: str = "enter_building") -> dict:
    return {"id": eid, "type": typ, "participants": list(parts), "a_s": a, "b_s": b, "multi": len(parts) > 1, "params": {}}


def _row(ann: str | None, inc: bool, eid: str | None = None) -> dict:
    r = {"id": eid or ann or "e001", "type": "enter_building", "participants": ["a"], "interval_ms": [0, 0], "declared": ann is not None,
         "salience": 1.0, "in_incomplete_region": inc, "evidence": {}}
    if ann is not None:
        r["annotation"] = ann
    return r


def test_exclusion_rules_and_reason_order() -> None:
    n = 100
    E = [_ev("hole", 2.0, 2.0), _ev("twice", 3.0, 3.0), _ev("open", 4.0, 4.0), _ev("unverified", 5.0, 5.0), _ev("inside", 6.0, 6.0, ("b",)),
         _ev("hole_inside", 6.0, 6.0, ("b",))]
    rows = [_row("hole", True), _row("twice", True), _row("twice", False, "twice-2"), _row("open", False), _row(None, True),
            _row("hole_inside", True)]
    present = {"a": np.ones(n + 1, bool), "b": np.ones(n + 1, bool)}
    present["b"][30:] = False
    inside = {"a": np.zeros(n + 1, bool), "b": ~present["b"]}
    E2, excl = exclusions(E, rows, present, n, inside=inside)
    assert excl == [{"event_id": "hole", "reason": "incomplete_region"}, {"event_id": "inside", "reason": "inside_building"},
                    {"event_id": "hole_inside", "reason": "incomplete_region"}]               # 两种原因同时成立记 incomplete_region
    assert [e.get("excluded") for e in E2] == ["incomplete_region", None, None, None, "inside_building", "incomplete_region"]
    assert all("excluded" not in e for e in E)                                                # 不改动输入
    assert {x["reason"] for x in excl} <= set(EXCLUDE_REASONS)
    # 任一命中可观测即照常计分；未声明真值行（无 annotation）不参与；无真值时不按不完整区域剔除
    _E3, excl3 = exclusions(E, None, present, n, inside=inside)
    assert excl3 == [{"event_id": "inside", "reason": "inside_building"}, {"event_id": "hole_inside", "reason": "inside_building"}]
    # 不在场但不在建筑内（例如尚未出生或在车内）不按 inside_building 剔除
    _E4, excl4 = exclusions(E, None, present, n, inside={"a": np.zeros(n + 1, bool), "b": np.zeros(n + 1, bool)})
    assert excl4 == []
    # 窗口内曾在场（[a − 2 s, b + 2 s] 与在场时段相交）不剔除
    _E5, excl5 = exclusions([_ev("edge", 4.5, 4.5, ("b",))], None, present, n, inside=inside)
    assert excl5 == []


def test_event_capture_drops_excluded_events_from_the_denominator() -> None:
    n = 100
    E = [_ev("seen", 2.0, 2.0), _ev("hole", 5.0, 5.0)]
    ident = {k: {("u1", "a")} for k in range(5, 25)}                                          # 只在 [0.5, 2.4] s 识别
    present = {"a": np.ones(n + 1, bool)}
    ec0, cov0, out0 = event_capture(E, ["u1"], ident, present, n)
    assert ec0 == 0.5 and set(out0) == {"seen", "hole"} and out0["hole"]["captured"] is False
    E2, excl = exclusions(E, [_row("hole", True)], present, n)
    assert excl == [{"event_id": "hole", "reason": "incomplete_region"}]
    ec, cov, out = event_capture(E2, ["u1"], ident, present, n)
    assert ec == 1.0 and set(out) == {"seen"}                                                 # 分母只剩洞外事件
    assert cov == pytest.approx(out0["seen"]["coverage"]) and cov > cov0
    # 全部被剔除时没有计分事件，EC 与覆盖记 0（与无计分事件同一口径）
    assert event_capture([dict(e, excluded="incomplete_region") for e in E], ["u1"], ident, present, n)[:2] == (0.0, 0.0)


def test_inside_ticks_do_not_count_in_coverage() -> None:
    """离开建筑（瞬时，b = 3 s）：覆盖区间 [1, 3] s 中参与者在 [1, 2.9] s 处于 INSIDE，这些 tick 不计入覆盖分母。"""
    n = 60
    E = [_ev("exit", 3.0, 3.0, typ="exit_building")]
    present = {"a": np.zeros(n + 1, bool)}
    present["a"][30:] = True
    inside = {"a": ~present["a"]}
    ident = {k: {("u1", "a")} for k in range(30, 40)}
    _ec, cov_plain, out_plain = event_capture(E, ["u1"], ident, present, n)
    ec, cov, out = event_capture(E, ["u1"], ident, present, n, inside=inside)
    assert ec == 1.0 and out["exit"]["captured"] and out_plain["exit"]["captured"]
    assert cov_plain == pytest.approx(1 / 21) and cov == 1.0                                  # 21 个 tick → 只剩出门的 1 个


def test_event_truth_rows_reads_the_latest_revision(tmp_path: Path) -> None:
    assert event_truth_rows(tmp_path) is None
    for rev, inc in ((1, False), (2, True)):
        d = tmp_path / "truth" / f"r{rev}"
        d.mkdir(parents=True)
        (d / "event_truth.json").write_text(json.dumps({"events": [_row("x", inc)]}))
    (tmp_path / "truth" / "rX").mkdir()
    assert [r["in_incomplete_region"] for r in event_truth_rows(tmp_path)] == [True]


# ---------------------------------------------------------------- 端到端：带洞世界


def _box(x: tuple[float, float], z: tuple[float, float], h: float) -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    """glTF 坐标的封闭盒子（顶、四壁、底，外法线）：→ (8 × 3 顶点，三角形)。"""
    bx = np.array([[xx, yy, zz] for yy in (0.0, h) for zz in z for xx in x])
    quads = [(4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5), (0, 2, 3, 1)]
    c0 = bx.mean(0)
    F = []
    for q in quads:
        for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3])):
            p = bx[list(t)]
            nrm = np.cross(p[1] - p[0], p[2] - p[0])
            F.append(t if nrm @ (p.mean(0) - c0) > 0 else t[::-1])
    return bx, F


def _hole_scene_factory(base):
    import test_quality_layer as tql

    def scene() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        V, F, UV = base()
        h0, h1 = tql.HOLE
        V, F, UV = [V], [F], [UV]
        n = sum(len(v) for v in V)
        # 洞内补片（法线 +Y，绕向与地面相同）
        x0, x1, z0, z1 = h0 + GAP, h1 - GAP, h0 + GAP, h1 - GAP
        P = np.array([[x0, 0.0, z0], [x1, 0.0, z0], [x1, 0.0, z1], [x0, 0.0, z1]])
        V.append(P)
        UV.append((P[:, [0, 2]] + tql.SIZE / 2) / tql.SIZE)
        F.append(np.array([[n, n + 2, n + 1], [n, n + 3, n + 2]]))
        n += 4
        # 小屋（世界 N = −glTF z）
        (e0, e1), (n0, n1), hh = HUT
        bx, bf = _box((e0, e1), (-n1, -n0), hh)
        V.append(bx)
        UV.append(np.full((8, 2), 0.5))
        F.append(np.asarray(bf) + n)
        return (np.vstack(V).astype(np.float32), np.vstack(F).astype(np.uint32), np.vstack(UV).astype(np.float32))

    return scene


def _sq(cx: float, cy: float, r: float) -> list[list[float]]:
    return [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]]


def _event(world_id: str) -> tuple[dict, dict]:
    overlay = {"schema": "awr.anchors.v1", "extends": "world", "world_id": world_id, "anchors": [
        {"id": "ev_seed", "kind": "point", "profile": "person", "xy": [0.0, 30.0], "seed": True},
        {"id": "ev_west", "kind": "point", "profile": "person", "xy": [-90.0, 50.0]},
        {"id": "ev_north", "kind": "point", "profile": "person", "xy": [-45.0, 75.0]},
        {"id": "ev_south", "kind": "point", "profile": "person", "xy": [0.0, -30.0]},
        {"id": "ev_door_h", "kind": "door", "profile": "person", "xy": list(DOOR_H), "building": "ev_hut"},
        {"id": "ev_door_m", "kind": "door", "profile": "person", "xy": list(DOOR_M), "building": "ev_box"},
        {"id": "ev_area", "kind": "zone", "profile": "person", "polygon": _sq(-20.0, 30.0, 80.0)},
    ]}
    ev = {"schema": "drone4d.event.v1", "event_id": "synth-hole-enter", "version": 1, "world": {"id": world_id}, "anchors_ref": "event:anchors.json",
          "seed": 23, "duration_s": 90,
          "actors": [
              {"id": "person_c", "class": "person", "appearance": {"top_rgb": [42, 93, 176], "height_m": 1.76},
               "route": [{"t": 0, "at": "ev_west", "do": "spawn"}, {"t": 30, "at": "ev_door_h", "do": "enter_building"},
                         {"t": 50, "at": "ev_door_h", "do": "exit_building"}, {"t": 85, "at": "ev_north", "do": "despawn"}]},
              {"id": "person_d", "class": "person",
               "route": [{"t": 0, "at": "ev_south", "do": "spawn"}, {"t": 30, "at": "ev_door_m", "do": "enter_building"}]}],
          "environment": {"keyframes": [{"t": 0, "wind_mps": 2.0}]},
          "annotations": [
              {"id": "enter_h", "type": "enter_building", "participants": ["person_c"], "window": [20, 40], "params": {"anchor": "ev_door_h"}},
              {"id": "exit_h", "type": "exit_building", "participants": ["person_c"], "window": [40, 60], "params": {"anchor": "ev_door_h"}},
              {"id": "enter_m", "type": "enter_building", "participants": ["person_d"], "window": [20, 40], "params": {"anchor": "ev_door_m"}},
              # 室内时段的示意声明（不会被验证）：窗口内 person_d 始终在室内 → inside_building
              {"id": "stay_m", "type": "exit_building", "participants": ["person_d"], "window": [62, 68], "params": {"anchor": "ev_door_m"}}],
          "brief": {"public_anchors": ["ev_door_h", "ev_door_m"],
                    "targets": [{"actor": "person_c", "description_en": "Blue top", "description_zh": "蓝色上衣"}]}}
    return ev, overlay


@pytest.fixture(scope="module")
def hole_event(tmp_path_factory: pytest.TempPathFactory):
    pytest.importorskip("open3d")
    pytest.importorskip("PIL")
    pytest.importorskip("zstandard")
    import test_quality_layer as tql

    from awr.sim.events.compile import CompileOptions, compile_event

    tmp = tmp_path_factory.mktemp("ec_holes")
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(tql, "_scene", _hole_scene_factory(tql._scene))
        rec, T = tql._write_fixture(tmp, mp, water=False)
        res = tql._build(rec, tmp / "worlds")
        assert res.exit_code == 0, res.message
    finally:
        mp.undo()
    w = tmp / "worlds" / tql.WID
    assert np.allclose(T[:3, :3] @ [1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) and np.allclose(T[:3, :3] @ [0.0, 0.0, 1.0], [0.0, -1.0, 0.0])
    ev, overlay = _event(tql.WID)
    d = tmp / "ev"
    d.mkdir()
    (d / "anchors.json").write_text(json.dumps(overlay))
    (d / "event.json").write_text(json.dumps(ev))
    # stay_m 为示意声明（不会被验证），门 3 以 lenient 编译（strict 下为 613）
    r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache", strict=False))
    assert r.status == "compiled", json.dumps([e for g in r.gates for e in g["errors"]], ensure_ascii=False)[:3000]
    return w, d / "v1", r


def test_hole_polygon_contains_the_door_and_patch_is_walkable(hole_event) -> None:
    from awr.sim.events.anchors import point_in_polygon
    from awr.sim.events.verify import load_quality

    w, _ev, _r = hole_event
    polys = load_quality(w, ("hole",))
    inside = [bool(point_in_polygon(P, np.array([DOOR_H[0]]), np.array([DOOR_H[1]]))[0]) for P in polys]
    assert any(inside), "hole polygon must contain the in-hole door"
    assert not any(point_in_polygon(P, np.array([DOOR_M[0]]), np.array([DOOR_M[1]]))[0] for P in load_quality(w))
    # 行人走进洞内：person_c 进门前最后一个在场 tick 的位置在 hole 多边形内
    from awr.bench.eval.ec import INSIDE
    from awr.sim.events.tape import open_tape

    tape = open_tape(_ev)
    i = {a["id"]: a["idx"] for a in tape.actors_info()}["person_c"]
    st = [tape.actor_states(k * 100_000_000, [i]) for k in range(0, 400)]
    k_in = next(k for k, x in enumerate(st) if x.state[0] == INSIDE)
    x, y = st[k_in - 1].pos_enu_m[0][:2]
    assert any(point_in_polygon(P, np.array([x]), np.array([y]))[0] for P in polys), (x, y)


def test_gate3_marks_in_hole_events(hole_event) -> None:
    _w, ev, r = hole_event
    truth = json.loads((ev / "truth" / "r1" / "event_truth.json").read_text())
    by = {}
    for row in truth["events"]:
        if row.get("annotation"):
            by.setdefault(row["annotation"], []).append(row["in_incomplete_region"])
    assert by["enter_h"] == [True] and by["exit_h"] == [True]                    # 发生点在 hole 多边形内（M23-AC-018）
    assert by["enter_m"] == [False] and "stay_m" not in by
    assert {c["id"]: c["verified"] for c in truth["annotation_checks"]} == {"enter_h": True, "exit_h": True, "enter_m": True, "stay_m": False}
    assert r.truth["incomplete_region"] >= 2
    assert truth["detector_params"]["incomplete_kinds"] == ["nodata", "hole"]


def test_evaluation_excludes_in_hole_events(hole_event, tmp_path: Path) -> None:
    """端到端（D3-AC-04、M27-AC-020）：洞内两条注释从 EC、覆盖、AO、CE 的分母中剔除，在 `excluded[]` 中单列；室内时段的
    注释以 inside_building 单列；EU 的事件集不变。改写真值去掉洞内标记后复评，两条注释回到分母，其余量与 report 匹配不变。"""
    from drone4d.baselines import BASELINES
    from test_env import _run

    from awr.agentsdk.env.core import _open_world
    from awr.agentsdk.env.spec import EnvSpec, FleetEntry
    from awr.agentsdk.schemas import bench_validator
    from awr.bench.eval.evaluator import evaluate_run

    w, ev, _r = hole_event
    fleet = (FleetEntry("uav-01", (-50.0, 30.0, 0.0)), FleetEntry("uav-02", (15.0, -5.0, 0.0)))   # 洞北侧、大盒子西侧各一架
    spec = EnvSpec("t-hole", 5, w, ev, fleet, physics="kinematic", duration_s=72.0, out_dir=tmp_path / "r")
    _run(spec, lambda u: BASELINES["static_guard"]())
    world = _open_world(w)
    diag: dict = {}
    doc = evaluate_run(tmp_path / "r", ev, world, diag=diag)
    errs = [e.message for e in bench_validator("evaluation").iter_errors(doc)]
    assert not errs, errs[:3]
    assert doc["excluded"] == [{"event_id": "enter_h", "reason": "incomplete_region"}, {"event_id": "exit_h", "reason": "incomplete_region"},
                               {"event_id": "stay_m", "reason": "inside_building"}]
    ids = [x["id"] for x in doc["ec"]["events"]]
    assert ids == ["enter_m"]
    assert doc["ec"]["value"] == float(doc["ec"]["events"][0]["captured"])
    assert doc["components"]["ec"] == doc["ec"]["value"] and diag["n_excluded"] == 3 and diag["n_events"] == 4
    # 对照：真值去掉洞内标记（同一运行、同一磁带），洞内注释回到分母，其余不变
    ev2 = tmp_path / "ev_nohole"
    shutil.copytree(ev, ev2)
    p = ev2 / "truth" / "r1" / "event_truth.json"
    t = json.loads(p.read_text())
    for row in t["events"]:
        row["in_incomplete_region"] = False
    p.write_text(json.dumps(t))
    doc2 = evaluate_run(tmp_path / "r", ev2, world)
    assert doc2["excluded"] == [{"event_id": "stay_m", "reason": "inside_building"}]
    ec2 = {x["id"]: x for x in doc2["ec"]["events"]}
    assert set(ec2) == {"enter_h", "exit_h", "enter_m"}
    assert ec2["enter_m"] == doc["ec"]["events"][0]
    assert doc2["ec"]["value"] == pytest.approx(np.mean([x["captured"] for x in ec2.values()]))
    assert doc2["eu"] == doc["eu"]                                                             # EU 不按可观测区剔除
    assert doc2["safety"] == doc["safety"] and doc2["actors"] == doc["actors"]
    # 同一输入逐字节一致
    assert json.dumps(evaluate_run(tmp_path / "r", ev, world), sort_keys=True) == json.dumps(doc, sort_keys=True)


def test_inside_masks_follow_the_tape(hole_event) -> None:
    """`inside_masks` 与磁带状态逐 tick 一致：person_d 进门后到运行结束都在室内，person_c 在进出之间在室内。"""
    from awr.bench.eval.ec import INSIDE, inside_masks
    from awr.sim.events.tape import open_tape

    _w, ev, _r = hole_event
    tape = open_tape(ev)
    n = 700
    run = SimpleNamespace(tape=tape, n_ticks=n)
    m = inside_masks(run, ["person_c", "person_d"])
    idx = {a["id"]: a["idx"] for a in tape.actors_info()}
    for k in range(0, n + 1, 7):
        st = tape.actor_states(k * 100_000_000, [idx["person_c"], idx["person_d"]]).state
        assert bool(m["person_c"][k]) == (st[0] == INSIDE) and bool(m["person_d"][k]) == (st[1] == INSIDE)
    kd = int(np.argmax(m["person_d"]))
    assert m["person_d"][kd:].all() and 200 <= kd <= 400
    kc = np.nonzero(m["person_c"])[0]
    assert len(kc) and 200 <= kc[0] < kc[-1] <= 600 and m["person_c"][kc[0]:kc[-1] + 1].all()
