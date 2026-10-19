"""车道图上的车辆驾驶行为（M23 §15.4，M23-FR-056、FR-075；AWR-05 §5.2.8 `vehicle_track`；ADR-132 v1.3、ADR-181；D3-AC-33、
M23-AC-031、M23-AC-040）：IDM 跟驰、弯道限速、路口让行与路口单元互斥、`stop` 与 `park`；以及只读磁带量（位置、状态、标志）
复算的验证（道路走廊、车距、无穿越、路口让行）与关键事件（`depart`、`turn{intersection}`、`stop`、`park`）。

输入为 M22 车道图（`geojson/awr-navgraph@1`，`LaneGraph`）。E09 把 DSL `actors[].drive` 与路线接入门 2 与磁带生成
（`gen.py`），把本模块的验证与关键事件登记为门 3 的 `vehicle_track` 验证器；本模块不读 DSL，也不写磁带。

运动模型（`simulate`）：
1. 路线：`LaneRoute`（车道与连接段交替，`lane_route` 或 `chain_route` 由锚点链展开）。车辆的纵向坐标 u 为路线弧长（u = 0 为
   首段的 `s_start`），车头位置由所在段折线按弧长插值，航向为该段切向；车身长 L（缺省 4.5 m，DSL 类别尺寸）沿路线向后占据
   [u − L, u]。首段起点距段首不足 L 时，出生点前移到 u = L − s_start（报告 `spawn_shift_m`），保证车身全在路线上。
2. 跟驰：IDM，`a = a_max · [1 − (v/v0)^4 − (s*/s)^2]`，`s* = s0 + max(0, v·T + v·Δv / (2√(a_max·b)))`；缺省 T = 1.5 s、
   s0 = 2 m、a = 1.5 m/s²、b = 2.0 m/s²；v0 为所在段限速（车道 `speed_mps`：OSM `maxspeed` 或道路等级缺省，居住区 8.3、次干道
   11.1、主干道 13.9 m/s）乘驾驶风格系数（urban 1.0、cautious 0.9、aggressive 1.1），可由 `idm.v0_mps` 再设上限；
   s0 不得小于 `MIN_GAP_M` = 2 m（D3-AC-33 最小车距）。前车为沿本车路线前方 lookahead 内最近的他车车身（含同一来源车道的
   分叉连接段在分离弧长内的车身，见第 4 条）；数值积分为 100 ms 弹道格式（Treiber–Kesting），减速度下限 −9 m/s²；静止车辆
   一步内的速度增量 < 0.05 m/s 时保持静止，减速中速度 < 0.05 m/s 即停稳（避免 IDM 渐近蠕行）。
3. 弯道与限速：连接段限速取 min(车道限速 × 系数, √(a_lat·R_min))，a_lat = 2.0 m/s²、R_min 为车道图 `radius_min_m`；前方
   低限速段按 v ≤ √(v_lim² + 2·b·d) 提前减速（d 扣除一步行程），速度上限是硬约束。
4. 路口单元与互斥：每条连接段 C（来源车道 A → 目标车道 D，路口节点 n）的路口单元 = A 的末段 e_in + C + D 的首段 e_out。
   e_in 为 A 末端中心线距其他路径（来源车道不是 A 的任一路口的连接段、A 所在边以外的 n 处车道，含 D）< `PROX_M` = 2.5 m 的
   长度（锐角路口车道端部重叠时 > 0，至多 min(30 m, len(A) − L − s0 − 1)），并保证等候点到 D 首端的路线距离 ≥ 2.5 m；
   e_out 同理（D 首段距汇入 D 与自 D 驶出以外的连接段、D 所在边以外的 n 处车道，至多 len(D)/2）。冲突 = 车道图 `conflicts`（汇入同一
   车道或几何相交）并上单元中心线采样点距离 < `PROX_M` 的单元对，同一来源车道的单元除外（分叉由跟驰处理：前车在兄弟连接段
   上、位置小于两者中心线分离到 `PROX_M` 的弧长时视为本车前车）；有冲突的单元需要准入。车辆在车头距单元起点
   d ≤ v²/(2b) + v·Δt + 2 m 且为本车道队首（连接段入口前无他车车身）时提出准入，首次提出的帧为到达时刻，此后每帧重提；
   准入（持有）条件依次为：(a) 冲突单元无他车持有；(b) 先到先行：同一帧内按 (等级降序, 到达时刻, 车辆 idx) 处理，较早处理而
   未获准入的冲突请求阻挡其后者（FIFO）；(c) 次路让主路：等级低于冲突单元等级时，驶向该冲突单元的他车到达时间
   d/max(v, 1 m/s) 均须 > `GAP_S` = 4 s，且他车到达其准入提出点的时间 (d − d_req)/max(v, 1 m/s) > 本车以 0.8·a 加速驶出单元的
   时间 + 1 s（前车持有同一单元时驶出时间不少于前车驶出时间 + T），使高级车辆提出准入时次路车已驶出；(d) 不堵塞路口：D 上单元终点以外的空余长度（占用车辆按速度外推 2 s）
   ≥ (同一单元前方持有车数 + 1)·(L + s0) + 1 m。等级：车道图 `priority` major = 2、equal 与 none = 1、minor = 0（来源车道停止线
   规则 `give_way` 计为 0）；停止线规则为 `stop_sign` 时须先在单元起点前 0.5 m 内停稳才能提出准入。未获准入的车辆以单元
   起点为虚拟静止障碍减速（IDM 间距取 d + s0，使车头停在该处；开始减速后保持到准入），且车头不越过单元起点（硬约束）；
   持有到车尾驶出单元终点（或泊车、在终点停住）为止。
5. 车距硬约束：每步更新后车头不越过 (前车上一帧车尾 − 2 m)；由于各车 u 单调不减，沿路线车距 ≥ 2 m（触发次数 `gap_clamps`）。
6. 几何安全层：每帧更新按 (未持有单元, idx) 次序逐车核对新车身矩形（外扩 5 cm）与已更新车辆的新矩形、未更新车辆的当前矩形
   不相交，相交则本步原地停住（`safety_stops`；正常路网为 0，非零表示车道图存在未建模的几何重叠）。出生同样要求不相交。
   由此任一时刻车身两两不相交（D3-AC-33 无穿越）。
7. `stop`（`stops[]`，路线坐标 u 与停留 `dwell_ms`）：以停车点为虚拟静止障碍，停稳（v = 0 且距停车点 ≤ 0.5 m）后停留
   `dwell_ms` 再出发；`end = park` 时在路线终点停稳即泊车，发动机标志立即清零；`end = despawn` 时车头到达终点即消失（状态 5）；
   `end = wait` 时停在终点。
8. 出生：到达 `depart_ms` 后，出生点前方（沿路线）`SPAWN_CLEAR_M` 内无他车车身、后方每辆车与本车车尾距离 ≥ 8 m + v·T +
   v²/(2b)、车身矩形与他车不相交时才出现（`v_init` 缺省 0），否则顺延（`spawns[].delay_ms`）。
9. 磁带量：状态 0 未出现、1 行驶（v > 0）、2 静止（含泊车）、5 消失；`flags` bit0 `engine_on`（M23 §15.5：行驶中或停车后
   120 s 内为 1，`park` 后为 0，未出现与消失为 0）。

确定性：整数毫秒步进（t_k = 100·k ms）；加速度以上一帧快照计算，路口准入按 (等级, 到达帧, idx) 排序，安全层按
(未持有单元, idx) 次序；运动学为 Python float64 标量的显式次序运算（不用 FMA、数组归约或三角函数），位置求值为逐元素四则运算。
背景车流的出发时刻、起点与终点取流 19 的 `vehicle` 通道（24–27），键 `(seed, 19, idx, g, ch)`，g 为重抽次数。
`VehicleSim.digest()` 为毫米定点位置与高度、状态、标志与决策日志的 sha256，由 E09 并入事件 `content_sha256`。

验证（`audit`，只读磁带量与车道图；路口规则部分另读生成日志的准入时刻与到达时刻）：
- 路线匹配：磁带位置沿路线折线单调投影，横向偏差 ≤ 0.02 m（车辆沿车道图运动）；
- 道路走廊：全部可见位置在走廊内（车辆道路中心线按 max(width_m/2, 5 m) 缓冲，与 M22 §6.6.11、`ROAD_CORRIDOR` 同一口径），
  路线经过的段全部 `in_corridor`；另报车行道口径（width_m/2 + 0.5 m）；
- 车距：沿路线（含分叉兄弟）的车头到前车车尾距离最小值 ≥ 2 m（毫米定点容差 5 mm）；
- 无穿越：每帧车身矩形（车头与车尾连线为纵轴，L × W）两两不相交（分离轴判定）；
- 路口：冲突单元在同一帧不被两车占用（占用 = 车身与单元有交）；每次驶入需要准入的单元之前有准入记录；准入时刻冲突单元
  无他车占用、无较早到达而未获准入的同级或高级冲突请求、次路准入时无高级车辆到达时间 ≤ 4 s − 0.3 s；另有不依赖日志的判据：
  次路车驶入时没有高级车辆停在冲突单元起点前等待（`major_blocked`）；
- 限速：连接段上的磁带速度 ≤ 弯道限速 + 0.2 m/s。

关键事件（`key_events`，只读磁带量与车道图）：
- `depart`：出现后首次开始移动的帧（瞬时）；
- `turn{intersection}`：驶过路口（车道图 `j-<node>`）的 left、right、uturn 连接段，区间为车头驶入到驶出，参数含路口、转向、
  连接段、来源与目标车道、航向变化（0.1°）；
- `stop`：静止（状态 2 且到下一帧位置不变）持续 ≥ 3 s 的区间（泊车除外），参数 `cause` 为 junction（停在需要准入的单元起点前
  ≤ 3 m）、queue（与前车车尾距离 ≤ s0 + 1.5 m）或 dwell；
- `park`：静止区间开始后 200 ms 内 `engine_on` 清零、此后保持为 0 且静止到可见结束（瞬时，取区间起点）。
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.sim.events.jcs import jcs
from awr.sim.sensors import cbrng
from awr.world.glb.derive.lanes import CARRIAGEWAY_TOL_M, Corridor, LaneGraph, LaneRoute
from awr.world.glb.derive.navmesh import CORRIDOR_HALF_M

__all__ = [
    "AUDIT_DEFAULTS",
    "A_LAT",
    "EVENT_DEFAULTS",
    "GAP_S",
    "IDM_DEFAULTS",
    "MIN_GAP_M",
    "PROFILES",
    "STREAM",
    "VEH_SIZE_M",
    "DriveParams",
    "Network",
    "VehicleSim",
    "VehicleSpec",
    "audit",
    "background",
    "chain_route",
    "drive_params",
    "idm_accel",
    "key_events",
    "match_route",
    "simulate",
]

TICK_MS = 100
DT = TICK_MS / 1000.0
STREAM = 19
CH_VEHICLE = (24, 27)                          # 出发时刻、起点车道、起点位置、终点车道
cbrng.CHANNELS.setdefault((STREAM, "vehicle"), CH_VEHICLE)

VEH_SIZE_M = (4.5, 1.8, 1.5)                   # DSL 类别 vehicle 的 l × w × h（M23 §6.2.3）
MIN_GAP_M = 2.0                                # D3-AC-33：最小车距
IDM_DEFAULTS = {"T_s": 1.5, "s0_m": 2.0, "a_mps2": 1.5, "b_mps2": 2.0}       # M23 §15.4
PROFILES = {"urban": {"v0_scale": 1.0, "T_s": 1.5, "a_mps2": 1.5, "b_mps2": 2.0},
            "cautious": {"v0_scale": 0.9, "T_s": 2.0, "a_mps2": 1.2, "b_mps2": 1.5},
            "aggressive": {"v0_scale": 1.1, "T_s": 1.1, "a_mps2": 2.0, "b_mps2": 2.5}}
A_LAT = 2.0                                    # 弯道限速的横向加速度（m/s²）
B_MAX = 9.0                                    # 减速度下限（m/s²）
GAP_S = 4.0                                    # 次路准入的临界间隙（高级车辆到达时间）下限
CLEAR_ACC_FRAC = 0.8                           # 驶出单元时间按 0.8·a 估计
CLEAR_MARGIN_S = 1.0                           # 临界间隙 = max(GAP_S, 驶出单元时间 + 1 s)
V_ETA_MIN = 1.0                                # 到达时间的速度下限
COMMIT_MARGIN_M = 2.0
STOP_TOL_M = 0.5                               # 停稳判据：距停车点 ≤ 0.5 m
V_SNAP = 0.05                                  # 减速中速度低于此值即停稳
V_START = 0.05                                 # 静止车辆一步内的速度增量不足此值时保持静止
SPACE_MARGIN_M = 1.0
SPACE_LOOK_S = 2.0                             # 目标车道空余长度按占用车辆速度外推 2 s
SPAWN_CLEAR_M = 8.0
SPAWN_BACK_M = 120.0
PROX_M = 2.5                                   # 近距冲突、单元延伸与分叉分离的中心线距离
PROX_STEP_M = 0.5
EXT_CAP_M = 30.0                               # 单元向车道延伸的上限
WAIT_CLEAR_M = MIN_GAP_M + 0.5                 # 等候点到目标车道首端的最小路线距离
SAFETY_MARGIN_M = 0.05                         # 安全层车身矩形外扩
GRID_M = 30.0                                  # 车身矩形空间索引格
COS_SAME = 0.8660254037844387                  # cos 30°：几何前车检查跳过航向相近的沿路线前车
LOOKAHEAD_MIN_M = 40.0
ETA_SCAN_M = 120.0
ENGINE_IDLE_MS = 120_000                       # M23 §15.5：停车后 120 s 内 engine_on = 1
CLASS = {"major": 2, "equal": 1, "none": 1, "minor": 0}
STATE_ABSENT, STATE_MOVING, STATE_WAITING, STATE_GONE = 0, 1, 2, 5
FLAG_ENGINE = 1
_ABSENT, _ACTIVE, _PARKED, _GONE = 0, 1, 2, 3

# 验证与关键事件参数（由 E09 写入 `validator_params`）
AUDIT_DEFAULTS = {"on_lane_tol_m": 0.02, "gap_tol_m": 0.005, "gap_scan_m": 30.0, "eta_tol_s": 0.3, "speed_tol_mps": 0.2,
                  "wait_m": 3.0, "v_wait_mps": 0.05}
EVENT_DEFAULTS = {"stop_min_ms": 3000, "park_flag_ms": 200, "junction_m": 3.0, "queue_m": IDM_DEFAULTS["s0_m"] + 1.5,
                  "turns": ["left", "right", "uturn"]}


# ---------------------------------------------------------------- 参数


@dataclass(frozen=True)
class DriveParams:
    """IDM 参数与驾驶风格（`actors[].drive`）。"""

    profile: str = "urban"
    v0_scale: float = 1.0
    v0_mps: float | None = None
    T_s: float = 1.5
    s0_m: float = 2.0
    a_mps2: float = 1.5
    b_mps2: float = 2.0

    def as_dict(self) -> dict:
        return {"profile": self.profile, "v0_scale": self.v0_scale, "v0_mps": self.v0_mps, "T_s": self.T_s, "s0_m": self.s0_m,
                "a_mps2": self.a_mps2, "b_mps2": self.b_mps2}


def drive_params(drive: Mapping[str, Any] | None = None) -> DriveParams:
    """DSL `drive{profile, idm{v0_mps?, T_s?, s0_m?, a_mps2?, b_mps2?}}` → 参数（风格缺省值见 `PROFILES`，`idm` 逐项覆盖）。
    s0 < 2 m、T ≤ 0、a ≤ 0、b ≤ 0、v0 ≤ 0、未知风格或未知参数抛 ValueError（门 1 由 E09 转为 611）。"""
    d = dict(drive or {})
    prof = str(d.get("profile", "urban"))
    if prof not in PROFILES:
        raise ValueError(f"unknown drive profile {prof!r}")
    p = {**PROFILES[prof], "s0_m": IDM_DEFAULTS["s0_m"]}
    idm = dict(d.get("idm") or {})
    unknown = sorted(set(idm) - {"v0_mps", "T_s", "s0_m", "a_mps2", "b_mps2"})
    if unknown:
        raise ValueError(f"unknown idm parameters {unknown}")
    for k in ("T_s", "s0_m", "a_mps2", "b_mps2"):
        if idm.get(k) is not None:
            p[k] = float(idm[k])
    v0 = None if idm.get("v0_mps") is None else float(idm["v0_mps"])
    if p["s0_m"] < MIN_GAP_M:
        raise ValueError(f"idm.s0_m {p['s0_m']} is below the minimum gap {MIN_GAP_M} m (D3-AC-33)")
    if min(p["T_s"], p["a_mps2"], p["b_mps2"]) <= 0 or (v0 is not None and v0 <= 0):
        raise ValueError("idm parameters must be positive")
    return DriveParams(prof, p["v0_scale"], v0, p["T_s"], p["s0_m"], p["a_mps2"], p["b_mps2"])


def idm_accel(v: float, v0: float, s: float | None, dv: float, p: DriveParams) -> float:
    """IDM 加速度（M23 §15.4）；`s` 为 None 时为自由路段。显式运算次序，不用幂函数；下限 −`B_MAX`。"""
    r = v / v0
    r2 = r * r
    free = 1.0 - r2 * r2
    if s is None:
        return max(-B_MAX, p.a_mps2 * free)
    if s <= 0:
        return -B_MAX
    ss = p.s0_m + max(0.0, v * p.T_s + v * dv / (2.0 * math.sqrt(p.a_mps2 * p.b_mps2)))
    q = ss / s
    return max(-B_MAX, p.a_mps2 * (free - q * q))


# ---------------------------------------------------------------- 几何工具


def _cum(P: np.ndarray) -> np.ndarray:
    d = P[1:] - P[:-1]
    return np.concatenate([[0.0], np.cumsum(np.sqrt(d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1]))])


def _densify(P: np.ndarray, step: float) -> np.ndarray:
    out = [P[:1]]
    for a, b in itertools.pairwise(P):
        d = b - a
        L = math.sqrt(float(d[0]) * float(d[0]) + float(d[1]) * float(d[1]))
        k = max(1, math.ceil(L / step))
        t = np.arange(1, k + 1, dtype=np.float64)[:, None] / k
        out.append(a + d * t)
    return np.concatenate(out)


def _cut(P: np.ndarray, s0: float, s1: float) -> np.ndarray:
    """折线在弧长 [s0, s1] 内的部分（端点插值）。"""
    cum = _cum(P)

    def at(s: float) -> np.ndarray:
        i = min(max(int(np.searchsorted(cum, s, side="right")) - 1, 0), len(P) - 2)
        L = cum[i + 1] - cum[i]
        return P[i] + (P[i + 1] - P[i]) * (0.0 if L <= 0 else (s - cum[i]) / L)

    mid = P[(cum > s0) & (cum < s1)]
    return np.vstack([at(s0)[None], mid, at(s1)[None]])


def _dist_to_polyline(pts: np.ndarray, P: np.ndarray) -> np.ndarray:
    """点 (n, 2) 到折线 P 的距离。"""
    a, ab = P[:-1][None], (P[1:] - P[:-1])[None]
    p = pts[:, None]
    den = ab[..., 0] * ab[..., 0] + ab[..., 1] * ab[..., 1]
    t = np.clip(((p[..., 0] - a[..., 0]) * ab[..., 0] + (p[..., 1] - a[..., 1]) * ab[..., 1]) / np.where(den > 0, den, 1.0), 0.0, 1.0)
    dx = p[..., 0] - (a[..., 0] + ab[..., 0] * t)
    dy = p[..., 1] - (a[..., 1] + ab[..., 1] * t)
    return np.sqrt(dx * dx + dy * dy).min(axis=1)


def _near_len(P: np.ndarray, others: Sequence[np.ndarray], cap: float, prox: float, from_end: bool) -> float:
    """折线 P 自末端（或首端）起 `cap` 以内、距 `others` 任一折线 < prox 的最远采样点到该端的弧长（无为 0）。"""
    if cap <= 0 or not others:
        return 0.0
    Q = P[::-1] if from_end else P
    S = _densify(_cut(Q, 0.0, min(cap, float(_cum(Q)[-1]))), PROX_STEP_M)
    cum = _cum(S)
    d = np.full(len(S), np.inf)
    for poly in others:
        if len(poly) >= 2:
            d = np.minimum(d, _dist_to_polyline(S, poly))
    hit = np.flatnonzero(d < prox)
    return 0.0 if not len(hit) else min(cap, float(cum[hit[-1]]) + PROX_STEP_M)


def _box(F: tuple[float, float], R: tuple[float, float], L: float, W: float, m: float) -> tuple[float, ...]:
    """车身矩形：车头 F 与车尾 R 连线为纵轴（弯道上为弦向），长 L、宽 W，各边外扩 m → (cx, cy, hx, hy, a, b)。"""
    dx, dy = F[0] - R[0], F[1] - R[1]
    n = math.sqrt(dx * dx + dy * dy)
    hx, hy = (dx / n, dy / n) if n > 0 else (1.0, 0.0)
    return ((F[0] + R[0]) / 2.0, (F[1] + R[1]) / 2.0, hx, hy, L / 2.0 + m, W / 2.0 + m)


def _hit(A: tuple[float, ...], B: tuple[float, ...]) -> bool:
    """两个车身矩形是否相交（分离轴；边界接触不算相交）。"""
    cx, cy = B[0] - A[0], B[1] - A[1]
    for ux, uy in ((A[2], A[3]), (-A[3], A[2]), (B[2], B[3]), (-B[3], B[2])):
        ra = A[4] * abs(A[2] * ux + A[3] * uy) + A[5] * abs(-A[3] * ux + A[2] * uy)
        rb = B[4] * abs(B[2] * ux + B[3] * uy) + B[5] * abs(-B[3] * ux + B[2] * uy)
        if abs(cx * ux + cy * uy) >= ra + rb - 1e-9:
            return False
    return True


# ---------------------------------------------------------------- 路网预计算


class Network:
    """车道图上与车辆无关的预计算：前驱、路口单元（延伸长度与中心线）、单元冲突、分叉兄弟与分离弧长、准入等级、段限速、走廊。"""

    def __init__(self, g: LaneGraph, *, prox_m: float = PROX_M) -> None:
        from scipy.spatial import cKDTree

        self.g = g
        self.prox_m = float(prox_m)
        segs = g.segs
        self.pred: dict[str, list[str]] = {}
        for k in sorted(segs):
            for w in segs[k].succ:
                self.pred.setdefault(w, []).append(k)
        conns = sorted(k for k, s in segs.items() if s.kind == "connector")
        self.src = {c: str(segs[c].props["from_lane"]) for c in conns}
        self.dst = {c: str(segs[c].props["to_lane"]) for c in conns}
        node_of = {c: str(segs[c].props.get("junction", "")) for c in conns}
        edge = {k: s.props.get("edge_id") for k, s in segs.items() if s.kind == "lane"}
        self.edge_of = dict(edge)                      # 车道 → 道路边（连接段不在表中）
        lanes_at: dict[str, list[str]] = {}
        for k in sorted(edge):
            p = segs[k].props
            for v in sorted({p["from_node"], p["to_node"]}):
                lanes_at.setdefault(v, []).append(k)
        L, s0 = VEH_SIZE_M[0], IDM_DEFAULTS["s0_m"]
        self.ext_in: dict[str, float] = {}
        self.ext_out: dict[str, float] = {}
        unit_xy: dict[str, np.ndarray] = {}
        far: dict[str, np.ndarray | None] = {}
        self.exclusive: set[str] = set()              # 单元独占（同一时刻只允许一辆车持有）
        c_samples = [_densify(segs[c].xy, PROX_STEP_M) for c in conns]
        c_tree = cKDTree(np.concatenate(c_samples)) if conns else None
        c_own = np.concatenate([np.full(len(P), i, np.int64) for i, P in enumerate(c_samples)]) if conns else None

        def near(P: np.ndarray, s0_: float, s1_: float) -> list[str]:
            """与折线 P 在弧长 [s0_, s1_] 内的部分距离 < prox 的连接段（任一路口）。"""
            if c_tree is None or s1_ <= s0_:
                return []
            hits = c_tree.query_ball_point(_densify(_cut(P, s0_, s1_), PROX_STEP_M), self.prox_m)
            return [conns[i] for i in sorted({int(c_own[h]) for lst in hits for h in lst})]

        for c in conns:
            v, A, D = node_of[c], self.src[c], self.dst[c]
            pa, pd = segs[A], segs[D]
            cap_a = min(EXT_CAP_M, max(0.0, pa.length - (L + s0 + 1.0)))
            oth_a = [segs[x].xy for x in near(pa.xy, pa.length - cap_a, pa.length) if self.src[x] != A] + \
                [segs[x].xy for x in lanes_at.get(v, []) if x != A and edge[x] != edge[A]]
            e_in = _near_len(pa.xy, oth_a, cap_a, self.prox_m, True)
            e_in = min(max(e_in, WAIT_CLEAR_M - segs[c].length), cap_a) if cap_a > 0 else 0.0
            cap_d = min(EXT_CAP_M, pd.length / 2.0)
            oth_d = [segs[x].xy for x in near(pd.xy, 0.0, cap_d) if x != c and self.dst[x] != D and self.src[x] != D] + \
                [segs[x].xy for x in lanes_at.get(v, []) if x != D and edge[x] != edge[D]]
            e_out = _near_len(pd.xy, oth_d, cap_d, self.prox_m, False)
            self.ext_in[c], self.ext_out[c] = max(0.0, e_in), e_out
            # 远段：连接段加 D 首段中距分叉点 ≥ 2·PROX 的部分；远段回到 A 末段附近（掉头式急弯）时单元独占并把 e_in 延伸到该处
            S = _densify(np.concatenate([segs[c].xy] + ([_cut(pd.xy, 0.0, e_out)] if e_out > 0 else [])), PROX_STEP_M)
            d0 = np.hypot(S[:, 0] - S[0, 0], S[:, 1] - S[0, 1])
            k0 = np.flatnonzero(d0 >= 2.0 * self.prox_m)
            far[c] = S[int(k0[0]):] if len(k0) and len(S) - int(k0[0]) >= 2 else None
            if far[c] is not None and cap_a > 0:
                e_hair = _near_len(pa.xy, [far[c]], cap_a, self.prox_m, True)
                if e_hair > 0:
                    self.ext_in[c] = max(self.ext_in[c], e_hair)
                    self.exclusive.add(c)
        for c in conns:
            pa, pd = segs[self.src[c]], segs[self.dst[c]]
            parts = []
            if self.ext_in[c] > 0:
                parts.append(_cut(pa.xy, pa.length - self.ext_in[c], pa.length))
            parts.append(segs[c].xy)
            if self.ext_out[c] > 0:
                parts.append(_cut(pd.xy, 0.0, self.ext_out[c]))
            unit_xy[c] = np.concatenate(parts)
        self.conflicts: dict[str, set[str]] = {c: {x for x in segs[c].props.get("conflicts", []) if x in segs} for c in conns}
        samples = [_densify(unit_xy[c], PROX_STEP_M) for c in conns]
        self.prox_pairs = 0
        if conns:
            own = np.concatenate([np.full(len(P), i, np.int64) for i, P in enumerate(samples)])
            tree = cKDTree(np.concatenate(samples))
            pairs = sorted({(min(int(own[a]), int(own[b])), max(int(own[a]), int(own[b]))) for a, b in tree.query_pairs(self.prox_m)})
            for a, b in pairs:
                ca, cb = conns[a], conns[b]
                if a == b or self.src[ca] == self.src[cb] or cb in self.conflicts[ca]:
                    continue
                self.conflicts[ca].add(cb)
                self.conflicts[cb].add(ca)
                self.prox_pairs += 1
        # 同一来源车道的单元：一方远段接近另一方单元时冲突（分叉后路径再次靠近，例如掉头式急弯回到来源车道旁）
        by_src0: dict[str, list[int]] = {}
        for i, c in enumerate(conns):
            by_src0.setdefault(self.src[c], []).append(i)
        for ids in by_src0.values():
            for a, b in itertools.combinations(ids, 2):
                ca, cb = conns[a], conns[b]
                hit = any(far[x] is not None and float(_dist_to_polyline(far[x], samples[y]).min()) < self.prox_m
                          for x, y in ((ca, b), (cb, a)))
                if hit and cb not in self.conflicts[ca]:
                    self.conflicts[ca].add(cb)
                    self.conflicts[cb].add(ca)
                    self.prox_pairs += 1
        self.siblings: dict[str, list[tuple[str, float]]] = {}
        by_src: dict[str, list[str]] = {}
        for c in conns:
            by_src.setdefault(self.src[c], []).append(c)
        for ids in by_src.values():
            for ci in ids:
                for cj in ids:
                    if ci != cj:
                        S = _densify(segs[cj].xy, PROX_STEP_M)
                        far = np.flatnonzero(_dist_to_polyline(S, segs[ci].xy) >= self.prox_m)
                        cum = _cum(S)
                        self.siblings.setdefault(ci, []).append((cj, float(cum[far[0]]) if len(far) else float(cum[-1])))
        stop_rule = {}
        for k, s in segs.items():
            sid = s.props.get("stop_line") if s.kind == "lane" else None
            if sid and sid in g.stops:
                stop_rule[k] = g.stops[sid]["rule"]
        self.commit = {c for c in conns if self.conflicts[c] or c in self.exclusive}
        self.cls = {c: (0 if stop_rule.get(self.src[c]) == "give_way" else CLASS.get(str(segs[c].props.get("priority", "none")), 1))
                    for c in conns}
        self.stop_sign = {c for c in conns if stop_rule.get(self.src[c]) == "stop_sign"}
        self.curve = {c: math.sqrt(A_LAT * float(segs[c].props["radius_min_m"])) for c in conns
                      if segs[c].props.get("radius_min_m") is not None and float(segs[c].props["radius_min_m"]) > 0}
        self.junction_of = {c: self._junction(node_of[c]) for c in conns}
        self.tail = {k: 0.0 for k in edge}            # 车道末端 / 首端被单元延伸占用的长度（背景出生避开）
        self.head = {k: 0.0 for k in edge}
        for c in sorted(self.commit):
            self.tail[self.src[c]] = max(self.tail[self.src[c]], self.ext_in[c])
            self.head[self.dst[c]] = max(self.head[self.dst[c]], self.ext_out[c])
        self._corridor: Corridor | None = None

    def _junction(self, node: str) -> str | None:
        jid = f"j-{node[2:]}" if node.startswith("n-") else f"j-{node}"
        return jid if jid in self.g.junctions else None

    def limit(self, sid: str, p: DriveParams) -> float:
        """段的期望速度上限：限速 × 风格系数（`v0_mps` 再设上限），连接段另取弯道限速。"""
        v = self.g.segs[sid].speed * p.v0_scale
        if p.v0_mps is not None:
            v = min(v, p.v0_mps)
        return min(v, self.curve[sid]) if sid in self.curve else v

    def corridor(self) -> Corridor:
        """道路走廊（车辆道路边中心线按 max(width_m/2, 5 m) 缓冲；车行道 width_m/2 + 0.5 m），与 M22 车道图构建同一口径。"""
        if self._corridor is None:
            lines = [(e["xy"], max(float(e["width_m"]) / 2.0, CORRIDOR_HALF_M), float(e["width_m"]) / 2.0 + CARRIAGEWAY_TOL_M)
                     for _k, e in sorted(self.g.edges.items()) if e.get("lane_ids")]
            self._corridor = Corridor(lines)
        return self._corridor


# ---------------------------------------------------------------- 路线


class _Path:
    """一条路线在路线坐标 u 下的几何：段、段首坐标 U、长度、需要准入的单元 [(路线下标, 连接段, u_in, u_out)]。"""

    __slots__ = ("U", "lens", "pl", "route", "segs", "u_end", "units")

    def __init__(self, g: LaneGraph, r: LaneRoute, net: Network) -> None:
        self.route = list(r.segments)
        self.segs = [g.segs[k] for k in self.route]
        self.lens = [s.length for s in self.segs]
        U = [-r.s_start]
        for ln in self.lens[:-1]:
            U.append(U[-1] + ln)
        self.U = U
        self.u_end = U[-1] + r.s_end
        self.units = [(j, c, U[j] - net.ext_in[c], U[j] + self.lens[j] + net.ext_out[c]) for j, c in enumerate(self.route)
                      if c in net.commit]
        self.pl = [(s.s.tolist(), s.xy[:, 0].tolist(), s.xy[:, 1].tolist()) for s in self.segs]

    def idx(self, u: float) -> int:
        """车头所在的路线段下标（u 恰在段首时属于前一段）。"""
        j = bisect.bisect_left(self.U, u) - 1
        return 0 if j < 0 else j

    def point(self, u: float) -> tuple[float, float]:
        """路线坐标 u 处的位置（越出路线两端时沿端段切向外推）。"""
        j = self.idx(u)
        sl, xl, yl = self.pl[j]
        s = u - self.U[j]
        k = bisect.bisect_right(sl, s) - 1
        k = 0 if k < 0 else (k if k < len(sl) - 1 else len(sl) - 2)
        Lk = sl[k + 1] - sl[k]
        t = 0.0 if Lk <= 0 else (s - sl[k]) / Lk
        return xl[k] + (xl[k + 1] - xl[k]) * t, yl[k] + (yl[k + 1] - yl[k]) * t

    def pieces(self, u: float, L: float) -> list[tuple[str, float, float, int]]:
        """车身 [u − L, u] 在各路线段上的区间 (段 id, lo, hi, 路线下标)。"""
        rear = u - L
        out = []
        j = self.idx(u)
        while j >= 0:
            a = self.U[j]
            lo, hi = max(rear, a) - a, min(u, a + self.lens[j]) - a
            if hi > lo:
                out.append((self.route[j], max(lo, 0.0), hi, j))
            if rear >= a:
                break
            j -= 1
        return out


def _with_start(g: LaneGraph, r: LaneRoute, s: float) -> LaneRoute:
    first = g.segs[r.segments[0]]
    return LaneRoute(list(r.segments), s, r.s_end, r.length_m - (s - r.s_start), r.time_s - (s - r.s_start) / first.speed)


def chain_route(g: LaneGraph, points: Sequence[Any]) -> tuple[LaneRoute, list[float]] | None:
    """锚点链（段 id、节点 id 或世界帧 (E, N)，见 `LaneGraph.lane_route`）→ (连续路线, 各点的路线坐标 u)；任一段不可达或
    相邻两段不在同一车道上衔接时为 None。相邻两段在衔接点所在的车道上合并（后一段首段与前一段末段相同）。"""
    if len(points) < 2:
        raise ValueError("a vehicle route needs at least two points")
    segs: list[str] = []
    s_start = s_end = 0.0
    us = [0.0]
    total = 0.0
    for a, b in itertools.pairwise(points):
        r = g.lane_route(a, b)
        if r is None:
            return None
        if not segs:
            segs, s_start = list(r.segments), r.s_start
        elif r.segments[0] != segs[-1]:
            return None
        else:
            segs += r.segments[1:]
        s_end = r.s_end
        total += r.length_m
        us.append(total)
    T = 0.0
    for i, k in enumerate(segs):
        s = g.segs[k]
        T += ((s_end if i == len(segs) - 1 else s.length) - (s_start if i == 0 else 0.0)) / s.speed
    return LaneRoute(segs, s_start, s_end, total, T), us


# ---------------------------------------------------------------- 车辆


@dataclass
class VehicleSpec:
    """一辆车的生成输入：路线、出发时刻（ms，100 ms 整数倍）、驾驶参数、途中停车 `stops[(u_m, dwell_ms)]` 与终点行为。"""

    id: str
    route: LaneRoute
    depart_ms: int = 0
    drive: DriveParams = field(default_factory=DriveParams)
    stops: tuple[tuple[float, int], ...] = ()
    end: str = "despawn"                       # despawn | park | wait
    v_init: float = 0.0
    length_m: float = VEH_SIZE_M[0]
    width_m: float = VEH_SIZE_M[1]


class _Veh:
    __slots__ = ("L", "W", "arr", "brake", "committed", "dwell_until", "eng_t", "i", "id", "lim", "p", "path", "released", "spec",
                 "status", "stop_i", "stopped_sign", "u", "u0", "v", "why")

    def __init__(self, i: int, spec: VehicleSpec, net: Network) -> None:
        if spec.end not in ("despawn", "park", "wait"):
            raise ValueError(f"{spec.id}: unknown end behaviour {spec.end!r}")
        if spec.depart_ms % TICK_MS:
            raise ValueError(f"{spec.id}: depart_ms must be a multiple of {TICK_MS} ms")
        self.i, self.id, self.spec, self.p = i, spec.id, spec, spec.drive
        self.path = _Path(net.g, spec.route, net)
        self.L, self.W = float(spec.length_m), float(spec.width_m)
        self.u0 = max(0.0, self.L - spec.route.s_start)
        if self.u0 >= self.path.u_end:
            raise ValueError(f"{spec.id}: route shorter than the vehicle")
        self.lim = [net.limit(k, self.p) for k in self.path.route]
        self.status, self.u, self.v = _ABSENT, self.u0, 0.0
        self.committed: set[int] = set()               # 已准入的单元（`path.units` 下标）
        self.released: set[int] = set()
        self.arr: dict[int, int] = {}                  # 单元 → 到达时刻（ms）
        self.stopped_sign: set[int] = set()
        self.brake: set[int] = set()                   # 已对其起点减速的单元（滞回：准入前保持）
        self.stop_i, self.dwell_until = 0, None
        self.eng_t = 0
        self.why: str | None = None                    # 最近一次未获准入的原因

    def next_unit(self) -> int | None:
        """下一个未准入且连接段尚未驶入的单元。"""
        for q, (j, _c, _ui, _uo) in enumerate(self.path.units):
            if q not in self.committed and self.path.U[j] >= self.u - 1e-9:
                return q
        return None

    def box(self, u: float, m: float = 0.0) -> tuple[float, ...]:
        return _box(self.path.point(u), self.path.point(u - self.L), self.L, self.W, m)


# ---------------------------------------------------------------- 生成


@dataclass
class VehicleSim:
    """生成结果：逐帧路线坐标与速度、磁带量（位置、速度、高度、状态、标志）与决策日志。"""

    ids: list[str]
    specs: list[VehicleSpec]
    t_ms: np.ndarray
    u: np.ndarray                              # (n_t, n) 路线坐标；未出现为 NaN
    v: np.ndarray
    state: np.ndarray                          # (n_t, n) uint8
    flags: np.ndarray                          # (n_t, n) uint8
    xy: np.ndarray                             # (n_t, n, 2)
    vxy: np.ndarray
    z: np.ndarray
    heading: np.ndarray                        # (n_t, n, 2) 车头所在段的单位切向
    log: dict

    @property
    def routes(self) -> list[LaneRoute]:
        return [s.route for s in self.specs]

    def track(self, i: int) -> dict[str, np.ndarray]:
        """与 `gen.eval_timeline` 同形的逐帧求值（xy、vxy、state、container、z_hint）加 `flags`，供 E09 接入磁带。"""
        return {"xy": self.xy[:, i].copy(), "vxy": self.vxy[:, i].copy(), "state": self.state[:, i].copy(),
                "container": np.full(len(self.t_ms), -1, np.int64), "z_hint": self.z[:, i].copy(), "flags": self.flags[:, i].copy()}

    def tape(self) -> dict[str, np.ndarray]:
        """磁带量（毫米定点后的米值）：验证与关键事件只读这些量。"""
        return {"xy": np.floor(self.xy * 1000.0 + 0.5) / 1000.0, "state": self.state.copy(), "flags": self.flags.copy()}

    def digest(self) -> str:
        """毫米定点位置与高度、状态、标志与决策日志的 sha256（确定性对拍；E09 并入事件 `content_sha256`）。"""
        h = hashlib.sha256()
        h.update(jcs({"ids": self.ids, "n_t": len(self.t_ms)}))
        for a in (np.floor(self.xy * 1000.0 + 0.5).astype(np.int64), np.floor(np.nan_to_num(self.z) * 1000.0 + 0.5).astype(np.int64),
                  self.state, self.flags):
            h.update(np.ascontiguousarray(a).tobytes())
        h.update(jcs(self.log))
        return h.hexdigest()


def _occupancy(vs: list[_Veh]) -> dict[str, list[tuple[float, float, int]]]:
    """段 id → 该段上的车身区间 [(lo, hi, 车辆)]（行驶、静止与泊车车辆）。"""
    occ: dict[str, list[tuple[float, float, int]]] = {}
    for w in vs:
        if w.status in (_ACTIVE, _PARKED):
            for sid, lo, hi, _j in w.path.pieces(w.u, w.L):
                occ.setdefault(sid, []).append((lo, hi, w.i))
    return occ


def _leader(w: _Veh, occ: Mapping[str, list], net: Network, vel: Mapping[int, float], la: float) -> tuple[float, float, int]:
    """沿路线前方最近的他车车身位置（路线坐标）、其速度与车辆（含分叉兄弟连接段上分离弧长内的车身）；无为 (inf, 0, −1)。"""
    P, u = w.path, w.u
    kf = P.idx(u)
    s_f = u - P.U[kf]
    j = kf
    while j < len(P.route) and P.U[j] - u < la:
        sid = P.route[j]
        best, bv, who = math.inf, 0.0, -1
        cands = list(occ.get(sid, ()))
        cands += [(lo, hi, o) for c2, div in net.siblings.get(sid, ()) for lo, hi, o in occ.get(c2, ()) if lo < div]
        for lo, hi, o in cands:
            if o == w.i or (j == kf and hi <= s_f + 1e-9):
                continue
            if P.U[j] + lo < best:
                best, bv, who = P.U[j] + lo, vel[o], o
        if best < math.inf:
            return best, bv, who
        j += 1
    return math.inf, 0.0, -1


def _grid(boxes: Mapping[int, tuple]) -> dict[tuple[int, int], list[int]]:
    grid: dict[tuple[int, int], list[int]] = {}
    for i, b in boxes.items():
        grid.setdefault((math.floor(b[0] / GRID_M), math.floor(b[1] / GRID_M)), []).append(i)
    return grid


def _probe(w: _Veh, lead_id: int, boxes: Mapping[int, tuple], grid: Mapping[tuple[int, int], list[int]],
           vel: Mapping[int, float], edge_now: Mapping[int, Any], net: Network) -> tuple[float, float]:
    """几何前车：沿本车路线前方 v·T + v²/(2b) + s0 + 3 m 内每 0.5 m 放置车头（中点与两角），最先进入他车车身矩形处的路线坐标
    （减 0.5 m）与该车速度；候选为车头附近、不在车头之后的他车，沿路线前车只在航向差 > 30° 时（急弯）计入，位于本车前方
    同一道路边车道上的车辆（同向前车与平行车道）不计入；每个候选只从其欧氏距离下界对应的弧长起检查。无为 (inf, 0)。供 IDM 在车道图几何重叠与急弯处提前减速；无相交的硬保证由安全层给出。"""
    p, u, v = w.p, w.u, w.v
    D = v * p.T_s + v * v / (2.0 * p.b_mps2) + p.s0_m + 3.0
    P = w.path
    fx, fy = P.point(u)
    bx, by = P.point(u - 0.5)
    tx, ty = fx - bx, fy - by
    n = math.sqrt(tx * tx + ty * ty)
    tx, ty = (tx / n, ty / n) if n > 0 else (0.0, 0.0)
    hw = w.W / 2.0
    kf = P.idx(u)
    mine = {net.edge_of.get(k) for k in P.route[kf:kf + 3]} - {None}
    gx, gy = math.floor(fx / GRID_M), math.floor(fy / GRID_M)
    r = math.ceil((D + VEH_SIZE_M[0]) / GRID_M)
    cands = []
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            for o in grid.get((gx + dx, gy + dy), ()):
                if o == w.i or edge_now.get(o) in mine:
                    continue
                b = boxes[o]
                ex, ey = b[0] - fx, b[1] - fy
                rad = b[4] + b[5] + hw
                dist = math.sqrt(ex * ex + ey * ey)
                if dist - rad > D or ex * tx + ey * ty < -rad or (o == lead_id and tx * b[2] + ty * b[3] >= COS_SAME):
                    continue
                cands.append((max(0.5, dist - rad), o, b))
    if not cands:
        return math.inf, 0.0
    cands.sort()
    qx, qy = P.point(u + cands[0][0] - 0.5)
    d = cands[0][0]
    while d <= D + 1e-9:
        px, py = P.point(u + d)
        sx, sy = px - qx, py - qy
        n = math.sqrt(sx * sx + sy * sy)
        if n > 0:
            sx, sy = sx / n, sy / n
            pts = ((px, py), (px - sy * hw, py + sx * hw), (px + sy * hw, py - sx * hw))
            for lb, o, b in cands:
                if lb > d:
                    break
                for ax, ay in pts:
                    ex, ey = ax - b[0], ay - b[1]
                    if abs(ex * b[2] + ey * b[3]) <= b[4] and abs(-ex * b[3] + ey * b[2]) <= b[5]:
                        return u + d - 0.5, vel.get(o, 0.0)
        qx, qy = px, py
        d += 0.5
    return math.inf, 0.0


def _spawn_need(v: float) -> float:
    """出生点车尾后方所需的空余距离：后车以缺省 IDM 参数舒适停车所需距离加 `SPAWN_CLEAR_M`。"""
    return SPAWN_CLEAR_M + v * IDM_DEFAULTS["T_s"] + v * v / (2.0 * IDM_DEFAULTS["b_mps2"])


def _spawn_clear(w: _Veh, occ: Mapping[str, list], net: Network, vel: Mapping[int, float], boxes: Mapping[int, tuple]) -> bool:
    """出生检查（模块说明第 8 条）。"""
    P = w.path
    j = P.idx(w.u0)
    sid = P.route[j]
    s_f = w.u0 - P.U[j]
    s_r = s_f - w.L
    for lo, hi, o in occ.get(sid, ()):
        if lo < s_f + SPAWN_CLEAR_M and hi > s_r - _spawn_need(vel.get(o, 0.0)):
            return False
    fwd = s_f + SPAWN_CLEAR_M - P.lens[j]               # 向前经路线后续段还需检查的长度
    jj = j + 1
    while fwd > 0 and jj < len(P.route):
        nxt = P.route[jj]
        if any(lo < fwd for lo, _hi, _o in occ.get(nxt, ())) or \
                any(lo < min(fwd, div) for c2, div in net.siblings.get(nxt, ()) for lo, _hi, _o in occ.get(c2, ())):
            return False
        fwd -= P.lens[jj]
        jj += 1
    frontier = [(sid, s_r)]                             # (段, 本车车尾到该段段首的距离)
    seen = {sid}
    while frontier:
        cur, dist = frontier.pop()
        if dist > SPAWN_BACK_M:
            continue
        for p in net.pred.get(cur, ()):
            Lp = net.g.segs[p].length
            for _lo, hi, o in occ.get(p, ()):
                if dist + (Lp - hi) < _spawn_need(vel.get(o, 0.0)):
                    return False
            if p not in seen:
                seen.add(p)
                frontier.append((p, dist + Lp))
    mine = w.box(w.u0, SPAWN_CLEAR_M / 16.0)
    return not any(_hit(mine, b) for b in boxes.values())


def _t_clear_own(w: _Veh, q: int) -> float:
    """自当前状态以 0.8·a 加速（不超过单元内各段限速）到车尾驶出单元终点的时间。"""
    j, _c, _ui, u_out = w.path.units[q]
    X = max(0.0, u_out + w.L - w.u)
    vmax = min(w.lim[max(0, j - 1):j + 2])
    a = CLEAR_ACC_FRAC * w.p.a_mps2
    v = min(w.v, vmax)
    t_acc = (vmax - v) / a
    x_acc = (v + vmax) / 2.0 * t_acc
    if x_acc >= X:
        return (math.sqrt(v * v + 2.0 * a * X) - v) / a
    return t_acc + (X - x_acc) / vmax


def _t_clear(w: _Veh, q: int, leader: _Veh | None) -> float:
    """次路准入所需的高级车辆让行时间：驶出单元时间 + 1 s；前车持有同一单元时驶出时间不少于前车驶出时间 + T。"""
    t = _t_clear_own(w, q)
    c = w.path.units[q][1]
    if leader is not None:
        for q2 in sorted(leader.committed - leader.released):
            if leader.path.units[q2][1] == c:
                t = max(t, _t_clear_own(leader, q2) + w.p.T_s)
    return t + CLEAR_MARGIN_S


def _d_req(v: float, p: DriveParams) -> float:
    """准入提出距离：舒适减速的停车距离加一步行程与 2 m 余量。"""
    return v * v / (2.0 * p.b_mps2) + v * DT + COMMIT_MARGIN_M


def _step(w: _Veh, lead: tuple[float, float, int], geo: tuple[float, float], log: dict) -> tuple[float, float]:
    """一步 100 ms 的 IDM 弹道更新（只读 t_k 快照与本车状态；`geo` 为几何前车位置）：返回 (u, v)。"""
    p, u, v, P = w.p, w.u, w.v, w.path
    kf = P.idx(u)
    v0 = w.lim[kf]
    acc = idm_accel(v, v0, None, 0.0, p)
    lu, lv, _who = lead
    u_lead = math.inf
    if lu < math.inf:
        acc = min(acc, idm_accel(v, v0, lu - u, v - lv, p))
        u_lead = lu - MIN_GAP_M
    if geo[0] < math.inf:
        acc = min(acc, idm_accel(v, v0, max(geo[0] - u, 0.0), v - geo[1], p))
    virt = math.inf                                     # 虚拟静止障碍（IDM，车头停在该处）
    u_hard = math.inf                                   # 硬约束：不越过未准入单元的起点、停车点与终点
    q = w.next_unit()
    if q is not None:
        u_in = max(P.units[q][2], u)
        u_hard = u_in
        if q in w.brake or u_in - u <= _d_req(v, p):
            w.brake.add(q)
            virt = u_in
    if w.stop_i < len(w.spec.stops):
        virt = min(virt, w.spec.stops[w.stop_i][0])
        u_hard = min(u_hard, w.spec.stops[w.stop_i][0])
    if w.spec.end in ("park", "wait"):
        virt = min(virt, P.u_end)
        u_hard = min(u_hard, P.u_end)
    if virt < math.inf:
        acc = min(acc, idm_accel(v, v0, virt - u + p.s0_m, v, p))
    if v == 0.0 and acc < V_START / DT:
        return u, 0.0                                   # 静止保持：一步内达不到起步速度时不动（避免蠕行）
    if -acc > log["max_decel_mps2"]:
        log["max_decel_mps2"] = round(-acc, 6)
    cap = v0 if v <= v0 else max(v0, v - p.b_mps2 * DT)  # 前方低限速段提前减速（硬上限）
    j = kf + 1
    while j < len(P.route) and P.U[j] - u < 200.0:
        dd = max(0.0, P.U[j] - u - v * DT)
        cap = min(cap, math.sqrt(w.lim[j] * w.lim[j] + 2.0 * p.b_mps2 * dd))
        j += 1
    vn = v + acc * DT
    if vn < 0.0:
        du = -0.5 * v * v / acc if acc < 0 else 0.0
        vn = 0.0
    else:
        vn = min(vn, cap)
        du = 0.5 * (v + vn) * DT
    if vn < V_SNAP and acc < 0:
        vn = 0.0
    un = u + du
    lim = min(u_lead, u_hard)
    if un > lim:
        un2 = max(u, lim)
        log["gap_clamps" if u_lead <= u_hard else "stop_clamps"] += 1
        vn = max(0.0, min(vn, 2.0 * (un2 - u) / DT - v))
        un = un2
    if w.spec.end == "despawn" and un > P.u_end:
        un = P.u_end
    return un, vn


def _safety(active: list[_Veh], new: dict[int, tuple[float, float]], cur: Mapping[int, tuple], grid: Mapping[tuple[int, int], list[int]],
            log: dict, t: int) -> None:
    """几何安全层（模块说明第 6 条）：按 (未持有单元, idx) 次序逐车核对新车身矩形，相交则原地停住（`cur` 为当前车身矩形）。"""
    by_i = {w.i: w for w in active}
    final: dict[int, tuple] = {}
    for w in sorted((w for w in active if w.i in new), key=lambda w: (not (w.committed - w.released), w.i)):
        un, _vn = new[w.i]
        if un == w.u:
            final[w.i] = cur[w.i]
            continue
        b = w.box(un, SAFETY_MARGIN_M)
        gx, gy = math.floor(b[0] / GRID_M), math.floor(b[1] / GRID_M)
        hit = next((o for dx in (-1, 0, 1) for dy in (-1, 0, 1) for o in grid.get((gx + dx, gy + dy), ())
                    if o != w.i and _hit(b, final.get(o, cur[o]))), None)
        if hit is None:
            final[w.i] = b
        else:
            new[w.i] = (w.u, 0.0)
            final[w.i] = cur[w.i]
            log["safety_stops"] += 1
            if len(log["safety_samples"]) < 20:
                log["safety_samples"].append({"t_ms": t, "actor": w.id, "other": by_i[hit].id})


def simulate(g: LaneGraph, specs: Sequence[VehicleSpec], duration_ms: int, *, net: Network | None = None,
             z_fn: Callable[[np.ndarray], np.ndarray] | None = None) -> VehicleSim:
    """按 M23 §15.4 推进全部车辆（见模块说明），返回逐帧结果与决策日志。`z_fn` 给出时高度取 `z_fn(xy)`（例如车辆 profile 的
    `walkable_height`），否则取车道图逐顶点高度的弧长插值（段内有缺失高度时为 0）。"""
    net = net or Network(g)
    if duration_ms % TICK_MS:
        raise ValueError(f"duration_ms must be a multiple of {TICK_MS} ms")
    n_t = duration_ms // TICK_MS
    vs = [_Veh(i, s, net) for i, s in enumerate(specs)]
    n = len(vs)
    if len({w.id for w in vs}) != n:
        raise ValueError("vehicle ids must be unique")
    U_rec = np.full((n_t, n), np.nan)
    V_rec = np.zeros((n_t, n))
    S_rec = np.zeros((n_t, n), np.uint8)
    F_rec = np.zeros((n_t, n), np.uint8)
    holds: dict[str, set[int]] = {}
    log: dict[str, Any] = {"commits": [], "arrivals": [], "stops": [], "parks": [], "spawns": [], "denied": {}, "gap_clamps": 0,
                           "stop_clamps": 0, "safety_stops": 0, "safety_samples": [], "max_decel_mps2": 0.0, "max_wait_ms": 0,
                           "spawn_shift_m": {w.id: round(w.u0, 6) for w in vs if w.u0 > 0}}
    denied = log["denied"]

    def release(w: _Veh, q: int) -> None:
        w.released.add(q)
        holds[w.path.units[q][1]].discard(w.i)

    for k in range(n_t):
        t = k * TICK_MS
        # 1. 出生
        occ = _occupancy(vs)
        vel0 = {w.i: w.v for w in vs if w.status in (_ACTIVE, _PARKED)}
        boxes = {w.i: w.box(w.u) for w in vs if w.status in (_ACTIVE, _PARKED)}
        for w in vs:
            if w.status == _ABSENT and t >= w.spec.depart_ms and _spawn_clear(w, occ, net, vel0, boxes):
                w.status, w.u, w.v, w.eng_t = _ACTIVE, w.u0, float(w.spec.v_init), t
                log["spawns"].append({"actor": w.id, "t_ms": t, "delay_ms": t - w.spec.depart_ms})
                for sid, lo, hi, _j in w.path.pieces(w.u, w.L):
                    occ.setdefault(sid, []).append((lo, hi, w.i))
                vel0[w.i] = w.v
                boxes[w.i] = w.box(w.u)
        # 2. 记录 t_k 的状态
        for w in vs:
            if w.status == _ABSENT:
                continue
            U_rec[k, w.i] = w.u
            if w.status == _GONE:
                S_rec[k, w.i] = STATE_GONE
                continue
            V_rec[k, w.i] = w.v
            S_rec[k, w.i] = STATE_MOVING if w.v > 0 else STATE_WAITING
            if w.v > 0:
                w.eng_t = t
            F_rec[k, w.i] = FLAG_ENGINE if w.status == _ACTIVE and t - w.eng_t <= ENGINE_IDLE_MS else 0
        active = [w for w in vs if w.status in (_ACTIVE, _PARKED)]
        moving = [w for w in active if w.status == _ACTIVE]
        vel = {w.i: w.v for w in active}
        lead = {w.i: _leader(w, occ, net, vel, max(LOOKAHEAD_MIN_M, w.v * w.p.T_s + w.v * w.v / w.p.b_mps2 + 30.0)) for w in moving}
        # 3. 路口准入
        approach: dict[str, list[tuple[float, float, float, int]]] = {}
        for w in moving:
            for q, (j, c, u_in, _uo) in enumerate(w.path.units):
                if q in w.committed or w.path.U[j] < w.u - 1e-9:
                    continue
                d = max(0.0, u_in - w.u)
                if d > ETA_SCAN_M:
                    break
                approach.setdefault(c, []).append((d, max(0.0, d - _d_req(w.v, w.p)), w.v, w.i))
        reqs = []
        for w in moving:
            q = w.next_unit()
            if q is None:
                continue
            j, c, u_in, _uo = w.path.units[q]
            d = max(0.0, u_in - w.u)
            if lead[w.i][0] < w.path.U[j] - 1e-9:
                continue                                        # 非队首
            if q not in w.arr and d > _d_req(w.v, w.p):
                continue
            if c in net.stop_sign and q not in w.stopped_sign:
                if w.v == 0.0 and d <= STOP_TOL_M:
                    w.stopped_sign.add(q)
                else:
                    continue
            if q not in w.arr:
                w.arr[q] = t
                log["arrivals"].append({"actor": w.id, "conn": c, "j": j, "t_ms": t})
            reqs.append((-net.cls[c], w.arr[q], w.i, q, c))
        reqs.sort()
        pend: list[tuple[str, int]] = []
        for _ncls, arr, i, q, c in reqs:
            w = vs[i]
            j, _c, _ui, u_out = w.path.units[q]
            cf = net.conflicts[c]
            why = None
            if any(holds.get(c2) and holds[c2] - {i} for c2 in sorted(cf)) or (c in net.exclusive and holds.get(c, set()) - {i}):
                why = "occupied"
            elif any(c2 in cf and o != i for c2, o in pend):
                why = "queue"
            elif any(net.cls[c2] > net.cls[c] for c2 in cf) and (t_cl := _t_clear(w, q, vs[lead[i][2]] if lead[i][2] >= 0 else None)) and \
                    any(net.cls[c2] > net.cls[c] and any(o != i and (d2 / max(v2, V_ETA_MIN) <= GAP_S or r2 / max(v2, V_ETA_MIN) <= t_cl)
                                                         for d2, r2, v2, o in approach.get(c2, ()))
                        for c2 in sorted(cf)):
                why = "gap"
            elif j + 1 < len(w.path.route):
                nxt = w.path.route[j + 1]
                ahead = len(holds.get(c, set()) - {i})
                need = net.ext_out[c] + (ahead + 1) * (w.L + w.p.s0_m) + SPACE_MARGIN_M
                free = min((lo + vel.get(o, 0.0) * SPACE_LOOK_S for lo, _hi, o in occ.get(nxt, ()) if o != i), default=math.inf)
                if free < need and u_out < w.path.u_end:
                    why = "space"
            if why is None:
                holds.setdefault(c, set()).add(i)
                w.committed.add(q)
                log["commits"].append({"actor": w.id, "conn": c, "j": j, "t_ms": t, "arr_ms": arr, "cls": net.cls[c]})
                log["max_wait_ms"] = max(log["max_wait_ms"], t - arr)
            else:
                pend.append((c, i))
                denied[why] = denied.get(why, 0) + 1
                w.why = why
        # 4. 加速度与更新（以 t_k 快照计算），几何安全层
        cur = {w.i: w.box(w.u, SAFETY_MARGIN_M) for w in active}
        grid = _grid(cur)
        edge_now = {w.i: net.edge_of.get(w.path.route[w.path.idx(w.u)]) for w in active}
        new = {w.i: _step(w, lead[w.i], _probe(w, lead[w.i][2], cur, grid, vel, edge_now, net), log) for w in moving}
        _safety(active, new, cur, grid, log, t)
        tn = t + TICK_MS
        for w in moving:
            w.u, w.v = new[w.i]
            for q in sorted(w.committed - w.released):
                if w.u - w.L >= w.path.units[q][3] - 1e-9:
                    release(w, q)
            if w.stop_i < len(w.spec.stops):                    # 途中停车
                us, dwell = w.spec.stops[w.stop_i]
                if w.dwell_until is None and w.v == 0.0 and us - w.u <= STOP_TOL_M:
                    w.dwell_until = tn + int(dwell)
                    log["stops"].append({"actor": w.id, "u_m": round(us, 6), "t_ms": tn, "dwell_ms": int(dwell)})
                if w.dwell_until is not None and tn >= w.dwell_until:
                    w.stop_i, w.dwell_until = w.stop_i + 1, None
            at_end = w.v == 0.0 and w.path.u_end - w.u <= STOP_TOL_M and w.stop_i >= len(w.spec.stops)
            if w.spec.end == "despawn" and w.u >= w.path.u_end:
                w.status = _GONE
            elif w.spec.end == "park" and at_end:
                w.status = _PARKED
                log["parks"].append({"actor": w.id, "t_ms": tn, "u_m": round(w.u, 6)})
            if w.status != _ACTIVE or (w.spec.end == "wait" and at_end):
                for q in sorted(w.committed - w.released):     # 消失、泊车或在终点停住：释放持有
                    release(w, q)
    log["pending_at_end"] = [{"actor": w.id, "since_ms": min(t for q, t in w.arr.items() if q not in w.committed), "why": w.why} for w in vs
                             if w.status == _ACTIVE and any(q not in w.committed for q in w.arr)]
    log["absent_at_end"] = sorted(w.id for w in vs if w.status == _ABSENT)
    log["holds_at_end"] = {c: sorted(vs[i].id for i in hs) for c, hs in sorted(holds.items()) if hs}
    return _finish(vs, U_rec, V_rec, S_rec, F_rec, log, z_fn)


def _finish(vs: list[_Veh], U_rec, V_rec, S_rec, F_rec, log: dict, z_fn) -> VehicleSim:
    n_t, n = U_rec.shape
    xy = np.zeros((n_t, n, 2))
    hd = np.zeros((n_t, n, 2))
    z = np.zeros((n_t, n))
    for w in vs:
        P = w.path
        uu = np.where(np.isfinite(U_rec[:, w.i]), U_rec[:, w.i], w.u0)    # 未出现前取出生点，消失后取最后位置
        Ua = np.asarray(P.U)
        jj = np.clip(np.searchsorted(Ua, uu, side="left") - 1, 0, len(Ua) - 1)
        for j in np.unique(jj):
            m = jj == j
            seg = P.segs[int(j)]
            s = uu[m] - Ua[int(j)]
            i = np.clip(np.searchsorted(seg.s, s, side="right") - 1, 0, len(seg.xy) - 2)
            a, b = seg.xy[i], seg.xy[i + 1]
            Lk = seg.s[i + 1] - seg.s[i]
            tt = np.where(Lk > 0, (s - seg.s[i]) / np.where(Lk > 0, Lk, 1.0), 0.0)
            xy[m, w.i] = a + (b - a) * tt[:, None]
            hd[m, w.i] = (b - a) / np.where(Lk > 0, Lk, 1.0)[:, None]
            if np.isfinite(seg.z).all():
                z[m, w.i] = np.interp(s, seg.s, seg.z)
    if z_fn is not None:
        z = np.asarray(z_fn(xy.reshape(-1, 2)), np.float64).reshape(n_t, n)
    t = np.arange(n_t, dtype=np.int64) * TICK_MS
    return VehicleSim([w.id for w in vs], [w.spec for w in vs], t, U_rec, V_rec, S_rec, F_rec, xy, hd * V_rec[:, :, None], z, hd, log)


# ---------------------------------------------------------------- 背景车流


def _u01(seed: int, idx: int, g: int, ch: int) -> float:
    return float(cbrng.uniform_elem(seed, STREAM, idx, g, ch))


def background(g: LaneGraph, count: int, seed: int, *, idx0: int = 0, depart_window_ms: int = 60_000, min_route_m: float = 80.0,
               drive: DriveParams | None = None, id_prefix: str = "bgv", end: str = "despawn", net: Network | None = None) -> list[VehicleSpec]:
    """背景车流（流 19 `vehicle` 通道，键 `(seed, 19, idx, g, ch)`，g 为重抽次数，最多 64 次）：出发时刻在
    [0, depart_window_ms) 内按 100 ms 取整（通道 24）；起点为可路由车道（通道 25）上避开路口单元延伸与 `SPAWN_CLEAR_M` 的位置
    （通道 26，毫米取整）；终点为可路由车道的终点（通道 27）；路线为 `lane_route` 最短路，长度 ≥ `min_route_m`。"""
    net = net or Network(g)
    L = VEH_SIZE_M[0]
    lanes = [k for k in g.routable() if g.segs[k].kind == "lane"]
    rng = {}
    for k in lanes:
        lo = max(L + 1.0, net.head[k] + L + 1.0)
        hi = g.segs[k].length - net.tail[k] - SPAWN_CLEAR_M - 1.0
        if hi > lo:
            rng[k] = (lo, hi)
    starts = sorted(rng)
    out: list[VehicleSpec] = []
    if not starts:
        return out
    drive = drive or DriveParams()
    for n in range(count):
        idx = idx0 + n
        for gg in range(64):
            t = math.floor(_u01(seed, idx, gg, 24) * (depart_window_ms // TICK_MS)) * TICK_MS
            o = starts[min(int(_u01(seed, idx, gg, 25) * len(starts)), len(starts) - 1)]
            lo, hi = rng[o]
            s = math.floor((lo + _u01(seed, idx, gg, 26) * (hi - lo)) * 1000.0 + 0.5) / 1000.0
            dst = lanes[min(int(_u01(seed, idx, gg, 27) * len(lanes)), len(lanes) - 1)]
            r = g.lane_route(o, dst)
            if r is None or len(r.segments) < 2:
                continue
            r = _with_start(g, r, s)
            if r.length_m >= min_route_m:
                out.append(VehicleSpec(f"{id_prefix}{idx}", r, t, drive, (), end))
                break
    return out


# ---------------------------------------------------------------- 磁带复算：路线匹配


def match_route(g: LaneGraph, r: LaneRoute, xy: np.ndarray, visible: np.ndarray, *, back_m: float = 1.0,
                fwd_m: float = 8.0) -> tuple[np.ndarray, np.ndarray]:
    """磁带位置沿路线折线单调投影：返回 (u (n_t,)，横向偏差 (n_t,))；不可见帧为 NaN。首个可见帧在全路线上取最近投影
    （相同距离取较小 u），其后在 [u_prev − back_m, u_prev + fwd_m] 的窗口内投影。"""
    U = [-r.s_start]
    for k in r.segments[:-1]:
        U.append(U[-1] + g.segs[k].length)
    P = np.concatenate([g.segs[k].xy for k in r.segments])
    Q = np.concatenate([U[j] + g.segs[k].s for j, k in enumerate(r.segments)])
    A, B, qa, qb = P[:-1], P[1:], Q[:-1], Q[1:]
    ok = qb - qa > 1e-9
    A, B, qa, qb = A[ok], B[ok], qa[ok], qb[ok]
    AB = B - A
    den = AB[:, 0] * AB[:, 0] + AB[:, 1] * AB[:, 1]
    n_t = len(xy)
    u = np.full(n_t, np.nan)
    lat = np.full(n_t, np.nan)
    prev = None
    for k in range(n_t):
        if not visible[k]:
            continue
        if prev is None:
            sel = np.arange(len(A))
        else:
            sel = np.flatnonzero((qb >= prev - back_m) & (qa <= prev + fwd_m))
            if not len(sel):
                sel = np.arange(len(A))
        p = xy[k]
        a, ab = A[sel], AB[sel]
        tt = np.clip(((p[0] - a[:, 0]) * ab[:, 0] + (p[1] - a[:, 1]) * ab[:, 1]) / den[sel], 0.0, 1.0)
        dx, dy = p[0] - (a[:, 0] + ab[:, 0] * tt), p[1] - (a[:, 1] + ab[:, 1] * tt)
        d = np.sqrt(dx * dx + dy * dy)
        uu = qa[sel] + (qb[sel] - qa[sel]) * tt
        if prev is not None:
            d = np.where(uu >= prev - back_m, d, np.inf)
        best = int(np.lexsort((uu, np.round(d, 6)))[0])
        u[k], lat[k] = uu[best], d[best]
        prev = float(uu[best])
    return u, lat


@dataclass
class _Matched:
    paths: list[_Path]
    u: np.ndarray                              # (n_t, n)
    lat: np.ndarray
    vis: np.ndarray
    v: np.ndarray                              # 磁带速度：前向差分（最后一帧取后向差分）
    L: list[float]


def _match_all(g: LaneGraph, net: Network, routes: Sequence[LaneRoute], xy: np.ndarray, state: np.ndarray, lengths: Sequence[float] | None,
               tick_ms: int) -> _Matched:
    n_t, n = state.shape
    vis = np.isin(state, (STATE_MOVING, STATE_WAITING))
    u = np.full((n_t, n), np.nan)
    lat = np.full((n_t, n), np.nan)
    for i, r in enumerate(routes):
        u[:, i], lat[:, i] = match_route(g, r, xy[:, i], vis[:, i])
    v = np.full((n_t, n), np.nan)
    if n_t >= 2:
        d = (u[1:] - u[:-1]) / (tick_ms / 1000.0)
        v[:-1], v[-1] = d, d[-1]
    return _Matched([_Path(g, r, net) for r in routes], u, lat, vis, v, [float(x) for x in (lengths or [VEH_SIZE_M[0]] * n)])


# ---------------------------------------------------------------- 磁带复算：验证


def audit(g: LaneGraph, routes: Sequence[LaneRoute], tape: Mapping[str, np.ndarray], *, log: Mapping[str, Any] | None = None,
          ids: Sequence[str] | None = None, lengths: Sequence[float] | None = None, widths: Sequence[float] | None = None,
          net: Network | None = None, tick_ms: int = TICK_MS, params: Mapping[str, Any] | None = None) -> dict:
    """D3-AC-33 运动条款的复算（见模块说明）。`tape` 为磁带量 `{xy (n_t, n, 2) m, state (n_t, n)}`，`routes` 为各车路线（由
    DSL 与车道图确定性展开）；`log` 为生成日志（准入与到达），缺省时跳过依赖日志的路口判据（`stats.order_checked = false`）。
    返回 `{ok, stats, counts, violations, params}`。"""
    p = {**AUDIT_DEFAULTS, **dict(params or {})}
    net = net or Network(g)
    xy, state = np.asarray(tape["xy"], np.float64), np.asarray(tape["state"])
    n_t, n = state.shape
    ids = list(ids or [f"v{i}" for i in range(n)])
    Wd = [float(x) for x in (widths or [VEH_SIZE_M[1]] * n)]
    m = _match_all(g, net, routes, xy, state, lengths, tick_ms)
    viol: dict[str, list] = {k: [] for k in ("off_lane", "corridor", "gap", "overlap", "conflict", "uncommitted", "order", "major_blocked",
                                             "speed")}
    # 1. 沿车道图运动与道路走廊
    lat_max = float(np.nanmax(m.lat)) if m.vis.any() else 0.0
    for i in range(n):
        bad = np.flatnonzero(m.vis[:, i] & (m.lat[:, i] > p["on_lane_tol_m"]))
        if len(bad):
            viol["off_lane"].append({"actor": ids[i], "t_ms": int(bad[0]) * tick_ms, "lat_m": round(float(m.lat[bad[0], i]), 4),
                                     "n": len(bad)})
    pts = xy[m.vis]
    ec, ek = net.corridor().excess(pts) if len(pts) else (np.zeros(0), np.zeros(0))
    seg_ok = all(g.segs[k].props.get("in_corridor", True) for r in routes for k in r.segments)
    if (ec > 0).any() or not seg_ok:
        viol["corridor"].append({"positions_outside": int((ec > 0).sum()), "max_excess_m": round(float(max(0.0, ec.max(initial=0.0))), 4),
                                 "route_segments_in_corridor": seg_ok})
    # 2. 车距、无穿越与单元占用
    min_gap, min_pair = math.inf, None
    occ_unit: dict[tuple[str, int], set[int]] = {}
    overlaps = 0
    for k in range(n_t):
        here = [i for i in range(n) if m.vis[k, i] and np.isfinite(m.u[k, i])]
        occ: dict[str, list[tuple[float, float, int]]] = {}
        boxes = {}
        for i in here:
            u = float(m.u[k, i])
            P = m.paths[i]
            for sid, lo, hi, _j in P.pieces(u, m.L[i]):
                occ.setdefault(sid, []).append((lo, hi, i))
            for _j, c, u_in, u_out in P.units:
                if min(u, u_out) - max(u - m.L[i], u_in) > p["gap_tol_m"]:
                    occ_unit.setdefault((c, k), set()).add(i)
            boxes[i] = _box(P.point(u), P.point(u - m.L[i]), m.L[i], Wd[i], 0.0)
        for i in here:
            u = float(m.u[k, i])
            P = m.paths[i]
            kf = P.idx(u)
            s_f = u - P.U[kf]
            best, who = math.inf, -1
            j = kf
            while j < len(P.route) and P.U[j] - u < p["gap_scan_m"] and best == math.inf:
                sid = P.route[j]
                cands = list(occ.get(sid, ()))
                cands += [(lo, hi, o) for c2, div in net.siblings.get(sid, ()) for lo, hi, o in occ.get(c2, ()) if lo < div]
                for lo, hi, o in cands:
                    if o != i and not (j == kf and hi <= s_f + 1e-9) and P.U[j] + lo - u < best:
                        best, who = P.U[j] + lo - u, o
                j += 1
            if best < min_gap:
                min_gap, min_pair = best, [ids[i], ids[who], k * tick_ms]
            if best < MIN_GAP_M - p["gap_tol_m"]:
                viol["gap"].append({"t_ms": k * tick_ms, "follower": ids[i], "leader": ids[who], "gap_m": round(best, 4)})
        if len(here) >= 2:
            C = xy[k, here]
            near = np.triu((np.abs(C[:, None, 0] - C[None, :, 0]) < 12.0) & (np.abs(C[:, None, 1] - C[None, :, 1]) < 12.0), 1)
            for a, b in zip(*np.nonzero(near), strict=True):
                ia, ib = here[int(a)], here[int(b)]
                if _hit(boxes[ia], boxes[ib]):
                    overlaps += 1
                    if len(viol["overlap"]) < 50:
                        viol["overlap"].append({"t_ms": k * tick_ms, "a": ids[ia], "b": ids[ib]})
    for (c, k), who in sorted(occ_unit.items()):
        for c2 in sorted(net.conflicts[c]):
            if c2 > c:
                pairs = sorted((ids[a], ids[b]) for a in sorted(who) for b in sorted(occ_unit.get((c2, k), set())) if a != b)
                if pairs:
                    viol["conflict"].append({"t_ms": k * tick_ms, "unit": c, "other": c2, "pairs": pairs})
    # 3. 驶入需要准入的单元：日志准入与让行
    traversals = []
    for i in range(n):
        for j, c, u_in, u_out in m.paths[i].units:
            ks = np.flatnonzero(m.vis[:, i] & (m.u[:, i] > u_in + p["gap_tol_m"]) & (m.u[:, i] - m.L[i] < u_out))
            if len(ks):
                traversals.append((int(ks[0]), i, j, c))
    commits = {(e["actor"], e["j"]): e for e in (log or {}).get("commits", [])}
    arrivals = sorted((e["t_ms"], e["actor"], e["conn"], e["j"]) for e in (log or {}).get("arrivals", []))
    pos = {a: i for i, a in enumerate(ids)}

    def higher(i: int, c: str, k: int):
        """第 k 帧驶向 c 的高级冲突单元（连接段尚未驶入）的车辆：(车辆, 路线下标, 冲突单元, 距单元起点)。"""
        for o in range(n):
            if o == i or not m.vis[k, o]:
                continue
            uo = float(m.u[k, o])
            for j2, c2, u_in2, _uo2 in m.paths[o].units:
                if c2 in net.conflicts[c] and net.cls[c2] > net.cls[c] and m.paths[o].U[j2] >= uo - 1e-6:
                    d = max(0.0, u_in2 - uo)
                    if d <= ETA_SCAN_M:
                        yield o, j2, c2, d

    for k_in, i, j, c in sorted(traversals):
        for o, _j2, c2, d in higher(i, c, k_in):              # 不依赖日志：高级车辆停在冲突单元起点前等待时次路车驶入
            if d <= p["wait_m"] and abs(float(np.nan_to_num(m.v[k_in, o]))) <= p["v_wait_mps"]:
                viol["major_blocked"].append({"t_ms": k_in * tick_ms, "entering": ids[i], "unit": c, "waiting": ids[o], "other": c2})
        if log is None:
            continue
        e = commits.get((ids[i], j))
        if e is None or e["t_ms"] > k_in * tick_ms:
            viol["uncommitted"].append({"t_ms": k_in * tick_ms, "actor": ids[i], "unit": c})
            continue
        kc = e["t_ms"] // tick_ms
        for c2 in sorted(net.conflicts[c]):
            busy = sorted(ids[o] for o in occ_unit.get((c2, kc), set()) if o != i)
            if busy:
                viol["order"].append({"t_ms": e["t_ms"], "actor": ids[i], "unit": c, "why": "occupied", "other": c2, "by": busy})
        for ta, who, c2, j2 in arrivals:
            if ta > e["t_ms"] or who == ids[i] or c2 not in net.conflicts[c] or net.cls[c2] < net.cls[c]:
                continue
            o = pos[who]
            e2 = commits.get((who, j2))
            if e2 is not None and e2["t_ms"] <= e["t_ms"]:
                continue
            if (net.cls[c2] > net.cls[c] or (ta, o) < (e["arr_ms"], i)) and m.vis[kc, o] and m.u[kc, o] <= m.paths[o].U[j2] + 1e-6:
                viol["order"].append({"t_ms": e["t_ms"], "actor": ids[i], "unit": c, "why": "precedence", "other": c2, "by": [who]})
        for o, j2, c2, d in higher(i, c, kc):
            e2 = commits.get((ids[o], j2))
            if e2 is not None and e2["t_ms"] <= e["t_ms"]:
                continue
            eta = d / max(abs(float(np.nan_to_num(m.v[kc, o]))), V_ETA_MIN)
            if eta <= GAP_S - p["eta_tol_s"]:
                viol["order"].append({"t_ms": e["t_ms"], "actor": ids[i], "unit": c, "why": "gap", "other": c2, "by": [ids[o]],
                                      "eta_s": round(eta, 3)})
    # 4. 弯道限速
    vmax_ratio = 0.0
    for i in range(n):
        P = m.paths[i]
        for j, c in enumerate(P.route):
            if c not in net.curve:
                continue
            on = m.vis[:, i] & (m.u[:, i] > P.U[j]) & (m.u[:, i] <= P.U[j] + P.lens[j]) & np.isfinite(m.v[:, i])
            if on.any():
                vm = float(np.max(m.v[on, i]))
                vmax_ratio = max(vmax_ratio, vm / net.curve[c])
                if vm > net.curve[c] + p["speed_tol_mps"]:
                    viol["speed"].append({"actor": ids[i], "conn": c, "v_mps": round(vm, 3), "limit_mps": round(net.curve[c], 3)})
    stats = {"vehicles": n, "ticks": n_t, "visible_samples": int(m.vis.sum()), "lat_max_m": round(lat_max, 6),
             "corridor_samples_frac": round(float((ec <= 0).mean()), 6) if len(ec) else 1.0,
             "carriageway_samples_frac": round(float((ek <= 0).mean()), 6) if len(ek) else 1.0,
             "max_corridor_excess_m": round(float(max(0.0, ec.max(initial=0.0))), 4),
             "min_gap_m": None if min_gap == math.inf else round(min_gap, 4), "min_gap_at": min_pair, "overlaps": overlaps,
             "junction_traversals": len(traversals), "curve_speed_ratio_max": round(vmax_ratio, 4), "order_checked": log is not None}
    counts = {k: len(v) for k, v in viol.items()}
    return {"ok": not any(counts.values()), "stats": stats, "counts": counts, "violations": {k: v[:50] for k, v in viol.items()}, "params": p}


# ---------------------------------------------------------------- 磁带复算：关键事件


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    mk = np.asarray(mask, bool)
    if not mk.any():
        return []
    d = np.diff(np.concatenate([[0], mk.view(np.int8), [0]]))
    return [(int(a), int(b) - 1) for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1), strict=True)]


def key_events(g: LaneGraph, routes: Sequence[LaneRoute], tape: Mapping[str, np.ndarray], *, ids: Sequence[str] | None = None,
               lengths: Sequence[float] | None = None, net: Network | None = None, tick_ms: int = TICK_MS,
               params: Mapping[str, Any] | None = None) -> list[dict]:
    """`vehicle_track` 的车辆关键事件（见模块说明）：`[{type, actor, interval_ms, at_xy, params}]`，按 (起始时刻, 类型, 车辆) 排序。
    `tape` 为 `{xy, state, flags}`（无 flags 时视为发动机一直开启，此时不产出 `park`）。"""
    p = {**EVENT_DEFAULTS, **dict(params or {})}
    net = net or Network(g)
    xy, state = np.asarray(tape["xy"], np.float64), np.asarray(tape["state"])
    flags = np.asarray(tape["flags"]) if tape.get("flags") is not None else np.full(state.shape, FLAG_ENGINE, np.uint8)
    n_t, n = state.shape
    ids = list(ids or [f"v{i}" for i in range(n)])
    m = _match_all(g, net, routes, xy, state, lengths, tick_ms)
    out: list[dict] = []

    def at(k: int, i: int) -> list[float]:
        return [round(float(xy[k, i, 0]), 3), round(float(xy[k, i, 1]), 3)]

    for i in range(n):
        vis = m.vis[:, i]
        if not vis.any():
            continue
        P = m.paths[i]
        uu = m.u[:, i]
        same = np.ones(n_t, bool)                              # 到下一帧位置不变（下一帧不可见或最后一帧视为不变）
        same[:-1] = (xy[1:, i] == xy[:-1, i]).all(-1) | ~vis[1:]
        km = np.flatnonzero(vis & ~same)
        if len(km):
            k0 = int(km[0])
            out.append({"type": "depart", "actor": ids[i], "interval_ms": [k0 * tick_ms, k0 * tick_ms], "at_xy": at(k0, i), "params": {}})
        for j, c in enumerate(P.route):
            seg = g.segs[c]
            if seg.kind != "connector" or seg.props.get("turn") not in p["turns"] or net.junction_of.get(c) is None:
                continue
            ins = np.flatnonzero(vis & (uu > P.U[j] + 1e-6))
            outs = np.flatnonzero(vis & (uu >= P.U[j] + P.lens[j] - 1e-6))
            if not len(ins) or not len(outs) or outs[0] < ins[0]:
                continue
            k0, k1 = int(ins[0]), int(outs[0])
            h0, h1 = seg.xy[1] - seg.xy[0], seg.xy[-1] - seg.xy[-2]
            dh = math.degrees(math.atan2(float(h0[0] * h1[1] - h0[1] * h1[0]), float(h0[0] * h1[0] + h0[1] * h1[1])))
            out.append({"type": "turn", "actor": ids[i], "interval_ms": [k0 * tick_ms, k1 * tick_ms], "at_xy": at((k0 + k1) // 2, i),
                        "params": {"intersection": net.junction_of[c], "turn": seg.props["turn"], "connector": c,
                                   "from_lane": seg.props["from_lane"], "to_lane": seg.props["to_lane"], "heading_change_deg": round(dh, 1)}})
        still = vis & (state[:, i] == STATE_WAITING) & same
        eng = (flags[:, i] & FLAG_ENGINE) > 0
        for k0, k1 in _runs(still):
            u0 = float(uu[k0])
            lane = P.route[P.idx(u0)]
            off = np.flatnonzero(~eng[k0:k1 + 1])
            to_end = k1 == n_t - 1 or not vis[k1 + 1:].any()
            if len(off) and int(off[0]) * tick_ms <= p["park_flag_ms"] and not eng[k0 + int(off[0]):k1 + 1].any() and to_end:
                out.append({"type": "park", "actor": ids[i], "interval_ms": [k0 * tick_ms, k0 * tick_ms], "at_xy": at(k0, i),
                            "params": {"lane": lane}})
                continue
            if (k1 - k0 + 1) * tick_ms < p["stop_min_ms"]:
                continue
            cause = "dwell"
            nxt = next((ui for j, _c, ui, _uo in P.units if P.U[j] >= u0 - 1e-6), None)
            if nxt is not None and nxt - u0 <= p["junction_m"]:
                cause = "junction"
            else:
                F = P.point(u0)
                for o in range(n):
                    if o != i and m.vis[k0, o]:
                        R = m.paths[o].point(float(m.u[k0, o]) - m.L[o])
                        if math.hypot(R[0] - F[0], R[1] - F[1]) <= p["queue_m"]:
                            cause = "queue"
                            break
            out.append({"type": "stop", "actor": ids[i], "interval_ms": [k0 * tick_ms, k1 * tick_ms], "at_xy": at(k0, i),
                        "params": {"cause": cause, "lane": lane}})
    out.sort(key=lambda e: (e["interval_ms"][0], e["type"], e["actor"]))
    return out
