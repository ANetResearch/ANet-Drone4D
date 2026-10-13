"""M19 感知运行时：可见性管线、等级与捕获迟滞、非成像传感器、航迹、云台引导、事件与数据产品（M19-FR-011 至 FR-020、
FR-025 至 FR-028；AWR-04 §6.3、§6.4、§11.7；M18-M19 PRD §6.8–§6.12、§7.1、§7.3、§7.5）。

stage `perception`（order 175；d1_250 每 2 tick、sandbox100 每 tick，M08 按档调度表）在一个 stage 内按 tick 轮转分片：
片数 = stage 频率 / 5 Hz（sandbox100 20 片、d1_250 25 片），片 k 处理 `slot % 片数 == k` 的在场机体，即每机 5 Hz。逐机：

1. 成像传感器（EO、NIR、LWIR，含 D1 相机与热像）：距离与视锥门（当前云台指向与变焦，M13 `sensor_world_pose`）→ 像素门
   （N ≥ 3.5）→ 环境（M07 `optical_depth` 按波长、照度、热背景）得到视线前的上界 n_max → n_max ≥ 3.5 的对经共享 `LosCache`
   请求三条射线（按 target_no 升序，起点按服务数轮转）→ N_eff、等级与捕获迟滞（state.py）→ 事件、检测环、航迹量测、帧；
2. 雷达（中心射线，Swerling 计数器 RNG，P1 的 R 级驻留与地杂波门限）、MID-360（点数判据）；D 级检测给航迹量测并引导云台；
3. 声阵列 2 Hz（全部机体，不发射新射线：遮挡只读缓存中的中心射线）；
4. 航迹 5 Hz 推进；识别物块的 M19 列（best_level、captured、best_sensor、capture_uav_no）在状态变化时重写。

确定性：处理顺序按 slot、target_no 升序；量测噪声取计数器 RNG 流 18 `perception`，键 `(seed, 18, agent_no, tick, ch)`，
`ch = ((sensor_no << 16 | target_no & 0xFFFF) << 3) + j`（PRD 的 `64·sensor_no + target_no` 在 target_no ≥ 64 时会与下一传感器
重叠，改为移位拼接；j 为分量序号）。对状态以 (agent_no, 传感器名, target_no) 为键（target_no 不复用），不进 checkpoint。

成本（D2-FX-R2-perception；D2-AC-13：12 × 30 ≤ 25 ms/仿真秒、单 tick ≤ 1 ms）：一次（机体，成像传感器）评估只进入 numba 三次
（`kernels.py`：`fov_gate` 视锥门；`eval_a` 判据、视线请求次序与共享视线缓存查表；`eval_b` 射线结果回写、等级、量测、对状态
推进与单一来源时的航迹更新），其间只有 M07 的光学厚度（核内取透过率）与射线投射回到 Python；评估输出数组按（机体，传感器）双缓冲；对状态存于数组表（pairs.py，`PairView` 为属性
视图），只有新对、状态转移（事件与检测环）、汇总标记与删除回到 Python；帧在被读取时才展开（`frame_for`，兴趣下推与 REST 才
需要）；识别物汇总为一次核遍历；航迹为逐轴闭式 Kalman（tracks.py）；空片调用（无机体、声阵列与航迹维护要做）不绑定、不取快照。numba 不可用或 `AWR_KERNEL=numpy` 时走等价的 numpy 判据
（`_eval_np`）与 Python 状态机（`state.step`），`tests/perception/test_kernels.py` 两条路径对拍。
"""

from __future__ import annotations

import array
import contextlib
import math
import operator
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from awr.contracts.rng_streams import Stream
from awr.sim.targets.geometry import critical_dim

from . import kernels as K
from . import nonimaging as NI
from . import pairs as PR
from . import tracks as TR
from .johnson import level_of, level_req_of, n_req_for, ttpf
from .level import _C, _LI, _LW, _TRACK, PairArrays, _k_passive, angles, evaluate
from .optics import p_prime_um, sigma_per_m
from .pairs import PairView
from .params import (
    ACOUSTIC_PERIOD_NS,
    CUE_HOLD_S,
    DET_RING,
    EVENTS_PER_S,
    LEVEL_NAMES,
    LEVELS,
    LIMITING,
    N50_PX,
    N_P90,
    Q_ACCEL,
    SHARD_HZ,
    TARGETS_PER_SENSOR,
    TRACK_CONFIRM_N,
)
from .specs import AcousticSpec, EnvSample, LidarSpec, RadarSpec, SensorSpecP, SensorState, imaging_state_spec
from .state import CAP_NAMES, CAPTURED, HOLDING, PairState, step
from .tracks import TrackTable
from .wire import DetectionRing, Publisher, rnd

__all__ = ["BLOCK", "FIELDS", "OWNER", "CaptureState", "Imaging", "NonImaging", "PerceptionRuntime", "Platform", "TargetSnap"]

BLOCK = "perception"
OWNER = "M19"
FIELDS: dict[str, tuple[str, tuple[int, ...]]] = {"tgt_best_level": ("u1", ()), "tgt_captured": ("u1", ()), "tgt_best_sensor": ("u1", ()),
                                                  "tgt_capture_uav_no": ("<u2", ())}
SENSOR_CODE = {"eo": 1, "nir": 2, "lwir": 3, "radar": 4, "acoustic": 5, "lidar": 6}
SENSOR_OF_CODE = {v: k for k, v in SENSOR_CODE.items()}
K_LOS_MIN = float(LEVELS["capture"]["k_los_min"])
_T_HOLD_S = float(LEVELS["capture"]["t_hold_s"])
# 对表列号（`_tr_rows` 内按全局名取）
_F_GSD = K.F_GSD
_F_NEFF = K.F_NEFF
_F_NREQ = K.F_NREQ
_F_P = K.F_P
_F_SIG = K.F_SIG
_F_Z0 = K.F_Z0
_F_Z2 = K.F_Z2
_I_HASLAST = K.I_HASLAST
_I_HASMEAS = K.I_HASMEAS
_I_KIND = K.I_KIND
_I_LEVEL = K.I_LEVEL
_I_LIM = K.I_LIM
_I_LREQ = K.I_LREQ
_KIND_NAMES = K.KIND_NAMES
_LREQ_NAMES = K.LREQ_NAMES
N_D = N_P90["D"]
POINT_ERR_RAD = 1e-3       # 云台角度与姿态合成误差 1 mrad（PRD §6.12）
LIDAR_SIGMA_M = 0.05
S_PC = int(Stream.PERCEPTION)


def _ch(sensor_no: int, target_no: int, j: int = 0) -> int:
    return (((int(sensor_no) & 0xFFFF) << 16 | (int(target_no) & 0xFFFF)) << 3) + int(j)


def _hash32(x: int) -> int:
    from awr.sim.sensors import cbrng

    return int(cbrng.sm(cbrng.u64(int(x)))) & 0xFFFFFFFF


_LREQ: dict[float, str] = {}
_KIND_CODE = {"eo": 0, "nir": 1, "lwir": 2}
_LAM = {"eo": 550.0, "nir": 850.0, "lwir": 10000.0}
_CAPSET = (CAPTURED, HOLDING)
_C_MIN = float(_C["c_min"])
_C_SPAN = float(_C["c_full"]) - float(_C["c_min"])
_HALF_DEG = math.radians(0.5)
_NO_VIS = np.zeros(0, np.bool_)
_NO_ACT = np.zeros(0, np.int64)
_MISS = object()
_KLOS3 = tuple(bin(mk).count("1") / 3 for mk in range(8))   # 三条射线时按可见位掩码的 k_los（与 LosCache 同式 `_POP[m] / 3`）


def _lreq(n_req: float) -> str:
    """`level_req_of` 的记忆版（N_req 只取少数几个值）。"""
    v = _LREQ.get(n_req)
    if v is None:
        v = level_req_of(n_req)
        if len(_LREQ) < 4096 and n_req == n_req:
            _LREQ[n_req] = v
    return v


_SKY_F = operator.attrgetter("illum_lx", "mor_m", "rho_v_gm3", "k_cross", "t_bg_c", "eps_bg", "t_sky_c")
_BG_DEFAULT = np.full(24, 40.0 - 10 * math.log10(24))      # 样本缺 l_bg_bands_dba 时的 24 频带背景（只读）
_BG_DEFAULT.setflags(write=False)


def _rnd(x: Any, nd: int) -> float | None:
    """同 `wire.rnd`（None 与非有限值为 None；x − x 对 NaN 与 ±inf 为 NaN），供转移的表行路径调用。"""
    if x is None or x - x != 0.0:
        return None
    return round(float(x), nd)


def _lget(last: Any, k: str) -> Any:
    """对状态 `last`（pairs `_SlotRec`、dict 或空）的字段。"""
    g = getattr(last, "get", None)
    return g(k) if g is not None else None


# ---------------------------------------------------------------- 一次（机体，成像传感器）评估的结果（数组）与按对视图
_CORE_F = {"n": 1, "gsd": 3, "k_c": 4, "k_light": 5, "k_blur": 6}
_RES_F = {"k_los": 0, "n_eff": 1, "p": 2}


class _Eval:
    """视锥内 m 对的评估数组：core[m, kernels.CORE_COLS]、res[m, (k_los, n_eff, p, sigma)]、codes[m, (lim, level, cond, has_meas)]、
    z[m, 3]（量测）、n_req[m]、dfr[m]（视线延期）。两条路径（numba 核、numpy）产出同一形状。"""

    __slots__ = ("_nr", "_nr_full", "cand", "codes", "core", "dfr", "kind", "m", "n_meas", "pst", "res", "z")

    @property
    def n_req(self) -> NDArray:
        """各对的 N_req（按 j）。"""
        if self._nr_full:
            self._nr = self._nr[self.cand]
            self._nr_full = False
        return self._nr

    def __init__(self, kind: str, cand: NDArray, core: NDArray, res: NDArray, codes: NDArray, z: NDArray, n_req: NDArray, dfr: NDArray,
                 n_meas: int, pst: NDArray | None = None, *, full_nreq: bool = False) -> None:
        self.kind, self.cand, self.m = kind, cand, int(cand.size)
        self.core, self.res, self.codes, self.z, self.dfr, self.n_meas = core, res, codes, z, dfr, int(n_meas)
        self._nr = n_req
        self._nr_full = full_nreq                        # n_req 为按识别物下标的整表（取 [cand]）
        self.pst = pst                                   # 推进后对的 (等级, 捕获态, hold_s)（numba 路径）

    def field(self, j: int, k: str, default: Any = None) -> Any:
        c = _CORE_F.get(k)
        if c is not None:
            return float(self.core[j, c])
        c = _RES_F.get(k)
        if c is not None:
            return float(self.res[j, c])
        if k == "n_req":
            return float(self._nr[self.cand[j]] if self._nr_full else self._nr[j])
        if k == "lim":
            c = int(self.codes[j, 0])
            return None if c < 0 else LIMITING[c]
        if k == "meas":
            return self.z[j] if self.codes[j, 3] else None
        if k == "sigma":
            return float(self.res[j, 3]) if self.codes[j, 3] else None
        if k == "dt_app":
            return float(self.core[j, 7]) if self.kind == "lwir" else None
        if k == "kind":
            return self.kind
        if k == "level":
            return int(self.codes[j, 1])
        if k == "cond":
            return bool(self.codes[j, 2])
        if k == "deferred":
            return bool(self.dfr[j])
        if k == "range_m":
            return float(self.core[j, 0])
        return default


class _SensorFrame:
    """一个成像传感器的帧段（`uav/{id}/perception` 的 sensors[k]）：评估时只记下标量与按斜距的前 8 对，读取时展开。"""

    __slots__ = ("ev", "head", "items", "n_in_fov", "snap")

    def __init__(self, head: tuple, n_in_fov: int, items: list | None, ev: _Eval | None, snap: Any) -> None:
        self.head, self.n_in_fov, self.items, self.ev, self.snap = head, n_in_fov, items, ev, snap

    def _items(self) -> list:
        """视锥内按斜距升序的前 8 对：(j, 识别物下标, 对等级, hold_s, captured, 捕获态名)，取自评估后的对状态 `ev.pst`。"""
        ev = self.ev
        if self.items is not None or ev is None:
            return self.items or []
        js = np.argsort(ev.core[:, 0], kind="stable")[:TARGETS_PER_SENSOR].tolist() if ev.m > 1 else list(range(ev.m))
        cand_l = ev.cand.tolist()
        out = []
        for j, (lv, c, h) in zip(js, ev.pst[js].tolist(), strict=True):
            c = int(c)
            out.append((j, cand_l[j], int(lv), h, c in _CAPSET, CAP_NAMES[c]))
        self.items = out
        return out

    def build(self) -> tuple[dict, dict | None]:
        name, kind, mode, f_mm, hfov, vfov, illum = self.head
        fr = {"sensor": name, "kind": kind, "mode": mode, "f_mm": rnd(f_mm, 2), "hfov_rad": rnd(hfov, 5), "vfov_rad": rnd(vfov, 5),
              "n_in_fov": int(self.n_in_fov), "targets": []}
        if illum:
            fr["illum"] = illum
        hud = None
        ev, snap = self.ev, self.snap
        for j, i, lv, hold, capd, capn in self._items():
            g = ev.field
            n_req = g(j, "n_req")
            lim = g(j, "lim")
            item = {"target": snap.ids[i], "cls_est": snap.cls[i] if lv >= 2 else "unknown", "level": LEVEL_NAMES[lv], "p": rnd(g(j, "p"), 3),
                    "n": rnd(g(j, "n"), 2), "n_eff": rnd(g(j, "n_eff"), 2), "n_req": rnd(n_req, 2), "level_req": _lreq(n_req),
                    "range_m": rnd(g(j, "range_m"), 1), "gsd_m": rnd(g(j, "gsd"), 4), "limiting": lim,
                    "k": {"los": rnd(g(j, "k_los"), 3), "c": rnd(g(j, "k_c"), 3), "light": rnd(g(j, "k_light"), 3), "blur": rnd(g(j, "k_blur"), 3),
                          "occ": rnd(snap.occ[i], 3)},
                    "hold_s": rnd(hold, 2), "captured": capd, "capture_state": capn}
            dt = g(j, "dt_app")
            if dt is not None:
                item["delta_t_app_k"] = rnd(dt, 2)
            z = g(j, "meas")
            if z is not None:
                item["meas_enu_m"] = [rnd(x, 2) for x in z]
                item["sigma_m"] = rnd(g(j, "sigma"), 3)
            fr["targets"].append(item)
            if hud is None:
                hud = {"sensor": name, "target": snap.ids[i], "n_eff": rnd(g(j, "n_eff"), 1), "n_req": rnd(n_req, 1), "limiting": lim}
        return fr, hud


# ---------------------------------------------------------------- 输入
@dataclass
class Imaging:
    name: str
    sensor_no: int
    spec: SensorSpecP
    st: SensorState
    p: NDArray                 # 传感器原点 ENU
    R: NDArray                 # WORLD ← SENSOR（FLU，x 为视轴）
    hfov: float
    vfov: float
    src: str = "default"       # 云台指向源（default、cue、tasking、operator）
    mode: str = "day"          # 生效的成像模式（day、lowlight、nir）
    illum: dict = field(default_factory=dict)   # {builtin, ext}：on | off
    col: int = 0


@dataclass
class NonImaging:
    kind: str                  # radar | acoustic | lidar
    name: str
    sensor_no: int
    spec: Any
    p: NDArray
    R: NDArray


@dataclass
class Platform:
    slot: int
    uav: str
    agent_no: int
    model_id: str
    frame: str                 # multirotor | fixed_wing
    pos: NDArray
    vel: NDArray
    hold_sigma_m: float = 0.02
    lw_bands: NDArray | None = None   # 本机未计权带声功率级（自噪声）
    cue: int = 0               # 主成像列的 cue 设置：0 未设、1 on、2 off
    imaging: list[Imaging] = field(default_factory=list)
    nonimg: list[NonImaging] = field(default_factory=list)


@dataclass
class TargetSnap:
    n: int
    tno: NDArray
    ids: list[str]
    rows: NDArray
    cls: list[str]
    pos: NDArray
    vel: NDArray
    yaw: NDArray
    size: NDArray
    occ: NDArray
    cfgs: tuple
    c0: NDArray
    camo: NDArray
    rho850: NDArray
    rho905: NDArray
    hot: NDArray
    th_abs: NDArray
    dt_rel: NDArray
    t_surf: NDArray
    emis: NDArray
    rcs: NDArray
    swerling1: NDArray
    crit_ov: NDArray            # NaN 为未覆盖
    level_req: list[str]
    n_req_px: list[float | None]
    ground: NDArray
    f0: NDArray                 # 声源基频（无声源 0）
    index: dict[int, int] = field(default_factory=dict)   # target_no -> 下标
    # ---- D2-FX-R2-perception：核输入与按识别物的缺省 N_req
    pk: NDArray | None = None                 # [n, kernels.PK_N] 打包列（位置、速度、航向、尺寸与判据参数）
    pmid: NDArray | None = None               # [n, 3] 识别物中心（脚点 + 0.5·h，路径透过率的终点）
    n_req0: NDArray | None = None             # 识别物自身的 N_req（无任务覆盖）
    n500: NDArray | None = None               # n_req0 对应的 TTPF N50
    id_index: dict[str, int] = field(default_factory=dict)
    tno_l: list[int] = field(default_factory=list)
    skey: Any = None                          # 静态部分的键（识别物 rev 与序号）
    tno_max: int = 0
    q2: NDArray | None = None                 # 航迹过程噪声 q²（按类别）
    tidx: NDArray | None = None               # target_no -> 下标（-1 不在快照）


@dataclass(frozen=True)
class CaptureState:
    captured: bool
    by: tuple[str, ...]
    since_ns: int | None
    t_last_ok_ns: int | None
    level: str = "none"


# ---------------------------------------------------------------- M13 读取
class M13Platforms:
    """从 M13 运行时（`awr.sim.sensors.plugin.runtime()`）读取挂载、成像状态与传感器位姿（d2-WP-10-to-M19 第 1 条）。"""

    def __init__(self) -> None:
        self._sig: dict[str, Any] = {}
        self._non: dict[int, Any] = {}
        self._lwb: dict[str, Any] = {}
        self._pcache: dict[tuple, tuple] = {}   # (slot, profile_id, frame_kind, id(profiles)) -> (profiles, 机型, 机体类, 悬停水平标准差, 声功率)
        self._vcache: dict[tuple, tuple] = {}   # (slot, imaging, kinds) -> 载荷视图的复用条目（`_view_entry`）

    def lw_bands(self, model_id: str, sig: Any) -> NDArray | None:
        """机型的未计权带声功率级（自噪声；按机型缓存，`VehicleSignature.lw_bands` 只取决于机型参数）。"""
        v = self._lwb.get(model_id, _MISS)
        if v is _MISS:
            v = self._lwb[model_id] = np.asarray(sig.lw_bands(), float) if hasattr(sig, "lw_bands") else None
        return v

    @staticmethod
    def runtime() -> Any:
        mod = sys.modules.get("awr.sim.sensors.plugin")
        fn = getattr(mod, "runtime", None)
        try:
            return fn() if callable(fn) else None
        except Exception:
            return None

    def signature(self, model_id: str, profiles: Any) -> Any:
        s = self._sig.get(model_id)
        if s is None:
            from awr.sim.targets.signature import signature_of

            s = self._sig[model_id] = signature_of(model_id, profiles)
        return s

    def _nonimg_spec(self, sp: Any) -> tuple[str, Any] | None:
        hit = self._non.get(id(sp), _MISS)
        if hit is not _MISS:
            return hit
        from awr.sim.sensors.enums import SensorKind

        from .specs import acoustic_from_doc, lidar_from_doc, radar_from_doc

        doc = getattr(sp, "doc", {}) or {}
        sid = str(getattr(sp, "schema_id", "") or doc.get("schema", ""))
        out: tuple[str, Any] | None = None
        try:
            if sid == "awr.sensor.radar_mmw.v1" or sp.kind == SensorKind.RADAR:
                out = ("radar", radar_from_doc(doc, name=sp.name))
            elif sid == "awr.sensor.acoustic_array.v1" or sp.kind == SensorKind.ACOUSTIC:
                out = ("acoustic", acoustic_from_doc(doc, name=sp.name))
            elif sp.kind == SensorKind.LIDAR:
                out = ("lidar", lidar_from_doc(doc, name=sp.name))
        except Exception:
            out = None
        self._non[id(sp)] = out  # type: ignore[assignment]
        return out

    def collect(self, S: Any, ctx: Any, slots: NDArray, *, imaging: bool = True, kinds: tuple[str, ...] = ("radar", "acoustic", "lidar"),
                nonimg_pose: bool = True) -> list[Platform]:
        """nonimg_pose = False（快照中没有识别物）时不计算非成像传感器的世界位姿（雷达、MID-360 与声阵列在没有识别物时不读取位姿；
        p、R 置 None）。

        D2-FX-R2-sim 第 2 次（D2 验收第 1 轮 4.1：沙盒真实组合中感知 stage 67 ms/仿真秒，约 60% 在本函数）：机体位姿一次取出
        （成像与非成像共用），成像传感器位姿走 `SensorRuntime.sensor_world_pose1`（与 `sensor_world_pose` 逐位相同），机型、
        签名与声功率按 (slot, profile_id, frame_kind) 缓存；结果与此前相同。"""
        rt = self.runtime()
        if rt is None or S is None or len(slots) == 0:
            return []
        from awr.sim.sensors.camera import world_pose
        from awr.sim.sensors.frames import quat_to_R

        S.enu.refresh()
        slots = np.asarray(slots, np.int64)
        need_nonimg = bool(kinds) and nonimg_pose
        PQ = S.enu.pose_enu_flu(slots) if (imaging or need_nonimg) else None
        pos_all, vel_all = S.enu.pos, S.enu.vel
        prof = getattr(ctx, "profiles", None)
        swp1 = getattr(rt, "sensor_world_pose1", None)
        pid_a = getattr(S, "profile_id", None)
        fk_a = getattr(S, "frame_kind", None)
        out: list[Platform] = []
        vkf = getattr(rt, "payload_view_key", None)
        for i, s in enumerate(slots.tolist()):
            # 成像状态与非成像传感器表按 M13 的载荷取值键复用（键相同则 payload_view 除云台角外逐字段相同；M19 不读其中的云台角，
            # 位姿每次按当前姿态与云台角重算）
            vk = None
            ent = None
            if vkf is not None:
                try:
                    vk = vkf(s)
                except Exception:
                    vk = None
                if vk is None:
                    continue
                ent = self._vcache.get((s, imaging, kinds))
                if ent is not None and (ent[0] != vk or ent[1] is not rt or ent[2] is not rt.rig_of(s)):
                    ent = None
            if ent is None:
                try:
                    pv = rt.payload_view(s)
                except Exception:
                    pv = None
                if pv is None:
                    continue
                ent = self._view_entry(rt, s, pv, vk, imaging, kinds)
            pv_uav, pv_agent = ent[3], ent[4]
            pidv = int(pid_a[s]) if pid_a is not None else -1
            fk = int(fk_a[s]) if fk_a is not None else 0
            ck = (s, pidv, fk, id(prof))
            cs = self._pcache.get(ck)
            if cs is None or cs[0] is not prof:
                mid = "p600_mid360"
                if prof is not None and pid_a is not None:
                    with contextlib.suppress(Exception):
                        mid = str(prof.ids[pidv])
                sig = self.signature(mid, prof)
                if len(self._pcache) > 1024:
                    self._pcache.clear()
                cs = self._pcache[ck] = (prof, mid, "fixed_wing" if fk == 1 else "multirotor", float(getattr(sig, "hold_sigma_h_m", 0.02)),
                                         self.lw_bands(mid, sig))
            P = Platform(int(s), pv_uav, pv_agent, cs[1], cs[2], np.array(pos_all[s], float), np.array(vel_all[s], float), cs[3], cs[4])
            pb = getattr(rt, "pblk", None)
            pq = PQ[i:i + 1] if PQ is not None else None
            Rb = quat_to_R(pq[:, 3:]) if pq is not None else None   # 同一机体的各传感器共用（与 world_pose 内的同一计算）
            if imaging:
                for (sensor, sensor_no, spec, sst, hf, vf, src, mode, ill, col) in ent[5]:
                    if swp1 is not None:
                        got = swp1(s, sensor, pq, Rb)
                        if got is None:
                            continue
                        p0, R0 = got
                    else:
                        p, R = rt.sensor_world_pose(np.array([s]), sensor)
                        p0, R0 = p[0], R[0]
                    if not (np.isfinite(p0).all() and np.isfinite(R0).all()):
                        continue
                    P.imaging.append(Imaging(sensor, sensor_no, spec, sst, p0, R0, hf, vf, src, mode, dict(ill), col))
                    if pb is not None and col == 0:
                        P.cue = int(pb["cue"][s, col])
            for (kind, name, sensor_no, nspec, sp) in ent[6]:
                if not need_nonimg:
                    P.nonimg.append(NonImaging(kind, name, sensor_no, nspec, None, None))  # type: ignore[arg-type]
                    continue
                p, R = world_pose(pq, sp, Rb=Rb)  # type: ignore[arg-type]
                P.nonimg.append(NonImaging(kind, name, sensor_no, nspec, p[0], R[0]))
            out.append(P)
        return out

    def _view_entry(self, rt: Any, s: int, pv: Any, vk: tuple | None, imaging: bool, kinds: tuple[str, ...]) -> tuple:
        """载荷视图的复用条目：(键, M13 运行时, rig, uav, agent_no, 成像表, 非成像表)；键为 None（M13 未提供取值键）时不入缓存。"""
        rig = rt.rig_of(s)
        img = []
        if imaging:
            for st in pv.imaging:
                if not st.active:
                    continue
                spec, sst = imaging_state_spec(st)
                ill = {}
                for d in st.illuminators or ():
                    ill["builtin" if d.get("kind") == "builtin" else "ext"] = "on" if d.get("on") else "off"
                img.append((st.sensor, int(st.sensor_no), spec, sst, float(st.hfov_rad), float(st.vfov_rad), str(st.src), str(st.mode), ill,
                            int(st.col)))
        non = []
        if rig is not None and kinds:
            off = pv.off
            for sp in rig.specs:
                if sp.name in off:
                    continue
                hit = self._nonimg_spec(sp)
                if hit is None or hit[0] not in kinds:
                    continue
                non.append((hit[0], sp.name, int(sp.sensor_no), hit[1], sp))
        ent = (vk, rt, rig, str(pv.uav), int(pv.agent_no), tuple(img), tuple(non))
        if vk is not None:
            if len(self._vcache) > 4096:
                self._vcache.clear()
            self._vcache[(s, imaging, kinds)] = ent
        return ent


# ---------------------------------------------------------------- 运行时
class PerceptionRuntime:
    def __init__(self) -> None:
        self.tab: Any = None
        self.trt: Any = None
        self.adapter = M13Platforms()
        self.platforms_fn: Any = None                   # 测试钩子：fn(S, ctx, slots, imaging, kinds) -> list[Platform]
        self.ptab = PR.PairTable(256)                    # 对状态的数组表（pairs.py）
        self.pairs: dict[tuple[int, str, int], PairView] = {}
        self._asmap: dict[tuple[int, str], dict[int, int]] = {}   # (agent_no, 传感器名) -> target_no -> 行
        self._info = np.zeros(K.INFO_N, np.int64)
        self._smaps: dict[tuple[int, str], NDArray] = {}      # (agent_no, 传感器名) -> target_no -> 行（核内查找与写入）
        self._asn: dict[int, dict[str, int]] = {}                # agent_no -> 传感器名 -> 该机该传感器的对数
        self._scr: dict[str, NDArray] = {}
        self._scr_n = 0
        self._cell_a, self._cell_b = 5.0, 2.0
        self._lcnt = np.zeros(6, np.int64)
        self.ev_hook: Any = None                         # 测试钩子：fn(P, t_ns, _Eval)
        self.rad: dict[tuple[int, str, int], dict] = {}  # 雷达对状态：track_id、level、r_since、last
        self.lid: dict[tuple[int, str, int], dict] = {}
        self.aco: dict[tuple[int, str, int], dict] = {}
        self.serial: dict[int, int] = {}
        self.tracks = TrackTable()
        self.reqs: dict[tuple[str, str], tuple[str | None, float | None]] = {}
        self.cue_on: dict[str, bool] = {}
        self.cue_t: dict[int, float] = {}
        self.ring = DetectionRing(DET_RING)
        self.frames: dict[str, dict] = {}
        self.radar_last: dict[str, dict] = {}
        self.acoustic_last: dict[str, dict] = {}
        self.lidar_last: dict[str, dict] = {}
        self.pub = Publisher()
        self.agent_uav: dict[int, str] = {}
        self.events_log: deque = deque(maxlen=512)
        self.stats = {"calls": 0, "pairs_eval": 0, "rays": 0, "deferred": 0, "stale": 0, "env_fallback": 0, "events": 0, "events_merged": 0,
                      "cue": 0, "acoustic_runs": 0, "los_requests": 0}
        self._rot: dict[tuple[int, str], int] = {}
        self._nil_cache: tuple | None = None            # `_nonimg_levels` 的缓存
        self._snap: TargetSnap | None = None
        self._snap_key: Any = None
        self._ts: list = []
        self._ts_key: Any = None
        self._static_key: Any = None
        self._static: dict = {}
        self._ev_tokens = float(EVENTS_PER_S)
        self._ev_t: float | None = None
        self._last_ac = -1
        self._last_track_t = -1.0
        self._last_t_ns = 0
        self._dirty = True
        self._seed: int | None = None
        self._rays_hist: deque = deque()
        self._rays_sum = 0                              # _rays_hist 的射线数之和（rays_s）
        self.rays_tick_max = 0
        self.full = (os.environ.get("AWR_PERCEPTION_MODE") or "").strip().lower() == "full"
        self._bound_trt: Any = None
        self._apairs: dict[int, dict[tuple[int, str, int], None]] = {}   # agent_no -> 该机的对键（与 pairs 同一插入次序）
        self._tpairs: dict[int, dict[tuple[int, str, int], None]] = {}   # target_no -> 该识别物的对键（同上）
        self._dirty_all = True                           # 汇总：全部识别物重算（否则只重算 _dirty_t）
        self._dirty_t: set[int] = set()
        self._sum_skey: Any = None
        self._prune_key: Any = None
        self._sum_bufs: Any = None
        self._sp_cache: dict[tuple, tuple] = {}          # 传感器与环境的标量参数（_sensor_params）
        self._obuf: dict = {}                            # (agent_no, 传感器名) -> [奇偶, 缓冲 0, 缓冲 1]（`_obufs`）
        self._sp_last: dict = {}                         # (agent_no, 传感器名) -> (状态, EnvSample, prm, ill)
        self._env_last: Any = None
        self._p0buf = np.empty((0, 3))
        self._nreq_cache: dict[str, tuple] = {}          # uav -> (snap.skey, reqs 版本, N_req[n])
        self._reqs_rev = 0
        self._pfx: tuple[Any, int] = (None, 0)          # 计数器 RNG 前缀键（(seed, agent, tick) -> 前四级）
        self._pfx3: dict[tuple[int, int], int] = {}     # (seed, agent) -> 前三级
        self._k3c: dict[tuple[int, int], np.uint64] = {}
        self._bind_key: Any = None
        self._shard: Any = None                          # (在场机体字节, 周期, n_sh, 按片排序的 slot, 各片的终点)
        self._cad_key: Any = None
        self._cad = (1, 1)
        self._mev: list = []                             # 本机本次处理中有量测的评估（_platform 并入航迹）
        self._env_tick: tuple[Any, Any] = (None, None)
        self._qrm = np.array([K_LOS_MIN - 1e-9, 0.02, POINT_ERR_RAD, N_P90["D"], N_P90["R"], N_P90["I"], float(LEVELS["ttpf"]["e0"]),
                              float(LEVELS["ttpf"]["e1"])])
        from .field import FieldService

        self.field = FieldService(self)             # D2.1：剖面、感知体表与逐对读数（WP-19b）

    # ------------------------------------------------------------ 绑定
    def bind(self, ctx: Any) -> bool:
        ents = getattr(ctx, "entities", None)
        tab = ents.get("target") if ents is not None and hasattr(ents, "get") else None
        if tab is None:
            return False
        if tab is not self.tab:
            self.tab = tab
            self._dirty = self._dirty_all = True
        # M18 运行时的查找与 `trt.bind`（行对齐、M13 接线、机型表）只在实体表、世界或机型表变化时与每 1 s【仿真】做一次：
        # 识别物的增删都使实体表 version + 1，M18 自己的 stage（10 Hz）也会 bind（D2-FX-R2-perception：此前每 tick 约 20 µs）
        w, pf = getattr(ctx, "world", None), getattr(ctx, "profiles", None)
        ver, sec = getattr(tab, "version", 0), int(getattr(ctx, "t_ns", 0)) // 1_000_000_000
        k0 = self._bind_key                            # (实体表, version, 世界, 机型表, 秒)：按身份与取值比较
        if k0 is not None and k0[0] is tab and k0[1] == ver and k0[2] is w and k0[3] is pf and k0[4] == sec and self.trt is not None \
                and self.trt is self._bound_trt:
            return True
        bk = (tab, int(ver), w, pf, sec)
        mod = sys.modules.get("awr.sim.targets.plugin")
        fn = getattr(mod, "runtime", None)
        trt = fn(create=False) if callable(fn) else None
        if trt is not None and trt is not self._bound_trt:
            trt.capture_fn = self.capture_detail
            self._bound_trt = trt
        self.trt = trt
        if trt is not None:
            trt.bind(ctx)
            self._bind_key = bk
            lc = getattr(trt, "los", None)
            self._cell_a = float(getattr(lc, "cell_a", 5.0))
            self._cell_b = float(getattr(lc, "cell_b", 2.0))
        return trt is not None

    @property
    def los(self) -> Any:
        if self.trt is not None:
            return self.trt.los
        from awr.sim.targets import shared_cache

        return shared_cache()

    def seed(self, ctx: Any) -> int:
        if self._seed is None:
            try:
                from awr.sim.sensors.runtime import world_seed_of

                self._seed = int(world_seed_of(ctx))
            except Exception:
                self._seed = 0
        return self._seed

    def reset(self) -> None:
        """剧本重置、恢复快照：清空对状态、航迹、检测环与缓存的帧（M17 会话重建）。"""
        self.pairs.clear()
        self._apairs.clear()
        self._tpairs.clear()
        self._asmap.clear()
        self._asn.clear()
        self._smaps.clear()
        self._obuf.clear()
        self.ptab.clear()
        self.rad.clear()
        self.lid.clear()
        self.aco.clear()
        self.serial.clear()
        self.tracks.clear()
        self.reqs.clear()
        self.cue_t.clear()
        self.ring.clear()
        self.frames.clear()
        self.radar_last.clear()
        self.acoustic_last.clear()
        self.lidar_last.clear()
        self._snap = self._snap_key = None
        self._rot.clear()
        self._nreq_cache.clear()
        self._rays_hist.clear()
        self._rays_sum = 0
        self._last_ac = -1
        self._last_track_t = -1.0
        self._dirty = self._dirty_all = True
        self._prune_key = None
        self.field.reset()

    # ------------------------------------------------------------ 识别物快照
    def _static_arrays(self, Ts: list[Any]) -> dict:
        key = (int(getattr(self.trt, "rev", 0)), tuple(int(T.target_no) for T in Ts))
        if key == self._static_key:
            return self._static
        from .level import cfg_value

        cf = [T.cfg for T in Ts]
        th = [cfg_value(c, "thermal", {}) or {} for c in cf]
        snd = [cfg_value(c, "sound", None) for c in cf]
        ov = [cfg_value(c, "critical_dim_m", None) for c in cf]
        n = len(Ts)
        lreq = [str(cfg_value(c, "level_req", "R") or "R") for c in cf]
        npx = [cfg_value(c, "n_req_px", None) for c in cf]
        self._static = {
            "c0": np.array([float(cfg_value(c, "contrast0", 0.3)) for c in cf]),
            "camo": np.array([float(cfg_value(c, "camouflage", 0.0) or 0.0) for c in cf]),
            "rho850": np.array([float(cfg_value(c, "reflect_850", 0.3)) for c in cf]),
            "rho905": np.array([float(cfg_value(c, "reflect_905", 0.3)) for c in cf]),
            "hot": np.array([float(t.get("hot_fraction", 1.0)) for t in th]),
            "th_abs": np.array([t.get("mode", "relative") == "absolute" for t in th], bool),
            "dt_rel": np.array([float(t.get("delta_t_k", 0.0) or 0.0) for t in th]),
            "t_surf": np.array([float(t.get("t_surface_c", 30.0) or 30.0) for t in th]),
            "emis": np.array([float(t.get("emissivity", 0.95) or 0.95) for t in th]),
            "rcs": np.array([float(cfg_value(c, "rcs_m2", 0.1) or 0.1) for c in cf]),
            "swerling1": np.array([cfg_value(c, "rcs_fluct", "swerling0") == "swerling1" for c in cf], bool),
            "crit_ov": np.array([np.nan if v is None else float(v) for v in ov]),
            "level_req": lreq,
            "n_req_px": npx,
            "ground": np.array([T.cfg.cls != "uav" for T in Ts], bool),
            "f0": np.array([float(s.get("f0_hz", 0.0)) if isinstance(s, dict) else 0.0 for s in snd]),
        }
        # 不随时间变化的部分（同一 rev 内识别物的配置不变）：序号、id、行、类别、尺寸、配置与缺省 N_req
        self._static_fix = {
            "tno": np.array([int(T.target_no) for T in Ts], np.int64), "ids": [T.id for T in Ts], "rows": np.array([int(T.row) for T in Ts], np.int64),
            "cls": [T.cfg.cls for T in Ts], "size": np.array([T.cfg.size_lwh for T in Ts], float).reshape(n, 3), "cfgs": tuple(cf),
            "n_req0": np.array([n_req_for(lreq[i], npx[i]) for i in range(n)], float),
        }
        fx = self._static_fix
        fx["n500"] = np.array([N50_PX[_lreq(float(x))] for x in fx["n_req0"].tolist()], float)
        tl = fx["tno"].tolist()
        fx["tno_l"] = tl
        fx["tno_max"] = max(tl) if tl else 0
        fx["index"] = {int(t): i for i, t in enumerate(tl)}
        fx["id_index"] = {tid: i for i, tid in enumerate(fx["ids"])}
        occ0 = np.array([float(T.cfg.raw.get("occlusion", 0.0)) for T in Ts], float)
        fx["occ0"] = occ0
        fx["occ_hid"] = np.maximum(occ0, 0.8)
        fx["q2"] = np.array([Q_ACCEL.get(c, 1.0) * Q_ACCEL.get(c, 1.0) for c in fx["cls"]], float)
        fx["half_h"] = np.array([0.0, 0.0, 0.5]) * fx["size"][:, 2:3]          # 识别物中心相对脚点（路径透过率终点）
        tidx = np.full(fx["tno_max"] + 1, -1, np.int64)
        if tl:
            tidx[np.array(tl, np.int64)] = np.arange(len(tl), dtype=np.int64)
        fx["tidx"] = tidx
        pk = np.zeros((n, K.PK_N))
        st = self._static
        C = K.PK_COLS
        pk[:, C["size"]:C["size"] + 3] = self._static_fix["size"]
        for nm in ("crit_ov", "hot", "c0", "camo", "rho850", "th_abs", "dt_rel", "t_surf", "emis"):
            pk[:, C[nm]] = st[nm]
        self._static_fix["pk"] = pk
        self._static_key = key
        return self._static

    def targets(self, ctx: Any) -> TargetSnap:
        """识别物快照：M18 的状态可能变化时重建。键为（M18 stage 调用计数、M18 rev、实体表版本与身份、1 s 时间片）：识别物的位置、
        航向、速度与 hidden 只在 M18 stage 内推进（每次推进前调用计数 + 1），增删改与 enabled 切换使 rev + 1，行的增删使实体表 version
        + 1；1 s 时间片为兜底刷新。此前按 100 ms 时间片重建，M18 未推进时重复重建、推进后最多晚一个时间片才看到新位置。

        动态量与 M18 的定义逐项相同：位置 `ms.pos`、航向 `ms.yaw`、速度 `(v·cos ψ, v·sin ψ, v_z)`（`TargetRuntime._vel`）、遮挡
        `max(occlusion, 0.8) if hidden else occlusion`（`TargetRuntime._occlusion`；`tests/perception/test_kernels.py` 对拍）。"""
        trt, tab = self.trt, self.tab
        t_ns = int(getattr(ctx, "t_ns", 0))
        st_ = getattr(trt, "stats", None)
        calls = st_.get("calls", 0) if st_ is not None else t_ns // 100_000_000
        rev, ver = trt.rev, getattr(tab, "version", 0)
        k0 = self._snap_key                            # (秒, M18 调用计数, rev, version, 实体表, M18 运行时)
        if k0 is not None and self._snap is not None and k0[0] == t_ns // 1_000_000_000 and k0[1] == calls and k0[2] == rev and k0[3] == ver \
                and k0[4] is tab and k0[5] is trt:
            return self._snap
        lk = (int(rev), int(ver), id(tab), id(trt))
        key = (t_ns // 1_000_000_000, calls, int(rev), int(ver), tab, trt)
        if self._ts_key != lk:
            self._ts = [T for T in trt.ordered() if bool(T.cfg.raw.get("enabled", True))]
            self._ts_key = lk
            self._static_arrays(self._ts)
        Ts = self._ts
        n = len(Ts)
        st = self._static
        fx = self._static_fix
        pk = fx["pk"].copy()
        C = K.PK_COLS
        if n:
            pk[:, 0:3] = np.concatenate([T.ms.pos for T in Ts]).reshape(n, 3)
            A = np.frombuffer(array.array("d", [x for T in Ts for ms in (T.ms,) for x in (ms.yaw, ms.speed, ms.vz, 1.0 if T.hidden else 0.0)]),
                              float).reshape(n, 4)
        else:
            A = np.zeros((0, 4))
        yaw = A[:, 0].copy()
        sp = A[:, 1]
        pk[:, 3] = sp * np.cos(yaw)
        pk[:, 4] = sp * np.sin(yaw)
        pk[:, 5] = A[:, 2]
        pk[:, C["yaw"]] = yaw
        occ = np.where(A[:, 3] != 0.0, fx["occ_hid"], fx["occ0"])
        pk[:, C["occ"]] = occ
        pos = np.ascontiguousarray(pk[:, 0:3])
        vel = np.ascontiguousarray(pk[:, 3:6])
        snap = TargetSnap(n, fx["tno"], fx["ids"], fx["rows"], fx["cls"], pos, vel, yaw, fx["size"], occ, fx["cfgs"], **st)
        snap.index = fx["index"]
        snap.id_index = fx["id_index"]
        snap.tno_l = fx["tno_l"]
        snap.n_req0 = fx["n_req0"]
        snap.n500 = fx["n500"]
        snap.tno_max = fx["tno_max"]
        snap.q2 = fx["q2"]
        snap.tidx = fx["tidx"]
        snap.skey = self._static_key
        snap.pmid = pos + fx["half_h"]
        snap.pk = pk
        self._snap, self._snap_key = snap, key
        return snap

    # ------------------------------------------------------------ 环境
    def env(self, ctx: Any, t_ns: int) -> tuple[EnvSample, NDArray]:
        e = getattr(ctx, "env", None)
        fn = getattr(e, "sky_sample", None)
        if callable(fn):
            try:
                s = fn(int(t_ns))
                last = self._env_last
                if last is not None and s is last[2]:
                    return last[1], last[3]                # M07 按 1 s 量化并缓存同一样本对象：同一对象直接复用
                bgv = getattr(s, "l_bg_bands_dba", None)
                bg = _BG_DEFAULT if bgv is None else np.asarray(bgv, float)
                raw = (*_SKY_F(s), getattr(s, "rain_mmph", 0.0), getattr(s, "band", None), getattr(s, "band_code", 0))
                if last is not None and raw == last[0]:
                    es = last[1]                          # 取值未变：复用同一 EnvSample（不可变），传感器参数缓存可按身份命中
                else:
                    band = raw[8] if raw[8] in ("day", "low", "night") else ("day", "low", "night")[int(raw[9])]
                    es = EnvSample(float(raw[0]), band, float(raw[1]), float(raw[2]), float(raw[3]), float(raw[4]), float(raw[5]),
                                   float(raw[6]), float(raw[7]))
                self._env_last = (raw, es, s, bg)
                return es, bg
            except Exception:
                pass
        self.stats["env_fallback"] += 1
        from awr.sim.targets.envview import fallback_env

        fb = fallback_env(int(t_ns))
        return EnvSample(mor_m=1e9, fallback=True), np.asarray(fb.l_bg_bands, float)

    def _tau(self, ctx: Any, p0: NDArray, p1: NDArray, t_ns: int, lam_nm: float, env: EnvSample) -> NDArray:
        return np.exp(-self._od(ctx, p0, p1, t_ns, lam_nm, env))

    def _od(self, ctx: Any, p0: NDArray, p1: NDArray, t_ns: int, lam_nm: float, env: EnvSample) -> NDArray:
        """p0 → p1 各对的光学厚度（M07 `optical_depth`；未装配或出错时按 EnvSample 的均匀消光）；float64 C 连续，透过率为 exp(−od)。"""
        e = getattr(ctx, "env", None)
        fn = getattr(e, "optical_depth", None)
        if callable(fn) and not env.fallback:
            try:
                n = p1.shape[0]
                P0 = self._p0buf
                if P0.shape[0] < n:
                    P0 = self._p0buf = np.empty((max(n, 2 * P0.shape[0], 32), 3))
                P0 = P0[:n]                               # 起点按行重复（M07 接口按对给出两端）；工作数组复用
                P0[:] = p0
                od = fn(P0, p1, int(t_ns), wavelength_nm=float(lam_nm))
                if not (od.__class__ is np.ndarray and od.dtype == np.float64 and od.shape == (n,) and od.flags.c_contiguous):
                    od = np.ascontiguousarray(np.asarray(od, float).reshape(n))
                return od
            except Exception:
                pass

        return sigma_per_m(env.mor_m, lam_nm, env.rho_v_gm3) * np.linalg.norm(p1 - p0, axis=1)

    # ------------------------------------------------------------ 需求
    def set_requirement(self, uav: str, target: str, level_req: str | None, n_req_px: float | None) -> None:
        """任务级 N_req 覆盖（M20；`uav = "*"` 对全部机体）；两者都为 None 时清除。"""
        key = (str(uav), str(target))
        if level_req is None and n_req_px is None:
            self.reqs.pop(key, None)
        else:
            if level_req is not None and level_req not in N_P90:
                from .specs import PerceptionError

                raise PerceptionError(561, "level_req", "LEVEL_UNKNOWN", value=level_req)
            self.reqs[key] = (level_req, None if n_req_px is None else float(n_req_px))
        self._reqs_rev += 1

    def n_req_of(self, uav: str, snap: TargetSnap, i: int) -> float:
        tid = snap.ids[i]
        lv, px = self.reqs.get((uav, tid)) or self.reqs.get(("*", tid)) or (None, None)
        return n_req_for(snap.level_req[i], snap.n_req_px[i], task_level=lv, task_n_px=px)

    def n_req_vec(self, uav: str, snap: TargetSnap) -> tuple[NDArray, NDArray]:
        """各识别物对机体 uav 的 (N_req, TTPF 的 N50)（N_req 与逐个 `n_req_of` 相同：(uav, id) 的任务覆盖优先于 ("*", id)，再到识别物
        自身；N50 = `N50_PX[level_req_of(N_req)]`）。"""
        if snap.n_req0 is None:
            base = np.array([n_req_for(snap.level_req[i], snap.n_req_px[i]) for i in range(snap.n)], float)
            return base, np.array([N50_PX[_lreq(float(x))] for x in base.tolist()], float)
        if not self.reqs:
            return snap.n_req0, snap.n500  # type: ignore[return-value]
        hit = self._nreq_cache.get(uav)
        if hit is not None and hit[0] is snap.skey and hit[1] == self._reqs_rev:
            return hit[2], hit[3]
        out = snap.n_req0.copy()
        idx = snap.id_index or {tid: i for i, tid in enumerate(snap.ids)}
        for who in ("*", uav):
            for (u, tid), (lv, px) in self.reqs.items():
                if u != who:
                    continue
                i = idx.get(tid)
                if i is not None:
                    out[i] = n_req_for(snap.level_req[i], snap.n_req_px[i], task_level=lv, task_n_px=px)
        n50 = np.array([N50_PX[_lreq(float(x))] for x in out.tolist()], float)
        if len(self._nreq_cache) > 256:
            self._nreq_cache.clear()
        self._nreq_cache[uav] = (snap.skey, self._reqs_rev, out, n50)
        return out, n50

    # ------------------------------------------------------------ stage
    def stage(self, S: Any, ctx: Any) -> None:
        self.field.S = S                               # FieldService.note_stage
        t_ns = int(getattr(ctx, "t_ns", 0))
        back = t_ns + 1_000_000 < self._last_t_ns     # 时钟回退（剧本重置、恢复快照）：从空状态开始
        sch = None
        if not back and not self._dirty and self.trt is not None and self.tab is not None:
            sch = self._sched(S, ctx, t_ns)
            if sch is not None and self.full and not (sch[1].size or sch[3] or sch[4]):
                # full 口径的空片（D2-FX-R2-sim 第 2 次）：与非 full 的空片相同地不绑定、不取快照，只照 full 口径清空视线缓存
                self.trt.los.clear()
                sch = None
            if sch is None:
                # 空片（本次无机体、无声阵列周期、无航迹维护，且无汇总要写）：不绑定、不取快照，只推进射线窗口；
                # 实体表与 M18 的变化在下一次有工作的调用中照常处理
                self.stats["calls"] += 1
                self._last_t_ns = t_ns
                h = self._rays_hist
                if h and t_ns - h[0][0] >= 1_000_000_000:
                    self._ray_stats(t_ns, 0)
                return
        if not self.bind(ctx):
            return
        self.stats["calls"] += 1
        if back:
            self.reset()
        self._last_t_ns = t_ns
        t = t_ns * 1e-9
        snap = self.targets(ctx)
        if sch is None:
            sch = self._sched(S, ctx, t_ns)
            if sch is None:
                # 空片：只推进射线窗口与写汇总
                h = self._rays_hist
                if h and t_ns - h[0][0] >= 1_000_000_000:
                    self._ray_stats(t_ns, 0)
                if self._dirty:
                    self._summary(snap)
                return
        act, slots, ac, do_ac, do_trk = sch
        los = self.trt.los
        rays0 = los.rays_by_owner.get("M19", 0)
        if self.full:
            los.clear()
        processed: list[Platform] = []
        if slots.size or do_ac:
            env, bg = self.env(ctx, t_ns)              # 只在本次有机体要处理时取环境（空片不取）
            if slots.size:
                processed = self._platforms(S, ctx, slots, imaging=True, kinds=("radar", "lidar"), nonimg_pose=snap.n > 0)
                for P in processed:
                    self._platform(P, snap, env, ctx, t, t_ns)
            if do_ac:
                self._last_ac = ac
                self._acoustic(S, ctx, act, snap, env, bg, t, t_ns)
        if do_trk:
            self._last_track_t = t
            self.tracks.tick(t)
            self._prune(snap, act, S)
        rays = los.rays_by_owner.get("M19", 0) - rays0
        if rays or self._rays_hist:
            self._ray_stats(t_ns, rays)
        if self._dirty:
            self._summary(snap)
        if processed:
            self._publish(ctx, processed, t_ns)

    def _sched(self, S: Any, ctx: Any, t_ns: int) -> tuple | None:
        """本次调用的工作：(在场机体, 本片机体, 声阵列周期号, 是否做声阵列, 是否做航迹维护)；三者皆无（且非 full）时为 None。"""
        ck = (getattr(ctx, "dt_tick", 0.004), getattr(ctx, "stage_dt", 0.0))
        if ck != self._cad_key:
            dt_tick = float(ck[0] or 0.004)
            sdt = float(ck[1] or 0.0) or dt_tick * 2
            self._cad = (max(1, round(sdt / dt_tick)), max(1, round((1.0 / sdt) / SHARD_HZ)))
            self._cad_key = ck
        every, n_sh = self._cad
        call = int(getattr(ctx, "tick", 0)) // every
        act = S.active_idx() if S is not None else _NO_ACT
        slots = self._slots(act, call, n_sh)
        ac = t_ns // ACOUSTIC_PERIOD_NS
        do_ac = ac != self._last_ac and act.size > 0
        do_trk = call % n_sh == 0 and t_ns * 1e-9 - self._last_track_t >= 0.199
        if not (slots.size or do_ac or do_trk or self.full):
            return None
        return act, slots, ac, do_ac, do_trk

    def _slots(self, act: NDArray, call: int, n_sh: int) -> NDArray:
        """`shard_slots` 的周期缓存：同一周期、同一在场集合只算一次各机所在片。"""
        n = int(act.size)
        if n == 0:
            return act
        cyc, k = divmod(int(call), int(n_sh))
        ab = act.tobytes()
        sh = self._shard
        if sh is None or sh[0] != ab or sh[1] != cyc or sh[2] != n_sh:
            # 第 k 片 = pos == k 的机体按 slot 升序（与 `shard_slots` 相同），排成一个数组，片 k 为 [ends[k], ends[k+1])，取用时切片
            if n <= 128:                                # 机体数小：纯 Python 计数排序（少量 numpy 调用）
                pos_l = [(((i + cyc) % n) * n_sh) // n for i in range(n)]
                ends = [0] * (n_sh + 1)
                for q in pos_l:
                    ends[q + 1] += 1
                for q in range(n_sh):
                    ends[q + 1] += ends[q]
                fill = ends[:-1]
                al = act.tolist()
                srt = [0] * n
                for i, q in enumerate(pos_l):
                    srt[fill[q]] = al[i]
                    fill[q] += 1
                sh = self._shard = (ab, cyc, n_sh, np.array(srt, dtype=act.dtype), ends)
            else:
                pos = (((np.arange(n) + cyc) % n) * n_sh) // n
                o = np.argsort(pos, kind="stable")
                sh = self._shard = (ab, cyc, n_sh, act[o], [0, *np.cumsum(np.bincount(pos, minlength=n_sh)).tolist()])
        e = sh[4]
        return sh[3][e[k]:e[k + 1]]

    @staticmethod
    def shard_slots(act: NDArray, call: int, n_sh: int) -> NDArray:
        """本次调用处理的机体：在场机体按 slot 升序编号 i（共 n 架），每周期（n_sh 次调用）每架恰好一次，位置为
        `⌊((i + 周期序号) mod n)·n_sh / n⌋`：均匀分布在周期内，且每周期整体错开一位，使共享射线令牌桶在各机之间轮流有余量
        （固定的 `slot % n_sh` 分片会让同一几架机在每个周期都恰好遇到令牌不足而长期得不到视线结果）。"""
        n = int(act.size)
        if n == 0:
            return act
        cyc, k = divmod(int(call), int(n_sh))
        pos = (((np.arange(n) + cyc) % n) * n_sh) // n
        return act[pos == k]

    def _platforms(self, S: Any, ctx: Any, slots: NDArray, *, imaging: bool, kinds: tuple[str, ...], nonimg_pose: bool = True) -> list[Platform]:
        if self.platforms_fn is not None:
            return list(self.platforms_fn(S, ctx, slots, imaging, kinds))
        return self.adapter.collect(S, ctx, slots, imaging=imaging, kinds=kinds, nonimg_pose=nonimg_pose)

    def _ray_stats(self, t_ns: int, rays: int) -> None:
        self.stats["rays"] += rays
        h = self._rays_hist
        if rays:
            if rays > self.rays_tick_max:
                self.rays_tick_max = rays
            h.append((t_ns, rays))
            self._rays_sum += rays
        while h and t_ns - h[0][0] >= 1_000_000_000:
            self._rays_sum -= h.popleft()[1]

    @property
    def rays_s(self) -> float:
        return float(self._rays_sum)

    # ------------------------------------------------------------ 成像
    def _platform(self, P: Platform, snap: TargetSnap, env: EnvSample, ctx: Any, t: float, t_ns: int) -> None:
        meas: dict[int, tuple] = {}                              # target_no -> (sigma, z, R[, (z 数组, 行)])
        self._mev = []
        sensors_out = [self._imaging(P, im, snap, env, ctx, t, t_ns, meas) for im in P.imaging]
        mev = self._mev
        if mev and (len(mev) > 1 or P.nonimg):
            for ev in mev:                                         # 多个来源：按此前的次序并入（同一识别物取 sigma 更小者）
                self._meas_into(ev, snap, meas)
            mev = []
        ak = self._apairs.get(P.agent_no)
        an = self._asn.get(P.agent_no)
        ghost = False
        if ak and an:
            imgs = P.imaging
            ghost = (imgs[0].name not in an or len(an) > 1) if len(imgs) == 1 else not self._img_names(P).issuperset(an)
        if ghost:
            names = self._img_names(P)
            gk = [kk for kk in ak if kk[1] not in names and kk[2] in snap.index]
            if gk:
                # 传感器关闭、卸下或机体不再给出该成像传感器：按视锥外处理（降级与丢失照常计时）
                ghosts = {nm: Imaging(nm, 0, None, None, P.pos, np.eye(3), 0.0, 0.0) for nm in {kk[1] for kk in gk}}  # type: ignore[arg-type]
                self._fov_steps(gk, P, ghosts, snap, t, t_ns, ctx)
        radar_out: dict | None = None
        lidar_out: list[dict] = []
        for N in P.nonimg:
            if N.kind == "radar" and radar_out is None:
                radar_out = self._radar(P, N, snap, env, ctx, t, t_ns, meas)
            elif N.kind == "lidar":
                lidar_out.extend(self._lidar(P, N, snap, env, ctx, t, t_ns, meas))
        if mev:
            ev = mev[0]                                            # 唯一来源（单个成像传感器）：直接从评估数组更新航迹
            rows = np.flatnonzero(ev.codes[:, 3])
            ti = ev.cand[rows].tolist()
            cls = snap.cls
            tno_l = snap.tno_l or snap.tno.tolist()
            self.tracks.update_batch([tno_l[i] for i in ti], [cls[i] for i in ti], t, ev.z[rows], ev.res[rows, 3].tolist())
        elif meas:
            idx, cls = snap.index, snap.cls
            tn_l, cl_l, z_l, s_l, src = [], [], [], [], []
            for tno, mv in meas.items():
                i = idx.get(tno)
                if i is not None:
                    tn_l.append(tno)
                    cl_l.append(cls[i])
                    z_l.append(mv[1])
                    s_l.append(mv[0])
                    src.append(mv[3] if len(mv) > 3 else None)
            if src and src[0] is not None and all(x is not None and x[0] is src[0][0] for x in src):
                Z = src[0][0][[x[1] for x in src]]          # 全部来自同一次评估：一次取行
            else:
                Z = np.array(z_l, float).reshape(len(z_l), 3) if z_l else np.zeros((0, 3))
            self.tracks.update_batch(tn_l, cl_l, t, Z, s_l)
        # 帧：读取时才展开（frame_for）；预算字段取本机处理时的值
        self.frames[P.uav] = {"uav": P.uav, "t_sim_ns": t_ns, "_agent": P.agent_no,
                              "_lazy": (sensors_out, (int(self.rays_tick_max), float(self._rays_sum), self._los_deferred()))}
        self.agent_uav[P.agent_no] = P.uav
        if radar_out is not None:
            self.radar_last[P.uav] = radar_out
        self.lidar_last[P.uav] = {"n_points_target": lidar_out} if P.nonimg and any(N.kind == "lidar" for N in P.nonimg) else None  # type: ignore[assignment]

    def _los_deferred(self) -> int:
        los = self.los
        v = getattr(los, "deferred_total", None)
        return int(v) if v is not None else int(los.stats.get("deferred", 0))

    def _prefix(self, ctx: Any, agent: int) -> int:
        """计数器 RNG 键的前四级（seed、流 18、agent_no、tick）；前三级按（seed，agent_no）缓存，同一 tick 同一机复用。"""
        tick = int(getattr(ctx, "tick", 0))
        seed = self._seed if self._seed is not None else self.seed(ctx)
        k = (seed, agent, tick)
        if self._pfx[0] == k:
            return self._pfx[1]
        k3 = self._pfx3.get((seed, agent))
        if k3 is None:
            if len(self._pfx3) > 4096:
                self._pfx3.clear()
            k3 = self._pfx3[(seed, agent)] = K.prefix3(seed, S_PC, int(agent))
        v = K.prefix_tick(k3, tick)
        self._pfx = (k, v)
        return v

    def _k3(self, ctx: Any, agent: int) -> np.uint64:
        """RNG 键的前三级（seed、流 18、agent_no；np.uint64，交给 post_fsm 在核内补 tick 一级）。"""
        seed = self._seed if self._seed is not None else self.seed(ctx)
        v = self._k3c.get((seed, agent))
        if v is None:
            if len(self._k3c) > 4096:
                self._k3c.clear()
            v = self._k3c[(seed, agent)] = np.uint64(K.prefix3(seed, S_PC, int(agent)))
        return v

    def _noise3(self, ctx: Any, agent: int, sensor_no: int, tno: int) -> NDArray:
        out = np.empty(3)
        if K.HAVE_NUMBA:
            K.normals3(np.uint64(self._prefix(ctx, agent)), int(sensor_no), int(tno), out)
            return out
        from awr.sim.sensors import cbrng

        ch = np.array([_ch(sensor_no, tno, j) for j in range(3)], np.int64)
        return np.asarray(cbrng.normal_elem(self.seed(ctx), S_PC, int(agent), int(getattr(ctx, "tick", 0)), ch), float).reshape(3)

    def _uniform(self, ctx: Any, agent: int, sensor_no: int, tno: int, j: int = 0) -> float:
        return K.uniform_ch(self._prefix(ctx, agent), _ch(sensor_no, tno, j))

    def _imaging(self, P: Platform, im: Imaging, snap: TargetSnap, env: EnvSample, ctx: Any, t: float, t_ns: int, meas: dict) -> _SensorFrame:
        name = im.name
        head = (name, im.spec.kind, im.mode, im.st.f_mm, im.hfov, im.vfov, dict(im.illum) if im.illum else None)
        rk = (P.agent_no, name)
        amap = self._asmap.get(rk)
        n_before = len(amap) if amap else 0              # 本（机体，传感器）已有的对数（评估之前）
        if K.HAVE_NUMBA:
            ev, n_old = self._imaging_nb(P, im, snap, env, ctx, t, t_ns)
        else:
            ev, n_old = self._imaging_py(P, im, snap, env, ctx, t, t_ns)
        if n_before > n_old:
            # 视锥外的已有对（按对的插入次序；本次新建的对都在视锥内）
            amap = self._asmap.get(rk)
            if amap:
                seen = set(snap.tno[ev.cand].tolist()) if ev is not None else set()
                idx = snap.index
                rest = [(rk[0], name, tn) for tn in amap if tn not in seen and tn in idx]
                if rest:
                    self._fov_steps(rest, P, {name: im}, snap, t, t_ns, ctx)
        if ev is None:
            return _SensorFrame(head, 0, [], None, snap)
        if self.ev_hook is not None:
            self.ev_hook(P, t_ns, ev)
        if ev.n_meas:
            self._mev.append(ev)                         # 量测在 _platform 中并入（同一机的各来源取 sigma 更小者）
        return _SensorFrame(head, ev.m, None, ev, snap)  # 帧的前 8 对在读取时取出

    def _meas_into(self, ev: _Eval, snap: TargetSnap, meas: dict) -> None:
        tno_l = snap.tno_l or snap.tno.tolist()
        cand_l = ev.cand.tolist()
        res, z, core = ev.res, ev.z, ev.core
        for j, h in enumerate(ev.codes[:, 3].tolist()):
            if not h:
                continue
            tno = tno_l[cand_l[j]]
            sig = float(res[j, 3])
            prev = meas.get(tno)
            if prev is None or sig < prev[0]:
                meas[tno] = (sig, z[j], float(core[j, 0]), (z, j))

    def _imaging_py(self, P: Platform, im: Imaging, snap: TargetSnap, env: EnvSample, ctx: Any, t: float, t_ns: int) -> tuple[_Eval | None, int]:
        """numpy 判据与 Python 对状态推进（`state.step`；numba 不可用或 `AWR_KERNEL=numpy`）。返回（评估，推进的已有对数）。"""
        ev = self._eval_np(P, im, snap, env, ctx, t_ns) if snap.n else None
        if ev is None:
            return None, 0
        kind = im.spec.kind
        kc = _KIND_CODE[kind]
        name = im.name
        agent = P.agent_no
        pairs = self.pairs
        cand_l = ev.cand.tolist()
        codes_l = ev.codes.tolist()                     # [lim, level, cond, has_meas]
        dfr_l = ev.dfr.tolist()
        nreq_l = ev.n_req.tolist()
        tno_l = snap.tno_l or snap.tno.tolist()
        n_old = 0
        for j, i in enumerate(cand_l):
            tno = tno_l[i]
            lc, lv, cnd, _h = codes_l[j]
            cond = cnd != 0
            key = (agent, name, tno)
            ps = pairs.get(key)
            if ps is None:
                if lv == 0 and not cond:
                    continue
                ps = self._pair_new(key)
            else:
                n_old += 1
                if self.ptab.PI[ps.s, PR.I_HASLAST] and self.ptab.PI[ps.s, PR.I_KIND] != kc and ps.level:
                    self._mark(tno)                     # 同名传感器换了类别（EO 与 NIR 之间切换）：best_sensor 可能变化
            nr = nreq_l[j]
            if nr != ps.n_req:
                ps.n_req = nr
                ps.level_req = _lreq(nr)
            ps.set_last(ev, j)
            prev_lv, prev_cap = ps.level, ps.cap
            trans = step(ps, t, lv, cond, deferred=dfr_l[j], reason=None if cond or lc < 0 else LIMITING[lc])
            if ps.level != prev_lv or (ps.cap in _CAPSET) != (prev_cap in _CAPSET):
                self._mark(tno)
            if trans:
                self._transitions(trans, ps, key, P, im, i, snap, t_ns, ctx)
            if not cond and not ps.active:
                self._pair_del(key)
        pst = np.zeros((ev.m, 3))
        for j, i in enumerate(cand_l):
            ps = pairs.get((agent, name, tno_l[i]))
            if ps is not None:
                pst[j] = (ps.level, ps.cap, ps.hold_s(t))
        ev.pst = pst
        return ev, n_old

    def _scratch(self, n: int) -> None:
        """不随评估保留的工作数组（按识别物数扩容）。"""
        if self._scr_n >= n:
            return
        c = max(n, 2 * self._scr_n, 32)
        self._scr = {"d": np.empty((c, 3)), "rr": np.empty(c), "need": np.empty(c, np.bool_), "keys": np.empty((c, 6), np.int64),
                     "order": np.empty(c, np.int64), "oB": np.empty((c, 3)), "oH": np.empty(c), "oK": np.empty((c, 6), np.int64),
                     "slots": np.empty(c, np.int64), "flags": np.empty(c, np.int64), "tr": np.empty((2 * c + 1, 7)), "tnew": np.empty(c, np.int64),
                     "lout": np.zeros((c, 5), np.int64), "ra": np.empty((3 * c, 3)), "rb": np.empty((3 * c, 3))}
        self._scr_n = c

    def _obufs(self, agent: int, name: str, n: int) -> list:
        """（机体，传感器）评估输出数组的双缓冲（交替使用）：[容量, cand, core, res, codes, z, dfr, pst, 按 m 的视图缓存]。评估结果
        （`_Eval`）只被该机最新的帧引用到下一次评估为止（帧读取时展开为值），因此隔一次评估复用同一组数组是安全的。"""
        ob = self._obuf.get((agent, name))
        if ob is None:
            ob = self._obuf[(agent, name)] = [1, None, None]
        par = ob[0] = 1 - ob[0]
        B = ob[1 + par]
        if B is None or B[0] < n:
            c = max(n, 32 if B is None else 2 * B[0])
            B = ob[1 + par] = [c, np.empty(c, np.int64), np.empty((c, K.CORE_N)), np.empty((c, K.POST_N)), np.empty((c, 4), np.int64),
                               np.empty((c, 3)), np.empty(c, np.bool_), np.empty((c, 3)), None]
        return B

    def _smap(self, rk: tuple[int, str], snap: TargetSnap) -> NDArray:
        """（机体，传感器）的 target_no → 对状态行（-1 无对），长度覆盖快照内最大 target_no。"""
        sm = self._smaps.get(rk)
        need = int(snap.tno_max) + 1
        if sm is None or sm.shape[0] < need:
            new = np.full(max(need, 64, 0 if sm is None else 2 * sm.shape[0]), -1, np.int64)
            if sm is not None:
                new[:sm.shape[0]] = sm
            sm = self._smaps[rk] = new
        return sm

    def _imaging_nb(self, P: Platform, im: Imaging, snap: TargetSnap, env: EnvSample, ctx: Any, t: float, t_ns: int) -> tuple[_Eval | None, int]:
        """numba 路径：视锥门 → 路径透过率（M07）→ 判据核（含视线请求次序与格键）→ 共享视线缓存 → 等级、量测与对状态核；
        只有新对、状态转移、汇总标记与删除回到 Python。返回（评估，推进的已有对数）。"""
        n = snap.n
        if n == 0:
            return None, 0
        self._scratch(n)
        sc = self._scr
        p = im.p if (im.p.__class__ is np.ndarray and im.p.dtype == np.float64 and im.p.shape == (3,)) else \
            np.ascontiguousarray(im.p, dtype=np.float64).reshape(3)
        Rm = im.R if (im.R.__class__ is np.ndarray and im.R.dtype == np.float64 and im.R.flags.c_contiguous) else \
            np.ascontiguousarray(im.R, dtype=np.float64)
        d, rr = sc["d"], sc["rr"]
        kind = im.spec.kind
        name = im.name
        agent = P.agent_no
        B = self._obufs(agent, name, n)
        m = K.fov_gate(snap.pos, p, Rm, 0.5 * im.hfov, 0.5 * im.vfov, d, rr, B[1])
        if m == 0:
            return None, 0
        V = B[8]
        if V is None or V[0] != m:
            V = B[8] = (m, B[1][:m], B[2][:m], B[3][:m], B[4][:m], B[5][:m], B[6][:m], B[7][:m])
        _m, cand, core, res, codes, z, dfr, pst = V
        self.stats["pairs_eval"] += m
        od = self._od(ctx, p, snap.pmid[cand], t_ns, _LAM[kind], env)   # 核内取 τ = exp(−od)
        if od.dtype != np.float64 or not od.flags.c_contiguous:
            od = np.ascontiguousarray(od, dtype=np.float64)
        n_req, n50 = self.n_req_vec(P.uav, snap)
        prm, ill = self._sensor_params(im.st, env, (agent, name))
        v = P.vel
        if not (v.__class__ is np.ndarray and v.dtype == np.float64 and v.shape == (3,)):
            v = np.ascontiguousarray(v, dtype=np.float64).reshape(3)
        need, keys, order, oB, oH, oK = sc["need"], sc["keys"], sc["order"], sc["oB"], sc["oH"], sc["oK"]
        info = self._info
        los = self.los
        rk = (agent, name)
        t_i = int(t_ns)
        kt = los.kernel_tables("M19", m)
        lout, ra, rb, cnt = sc["lout"], sc["ra"], sc["rb"], self._lcnt
        KEY, HSH, HT, COMP, VIS, TT, PREV, NEXT, LM, ttl, stale_ns, max_e, tot, sub, has_sub, lst = kt
        n_jobs = K.eval_a(cand, m, d, rr, snap.pk, od, n_req, prm, ill, p, Rm, v, im.src != "default", max(0.25 * im.hfov, _HALF_DEG), core,
                          need, keys, self._cell_a, self._cell_b, self._rot.get(rk, 0), order, oB, oH, oK, info, KEY, HSH, HT, COMP, VIS, TT, PREV,
                          NEXT, LM, t_i, ttl, stale_ns, tot, sub, has_sub, lout, ra, rb, cnt, lst)
        nn, s0 = info[:2].tolist()
        vis = _NO_VIS
        if nn:
            self.stats["los_requests"] += nn
            if n_jobs < 0:                               # 格键无效（坐标非有限）：经 LosCache 的通用入口（与此前一样抛出）
                lout[:nn] = los.query_raw(p, oB[:nn], oH[:nn], "M19", t_ns, "all")
                n_jobs = 0
            elif n_jobs > 0:
                vis = los.cast_jobs("M19", ra, rb, int(cnt[5]), int(cnt[3]))
        tab = self.ptab
        if tab.n_free < m:
            tab.grow(m)
        smap = self._smap(rk, snap)
        slots, flags, tr = sc["slots"], sc["flags"], sc["tr"]
        qrm = self._qrm
        qrm[K.Q_HOLD] = P.hold_sigma_m
        trk = self.tracks
        trk_on = len(P.imaging) == 1 and not P.nonimg          # 唯一量测来源：航迹在核内更新（与 _platform 的批量更新相同）
        if trk_on:
            trk.ensure(snap.tno_max, m)
        tnew = sc["tnew"]
        n_meas = K.eval_b(n_jobs, vis, KEY, HSH, HT, COMP, VIS, TT, PREV, NEXT, LM, t_i, ttl, max_e, oK, cand, m, core, snap.pk, snap.tno, n_req, n50,
                          need, order, lout, nn, qrm, self._k3(ctx, agent), int(getattr(ctx, "tick", 0)), int(im.sensor_no), _KIND_CODE[kind], t,
                          smap, slots, tab.PI, tab.PF, tab.FREE, tab.PM, res, codes, z, dfr, flags, tr, info, pst, trk_on, snap.q2, trk.TMAP,
                          trk.FREE, trk.FM, trk.TF, trk.TI, trk.HIST, TR.CONFIRM_S, TRACK_CONFIRM_N, TR.V0, tnew, agent)
        _c, n_def, n_stale, first_def, n_tr, _nw, n_old, n_flag, n_tn = info.tolist()
        if trk_on:
            if n_tn:
                tm_ = trk.TMAP
                for tn in tnew[:n_tn].tolist():
                    trk.register(tn, int(tm_[tn]))
            n_meas = 0                                   # 已并入航迹
        if nn:
            st_ = self.stats
            st_["deferred"] += n_def
            st_["stale"] += n_stale
            # 下一次从本次第一个延期的对开始（预算用尽处接续，确定性轮转）
            self._rot[rk] = (s0 + (first_def if first_def >= 0 else nn)) % nn
        ev = _Eval(kind, cand, core, res, codes, z, n_req, dfr, n_meas, pst, full_nreq=True)
        if n_tr or n_flag:
            fl = flags[:m].tolist()
            rows = [r for r, f in enumerate(fl) if f]    # 有标志或有转移（位 3）的行，升序
            tno_l = snap.tno_l or snap.tno.tolist()
            ci = cand.tolist()
            self._after_rows(P, im, snap, t_ns, ctx, {r: (agent, name, tno_l[ci[r]]) for r in rows}, ci, slots, fl, tr, n_tr, rows=rows, rr=rr)
        return ev, n_old

    def _after_rows(self, P: Platform, im: Imaging, snap: TargetSnap, t_ns: int, ctx: Any, keys: Any, idx: list, slots: NDArray, fl: list,
                    tr: NDArray, n_tr: int, ims: dict | None = None, rows: list | None = None, rr: NDArray | None = None) -> None:
        """核推进之后的 Python 部分（按行次序）：登记新对、发出转移的事件与检测环、汇总标记、删除不再活跃的对。
        keys[r]：第 r 行的对键（list，或只含 rows 的 dict）；fl[r] 为第 r 行的标志；rows 为要处理的行（缺省全部）；rr：本次视锥门按识别物
        下标算出的距离（与 `_range` 同一式，事件的 range_m 直接取用）。"""
        rows = list(range(len(keys))) if rows is None else rows
        for r in rows:
            if fl[r] & 4:
                self._pair_register(keys[r], int(slots[r]))
        if n_tr:
            trr = tr[:n_tr].tolist()
            k = 0
            while k < n_tr:
                r = int(trr[k][0])
                k1 = k + 1
                while k1 < n_tr and int(trr[k1][0]) == r:
                    k1 += 1
                key = keys[r]
                self._tr_rows(trr[k:k1], self.pairs[key], key, P, im if ims is None else ims[key[1]], idx[r], snap, t_ns, ctx, rr)
                k = k1
        for r in rows:
            if fl[r] & 1:
                self._mark(keys[r][2])
        for r in rows:
            if fl[r] & 2:
                self._pair_del(keys[r])

    def _fov_steps(self, keys: list, P: Platform, ims: dict, snap: TargetSnap, t: float, t_ns: int, ctx: Any) -> None:
        """视锥外或传感器缺失的已有对（level_m 0、条件不满足、reason fov）：按 keys 次序推进。ims：传感器名 → Imaging（事件字段）。"""
        if not K.HAVE_NUMBA:
            for kk in keys:
                self._step_pair(kk, P, ims[kk[1]], snap.index.get(kk[2]), snap, t, t_ns, ctx, 0, False, False, "fov")
            return
        pairs = self.pairs
        n = len(keys)
        slots = np.array([pairs[kk].s for kk in keys], np.int64)
        flags = np.empty(n, np.int64)
        tr = np.empty((2 * n + 1, 7))
        n_tr = int(PR.fov_rows(self.ptab.PI, self.ptab.PF, slots, n, t, tr, flags))
        if n_tr or flags.any():
            idx = snap.index
            self._after_rows(P, ims[keys[0][1]], snap, t_ns, ctx, keys, [idx.get(kk[2]) for kk in keys], slots, flags.tolist(), tr, n_tr, ims)

    # ------------------------------------------------------------ 评估（视锥门、判据、视线、等级与量测）
    def _sensor_params(self, st: SensorState, env: EnvSample, rk: Any = None) -> tuple[NDArray, NDArray]:
        """(prm（kernels.P_*），开启的照明器 [k, 3])：与 `level.evaluate` 同一组标量与同一运算次序。与该（机体，传感器）rk 上次同一
        EnvSample 对象、同一规格对象且状态值相同时直接复用上次结果。"""
        by = self._sp_last
        fast = by.get(rk)
        if fast is not None and fast[1] is env:
            l0 = fast[0]
            if l0 is st or (l0.spec is st.spec and l0.f_mm == st.f_mm and l0.mode == st.mode and l0.resolution == st.resolution
                            and l0.illum_on == st.illum_on):
                return fast[2], fast[3]                  # 同一规格对象与同一组状态值（状态对象可能每次新建）
        r = self._sensor_params_k(st, env)
        if len(by) > 4096:
            by.clear()
        by[rk] = (st, env, r[0], r[1])
        return r

    def _sensor_params_k(self, st: SensorState, env: EnvSample) -> tuple[NDArray, NDArray]:
        spec = st.spec
        key = (id(spec), float(st.f_mm), st.mode, st.resolution, st.illum_on, float(env.illum_lx), env.band, float(env.t_sky_c), float(env.t_bg_c),
               float(env.eps_bg), float(env.k_cross))
        hit = self._sp_cache.get(key)
        if hit is not None and hit[0] is spec:
            return hit[1], hit[2]
        kind = spec.kind
        f = float(st.f_mm)
        pp = p_prime_um(spec, f, st.resolution)
        E = float(env.illum_lx)
        prm = np.zeros(K.PRM_N)
        prm[K.P_ND] = N_D
        prm[K.P_FULL] = 1.0 if self.full else 0.0
        prm[K.P_KIND] = _KIND_CODE[kind]
        prm[K.P_F] = f
        prm[K.P_PP] = pp
        prm[K.P_TRACK] = _TRACK
        prm[K.P_CMIN] = _C_MIN
        prm[K.P_CSPAN] = _C_SPAN
        prm[K.P_EDGE] = float(_LI["nir_edge_frac"])
        prm[K.P_SNR_DEN] = 1.0
        prm[K.P_SNR_SPAN] = 1.0
        ill = np.zeros((0, 3))
        if kind == "lwir":
            ts, tb = env.t_sky_c + K.K0, env.t_bg_c + K.K0
            clutter = spec.sigma_clutter_k[0] if env.band == "day" else spec.sigma_clutter_k[1]
            prm[K.P_TS] = ts
            prm[K.P_TB_EB] = env.eps_bg * tb
            prm[K.P_TS_ES] = (1.0 - env.eps_bg) * ts
            prm[K.P_KCROSS] = env.k_cross
            prm[K.P_SNR_DEN] = math.sqrt(spec.netd_k ** 2 + clutter ** 2)
            prm[K.P_SNR_MIN] = float(_LW["snr_min"])
            prm[K.P_SNR_SPAN] = float(_LW["snr_span"])
            prm[K.P_TEXP], prm[K.P_JIT] = spec.t_int_s, spec.jitter_short_urad
        else:
            kc, km = _k_passive(E, spec.e_min_color_lx), _k_passive(E, spec.e_min_mono_lx)
            prm[K.P_KM] = km
            if kind == "nir":
                o = spec.outputs.get(st.resolution or spec.analysis) if spec.outputs else None
                crop_mm = (o[1] if o is not None else spec.w_native_px) * spec.pixel_um * 1e-3
                hf = 2.0 * np.arctan(crop_mm / (2.0 * np.float64(f)))
                rows = []
                for j, il in enumerate(spec.illuminators):
                    if st.illum_on and (j >= len(st.illum_on) or not st.illum_on[j]):
                        continue
                    th_deg = np.degrees(hf) if il.divergence_deg is None else np.float64(float(il.divergence_deg))
                    coef = il.r_ref_m * (il.theta_ref_deg / np.maximum(th_deg, 1e-9)) * math.sqrt(il.p_w / il.p_ref_w)
                    rows.append((float(coef), float(il.rho_ref), float(0.5 * np.radians(th_deg) + 1e-9)))
                ill = np.array(rows, float).reshape(-1, 3)
                t_exp = spec.t_exp_low_s
            else:
                prm[K.P_KL] = {"day": kc, "lowlight": km, "nir": km}.get(st.mode, max(kc, km))
                t_exp = spec.t_exp_day_s if spec.switch_lx <= E else spec.t_exp_low_s
            prm[K.P_TEXP] = t_exp
            prm[K.P_JIT] = spec.jitter_short_urad if t_exp <= spec.jitter_fast_max_s else spec.jitter_long_urad
        if len(self._sp_cache) > 64:
            self._sp_cache.clear()
        self._sp_cache[key] = (spec, prm, ill)
        return prm, ill

    def _los_pairs(self, P: Platform, im: Imaging, snap: TargetSnap, cand: NDArray, need: NDArray, t_ns: int,
                   keys: NDArray | None = None) -> tuple[NDArray, NDArray]:
        """请求了视线的对（need）按 target_no 升序、起点按服务数轮转，经共享 `LosCache` 取三条射线；返回 (k_los, deferred)。
        keys：各对的缓存键（核内算出，[m, 6]），None 时由 `LosCache` 自己取。"""
        m = cand.size
        k_los = np.zeros(m)
        deferred = np.zeros(m, bool)
        idx_l = [j for j, v in enumerate(need.tolist()) if v]
        n_idx = len(idx_l)
        if not n_idx:
            return k_los, deferred
        rk = (P.agent_no, im.name)
        s0 = self._rot.get(rk, 0) % n_idx
        order = idx_l[s0:] + idx_l[:s0] if s0 else idx_l
        self.stats["los_requests"] += n_idx
        sel = cand[order]
        los = self.los
        q = getattr(los, "query_raw", None)
        if q is not None:
            out = q(np.asarray(im.p, float).reshape(3), snap.pos[sel], snap.size[sel, 2], "M19", t_ns, "all",
                    keys=None if keys is None else keys[order])
            kv = _KLOS3
            rows = out.tolist()
            kl_l = [kv[r[0]] for r in rows]
            st_l = [r[1] for r in rows]
            dl_l = [r[2] for r in rows]
        else:
            out = los.get_batch(np.broadcast_to(im.p, (n_idx, 3)), snap.pos[sel], snap.size[sel, 2], "M19", t_ns, "all")
            kl_l, st_l, dl_l = [o.k_los for o in out], [o.stale for o in out], [o.deferred for o in out]
        first_def = None
        n_def = n_stale = 0
        for pos, j in enumerate(order):
            if dl_l[pos]:
                deferred[j] = True
                n_def += 1
                if first_def is None:
                    first_def = pos
            else:
                k_los[j] = kl_l[pos]
                if st_l[pos]:
                    n_stale += 1
        self.stats["deferred"] += n_def
        self.stats["stale"] += n_stale
        # 下一次从本次第一个延期的对开始（预算用尽处接续，确定性轮转）
        self._rot[rk] = (s0 + (first_def if first_def is not None else n_idx)) % max(n_idx, 1)
        return k_los, deferred

    def _eval_np(self, P: Platform, im: Imaging, snap: TargetSnap, env: EnvSample, ctx: Any, t_ns: int) -> _Eval | None:
        """numpy 路径（numba 不可用或 `AWR_KERNEL=numpy`）：与 `_eval_nb` 同一判据与同一输出。"""
        n = snap.n
        d = snap.pos - im.p[None, :]
        R = np.linalg.norm(d, axis=1)
        u = d @ im.R                                   # 传感器系（FLU，x 为视轴）
        with np.errstate(invalid="ignore", divide="ignore"):
            in_fov = (u[:, 0] > 0.5) & (np.abs(np.arctan2(u[:, 1], u[:, 0])) <= 0.5 * im.hfov) & \
                     (np.abs(np.arctan2(u[:, 2], u[:, 0])) <= 0.5 * im.vfov)
        cand = np.flatnonzero(in_fov)
        if cand.size == 0 or n == 0:
            return None
        m = cand.size
        self.stats["pairs_eval"] += m
        Rc = R[cand]
        _R, eps, beta = angles(np.broadcast_to(im.p, (m, 3)), snap.pos[cand], snap.yaw[cand])
        dc = np.asarray(critical_dim(snap.size[cand], eps, beta), float).reshape(m)
        ov = snap.crit_ov[cand]
        dc = np.where(np.isnan(ov), dc, ov)
        bore = im.R[:, 0]
        cos_off = np.clip((d[cand] @ bore) / np.maximum(Rc, 1e-9), -1.0, 1.0)
        off = np.arccos(cos_off)
        tracking = (im.src != "default") & (off <= max(0.25 * im.hfov, math.radians(0.5)))
        los_u = d[cand] / np.maximum(Rc, 1e-9)[:, None]
        v_rel = snap.vel[cand] - P.vel[None, :]
        v_perp = np.linalg.norm(v_rel - np.sum(v_rel * los_u, axis=1)[:, None] * los_u, axis=1)
        kind = im.spec.kind
        tau = self._tau(ctx, im.p, snap.pos[cand] + np.array([0.0, 0.0, 0.5]) * snap.size[cand, 2:3], t_ns, _LAM[kind], env)
        a = PairArrays(Rc, dc, snap.hot[cand], snap.c0[cand], snap.camo[cand], snap.rho850[cand], snap.th_abs[cand], snap.dt_rel[cand],
                       snap.t_surf[cand], snap.emis[cand], snap.occ[cand], np.ones(m), np.broadcast_to(tracking, (m,)).copy(), v_perp,
                       np.ones(m, bool), np.ones(m, bool), tau, tau if kind == "nir" else None, off)
        n_req = self.n_req_vec(P.uav, snap)[0][cand]
        r = evaluate(im.st, a, env, n_req)
        gate = np.minimum(N_D, n_req)
        need = (r["n"] >= gate) & (r["n_max"] >= gate)
        if self.full:
            need = np.ones(m, bool)
        k_los, deferred = self._los_pairs(P, im, snap, cand, need, t_ns)
        k_los = np.where(need | deferred, k_los, 1.0)        # 未过前级门控的对不请求视线：k_los 不参与限制（n_max < 3.5 已判 NONE）
        n_eff = r["n_max"] * k_los
        stack = np.stack([k_los * (1.0 - a.occ), r["k_light"], r["k_c"], r["k_blur"]], axis=1)
        arg = np.array([1, 3, 4, 5], np.int8)[np.argmin(stack, axis=1)]
        lim = np.where(r["n"] < n_req, 2, arg)
        lim = np.where(n_eff >= n_req, -1, lim)
        level = np.where(deferred, 0, level_of(n_eff))
        cond = ~deferred & (k_los >= K_LOS_MIN - 1e-9) & (n_eff >= n_req)
        p = np.zeros(m)
        for nr in np.unique(n_req):
            sel = n_req == nr
            p[sel] = ttpf(n_eff[sel], N50_PX[_lreq(float(nr))])
        core = np.empty((m, K.CORE_N))
        core[:, 0], core[:, 1], core[:, 2], core[:, 3] = Rc, r["n"], r["n_max"], r["gsd"]
        core[:, 4], core[:, 5], core[:, 6] = r["k_c"], r["k_light"], r["k_blur"]
        core[:, 7] = r["dt_app"] if r["dt_app"] is not None else np.nan
        core[:, 8] = off
        has = (level >= 1) & ~deferred
        sig = np.sqrt((Rc * POINT_ERR_RAD) * (Rc * POINT_ERR_RAD) + r["gsd"] * r["gsd"] + P.hold_sigma_m * P.hold_sigma_m)
        z = np.zeros((m, 3))
        hi = np.flatnonzero(has)
        if hi.size:
            from awr.sim.sensors import cbrng

            tn = snap.tno[cand[hi]]
            ch = ((((int(im.sensor_no) & 0xFFFF) << 16) | (tn & 0xFFFF)) << 3)[:, None] + np.arange(3)[None, :]
            nz = np.asarray(cbrng.normal_elem(self.seed(ctx), S_PC, int(P.agent_no), int(getattr(ctx, "tick", 0)), ch), float).reshape(-1, 3)
            z[hi] = snap.pos[cand[hi]] + sig[hi, None] * nz
        res = np.empty((m, K.POST_N))
        res[:, 0], res[:, 1], res[:, 2] = k_los, n_eff, p
        res[:, 3] = np.where(has, sig, np.nan)
        codes = np.empty((m, 4), np.int64)
        codes[:, 0], codes[:, 1], codes[:, 2], codes[:, 3] = lim, level, cond, has
        return _Eval(kind, cand, core, res, codes, z, n_req, deferred, int(hi.size))

    # ------------------------------------------------------------ 对状态、事件与检测环
    @staticmethod
    def _img_names(P: Platform) -> set[str]:
        return {im.name for im in P.imaging}

    def _pair_register(self, key: tuple[int, str, int], s: int) -> PairView:
        ps = self.pairs[key] = PairView(self.ptab, s)
        an = self._asn.get(key[0])
        if an is None:
            an = self._asn[key[0]] = {}
        an[key[1]] = an.get(key[1], 0) + 1
        for idx, k in ((self._apairs, key[0]), (self._tpairs, key[2])):
            a = idx.get(k)
            if a is None:
                a = idx[k] = {}
            a[key] = None
        m = self._asmap.get((key[0], key[1]))
        if m is None:
            m = self._asmap[(key[0], key[1])] = {}
        m[key[2]] = s
        sm = self._smaps.get((key[0], key[1]))
        if sm is not None and key[2] < sm.shape[0]:
            sm[key[2]] = s
        else:
            sm = self._smaps.get((key[0], key[1]))
            new = np.full(max(64, key[2] + 1, 0 if sm is None else 2 * sm.shape[0]), -1, np.int64)
            if sm is not None:
                new[:sm.shape[0]] = sm
            new[key[2]] = s
            self._smaps[(key[0], key[1])] = new
        return ps

    def _pair_new(self, key: tuple[int, str, int]) -> PairView:
        return self._pair_register(key, self.ptab.alloc(key[2], key[0]))

    def _pair_del(self, key: tuple[int, str, int]) -> None:
        ps = self.pairs.pop(key, None)
        if ps is None:
            return
        an = self._asn.get(key[0])
        if an is not None:
            c = an.get(key[1], 0) - 1
            if c > 0:
                an[key[1]] = c
            else:
                an.pop(key[1], None)
                if not an:
                    del self._asn[key[0]]
        for idx, k in ((self._apairs, key[0]), (self._tpairs, key[2])):
            a = idx.get(k)
            if a is not None:
                a.pop(key, None)
                if not a:
                    del idx[k]
        m = self._asmap.get((key[0], key[1]))
        if m is not None:
            m.pop(key[2], None)
            if not m:
                del self._asmap[(key[0], key[1])]
        sm = self._smaps.get((key[0], key[1]))
        if sm is not None and key[2] < sm.shape[0]:
            sm[key[2]] = -1
        self.ptab.release(ps.s)

    def _mark(self, tno: int) -> None:
        """识别物 tno 的汇总列需要重算（下一次 `_summary` 只重算被标记的识别物）。"""
        self._dirty = True
        self._dirty_t.add(int(tno))

    def _step_pair(self, key: tuple[int, str, int], P: Platform, im: Imaging, i: int | None, snap: TargetSnap, t: float, t_ns: int, ctx: Any,
                   level_m: int, cond: bool, deferred: bool, lim: str | None) -> None:
        """已有对的一次无测量推进（Python 路径：视锥外与传感器缺失，保留上次测量）。"""
        ps = self.pairs.get(key)
        if ps is None:
            ps = self._pair_new(key)
        prev_lv, prev_cap = ps.level, ps.cap
        trans = step(ps, t, int(level_m), bool(cond), deferred=deferred, reason=lim if not cond else None)
        if ps.level != prev_lv or (ps.cap in _CAPSET) != (prev_cap in _CAPSET):
            self._mark(key[2])
        if trans:
            self._transitions(trans, ps, key, P, im, i, snap, t_ns, ctx)
        if not cond and not ps.active:
            self._pair_del(key)

    def _transitions(self, trans: list, ps: PairState, key: tuple[int, str, int], P: Platform, im: Imaging, i: int | None, snap: TargetSnap,
                     t_ns: int, ctx: Any) -> None:
        tid = snap.ids[i] if i is not None else self._tid_of(key[2], snap)
        last = PR.last_dict(ps) if isinstance(ps, PairView) else (ps.last or {})   # 最近测量（一次读出）
        skind = im.spec.kind if im.spec is not None else str(last.get("kind") or "eo")
        rng: Any = False
        for tr in trans:
            base = {"uav": P.uav, "sensor": im.name, "target": tid}
            if tr.kind != "capture_lost" and rng is False:
                rng = rnd(self._range(P, im, snap, i), 1)
            if tr.kind == "level":
                data = {**base, "level_from": LEVEL_NAMES[tr.level_from], "level_to": LEVEL_NAMES[tr.level_to], "n_eff": rnd(last.get("n_eff"), 2),
                        "n_req": rnd(ps.n_req, 2), "range_m": rng, "gsd_m": rnd(last.get("gsd"), 4),
                        "p": rnd(last.get("p"), 3), "limiting": last.get("lim")}
                self._emit(ctx, "perception.level", 0, t_ns, data, P.uav, critical=False)
                self._ring(t_ns, "level", P, im.name, skind, data, last)
            elif tr.kind == "capture":
                data = {**base, "level_from": LEVEL_NAMES[tr.level_from], "level_to": LEVEL_NAMES[tr.level_to], "n_eff": rnd(last.get("n_eff"), 2),
                        "n_req": rnd(ps.n_req, 2), "range_m": rng, "gsd_m": rnd(last.get("gsd"), 4),
                        "p": rnd(last.get("p"), 3), "limiting": last.get("lim"), "level_req": ps.level_req,
                        "t_hold_s": float(LEVELS["capture"]["t_hold_s"])}
                self._emit(ctx, "perception.capture", 1, t_ns, data, P.uav, critical=True)
                self._ring(t_ns, "capture", P, im.name, skind, data, last)
                self._mark(key[2])
            else:
                data = {**base, "reason": tr.reason or "fov", "captured_s": rnd(tr.captured_s, 2),
                        "t_last_ok_ns": None if tr.t_last_ok is None else round(tr.t_last_ok * 1e9)}
                self._emit(ctx, "perception.capture_lost", 1, t_ns, data, P.uav, critical=True)
                self._ring(t_ns, "capture_lost", P, im.name, skind, {**data, "level_from": LEVEL_NAMES[ps.level],
                                                                           "level_to": LEVEL_NAMES[ps.level]}, last)
                self._mark(key[2])

    def _tr_rows(self, trs: list, ps: PairView, key: tuple[int, str, int], P: Platform, im: Imaging, i: int | None, snap: TargetSnap,
                 t_ns: int, ctx: Any, rr: NDArray | None = None) -> None:
        """`_transitions` 的表行版本（核给出的转移行 [r, kind, level_from, level_to, 原因码, captured_s, t_last_ok]）：对行的 PI、PF 一次
        读出，事件字段与检测环记录（键序与取值）与 `_transitions` 逐项相同，不经 Transition 与 last_dict。"""
        tab, s = ps.tab, ps.s
        pi = tab.PI[s].tolist()
        pf = tab.PF[s].tolist()
        tid = snap.ids[i] if i is not None else self._tid_of(key[2], snap)
        if pi[_I_HASLAST]:
            lc, kc = pi[_I_LIM], pi[_I_KIND]
            lim = None if lc < 0 else LIMITING[lc]
            n_eff, gsd, pp = _rnd(pf[_F_NEFF], 2), _rnd(pf[_F_GSD], 4), _rnd(pf[_F_P], 3)
            if pi[_I_HASMEAS]:
                meas = [_rnd(x, 2) for x in pf[_F_Z0:_F_Z2 + 1]]
                sig = _rnd(pf[_F_SIG], 3)
            else:
                meas = sig = None
            skind = im.spec.kind if im.spec is not None else str((None if kc < 0 else _KIND_NAMES[kc]) or "eo")
        else:
            lim = n_eff = gsd = pp = meas = sig = None
            skind = im.spec.kind if im.spec is not None else "eo"
        n_req = _rnd(pf[_F_NREQ], 2)
        uav, name = P.uav, im.name
        rng: Any = False
        for row in trs:
            kind = int(row[1])
            if kind != 2 and rng is False:
                rng = _rnd(self._range(P, im, snap, i) if rr is None or i is None else float(rr[i]), 1)
            if kind == 2:
                tlo = row[6]
                data = {"uav": uav, "sensor": name, "target": tid, "reason": (None if row[4] < 0 else LIMITING[int(row[4])]) or "fov",
                        "captured_s": _rnd(row[5], 2), "t_last_ok_ns": None if tlo != tlo else round(float(tlo) * 1e9)}
                self._emit(ctx, "perception.capture_lost", 1, t_ns, data, uav, critical=True)
                ln = LEVEL_NAMES[pi[_I_LEVEL]]
                self.ring.push_rec({"seq": 0, "t_sim_ns": int(t_ns), "kind": "capture_lost", "uav": uav, "sensor": name, "sensor_kind": skind,
                                    "target": tid, "track_id": None, "bearing_id": None, "level_from": ln, "level_to": ln, "n_eff": None,
                                    "n_req": None, "p": None, "range_m": None, "gsd_m": None, "az_rad": None, "el_rad": None, "vr_mps": None,
                                    "snr_db": None, "limiting": None, "reason": data["reason"], "meas_enu_m": meas, "sigma_m": sig})
                self._mark(key[2])
                continue
            lf, lt = LEVEL_NAMES[int(row[2])], LEVEL_NAMES[int(row[3])]
            data = {"uav": uav, "sensor": name, "target": tid, "level_from": lf, "level_to": lt, "n_eff": n_eff, "n_req": n_req, "range_m": rng,
                    "gsd_m": gsd, "p": pp, "limiting": lim}
            if kind == 1:
                data["level_req"] = _LREQ_NAMES[pi[_I_LREQ]]
                data["t_hold_s"] = _T_HOLD_S
                self._emit(ctx, "perception.capture", 1, t_ns, data, uav, critical=True)
            else:
                self._emit(ctx, "perception.level", 0, t_ns, data, uav, critical=False)
            self.ring.push_rec({"seq": 0, "t_sim_ns": int(t_ns), "kind": "capture" if kind == 1 else "level", "uav": uav, "sensor": name,
                                "sensor_kind": skind, "target": tid, "track_id": None, "bearing_id": None, "level_from": lf, "level_to": lt,
                                "n_eff": n_eff, "n_req": n_req, "p": pp, "range_m": rng, "gsd_m": gsd, "az_rad": None, "el_rad": None,
                                "vr_mps": None, "snr_db": None, "limiting": lim, "reason": None, "meas_enu_m": meas, "sigma_m": sig})
            if kind == 1:
                self._mark(key[2])

    @staticmethod
    def _range(P: Platform, im: Imaging, snap: TargetSnap, i: int | None) -> float | None:
        if i is None:
            return None
        a = snap.pos[i].tolist()
        b = np.asarray(im.p, float).reshape(3).tolist()
        d0, d1, d2 = a[0] - b[0], a[1] - b[1], a[2] - b[2]
        return math.sqrt(d0 * d0 + d1 * d1 + d2 * d2)

    @staticmethod
    def _tid_of(tno: int, snap: TargetSnap) -> str | None:
        i = snap.index.get(int(tno))
        return snap.ids[i] if i is not None else None

    def _emit(self, ctx: Any, kind: str, sev: int, t_ns: int, data: dict, uav: str | None, *, critical: bool) -> None:
        """事件（≤ 50 条/s/会话；超出的 level 与 detect 合并为计数，capture 与 capture_lost 不丢）。"""
        t = t_ns * 1e-9
        if self._ev_t is None or t < self._ev_t:
            self._ev_t = t
        self._ev_tokens = min(float(EVENTS_PER_S), self._ev_tokens + (t - self._ev_t) * EVENTS_PER_S)
        self._ev_t = t
        if self._ev_tokens < 1.0 and not critical:
            self.stats["events_merged"] += 1
            return
        self._ev_tokens -= 1.0
        merged = self.stats["events_merged"]
        if merged and kind in ("perception.level", "perception.detect"):
            data = {**data, "merged": int(merged)}
            self.stats["events_merged"] = 0
        self.stats["events"] += 1
        self.events_log.append((kind, t_ns, dict(data)))
        ev = getattr(ctx, "events", None)
        if ev is not None and hasattr(ev, "emit"):
            ev.emit(kind, t_sim_ns=int(t_ns), severity=sev, uav=uav, fields=data)

    def _ring(self, t_ns: int, kind: str, P: Platform, sensor: str, sensor_kind: str, data: dict, last: Any, **extra: Any) -> None:
        meas = _lget(last, "meas")
        sig = _lget(last, "sigma")
        g = data.get
        if not extra:                                     # 成像对的转移（无雷达、声阵列字段）：同一键序，省去逐项查 extra
            self.ring.push_rec({"seq": 0, "t_sim_ns": int(t_ns), "kind": kind, "uav": P.uav, "sensor": sensor, "sensor_kind": sensor_kind,
                                "target": g("target"), "track_id": None, "bearing_id": None, "level_from": g("level_from", "none"),
                                "level_to": g("level_to", "none"), "n_eff": g("n_eff"), "n_req": g("n_req"), "p": g("p"), "range_m": g("range_m"),
                                "gsd_m": g("gsd_m"), "az_rad": None, "el_rad": None, "vr_mps": None, "snr_db": None,
                                "limiting": g("limiting") if kind != "capture_lost" else None, "reason": g("reason") if kind == "capture_lost" else None,
                                "meas_enu_m": [rnd(x, 2) for x in meas] if meas is not None else None,
                                "sigma_m": rnd(sig, 3) if sig is not None else None})
            return
        rec = {"seq": 0, "t_sim_ns": int(t_ns), "kind": kind, "uav": P.uav, "sensor": sensor, "sensor_kind": sensor_kind, "target": data.get("target"),
               "track_id": extra.get("track_id"), "bearing_id": extra.get("bearing_id"), "level_from": data.get("level_from", "none"),
               "level_to": data.get("level_to", "none"), "n_eff": data.get("n_eff"), "n_req": data.get("n_req"), "p": data.get("p"),
               "range_m": data.get("range_m"), "gsd_m": data.get("gsd_m"), "az_rad": extra.get("az_rad"), "el_rad": extra.get("el_rad"),
               "vr_mps": extra.get("vr_mps"), "snr_db": extra.get("snr_db"), "limiting": data.get("limiting") if kind != "capture_lost" else None,
               "reason": data.get("reason") if kind == "capture_lost" else None,
               "meas_enu_m": [rnd(x, 2) for x in meas] if meas is not None else extra.get("meas_enu_m"),
               "sigma_m": rnd(sig, 3) if sig is not None else extra.get("sigma_m")}
        self.ring.push_rec(rec)

    # ------------------------------------------------------------ 雷达与 MID-360
    def _center_los(self, a: NDArray, B: NDArray, H: NDArray, t_ns: int) -> tuple[NDArray, NDArray]:
        if len(B) == 0:
            return np.zeros(0), np.zeros(0, bool)
        out = self.los.get_batch(np.broadcast_to(a, (len(B), 3)), B, H, "M19", t_ns, "center")
        self.stats["los_requests"] += len(B)
        vis = np.array([0.0 if o.deferred else o.k_los for o in out])
        dfr = np.array([o.deferred for o in out], bool)
        self.stats["deferred"] += int(dfr.sum())
        return vis, dfr

    def _radar(self, P: Platform, N: NonImaging, snap: TargetSnap, env: EnvSample, ctx: Any, t: float, t_ns: int, meas: dict) -> dict:
        sp: RadarSpec = N.spec
        out = {"uav": P.uav, "t_sim_ns": t_ns, "sensor": N.name, "tracks": [], "clutter_gated": False}
        if snap.n == 0:
            return out
        d = snap.pos + np.array([0.0, 0.0, 0.5]) * snap.size[:, 2:3] - N.p[None, :]
        R = np.linalg.norm(d, axis=1)
        u = d @ N.R
        az_m = np.arctan2(u[:, 1], u[:, 0])
        el_m = np.arctan2(u[:, 2], np.hypot(u[:, 0], u[:, 1]))
        inside = (sp.r_max_m >= R) & (R > 0.5) & (np.abs(az_m) <= math.radians(sp.fov_az_deg)) & (np.abs(el_m) <= math.radians(sp.fov_el_deg))
        cand = np.flatnonzero(inside)
        vis, dfr = self._center_los(N.p, snap.pos[cand], snap.size[cand, 2], t_ns)
        agent = P.agent_no
        seen = set()
        # 视线可见且未延期的候选：SNR、径向速度、擦地角与地杂波门限一次向量化（与逐个标量求值逐位相同）
        cl = cand.tolist()
        pj = [j for j in range(len(cl)) if not dfr[j] and vis[j] >= 1.0]
        pre: dict[int, tuple[float, float, bool, bool]] = {}
        if pj:
            ii = cand[pj]
            rcs_v = snap.rcs[ii]
            sw = snap.swerling1[ii]
            u_v = np.array([self._uniform(ctx, agent, N.sensor_no, int(snap.tno[i]), 7) if w else 1.0 for i, w in zip(ii.tolist(), sw.tolist(),
                                                                                                                       strict=True)])
            sig_v = np.where(sw, -rcs_v * np.log(np.maximum(u_v, 1e-12)), rcs_v)
            Ri = R[ii]
            snr_v = np.asarray(NI.radar_snr_db(sp, sig_v, Ri, env.rain_mmph), float).reshape(-1)
            LU = d[ii] / Ri[:, None]
            vr_v = ((snap.vel[ii] - P.vel[None, :]) * LU).sum(axis=1)
            gx = -LU[:, 2]
            graze_v = np.arcsin(np.where(np.isnan(gx), 1.0, np.clip(gx, -1.0, 1.0)))
            cf_v = snap.ground[ii] & (np.abs(vr_v) < sp.v_clutter_mps) & ~np.asarray(NI.clutter_ok(sp, rcs_v, Ri, graze_v), bool).reshape(-1)
            for q, j in enumerate(pj):
                pre[j] = (float(snr_v[q]), float(vr_v[q]), bool(cf_v[q]), bool(snr_v[q] >= sp.snr_d_db and not cf_v[q]))
        for j, i in enumerate(cl):
            tno = int(snap.tno[i])
            key = (agent, N.name, tno)
            stt = self.rad.get(key)
            if dfr[j]:
                if stt is not None:
                    seen.add(tno)
                continue
            if vis[j] < 1.0:
                continue
            snr, vr, clutter_fail, det = pre[j]
            if clutter_fail:
                out["clutter_gated"] = True
            if not det:
                continue
            seen.add(tno)
            if stt is None:
                ser = self.serial.get(agent, 0)
                self.serial[agent] = ser + 1
                stt = self.rad[key] = {"track_id": _hash32(agent * 65536 + ser), "level": 0, "r_since": None}
            if snr >= sp.snr_r_db:
                stt["r_since"] = t if stt["r_since"] is None else stt["r_since"]
            else:
                stt["r_since"] = None
            lv = 2 if stt["r_since"] is not None and t - stt["r_since"] >= sp.dwell_r_s - 1e-6 else 1
            nz = self._noise3(ctx, agent, N.sensor_no, tno)
            az_w = math.atan2(d[i, 1], d[i, 0])
            el_w = math.atan2(d[i, 2], math.hypot(d[i, 0], d[i, 1]))
            rm = float(R[i]) + sp.sigma_range_m * nz[0]
            azm = az_w + math.radians(sp.sigma_az_deg) * nz[1]
            elm = el_w + math.radians(sp.sigma_el_deg) * nz[2]
            vrm = vr + sp.vel_res_mps * 0.5 * float(self._noise3(ctx, agent, N.sensor_no + 64, tno)[0])
            z = N.p + rm * np.array([math.cos(elm) * math.cos(azm), math.cos(elm) * math.sin(azm), math.sin(elm)])
            sig = math.sqrt(sp.sigma_range_m ** 2 + (float(R[i]) * math.radians(sp.sigma_az_deg)) ** 2 + (float(R[i]) * math.radians(sp.sigma_el_deg)) ** 2)
            prev = meas.get(tno)
            if prev is None or sig < prev[0]:
                meas[tno] = (sig, z, float(R[i]))
            out["tracks"].append({"track_id": int(stt["track_id"]), "range_m": rnd(rm, 2), "az_rad": rnd(azm, 4), "el_rad": rnd(elm, 4),
                                  "vr_mps": rnd(vrm, 2), "snr_db": rnd(snr, 1), "level": LEVEL_NAMES[lv],
                                  "class_hint": snap.cls[i] if lv >= 2 else None})
            if lv != stt["level"]:
                data = {"uav": P.uav, "sensor": N.name, "sensor_kind": "radar", "track_id": int(stt["track_id"]), "level": LEVEL_NAMES[lv]}
                self._emit(ctx, "perception.detect", 0, t_ns, data, P.uav, critical=False)
                self._ring(t_ns, "detect", P, N.name, "radar", {"target": None, "level_from": LEVEL_NAMES[stt["level"]], "level_to": LEVEL_NAMES[lv],
                                                                "range_m": rnd(rm, 1)}, {}, track_id=int(stt["track_id"]), az_rad=rnd(azm, 4),
                           el_rad=rnd(elm, 4), vr_mps=rnd(vrm, 2), snr_db=rnd(snr, 1), meas_enu_m=[rnd(x, 2) for x in z], sigma_m=rnd(sig, 2))
                if stt["level"] < 1 <= lv:
                    self._mark(tno)
                stt["level"] = lv
            stt["last_t"] = t
            self._cue(P, snap, i, t, ctx, z)
        for key in [kk for kk in self.rad if kk[0] == agent and kk[1] == N.name and kk[2] not in seen]:
            st0 = self.rad.pop(key)
            if st0.get("level", 0):
                self._mark(key[2])
        return out

    def _lidar(self, P: Platform, N: NonImaging, snap: TargetSnap, env: EnvSample, ctx: Any, t: float, t_ns: int, meas: dict) -> list[dict]:
        sp: LidarSpec = N.spec
        if snap.n == 0:
            return []
        c = snap.pos + np.array([0.0, 0.0, 0.5]) * snap.size[:, 2:3]
        d = c - N.p[None, :]
        R = np.linalg.norm(d, axis=1)
        u = d @ N.R
        el = np.degrees(np.arctan2(u[:, 2], np.hypot(u[:, 0], u[:, 1])))
        pre = (sp.blind_m <= R) & (sp.hard_max_m >= R) & (el >= sp.fov_v_deg[0]) & (el <= sp.fov_v_deg[1])
        cand = np.flatnonzero(pre)
        if cand.size == 0:
            return []
        t905 = self._tau(ctx, N.p, c[cand], t_ns, 905.0, env)
        rmax = np.asarray(NI.lidar_r_max(sp, snap.rho905[cand] * t905 ** 2), float).reshape(-1)
        keep = R[cand] <= rmax
        cand = cand[keep]
        if cand.size == 0:
            return []
        _R, eps, beta = angles(np.broadcast_to(N.p, (cand.size, 3)), snap.pos[cand], snap.yaw[cand])
        dc = np.asarray(critical_dim(snap.size[cand], eps, beta), float).reshape(cand.size)
        npts = np.asarray(NI.lidar_points(sp, dc * dc, R[cand], snap.occ[cand]), float).reshape(-1)
        vis, dfr = self._center_los(N.p, snap.pos[cand], snap.size[cand, 2], t_ns)
        out = []
        agent = P.agent_no
        for j, i in enumerate(cand.tolist()):
            if dfr[j] or vis[j] < 1.0:
                continue
            n1 = float(npts[j])
            lv = 2 if n1 >= sp.n_r else 1 if n1 >= sp.n_d else 0
            out.append({"target": snap.ids[i], "n_1s": rnd(n1, 1), "level": LEVEL_NAMES[lv]})
            tno = int(snap.tno[i])
            key = (agent, N.name, tno)
            stt = self.lid.get(key, {"level": 0})
            if lv >= 1:
                z = snap.pos[i] + LIDAR_SIGMA_M * self._noise3(ctx, agent, N.sensor_no, tno)
                prev = meas.get(tno)
                if prev is None or prev[0] > LIDAR_SIGMA_M:
                    meas[tno] = (LIDAR_SIGMA_M, z, float(R[i]))
            if lv != stt["level"]:
                data = {"uav": P.uav, "sensor": N.name, "sensor_kind": "lidar", "track_id": None, "level": LEVEL_NAMES[lv]}
                self._emit(ctx, "perception.detect", 0, t_ns, data, P.uav, critical=False)
                self._ring(t_ns, "detect", P, N.name, "lidar", {"target": None, "level_from": LEVEL_NAMES[stt["level"]], "level_to": LEVEL_NAMES[lv],
                                                                "range_m": rnd(R[i], 1)}, {})
                self._mark(tno)
            if lv:
                self.lid[key] = {"level": lv}
            else:
                self.lid.pop(key, None)
        return out

    # ------------------------------------------------------------ 声阵列（2 Hz）
    def _peek_center(self, a: NDArray, b: NDArray, h: float, t_ns: int) -> bool:
        """缓存中的中心射线是否被遮挡（不发射新射线，PRD §6.8）；无缓存条目为未遮挡。"""
        los = self.los
        d = getattr(los, "d", None)
        key_fn = getattr(los, "_key", None)
        if d is None or not callable(key_fn):
            return False
        e = d.get(key_fn(a, b))
        if e is None or not (0 <= t_ns - e[2] <= getattr(los, "stale_ns", 2_000_000_000)):
            return False
        return bool(e[0] & 0b010) and not bool(e[1] & 0b010)

    def _acoustic(self, S: Any, ctx: Any, act: NDArray, snap: TargetSnap, env: EnvSample, bg: NDArray, t: float, t_ns: int) -> None:
        plats = [P for P in self._platforms(S, ctx, act, imaging=False, kinds=("acoustic",), nonimg_pose=snap.n > 0)
                 if any(N.kind == "acoustic" for N in P.nonimg)]
        if not plats:
            return
        self.stats["acoustic_runs"] += 1
        trt = self.trt
        lw = []
        for i in range(snap.n):
            try:
                lw.append(trt.sound_bands_now(snap.ids[i], t_ns))
            except Exception:
                lw.append(None)
        vi = [i for i, v in enumerate(lw) if v is not None]       # 有声源的识别物（下标升序）
        LW = np.array([lw[i] for i in vi], float).reshape(len(vi), -1) if vi else None
        lws = (lw, np.array(vi, np.int64), LW)
        streams = getattr(ctx, "streams", None) or {}
        want = streams.get("acoustic")
        want_s = set(int(x) for x in np.asarray(want).reshape(-1).tolist()) if want is not None and len(want) else set()
        items = {}
        for P in plats:
            self.agent_uav[P.agent_no] = P.uav
            for N in P.nonimg:
                if N.kind != "acoustic":
                    continue
                self._acoustic_one(P, N, snap, lws, env, bg, ctx, t, t_ns)
                if P.agent_no in want_s and P.uav not in items:
                    items[P.uav] = self.acoustic_last[P.uav]
        if items:
            from awr.contracts import bus_keys

            self.pub.put(ctx, str(bus_keys.STATE_ACOUSTIC), t_ns, items)

    def _acoustic_one(self, P: Platform, N: NonImaging, snap: TargetSnap, lws: tuple, env: EnvSample, bg: NDArray, ctx: Any, t: float,
                      t_ns: int) -> None:
        """一机一声阵列（2 Hz）。各识别物的带 SNR 一次向量化（`NI.acoustic_snr_matrix`，与逐个 `acoustic_snr_bands` 逐位相同；斜距
        `sqrt(Σd²)` 与逐个 `np.linalg.norm` 逐位相同）、缓存中心射线的遮挡一次批量查询（`LosCache.blocked_batch`），只对过门限的
        识别物逐个生成方位、事件与引导（次序同前：识别物下标升序）。"""
        sp: AcousticSpec = N.spec
        ego = P.lw_bands if P.lw_bands is not None else np.full(24, 60.0)
        r_ego = float(sp.r_ego_m.get(P.frame, 0.5))
        nr = float(sp.nr_db.get(P.frame, 25.0))
        from awr.sim.targets.sound import A_W, BANDS_HZ

        ego_a = 10.0 * math.log10(float(np.sum(10.0 ** ((ego - 20.0 * math.log10(r_ego) - 11.0 - nr + A_W) / 10.0))))
        out = {"uav": P.uav, "t_sim_ns": t_ns, "sensor": N.name, "ego_noise_dba": rnd(ego_a, 1), "bearings": []}
        agent = P.agent_no
        seen = set()
        _lw, vi, LW = lws
        det: list[tuple[int, NDArray, NDArray, float]] = []
        if vi.size:
            D = snap.pos[vi] - N.p[None, :]
            Rv = np.sqrt((D * D).sum(axis=1))
            ok = ~(Rv < 0.5)
            if ok.any():
                sel = vi[ok]
                los = self.los
                bb = getattr(los, "blocked_batch", None)
                if bb is not None:
                    blk = bb(N.p, snap.pos[sel], t_ns, "center")
                else:
                    blk = np.array([self._peek_center(N.p, snap.pos[i], float(snap.size[i, 2]), t_ns) for i in sel.tolist()], bool)
                SNR = NI.acoustic_snr_matrix(sp, LW[ok], Rv[ok], ego, bg, frame=P.frame, blocked=blk)
                mx = SNR.max(axis=1)
                hit = ~(mx < sp.snr_d_db)
                Dk = D[ok]
                det.extend((int(sel[q]), Dk[q], SNR[q], float(mx[q])) for q in np.flatnonzero(hit).tolist())
        for i, d, snr, mx in det:
            tno = int(snap.tno[i])
            harm = NI.harmonic_bands(float(snap.f0[i]))
            lv = 2 if int(np.sum((snr >= sp.snr_r_db) & harm)) >= sp.n_harm else 1
            key = (agent, N.name, tno)
            stt = self.aco.get(key)
            if stt is None:
                bid = len([k for k in self.aco if k[0] == agent and k[1] == N.name])
                used = {v["bearing_id"] for k, v in self.aco.items() if k[0] == agent and k[1] == N.name}
                while bid in used:
                    bid += 1
                stt = self.aco[key] = {"bearing_id": bid, "level": 0}
            seen.add(tno)
            nz = self._noise3(ctx, agent, N.sensor_no, tno)
            az = math.atan2(d[1], d[0]) + math.radians(sp.bearing_sigma_deg) * nz[0]
            el = math.atan2(d[2], math.hypot(d[0], d[1])) + math.radians(sp.bearing_sigma_deg) * nz[1]
            bands = [{"fc_hz": float(BANDS_HZ[b]), "snr_db": rnd(float(snr[b]), 1)} for b in range(24) if np.isfinite(snr[b]) and snr[b] >= 0.0]
            out["bearings"].append({"bearing_id": int(stt["bearing_id"]), "az_rad": rnd(az, 4), "el_rad": rnd(el, 4), "level": LEVEL_NAMES[lv],
                                    "bands": bands, "harmonic_f0_hz": float(snap.f0[i]) if lv >= 2 and snap.f0[i] > 0 else None})
            if lv != stt["level"]:
                data = {"uav": P.uav, "sensor": N.name, "sensor_kind": "acoustic", "bearing_id": int(stt["bearing_id"]), "level": LEVEL_NAMES[lv]}
                self._emit(ctx, "perception.detect", 0, t_ns, data, P.uav, critical=False)
                self._ring(t_ns, "detect", P, N.name, "acoustic", {"target": None, "level_from": LEVEL_NAMES[stt["level"]],
                                                                   "level_to": LEVEL_NAMES[lv]}, {}, bearing_id=int(stt["bearing_id"]),
                           az_rad=rnd(az, 4), el_rad=rnd(el, 4), snr_db=rnd(mx, 1))
                stt["level"] = lv
                self._mark(tno)
            self._cue(P, snap, i, t, ctx, None, az=az, el=el)
        for key in [k for k in self.aco if k[0] == agent and k[1] == N.name and k[2] not in seen]:
            self.aco.pop(key)
            self._mark(key[2])
        self.acoustic_last[P.uav] = out

    # ------------------------------------------------------------ 云台引导（M19-FR-020）
    def cue_enabled(self, P: Platform) -> bool:
        if P.cue == 1:
            return True
        if P.cue == 2:
            return False
        return bool(self.cue_on.get(P.uav, False))

    def _cue(self, P: Platform, snap: TargetSnap, i: int, t: float, ctx: Any, z: NDArray | None, *, az: float | None = None,
             el: float | None = None) -> None:
        if not self.cue_enabled(P):
            return
        tno = int(snap.tno[i])
        pairs = self.pairs
        for kk in self._apairs.get(P.agent_no, ()):
            if kk[2] == tno and pairs[kk].cap in _CAPSET:
                return
        last = self.cue_t.get(P.agent_no)
        if last is not None and t - last < CUE_HOLD_S - 1e-9:
            return
        rt = self.adapter.runtime()
        if rt is None:
            return
        trk = self.tracks.get(tno, t)
        ok = False
        try:
            if trk is not None:
                ok = bool(rt.cue_point(P.slot, np.asarray(trk.pos, float)))
            elif z is not None:
                ok = bool(rt.cue_point(P.slot, np.asarray(z, float)))
            elif az is not None:
                from .params import CUE_RANGE_M

                ok = bool(rt.cue(P.slot, math.degrees(az), math.degrees(el or 0.0), CUE_RANGE_M, None))
        except Exception:
            ok = False
        self.cue_t[P.agent_no] = t
        if ok:
            self.stats["cue"] += 1

    # ------------------------------------------------------------ 汇总与清理
    def _prune(self, snap: TargetSnap, act: NDArray, S: Any) -> None:
        """删除识别物已不在快照或机体已不在场的对状态、雷达/MID-360/声阵列状态、航迹与帧。识别物集合（快照静态键）与在场机体的
        agent_no 都未变化时没有可删的项（对、航迹与帧只为快照内的识别物与在场机体建立），直接返回。"""
        ag = S.agent_no[act] if S is not None and hasattr(S, "agent_no") else None
        pk = (snap.skey, None if ag is None else ag.tobytes())
        if self._prune_key is not None and pk[0] is self._prune_key[0] and pk[1] == self._prune_key[1] and snap.skey is not None:
            return
        self._prune_key = pk
        live_t = set(snap.tno_l) if snap.tno_l else set(int(x) for x in snap.tno)
        live_a = set(ag.tolist()) if ag is not None else None
        for k in [k for k in self.pairs if k[2] not in live_t or (live_a is not None and k[0] not in live_a)]:
            self._pair_del(k)
            self._mark(k[2])
        for dct in (self.rad, self.lid, self.aco):
            for k in [k for k in dct if k[2] not in live_t or (live_a is not None and k[0] not in live_a)]:
                dct.pop(k, None)
                self._mark(k[2])
        for tn in [tn for tn in list(self.tracks.tr) if tn not in live_t]:
            self.tracks.remove(tn)
        if live_a is not None:
            for uav in [u for u, f in self.frames.items() if f.get("_agent") not in live_a]:
                self.frames.pop(uav, None)
                self.radar_last.pop(uav, None)
                self.acoustic_last.pop(uav, None)
                self.lidar_last.pop(uav, None)

    def best_of(self, tno: int) -> tuple[int, int, bool, int, PairState | None]:
        """(best_level, best_sensor 码, captured, capture_uav_no, 最早捕获的对)。"""
        best, sens, cap, uno, first = 0, 0, False, 0xFFFF, None
        pairs = self.pairs
        for kk in self._tpairs.get(int(tno), ()):          # 该识别物的对（与 pairs 同一插入次序）
            ag = kk[0]
            ps = pairs[kk]
            if ps.level > best:
                kind = _lget(ps.last, "kind") or "eo"
                best, sens = ps.level, SENSOR_CODE.get(kind, 1)
            if ps.cap in (CAPTURED, HOLDING):
                cap = True
                if first is None or ps.since < first.since:
                    first, uno = ps, ag
        for lv, code in self._nonimg_levels().get(int(tno), ()):
            if lv > best:
                best, sens = lv, code
        return best, sens, cap, uno, first

    def _nonimg_levels(self) -> dict[int, list[tuple[int, int]]]:
        """识别物 → 雷达、MID-360、声阵列各对的 (等级, 传感器码)，按 `best_of` 的遍历次序（雷达、MID-360、声阵列，各自按插入
        次序）。按（stage 调用计数、三张表的身份与条目数）缓存：M18 detail 慢任务对每个识别物调用一次 `best_of`，此前每次遍历
        三张表的全部对（D2-FX-R2-sim 第 2 次）。"""
        key = (self.stats.get("calls", 0), id(self.rad), len(self.rad), id(self.lid), len(self.lid), id(self.aco), len(self.aco))
        c = self._nil_cache
        if c is not None and c[0] == key:
            return c[1]
        out: dict[int, list[tuple[int, int]]] = {}
        for dct, kind in ((self.rad, "radar"), (self.lid, "lidar"), (self.aco, "acoustic")):
            code = SENSOR_CODE[kind]
            for (_ag, _s, tn), st in dct.items():
                out.setdefault(int(tn), []).append((int(st.get("level", 0)), code))
        self._nil_cache = (key, out)
        return out

    def _aggregate(self, only: set[int] | None) -> dict[int, list]:
        """识别物 → [best_level, best_sensor 码, captured, capture_uav_no, 最早 since]：与逐识别物 `best_of` 相同的规则（按对状态的
        插入次序取等级严格更高者，捕获取 since 最早者，再依次看雷达、MID-360、声阵列）。only 给出时只算这些识别物。"""
        agg: dict[int, list] = {}
        pairs = self.pairs
        if only is None:
            keys: list = list(pairs)
        else:
            tp = self._tpairs
            keys = [kk for tn in only for kk in tp.get(tn, ())]
        if keys and K.HAVE_NUMBA:
            uniq: dict[int, int] = {}
            tix = np.array([uniq.setdefault(kk[2], len(uniq)) for kk in keys], np.int64)
            out = np.zeros((len(uniq), 4), np.int64)
            out[:, 3] = 0xFFFF
            since = np.full(len(uniq), np.nan)
            K.agg_rows(np.array([pairs[kk].s for kk in keys], np.int64), tix, np.array([kk[0] for kk in keys], np.int64), self.ptab.PI,
                       self.ptab.PF, out, since)
            ol, sl = out.tolist(), since.tolist()
            for tn, q in uniq.items():
                o = ol[q]
                agg[tn] = [o[0], o[1], bool(o[2]), o[3], None if sl[q] != sl[q] else sl[q]]
        else:
            for kk in keys:
                ps = pairs[kk]
                tn = kk[2]
                a = agg.get(tn)
                if a is None:
                    a = agg[tn] = [0, 0, False, 0xFFFF, None]
                if ps.level > a[0]:
                    a[0], a[1] = ps.level, SENSOR_CODE.get(_lget(ps.last, "kind") or "eo", 1)
                if ps.cap in _CAPSET:
                    a[2] = True
                    if a[4] is None or ps.since < a[4]:
                        a[4], a[3] = ps.since, kk[0]
        for dct, kind in ((self.rad, "radar"), (self.lid, "lidar"), (self.aco, "acoustic")):
            if not dct:
                continue
            code = SENSOR_CODE[kind]
            for (_ag, _s, tn), st in dct.items():
                if only is not None and tn not in only:
                    continue
                lv = int(st.get("level", 0))
                a = agg.get(tn)
                if a is None:
                    if lv <= 0:
                        continue
                    a = agg[tn] = [0, 0, False, 0xFFFF, None]
                if lv > a[0]:
                    a[0], a[1] = lv, code
        return agg

    def _summary_nb(self, snap: TargetSnap, c: dict) -> None:
        """numba 路径：一次核遍历全部在用的对行（kernels.summary_cols，与逐识别物 `best_of` 相同），再依次并入雷达、MID-360、
        声阵列（等级严格更高者覆盖），写全部识别物的四列（只有成像对时由核直接写）。"""
        self._dirty_all = False
        self._dirty_t = set()
        self._sum_skey = snap.skey
        n = snap.n
        bufs = self._sum_bufs
        if bufs is None or bufs[0].shape[0] != n:
            bufs = self._sum_bufs = (np.empty((n, 4), np.int64), np.empty(n), np.empty((n, 2), np.int64))
        out, since, seqb = bufs
        rows = snap.rows
        cb, cs, cc, cu = c["tgt_best_level"], c["tgt_best_sensor"], c["tgt_captured"], c["tgt_capture_uav_no"]
        merge = bool(self.rad or self.lid or self.aco)
        if rows.dtype != np.int64 or not rows.flags.c_contiguous:
            rows = np.ascontiguousarray(rows, dtype=np.int64)
        K.summary_cols(self.ptab.PI, self.ptab.PF, snap.tidx, out, since, seqb, rows, cb, cs, cc, cu, not merge)
        if not merge:
            return                                     # 只有成像对：核内已写四列
        idx = snap.index
        for dct, kind in ((self.rad, "radar"), (self.lid, "lidar"), (self.aco, "acoustic")):
            code = SENSOR_CODE[kind]
            for (_ag, _s, tn), st in dct.items():
                q = idx.get(tn)
                if q is None:
                    continue
                lv = int(st.get("level", 0))
                if lv > out[q, 0]:
                    out[q, 0], out[q, 1] = lv, code
        cb[rows] = out[:, 0]
        cs[rows] = out[:, 1]
        cc[rows] = out[:, 2]
        cu[rows] = out[:, 3]

    def _summary(self, snap: TargetSnap) -> None:
        """识别物块的 M19 列（best_level、best_sensor、captured、capture_uav_no）。只重算被标记（`_mark`）的识别物；识别物集合变化、
        实体表更换与重置时全部重算。结果与逐识别物 `best_of` 相同。"""
        self._dirty = False
        tab = self.tab
        if tab is None or snap.n == 0:
            return
        c = tab.cols
        if "tgt_best_level" not in c:
            return
        if K.HAVE_NUMBA and snap.tidx is not None:
            self._summary_nb(snap, c)
            return
        full = self._dirty_all or snap.skey is None or snap.skey is not self._sum_skey
        only = None if full else self._dirty_t
        self._dirty_all = False
        self._dirty_t = set()
        self._sum_skey = snap.skey
        agg = self._aggregate(only)
        cb, cs, cc, cu = c["tgt_best_level"], c["tgt_best_sensor"], c["tgt_captured"], c["tgt_capture_uav_no"]
        zero = (0, 0, False, 0xFFFF, None)
        if only is None:
            todo: Any = zip(snap.tno_l or snap.tno.tolist(), snap.rows.tolist(), strict=True)
        else:
            idx = snap.index
            todo = [(tn, int(snap.rows[idx[tn]])) for tn in sorted(only) if tn in idx]
        for tno, row in todo:
            a = agg.get(tno, zero)
            cb[row] = a[0]
            cs[row] = a[1]
            cc[row] = 1 if a[2] else 0
            cu[row] = a[3]

    def _publish(self, ctx: Any, processed: list[Platform], t_ns: int) -> None:
        streams = getattr(ctx, "streams", None)
        if not streams:
            return
        for name, key, src in (("perception", "STATE_PERCEPTION", self.frames), ("radar", "STATE_RADAR", self.radar_last)):
            want = streams.get(name)
            if want is None or len(want) == 0:
                continue
            want_s = set(int(x) for x in np.asarray(want).reshape(-1).tolist())
            items = {}
            for P in processed:
                if P.agent_no in want_s and P.uav in src and src[P.uav] is not None:
                    items[P.uav] = self.frame_for(P.uav) if name == "perception" else src[P.uav]
            if items:
                from awr.contracts import bus_keys

                self.pub.put(ctx, str(getattr(bus_keys, key)), t_ns, items)

    # ------------------------------------------------------------ 只读接口（REST、M18 detail、M20）
    def _frame(self, uav: str) -> dict | None:
        """展开（并缓存）机体的最新帧：sensors、hud 与 budget 在处理时只记下数组与标量。"""
        f = self.frames.get(uav)
        if f is None:
            return None
        lz = f.get("_lazy")
        if lz is None:
            return f
        sensors, (rtm, rs, dr) = lz
        out: list[dict] = []
        hud = None
        for sf in sensors:
            fr, h = sf.build()
            out.append(fr)
            if hud is None and h is not None:
                hud = h
        g = {"uav": f["uav"], "t_sim_ns": f["t_sim_ns"], "sensors": out, "hud": hud, "budget": {"rays_tick_max": rtm, "rays_s": rnd(rs, 1), "dropped": dr},
             "_agent": f["_agent"]}
        self.frames[uav] = g
        return g

    def frame_for(self, uav: str) -> dict | None:
        f = self._frame(uav)
        if f is None:
            return None
        return {k: v for k, v in f.items() if not k.startswith("_")}

    def vehicle(self, uav: str) -> dict | None:
        """SB-38：`uav/{id}/perception` 的最新载荷，另附 radar、acoustic、lidar。"""
        f = self.frame_for(uav)
        if f is None and uav not in self.acoustic_last:
            return None
        out = f if f is not None else {"uav": uav, "t_sim_ns": int(self.acoustic_last[uav]["t_sim_ns"]), "sensors": []}
        out = dict(out)
        out["radar"] = self.radar_last.get(uav)
        out["acoustic"] = self.acoustic_last.get(uav)
        out["lidar"] = self.lidar_last.get(uav)
        return out

    def capture_state(self, target: str, level_req: str = "R") -> CaptureState:
        """明确捕获指示（M20 的 G(t)）：存在一对处于 CAPTURED 或 HOLDING 且其 N_req 不低于所需等级的阈值。"""
        snap = self._snap
        tno = None
        if snap is not None:
            tno = next((int(snap.tno[i]) for i in range(snap.n) if snap.ids[i] == target), None)
        if tno is None and self.trt is not None:
            T = self.trt._by_id(target)
            tno = int(T.target_no) if T is not None else None
        if tno is None:
            return CaptureState(False, (), None, None)
        thr = N_P90.get(level_req, N_P90["R"])
        by: list[tuple[float, str]] = []
        t_last = None
        best = 0
        pairs = self.pairs
        for kk in self._tpairs.get(int(tno), ()):          # 该识别物的对（与 pairs 同一插入次序）
            ag = kk[0]
            ps = pairs[kk]
            best = max(best, ps.level)
            if ps.cap in (CAPTURED, HOLDING) and ps.n_req >= thr - 1e-9:
                by.append((ps.since, self._uav_of_agent(ag)))
                if ps.t_last_ok is not None:
                    t_last = ps.t_last_ok if t_last is None else max(t_last, ps.t_last_ok)
        by.sort()
        return CaptureState(bool(by), tuple(u for _s, u in by), round(by[0][0] * 1e9) if by else None,
                            None if t_last is None else round(t_last * 1e9), LEVEL_NAMES[best])

    def _uav_of_agent(self, agent: int) -> str:
        return self.agent_uav.get(int(agent), f"agent-{int(agent)}")

    def capture_detail(self, target: str) -> dict | None:
        """M18 detail 的 `capture` 段（`runtime().capture_fn`，d2-WP-05-to-M19 第 5 条）。"""
        snap = self._snap
        if snap is None:
            return None
        ix = snap.id_index
        i = ix.get(target) if ix else next((j for j in range(snap.n) if snap.ids[j] == target), None)
        if i is None:
            return None
        best, sens, cap, uno, first = self.best_of(int(snap.tno[i]))
        last = (first.last if first is not None else None) or {}
        return {"best_level": LEVEL_NAMES[best], "captured": cap, "sensor": SENSOR_OF_CODE.get(sens),
                "uav": self._uav_of_agent(uno) if uno != 0xFFFF else None, "n_eff": rnd(last.get("n_eff"), 2) if first else None,
                "n_req": rnd(first.n_req, 2) if first else None, "since_t_sim_ns": round(first.since * 1e9) if first else None}

    def track(self, target: str, t: float | None = None) -> Any:
        snap = self._snap
        if snap is None:
            return None
        i = next((j for j in range(snap.n) if snap.ids[j] == target), None)
        if i is None:
            return None
        tt = t if t is not None else (max((f["t_sim_ns"] for f in self.frames.values()), default=0) * 1e-9)
        return self.tracks.get(int(snap.tno[i]), tt, target)

    def tracks_wire(self, t: float, target: str | None = None) -> list[dict]:
        snap = self._snap
        if snap is None:
            return []
        out = []
        for i in range(snap.n):
            if target and snap.ids[i] != target:
                continue
            tr = self.tracks.get(int(snap.tno[i]), t, snap.ids[i])
            if tr is not None:
                out.append(tr.to_wire())
        return out

    def set_cue(self, uav: str, on: bool | None) -> None:
        """任务缺省的 cue 策略（M20：执行巡查、扫描、值守的机为 on；None 恢复缺省 off）。`sensor/mode{cue}` 的显式设置优先。"""
        if on is None:
            self.cue_on.pop(str(uav), None)
        else:
            self.cue_on[str(uav)] = bool(on)
