"""M18 识别物运行时：增删改（TargetService）、10 Hz stage（运动、声源、判据、状态机、行为）、只读视图与 detail（M18-FR-001 至
FR-022、FR-027；M18-M19 PRD §6、§7.1；AWR-04 §7）。

- 实体：`EntityTables["target"]`（M08，容量 32、会话上限 30 → 505）；本模块只写 M18 的 `tgt_*` 列（block.py），每行另有 Python
  侧动态状态 `TState`（运动、驻留、行为）。增删只在步边界（命令处理函数、会话装配）执行，写入于下一 tick 的 stage 生效。
- stage（`targets`，10 Hz，order 170，两档时钟同一实现：d1_250 every 25、sandbox100 every 10，由 M08 按档调度表换算）：按
  target_no 升序 → 运动（流 17 `targets_motion`）→ 声源相位 → 敏感体内外与判据（geometric 或 physical）→ 驻留与状态机 →
  广播 → 写列。参与者为我方 roster 中 armed 或 in_air 的机体（落地上锁的不计）。判据不使用随机数。
- 事件：`target.discovered`（severity 3）、`target.state`（severity 0，17 §17.7.5）；计数口径：同一告警轮次内每架机首次满足
  驻留计一次，轮次在回到 UNAWARE（或平复中再次被发现）时结束（M18-FR-015）。
- 只读接口：`view()`（TargetView，M19、M20）、`config(id)`、`state1(id)`、`keepout(id, ...)`、`sound_bands_now(id, t)`、`detail(...)`；
  D2.1（WP-19b）：`fleet(S, ctx)`（全部在场机体与参与者标记、带灯光的签名）、`quant_view(id)`（逐对读数所需的识别物状态与驻留）。
- D3 扩展点 3（AWR-05 §12.3，M23 §6.12）：`motion.mode = timeline` 的识别物每 tick 由 `set_motion_source` 登记的运动源给出位置、
  航向、速度、enabled 与 hidden，跳过 `step_motion` 与行为覆盖；`reaction_policy = exogenous` 时覆盖恒为 (None, True) 且不广播，
  判据与状态机照跑。运动源可只读调用 `state1(id)` 与 `config(id)`。缺省无运动源、无 timeline 识别物，D2 行为逐位不变。
- D2.1 判据增补（M18-FR-029、FR-030）：声学带余量计入 `hearing_band_adj_db`（缺省不修正，逐位不变）；配置 `radar_rx` 的识别物其
  radar 敏感体参与判据（geometric 只看配置体；physical 另要求接收视场内且 SNR ≥ snr_req，`radar.py`）。
"""

from __future__ import annotations

import copy
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from awr.contracts.enums import Awareness, TargetClass
from awr.contracts.rng_streams import Stream

from . import kernels_motion as KM
from .acoustic import acoustic_margin, threshold_bands
from .effective import DEFAULT_MARGIN_M, EffCache, effective_radius, keepout_volumes, suggested_radius
from .envview import TargetEnv, env_at
from .geometry import Volume, is_moving, wrap_pi
from .los_cache import LosCache
from .model import (
    TargetConfig,
    TargetError,
    TargetState1,
    TargetView,
    band_adj_of,
    class_defaults,
    label_zh,
    merge_patch,
    merged_config,
    radar_rx_of,
    sanitize_label,
    validate_config,
)
from .motion import (
    CH_OMEGA,
    CH_PAUSE,
    CH_PAUSE_LEN,
    CH_SPEED,
    MotionState,
    _path_points,
    _poly_cache,
    dsm_of,
    step_motion,
)
from .motion import _params as _motion_params
from .placement import place
from .radar import gamma_rain_db_km, radar_in_fov, radar_snr_db
from .signature import VehicleSignature, signature_of
from .sound import SoundBands, band_spectrum, effective_sound, lw_bands_now, sound_phase
from .visual import visual_ok

__all__ = ["BODIES_SESSION_MAX", "FleetEntry", "TargetQuantView", "TargetRuntime", "TargetView"]

BODIES_SESSION_MAX = 64
UNAWARE, SUSPICIOUS, REACTING, ALERTED, CALMING = (int(Awareness.UNAWARE), int(Awareness.SUSPICIOUS), int(Awareness.REACTING),
                                                   int(Awareness.ALERTED), int(Awareness.CALMING))
AW_NAMES = {int(a): a.name for a in Awareness}
DECAY_PER_S = 0.01
DISCOVER_DA = 0.2
BROADCAST_DA = 0.1
EPS = 1e-9
_SENSOR_NAMES = {0: None, 1: "eo", 2: "nir", 3: "lwir", 4: "radar", 5: "acoustic", 6: "lidar"}


def _class_code(cls: str) -> int:
    try:
        return int(TargetClass[cls.upper()])
    except KeyError:
        return int(TargetClass.UNKNOWN)


@dataclass(frozen=True)
class FleetEntry:
    """在场机体（`TargetRuntime.fleet`）：participant 为 armed 或 in_air（判据参与者）。"""

    id: str
    agent_no: int
    slot: int
    model_id: str
    pos: np.ndarray
    participant: bool
    sig: VehicleSignature


@dataclass(frozen=True)
class TargetQuantView:
    """逐对读数所需的识别物状态（`TargetRuntime.quant_view`）；dwell 为 {机体 id: 驻留计数（10 Hz）}。"""

    id: str
    target_no: int
    cfg: TargetConfig
    pos: np.ndarray
    yaw_rad: float
    h_m: float
    alertness: float
    awareness: str
    dwell: dict
    t_dwell_s: float
    rev: int


class MotionSource(Protocol):
    """D3 运动源（M23 `TapeMotionSource` 等）：t_ns 时刻识别物 `tid` 的 (pos_enu_m, yaw_rad, vel_enu_mps, enabled, hidden)；
    不认识该识别物时返回 None（保持上一位置、速度归零）。"""

    def pose(self, tid: str, t_ns: int) -> tuple[Sequence[float], float, Sequence[float], bool, bool] | None: ...


def _timeline(T: Any) -> bool:
    return (T.cfg.raw.get("motion") or {}).get("mode") == "timeline"


def _exogenous(T: Any) -> bool:
    return T.cfg.raw.get("reaction_policy") == "exogenous"


class TargetRuntime:
    def __init__(self) -> None:
        self.tab: Any = None
        self.st: dict[int, Any] = {}        # row -> TState
        self.world: Any = None
        self.profiles: Any = None
        self.los = LosCache()
        self.eff = EffCache()
        self.rev = 0
        self.lights_fn: Any = None          # vid -> (nav, strobe)；缺省关闭（AWR-20 §4.4 第 2 条）
        self.capture_fn: Any = None         # target id -> capture 对象（M19 提供时优先）
        self._env: TargetEnv | None = None
        self._bt: dict | None = None
        self._ktab: tuple | None = None     # 运动核的路径点、驻留、区域拼接表与参数矩阵（按配置与区域对象的身份缓存）
        self._ms_soa: np.ndarray | None = None   # 各识别物运动状态的行（MotionState._v 为其中一行的视图；按实体行号）
        self._env_s = -1
        self._models: list[str] = []
        self._seed: int | None = None
        self.stats = {"calls": 0, "discovered": 0, "env_fallback": 0, "stage_ns": 0}
        self.sink: Any = None               # 测试钩子：detail 载荷（bytes）的替代去向
        self.last_detail: dict | None = None
        self._pub = None
        self._bus = None
        self.events_log: list[tuple[str, dict]] = []   # 最近事件（测试与 SB-45 的 last_discovered 外的诊断，≤ 256）
        self.motion_source: MotionSource | None = None  # D3 扩展点 3（timeline 识别物的位置来源）
        self._src_off: set[str] = set()                 # 运动源本 tick 给出 enabled = false 的 timeline 识别物

    # ------------------------------------------------------------ D3 运动源（扩展点 3）
    def set_motion_source(self, src: MotionSource | None) -> None:
        """登记（或以 None 撤销）timeline 识别物的运动源；在步边界调用（会话装配）。"""
        self.motion_source = src

    def _timeline_step(self, T: Any, t_ns: int) -> None:
        """timeline 识别物的一步：取运动源的位姿写入 MotionState，不推进 `step_motion`、不取流 17、不应用行为覆盖。"""
        src = self.motion_source
        p = src.pose(T.id, t_ns) if src is not None else None
        if p is None:
            T.ms.speed, T.ms.vz = 0.0, 0.0
            return
        pos, yaw, vel, enabled, hidden = p
        T.ms.pos = np.asarray(pos, np.float64)
        T.ms.yaw = float(yaw)
        T.ms.speed = float(math.hypot(float(vel[0]), float(vel[1])))
        T.ms.vz = float(vel[2])
        T.hidden = bool(hidden)
        if enabled:
            self._src_off.discard(T.id)
        else:
            self._src_off.add(T.id)

    # ------------------------------------------------------------ 绑定
    def bind(self, ctx: Any) -> bool:
        ents = getattr(ctx, "entities", None)
        tab = ents.get("target") if ents is not None and hasattr(ents, "get") else None
        if tab is None:
            return False
        if tab is not self.tab:
            self.tab = tab
            self._drop_all()
            self._ms_soa = np.zeros((int(getattr(tab, "capacity", 32)), KM.MS_N))   # 各识别物运动状态的行（按实体行号）
        w = getattr(ctx, "world", None)
        if w is not self.world:
            self.world = w
            self.los.set_world(w)
        self.profiles = getattr(ctx, "profiles", None) or self.profiles
        self._sync()
        self._wire_m13()
        return True

    def _sync(self) -> None:
        """实体表与 Python 侧状态对齐（M08 清空、`sim/reset` 或他处删除的行丢弃其状态）。"""
        if self.tab is None:
            return
        meta = self.tab.meta
        for row in [r for r, T in self.st.items() if r not in meta or meta[r].id != T.id]:
            self._drop(row)

    def _wire_m13(self) -> None:
        """M13 云台 track 的 id 解析与 MID-360 反射率（d2-WP-10-to-M18）。"""
        mod = sys.modules.get("awr.sim.sensors.plugin")
        fn = getattr(mod, "runtime", None)
        if not callable(fn):
            return
        try:
            rt = fn()
        except Exception:
            return
        if rt is None or getattr(rt, "_m18_wired", False) is self:
            return
        try:
            rt.target_resolver = self.resolve_for_m13
            rt.target_reflect_fn = self.reflect_905
            rt._m18_wired = self
        except Exception:
            pass

    def resolve_for_m13(self, tid: str) -> tuple[int, tuple[float, float, float] | None] | None:
        T = self._by_id(tid)
        if T is None:
            return None
        return int(T.target_no), tuple(float(x) for x in T.ms.pos)  # type: ignore[return-value]

    def reflect_905(self, target_no: int) -> float:
        for T in self.st.values():
            if T.target_no == int(target_no):
                return float(T.cfg.raw.get("reflect_905", 0.3))
        return 0.3

    def seed(self, ctx: Any) -> int:
        r = getattr(ctx, "rng", None)
        try:
            g = r.get("targets_motion") if r is not None else None
            if g is not None:
                ent = g.bit_generator.seed_seq.entropy
                if isinstance(ent, (list, tuple)) and ent:
                    return int(ent[0])
        except Exception:
            pass
        cfg = getattr(ctx, "cfg", None)
        return int(getattr(cfg, "world_seed", 0) or 0)

    # ------------------------------------------------------------ 查找
    def ordered(self) -> list[Any]:
        if self.tab is None:
            return []
        out = []
        for m in self.tab.ordered():
            T = self.st.get(m.row)
            if T is not None:
                out.append(T)
        return out

    def _by_id(self, tid: str) -> Any:
        if self.tab is None:
            return None
        m = self.tab.by_id.get(str(tid))
        return None if m is None else self.st.get(m.row)

    def ids(self) -> list[str]:
        return [T.id for T in self.ordered()]

    # ------------------------------------------------------------ 增删改（步边界；TargetService）
    def _session_bodies(self, exclude: str | None = None) -> int:
        n = 0
        for T in self.st.values():
            if T.id != exclude and T.cfg.raw.get("enabled", True):
                n += sum(1 for b in T.cfg.raw.get("sensitivity") or [] if b.get("enabled", True))
        return n

    def _check_body_limit(self, raw: dict, exclude: str | None) -> None:
        if not raw.get("enabled", True):
            return
        n = sum(1 for b in raw.get("sensitivity") or [] if b.get("enabled", True))
        tot = self._session_bodies(exclude) + n
        if tot > BODIES_SESSION_MAX:
            raise TargetError(542, "sensitivity", "SESSION_BODY_LIMIT", value=tot, limit=BODIES_SESSION_MAX)

    def add(self, ctx: Any, d: dict) -> tuple[str, dict, list[str]]:
        """新增（M18-FR-001 至 FR-009、FR-019）：合并模板、校验、放置、加入实体表；返回 (id, cfg, warnings)。"""
        from awr.sim.core.entities import EntityLimitError

        if not self.bind(ctx):
            raise TargetError(213, None, "TARGETS_NOT_READY")
        if not isinstance(d, dict):
            raise TargetError(110, "target", "NOT_AN_OBJECT")
        if "cfg_rev" in d:
            d = {k: v for k, v in d.items() if k != "cfg_rev"}
        raw = merged_config(d)
        warnings = validate_config(raw)
        tid = raw.pop("id", None)
        label = raw.pop("label", None)
        pos0, region = place(raw, self.world)
        self._check_body_limit(raw, None)
        cls = raw["class"]
        try:
            meta = self.tab.add(model=cls, eid=tid)
        except EntityLimitError as e:
            raise TargetError(505, None, "SANDBOX_ENTITY_LIMIT", limit=e.limit) from None
        except KeyError:
            raise TargetError(110, "id", "TARGET_ID_DUP", value=tid) from None
        if label is None or not sanitize_label(label):
            suffix = meta.id.rsplit("-", 1)[-1] if meta.id.startswith(cls + "-") else meta.id
            label = f"{label_zh(cls)}-{suffix}"
        label = sanitize_label(label)
        self.tab.set_label(meta.id, label)
        T = self._new_state(meta, raw, pos0, region)
        self.st[meta.row] = T
        self._write_row(T, float(getattr(ctx, "t_ns", 0)) * 1e-9)
        return meta.id, self.cfg_dict(T), warnings

    def _drop(self, row: int) -> None:
        """移出 Python 侧状态；其运动状态改为自带的行（表中该行可能被新识别物复用，仍持有旧对象者读到的是移出时的值）。"""
        T = self.st.pop(row, None)
        if T is not None:
            T.ms.detach()

    def _drop_all(self) -> None:
        for T in self.st.values():
            T.ms.detach()
        self.st.clear()

    def _new_state(self, meta: Any, raw: dict, pos0: np.ndarray, region: np.ndarray | None) -> Any:
        from .behavior import TState

        self.rev += 1
        cfg = TargetConfig(meta.id, raw["class"], raw)
        soa = self._ms_soa
        row = soa[int(meta.row)] if soa is not None and 0 <= int(meta.row) < soa.shape[0] else None
        ms = MotionState(pos=pos0.copy(), yaw=float(raw.get("yaw_rad", 0.0)), region=region, row=row)
        a = float(raw.get("alertness", 0.5))
        return TState(meta.row, meta.target_no, meta.id, cfg, 1, ms, pos0.copy(), a, a)

    def update(self, ctx: Any, tid: str, patch: dict) -> tuple[dict, list[str]]:
        """修改（JSON Merge Patch，数组整体替换；`cfg_rev` + 1）；类别与 id 不可改。"""
        if not self.bind(ctx):
            raise TargetError(213, None, "TARGETS_NOT_READY")
        T = self._by_id(tid)
        if T is None:
            raise TargetError(543, "id", "TARGET_NOT_FOUND", value=str(tid))
        if not isinstance(patch, dict):
            raise TargetError(110, "patch", "NOT_AN_OBJECT")
        patch = {k: v for k, v in patch.items() if k != "cfg_rev"}
        if "class" in patch and patch["class"] != T.cfg.cls:
            raise TargetError(110, "class", "CLASS_IMMUTABLE", value=patch["class"])
        if "id" in patch and patch["id"] != T.id:
            raise TargetError(110, "id", "ID_IMMUTABLE", value=patch["id"])
        cur = copy.deepcopy(T.cfg.raw)
        newd = merge_patch(cur, {k: v for k, v in patch.items() if k not in ("id", "label")})
        raw = merged_config(newd)
        raw.pop("id", None)
        raw.pop("label", None)
        warnings = validate_config(raw)
        geo_keys = ("pos", "motion", "alt_agl_m")
        moved = any(k in patch for k in geo_keys)
        if moved:
            pos0, region = place(raw, self.world)
        else:
            pos0, region = None, T.ms.region
        self._check_body_limit(raw, T.id)
        if "label" in patch:
            lab = sanitize_label(patch["label"]) if patch["label"] is not None else ""
            if lab:
                self.tab.set_label(T.id, lab)
        self.rev += 1
        T.cfg = TargetConfig(T.id, raw["class"], raw)
        T.rev += 1
        if raw.get("alertness") != cur.get("alertness"):
            T.base_alert = T.alert = float(raw.get("alertness", T.alert))
        if moved:
            if "pos" in patch or "alt_agl_m" in patch:
                T.ms.pos = pos0.copy()  # type: ignore[union-attr]
                T.home = pos0.copy()  # type: ignore[union-attr]
            T.ms.region = region
            T.ms.seg, T.ms.dirn, T.ms.path_done, T.ms.dwell_until, T.ms.pause_until = 0, 1, False, -1.0, -1.0
        if "yaw_rad" in patch:
            T.ms.yaw = float(raw.get("yaw_rad", 0.0))
        T.snd_key = None
        self._write_row(T, float(getattr(ctx, "t_ns", 0)) * 1e-9)
        return self.cfg_dict(T), warnings

    def remove(self, ctx: Any, tid: str) -> None:
        if not self.bind(ctx):
            raise TargetError(213, None, "TARGETS_NOT_READY")
        T = self._by_id(tid)
        if T is None:
            raise TargetError(543, "id", "TARGET_NOT_FOUND", value=str(tid))
        self.tab.remove(T.id)
        self._drop(T.row)
        self.rev += 1

    def clear(self, ctx: Any) -> None:
        if self.bind(ctx):
            for tid in self.ids():
                self.tab.remove(tid)
            self._drop_all()
            self.rev += 1

    def export_targets(self) -> list[dict]:
        """会话快照段 `targets`（create_request 形状；配置的放置位置，不含运行状态，M18-FR-021）。"""
        out = []
        for T in self.ordered():
            d = self.cfg_dict(T)
            d.pop("cfg_rev", None)
            out.append(d)
        return out

    def import_targets(self, ctx: Any, items: list[dict]) -> list[dict]:
        probs = []
        for i, it in enumerate(items or []):
            try:
                self.add(ctx, it)
            except TargetError as e:
                probs.append({"section": "targets", "idx": i, "code": e.code, "detail": e.detail})
        return probs

    def cfg_dict(self, T: Any) -> dict:
        lab = self.tab.by_id[T.id].label if self.tab is not None and T.id in self.tab.by_id else T.id
        d = {"id": T.id, "label": lab, **copy.deepcopy(T.cfg.raw), "cfg_rev": int(T.rev)}
        d["class"] = T.cfg.cls
        return d

    # ------------------------------------------------------------ 只读视图
    def view(self) -> TargetView:
        Ts = self.ordered()
        n = len(Ts)
        return TargetView(n, np.array([T.target_no for T in Ts], np.uint16), np.array([_class_code(T.cfg.cls) for T in Ts], np.uint8),
                          np.array([T.ms.pos for T in Ts], float).reshape(n, 3), np.array([T.ms.yaw for T in Ts], float),
                          np.array([self._vel(T) for T in Ts], float).reshape(n, 3),
                          np.array([T.cfg.size_lwh for T in Ts], float).reshape(n, 3),
                          np.array([self._occlusion(T) for T in Ts], float), tuple(T.cfg for T in Ts), int(self.rev))

    def config(self, tid: str) -> TargetConfig | None:
        T = self._by_id(tid)
        return None if T is None else T.cfg

    def state1(self, tid: str) -> TargetState1 | None:
        T = self._by_id(tid)
        if T is None:
            return None
        return TargetState1(T.id, T.target_no, tuple(float(x) for x in T.ms.pos), float(T.ms.yaw), tuple(float(x) for x in self._vel(T)),
                            float(T.ms.speed), is_moving(T.cfg.raw.get("motion"), T.ms.speed), AW_NAMES[T.aware], float(T.alert))

    def keepout(self, tid: str, *, margin_m: float = DEFAULT_MARGIN_M, mode: str | None = None, conservative: bool = True,
                moving_as_circle: bool = True) -> list[Volume]:
        T = self._by_id(tid)
        if T is None:
            return []
        st = self.state1(tid)
        sigs = [signature_of(m, self.profiles) for m in self._models] or None
        return keepout_volumes(T.cfg, st, mode or T.cfg.raw.get("criterion_mode", "geometric"), margin_m, conservative=conservative,
                               moving_as_circle=moving_as_circle, models=sigs, env=None, cache=self.eff)

    def sound_bands_now(self, tid: str, t_ns: int) -> np.ndarray | None:
        """识别物声源在 t 的 L_W[b]（未计权，24 带；关闭相位 −40 dB；M19 声阵列用）。"""
        T = self._by_id(tid)
        if T is None:
            return None
        s = effective_sound(T.cfg.raw.get("sound"), T.emit_over)
        return lw_bands_now(s, t_ns * 1e-9, self._bands(T, s))

    def _bands(self, T: Any, s: dict | None) -> SoundBands | None:
        if s is None:
            return None
        key = tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in s.items()))
        if T.snd_key != key:
            T.snd_key, T.snd_bands = key, band_spectrum(s)
        return T.snd_bands

    def _vel(self, T: Any) -> tuple[float, float, float]:
        sp = float(T.ms.speed)
        return (sp * math.cos(T.ms.yaw), sp * math.sin(T.ms.yaw), float(T.ms.vz))

    def _occlusion(self, T: Any) -> float:
        base = float(T.cfg.raw.get("occlusion", 0.0))
        return max(base, 0.8) if T.hidden else base

    # ------------------------------------------------------------ 环境与参与者
    def env(self, ctx: Any) -> TargetEnv:
        t = int(getattr(ctx, "t_ns", 0))
        s = t // 1_000_000_000
        if self._env is None or s != self._env_s:
            self._env = env_at(getattr(ctx, "env", None), t)
            self._env_s = s
            if self._env.fallback:
                self.stats["env_fallback"] += 1
        return self._env

    def participants(self, S: Any, ctx: Any) -> tuple[list[str], np.ndarray, list[str], list[VehicleSignature]]:
        """我方 armed 或 in_air 的机体（slot 升序）：(ids, 位置 [n, 3], 机型, 签名)。"""
        if S is None:
            return [], np.zeros((0, 3)), [], []
        idx = S.active_idx()
        if idx.size == 0:
            self._models = []
            return [], np.zeros((0, 3)), [], []
        armed = np.zeros(idx.size, bool)
        sb = getattr(S, "blocks", {}).get("safety") if hasattr(S, "blocks") else None
        if sb is not None and "armed" in sb:
            armed |= np.asarray(sb["armed"][idx], bool)
        if hasattr(S, "in_air"):
            armed |= np.asarray(S.in_air[idx], bool)
        try:
            S.enu.refresh()
            pos_all = S.enu.pos
        except Exception:
            pos_all = np.zeros((S.capacity, 3))
        roster = getattr(ctx, "roster", None)
        prof = self.profiles
        ids, models = [], []
        all_models = []
        for s in idx.tolist():
            e = roster.by_slot.get(s) if roster is not None and hasattr(roster, "by_slot") else None
            mid = None
            if prof is not None:
                with _suppress():
                    mid = prof.ids[int(S.profile_id[s])]
            mid = mid or (getattr(e, "profile_id", None) if e is not None else None) or "p600_mid360"
            all_models.append(str(mid))
            ids.append(e.id if e is not None else str(s))
            models.append(str(mid))
        self._models = sorted(set(all_models))[:8]
        keep = np.flatnonzero(armed)
        ids = [ids[i] for i in keep]
        models = [models[i] for i in keep]
        P = np.asarray(pos_all[idx[keep]], float).reshape(-1, 3)
        sigs = []
        for vid, mid in zip(ids, models, strict=True):
            sig = signature_of(mid, prof)
            if self.lights_fn is not None:
                with _suppress():
                    nav, strobe = self.lights_fn(vid)
                    if nav or strobe:
                        sig = sig.with_lights(bool(nav), bool(strobe))
            sigs.append(sig)
        return ids, P, models, sigs

    def fleet(self, S: Any, ctx: Any) -> list[FleetEntry]:
        """全部在场机体（slot 升序）：id、agent_no、机型、位置、是否参与判据（armed 或 in_air）与带灯光的签名（M19 逐对读数与场视图，
        WP-19b）。与 `participants` 同一来源与同一口径。"""
        if S is None:
            return []
        idx = S.active_idx()
        if idx.size == 0:
            return []
        armed = np.zeros(idx.size, bool)
        sb = getattr(S, "blocks", {}).get("safety") if hasattr(S, "blocks") else None
        if sb is not None and "armed" in sb:
            armed |= np.asarray(sb["armed"][idx], bool)
        if hasattr(S, "in_air"):
            armed |= np.asarray(S.in_air[idx], bool)
        try:
            S.enu.refresh()
            pos_all = S.enu.pos
        except Exception:
            pos_all = np.zeros((S.capacity, 3))
        roster = getattr(ctx, "roster", None)
        prof = getattr(ctx, "profiles", None) or self.profiles
        out: list[FleetEntry] = []
        for k, s in enumerate(idx.tolist()):
            e = roster.by_slot.get(s) if roster is not None and hasattr(roster, "by_slot") else None
            mid = None
            if prof is not None:
                with _suppress():
                    mid = prof.ids[int(S.profile_id[s])]
            mid = str(mid or (getattr(e, "profile_id", None) if e is not None else None) or "p600_mid360")
            vid = e.id if e is not None else str(s)
            sig = signature_of(mid, prof)
            if self.lights_fn is not None:
                with _suppress():
                    nav, strobe = self.lights_fn(vid)
                    if nav or strobe:
                        sig = sig.with_lights(bool(nav), bool(strobe))
            no = getattr(e, "agent_no", None) if e is not None else None
            out.append(FleetEntry(str(vid), int(no) if no is not None else int(s), int(s), mid, np.asarray(pos_all[s], float).copy(),
                                  bool(armed[k]), sig))
        return out

    def quant_view(self, tid: str) -> TargetQuantView | None:
        """逐对读数所需的识别物状态（只读快照）：配置、脚点位置、航向、高度、警觉度、感知态、驻留计数与所需驻留（WP-19b）。"""
        T = self._by_id(tid)
        if T is None:
            return None
        return TargetQuantView(T.id, int(T.target_no), T.cfg, np.asarray(T.ms.pos, float).copy(), float(T.ms.yaw), float(T.cfg.size_lwh[2]),
                               float(T.alert), AW_NAMES[T.aware], dict(T.dwell), float(self.t_dwell(T)), int(T.rev))

    # ------------------------------------------------------------ stage
    def stage(self, S: Any, ctx: Any) -> None:
        if not self.bind(ctx):
            return
        Ts = self.ordered()
        if not Ts:
            return
        self.stats["calls"] += 1
        dt = float(getattr(ctx, "stage_dt", 0.0) or 0.1)
        t_ns = int(getattr(ctx, "t_ns", 0))
        t_s = t_ns * 1e-9
        tick = int(getattr(ctx, "tick", 0))
        seed = self.seed(ctx)
        from awr.sim.sensors import cbrng

        nos = np.array([T.target_no for T in Ts], np.int64)
        nrm = cbrng.normal(seed, int(Stream.TARGETS_MOTION), nos, tick, np.array([CH_OMEGA, CH_SPEED]))
        uni = cbrng.uniform(seed, int(Stream.TARGETS_MOTION), nos, tick, np.array([CH_PAUSE, CH_PAUSE_LEN]))
        ids, P, _models, sigs = self.participants(S, ctx)
        pos_of = {vid: (float(P[j, 0]), float(P[j, 1])) for j, vid in enumerate(ids)} if any(T.focus is not None for T in Ts) else {}
        env = self.env(ctx)
        world = self.world
        broadcasts: list[tuple[Any, tuple[float, float]]] = []
        kern = self._kern_env(world)
        batch: list[tuple[int, Any, Any, bool]] = []
        for k, T in enumerate(Ts):
            ts = self._tstat_full(T)
            if not ts[1]:
                continue
            if T.focus is not None and T.focus in pos_of:
                T.focus_pt = pos_of[T.focus]
            if _timeline(T):
                self._timeline_step(T, t_ns)
            elif T.aware == UNAWARE and not T.look_until > t_s:
                ov, base = None, True        # TState.override 的首两条均不成立时的结果（无覆盖、原运动）
            else:
                ov, base = T.override(t_s, pos_of.get(T.focus) if T.focus else None, world)
                if _exogenous(T):
                    ov, base = None, True    # D3 扩展点 3：响应的计时照常推进，运动不被覆盖
            # 运动：静止、随机游走与路径三种模式在核内一次推进（`kernels_motion.step_all`，与 step_motion 逐位相同）；识别物之间的
            # 运动互不依赖，批量与逐个的结果相同。合成世界（无 DSM 栅格）与 numba 不可用时逐个调用 step_motion
            kp = ts[5] if kern is not None else None
            if _timeline(T):
                pass
            elif kp is not None:
                if ov is None and (kp[0] == KM.MODE_STATIC or not base) and T.ms.speed == 0.0:
                    T.ms.vz = 0.0   # step_motion 的首个早退：静止且无行为覆盖，本步无事可做（不进核）
                else:
                    batch.append((k, T, ov, base))
            else:
                step_motion(T.ms, T.cfg.raw, dt, t_s, world, nrm[k], uni[k], ov, base_active=base)
            sp = ts[6]
            if sp is not None and not T.emit_over:
                per, ph0, duty = sp                                  # sound_phase 与 gate_on（同一相位，配置的浮点值预先取出）
                ph = float((t_s - ph0) % per)
                T.sound_on = duty >= 1.0 or (duty > 0.0 and ph < duty * per)
                T.sound_phase = ph
            else:
                snd = ts[2]
                s = snd if (snd is None or not T.emit_over) else effective_sound(snd, T.emit_over)
                if s is not None:
                    ph = sound_phase(s, t_s)
                    duty = float(s["duty"])
                    T.sound_on = duty >= 1.0 or (duty > 0.0 and ph < duty * float(s["period_s"]))   # gate_on（同一相位）
                    T.sound_phase = ph
                else:
                    T.sound_on, T.sound_phase = False, 0.0
        if batch:
            self._motion_batch(batch, kern, nrm, uni, dt, t_s)  # type: ignore[arg-type]
        IN, rows, R3 = self._inside_all_r3(Ts, P)
        idset = set(ids)
        bt = self._bt
        nt, nu = len(Ts), len(ids)
        ORD = np.empty((nt, max(nu, 1)), np.int64)
        QQ = np.empty((nt, max(nu, 1)), np.int64)
        CNT = np.zeros(nt, np.int64)
        if nu and IN.shape[0]:
            KM.geo_hits(IN, bt["starts"], bt["ends"], ORD, QQ, CNT)
        cntl = CNT.tolist()
        anyhit = any(cntl)
        r3l = R3.tolist() if nu and anyhit else None
        ordl = ORD.tolist() if anyhit else None
        qql = QQ.tolist() if anyhit else None
        off = self._src_off
        for k, T in enumerate(Ts):
            if not T._ts[1] or (off and T.id in off):
                continue
            s0, s1, meta = rows.get(k, (0, 0, ()))
            if T._ts[3]:
                hits, deferred = self._evaluate(T, ids, P, sigs, env, t_ns, IN[s0:s1], meta)
            else:
                m = cntl[k]
                hits, deferred = self._evaluate_geo(T, ids, IN, s0, s1, meta, ordl[k][:m] if m else (),  # type: ignore[index]
                                                    qql[k][:m] if m else (), r3l[s0] if m else None, idset)  # type: ignore[index]
            if hits or deferred or T.aware != UNAWARE:
                self._fsm(ctx, T, hits, deferred, t_s, dt, broadcasts)
            if T.aware == UNAWARE and T.alert > T.base_alert:
                T.alert = max(T.base_alert, T.alert - DECAY_PER_S * dt)
        for src, pt in broadcasts:
            if not _exogenous(src):
                self._broadcast(ctx, src, pt, t_s)
        self._write_rows([T for T in Ts if T.row in self.tab.meta], t_s)

    # ------------------------------------------------------------ 运动核（D2-FX-R2-sim 第 2 次）
    def _kern_env(self, world: Any) -> tuple[Any, bool] | None:
        """运动核的世界输入：(DSM 栅格或 None, 有世界)；世界不是 `DsmWorldQuery`（测试的合成世界）或 numba 不可用时为 None。"""
        if not KM.HAVE_NUMBA:
            return None
        if world is None:
            return (None, False)
        g = dsm_of(world)
        return None if g is None else (g, True)

    def _kparams(self, T: Any) -> tuple | None:
        """配置派生的核参数（按配置对象缓存）：(模式码, 参数行 [PR_N]，路径点 (n, 3)，驻留 (n,))；模式不在核支持范围时为 None。"""
        raw = T.cfg.raw
        c = getattr(T, "_kp", None)
        if c is not None and c[0] is raw:
            return c[1]
        m = raw.get("motion") or {}
        mode = {"static": KM.MODE_STATIC, "random_walk": KM.MODE_RW, "path": KM.MODE_PATH}.get(m.get("mode", "static"))
        out = None
        if mode is not None:
            pv = _motion_params(T.ms, raw)
            cls, _m, _mode, turn_max, vmax, acc, alt, tau, v0, sig_v, pause_p = pv
            loop = {"loop": KM.LOOP_LOOP, "pingpong": KM.LOOP_PINGPONG, "once": KM.LOOP_ONCE}.get(str(m.get("loop", "loop")), 3)
            row = [0.0] * KM.PR_N
            row[KM.P_GROUND] = 1.0 if cls != "uav" else 0.0
            row[KM.P_UAV] = 1.0 if cls == "uav" else 0.0
            row[KM.P_MODE] = float(mode)
            row[KM.P_TURN], row[KM.P_VMAX], row[KM.P_ACC], row[KM.P_ALT] = turn_max, vmax, acc, alt
            row[KM.P_TAU], row[KM.P_V0], row[KM.P_SIGV], row[KM.P_PAUSEP] = tau, v0, sig_v, pause_p
            row[KM.P_LOOP] = float(loop)
            pts = _path_points(m) if mode == KM.MODE_PATH else np.zeros((0, 3))
            dws = (m.get("dwell_s") or []) if mode == KM.MODE_PATH else []
            dw = np.array([float(dws[i]) if i < len(dws) else 0.0 for i in range(len(pts))], float)
            out = (mode, row, np.ascontiguousarray(pts.reshape(-1, 3), np.float64), dw)
        T._kp = (raw, out)
        return out

    def _motion_batch(self, batch: list, kern: tuple[Any, bool], nrm: np.ndarray, uni: np.ndarray, dt: float, t_s: float) -> None:
        """核适用识别物的一步运动：打包 MotionState 与参数 → `kernels_motion.step_all` → 写回（字段与 step_motion 的结果逐位相同）。"""
        dsm, has_world = kern
        n = len(batch)
        # 路径点与区域多边形的拼接表（按配置与区域数组的身份缓存）
        tkey = [(T.cfg.raw, T.ms.region) for _k, T, _o, _b in batch]
        tc = self._ktab
        if tc is None or len(tc[0]) != n or any(x[0] is not y[0] or x[1] is not y[1] for x, y in zip(tc[0], tkey, strict=True)):
            pts_l, dw_l, reg_l = [], [], []
            offs = []
            po = ro = 0
            for _k, T, _o, _b in batch:
                kp = self._kparams(T)
                mode, _row, pts, dw = kp  # type: ignore[misc]
                pts_l.append(pts)
                dw_l.append(dw)
                if mode == KM.MODE_RW and T.ms.region is not None:
                    reg = np.ascontiguousarray(np.asarray(T.ms.region, float).reshape(-1, 2))
                    c = _poly_cache(T.ms.region)[1]
                    offs.append((po, len(pts), ro, len(reg), c[0], c[1]))
                    reg_l.append(reg)
                    ro += len(reg)
                else:
                    offs.append((po, len(pts), -1, 0, 0.0, 0.0))
                po += len(pts)
            PTS = np.ascontiguousarray(np.concatenate(pts_l) if po else np.zeros((1, 3)))
            DW = np.ascontiguousarray(np.concatenate(dw_l) if po else np.zeros(1))
            REG = np.ascontiguousarray(np.concatenate(reg_l) if ro else np.zeros((1, 2)))
            tc = self._ktab = (tkey, PTS, DW, REG, offs, [list(self._kparams(T)[1]) for _k, T, _o, _b in batch])  # type: ignore[index]
            for row, (o0, o1, o2, o3, o4, o5) in zip(tc[5], offs, strict=True):
                row[KM.P_POFF], row[KM.P_PN], row[KM.P_ROFF], row[KM.P_RN], row[KM.P_RCX], row[KM.P_RCY] = o0, o1, o2, o3, o4, o5
            self._ktab = (tkey, PTS, DW, REG, offs, np.array(tc[5], np.float64).reshape(n, KM.PR_N))
            tc = self._ktab
        _tk, PTS, DW, REG, _offs, PR = tc
        soa = self._ms_soa
        rows = [T.row for _k, T, _o, _b in batch]
        direct = soa is not None and all(T.ms._v.base is soa for _k, T, _o, _b in batch)
        if direct:
            MS = soa
            ROWS = np.array(rows, np.int64)
        else:   # 运动状态不在表中（例如测试替换了 MotionState）：拼成临时表，核后写回各自的行
            MS = np.ascontiguousarray(np.array([T.ms._v for _k, T, _o, _b in batch], np.float64).reshape(n, KM.MS_N))
            ROWS = np.arange(n, dtype=np.int64)
        ovl = []
        ba = []
        ks = []
        for k, _T, ov, base in batch:
            if ov is None:
                ovl.append(_OV_ZERO)
            else:
                kc = _OV_CODE.get(ov.kind, KM.OV_NONE)
                pt = ov.point
                ovl.append((float(kc), 1.0 if pt is not None else 0.0, float(pt[0]) if pt is not None else 0.0,
                            float(pt[1]) if pt is not None else 0.0, float(ov.speed), 1.0 if ov.away else 0.0, float(ov.stop_within_m)))
            ba.append(1 if base else 0)
            ks.append(k)
        OV = np.array(ovl, np.float64).reshape(n, KM.OV_N)
        kk = np.array(ks, np.int64)
        NZ = np.ascontiguousarray(nrm[kk], np.float64)
        UN = np.ascontiguousarray(uni[kk], np.float64)
        if dsm is not None:
            a, x0, y0, cell = dsm.a, dsm.x0, dsm.y0, dsm.cell
        else:
            a, x0, y0, cell = _NO_DSM, 0.0, 0.0, 1.0
        KM.step_all(MS, ROWS, PR, OV, np.array(ba, np.int64), NZ, UN, PTS, DW, REG, a, x0, y0, cell, dsm is not None, has_world, float(dt),
                    float(t_s))
        if not direct:
            for i, (_k, T, _o, _b) in enumerate(batch):
                T.ms._v[:] = MS[i]

    def _tstat_full(self, T: Any) -> tuple:
        """`_tstat` 的缓存元组另加运动核参数与声源浮点参数：(rev, enabled, sound, physical, cfg, 核参数, (周期, 相位, 占空) | None)。"""
        ts = getattr(T, "_ts", None)
        if ts is None or ts[0] != T.rev or ts[4] is not T.cfg or len(ts) < 7:
            self._tstat(T)
            ts = T._ts
            snd = ts[2]
            sp = None
            if isinstance(snd, dict):
                try:
                    sp = (float(snd["period_s"]), float(snd.get("phase_s", 0.0)), float(snd["duty"]))
                except (KeyError, TypeError, ValueError):
                    sp = None
            ts = T._ts = (*ts[:5], self._kparams(T), sp)
        return ts

    def _tstat(self, T: Any) -> tuple[bool, dict | None]:
        """配置派生的逐识别物常量（按 cfg_rev 缓存）：`T._ts = (rev, enabled, sound, physical)`；返回 (enabled, sound)。"""
        ts = getattr(T, "_ts", None)
        if ts is None or ts[0] != T.rev or ts[4] is not T.cfg:
            raw = T.cfg.raw
            ts = (T.rev, bool(raw.get("enabled", True)), raw.get("sound"), raw.get("criterion_mode", "geometric") == "physical", T.cfg)
            T._ts = ts
        return ts[1], ts[2]

    # ------------------------------------------------------------ 判据
    def _body_table(self, Ts: list[Any]) -> dict:
        """全部启用识别物的启用视觉、声学体参数（按识别物顺序连续存放；配置变化时重建）。"""
        key = tuple((T.id, T.rev, bool(T.cfg.raw.get("enabled", True))) for T in Ts)
        if self._bt is not None and self._bt["key"] == key:
            return self._bt
        tk, shp, r, zmin, zmax, half, off = [], [], [], [], [], [], []
        rows: dict[int, tuple[int, int, tuple]] = {}
        codes = {"sphere": 0, "hemisphere": 1, "cylinder": 2, "sector": 3}
        for k, T in enumerate(Ts):
            if not T.cfg.raw.get("enabled", True):
                continue
            s0 = len(tk)
            meta = []
            judged = ("visual", "acoustic", "radar") if radar_rx_of(T.cfg.raw) else ("visual", "acoustic")
            for i, b in enumerate(T.cfg.raw.get("sensitivity") or []):
                if not b.get("enabled", True) or b.get("kind") not in judged:
                    continue
                h = math.radians(float(b.get("half_angle_deg", 60.0)))
                code = codes[b["shape"]]
                if code == 3 and h >= math.pi - 1e-9:
                    code = 2
                tk.append(k)
                shp.append(code)
                r.append(float(b["radius_max_m"]))
                zmin.append(float(b.get("z_min_m", 0.0)))
                zmax.append(float(b.get("z_max_m", 300.0)))
                half.append(h)
                off.append(math.radians(float(b.get("azimuth_offset_deg", 0.0))))
                meta.append((i, str(b["kind"])))
            if meta:
                rows[k] = (s0, len(tk), tuple(meta))
        starts = np.zeros(len(Ts), np.int64)
        ends = np.zeros(len(Ts), np.int64)
        for k, (s0, s1, _m) in rows.items():
            starts[k], ends[k] = s0, s1
        self._bt = {"key": key, "tk": np.array(tk, np.int64), "shape": np.array(shp, np.int8), "r2": np.array(r) ** 2,
                    "zmin": np.array(zmin), "zmax": np.array(zmax), "half": np.array(half), "off": np.array(off), "rows": rows,
                    "starts": starts, "ends": ends}
        return self._bt

    def _inside_all(self, Ts: list[Any], P: np.ndarray) -> tuple[np.ndarray, dict]:
        """所有启用体 × 参与者的内外矩阵与各识别物的体行区间（M18-NFR-001；不含斜距的既有接口）。"""
        IN, rows, _r3 = self._inside_all_r3(Ts, P)
        return IN, rows

    def _inside_all_r3(self, Ts: list[Any], P: np.ndarray) -> tuple[np.ndarray, dict, np.ndarray]:
        """所有启用体 × 参与者的内外矩阵（M18-NFR-001）；判定用真实方向（扇形不按整圆）。另返回各体行的中心到参与者的斜距
        R3[体, 机]（与逐识别物 `np.linalg.norm(P − pos, axis=1)` 同一归约；geometric 判据的 range_m）。numba 可用时在
        `kernels_motion.inside_all` 中一次求值（逐元素同式），否则用 numpy 表达式。"""
        bt = self._body_table(Ts)
        nb, nu = len(bt["tk"]), len(P)
        if nb == 0 or nu == 0:
            return np.zeros((nb, nu), bool), bt["rows"], np.zeros((nb, nu))
        soa = self._ms_soa
        if soa is not None and all(T.ms._v.base is soa for T in Ts):
            M = soa[np.fromiter((T.row for T in Ts), np.int64, len(Ts))]
        else:
            M = np.array([T.ms._v for T in Ts], float).reshape(len(Ts), KM.MS_N)
        pos = M[:, KM.S_X:KM.S_Z + 1]
        C = pos[bt["tk"]]
        if KM.HAVE_NUMBA:
            yaw = M[:, KM.S_YAW][bt["tk"]] + bt["off"]
            IN = np.empty((nb, nu), np.bool_)
            R3 = np.empty((nb, nu))
            KM.inside_all(C, np.ascontiguousarray(P, np.float64), bt["shape"], bt["r2"], bt["zmin"], bt["zmax"], bt["half"], yaw, IN, R3)
            return IN, bt["rows"], R3
        d = P[None, :, :] - C[:, None, :]
        rh2 = d[..., 0] ** 2 + d[..., 1] ** 2
        r2 = bt["r2"][:, None]
        sh = bt["shape"][:, None]
        dz = d[..., 2]
        ball = (rh2 + dz * dz) <= r2
        prism = (rh2 <= r2) & (dz >= bt["zmin"][:, None]) & (dz <= bt["zmax"][:, None])
        IN = np.where(sh <= 1, ball & ((sh == 0) | (dz >= 0.0)), prism)
        sec = np.flatnonzero(bt["shape"] == 3)
        if sec.size:
            yaw = M[:, KM.S_YAW][bt["tk"][sec]] + bt["off"][sec]
            a = np.abs((np.arctan2(d[sec, :, 1], d[sec, :, 0]) - yaw[:, None] + math.pi) % (2.0 * math.pi) - math.pi)
            IN[sec] &= (a <= bt["half"][sec][:, None]) | (rh2[sec] < 1e-12)
        R3 = np.linalg.norm(d, axis=2)
        return IN, bt["rows"], R3

    def _evaluate_geo(self, T: Any, ids: list[str], IN: np.ndarray, s0: int, s1: int, meta: tuple, order: Any, qs: Any,
                      r3: list | None, idset: set) -> tuple[dict[str, tuple], frozenset]:
        """geometric 判据（与 `_evaluate` 的 geometric 分支同一结果与次序）：每架机取首个包含它的敏感体（按体次序，同体内按参与者
        次序；`kernels_motion.geo_hits` 给出 order 与体序号 qs），命中信息为 (kind, vol_idx, 斜距)；驻留按参与者次序更新。逐对的
        Python 只在命中的机上执行（D2-FX-R2-sim 第 2 次）。"""
        hits: dict[str, tuple] = {}
        if not order:
            T.inside_n = 0
            T.occ = None
            if T.dwell:
                T.dwell.clear()
                T.hit_info.clear()
            return hits, _NO_DEFER
        T.occ = (IN[s0:s1], ids, meta)
        T.inside_n = len(order)
        for j, q in zip(order, qs, strict=True):
            vi, kind = meta[q]
            hits[ids[j]] = (kind, vi, r3[j])  # type: ignore[index]
        dw, hi = T.dwell, T.hit_info
        for vid in ids:
            h = hits.get(vid)
            if h is not None:
                dw[vid] = dw.get(vid, 0) + 1
                hi[vid] = h
            elif vid in dw:
                del dw[vid]
                hi.pop(vid, None)
        if len(dw) != len(hits):
            for vid in [x for x in dw if x not in idset]:
                dw.pop(vid, None)
                hi.pop(vid, None)
        return hits, _NO_DEFER

    def _evaluate(self, T: Any, ids: list[str], P: np.ndarray, sigs: list[VehicleSignature], env: TargetEnv, t_ns: int,
                  ins: np.ndarray, meta: tuple) -> tuple[dict[str, dict], set[str]]:
        raw = T.cfg.raw
        bodies = raw.get("sensitivity") or []
        hits: dict[str, dict] = {}
        deferred: set[str] = set()
        any_in = ins.any(axis=0) if ins.size else np.zeros(len(ids), bool)
        if not any_in.any():
            T.inside_n = 0
            T.occ = None
            if T.dwell:
                T.dwell.clear()
                T.hit_info.clear()
            return hits, deferred
        T.occ = (ins, ids, meta)       # occupied_by 在 detail 中按需展开
        T.inside_n = int(any_in.sum())
        pos = T.ms.pos
        d = P - pos[None, :]
        r3 = np.linalg.norm(d, axis=1)
        physical = raw.get("criterion_mode", "geometric") == "physical"
        h = float(T.cfg.size_lwh[2])
        for q, (vol_idx, kind) in enumerate(meta):
            cand = [j for j in np.flatnonzero(ins[q]).tolist() if ids[j] not in hits]
            if not cand:
                continue
            if not physical:
                for j in cand:
                    hits[ids[j]] = {"kind": kind, "vol_idx": vol_idx, "range_m": round(float(r3[j]), 2)}
                continue
            if kind == "visual":
                top = np.array([pos[0], pos[1], pos[2] + 0.95 * h])
                los = self.los.get_batch(P[cand], np.repeat(pos[None, :], len(cand), 0), np.full(len(cand), h), "M18", t_ns, "top")
                dz = P[cand, 2] - top[2]
                rh = np.hypot(P[cand, 0] - pos[0], P[cand, 1] - pos[1])
                elev = np.arctan2(dz, rh)
                ok, ratio = visual_ok(r3[cand], elev, [sigs[j] for j in cand], env, float(raw.get("visual_theta0_arcmin", 1.0)), T.alert)
                for qq, j in enumerate(cand):
                    if los[qq].deferred:
                        deferred.add(ids[j])
                    elif los[qq].k_los > 0.5 and ok[qq]:
                        hits[ids[j]] = {"kind": "visual", "vol_idx": vol_idx, "range_m": round(float(r3[j]), 2),
                                        "margin_ratio": round(float(ratio[qq]), 3)}
            elif kind == "acoustic":
                b = bodies[vol_idx]
                lw = np.array([sigs[j].lw_bands() for j in cand])
                thr = np.array([threshold_bands(env.l_bg_bands, sigs[j].masking_db(), float(raw.get("hearing_th_dba", 20.0)), T.alert,
                                                float(raw.get("acoustic_adv_db", 0.0))) for j in cand])
                az = np.arctan2(d[cand, 1], d[cand, 0])
                dpsi = wrap_pi(az - (T.ms.yaw + math.radians(float(b.get("azimuth_offset_deg", 0.0)))))
                g = float(b.get("gain_back_db", 0.0)) * (1.0 - np.cos(dpsi)) / 2.0
                marg = acoustic_margin(lw, r3[cand], g, thr, band_adj_of(raw))
                for qq, j in enumerate(cand):
                    if marg[qq] >= 0.0:
                        hits[ids[j]] = {"kind": "acoustic", "vol_idx": vol_idx, "range_m": round(float(r3[j]), 2),
                                        "margin_db": round(float(marg[qq]), 2)}
            elif kind == "radar":
                rx = radar_rx_of(raw)
                if rx is None:
                    continue
                b = bodies[vol_idx]
                fov = radar_in_fov(rx, d[cand], T.ms.yaw + math.radians(float(b.get("azimuth_offset_deg", 0.0))))
                snr = radar_snr_db(rx, np.array([sigs[j].rcs_m2 for j in cand]), r3[cand], gamma_rain_db_km(getattr(env, "rain_mmph", 0.0)))
                marg = np.asarray(snr, float).reshape(-1) - float(rx["snr_req_db"])
                for qq, j in enumerate(cand):
                    if fov[qq] and marg[qq] >= 0.0:
                        hits[ids[j]] = {"kind": "radar", "vol_idx": vol_idx, "range_m": round(float(r3[j]), 2),
                                        "margin_db": round(float(marg[qq]), 2)}
        for vid in ids:
            if vid in hits:
                T.dwell[vid] = T.dwell.get(vid, 0) + 1
                T.hit_info[vid] = hits[vid]
            elif vid not in deferred:
                T.dwell.pop(vid, None)
                T.hit_info.pop(vid, None)
        for vid in [x for x in T.dwell if x not in ids]:
            T.dwell.pop(vid, None)
            T.hit_info.pop(vid, None)
        return hits, deferred

    def t_dwell(self, T: Any) -> float:
        return 3.0 * (1.2 - T.alert) * (0.5 if T.aware == CALMING else 1.0)

    # ------------------------------------------------------------ 状态机
    def _set_state(self, ctx: Any, T: Any, to: int, reason: str, t_s: float, uav: str | None = None) -> None:
        if to == T.aware:
            return
        frm = T.aware
        T.aware = to
        data = {"target": T.id, "from": AW_NAMES[frm], "to": AW_NAMES[to], "reason": reason}
        if uav is not None:
            data["uav"] = uav
        self._emit(ctx, "target.state", 0, t_s, data, uav=uav)

    def _ready(self, T: Any, dt: float) -> list[str]:
        """驻留已达 t_dwell 的机（按参与者顺序）；平复中再次满足属于新轮次，不排除本轮已发现的机。"""
        tdw = self.t_dwell(T)
        fresh = T.aware == CALMING
        return [vid for vid, n in T.dwell.items() if (n - 1) * dt >= tdw - EPS and (fresh or vid not in T.disc_set)]

    def _discover(self, ctx: Any, T: Any, vid: str, t_s: float) -> None:
        info = _hinfo(T.hit_info.get(vid))
        T.disc_set.add(vid)
        T.disc_n = min(65535, T.disc_n + 1)
        T.alert = min(1.0, T.alert + DISCOVER_DA)
        self.stats["discovered"] += 1
        mode = T.cfg.raw.get("criterion_mode", "geometric")
        data: dict[str, Any] = {"target": T.id, "uav": vid, "kind": info.get("kind", "visual"), "mode": mode,
                                "vol_idx": int(info.get("vol_idx", 0)), "range_m": float(info.get("range_m", 0.0)),
                                "episode": int(T.episode), "count": int(T.disc_n)}
        if "margin_ratio" in info:
            data["margin_ratio"] = info["margin_ratio"]
        if "margin_db" in info:
            data["margin_db"] = info["margin_db"]
        T.last_disc = {"t_sim_ns": round(t_s * 1e9), "uav": vid, "kind": data["kind"], "range_m": data["range_m"],
                       "margin_db": data.get("margin_db"), "margin_ratio": data.get("margin_ratio")}
        self._emit(ctx, "target.discovered", 3, t_s, data, uav=vid)

    def _new_episode(self, ctx: Any, T: Any, vids: list[str], t_s: float, reason: str, broadcasts: list) -> None:
        T.episode = min(65535, T.episode + 1)
        T.disc_set = set()
        for vid in vids:
            self._discover(ctx, T, vid, t_s)
        T.clear_behavior()
        T.look_until = -1.0
        T.start_onset(t_s, vids[0], T.focus_pt)
        self._set_state(ctx, T, REACTING, reason, t_s, vids[0])
        if (T.cfg.raw.get("reaction") or {}).get("alert_others"):
            broadcasts.append((T, (float(T.ms.pos[0]), float(T.ms.pos[1]))))

    def _fsm(self, ctx: Any, T: Any, hits: dict, deferred: set, t_s: float, dt: float, broadcasts: list) -> None:
        any_hit = bool(hits)
        if T.aware == UNAWARE and any_hit:
            self._set_state(ctx, T, SUSPICIOUS, "dwell", t_s, next(iter(hits)))
        if T.aware == SUSPICIOUS:
            ready = self._ready(T, dt)
            if ready:
                self._new_episode(ctx, T, ready, t_s, "dwell", broadcasts)
            elif not any_hit and not deferred:
                self._set_state(ctx, T, UNAWARE, "clear", t_s)
                return
        if T.aware in (REACTING, ALERTED):
            for vid in self._ready(T, dt):
                self._discover(ctx, T, vid, t_s)
        if T.aware == REACTING and t_s >= T.onset_until - EPS:
            self._set_state(ctx, T, ALERTED, "onset_done", t_s)
            T.start_response(t_s)
        if T.aware == ALERTED and not any_hit and not deferred:
            T.calm_t0 = t_s
            self._set_state(ctx, T, CALMING, "clear", t_s)
            return
        if T.aware == CALMING:
            ready = self._ready(T, dt)
            if ready:
                self._new_episode(ctx, T, ready, t_s, "redetect", broadcasts)
            elif any_hit or deferred:
                T.calm_t0 = t_s
            elif t_s - T.calm_t0 >= float(T.cfg.raw.get("calm_s", 60.0)) - EPS:
                T.clear_behavior()
                T.disc_set = set()
                T.look_until = -1.0
                T.ms.pause_until = -1.0
                self._set_state(ctx, T, UNAWARE, "calm", t_s)

    def _broadcast(self, ctx: Any, src: Any, pt: tuple[float, float], t_s: float) -> None:
        r = float(src.cfg.raw.get("alert_radius_m", 50.0))
        for T in self.ordered():
            if T is src or T.cfg.cls != src.cfg.cls or T.aware != UNAWARE or not T.cfg.raw.get("enabled", True):
                continue
            if math.hypot(T.ms.pos[0] - pt[0], T.ms.pos[1] - pt[1]) > r:
                continue
            T.alert = min(1.0, T.alert + BROADCAST_DA)
            T.look_until, T.look_pt = t_s + 5.0, pt
            T.calm_t0 = t_s
            self._set_state(ctx, T, ALERTED, "broadcast", t_s)
            self._write_row(T, t_s)

    # ------------------------------------------------------------ 列与事件
    def _keepout_r(self, T: Any) -> float:
        raw = T.cfg.raw
        if not raw.get("enabled", True):
            return 0.0
        kinds = ("visual", "acoustic", "radar") if radar_rx_of(raw) else ("visual", "acoustic")
        bodies = [b for b in raw.get("sensitivity") or [] if b.get("enabled", True) and b.get("kind") in kinds]
        if not bodies:
            return 0.0
        if raw.get("criterion_mode", "geometric") != "physical":
            return float(max(float(b["radius_max_m"]) for b in bodies) + DEFAULT_MARGIN_M)
        vols = self.keepout(T.id)
        return float(max((v.r_m for v in vols), default=0.0))

    def _write_row(self, T: Any, t_s: float) -> None:
        tab = self.tab
        if tab is None or T.row not in tab.meta:
            return
        c, r, raw = tab.cols, T.row, T.cfg.raw
        if int(t_s) != getattr(T, "_kr_s", None) or T.keepout_r == 0.0:
            T.keepout_r = self._keepout_r(T)
            T._kr_s = int(t_s)
        c["tgt_cls"][r] = _class_code(T.cfg.cls)
        c["tgt_pos"][r] = T.ms.pos
        c["tgt_vel"][r] = self._vel(T)
        c["tgt_yaw"][r] = T.ms.yaw
        c["tgt_speed"][r] = T.ms.speed
        c["tgt_size"][r] = T.cfg.size_lwh
        c["tgt_occl"][r] = self._occlusion(T)
        c["tgt_aware"][r] = T.aware
        c["tgt_alert"][r] = T.alert
        c["tgt_crit"][r] = 1 if raw.get("criterion_mode") == "physical" else 0
        c["tgt_enabled"][r] = 1 if raw.get("enabled", True) else 0
        c["tgt_moving"][r] = 1 if is_moving(raw.get("motion"), T.ms.speed) else 0
        c["tgt_occupied"][r] = 1 if T.inside_n > 0 else 0
        c["tgt_sound_on"][r] = 1 if T.sound_on else 0
        c["tgt_hidden"][r] = 1 if T.hidden else 0
        c["tgt_disc_recent"][r] = 1 if T.aware in (REACTING, ALERTED, CALMING) else 0
        c["tgt_disc_n"][r] = T.disc_n
        c["tgt_episode"][r] = T.episode
        c["tgt_n_vol"][r] = sum(1 for b in raw.get("sensitivity") or [] if b.get("enabled", True))
        c["tgt_keepout_r"][r] = T.keepout_r
        c["tgt_cfg_rev"][r] = T.rev
        c["tgt_path_idx"][r] = T.ms.seg if (raw.get("motion") or {}).get("mode") == "path" else -1
        c["tgt_sound_phase"][r] = T.sound_phase

    def _write_rows(self, Ts: list[Any], t_s: float) -> None:
        """stage 末尾一次写全部行：配置派生的列（类别、尺寸、判据、启用、体数、cfg_rev）只在行集合或配置版本变化时写；动态列先
        汇成一个 float64 与一个整数矩阵，每列一次向量赋值（取值与逐列列表赋值相同；D2-FX-R2-sim 第 2 次）。"""
        tab = self.tab
        if tab is None or not Ts:
            return
        c = tab.cols
        rows = np.fromiter((T.row for T in Ts), np.int64, len(Ts))
        sec = int(t_s)
        for T in Ts:
            if sec != getattr(T, "_kr_s", None) or T.keepout_r == 0.0:
                T.keepout_r = self._keepout_r(T)
                T._kr_s = sec
            if getattr(T, "_static_rev", None) != T.rev:
                raw = T.cfg.raw
                T._static = (_class_code(T.cfg.cls), T.cfg.size_lwh, 1 if raw.get("criterion_mode") == "physical" else 0,
                             1 if raw.get("enabled", True) else 0, sum(1 for b in raw.get("sensitivity") or [] if b.get("enabled", True)),
                             (raw.get("motion") or {}).get("mode", "static"), raw.get("motion"), float(raw.get("occlusion", 0.0)))
                T._static_rev = T.rev
        st = [T._static for T in Ts]
        wkey = (id(c["tgt_cls"]), rows.tobytes(), tuple((T.id, T.target_no, T.rev) for T in Ts))
        if wkey != getattr(self, "_wkey", None):
            c["tgt_cls"][rows] = [x[0] for x in st]
            c["tgt_size"][rows] = [x[1] for x in st]
            c["tgt_crit"][rows] = [x[2] for x in st]
            c["tgt_enabled"][rows] = [x[3] for x in st]
            c["tgt_n_vol"][rows] = [x[4] for x in st]
            c["tgt_cfg_rev"][rows] = [T.rev for T in Ts]
            self._wkey = wkey
        fl = []
        it = []
        soa = self._ms_soa
        if soa is not None and all(T.ms._v.base is soa for T in Ts):
            V = soa[rows].tolist()                        # 运动状态的行（同 MotionState 的字段值）
        else:
            V = [T.ms._v.tolist() for T in Ts]
        sx, sy, sz, syaw, ssp, svz, sseg = KM.S_X, KM.S_Y, KM.S_Z, KM.S_YAW, KM.S_SPEED, KM.S_VZ, KM.S_SEG
        for T, x, v in zip(Ts, st, V, strict=True):
            sp = v[ssp]
            yaw = v[syaw]
            base = float(x[7])
            fl.append((v[sx], v[sy], v[sz], sp * math.cos(yaw), sp * math.sin(yaw), v[svz], yaw, sp,
                       max(base, 0.8) if T.hidden else base, float(T.alert), float(T.keepout_r), float(T.sound_phase)))
            aw = T.aware
            it.append((aw, 1 if (x[5] != "static" or sp > 0.1) else 0, 1 if T.inside_n > 0 else 0, 1 if T.sound_on else 0,
                       1 if T.hidden else 0, 1 if aw in (REACTING, ALERTED, CALMING) else 0, T.disc_n, T.episode,
                       int(v[sseg]) if x[5] == "path" else -1))
        F = np.array(fl, np.float64)
        I = np.array(it, np.int64)  # noqa: E741
        c["tgt_pos"][rows] = F[:, 0:3]
        c["tgt_vel"][rows] = F[:, 3:6]
        c["tgt_yaw"][rows] = F[:, 6]
        c["tgt_speed"][rows] = F[:, 7]
        c["tgt_occl"][rows] = F[:, 8]
        c["tgt_alert"][rows] = F[:, 9]
        c["tgt_keepout_r"][rows] = F[:, 10]
        c["tgt_sound_phase"][rows] = F[:, 11]
        c["tgt_aware"][rows] = I[:, 0]
        c["tgt_moving"][rows] = I[:, 1]
        c["tgt_occupied"][rows] = I[:, 2]
        c["tgt_sound_on"][rows] = I[:, 3]
        c["tgt_hidden"][rows] = I[:, 4]
        c["tgt_disc_recent"][rows] = I[:, 5]
        c["tgt_disc_n"][rows] = I[:, 6]
        c["tgt_episode"][rows] = I[:, 7]
        c["tgt_path_idx"][rows] = I[:, 8]

    def _emit(self, ctx: Any, kind: str, sev: int, t_s: float, data: dict, *, uav: str | None = None) -> None:
        self.events_log.append((kind, dict(data)))
        if len(self.events_log) > 256:
            del self.events_log[:64]
        ev = getattr(ctx, "events", None)
        if ev is not None and hasattr(ev, "emit"):
            ev.emit(kind, t_sim_ns=round(t_s * 1e9), severity=sev, uav=uav, fields=data)

    # ------------------------------------------------------------ detail（awr.target.detail.v1）
    def capture_of(self, T: Any) -> dict | None:
        if self.capture_fn is not None:
            with _suppress():
                got = self.capture_fn(T.id)
                if isinstance(got, dict):
                    return got
        c = self.tab.cols if self.tab is not None else {}
        lv = int(c["tgt_best_level"][T.row]) if "tgt_best_level" in c else 0
        cap = bool(c["tgt_captured"][T.row]) if "tgt_captured" in c else False
        sen = int(c["tgt_best_sensor"][T.row]) if "tgt_best_sensor" in c else 0
        uno = int(c["tgt_capture_uav_no"][T.row]) if "tgt_capture_uav_no" in c else 0xFFFF
        return {"best_level": ("none", "D", "R", "I")[lv & 3], "captured": cap, "sensor": _SENSOR_NAMES.get(sen),
                "uav": self._uav_id(uno), "n_eff": None, "n_req": None, "since_t_sim_ns": None}

    def _uav_id(self, agent_no: int) -> str | None:
        if agent_no == 0xFFFF:
            return None
        ctx_roster = getattr(self, "_roster", None)
        if ctx_roster is not None:
            for e in getattr(ctx_roster, "by_slot", {}).values():
                if int(getattr(e, "agent_no", -1)) == agent_no:
                    return str(e.id)
        return None

    def detail(self, T: Any, t_ns: int, env: TargetEnv, *, with_cfg: bool = False) -> dict:
        raw = T.cfg.raw
        t_s = t_ns * 1e-9
        models = self._models[:8]
        occ = T.occupied()
        # D2-FX-R2-sim 第 2 次（m18.detail 11.9 ms/仿真秒）：敏感体的配置字段按 cfg_rev 预先展开；有效半径按（配置、机型集合、
        # 环境键、剖面、雨强、警觉度 2 位小数）记忆——与 EffCache 的键同粒度，取值相同
        vs = getattr(T, "_dvol", None)
        if vs is None or vs[0] != T.rev or vs[1] is not T.cfg:
            pre = []
            for i, b in enumerate(raw.get("sensitivity") or []):
                pre.append((i, b.get("kind") in ("visual", "acoustic") or (b.get("kind") == "radar" and bool(radar_rx_of(raw))),
                            {"idx": i, "kind": b["kind"], "shape": b["shape"], "radius_cfg_m": _c(b["radius_max_m"]), "radius_eff_m": None,
                             "z_min_m": _c(b.get("z_min_m", 0.0)), "z_max_m": _c(b.get("z_max_m", 300.0)),
                             "half_angle_deg": _c(b.get("half_angle_deg", 60.0)),
                             "azimuth_offset_deg": _c(b.get("azimuth_offset_deg", 0.0)), "enabled": bool(b.get("enabled", True)),
                             "occupied_by": None}))
            vs = T._dvol = (T.rev, T.cfg, pre)
        ek = (T.rev, T.cfg, tuple(models), id(self.profiles), env.key(), env.profile, round(float(getattr(env, "rain_mmph", 0.0)), 1),
              round(float(T.alert), 2))
        em = getattr(T, "_deff", None)
        if em is None or em[0] != ek:
            sigs = [signature_of(m, self.profiles) for m in models]
            effs = []
            for i, judged, _d in vs[2]:
                eff = {}
                if judged:
                    for m, sg in zip(models, sigs, strict=True):
                        eff[m] = _c(effective_radius(T.cfg, i, sg, env, conservative=False, alertness=T.alert, cache=self.eff), 1)
                effs.append(eff)
            em = T._deff = (ek, effs)
        vols = []
        no = len(occ)
        for (i, _j, d0), eff in zip(vs[2], em[1], strict=True):
            v = d0.copy()
            v["radius_eff_m"] = dict(eff)
            v["occupied_by"] = occ[i] if i < no else []
            vols.append(v)
        dw = None
        if T.dwell:
            dd = T.dwell
            mx = max(dd.values())
            vid = max(k for k, v in dd.items() if v == mx)       # 与 max(dwell, key=(计数, id)) 相同
            info = _hinfo(T.hit_info.get(vid))
            dw = {"uav": vid, "kind": info.get("kind", "visual"), "t_s": _c(max(0, dd[vid] - 1) * 0.1, 1),
                  "t_dwell_s": _c(self.t_dwell(T), 2)}
        s = effective_sound(raw.get("sound"), T.emit_over)
        snd = None
        if s is not None:
            snd = {"on": bool(T.sound_on), "phase_s": _c(T.sound_phase, 2), "lw_dba_now": _c(float(s["lw_dba"]) - (0.0 if T.sound_on else 40.0), 1)}
        moving = is_moving(raw.get("motion"), T.ms.speed)
        lab = self.tab.by_id[T.id].label if self.tab is not None and T.id in self.tab.by_id else T.id
        d = {"id": T.id, "label": lab, "class": T.cfg.cls, "t_sim_ns": int(t_ns), "awareness": AW_NAMES[T.aware],
             "alertness": _c(T.alert, 3), "criterion_mode": raw.get("criterion_mode", "geometric"),
             "level_req": raw.get("level_req", "R"), "discovered_count": int(T.disc_n), "episode": int(T.episode),
             "last_discovered": T.last_disc, "dwell": dw, "behavior": T.behavior_phase(t_s), "sensitivity": vols,
             "inside_n": int(T.inside_n),
             "keepout": {"radius_m": _c(T.keepout_r, 1), "margin_m": DEFAULT_MARGIN_M, "full_circle": bool(moving)},
             "sound": snd,
             "motion": {"mode": (raw.get("motion") or {}).get("mode", "static"), "speed_mps": _c(T.ms.speed, 2),
                        "vz_mps": _c(T.ms.vz, 2),
                        "path_idx": int(T.ms.seg) if (raw.get("motion") or {}).get("mode") == "path" else None},
             "capture": self.capture_of(T), "cfg_rev": int(T.rev)}
        if with_cfg:
            d["cfg"] = self.cfg_dict(T)
        return d

    def publish_detail(self, ctx: Any) -> Any:
        """慢任务（2 Hz【仿真】）：`state/sim-core/target_detail` = `{v: 1, t_sim_ns, items: {id: detail}}`（全集）；cfg_rev 变化与每 10 s
        附完整配置（M18-FR-018）。"""
        if not self.bind(ctx):
            return False
        Ts = self.ordered()
        t_ns = int(getattr(ctx, "t_ns", 0))
        self._roster = getattr(ctx, "roster", None)
        env = self.env(ctx)
        items = {}
        for T in Ts:
            with_cfg = T.rev != T.detail_rev_sent or t_ns - T.detail_cfg_t >= 10e9
            items[T.id] = self.detail(T, t_ns, env, with_cfg=with_cfg)
            if with_cfg:
                T.detail_rev_sent, T.detail_cfg_t = T.rev, t_ns
        msg = {"v": 1, "t_sim_ns": t_ns, "items": items}
        self.last_detail = msg
        if not items and getattr(self, "_detail_empty", False):
            return None
        self._detail_empty = not items
        import msgpack

        raw = msgpack.packb(msg, use_bin_type=True)
        if self.sink is not None:
            self.sink(raw)
            return None
        ev = getattr(ctx, "events", None)
        bus = getattr(ev, "bus", None)
        if bus is None:
            return None
        try:
            from awr.contracts import bus_keys

            if self._pub is None or self._bus is not bus:
                self._bus, self._pub = bus, bus.publisher(bus_keys.STATE_TARGET_DETAIL)
            self._pub.put(raw)
        except Exception:
            self._pub = None
        return None

    def suggested(self, ctx: Any, tid: str, model_id: str | None, light: str | None = None, bg: str | None = None) -> dict:
        T = self._by_id(tid)
        if T is None:
            raise TargetError(543, "id", "TARGET_NOT_FOUND", value=str(tid))
        mid = model_id or (self._models[0] if self._models else "p600_mid360")
        if self.profiles is not None and hasattr(self.profiles, "has") and not self.profiles.has(mid):
            raise TargetError(520, "model_id", "MODEL_UNKNOWN", value=mid)
        env = self.env(ctx)
        if bg in ("rural", "suburban", "urban") or light in ("day", "low", "night"):
            from .envview import day_night

            prof = bg if bg in ("rural", "suburban", "urban") else env.profile
            d_env, n_env = day_night(prof, env.mor_m)
            base = n_env if light == "night" else d_env
            from dataclasses import replace

            env = replace(base, band=light or base.band)
        sig = signature_of(mid, self.profiles)
        return suggested_radius(T.cfg, sig, env, cache=self.eff, alertness=T.alert).to_wire()


_NO_DEFER: frozenset = frozenset()
_OV_CODE = {"hold": KM.OV_HOLD, "look": KM.OV_LOOK, "move": KM.OV_MOVE}
_OV_ZERO = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
_NO_DSM = np.zeros((1, 1), np.float32)



def _hinfo(v: Any) -> dict:
    """命中信息：physical 判据为 dict；geometric 判据为 (kind, vol_idx, 斜距) 元组，在用到时按原口径展开（range_m 取 2 位小数）。"""
    if v is None:
        return {}
    if isinstance(v, tuple):
        return {"kind": v[0], "vol_idx": v[1], "range_m": round(float(v[2]), 2)}
    return v


def _c(x: float, nd: int = 2) -> float | int:
    """detail 的紧凑数值：整数值写成 int（msgpack 1–3 B，而 float64 为 9 B），其余保留 nd 位小数。"""
    v = round(float(x), nd)
    return int(v) if v.is_integer() and abs(v) < 2**31 else v


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return exc[0] is not None and issubclass(exc[0], Exception)


def class_defaults_doc() -> dict:
    return class_defaults()
