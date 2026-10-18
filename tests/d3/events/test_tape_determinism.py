"""事件磁带逐字节确定性（D3-AC-07；M23 §6.6–§6.8、§6.7.5、§6.9.4、§6.10.1；AWR-05 §5.2.3）。

合成起伏地形世界：300 m × 300 m 起伏地面（两组正弦叠加，高差约 4 m、坡度 ≤ 0.2），盒形建筑 20 × 20 × 30 m 位于 world
(30..50, −50..−30)，墙脚埋入地面。事件含 5 名脚本演员：绕行建筑的行人、zone 站位后上车并在第二站下车的行人、进出建筑的行人、
两站停靠的车辆与静止售货机；背景行人按用例取 0 / 38 / 40 / 200 / 1,000 名（5 + 38 = 43 名演员即 r6 原型规模），时长 360 s。

逐条对照 §11 D3-AC-07（ADR-197 修订：Python 生成 + TS 解码逐字节对拍，TS 侧见 apps/web/tests/m06/tape.test.ts）：
同机连续 3 次（含 1 次独立进程）`content_sha256` 相同；zstd 级别不影响 `content_sha256`；只改注释、关系或显示名时
`content_sha256` 不变而 `truth_sha256` 改变；背景 0 / 40 / 200 / 1,000 人时脚本演员列哈希相同；脚本演员到达误差 ≤ 1 mm；
全部演员帧 |z_tape − z_mesh| ≤ 2 cm；上下车与进出建筑不跨帧插值、无滑移，移动与等待之间的切换无位移跳变；存储 ≤ 25 B/演员/s；
43 名演员 360 s 生成 ≤ 10 s。跨 CPU 型号一次生成的对拍由 X01（tools/bench/d3/crossmachine.py）在 dmax 执行。
"""

from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")
pytest.importorskip("PIL")
pytest.importorskip("zstandard")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world"))
from awr.sim.events import gen
from awr.sim.events import tape as tp
from awr.sim.events.compile import CompileOptions, compile_event, stage_bbox
from awr.sim.events.jcs import jcs

ROOT = Path(__file__).resolve().parents[3]
SIZE = 300.0
DUR_S = 360
TICK_NS = tp.TICK_NS


# ---------------------------------------------------------------------- 起伏地形世界
def terrain_h(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """world 帧地面高度（m）：两组正弦叠加，均值约 0、无整体倾斜（调平 QA 不受影响）。"""
    return 1.2 * np.sin(2 * np.pi * x / 97.0) * np.cos(2 * np.pi * y / 131.0) + 0.6 * np.sin(2 * np.pi * (x + y) / 53.0)


def _scene() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """glTF Y-up（world y = −z_gltf）：61 × 61 顶点起伏地面（5 m 格）加盒子（底 −3 m、顶 30 m，外法线，无底面）。"""
    n = 61
    x, z = np.meshgrid(np.linspace(-SIZE / 2, SIZE / 2, n), np.linspace(-SIZE / 2, SIZE / 2, n))
    V = np.stack([x.ravel(), terrain_h(x.ravel(), -z.ravel()), z.ravel()], 1)
    UV = np.stack([(x.ravel() + SIZE / 2) / SIZE, (z.ravel() + SIZE / 2) / SIZE], 1)
    F = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j
            F += [[a, c, b], [a, d, c]]
    bx = np.array([[x0 + 40.0, y0, z0 + 40.0] for y0 in (-3.0, 30.0) for z0 in (-10.0, 10.0) for x0 in (-10.0, 10.0)])
    quads = [(4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    base = len(V)
    c0 = bx.mean(0)
    for q in quads:
        for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3])):
            p = bx[list(t)]
            nrm = np.cross(p[1] - p[0], p[2] - p[0])
            F.append([base + k for k in (t if nrm @ (p.mean(0) - c0) > 0 else t[::-1])])
    V = np.vstack([V, bx]).astype(np.float32)
    UV = np.vstack([UV, np.full((8, 2), 0.5)]).astype(np.float32)
    return V, np.asarray(F, np.uint32), UV


def build_terrain_world(tmp: Path, mp: pytest.MonkeyPatch) -> Path:
    """与 synthworld.build_synth_world 同一流水线（手工配准、landmark 尺度），场景换为起伏地形。"""
    import test_glb_build as g
    from glbfix import png, write_glb

    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.glb.recipe import file_sha256, load_recipe
    from awr.world.glb.register import register_manual, write_registration
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    raw = tmp / "raw"
    (raw / "terrain").mkdir(parents=True)
    V, F, UV = _scene()
    glb = write_glb(raw / "terrain" / "terrain.glb", V=V, F=F, UV=UV, tex=png(8, 8, (90, 120, 60)))
    man = tmp / "raw-manifest.json"
    man.write_text(json.dumps({"files": [{"name": "terrain/terrain.glb", "bytes": glb.stat().st_size, "sha256": file_sha256(glb)}]}))
    mp.setattr(g.recipe_mod, "RAW_DIR", raw)
    mp.setattr(g.recipe_mod, "RAW_MANIFEST", man)
    rd = tmp / "recipes" / "terrain-glb"
    rd.mkdir(parents=True)
    (rd / "recipe.yaml").write_text(
        "schema: awr.glb.recipe.v1\nworld_id: terrain-glb\nname: Synthetic terrain GLB test world\nrole: event-main\n"
        f"source:\n  glb: {{path: terrain/terrain.glb, sha256: {file_sha256(glb)}}}\n"
        "  sketchfab: {uid: '', author: test, license: CC0-1.0}\nregistration: registration.json\n"
        "build:\n  tiling_m: 128\n  pointcloud: {target_points: 60000, seed: 7}\n")
    rec = load_recipe("terrain-glb", tmp / "recipes")
    doc = register_manual(rec, {"s": 1.0, "yaw_deg": 0.0, "g": (0.0, 0.0), "t_osm_m": (0.0, 0.0), "osm_ref": (43.73, 23.96)},
                          h_msl_m=100.0, evidence=["synthetic fixture"])
    write_registration(rec, doc, accept_by="test")
    assert build_world(GlbAdapter("terrain-glb", recipe=rec), tmp / "worlds", ctx=StageContext(world_id="terrain-glb")).exit_code == 0
    return tmp / "worlds" / "terrain-glb"


# ---------------------------------------------------------------------- 事件
def _sq(cx: float, cy: float, r: float) -> list[list[float]]:
    return [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]]


# 点锚点均取在 1 m 格中心（x.5、y.5），可走格内吸附距离为 0，到达点即作者给出的坐标
OVERLAY = {"schema": "awr.anchors.v1", "extends": "world", "world_id": "terrain-glb", "anchors": [
    {"id": "ev_west", "kind": "point", "profile": "person", "xy": [-60.5, -40.5], "seed": True},
    {"id": "ev_east", "kind": "point", "profile": "person", "xy": [100.5, -40.5]},
    {"id": "ev_south", "kind": "point", "profile": "person", "xy": [0.5, -70.5]},
    {"id": "ev_north", "kind": "point", "profile": "person", "xy": [0.5, -10.5]},
    {"id": "ev_door", "kind": "door", "profile": "person", "xy": [27.5, -40.5], "building": "ev_box"},
    {"id": "ev_plaza", "kind": "zone", "profile": "person", "polygon": _sq(0.0, 60.0, 10.0)},
    {"id": "ev_curb", "kind": "curb", "profile": "person", "xy": [60.5, 60.5], "pair": "ev_road"},
    {"id": "ev_road", "kind": "point", "profile": "vehicle", "xy": [64.5, 60.5]},
    {"id": "ev_curb2", "kind": "curb", "profile": "person", "xy": [96.5, 20.5], "pair": "ev_stop2"},
    {"id": "ev_stop2", "kind": "point", "profile": "vehicle", "xy": [100.5, 20.5]},
    {"id": "ev_road_w", "kind": "point", "profile": "vehicle", "xy": [-100.5, 100.5]},
    {"id": "ev_road_e", "kind": "point", "profile": "vehicle", "xy": [120.5, 100.5]},
    {"id": "ev_kiosk", "kind": "point", "profile": "person", "xy": [-20.5, 20.5]},
    {"id": "ev_town", "kind": "zone", "profile": "person", "polygon": _sq(0.0, 0.0, 130.0)},
]}

EVENT = {
    "schema": "drone4d.event.v1", "event_id": "terrain-det", "version": 1, "world": {"id": "terrain-glb"}, "anchors_ref": "event:anchors.json",
    "seed": 23, "duration_s": DUR_S,
    "actors": [
        {"id": "person_a", "class": "person", "appearance": {"top_rgb": [42, 93, 176], "height_m": 1.76},
         "route": [{"t": 0, "at": "ev_west", "do": "spawn"}, {"t": 140, "at": "ev_east", "dwell_s": 30},
                   {"t": 300, "at": "ev_plaza", "do": "wait"}]},
        {"id": "person_b", "class": "person", "appearance": {"top_rgb": [200, 40, 40]},
         "route": [{"t": 0, "at": "ev_plaza", "do": "spawn", "dwell_s": 20}, {"t": 80, "at": "ev_curb", "do": "board", "target": "vehicle_c"},
                   {"t": 170, "at": "ev_curb2", "do": "alight", "target": "vehicle_c"}, {"t": 300, "at": "ev_north", "do": "despawn"}]},
        {"id": "person_c", "class": "person",
         "route": [{"t": 0, "at": "ev_south", "do": "spawn"}, {"t": 40, "at": "ev_door", "do": "enter_building"},
                   {"t": 70, "at": "ev_door", "do": "exit_building"}, {"t": 110, "at": "ev_north"},
                   {"t": 250, "at": "ev_west", "do": "despawn"}]},
        {"id": "vehicle_c", "class": "vehicle",
         "route": [{"t": 0, "at": "ev_road_w", "do": "spawn"}, {"t": 60, "at": "ev_road", "do": "wait", "dwell_s": 40},
                   {"t": 160, "at": "ev_stop2", "do": "wait", "dwell_s": 30}, {"t": 260, "at": "ev_road_e", "do": "despawn"}]},
        {"id": "kiosk", "class": "machine", "route": [{"t": 0, "at": "ev_kiosk", "do": "spawn"}]},
    ],
    "background": [{"class": "person", "count": 38, "zone": "ev_town", "pause_s": [0, 5]}],
    "environment": {"keyframes": [{"t": 0, "wind_mps": 2.0}, {"t": 200, "wind_mps": 6.0, "wind_dir_deg": 300, "rain_mm_h": 2.0}]},
    "comms": {"zones": [{"id": "jam", "polygon": [[-10, -10], [10, -10], [10, 10], [-10, 10]], "t": [30, 60], "extra_loss": 0.4}]},
    "annotations": [
        {"id": "an_board", "type": "board_vehicle", "participants": ["person_b", "vehicle_c"], "window": [75, 85], "label_zh": "上车",
         "label_en": "Boarding"},
        {"id": "an_enter", "type": "enter_building", "participants": ["person_c"], "window": [35, 45]},
        {"id": "an_alight", "type": "alight_vehicle", "participants": ["person_b", "vehicle_c"], "window": [165, 175]},
    ],
    "relations": [{"src": "an_enter", "rel": "before", "dst": "an_board"}, {"src": "an_board", "rel": "before", "dst": "an_alight"}],
    "labels": {"zh": "起伏地形确定性事件", "en": "Terrain determinism event"},
}
N_S = 5


def _event(bg: int = 38, **patch) -> dict:
    ev = copy.deepcopy(EVENT)
    if bg:
        ev["background"][0]["count"] = bg
    else:
        ev.pop("background")
    ev.update(patch)
    return ev


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory):
    tmp = tmp_path_factory.mktemp("terrain")
    mp = pytest.MonkeyPatch()
    yield build_terrain_world(tmp, mp), tmp
    mp.undo()


def _write(tmp: Path, name: str, ev: dict) -> Path:
    d = tmp / name
    d.mkdir(exist_ok=True)
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    return d


def _compile(world, ev: dict, name: str):
    w, tmp = world
    d = _write(tmp, name, ev)
    t0 = time.perf_counter()
    r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache"))
    wall = time.perf_counter() - t0
    assert r.status == "compiled", json.dumps(r.as_dict(), ensure_ascii=False)[:3000]
    return r, tp.open_tape(r.out), wall


@pytest.fixture(scope="module")
def runs(world):
    """43 名演员（5 脚本 + 38 背景）、360 s：首次编译（冷）与紧随其后的第二次编译（热）。"""
    first = _compile(world, _event(), "run1")
    second = _compile(world, _event(), "run2")
    return first, second


@pytest.fixture(scope="module")
def bg_series(world):
    return {n: _compile(world, _event(bg=n), f"bg{n}") for n in (0, 40, 200, 1000)}


def _frames(t: tp.TapeReader, *, background: bool = True) -> np.ndarray:
    return np.concatenate([t.block_frames(i, background=background) for i in range(len(t.blocks))])


def _stage(world, plan: dict):
    """与 compile.py 同一舞台装载（门 2 输入），供生成侧到达量复算。"""
    from awr.sim.events.anchors import anchor_xy, load_overlay, resolve_anchors, world_anchors
    from awr.sim.events.ground import build_ground_index
    from awr.sim.events.nav import build_nav
    from awr.world.mesh.query import open_mesh_world_query

    w, tmp = world
    anchors = resolve_anchors(world_anchors(w), load_overlay(OVERLAY))
    q = open_mesh_world_query(w, tmp / "cache")
    bbox = stage_bbox(plan, anchors)
    seeds = [anchor_xy(a) for a in anchors.values() if a.get("seed")]
    nav_p = build_nav(q, bbox, "person", seeds)
    nav_v = build_nav(q, bbox, "vehicle", seeds, z=nav_p.z)
    return gen.Stage({"person": nav_p, "vehicle": nav_v}, build_ground_index(q, bbox), anchors), q


# ---------------------------------------------------------------------- 1. 同机连续 3 次
def test_three_consecutive_generations_are_byte_identical(world, runs) -> None:
    (r1, t1, _), (r2, t2, _) = runs
    w, tmp = world
    d = _write(tmp, "run3", _event())
    env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONHASHSEED": "4242"}
    p = subprocess.run([sys.executable, "-m", "awr.sim.events", "compile", str(d / "event.json"), "--world", str(w), "--out", str(d / "v1"),
                        "--cache", str(tmp / "cache-proc")], env=env, capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr[-3000:]
    r3 = json.loads(p.stdout)
    t3 = tp.open_tape(d / "v1")
    keys = ("content_sha256", "body_sha256", "header_sha256", "file_sha256")
    h = [t.hashes() for t in (t1, t2, t3)]
    for k in keys:
        assert h[0][k] == h[1][k] == h[2][k], k
    assert r1.tape["content_sha256"] == r2.tape["content_sha256"] == r3["tape"]["content_sha256"] == h[0]["content_sha256"]
    b = [(x.out / "tape.bin").read_bytes() for x in (r1, r2)] + [(d / "v1" / "tape.bin").read_bytes()]
    assert b[0] == b[1] == b[2]
    assert t1.header_bytes == jcs(t1.header)                                       # 规范头即 JCS 原文
    m = [json.loads((p_ / "tape.json").read_text()) for p_ in (r1.out, r2.out, d / "v1")]
    assert m[0]["provenance"]["created_at"] and m[0]["header"] == m[2]["header"]   # 出处不进哈希
    assert t1.n_actors == 43 and t1.n_scripted == N_S and t1.n_ticks == DUR_S * 10


# ---------------------------------------------------------------------- 2. 压缩级别
@pytest.mark.parametrize("level", [-5, 1, 9, 22])
def test_zstd_level_does_not_change_content_hash(runs, tmp_path: Path, level: int, monkeypatch: pytest.MonkeyPatch) -> None:
    (_r1, t1, _), _ = runs
    fr = _frames(t1)
    ref = t1.hashes()
    h = tp.write_tape(tmp_path / f"l{level}.bin", t1.header, fr, n_scripted=t1.n_scripted, level=level)
    assert h["content_sha256"] == ref["content_sha256"] and h["body_sha256"] == ref["body_sha256"]
    assert h["header_sha256"] == ref["header_sha256"] and h["file_sha256"] != ref["file_sha256"]
    rr = tp.TapeReader(tmp_path / f"l{level}.bin")
    assert rr.verify(ref["content_sha256"])["content_sha256"] == ref["content_sha256"]
    assert rr.scripted_sha256() == t1.scripted_sha256() and np.array_equal(_frames(rr), fr)
    whole = tp.write_tape(tmp_path / "whole.bin", t1.header, fr, n_scripted=t1.n_actors, level=level)    # 不拆背景子块
    assert whole["content_sha256"] == ref["content_sha256"]
    monkeypatch.setenv("AWR_TAPE_THREADS", "1")                                    # 串行压缩：压缩字节与并行一致
    assert tp.write_tape(tmp_path / "serial.bin", t1.header, fr, n_scripted=t1.n_scripted, level=level) == h
    assert (tmp_path / "serial.bin").read_bytes() == (tmp_path / f"l{level}.bin").read_bytes()


# ---------------------------------------------------------------------- 3. 注释、关系、显示名
def test_annotations_relations_labels_change_truth_not_tape(world, runs) -> None:
    jsonschema = pytest.importorskip("jsonschema")

    (r1, t1, _), _ = runs
    base_plan = json.loads((r1.out / "plan.json").read_text())
    variants = {}
    ev = _event()
    ev["annotations"][0]["label_zh"] = "上车（复核）"
    ev["annotations"][1]["window"] = [36, 44]
    variants["annotations"] = ev
    ev = _event()
    ev["relations"] = [{"src": "an_enter", "rel": "before", "dst": "an_alight"}]
    variants["relations"] = ev
    ev = _event()
    ev["labels"] = {"zh": "起伏地形确定性事件（修订）", "en": "Terrain determinism event (revised)"}
    variants["labels"] = ev
    sch = json.loads((ROOT / "packages/contracts/event/truth.v1.schema.json").read_text())
    truth0 = gen.build_truth(base_plan, r1.tape["content_sha256"])
    jsonschema.Draft202012Validator(sch).validate(truth0)
    shas = {"base": gen.truth_sha256(truth0)}
    dsl = {"base": truth0["dsl_sha256"]}
    for name, ev in variants.items():
        r, t, _ = _compile(world, ev, f"truth-{name}")
        assert r.tape["content_sha256"] == r1.tape["content_sha256"], name
        assert t.header["event_sha256"] == t1.header["event_sha256"] and t.header_bytes == t1.header_bytes
        assert (r.out / "tape.bin").read_bytes() == (r1.out / "tape.bin").read_bytes()
        plan = json.loads((r.out / "plan.json").read_text())
        tr = gen.build_truth(plan, r.tape["content_sha256"], truth_rev=2)
        jsonschema.Draft202012Validator(sch).validate(tr)
        shas[name], dsl[name] = gen.truth_sha256(tr), tr["dsl_sha256"]
    assert len(set(shas.values())) == 4 and len(set(dsl.values())) == 4
    # truth_sha256 只覆盖去掉 perf 的内容；同一输入复算相同
    assert gen.truth_sha256({**truth0, "perf": {"wall_ms": 12}}) == shas["base"] == gen.truth_sha256(gen.build_truth(base_plan, r1.tape["content_sha256"]))
    # 对照：磁带输入子集（种子）变化时 content_sha256 改变
    r, _t, _ = _compile(world, _event(seed=24), "truth-seed")
    assert r.tape["content_sha256"] != r1.tape["content_sha256"]


# ---------------------------------------------------------------------- 4. 背景人数与脚本演员列
def test_background_counts_keep_scripted_columns(bg_series, runs) -> None:
    (r43, t43, _), _ = runs
    tapes = {n: v[1] for n, v in bg_series.items()} | {38: t43}
    ref = tapes[0]
    s_ref = _frames(ref, background=False)
    sha = {n: t.scripted_sha256() for n, t in tapes.items()}
    assert len(set(sha.values())) == 1, sha
    for n, t in tapes.items():
        assert t.n_scripted == N_S and t.n_actors == N_S + n
        assert t.actors[:N_S] == ref.actors[:N_S]
        assert np.array_equal(_frames(t, background=False), s_ref)
        # 脚本演员子块的压缩字节也相同（S 子块只由脚本演员决定）
        assert [t._buf[b.off_s:b.off_s + b.comp_s] for b in t.blocks] == [ref._buf[b.off_s:b.off_s + b.comp_s] for b in ref.blocks]
    assert len({v[0].tape["content_sha256"] for v in bg_series.values()} | {r43.tape["content_sha256"]}) == 5
    assert all(b.comp_b == 0 for b in ref.blocks)                                  # 无背景：B 子块为空


# ---------------------------------------------------------------------- 5. 到达误差
def test_scripted_arrivals_within_1mm(world, runs) -> None:
    (r1, t1, _), _ = runs
    plan = json.loads((r1.out / "plan.json").read_text())
    stage, _q = _stage(world, plan)
    pf = gen.preflight(plan, stage)
    assert not pf["errors"]
    rows = gen.arrivals(plan, pf)
    assert len(rows) == sum(len(a["route"]) for a in plan["actors"])
    assert max(r["err_mm"] for r in rows) <= 1.0 and all(r["late_ms"] == 0 for r in rows)
    man = json.loads((r1.out / "tape.json").read_text())["report"]["arrivals"]
    assert [(m["actor"], m["at"], m["t_arrive_s"], m["late_s"]) for m in man] == \
        [(r["actor"], r["at"], r["t_arrive_ms"] / 1000.0, 0.0) for r in rows]
    # 吸附距离为 0：计划点即作者坐标（zone 站位除外，按 §6.6.2 的 0.8 m 圆周）
    assert all(s["dist_m"] == 0.0 for s in json.loads((r1.out / "compile_report.json").read_text())["snaps"])
    authored = {a["id"]: a for a in OVERLAY["anchors"]}
    ids = [a["id"] for a in t1.actors]
    fr = _frames(t1)
    seen = set()
    for r in rows:
        an = authored[r["at"]]
        if an["kind"] != "zone":
            assert math.dist(r["xy_plan_m"], an["xy"]) == 0.0
        else:                                                                     # person_a、person_b 共用 ev_plaza：两人站位
            assert abs(math.dist(r["xy_plan_m"], (0.0, 60.0)) - gen.ZONE_SLOT_R) < 1e-9
        k, rem = divmod(r["t_arrive_ms"], 100)
        assert rem == 0 and k < t1.n_ticks
        i = ids.index(r["actor"])
        rec = fr[k, i]
        if rec["state"] == 3:                                                     # 上车瞬间随车（阶跃），到达点只在生成侧可见
            assert r["do"] == "board" and rec["container"] == ids.index("vehicle_c")
            seen.add("board")
            continue
        xy = (rec["x_mm"] / 1000.0, rec["y_mm"] / 1000.0)
        assert math.dist(xy, r["xy_plan_m"]) <= 1e-3, r                           # 磁带在到达 tick 的位置
        assert [int(rec["x_mm"]), int(rec["y_mm"])] == r["xy_mm"]                  # 与生成侧同一时间线
        st = t1.actor_states(k * TICK_NS, [i])
        assert math.dist(st.pos_enu_m[0, :2], r["xy_plan_m"]) <= 1e-3
        seen.add(r["do"])
    assert {"spawn", "pass", "wait", "board", "alight", "enter_building", "exit_building", "despawn"} <= seen


# ---------------------------------------------------------------------- 6. 贴地
def test_all_actor_frames_on_mesh_within_2cm(world, runs, bg_series) -> None:
    from awr.world.mesh.query import open_mesh_world_query

    w, tmp = world
    q = open_mesh_world_query(w, tmp / "cache")
    for t in (runs[0][1], bg_series[1000][1]):
        fr = _frames(t)
        xy = np.stack([fr["x_mm"], fr["y_mm"]], -1).reshape(-1, 2).astype(np.float64) / 1000.0
        z = fr["z_mm"].ravel().astype(np.float64) / 1000.0
        assert np.ptp(z) > 2.0                                                   # 起伏地形：贴地检查不是平地上的恒等式
        for zm in (q.walkable_height(xy), q.walkable_height(xy, nav_z=z)):       # 最高表面（与磁带无关）与"磁带高度 + 2 m 向下"
            zm = zm.astype(np.float64)
            assert np.isfinite(zm).all()
            dz = np.abs(z - zm)
            assert dz.max() <= 0.02, (float(dz.max()), int(dz.argmax()))
        # 解析地面：世界包构建会整体平移高程基准，残差扣除常数后只剩 5 m 格分段线性化误差，只作量级检查
        res = z - terrain_h(xy[:, 0], xy[:, 1])
        assert np.ptp(res) < 0.25, float(np.ptp(res))


# ---------------------------------------------------------------------- 7. 可见性切换与移动/等待切换
def _vis(s: np.ndarray) -> np.ndarray:
    return np.isin(s, (1, 2))


def test_transitions_step_without_slide_or_jump(runs) -> None:
    (_r, t, _), _ = runs
    fr = _frames(t)
    ids = [a["id"] for a in t.actors]
    v = ids.index("vehicle_c")
    st, ct = fr["state"], fr["container"]
    kinds = set()
    n_step = n_smooth = 0
    vis = _vis(st)
    jump = (vis[:-1] != vis[1:]) | (ct[:-1] != ct[1:])                             # (k, i)：k → k + 1 可见性类别或容器变化
    mw = vis[:-1] & vis[1:] & (st[:-1] != st[1:]) & ~jump                          # 移动 ↔ 等待
    for k, i in zip(*np.nonzero(jump), strict=True):
        a, b = fr[k, i], fr[k + 1, i]
        # 阶跃：区间内一律取第 k 帧（位置、状态、容器），t_{k+1} 起取第 k + 1 帧
        p0 = np.array([a["x_mm"], a["y_mm"], a["z_mm"]]) / 1000.0
        for rem in (1, 1_000_000, 50_000_000, TICK_NS - 1):
            s = t.actor_states(int(k) * TICK_NS + rem, [int(i)])
            assert np.array_equal(s.pos_enu_m[0], p0) and s.state[0] == a["state"] and s.container[0] == a["container"]
        s = t.actor_states((int(k) + 1) * TICK_NS, [int(i)])
        assert s.state[0] == b["state"] and np.array_equal(s.pos_enu_m[0], np.array([b["x_mm"], b["y_mm"], b["z_mm"]]) / 1000.0)
        kinds.add((int(a["state"]), int(b["state"])))
        n_step += 1
    for k, i in zip(*np.nonzero(mw), strict=True):
        a, b = fr[k, i], fr[k + 1, i]
        # 移动 ↔ 等待：Hermite 插值连续，5 ms 采样的相邻位移不超过该类最大速度 × 5 ms 的 1.5 倍，两端与帧记录重合
        P = np.stack([t.actor_states(int(x), [int(i)]).pos_enu_m[0] for x in int(k) * TICK_NS + np.arange(0, TICK_NS + 1, 5_000_000)])
        vmax = 11.0 if t.actors[i]["class"] == "vehicle" else 1.8
        assert np.linalg.norm(np.diff(P[:, :2], axis=0), axis=1).max() <= 1.5 * vmax * 5e-3 + 1e-6
        assert np.allclose(P[0], np.array([a["x_mm"], a["y_mm"], a["z_mm"]]) / 1000.0, atol=1e-12)
        assert np.allclose(P[-1], np.array([b["x_mm"], b["y_mm"], b["z_mm"]]) / 1000.0, atol=1e-12)
        n_smooth += 1
    assert {(0, 1), (1, 3), (3, 2), (1, 4), (4, 1), (1, 5)} <= kinds and n_smooth > 10, (sorted(kinds), n_smooth)
    # 上车：乘客整段与车辆同位、同航向；下车：出现于 curb 吸附点，此前一帧仍在车内
    b = ids.index("person_b")
    inv = np.nonzero(st[:, b] == 3)[0]
    assert len(inv) == 900 and inv[0] == 800 and inv[-1] == 1699 and (ct[inv, b] == v).all()
    for f in ("x_mm", "y_mm", "z_mm", "yaw_cdeg"):
        assert np.array_equal(fr[f][inv, b], fr[f][inv, v])
    assert st[1700, b] in (1, 2) and ct[1700, b] == tp.NO_CONTAINER and (fr["x_mm"][1700, b], fr["y_mm"][1700, b]) == (96500, 20500)
    assert (st[800:1700, v] == 2).sum() > 0 and st[1599, v] == 1 and st[1600, v] == 2       # 第二站停靠窗口 [160, 190] s 内下车
    # 进出建筑：室内整段位置不动且等于门吸附点，出门后由门点出发
    c = ids.index("person_c")
    ins = np.nonzero(st[:, c] == 4)[0]
    assert ins[0] == 400 and ins[-1] == 699
    assert (fr["x_mm"][ins, c] == 27500).all() and (fr["y_mm"][ins, c] == -40500).all() and (fr["vx_cmps"][ins, c] == 0).all()
    assert st[399, c] == 1 and st[700, c] in (1, 2) and (fr["x_mm"][700, c], fr["y_mm"][700, c]) == (27500, -40500)
    # 磁带帧间：可见且同类的相邻帧位移不超过最大速度 × 0.1 s（无跳点）
    P = np.stack([fr["x_mm"], fr["y_mm"]], -1).astype(np.float64) / 1000.0
    step = np.linalg.norm(np.diff(P, axis=0), axis=-1)
    same = _vis(st[:-1]) & _vis(st[1:]) & (ct[:-1] == ct[1:])
    lim = np.array([1.1 if a["class"] == "vehicle" else 0.18 for a in t.actors]) + 2e-3
    assert (step[same] <= np.broadcast_to(lim, step.shape)[same]).all()
    assert n_step > 0


# ---------------------------------------------------------------------- 8. 存储与生成耗时
def test_storage_budget(runs, bg_series, capsys: pytest.CaptureFixture[str]) -> None:
    from awr.sim.events.__main__ import main

    (r1, _t1, _), _ = runs
    for r in [r1, *(v[0] for v in bg_series.values())]:
        assert r.tape["bytes_per_actor_s"] <= 25.0, r.tape
    assert main(["inspect", str(bg_series[1000][0].out / "tape.bin")]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["n_actors"] == 1005 and info["bytes_per_actor_second"] <= 25.0


def test_generation_43_actors_360s_within_10s(runs) -> None:
    (_r1, _t1, wall1), (r2, _t2, wall2) = runs
    prov = json.loads((r2.out / "tape.json").read_text())["provenance"]
    print(f"\n43 actors x 360 s compile wall: cold {wall1:.2f} s, warm {wall2:.2f} s (provenance {prov['wall_ms']} ms)")
    assert wall2 <= 10.0 and prov["wall_ms"] <= 10_000
    assert r2.tape["n_actors"] == 43 and r2.tape["n_ticks"] == 3600

