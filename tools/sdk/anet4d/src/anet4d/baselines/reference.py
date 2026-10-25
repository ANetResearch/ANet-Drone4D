"""6 种参考策略（r6 §12.3，M26-FR-081，D3-AC-18）：评测口径校核用的机队策略登记表。

r6 原型以"策略 × 机数"为一行（`hover1_plaza` 至 `track5_share`），本表把每一行登记为（策略类、机数、链路开关）与 r6 实测值：

| r6 行 | 策略类 | 机数 | 链路 |
|---|---|---|---|
| `hover1_plaza` | `hover_plaza` | 1 | 开（策略不发送） |
| `hover3_anchors` | `static_guard` | 3 | 开（策略不发送） |
| `lawn3_cover` | `coverage_scan` | 3 | 开（策略不发送） |
| `track3_nocomm` | `track_solo` | 3 | 关（M26-FR-069） |
| `track3_share` | `track_share` | 3 | 开 |
| `track5_share` | `track_share` | 5 | 开 |

r6 行名同时登记为 `anet4d.baselines.BASELINES` 的键与 run_spec `policy.name` 的取值，`policy = {kind: reference, name: <r6 行>}`
的运行由平台镜像以 `--baseline <r6 行>` 启动；`reference_run_spec` 由一份基础 run_spec 派生该行的规格（机队按行的机数截取或沿
起飞点间隔延长、链路开关），`check_reference_spec` 校验规格与登记一致。r6 值只作对照基准（EC 均值差 ≤ 0.1，由 M27 侧的对照
脚本判定），原型事件有 4 个关键事件（相遇、上车、进入建筑、车辆经过），`crop-handoff-001` 只有前 3 类，逐事件对照按事件类型取。

观测设置（ADR-217 决策 (1)）：参考行按 r6 §12.3 的观测设置运行——相机 HFOV 40°（`hfov_deg`，经 run_spec
`fleet[].sensors[eo].overrides.hfov_deg` 覆盖 L1 标称相机，只用于参考行与不排名运行），巡航高度 hover 与跟踪行 60 m、跟踪时 45 m、
覆盖扫描行 70 m，hover 行固定注视（`params`，策略类构造参数）。`BASELINES[<r6 行>]` 是绑定该行 `params` 的策略工厂，平台镜像以
`--baseline <r6 行>` 启动即按该设置运行；`reference_run_spec` 把相机覆盖写入机队、把 `params` 记入 `policy.params.baseline`
（进入运行哈希），`check_reference_spec` 核对两者与登记一致。
"""

from __future__ import annotations

import copy
import functools
from dataclasses import dataclass, field

__all__ = ["CAMERA_SENSOR_ID", "R6_HFOV_DEG", "REFERENCE6", "Reference", "check_reference_spec", "reference_run_spec"]

R6_HFOV_DEG = 40.0                  # r6 §12.3：1080p、HFOV 40° 相机（约 2 倍变焦）
CAMERA_SENSOR_ID = "eo"             # L1 标称相机的 sensor_id（Env `CameraModel.sensor_id`）


@dataclass(frozen=True)
class Reference:
    name: str                       # r6 行名（run_spec `policy.name`）
    policy: str                     # 策略类登记名（BASELINES 键）
    n_uav: int
    comm: bool                      # run_spec `comm.enabled`
    display: str
    r6: dict = field(default_factory=dict)   # r6 §12.3 实测（12 种子均值）：ec、ec_cov、ao_real、ao_pos、u_f1、gain、per_event
    params: dict = field(default_factory=dict)   # r6 观测设置：策略类构造参数（alt_agl_m、gaze、track_alt_agl_m）
    hfov_deg: float = R6_HFOV_DEG                 # r6 观测设置：相机水平视场（run_spec 相机覆盖）

    def factory(self):
        """绑定本行 `params` 的策略工厂（`BASELINES[<r6 行>]`）。"""
        from anet4d.baselines import POLICIES

        return functools.partial(POLICIES[self.policy], **self.params)


def _r6(ec, ec_cov, ao_real, ao_pos, u_f1, gain, meet, board, enter, vehicle) -> dict:
    return {"ec": ec, "ec_cov": ec_cov, "ao_real": ao_real, "ao_pos": ao_pos, "u_f1": u_f1, "gain": gain,
            "per_event": {"proximity_encounter": meet, "board_vehicle": board, "enter_building": enter, "vehicle_pass": vehicle}}


# r6 §12.3 观测设置（原型 `bench.py` 的 fleets：Hover alt 60 并注视锚点，Lawnmower alt 70，Tracker 岗位 alt 60、跟踪 alt 45）
HOVER = {"alt_agl_m": 60.0, "gaze": "fixed"}
COVER = {"alt_agl_m": 70.0}
TRACK = {"alt_agl_m": 60.0, "track_alt_agl_m": 45.0}

REFERENCE6: dict[str, Reference] = {r.name: r for r in (
    Reference("hover1_plaza", "hover_plaza", 1, True, "Hover at plaza (1 UAV)", _r6(0.50, 0.31, 0.63, 0.80, 0.50, 0.0, 1.00, 0.00, 0.00, 1.00),
              HOVER),
    Reference("hover3_anchors", "static_guard", 3, True, "Guard anchors (3 UAVs)", _r6(0.75, 0.54, 0.83, 1.00, 0.80, 0.25, 1.00, 0.00, 1.00, 1.00),
              HOVER),
    Reference("lawn3_cover", "coverage_scan", 3, True, "Coverage scan (3 UAVs)", _r6(0.25, 0.04, 0.00, 0.92, 0.00, 0.0, 0.00, 0.00, 0.00, 1.00),
              COVER),
    Reference("track3_nocomm", "track_solo", 3, False, "Track without links (3 UAVs)",
              _r6(0.90, 0.79, 0.95, 0.97, 0.53, 0.0, 1.00, 0.58, 1.00, 1.00), TRACK),
    Reference("track3_share", "track_share", 3, True, "Track and share (3 UAVs)", _r6(0.88, 0.78, 0.88, 0.95, 0.57, 0.02, 1.00, 1.00, 0.50, 1.00),
              TRACK),
    Reference("track5_share", "track_share", 5, True, "Track and share (5 UAVs)", _r6(1.00, 0.87, 1.00, 1.00, 0.65, 0.06, 1.00, 1.00, 1.00, 1.00),
              TRACK),
)}

PAD_STEP_M = (0.0, -4.0, 0.0)       # 基础机队只有 1 架时延长起飞点的缺省间隔


def _pads(fleet: list[dict], n: int) -> list[list[float]]:
    """前 n 个起飞点：截取基础机队，不足时沿末两点间隔（只有 1 架时取 PAD_STEP_M）延长。"""
    pos = [[float(x) for x in f["start"]["pos_enu_m"]] for f in fleet]
    step = [b - a for a, b in zip(pos[-2], pos[-1], strict=True)] if len(pos) >= 2 else list(PAD_STEP_M)
    while len(pos) < n:
        pos.append([p + s for p, s in zip(pos[-1], step, strict=True)])
    return pos[:n]


def _with_camera(f: dict, hfov_deg: float) -> dict:
    """机队项的传感器列表加入（或改写）L1 相机覆盖 `{sensor_id: eo, overrides: {hfov_deg}}`，其余传感器项不变。"""
    sensors = [copy.deepcopy(x) for x in f.get("sensors", [])]
    cam = next((x for x in sensors if x.get("sensor_id") == CAMERA_SENSOR_ID), None)
    if cam is None:
        cam = {"sensor_id": CAMERA_SENSOR_ID}
        sensors.append(cam)
    cam["overrides"] = {**(cam.get("overrides") or {}), "hfov_deg": float(hfov_deg)}
    return {**f, "sensors": sensors}


def reference_run_spec(base: dict, name: str, pads: list[list[float]] | None = None) -> dict:
    """由基础 run_spec 派生参考行 `name` 的规格：`policy = {kind: reference, name, params}`（保留基础规格的 `params`，并以
    `params.baseline` 记录该行的 r6 观测设置），机队取行的机数（沿用基础机队的前若干项，新增项复制首项的 `vehicle_ref` 与传感器、
    `uav_id` 依次编号），各机加入 r6 相机覆盖（HFOV 40°），`comm.enabled` 取登记值。`pads` 给出时逐项替换起飞点（新增起飞点的
    地面平整与离演员路径距离由调用方核对）。不修改 `base`。"""
    ref = REFERENCE6[name]
    rs = copy.deepcopy(base)
    fleet = rs["fleet"]
    if pads is not None and len(pads) != ref.n_uav:
        raise ValueError(f"{name}: {ref.n_uav} pads required, got {len(pads)}")
    pos = [list(map(float, p)) for p in pads] if pads is not None else _pads(fleet, ref.n_uav)
    taken = {f["uav_id"] for f in fleet[:ref.n_uav]}
    out, k = [], 0
    for i in range(ref.n_uav):
        if i < len(fleet):
            f = fleet[i]
        else:
            f = copy.deepcopy(fleet[0])
            while True:
                k += 1
                uid = f"uav-{k:02d}"
                if uid not in taken:
                    break
            f["uav_id"] = uid
            taken.add(uid)
        f["start"] = {**f["start"], "pos_enu_m": pos[i]}
        out.append(_with_camera(f, ref.hfov_deg))
    rs["fleet"] = out
    params = copy.deepcopy((base.get("policy") or {}).get("params") or {})
    params["baseline"] = copy.deepcopy(ref.params)
    rs["policy"] = {"kind": "reference", "name": name, "params": params}
    rs["comm"] = {**(rs.get("comm") or {}), "enabled": ref.comm}
    return rs


def check_reference_spec(rs: dict) -> list[str]:
    """参考行规格与登记是否一致：→ 问题列表（空为一致）。`policy.name` 不是 r6 行名时不检查（基础策略名可用于任意机数）。"""
    pol = rs.get("policy") or {}
    ref = REFERENCE6.get(str(pol.get("name")))
    if ref is None:
        return []
    probs = []
    if pol.get("kind") != "reference":
        probs.append(f"policy.kind must be reference for {ref.name}")
    if len(rs.get("fleet", [])) != ref.n_uav:
        probs.append(f"{ref.name} runs {ref.n_uav} UAVs, fleet has {len(rs.get('fleet', []))}")
    if bool((rs.get("comm") or {}).get("enabled", True)) != ref.comm:
        probs.append(f"{ref.name} requires comm.enabled = {str(ref.comm).lower()}")
    if ((pol.get("params") or {}).get("baseline") or {}) != ref.params:
        probs.append(f"{ref.name} requires policy.params.baseline = {ref.params} (r6 observation setting)")
    for f in rs.get("fleet", []):
        cam = next((x for x in f.get("sensors", []) if x.get("sensor_id") == CAMERA_SENSOR_ID), {})
        if (cam.get("overrides") or {}).get("hfov_deg") != ref.hfov_deg:
            probs.append(f"{ref.name}: {f.get('uav_id')} requires camera override hfov_deg = {ref.hfov_deg:g}")
    return probs
