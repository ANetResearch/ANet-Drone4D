"""D3-AC-61 热成像验收汇总：识别判据构造用例、跨进程逐字节一致与全部阈值的条款登记（docs/05 §11.3；M25 §15.6；M27-FR-176）。

阈值逐条对照 AWR-05 §11.3 D3-AC-61 原文（`AC61` 登记表，`test_ac61_thresholds_match_docs05` 校验原文）：

1. 辐射换算：Planck 正反表往返 ≤ 0.01 K（200–400 K）；
2. 构造场景 (a) 均匀 300 K、ε = 1 平板无噪声 ≤ 0.05 K；(b) ε = 0.9 水平面晴空（T_sky = T_air − 25 K）≤ 0.1 K；(c) 雾中 1 km 表观温差
   按 τ(d) 衰减 ≤ 5%；
3. 噪声：均匀 300 K 场景逐像素时间标准差在标称 NETD 的 ±10% 内；固定图案噪声运行内恒定、换种子改变；
4. 动态实体：人员与车辆（发动机运转与熄火 600 s 后）无噪声档 ≤ 0.2 K；
5. `ThermalObs` 通过 `bench/obs.v1`，交付字节即存档字节；
6. 同镜像同型号 GPU 两次运行逐字节一致（同进程两次见 test_thermal_render.py；本文件另做两个独立进程各自建上下文的对比）；
7. 识别判据 `h_px_unocc ≥ 16`、可见比例 ≥ 0.5、`|dT_app_k| ≥ 1 K` 的构造用例（本文件）：谓词边界（CPU）、可见度归约与 `dT_app_k`
   经识别表（CPU）、GPU 渲染的距离、遮挡与温差三组构造（期望值由投影几何与渲染前给定的温差独立求得）。

条款 1–5 的实现用例在 test_thermal_core.py、test_thermal_render.py、test_thermal_obs.py；本文件登记每条条款对应的用例与阈值常量，
并校验这些用例存在且阈值不宽于原文。GPU 用例需要 moderngl 与 EGL（d3-sensorsvc 镜像，dmax 单卡绑定），本机只运行 CPU 用例。
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sensorsvc"))
from appfix import scene_enu, write_world

from awr.bench.eval.ec import event_capture
from awr.bench.eval.ident import build_ident, identified, is_thermal, thermal_rows
from awr.bench.eval.thresholds import TH, Thresholds
from awr.sensorsvc.thermal import render as tr

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def _sibling(name: str):
    """按路径加载同目录的测试模块（只取夹具与辅助函数；不把 test_* 引入本模块，避免重复收集）。"""
    key = f"_ac61_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return sys.modules[key]


ttr = _sibling("test_thermal_render")
gl_world = ttr.gl_world                                  # 夹具世界（200 m 地面 + 20 × 20 × 30 m 盒子）与 EGL 上下文，与渲染用例共用

H_PX, VIS, DT_K = 16, 0.5, 1.0                           # D3-AC-61 识别判据（eval_thresholds.v1）
PERSON = ttr.PERSON
VEHICLE = ttr.VEHICLE
NIGHT, DAY, T_AIR = ttr.NIGHT, ttr.DAY, ttr.T_AIR
CLEAN = tr.ThermalModel(noise="clean")
DT_CONSTRUCT_TOL_K = 0.1                                 # 温差构造：dT_app_k 与给定 δ 之差（本任务设定）


# ---------------------------------------------------------------- 条款登记（docs/05 §11.3 D3-AC-61）

AC61 = (
    # (条款, §11 原文阈值片段, [(模块, 用例)], {模块: {常量: 原文阈值}}, 源码中须出现的阈值字面量 [(模块, 用例, 字面量)])
    ("planck_round_trip", ["往返误差 ≤ 0.01 K（200–400 K）"],
     [("test_thermal_core", "test_planck_table_round_trip_within_10_mk")], {},
     [("test_thermal_core", "test_planck_table_round_trip_within_10_mk", "<= 0.01")]),
    ("case_a_plate_300k", ["ε = 1 的平板，无噪声输出表观温度误差 ≤ 0.05 K"],
     [("test_thermal_core", "test_case_a_uniform_300k_blackbody_plate"), ("test_thermal_render", "test_gpu_plate_300k_eps1_and_clear_sky_eps09")],
     {"test_thermal_render": {"PLATE_TOL_K": 0.05}}, [("test_thermal_core", "test_case_a_uniform_300k_blackbody_plate", "<= 0.05")]),
    ("case_b_clear_sky", ["T_sky = T_air − 25 K", "与解析解差 ≤ 0.1 K"],
     [("test_thermal_core", "test_case_b_eps09_horizontal_surface_clear_sky"), ("test_thermal_render", "test_gpu_plate_300k_eps1_and_clear_sky_eps09")],
     {"test_thermal_render": {"CLEAR_TOL_K": 0.1}}, [("test_thermal_core", "test_case_b_eps09_horizontal_surface_clear_sky", "<= 0.1")]),
    ("case_c_fog_1km", ["雾中 1 km 外目标的表观温差按 `τ(d)` 衰减，与解析解差 ≤ 5%"],
     [("test_thermal_core", "test_case_c_fog_contrast_attenuates_with_tau"), ("test_thermal_render", "test_gpu_fog_1km_contrast_follows_tau")],
     {"test_thermal_render": {"FOG_TOL": 0.05}}, [("test_thermal_core", "test_case_c_fog_contrast_attenuates_with_tau", "(dec, 0.05)")]),
    ("netd_10pct", ["逐像素时间标准差在标称 NETD 的 ±10% 内"],
     [("test_thermal_core", "test_per_pixel_temporal_std_within_10pct_of_netd"), ("test_thermal_core", "test_full_frame_temporal_noise_matches_netd")],
     {}, [("test_thermal_core", "test_per_pixel_temporal_std_within_10pct_of_netd", "<= 0.10")]),
    ("fpn_run_constant", ["固定图案噪声在运行内恒定、换种子改变"],
     [("test_thermal_core", "test_fpn_is_constant_within_run_and_changes_with_seed")], {}, []),
    ("dynamic_entities", ["发动机运转与熄火 600 s 后", "误差 ≤ 0.2 K，无噪声档"],
     [("test_thermal_render", "test_gpu_dynamic_entities_match_table"), ("test_thermal_render", "test_vehicle_engine_running_and_600s_after_off"),
      ("test_thermal_render", "test_person_and_dog_temperatures_and_dsl_override")],
     {"test_thermal_render": {"DYN_TOL_K": 0.2}}, []),
    ("thermal_obs_delivered_bytes", ["`ThermalObs` 通过 `bench/obs.v1` 校验，交付字节即存档字节"],
     [("test_thermal_obs", "test_golden_vectors_host_and_sdk_roundtrip"), ("test_thermal_obs", "test_thermal_obs_is_projection_with_delivered_bytes"),
      ("test_thermal_obs", "test_recorder_archives_delivered_bytes"), ("test_thermal_obs", "test_truth_and_internal_fields_are_rejected")], {}, []),
    ("gpu_byte_identical", ["同镜像同型号 GPU 两次运行逐字节一致"],
     [("test_thermal_render", "test_gpu_two_runs_byte_identical_and_static_shortcut"), ("test_thermal", "test_gpu_two_processes_byte_identical")],
     {}, []),
    ("ident_criteria", ["`h_px_unocc ≥ 16`", "可见比例 ≥ 0.5", "≥ 1 K`）有构造用例"],
     [("test_thermal", "test_ident_predicate_boundaries"), ("test_thermal", "test_thermal_objects_construct_through_ident_table"),
      ("test_thermal", "test_gpu_ident_projected_height"), ("test_thermal", "test_gpu_ident_visible_ratio"),
      ("test_thermal", "test_gpu_ident_temperature_contrast")], {"test_thermal": {"H_PX": 16, "VIS": 0.5, "DT_K": 1.0}}, []),
)


def _module(name: str):
    return SimpleNamespace(**globals()) if name == "test_thermal" else _sibling(name)


def test_ac61_clause_registry_complete() -> None:
    """每条 AC-61 条款都有实现用例，用例中的容差常量与阈值字面量不宽于 §11 原文。"""
    assert len(AC61) == 10 and len({c[0] for c in AC61}) == 10
    for clause, _text, tests, consts, literals in AC61:
        for mod, fn in tests:
            f = getattr(_module(mod), fn, None)
            assert callable(f) and fn.startswith("test_"), (clause, mod, fn)
        for mod, kv in consts.items():
            for k, want in kv.items():
                assert getattr(_module(mod), k) == want, (clause, mod, k)
        for mod, fn, lit in literals:
            assert lit in inspect.getsource(getattr(_module(mod), fn)), (clause, fn, lit)
    assert (TH.h_px, TH.vis, TH.dT_app_k) == (H_PX, VIS, DT_K)          # 评测器阈值与本文件判据同源


def test_ac61_thresholds_match_docs05() -> None:
    """登记表的阈值片段逐条出现在 docs/05 §11.3 D3-AC-61 行原文中（原文修订时本用例提示同步登记表与用例）。"""
    doc = ROOT / "docs/05-D3-设计增补与决策记录.md"
    if not doc.exists():
        pytest.skip("[data] docs/05 not in snapshot")
    row = next((ln for ln in doc.read_text(encoding="utf-8").splitlines() if ln.startswith("| D3-AC-61 ")), None)
    assert row is not None
    for clause, texts, *_ in AC61:
        for t in texts:
            assert t in row, (clause, t)
    assert "tests/d3/sensors/test_thermal.py" in row and "dmax（单卡绑定）" in row


# ---------------------------------------------------------------- 识别谓词（CPU）

def _row(h, vis, dT=None, *, kind="thermal", flags=None) -> dict:
    r = {"uav": "u1", "sensor": "therm0" if kind == "thermal" else "cam0", "seq": 5, "actor": "p1", "h_unocc_px": h, "vis": vis}
    if kind == "thermal":
        r["kind"] = "thermal"
    if dT is not None:
        r["dT_app_k"] = dT
    if flags:
        r["flags"] = flags
    return r


@pytest.mark.parametrize(("row", "want"), [
    (_row(16, 0.5, 1.0), True),                                   # 三项均恰在阈值上（闭区间）
    (_row(16, 0.5, -1.0), True),                                  # 冷目标：取绝对值
    (_row(15, 1.0, 5.0), False),
    (_row(15.99, 1.0, 5.0), False),                               # L1 真值高度为两位小数
    (_row(40, 0.499999, 5.0), False),
    (_row(40, 1.0, 0.999999), False),
    (_row(40, 1.0, -0.999), False),
    (_row(40, 1.0, None, flags=["no_ring"]), False),              # 环带为空：表观温差无法确定，不计识别
    (_row(16, 0.5, kind="camera"), True),                         # 可见光与 L1 行不受温差判据约束
    (_row(15, 0.9, kind="camera"), False),
])
def test_ident_predicate_boundaries(row: dict, want: bool) -> None:
    """D3-AC-61 识别判据的谓词边界：h_px_unocc ≥ 16、可见比例 ≥ 0.5、热成像 |dT_app_k| ≥ 1 K，三项同时满足才识别。"""
    assert identified(row) is want
    assert is_thermal(row) is (row.get("kind") == "thermal")
    if "dT_app_k" in row or row.get("flags"):                       # 缺 kind 时按 dT_app_k / no_ring 判为热成像行，结果不变
        assert identified({k: v for k, v in row.items() if k != "kind"}) is want


def test_ident_thresholds_are_parameterised() -> None:
    r = _row(20, 0.8, 1.5)
    assert identified(r) and not identified(r, Thresholds(dT_app_k=2.0)) and not identified(r, Thresholds(h_px=24.0))
    assert TH.dT_app_k == DT_K


def _synthetic_frame():
    """CPU 构造帧（64 × 96，背景 290 K，各演员块的高度、遮挡与温度按判据边界设定）。返回 (t_app, inst, unocc, sem, actors, 期望)。"""
    H, W = 64, 96
    t = np.full((H, W), 290.0, np.float32)
    inst = np.zeros((H, W), np.uint16)
    unocc = np.zeros((H, W), np.uint16)
    actors, want = [], {}

    def put(aid, rows, cols, temp, *, occ_rows=0, ok):
        k = len(actors) + 1
        actors.append({"id": aid, "class": "person"})
        r0, r1 = rows
        unocc[r0:r1, cols[0]:cols[1]] = k
        inst[r0:r1 - occ_rows, cols[0]:cols[1]] = k                # 下沿 occ_rows 行被静态几何遮挡（该处为背景温度）
        t[r0:r1 - occ_rows, cols[0]:cols[1]] = temp
        want[aid] = ok

    put("edge", (4, 20), (4, 8), 291.0, occ_rows=8, ok=True)        # h 16、可见 8/16 = 0.5、dT +1.0：恰在阈值上
    put("short", (4, 19), (16, 20), 300.0, ok=False)                 # h 15
    put("occluded", (4, 24), (28, 32), 300.0, occ_rows=11, ok=False)  # 可见 9/20 = 0.45
    put("faint", (4, 24), (40, 44), 290.9, ok=False)                # dT +0.9
    put("cold", (4, 24), (52, 56), 288.8, ok=True)                  # dT −1.2
    put("inner", (34, 54), (70, 74), 300.0, ok=False)               # 被另一名演员包围：环带为空（no_ring）
    k = len(actors) + 1
    actors.append({"id": "outer", "class": "person"})
    ring = np.zeros((H, W), bool)
    ring[30:58, 66:78] = True
    ring[34:54, 70:74] = False
    inst[ring], unocc[ring], t[ring] = k, k, 296.0
    want["outer"] = True
    sem = np.where(inst > 0, 32, 1).astype(np.uint8)
    return t, inst, unocc, sem, actors, want


def test_thermal_objects_construct_through_ident_table() -> None:
    """可见度归约与 `dT_app_k`（`render.thermal_objects`）→ 识别行（`thermal_rows`）→ 识别表（`build_ident`）；
    同一机同一帧的可见光行与热成像行取并集，热成像独立满足判据的目标进入 EC。"""
    t, inst, unocc, sem, actors, want = _synthetic_frame()
    objs = {o["actor"]: o for o in tr.thermal_objects(t, inst, unocc, sem, actors)}
    assert set(objs) == set(want)
    e = objs["edge"]
    assert (e["h_px_unocc"], e["visible_ratio"], e["dT_app_k"]) == (16, 0.5, 1.0)
    assert objs["short"]["h_px_unocc"] == 15 and objs["occluded"]["visible_ratio"] == 0.45
    assert objs["faint"]["dT_app_k"] == pytest.approx(0.9, abs=1e-4) and objs["cold"]["dT_app_k"] == pytest.approx(-1.2, abs=1e-4)
    assert "dT_app_k" not in objs["inner"] and "no_ring" in objs["inner"]["flags"]
    rows = thermal_rows(objs.values(), uav="u1", sensor="therm0", seq=12, t_ns=1_200_000_000)
    assert all(r["kind"] == "thermal" and r["seq"] == 12 for r in rows)
    assert {r["actor"]: identified(r) for r in rows} == want
    ident = build_ident(SimpleNamespace(l1=rows))
    assert ident == {12: {("u1", a) for a, ok in want.items() if ok}}

    # 并集：可见光行判据独立；热成像判据只约束热成像行
    cam_rows = [{"uav": "u2", "sensor": "cam0", "seq": 12, "actor": "faint", "h_unocc_px": 30, "vis": 1.0}]
    ident = build_ident(SimpleNamespace(l1=rows + cam_rows))
    assert ("u2", "faint") in ident[12] and ("u1", "faint") not in ident[12]
    E = [{"id": "e1", "type": "loiter", "participants": ["cold"], "a_s": 1.0, "b_s": 1.5, "multi": False, "params": {}},
         {"id": "e2", "type": "loiter", "participants": ["faint"], "a_s": 1.0, "b_s": 1.5, "multi": False, "params": {}}]
    present = {a["id"]: np.ones(31, bool) for a in actors}
    _ec, _cov, out = event_capture(E, ["u1"], build_ident(SimpleNamespace(l1=rows)), present, 30)
    assert out["e1"]["captured"] and not out["e2"]["captured"]


def test_thermal_frame_objects_feed_ident() -> None:
    """帧记录 `ground_truth.objects[]`（`thermal_frame`）→ 识别行：字段口径与 `thermal_objects` 相同，`kind = thermal`。"""
    pytest.importorskip("zstandard")
    from awr.sensorsvc.frame import FrameContext
    from awr.sensorsvc.poses import Mount

    m = tr.THERM512
    t, inst, unocc, sem, actors, want = _synthetic_frame()
    big = np.full((m.height, m.width), 290.0, np.float32)
    bi, bu = (np.zeros((m.height, m.width), np.uint16) for _ in range(2))
    bs = np.ones((m.height, m.width), np.uint8)
    big[100:164, 200:296], bi[100:164, 200:296], bu[100:164, 200:296], bs[100:164, 200:296] = t, inst, unocc, sem
    ck = np.round(big * 100).astype(np.uint16)
    out = tr.ThermalOut(ck, big, None, bi, bs, bu, np.array([1.0]), np.array([0.4]), 1.0, 1.0, 1, len(actors))
    man = tr.thermal_manifest(tr.load_thermal_table(), ttr.rad.planck_table(), m)
    rec, _blocks = tr.thermal_frame(FrameContext("run1", "s1", "cv1", "A40", "580"), uav="u3", sensor_id="therm0", seq=4, t_mid_ns=400_000_000,
                                    model=m, mount=Mount(), gimbal_reading=(0, 0, 0), gimbal_true=(0, 0, 0), pos_enu_m=np.zeros(3), R_ws=np.eye(3),
                                    out=out, manifest=man, actors=actors)
    rows = thermal_rows(rec["ground_truth"]["objects"], uav="u3", sensor="therm0", seq=4, t_ns=400_000_000)
    assert {r["actor"]: identified(r) for r in rows} == want


# ---------------------------------------------------------------- 识别判据的 GPU 构造（dmax）

def _proxy_rows(actor: dict, P, R, p, model) -> tuple[int, np.ndarray]:
    """独立期望：代理网格顶点投影（yaw = 0）的像素行范围 → 按像素中心覆盖规则的高度（px），及顶点像素坐标。"""
    from awr.sensorsvc.camera.actors import proxy_mesh
    from awr.sensorsvc.camera.proj import project_points

    X = proxy_mesh(actor)[0].astype(np.float64) + np.asarray(P, np.float64)
    uv, z = project_points(model.camera(), R, p, X)
    assert (z > 0).all()
    v0, v1 = float(uv[:, 1].min()), float(uv[:, 1].max())
    return math.floor(v1 - 0.5) - math.ceil(v0 - 0.5) + 1, uv


def _view_person(gl_world, actor: dict, P, C, env, *, cam=None, model=CLEAN):
    """相机 C 看向演员 P（瞄准身高中点，`look_at_R`）；无噪声档、静止机体。返回 (渲染输出, objects, R)。"""
    from awr.sensorsvc.camera.proj import look_at_R
    from awr.sensorsvc.camera.raster import ActorFrame

    cam = cam or ttr._cam(gl_world)
    P, C = np.asarray(P, np.float64), np.asarray(C, np.float64)
    R = look_at_R(C, P + np.array([0.0, 0.0, actor["size_mm"][2] / 2000.0]))
    cam.set_actors([actor])
    th = [tr.actor_thermal(actor, cam.table, env, NIGHT)]
    frame = ActorFrame(np.array([0]), P[None], np.array([0.0]))
    o = ttr._render1(cam, R, C, env, model, actor_frames=[frame] * model.lag_subframes, actor_th=th)
    objs = tr.thermal_objects(o.t_app_k, o.instance, o.instance_unocc, o.semantic, [actor])
    assert len(objs) == 1
    return o, objs[0], R


def _slant(P, d_m: float, dep_deg: float) -> np.ndarray:
    """沿 −x 方向、俯角 dep_deg、斜距 d_m 的相机位置。"""
    a = math.radians(dep_deg)
    return np.asarray(P, np.float64) + d_m * np.array([-math.cos(a), 0.0, math.sin(a)])


def test_gpu_ident_projected_height(gl_world) -> None:
    """距离构造（俯角 20°，夜间、云量 0.5，人员无遮挡）：斜距 90 m 时 h_px_unocc ≥ 16 判为识别，135 m 时 < 16 不识别；
    渲染的无遮挡高度与代理网格投影的独立期望差 ≤ 1 px，可见比例 1，表观温差 ≫ 1 K（只由高度决定结果）。"""
    env = tr.thermal_env(ttr._table(), T_AIR, cloud=0.5)
    P = np.array([0.0, -60.0, 0.0])
    cam = ttr._cam(gl_world)
    rep = []
    for d, ok in ((90.0, True), (135.0, False)):
        C = _slant(P, d, 20.0)
        _o, ob, R = _view_person(gl_world, PERSON, P, C, env, cam=cam)
        h_exp, _uv = _proxy_rows(PERSON, P, R, C, CLEAN)
        rep.append((d, ob["h_px_unocc"], h_exp, ob["visible_ratio"], ob["dT_app_k"]))
        assert abs(ob["h_px_unocc"] - h_exp) <= 1 and (h_exp >= H_PX) is ok and (ob["h_px_unocc"] >= H_PX) is ok, rep
        assert ob["visible_ratio"] == 1.0 and ob["dT_app_k"] > 5.0 and not ob["flags"]
        row = thermal_rows([ob], uav="u1", sensor="therm0", seq=1, t_ns=0)[0]
        assert identified(row) is ok
    print("ident height", rep)
    cam.release()


def test_gpu_ident_visible_ratio(gl_world) -> None:
    """遮挡构造：相机 (90, 10, 16) 西望，人员站在盒子（x 40–60、y −10–10、高 30 m）背后 (30, 10 + s, 0)。相机与平面 y = 10 共面，
    盒子北缘在像面上投影为一条竖线，人员只有 y > 10 一侧可见；可见比例的独立期望 = 无遮挡掩膜中位于该线可见一侧的像素比例。
    s = +0.1 m 时可见比例 ≥ 0.5 识别，s = −0.1 m 时 < 0.5 不识别（高度 ≥ 16 px、|dT| ≥ 1 K 两项都满足）。"""
    from awr.sensorsvc.camera.proj import project_points

    env = tr.thermal_env(ttr._table(), T_AIR, cloud=0.5)
    C = np.array([90.0, 10.0, 16.0])
    cam = ttr._cam(gl_world)
    rep = []
    for s, ok in ((0.1, True), (-0.1, False)):
        P = np.array([30.0, 10.0 + s, 0.0])
        o, ob, R = _view_person(gl_world, PERSON, P, C, env, cam=cam)
        uv = project_points(CLEAN.camera(), R, C, np.array([[40.0, 10.0, 0.0], [40.0, 10.0, 3.0], [30.0, 12.0, 0.9]]))[0]
        (u0, v0), (u1, v1), (u2, v2) = uv                                 # 平面 y = 10 过相机中心：像面上为过前两点的直线

        def side(u, v, u0=u0, v0=v0, u1=u1, v1=v1):
            return (u1 - u0) * (v - v0) - (v1 - v0) * (u - u0)

        vv, uu = np.nonzero(o.instance_unocc == 1)
        vis_exp = float(np.mean(np.sign(side(uu + 0.5, vv + 0.5)) == np.sign(side(u2, v2))))
        rep.append((s, ob["visible_ratio"], round(vis_exp, 4), ob["h_px_unocc"], ob["dT_app_k"]))
        assert abs(ob["visible_ratio"] - vis_exp) <= 0.01 and (vis_exp >= VIS) is ok, rep
        assert ob["h_px_unocc"] >= H_PX and abs(ob["dT_app_k"]) >= DT_K and ob["occluder_sem_px"], rep
        assert identified(thermal_rows([ob], uav="u1", sensor="therm0", seq=1, t_ns=0)[0]) is ok
    print("ident visibility", rep)
    cam.release()


def test_gpu_ident_temperature_contrast(gl_world) -> None:
    """温差构造（斜距 90 m、俯角 20°，人员无遮挡）：先渲染缺省人员得到其 3 px 环带的表观温度 T_ring，再以 DSL
    `thermal.surface_temp_c = T_ring + δ`、`emissivity = 1` 渲染，`dT_app_k` 与 δ 差 ≤ 0.1 K（皮肤分区只占少数像素，中位数取着装温度；
    余差为 90 m 大气路径辐射）；
    |δ| = 0、0.5 K 不识别，δ = +1.5、−1.5、−3 K 识别（冷目标取绝对值）。随后经帧记录 → 识别行 → 识别表核对端到端结果。
    光学模糊取 0：否则人员边缘像素与环带内侧像素按温差混合，中位数随 δ 的符号非对称偏移（皮肤分区恒热），期望值不再只由 δ 决定。"""
    pytest.importorskip("zstandard")
    from awr.sensorsvc.frame import FrameContext
    from awr.sensorsvc.poses import Mount

    env = tr.thermal_env(ttr._table(), T_AIR, cloud=0.5)
    P = np.array([0.0, -60.0, 0.0])
    C = _slant(P, 90.0, 20.0)
    cam = ttr._cam(gl_world)
    sharp = tr.ThermalModel(noise="clean", optics_sigma_px=0.0)
    o, ob, R = _view_person(gl_world, PERSON, P, C, env, cam=cam, model=sharp)
    assert ob["dT_app_k"] > 5.0
    m = o.instance == 1
    t_ring = float(np.median(np.asarray(o.t_app_k, np.float64)[m])) - ob["dT_app_k"]
    rows, want, rep = [], {}, []
    man = tr.thermal_manifest(cam.table, cam.planck, sharp)
    for k, (dl, ok) in enumerate(((0.0, False), (0.5, False), (-0.5, False), (1.5, True), (-1.5, True), (-3.0, True))):
        actor = {**PERSON, "id": f"p{k}", "thermal": {"surface_temp_c": round(t_ring + dl - 273.15, 4), "emissivity": 1.0}}
        o2, ob2, _ = _view_person(gl_world, actor, P, C, env, cam=cam, model=sharp)
        rep.append((dl, ob2["dT_app_k"]))
        assert abs(ob2["dT_app_k"] - dl) <= DT_CONSTRUCT_TOL_K and ob2["h_px_unocc"] >= H_PX and ob2["visible_ratio"] == 1.0, rep
        rec, _ = tr.thermal_frame(FrameContext("run-s18", "s1", "cv1", "A40", "580"), uav="u1", sensor_id="therm0", seq=k, t_mid_ns=k * 100_000_000,
                                  model=sharp, mount=Mount(), gimbal_reading=(0, 0, 0), gimbal_true=(0, 0, 0), pos_enu_m=C, R_ws=R, out=o2,
                                  manifest=man, actors=[actor])
        assert rec["ground_truth"]["objects"][0]["dT_app_k"] == ob2["dT_app_k"]
        rows += thermal_rows(rec["ground_truth"]["objects"], uav="u1", sensor="therm0", seq=k, t_ns=k * 100_000_000)
        want[k] = ok
    print("ident contrast", f"T_ring {t_ring:.3f} K", rep)
    ident = build_ident(SimpleNamespace(l1=rows))
    assert {k: bool(ident.get(k)) for k in want} == want
    cam.release()


# ---------------------------------------------------------------- 同镜像同型号 GPU：两个独立进程逐字节一致（dmax）

DET_KEYS = ("temp_ck", "t_app_k", "instance", "semantic", "instance_unocc", "depth_m")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def determinism_workload(work: Path) -> dict:
    """独立进程内的确定性负载：新建 EGL 上下文与夹具世界，昼间（numpy 日照烘焙）、雾 3 km、运动机体 4 子帧、运动人员与运转车辆、
    realistic 噪声，连续 3 帧；返回逐数组、u16 + zstd 交付字节、帧记录与载荷块的 sha256，以及 GL 设备标识。"""
    import moderngl

    from awr.sensorsvc.camera.proj import gimbal_R
    from awr.sensorsvc.camera.raster import ActorFrame, RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance
    from awr.sensorsvc.frame import FrameContext
    from awr.sensorsvc.poses import Mount

    ctx = moderngl.create_standalone_context(backend="egl", require=450)
    V, F, _UV = scene_enu()
    cen = V[F.astype(np.int64)].mean(1)
    cls = np.where(cen[:, 2] > 1e-6, ttr.BOX_CLS, ttr.GROUND_CLS).astype(np.uint8)
    root = ttr._with_cls(write_world(work), cls)
    scene = load_appearance(root, tile_m=64.0)
    raster = RasterCamera(scene, ctx=ctx, textures=False)
    table = tr.load_thermal_table()
    static = tr.thermal_bake(scene, DAY, backend="numpy")
    cam = tr.ThermalCamera(scene, table=table, tri_cls=tr.static_classes(root, scene.triangles), static=static, sun_az_el_deg=DAY, raster=raster)
    model = tr.THERM512
    env = tr.thermal_env(table, T_AIR, cloud=0.2, rh=0.6, fog_visibility_km=3.0)
    cam.set_actors([PERSON, VEHICLE])
    th = [tr.actor_thermal(PERSON, table, env, DAY), tr.actor_thermal(VEHICLE, table, env, DAY, engine_off=None)]
    info = {"gl_renderer": ctx.info.get("GL_RENDERER"), "gl_version": ctx.info.get("GL_VERSION"), "static_npz": _sha(static.npz)}
    fctx = FrameContext("run-s18", "s1", "cv-fixture", str(info["gl_renderer"]), str(info["gl_version"]))
    man = tr.thermal_manifest(table, cam.planck, model, static)
    frames = []
    for k in range(3):
        t_end = 12.0 + 0.1 * k
        ts = tr.subframe_times_s(t_end, model)
        poses = [(gimbal_R(20.0 + 90.0 * (s - 12.0), -20.0), np.array([-60.0 + 8.0 * (s - 12.0), -20.0, 30.0])) for s in ts]
        fr = [ActorFrame(np.array([0, 1]), np.array([[0.0, -10.0, 0.0], [8.0 + 6.0 * (s - 12.0), 5.0, 0.0]]), np.array([0.3, 1.2])) for s in ts]
        o = cam.render(model, poses, ts, t_end, env, actor_frames=fr, actor_th=th, noise=tr.NoiseKeys(20261019, 1, 0, k), depth=True)
        d = {key: _sha(np.ascontiguousarray(getattr(o, key)).tobytes()) for key in DET_KEYS}
        rec, blocks = tr.thermal_frame(fctx, uav="u1", sensor_id="therm0", seq=k, t_mid_ns=round(t_end * 1e9), model=model, mount=Mount(),
                                       gimbal_reading=(0, 0, 0), gimbal_true=(0, 0, 0), pos_enu_m=poses[-1][1], R_ws=poses[-1][0], out=o,
                                       manifest=man, actors=[PERSON, VEHICLE])
        d["temp_ck_zstd"] = rec["agent_view"]["temp_ck"]["sha256"]
        d["record"] = _sha(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode())
        d["blocks"] = {ref: _sha(b) for ref, b in sorted(blocks.items())}
        d["objects"] = [{k2: ob[k2] for k2 in ("actor", "h_px_unocc", "visible_ratio", "dT_app_k") if k2 in ob} for ob in rec["ground_truth"]["objects"]]
        frames.append(d)
    try:
        import warp  # noqa: F401

        info["warp_bake_npz"] = _sha(tr.thermal_bake(scene, (100.0, 30.0), backend="warp").npz)
    except ImportError:
        pass
    cam.release()
    raster.release()
    return {"pid": os.getpid(), "info": info, "frames": frames}


def test_gpu_two_processes_byte_identical(tmp_path: Path) -> None:
    """D3-AC-61：同镜像同型号 GPU 两次运行逐字节一致。两个独立 Python 进程各自建 EGL 上下文、世界与着色器，渲染相同的 3 帧
    （运动机体、运动演员、日照、雾、realistic 噪声）：全部数组、交付字节、帧记录与载荷块的 sha256 相同；帧间互不相同（噪声帧号生效）。"""
    pytest.importorskip("moderngl")
    pytest.importorskip("zstandard")
    import moderngl

    try:
        moderngl.create_standalone_context(backend="egl", require=450).release()
    except Exception as e:                                               # 无 EGL 设备
        pytest.skip(f"[gpu] EGL unavailable: {e}")
    import awr

    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(awr.__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")])}
    runs = []
    for i in range(2):
        out = tmp_path / f"run{i}.json"
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--determinism-worker", str(out), str(tmp_path / f"w{i}")], env=env,
                       check=True, timeout=900)
        runs.append(json.loads(out.read_text(encoding="utf-8")))
    a, b = runs
    assert a["pid"] != b["pid"] and a["info"] == b["info"], (a["info"], b["info"])
    assert a["frames"] == b["frames"]
    assert len({f["temp_ck"] for f in a["frames"]}) == 3 and len({f["record"] for f in a["frames"]}) == 3
    assert any(ob.get("dT_app_k") is not None for f in a["frames"] for ob in f["objects"])
    print("two-process digests", json.dumps({"info": a["info"], "frame0": a["frames"][0]}, ensure_ascii=False))
    report = os.environ.get("AWR_AC61_REPORT")
    if report:                                                           # 验收报告取证（RP0b）：逐帧摘要与设备标识
        Path(report).write_text(json.dumps({"clause": "gpu_byte_identical", "runs": runs}, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--determinism-worker":
    _w = Path(sys.argv[3])
    _w.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=_w) as _d:
        Path(sys.argv[2]).write_text(json.dumps(determinism_workload(Path(_d))), encoding="utf-8")
