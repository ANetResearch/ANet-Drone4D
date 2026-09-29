"""EnvKeyframe 线上形态、规范编码与关键帧管理（M07-FR-001、FR-021、FR-025；M07 §6.2.5、§6.3.2、§6.6.1；ADR-025）。

线上：msgpack，snake_case，字段顺序按 §6.2.5 表；EnvScalars 位置编码（21 个数）；路由中间态只传预设 id；资产 URL 不上线。
规范数值编码：整数值（含整值浮点，|x| < 2^5^3）写最短整数，其余写 float64，−0 归一为 0，与 @msgpack/msgpack 3.1.3 默认编码器
一致，因此"Python 编码 → TS 解码 → TS 编码"逐字节相同。编码时断言 ≤ 2048 B。

KeyframeManager 实现 §6.6.1 K01–K09：`apply` 在 env 网格点生效，from = eval_env(kf, t_apply)，按路由生成 via（仅稳态且
to_preset 等于起点时），version + 1；过渡中新操作以当前求值态为起点（路由不启用）；`config` 补丁为 step 帧并对活跃锋面
按 f_adv 变化重基；锋面事件每次版本变化先修剪过期项再追加（≤ 4 项）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import msgpack
import numpy as np

from .anchors import Anchors, anchors_initial
from .weather.presets import NF, USER_AXIS, EnvError, Presets, presets
from .weather.transitions import TransitionKf, eval_env, exp_t1_ns
from .wind.gust import GustEvent, gust_expired
from .wind.profile import f_adv as f_adv_of

__all__ = ["MAX_FRAME_BYTES", "ApplyResult", "EnvKeyframe", "EnvOp", "KeyframeManager", "canon", "decode", "encode"]

SCHEMA = "awr.env.keyframe.v1"
MAX_FRAME_BYTES = 2048
SAFE_INT = 2**53
SEC = 1_000_000_000


def canon(x: Any) -> Any:
    """规范数值编码的预处理（递归）。"""
    if isinstance(x, bool) or x is None or isinstance(x, str):
        return x
    if isinstance(x, (float, np.floating)):
        v = float(x)
        if v == 0.0:
            return 0
        if math.isfinite(v) and v.is_integer() and abs(v) < SAFE_INT:
            return int(v)
        return v
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, Mapping):
        return {str(k): canon(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, np.ndarray)):
        return [canon(v) for v in x]
    return x


@dataclass(frozen=True, slots=True)
class EnvOp:
    kind: Literal["set", "preset", "gust", "reset"]
    patch: dict | None = None
    name: str | None = None
    amp_mps: float | None = None
    length_m: float | None = None
    dir_from_deg: float | None = None
    duration_s: float | None = None
    mode: Literal["smooth", "step", "exp"] = "smooth"
    by: str = ""


@dataclass(frozen=True, slots=True)
class ApplyResult:
    version: int
    t_apply_ns: int
    route: list[str]
    warnings: list[str]


@dataclass
class EnvKeyframe:
    world_id: str
    version: int
    epoch: int
    seed: int
    t_ns: int
    t_apply_ns: int
    config: dict[str, Any]
    mode: str
    t0_ns: int
    t1_ns: int
    from_: np.ndarray
    to: np.ndarray
    via: list[str]
    to_preset: str | None
    anchors: Anchors
    events: list[GustEvent] = field(default_factory=list)
    vis: dict[str, Any] = field(default_factory=lambda: {"streamlines": None, "vmax_mps": 20})
    _tr: TransitionKf | None = None

    def transition(self) -> TransitionKf:
        if self._tr is None:
            self._tr = TransitionKf(self.mode, self.t0_ns, self.t1_ns, self.from_, self.to, list(self.via))
        return self._tr

    def same_transition(self, other: EnvKeyframe) -> bool:
        return (self.mode == other.mode and self.t0_ns == other.t0_ns and self.t1_ns == other.t1_ns and self.via == other.via
                and np.array_equal(self.from_, other.from_) and np.array_equal(self.to, other.to))

    def to_wire(self) -> dict[str, Any]:
        return canon({
            "schema": SCHEMA, "world_id": self.world_id, "version": self.version, "epoch": self.epoch, "seed": self.seed,
            "t_ns": self.t_ns, "t_apply_ns": self.t_apply_ns, "config": self.config, "mode": self.mode, "t0_ns": self.t0_ns,
            "t1_ns": self.t1_ns, "from": list(self.from_), "to": list(self.to), "via": list(self.via), "to_preset": self.to_preset,
            "anchors": self.anchors.to_json(), "events": [ev.to_list() for ev in self.events], "vis": self.vis,
        })

    def encode(self) -> bytes:
        return encode(self.to_wire())

    def with_heartbeat(self, t_ns: int, anchors: Anchors) -> EnvKeyframe:
        """心跳：只更新 t_ns 与 anchors（最近一个已推进的网格时刻）。"""
        kf = replace(self, t_ns=int(t_ns), anchors=anchors.copy())
        kf._tr = self._tr
        return kf


def encode(wire: Mapping[str, Any]) -> bytes:
    b = msgpack.packb(canon(wire), use_bin_type=True)
    if len(b) > MAX_FRAME_BYTES:
        raise ValueError(f"EnvKeyframe {len(b)} B exceeds the {MAX_FRAME_BYTES} B hard limit (M07-NFR-004)")
    return b


def from_wire(d: Mapping[str, Any]) -> EnvKeyframe:
    if d.get("schema") != SCHEMA:
        raise ValueError(f"not an EnvKeyframe: {d.get('schema')!r}")
    return EnvKeyframe(
        world_id=str(d["world_id"]), version=int(d["version"]), epoch=int(d["epoch"]), seed=int(d["seed"]), t_ns=int(d["t_ns"]),
        t_apply_ns=int(d["t_apply_ns"]), config=dict(d["config"]), mode=str(d["mode"]), t0_ns=int(d["t0_ns"]), t1_ns=int(d["t1_ns"]),
        from_=np.asarray(d["from"][:NF], np.float64), to=np.asarray(d["to"][:NF], np.float64), via=[str(v) for v in d["via"]],
        to_preset=d["to_preset"], anchors=Anchors.from_json(d["anchors"]), events=[GustEvent.from_list(e) for e in d["events"]],
        vis=dict(d["vis"]))


def decode(b: bytes) -> EnvKeyframe:
    return from_wire(msgpack.unpackb(b, raw=False, strict_map_key=False))


def _merge(dst: dict, src: Mapping) -> dict:
    out = dict(dst)
    for k, v in src.items():
        out[k] = _merge(out[k], v) if isinstance(v, Mapping) and isinstance(out.get(k), Mapping) else v
    return out


class KeyframeManager:
    """服务端关键帧状态机（§6.6.1）。"""

    def __init__(self, *, world_id: str, seed: int, epoch: int, config: dict[str, Any], initial: np.ndarray, initial_preset: str | None,
                 bounds_min: Sequence[float], bounds_max: Sequence[float], P: Presets | None = None, vmax_mps: float = 20.0) -> None:
        self.P = P or presets()
        self.world_id = world_id
        self.seed = int(seed)
        self.epoch = int(epoch)
        self.bounds_min = [float(bounds_min[0]), float(bounds_min[1])]
        self.bounds_max = [float(bounds_max[0]), float(bounds_max[1])]
        self.vmax = float(vmax_mps)
        self.version = 0
        self.next_event_id = 1
        self.streamlines: str | None = None  # AWSL URL prefix of the analytic field (D1-ext), appended /d{deg:03d}.awsl
        self.initial = np.asarray(initial, np.float64).copy()
        self.initial_preset = initial_preset
        self.kf = self._new(0, config, "step", 0, 0, self.initial, self.initial, [], initial_preset,
                            anchors_initial(TransitionKf("step", 0, 0, self.initial, self.initial), 0), [])

    # ------------------------------------------------------------ 构造
    def _new(self, t_ns: int, config: dict, mode: str, t0: int, t1: int, fr: np.ndarray, to: np.ndarray, via: list[str],
             to_preset: str | None, anchors: Anchors, events: list[GustEvent]) -> EnvKeyframe:
        self.version += 1
        return EnvKeyframe(self.world_id, self.version, self.epoch, self.seed, int(t_ns), int(t_ns), config, mode, int(t0), int(t1),
                           np.array(fr, np.float64), np.array(to, np.float64), list(via), to_preset, anchors.copy(), list(events),
                           {"streamlines": self.streamlines, "vmax_mps": self.vmax})

    @property
    def f_adv(self) -> float:
        return f_adv_of(self.kf.config["wind"]["profile"])

    def s_at(self, t_ns: int) -> np.ndarray:
        return np.asarray(eval_env(self.kf.transition(), t_ns, np.empty(NF)))

    def live_events(self, S_t: float) -> list[GustEvent]:
        fa = self.f_adv
        return [ev for ev in self.kf.events if not gust_expired(S_t, ev, fa)]

    def is_steady(self, t_ns: int) -> bool:
        return self.kf.mode == "step" or t_ns >= self.kf.t1_ns

    # ------------------------------------------------------------ 操作（K03、K04、K06）
    def prepare(self, op: EnvOp, t_apply_ns: int) -> tuple[np.ndarray, list[str], str | None, str, int, int, dict | None]:
        """校验并构造新帧的过渡参数（不改状态）；失败抛 EnvError。"""
        P = self.P
        cur = self.kf
        fr = self.s_at(t_apply_ns)
        cfg_patch = None
        via: list[str] = []
        if op.kind == "preset":
            name = op.name or ""
            if name not in P.ids:
                raise EnvError(440, {"name": name})
            dur = P.check_duration(op.duration_s if op.duration_s is not None else P.durations["preset"])
            to = P.overlay(fr, name)
            mode = "step" if dur == 0 else "smooth"
            if mode == "smooth" and self.is_steady(t_apply_ns) and cur.to_preset is not None and cur.to_preset != name:
                via = P.route(cur.to_preset, name)
            to_preset: str | None = name
        elif op.kind == "set":
            items, cfg_patch = P.validate_patch(op.patch or {})
            if op.mode not in ("smooth", "step", "exp"):
                raise EnvError(110, {"field": "mode"})
            dur = P.check_duration(op.duration_s if op.duration_s is not None else P.durations["ui_edit"])
            to = fr.copy()
            for i, x in items:
                to[i] = x
            mode = op.mode if dur > 0 or op.mode == "exp" else "step"
            if cfg_patch:
                mode = "step"
            only_user = all(USER_AXIS[i] for i, _ in items)
            to_preset = cur.to_preset if (only_user and self.is_steady(t_apply_ns)) else None
            if not items:
                to_preset = cur.to_preset
        else:
            raise EnvError(300, {"field": "kind"})
        P.check_state(to)
        if mode == "step":
            t0 = t1 = t_apply_ns
        elif mode == "exp":
            t0, t1 = t_apply_ns, exp_t1_ns(t_apply_ns)
        else:
            t0, t1 = t_apply_ns, t_apply_ns + round(dur * SEC)
        return to, via, to_preset, mode, t0, t1, cfg_patch

    def apply(self, op: EnvOp, t_apply_ns: int, anchors: Anchors) -> EnvKeyframe:
        to, via, to_preset, mode, t0, t1, cfg_patch = self.prepare(op, t_apply_ns)
        fr = self.s_at(t_apply_ns)
        cfg = self.kf.config
        events = self.live_events(anchors.s_m)
        if cfg_patch:
            old_fa = self.f_adv
            cfg = _merge(cfg, cfg_patch)
            new_fa = f_adv_of(cfg["wind"]["profile"])
            if new_fa != old_fa:  # §6.3.7：f_adv 变化时重基，使 xi 在 t_apply 连续
                events = [replace(ev, x0_m=ev.x0_m + (new_fa - old_fa) * anchors.s_m) for ev in events]
        self.kf = self._new(t_apply_ns, cfg, mode, t0, t1, fr, to, via, to_preset, anchors, events)
        return self.kf

    def add_event(self, ev: GustEvent, t_ns: int, anchors: Anchors) -> EnvKeyframe:
        """K07：追加锋面（先修剪过期项）；过渡本身不变。"""
        cur = self.kf
        events = [*self.live_events(anchors.s_m)[-3:], ev]
        self.version += 1
        self.kf = EnvKeyframe(self.world_id, self.version, self.epoch, self.seed, int(t_ns), int(t_ns), cur.config, cur.mode, cur.t0_ns,
                              cur.t1_ns, cur.from_, cur.to, list(cur.via), cur.to_preset, anchors.copy(), events, dict(cur.vis))
        self.kf._tr = cur._tr
        return self.kf

    def reset(self, t_ns: int = 0, initial: np.ndarray | None = None, preset: str | None = None) -> EnvKeyframe:
        """K06 剧本重置：step 帧，锚点归零（湿度取稳态），事件清空。"""
        s = self.initial if initial is None else np.asarray(initial, np.float64)
        tp = self.initial_preset if initial is None else preset
        A = anchors_initial(TransitionKf("step", t_ns, t_ns, s, s), t_ns)
        self.kf = self._new(t_ns, self.kf.config, "step", t_ns, t_ns, s, s, [], tp, A, [])
        return self.kf

    def disable_turbulence(self) -> None:
        """资产不可用（K02）：湍流模型置 off（不单独发版本，由调用方在初始帧前调用）。"""
        cfg = _merge(self.kf.config, {"wind": {"turbulence": {"model": "off"}}})
        self.kf.config = cfg
