"""D3-AC-62 / D3-AC-58（M26-FR-122 P0b，M26 §14.3）：基线按 report 类型输出四类事件的证据，并可选使用热成像通道。

构造观测（不经物理档与渲染，L1 检测按脚本逐帧给出，agent_config 通过 `bench/agent_config.v1` 校验）：
1. 交汇与交接：`meet`、`handover`、`disperse`（两人相距 2 m 驻留后分开 ≥ 10 m）与 `alight`（人员在静止车辆旁出现）；
2. Re-ID：目标进入建筑后从门口重新出现（`enter_building`、`exit_building`、`reacquire`），树冠下 6 s 遮挡后同一轨迹恢复
   （第二次 `reacquire`）；上衣同色、下装不同的干扰者在目标离场后出现，不得报为重新捕获；
3. 车辆追踪：目标车辆起步、转弯、临停、泊车（`vehicle_event` 的 `event` 为 depart、turn、stop、park），泊车期间 5 s 遮挡后
   报 `identity_link`；异色干扰车辆转弯不报；
4. 目标搜索：`brief.search` 先验（gmm）下报一次 `found`：外观未知时取静止且先验密度最高的人员，外观已知时取外观匹配者；
   `coverage_scan` 的航带按先验质量排序，`static_guard` 的观察位取先验中心。
每个场景对三个基线逐一运行两次：动作流逐字节一致；全部动作通过 `bench/act.v1`；report 经动作守卫回执 `applied`；
每个参与者的证据（1–3 条）指回同一演员且不晚于窗口终点；report 类型与参与者集合等于预期。
5. 热成像（`ref_thermal_blob_v1`）：`decode_temp_ck` 与契约金标向量、`awr.agentsdk.schemas.decode_thermal_obs` 一致；局部背景与
   暴力中值、连通域与暴力洪泛逐项一致；2 K 阈值与尺寸先验；地面投影误差 ≤ 0.5 m；夜间（相机无检测）只凭热成像帧报 `meet`，
   证据为热像仪帧框；关闭热成像通道时不报；两次运行逐字节一致。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "tools" / "sdk" / "anet4d" / "src"))

zstandard = pytest.importorskip("zstandard")

from awr.agentsdk.env.camera import CameraModel  # noqa: E402
from awr.agentsdk.env.guard import ActionGuard, Limits  # noqa: E402
from awr.agentsdk.schemas import bench_validator  # noqa: E402

POLICIES = ["static_guard", "coverage_scan", "track_share"]
DT_NS = 100_000_000                       # Env 控制周期 10 Hz（awr.agentsdk.env.core.TICK_MS）
CAM, TH = "eo", "therm0"
TH_W, TH_H, TH_F = 320, 256, 343.0
AREA = [[-20.0, -100.0], [80.0, -100.0], [80.0, 0.0], [-20.0, 0.0]]
DOOR = {"id": "ev_door", "kind": "door", "name": "Door", "pos_enu_m": [28.0, -40.0, 0.0]}
EV_KEYS = ("bbox_xywh", "point_xy", "det_id")


def _canon(o) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=lambda b: b.hex()).encode()


def _config(vocab: list[str], *, targets=(), anchors=(), area=AREA, start=(0.0, 0.0), obs_level="L1", thermal=False) -> dict:
    sensors = [CameraModel(sensor_id=CAM).config()]
    if thermal:
        sensors.append({"sensor_id": TH, "kind": "thermal", "model_ref": "d4d_therm320@1", "rate_hz": 10.0,
                        "mount_nominal": {"t_bs_m": [0.12, 0.0, -0.08], "q_bs_xyzw": [0.0, 0.0, 0.0, 1.0]}, "width": TH_W, "height": TH_H,
                        "K": [TH_F, 0.0, TH_W / 2, 0.0, TH_F, TH_H / 2, 0.0, 0.0, 1.0], "dist": [0.0] * 5})
    cfg = {"schema": "drone4d.bench.agent_config.v1", "uav_id": "uav-01", "kind": "uav", "peers": ["uav-02"],
           "vehicle": {"ref": "x500", "limits": Limits().config(), "endurance_s": 1500.0}, "sensors": sensors,
           "start": {"kind": "pose", "state": "landed", "nav_pos_enu_m": [start[0], start[1], 0.0]}, "obs_level": obs_level, "control_hz": 10,
           "decision": {"mode": "fixed", "tau_ms": 0, "tau_min_ms": 0, "step_budget_s": 2.0, "wall_ratio": 1.0},
           "comm": {"enabled": True, "bucket_Bps": 4000, "burst_B": 8000, "msg_max_B": 4096, "model": "ideal"},
           "episode": {"duration_s": 90.0}, "world": {"id": "synth", "content_version": "1", "origin": {"lat_deg": 0.0, "lon_deg": 0.0, "alt_m": 0.0}},
           "brief": {"task": {"type": "event_capture", "description": "constructed"}, "area": {"polygon_enu_m": area, "alt_agl_range_m": [30.0, 120.0]},
                     "keepouts": [], "anchors": list(anchors), "targets": list(targets), "report_vocab": sorted(vocab), "maps": {"dsm": "point"},
                     "nominal_noise": {}},
           "protocol": "drone4d.bench.wire.v1"}
    errs = [e.message for e in bench_validator("agent_config").iter_errors(cfg)]
    assert not errs, errs
    return cfg


def _l1(name: str, cls: str, xy, k: int, j: int, app: dict | None) -> dict:
    d = {"det_id": hashlib.sha256(f"{name}|{k}".encode()).hexdigest()[:16], "sensor_id": CAM, "seq": k, "t_mid_ns": k * DT_NS, "class": cls,
         "bbox_xywh": [100.0 + 60.0 * j, 300.0, 18.0, 40.0 if cls == "person" else 30.0], "h_px": 40.0, "slant_range_m": 80.0,
         "ground_xy_noisy": [round(float(xy[0]), 3), round(float(xy[1]), 3)]}
    if app is not None:
        d["appearance"] = app
    return d


def _guard(vocab: list[str]) -> ActionGuard:
    return ActionGuard("uav-01", ["uav-02"], Limits(), sorted(vocab), True, 4096, CameraModel(sensor_id=CAM))


def _run(policy: str, cfg: dict, script, seconds: float, *, nav_xy=(0.0, 0.0), thermal_scene=None, thermal=None):
    """逐步构造观测并经动作守卫核对 → (动作与回执列表, 证据所有者映射, 智能体)。"""
    from anet4d.baselines import BASELINES

    ag = BASELINES[policy](thermal=thermal)
    ag.reset(copy.deepcopy(cfg))
    guard = _guard(cfg["brief"]["report_vocab"])
    owner: dict = {}
    seen_th: list[dict] = []
    if ag.thermal is not None:                            # 记录热成像检测，按地面位置把热像仪框归属到演员
        inner = ag.thermal.detect

        def detect(obs):
            dets = inner(obs)
            seen_th.extend(dets)
            return dets

        ag.thermal.detect = detect
    out = []
    for k in range(int(seconds * 10) + 1):
        t = k * DT_NS / 1e9
        dets = []
        for j, (name, cls, xy, app) in enumerate(script(t)):
            d = _l1(name, cls, xy, k, j, app)
            owner[d["det_id"]] = name
            dets.append(d)
        z = 0.0 if k == 0 else ag.alt
        sensors = {"detections": dets}
        if thermal_scene is not None:
            sensors = {CAM: [], TH: [_thermal_frame(k, thermal_scene(t), z)]}
            guard.frames.add((TH, k))                     # 交付登记：热像仪帧（Env 接线后由 Env 登记）
        else:
            guard.saw(dets)
        obs = {"schema": "drone4d.bench.obs.v1", "k": k, "t_ns": k * DT_NS,
               "nav": {"pos_enu_m": [nav_xy[0], nav_xy[1], z], "q_wb_xyzw": [0.0, 0.0, 0.0, 1.0]},
               "status": {"mode": "landed" if k == 0 else "airborne"}, "ground_hint": {"dsm_m": 0.0, "dtm_m": 0.0}, "sensors": sensors, "acks": []}
        n0 = len(seen_th)
        act = ag.step(obs, [])
        if thermal_scene is not None:
            for d in seen_th[n0:]:
                near = min(thermal_scene(t), key=lambda p: math.dist(p[1], d["ground_xy_noisy"]))
                if math.dist(near[1], d["ground_xy_noisy"]) <= 1.0:
                    owner[(d["sensor_id"], d["seq"], tuple(d["bbox_xywh"]))] = near[0]
        r = guard.check(act, k, np.array([nav_xy[0], nav_xy[1], z]), 0.0, np.zeros(3), 0.0)
        out.append((act, r.acks))
    return out, owner, ag


def _reports(policy: str, cfg: dict, script, seconds: float, **kw) -> list[tuple[str, frozenset, dict]]:
    """运行两次（逐字节一致）并核对动作、回执与证据 → [(类型, 参与演员集合, report)]，按发出次序。"""
    run1, owner, _ = _run(policy, cfg, script, seconds, **kw)
    run2, _, _ = _run(policy, cfg, script, seconds, **kw)
    assert b"\n".join(_canon(a) for a, _ in run1) == b"\n".join(_canon(a) for a, _ in run2)        # 同一输入动作流逐字节一致
    val = bench_validator("act")
    got = []
    for act, acks in run1:
        assert not [e.message for e in val.iter_errors(act)], act
        assert not [x for x in acks if x["item"] == "act"], acks
        rep_acks = {x["index"]: x for x in acks if x["item"] == "report"}
        for i, rep in enumerate(act.get("report", [])):
            assert rep_acks[i]["result"] == "applied", (rep, rep_acks[i])
            assert rep["window"]["t_start_ns"] <= rep["window"]["t_end_ns"]
            actors = set()
            for p in rep["participants"]:
                assert 1 <= len(p["evidence"]) <= 3, p
                names = set()
                for e in p["evidence"]:
                    kinds = [k for k in EV_KEYS if k in e]
                    assert len(kinds) == 1 and e["uav"] == "uav-01", e
                    key = e["det_id"] if "det_id" in e else (e["sensor"], e["frame_seq"], tuple(e["bbox_xywh"]))
                    names.add(owner[key])
                    assert e["frame_seq"] * DT_NS <= rep["window"]["t_end_ns"], (e, rep["window"])   # 证据不晚于窗口终点
                assert len(names) == 1, p                                                          # 一个参与者的证据指向同一演员
                actors |= names
            got.append((rep["type"], frozenset(actors), rep))
    return got


def _summary(got) -> list[tuple[str, tuple[str, ...]]]:
    return [(ty if ty != "vehicle_event" else f"vehicle_event:{rep['event']}", tuple(sorted(a))) for ty, a, rep in got]


# ------------------------------------------------------------------ 1. 交汇与交接
VOCAB_HANDOFF = ["alight", "board", "disperse", "enter_building", "handover", "meet", "relation"]


def _handoff_script(t: float):
    out = [("P1", "person", (0.0, 0.0), None),
           ("P2", "person", (2.0, 0.0) if t <= 20.0 else (2.0, 1.5 * (t - 20.0)), None),
           ("V1", "vehicle", (40.0, 0.0), None)]
    if t >= 8.0:
        out.append(("P5", "person", (41.5, 1.0 * (t - 8.0)), None))
    return out


@pytest.mark.parametrize("policy", POLICIES)
def test_rendezvous_reports(policy: str) -> None:
    got = _reports(policy, _config(VOCAB_HANDOFF), _handoff_script, 40.0)
    assert sorted(_summary(got)) == sorted([("meet", ("P1", "P2")), ("handover", ("P1", "P2")), ("disperse", ("P1", "P2")),
                                            ("alight", ("P5", "V1"))]), _summary(got)
    by = {ty: rep for ty, _a, rep in got}
    meet, ho, dis = by["meet"]["window"], by["handover"]["window"], by["disperse"]["window"]
    assert ho["t_start_ns"] == meet["t_start_ns"] and ho["t_end_ns"] >= meet["t_end_ns"]          # 交接窗口为交汇区间
    assert dis["t_start_ns"] >= ho["t_end_ns"] and 26e9 <= dis["t_end_ns"] <= 30e9                  # 相距 ≥ 10 m（t ≈ 26.7 s，拟合滞后）
    assert 6e9 <= by["alight"]["window"]["t_start_ns"] <= by["alight"]["window"]["t_end_ns"] <= 8e9


# ------------------------------------------------------------------ 2. Re-ID
VOCAB_REID = ["alight", "board", "enter_building", "exit_building", "identity_link", "reacquire"]
TARGET_P = {"label": "target_1", "class": "person", "appearance": {"top_rgb": [200, 30, 30], "bottom_rgb": [20, 20, 20]}}
APP_T = {"top_rgb": [200, 30, 30], "bottom_rgb": [20, 20, 20]}
APP_D = {"top_rgb": [200, 30, 30], "bottom_rgb": [220, 220, 220]}          # 上衣同色、下装不同（难负样本）


def _reid_script(t: float):
    out = []
    if t <= 10.0:
        out.append(("T", "person", (28.0, -54.0 + 1.4 * t), APP_T))                 # 走向门（28, −40）后进入建筑
    elif 20.0 <= t <= 30.0 or 36.0 <= t <= 44.0:
        out.append(("T", "person", (28.0, -40.0 - (t - 20.0)), APP_T))              # 门口出现；30–36 s 树冠遮挡
    if t >= 50.0:
        out.append(("D", "person", (60.0, -60.0), APP_D))
    out.append(("B", "person", (0.0, -80.0), {"top_rgb": [30, 30, 30], "bottom_rgb": [30, 30, 30]}))
    return out


@pytest.mark.parametrize("policy", POLICIES)
def test_reid_reports(policy: str) -> None:
    cfg = _config(VOCAB_REID, targets=[TARGET_P], anchors=[DOOR], start=(28.0, -40.0))
    got = _reports(policy, cfg, _reid_script, 60.0, nav_xy=(28.0, -40.0))
    assert _summary(got) == [("enter_building", ("T",)), ("reacquire", ("T",)), ("exit_building", ("T",)), ("reacquire", ("T",))], _summary(got)
    reacq = [rep["window"]["t_start_ns"] for ty, _a, rep in got if ty == "reacquire"]
    assert reacq == [20 * 10**9, 36 * 10**9]                                          # 重新可见的首帧（门口出现、树冠后恢复）
    ex = next(rep for ty, _a, rep in got if ty == "exit_building")
    assert ex["window"]["t_end_ns"] == 20 * 10**9 and math.dist(ex["position_enu_m"][:2], DOOR["pos_enu_m"][:2]) <= 4.0


# ------------------------------------------------------------------ 3. 车辆追踪
VOCAB_VEHICLE = ["identity_link", "vehicle_event"]
TARGET_V = {"label": "target_1", "class": "vehicle", "appearance": {"top_rgb": [30, 30, 200]}}


def _vehicle_xy(t: float) -> tuple[float, float]:
    if t <= 8.0:
        return (0.0, 0.0)
    if t <= 16.0:
        return (8.0 * (t - 8.0), 0.0)
    if t <= 24.0:
        return (64.0, 8.0 * (t - 16.0))
    if t <= 30.0:
        return (64.0, 64.0)
    if t <= 36.0:
        return (64.0, 64.0 + 8.0 * (t - 30.0))
    return (64.0, 112.0)


def _vehicle_script(t: float):
    out = []
    if not 40.0 <= t < 45.0:                                                          # 泊车期间 5 s 遮挡
        out.append(("V", "vehicle", _vehicle_xy(t), {"top_rgb": [30, 30, 200]}))
    w = (8.0 * t, 50.0) if t <= 10.0 else (80.0, 50.0 + 8.0 * (t - 10.0))         # 异色干扰车辆：起步即转弯
    out.append(("W", "vehicle", w, {"top_rgb": [200, 200, 30]}))
    return out


@pytest.mark.parametrize("policy", POLICIES)
def test_vehicle_track_reports(policy: str) -> None:
    got = _reports(policy, _config(VOCAB_VEHICLE, targets=[TARGET_V]), _vehicle_script, 75.0)
    assert _summary(got) == [("vehicle_event:depart", ("V",)), ("vehicle_event:turn", ("V",)), ("vehicle_event:stop", ("V",)),
                             ("identity_link", ("V",)), ("vehicle_event:park", ("V",))], _summary(got)
    w = {(rep.get("event") or ty): rep["window"] for ty, _a, rep in got}
    assert 8e9 <= w["depart"]["t_end_ns"] <= 10e9
    assert 16e9 <= w["turn"]["t_end_ns"] <= 19e9
    assert 24e9 <= w["stop"]["t_start_ns"] < 30e9 <= w["stop"]["t_end_ns"] <= 32e9                  # 窗口为静止区间
    assert w["identity_link"]["t_start_ns"] == 45 * 10**9
    assert w["park"]["t_start_ns"] >= 36e9 and w["park"]["t_end_ns"] - w["park"]["t_start_ns"] >= 30e9


def test_act_contract_carries_vehicle_event_type() -> None:
    val = bench_validator("act")
    rep = {"type": "vehicle_event", "event": "turn", "participants": [{"track_id": "t1", "evidence": [
        {"uav": "uav-01", "sensor": CAM, "frame_seq": 3, "det_id": "0123456789abcdef"}]}], "window": {"t_start_ns": 0, "t_end_ns": 1}}
    assert not list(val.iter_errors({"schema": "drone4d.bench.act.v1", "k": 3, "report": [rep]}))
    assert list(val.iter_errors({"schema": "drone4d.bench.act.v1", "k": 3, "report": [{**rep, "event": "Turn Left"}]}))


# ------------------------------------------------------------------ 4. 目标搜索
SEARCH = {"target_class": "person", "motion": "static", "window_s": 60.0, "area_polygon": [[-150.0, -50.0], [150.0, -50.0], [150.0, 150.0], [-150.0, 150.0]],
          "prior": {"type": "gmm", "components": [{"xy": [100.0, 100.0], "sigma_m": 30.0, "weight": 0.7},
                                                  {"xy": [-100.0, 50.0], "sigma_m": 30.0, "weight": 0.3}]}}


def _search_cfg(targets=()) -> dict:
    cfg = _config(["found"], targets=targets, area=SEARCH["area_polygon"])
    cfg["brief"]["search"] = copy.deepcopy(SEARCH)        # brief.search 尚未进入 agent_config.v1（Env 投影待接线），校验后补入
    return cfg


def _search_script(t: float):
    return [("S1", "person", (-100.0, 50.0), {"top_rgb": [10, 200, 10]}),
            ("S2", "person", (95.0, 105.0), {"top_rgb": [120, 120, 120]}),
            ("W", "person", (100.0 + 1.0 * t, 95.0), {"top_rgb": [120, 120, 120]})]


@pytest.mark.parametrize("policy", POLICIES)
def test_search_found_once(policy: str) -> None:
    got = _reports(policy, _search_cfg(), _search_script, 15.0)
    assert _summary(got) == [("found", ("S2",))]                                      # 外观未知：静止且先验密度最高
    assert math.dist(got[0][2]["position_enu_m"][:2], (95.0, 105.0)) <= 1.0
    got = _reports(policy, _search_cfg([{"label": "target_1", "class": "person", "appearance": {"top_rgb": [10, 200, 10]}}]), _search_script, 15.0)
    assert _summary(got) == [("found", ("S1",))]                                      # 外观已知：外观匹配者（低先验处）


def test_search_prior_orders_lanes_and_posts() -> None:
    from anet4d.baselines import BASELINES
    from anet4d.baselines.prior import search_prior

    cfg = _search_cfg()
    pri = search_prior(cfg["brief"])
    assert pri.centers(2) == [(100.0, 100.0), (-100.0, 50.0)]
    assert pri.density(100.0, 100.0) > pri.density(-100.0, 50.0) > pri.density(0.0, -40.0)
    sg = BASELINES["static_guard"]()
    sg.reset(copy.deepcopy(cfg))
    assert sg.post == (100.0, 100.0)
    c2 = copy.deepcopy(cfg)
    c2["uav_id"], c2["peers"] = "uav-02", ["uav-01"]
    sg.reset(c2)
    assert sg.post == (-100.0, 50.0)
    cs = BASELINES["coverage_scan"]()
    cs.reset(copy.deepcopy(cfg))
    c2 = copy.deepcopy(cfg)
    c2["uav_id"], c2["peers"] = "uav-02", ["uav-01"]
    cs2 = BASELINES["coverage_scan"]()
    cs2.reset(c2)
    lanes = sorted({w[0] for w in cs.wps} | {w[0] for w in cs2.wps})
    assert abs(cs.wps[0][0] - 100.0) == min(abs(x - 100.0) for x in lanes)          # 先飞先验质量最高的航带（靠近权重 0.7 的分量）
    assert not {w[0] for w in cs.wps} & {w[0] for w in cs2.wps}                       # 两机分得的航带不重叠
    c3 = copy.deepcopy(cfg)
    c3["brief"]["search"]["prior"] = {"type": "grid", "grid": {"origin_xy": [0.0, 0.0], "cell_m": 10.0, "nx": 3, "ny": 2,
                                                               "weights": [0.0, 1.0, 0.0, 0.0, 0.0, 5.0]}}
    pg = search_prior(c3["brief"])
    assert pg.centers(3) == [(25.0, 15.0)]                                            # (15, 5) 距最高格心 < 40 m，不另设观察位
    assert pg.density(25.0, 15.0) == 5.0 / 100.0 and pg.density(-1.0, 0.0) == 0.0


# ------------------------------------------------------------------ 5. 热成像
def _golden(name: str) -> bytes:
    return bytes.fromhex("".join((ROOT / "packages/contracts/bench/golden/thermal" / name).read_text().split()))


def test_decode_temp_ck_matches_golden_and_env_decoder() -> None:
    import msgpack
    from anet4d.baselines.thermal import decode_temp_ck

    from awr.agentsdk.schemas import decode_thermal_obs

    frame = _golden("obs_thermal_8x6.hex")
    body = msgpack.unpackb(frame[8:], raw=False)
    th = [f for fs in body["obs"]["sensors"].values() for f in fs if isinstance(f, dict) and "temp_ck" in f]
    assert len(th) == 1
    img = decode_temp_ck(th[0])
    assert img.dtype == np.uint16 and img.shape == (6, 8)
    assert img.astype("<u2").tobytes() == _golden("temp_ck_8x6.u16le.hex")
    assert np.array_equal(img, decode_thermal_obs(th[0]))
    assert img[5, 7] == 30000 and img[0, 1] == 65535
    with pytest.raises(ValueError):
        decode_temp_ck({**th[0], "codec": "agc_jpeg"})


def test_background_and_components_match_brute_force() -> None:
    from anet4d.baselines.thermal import BG_STEP, BG_WIN, blobs, local_background

    rng = np.random.default_rng(7)
    t = (29000 + rng.integers(0, 60, (37, 45))).astype(np.uint16)
    bg = local_background(t)
    r = BG_WIN // 2
    pad = np.pad(t, r, mode="edge")
    for y in range(t.shape[0]):
        for x in range(t.shape[1]):
            cy = min((y // BG_STEP) * BG_STEP + BG_STEP // 2, t.shape[0] - 1)
            cx = min((x // BG_STEP) * BG_STEP + BG_STEP // 2, t.shape[1] - 1)
            assert bg[y, x] == int(np.median(pad[cy:cy + BG_WIN, cx:cx + BG_WIN]))
    hot = rng.random((60, 70)) < 0.12
    img = np.where(hot, 31000, 29000).astype(np.uint16)
    got = sorted((b["x0"], b["y0"], b["x1"], b["y1"], b["area"]) for b in blobs(img))
    lab = -np.ones(hot.shape, int)
    want = []
    for y0, x0 in zip(*np.nonzero(hot), strict=True):                                # 暴力 8 邻接洪泛
        if lab[y0, x0] >= 0:
            continue
        stack, pts = [(y0, x0)], []
        lab[y0, x0] = len(want)
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < hot.shape[0] and 0 <= xx < hot.shape[1] and hot[yy, xx] and lab[yy, xx] < 0:
                        lab[yy, xx] = len(want)
                        stack.append((yy, xx))
        ys, xs = [p[0] for p in pts], [p[1] for p in pts]
        want.append((min(xs), min(ys), max(xs), max(ys), len(pts)))
    assert got == sorted(want)


def _scene_image(people, z_cam: float) -> np.ndarray:
    """夜间地面 288 K（带确定性纹理），人员为 0.6 m 见方、+8 K 的热斑（正下视）。"""
    yy, xx = np.mgrid[0:TH_H, 0:TH_W]
    img = 28800 + ((xx * 7 + yy * 13) % 40)
    s = max(2, round(0.6 * TH_F / z_cam))
    for _name, (gx, gy) in people:
        v = TH_H / 2 - (gx - 0.12) / z_cam * TH_F                                   # 正下视反投影（见 ThermalPerception）
        u = TH_W / 2 - gy / z_cam * TH_F
        if not (s <= v <= TH_H and s <= u <= TH_W - s):
            continue
        img[round(v) - s:round(v), round(u - s / 2):round(u - s / 2) + s] = 29600
    return img.astype(np.uint16)


def _thermal_frame(k: int, people, z: float) -> dict:
    img = _scene_image(people, max(z - 0.08, 1.0))
    return {"sensor_id": TH, "seq": k, "t_mid_ns": k * DT_NS, "width": TH_W, "height": TH_H,
            "K": [TH_F, 0.0, TH_W / 2, 0.0, TH_F, TH_H / 2, 0.0, 0.0, 1.0], "dist": [0.0] * 5,
            "mount_nominal": {"t_bs_m": [0.12, 0.0, -0.08], "q_bs_xyzw": [0.0, 0.0, 0.0, 1.0]},
            "gimbal_reading": {"roll_deg": 0.0, "pitch_deg": -90.0, "yaw_deg": 0.0},
            "temp_ck": zstandard.ZstdCompressor(level=3).compress(img.astype("<u2").tobytes()), "codec": "radiometric_u16"}


def test_thermal_blob_detection_and_projection() -> None:
    from anet4d.baselines.thermal import ThermalPerception, blobs, classify, decode_temp_ck
    from jsonschema import Draft202012Validator

    from awr.agentsdk.schemas import BENCH_ID
    from awr.sim.schemas import contracts_registry

    f = _thermal_frame(3, [("A", (10.0, 5.0)), ("B", (-6.0, -12.0))], 60.0)
    v = Draft202012Validator({"$ref": BENCH_ID.format(name="obs") + "#/$defs/ThermalObs"}, registry=contracts_registry())
    assert not [e.message for e in v.iter_errors(f)]
    cfg = _config(["meet"], obs_level="L2", thermal=True)
    obs = {"k": 3, "t_ns": 3 * DT_NS, "nav": {"pos_enu_m": [0.0, 0.0, 60.0], "q_wb_xyzw": [0.0, 0.0, 0.0, 1.0]},
           "ground_hint": {"dtm_m": 0.0}, "sensors": {TH: [f]}}
    dets = ThermalPerception(cfg).detect(obs)
    assert [d["class"] for d in dets] == ["person", "person"]
    for d, want in zip(sorted(dets, key=lambda d: d["ground_xy_noisy"][0]), ((-6.0, -12.0), (10.0, 5.0)), strict=True):
        assert math.dist(d["ground_xy_noisy"], want) <= 0.5, (d, want)
        assert d["thermal"] and d["l2"] and d["sensor_id"] == TH and d["seq"] == 3 and abs(d["dT_k"] - 8.0) <= 0.5
    assert ThermalPerception(cfg).detect(obs) == dets                                     # 逐字节确定
    two = {**obs, "sensors": {TH: [_thermal_frame(2, [("C", (0.0, 0.0))], 60.0), f]}}
    assert ThermalPerception(cfg).detect(two) == dets                                     # 每步只处理最新一帧（墙钟预算）
    img = decode_temp_ck(f)
    warm = img.copy()
    warm[100:104, 100:104] = 28995                                                     # 高于局部中值约 1.8 K：低于 2 K 阈值
    assert len(blobs(warm)) == len(blobs(img))
    assert classify({"x0": 0, "y0": 0, "x1": 39, "y1": 15, "area": 600}, 60.0, TH_F, TH_F) == "vehicle"    # 7.0 × 2.8 m
    assert classify({"x0": 0, "y0": 0, "x1": 79, "y1": 3, "area": 320}, 60.0, TH_F, TH_F) is None         # 14 m 长条（墙沿）
    assert classify({"x0": 0, "y0": 0, "x1": 0, "y1": 0, "area": 1}, 60.0, TH_F, TH_F) is None           # 单像素噪点
    no = {**f, "codec": "agc_jpeg", "temp_ck": b"\xff\xd8\xff\xd9"}
    assert ThermalPerception(cfg).detect({**obs, "sensors": {TH: [no]}}) == []           # 预览编码不做辐射测量


def _night_scene(t: float):
    return [("P1", (5.0, 3.0)), ("P2", (5.0, 4.5))] if t <= 16.0 else []


@pytest.mark.parametrize("policy", POLICIES)
def test_thermal_channel_reports_meet_at_night(policy: str) -> None:
    class _Dark:                                          # 夜间相机：参考检测器无检出
        def detect(self, obs):
            return []

    cfg = _config(["meet"], obs_level="L2", thermal=True)

    def script(_t):
        return []

    from anet4d.baselines import BASELINES

    ag = BASELINES[policy]()
    ag.reset(copy.deepcopy(cfg))
    assert ag.thermal is not None and ag.perception is not None                      # L2 且配有热像仪：缺省启用热成像通道
    off = BASELINES[policy](thermal=False)
    off.reset(copy.deepcopy(cfg))
    assert off.thermal is None
    import anet4d.perception.ref_det as rd

    orig = rd.L2Perception
    rd.L2Perception = lambda config, det=None: _Dark()
    try:
        got = _reports(policy, cfg, script, 16.0, thermal_scene=_night_scene)
        none = _reports(policy, cfg, script, 16.0, thermal_scene=_night_scene, thermal=False)
    finally:
        rd.L2Perception = orig
    assert _summary(got) == [("meet", ("P1", "P2"))]
    assert none == []
    for p in got[0][2]["participants"]:
        assert all(e["sensor"] == TH and "bbox_xywh" in e for e in p["evidence"])    # 证据为热像仪帧框
