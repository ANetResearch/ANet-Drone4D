"""排行榜 v1 第二个内部示例提交 `relay_scan`（tools/bench/d3/examples/relay_scan；D3-AC-43）：航带扫描与单机接力跟踪。

构造观测（L1 检测按脚本逐帧给出，agent_config 通过 `bench/agent_config.v1` 校验，动作经 `ActionGuard` 核对）：
1. 航带：间距按相机足迹 70% 重叠，各机交错分配、互不重叠且合起来覆盖全部航带，航点在搜索区外接框内；有先验时按先验质量排序；
2. 无目标时沿航带扫描（goto 依次指向本机航点，到位后切换），与 `coverage_scan` 的连续条带分区不同；
3. 本机看到简报目标：转为后方 50 m 跟踪站位并每秒广播跟踪声明；他机 3 s 内声明且更近时不抢，声明过期后接力；
4. 无人声明而已知更近的空闲他机时由他机接手，他机位置过期（≥ 3 s）后本机接手；
5. 全部动作通过 `bench/act.v1`、守卫回执无拒绝；同一输入两次运行动作流逐字节一致；镜像入口 `relay_scan.agent:Agent` 可加载。
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import itertools
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _p in (ROOT / "tools" / "sdk" / "drone4d" / "src", ROOT / "tools" / "bench" / "d3" / "examples" / "relay_scan"):
    sys.path.insert(0, str(_p))

from awr.agentsdk.env.camera import CameraModel  # noqa: E402
from awr.agentsdk.env.guard import ActionGuard, Limits  # noqa: E402
from awr.agentsdk.schemas import bench_validator  # noqa: E402

DT_NS = 100_000_000
CAM = "eo"
AREA = [[-150.0, -100.0], [150.0, -100.0], [150.0, 100.0], [-150.0, 100.0]]
TARGET = {"label": "target_1", "class": "person", "appearance": {"top_rgb": [200, 30, 30], "bottom_rgb": [20, 20, 20]}}
APP_T = {"top_rgb": [200, 30, 30], "bottom_rgb": [20, 20, 20]}
VOCAB = ["enter_building", "meet"]
FLEET = ["uav-01", "uav-02", "uav-03"]


def _config(uav: str = "uav-01", *, search: dict | None = None) -> dict:
    cfg = {"schema": "drone4d.bench.agent_config.v1", "uav_id": uav, "kind": "uav", "peers": [u for u in FLEET if u != uav],
           "vehicle": {"ref": "x500", "limits": Limits().config(), "endurance_s": 1500.0}, "sensors": [CameraModel(sensor_id=CAM).config()],
           "start": {"kind": "pose", "state": "landed", "nav_pos_enu_m": [0.0, 0.0, 0.0]}, "obs_level": "L1", "control_hz": 10,
           "decision": {"mode": "fixed", "tau_ms": 0, "tau_min_ms": 0, "step_budget_s": 2.0, "wall_ratio": 1.0},
           "comm": {"enabled": True, "bucket_Bps": 125000, "burst_B": 125000, "msg_max_B": 4096, "model": "ideal"},
           "episode": {"duration_s": 120.0}, "world": {"id": "synth", "content_version": "1", "origin": {"lat_deg": 0.0, "lon_deg": 0.0, "alt_m": 0.0}},
           "brief": {"task": {"type": "event_capture", "description": "constructed"}, "area": {"polygon_enu_m": AREA, "alt_agl_range_m": [30.0, 120.0]},
                     "keepouts": [], "anchors": [], "targets": [TARGET], "report_vocab": VOCAB, "maps": {"dsm": "point"}, "nominal_noise": {}},
           "protocol": "drone4d.bench.wire.v1"}
    errs = [e.message for e in bench_validator("agent_config").iter_errors(cfg)]
    assert not errs, errs
    if search is not None:                                # brief.search 尚未进入 agent_config.v1（Env 投影待接线），校验后补入
        cfg["brief"]["search"] = search
    return cfg


def _agent(cfg: dict):
    from relay_scan.agent import Agent

    ag = Agent()
    ag.reset(copy.deepcopy(cfg))
    return ag


def _msg(src: str, obj: dict) -> dict:
    return {"src": src, "topic": obj["kind"], "payload": json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()}


def _det(xy, k: int) -> dict:
    return {"det_id": hashlib.sha256(f"T|{k}".encode()).hexdigest()[:16], "sensor_id": CAM, "seq": k, "t_mid_ns": k * DT_NS, "class": "person",
            "bbox_xywh": [300.0, 300.0, 18.0, 40.0], "h_px": 40.0, "slant_range_m": 80.0, "ground_xy_noisy": [float(xy[0]), float(xy[1])],
            "appearance": dict(APP_T)}


def _run(cfg: dict, seconds: float, *, nav=lambda t: (0.0, 0.0), target=lambda t: None, inbox=lambda t: []):
    """逐步构造观测（机位置按 `nav(t)` 给出）→ [(动作, 守卫回执)]、智能体。"""
    ag = _agent(cfg)
    guard = ActionGuard(cfg["uav_id"], cfg["peers"], Limits(), sorted(VOCAB), True, 4096, CameraModel(sensor_id=CAM))
    out = []
    for k in range(int(seconds * 10) + 1):
        t = k * DT_NS / 1e9
        xy = nav(t)
        z = 0.0 if k == 0 else ag.alt
        txy = target(t)
        dets = [_det(txy, k)] if txy is not None else []
        guard.saw(dets)
        obs = {"schema": "drone4d.bench.obs.v1", "k": k, "t_ns": k * DT_NS, "nav": {"pos_enu_m": [xy[0], xy[1], z], "q_wb_xyzw": [0.0, 0.0, 0.0, 1.0]},
               "status": {"mode": "landed" if k == 0 else "airborne"}, "ground_hint": {"dsm_m": 0.0, "dtm_m": 0.0}, "sensors": {"detections": dets},
               "acks": []}
        act = ag.step(obs, inbox(t))
        r = guard.check(act, k, np.array([xy[0], xy[1], z]), 0.0, np.zeros(3), 0.0)
        out.append((act, r.acks))
    return out, ag


def _goal(act: dict) -> tuple[float, float] | None:
    nav = act.get("nav") or {}
    return (nav["pos_enu_m"][0], nav["pos_enu_m"][1]) if nav.get("mode") == "goto" else None


def _canon(o) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=lambda b: b.hex()).encode()


def _topics(act: dict) -> list[str]:
    return [m["topic"] for m in act.get("msgs", [])]


def test_scan_lanes_interleave_and_cover_the_area() -> None:
    from relay_scan.agent import scan_waypoints

    wps = {u: _agent(_config(u)).wps for u in FLEET}
    lanes = {u: sorted({round(x, 6) for x, _ in w}) for u, w in wps.items()}
    union = sorted(x for v in lanes.values() for x in v)
    assert len(union) == len(set(union)) >= 6                                       # 互不重叠
    sp = union[1] - union[0]
    cam = CameraModel(sensor_id=CAM).config()
    foot = 2 * 75.0 * math.tan(math.atan((cam["width"] / 2) / cam["K"][0]))
    assert sp == pytest.approx(max(10.0, 0.3 * foot))                               # 70% 重叠（名义高度 = 包络中值 75 m）
    assert all(b - a == pytest.approx(sp) for a, b in itertools.pairwise(union))   # 合起来铺满外接框
    assert union[0] == pytest.approx(-150.0 + sp / 2) and union[-1] < 150.0
    assert [lanes["uav-01"][0], lanes["uav-02"][0], lanes["uav-03"][0]] == union[:3]   # 交错分配：第 i 架机取第 i、i + n、… 条
    for w in wps.values():
        assert all(-150.0 <= x <= 150.0 and y in (-100.0, 100.0) for x, y in w)
        for a, b in zip(w[1::2], w[2::2], strict=False):                            # 下一条航带从靠近上一条终点的一端进入
            assert a[1] == b[1]
    prior = {"target_class": "person", "area_polygon": AREA, "window_s": 60.0, "motion": "static",
             "prior": {"type": "gmm", "components": [{"xy": [120.0, 0.0], "sigma_m": 15.0, "weight": 1.0}]}}
    ag_p = _agent(_config("uav-01", search=prior))
    assert ag_p.prior is not None
    plain = scan_waypoints(_config(), 75.0, [tuple(p) for p in AREA], None, "uav-01", FLEET[1:])
    assert abs(ag_p.wps[0][0] - 120.0) < sp < abs(plain[0][0] - 120.0)              # 先验质量最高的航带最先飞


def test_scan_follows_lanes_without_targets_and_differs_from_coverage_scan() -> None:
    from drone4d.baselines import CoverageScan

    cfg = _config("uav-02")
    ag = _agent(cfg)
    wps = ag.wps

    def nav(t: float) -> tuple[float, float]:                                       # 5 s 起每 5 s 到达下一个航点
        return wps[min(int(t // 5.0) - 1, len(wps) - 1)] if t >= 5.0 else (0.0, 0.0)

    out, ag = _run(cfg, 22.0, nav=nav)
    goals = [g for g in (_goal(a) for a, _ in out) if g is not None]
    assert goals and goals[0] == pytest.approx(wps[0]) and wps[1] in goals and wps[2] in goals
    cov = CoverageScan()
    cov.reset(copy.deepcopy(cfg))
    assert cov.wps[0] != wps[0]                                                      # 连续条带分区 ≠ 交错航带
    assert any("state" in _topics(a) for a, _ in out)                                # 每秒广播位置与忙闲
    st = next(json.loads(bytes(m["payload"])) for a, _ in out for m in a.get("msgs", []) if m["topic"] == "state")
    assert st["busy"] is False


def test_tracks_target_it_sees_and_yields_to_a_closer_claim() -> None:
    cfg = _config("uav-01")
    tgt = (40.0, 30.0)
    out, ag = _run(cfg, 20.0, nav=lambda t: (0.0, 0.0), target=lambda t: tgt if t >= 6.0 else None)
    g = _goal(out[-1][0])
    assert ag.track == 0 and math.dist(g, tgt) == pytest.approx(50.0, abs=0.5)        # 后方 50 m 站位
    assert any("claim" in _topics(a) for a, _ in out[60:])

    def claimed(t: float) -> list[dict]:                                             # uav-02 更近（距目标 5 m）且持续声明
        if t < 6.0 or int(t * 10) % 10:
            return []
        return [_msg("uav-02", {"kind": "claim", "t": int(t * 1e9), "target": 0, "dist": 5.0}),
                _msg("uav-02", {"kind": "sight", "targets": [{"target": 0, "t": int(t * 1e9), "xy": list(tgt), "vel": [0.0, 0.0]}]})]

    out2, ag2 = _run(cfg, 20.0, target=lambda t: tgt if t >= 6.0 else None, inbox=claimed)
    assert ag2.track is None and _goal(out2[-1][0]) in ag2.wps                       # 不抢，继续扫描
    assert not any("claim" in _topics(a) for a, _ in out2)

    def stops(t: float) -> list[dict]:                                               # 声明 10 s 后停止：3 s 后本机接力
        return claimed(t) if t < 10.0 else []

    out3, ag3 = _run(cfg, 20.0, target=lambda t: tgt if t >= 6.0 else None, inbox=stops)
    took = [i for i, (a, _) in enumerate(out3) if "claim" in _topics(a)]
    assert ag3.track == 0 and took and 120 <= took[0] <= 130                          # 末次声明 9.0 s + 3 s


def test_unclaimed_shared_sight_goes_to_the_nearest_free_uav() -> None:
    cfg = _config("uav-03")
    tgt = (100.0, 0.0)

    def inbox(t: float) -> list[dict]:
        if int(t * 10) % 10:
            return []
        msgs = [_msg("uav-01", {"kind": "sight", "targets": [{"target": 0, "t": int(t * 1e9), "xy": list(tgt), "vel": [0.0, 0.0]}]})]
        if t < 8.0:                                                                   # uav-02 空闲且离目标 10 m，8 s 后不再广播
            msgs.append(_msg("uav-02", {"kind": "state", "t": int(t * 1e9), "xy": [90.0, 0.0], "busy": False}))
        return msgs

    out, ag = _run(cfg, 15.0, inbox=inbox)
    first = [i for i, (a, _) in enumerate(out) if "claim" in _topics(a)]
    assert ag.track == 0 and first and 100 <= first[0] <= 120                        # 他机位置过期（≥ 3 s）后本机接手
    assert all(_goal(a) is None or _goal(a) in ag.wps for a, _ in out[:100])


def test_actions_valid_and_deterministic() -> None:
    cfg = _config("uav-01")
    kw = {"nav": lambda t: (0.0, 0.0), "target": lambda t: (30.0 + t, 20.0) if 5.0 <= t <= 25.0 else None}
    run1, _ = _run(cfg, 40.0, **kw)
    run2, _ = _run(cfg, 40.0, **kw)
    assert b"\n".join(_canon(a) for a, _ in run1) == b"\n".join(_canon(a) for a, _ in run2)
    val = bench_validator("act")
    for act, acks in run1:
        assert not [e.message for e in val.iter_errors(act)], act
        assert not [x for x in acks if x.get("result") == "rejected"], acks


def test_container_entry_and_image_files() -> None:
    d = ROOT / "tools" / "bench" / "d3" / "examples" / "relay_scan"
    df = (d / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "drone4d.serve", "relay_scan.agent:Agent"]' in df and "COPY relay_scan/ /opt/agent/relay_scan/" in df
    mod, _, attr = "relay_scan.agent:Agent".partition(":")
    assert getattr(importlib.import_module(mod), attr).name == "relay_scan"
