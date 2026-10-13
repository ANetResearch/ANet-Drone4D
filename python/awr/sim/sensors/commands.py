"""D2 机体命令 `uav/{id}/cmd/{gimbal,zoom,sensor/mode}` 的处理（M13-FR-204、FR-205、FR-208；AWR-04 §11.2；17 §17.7.6）。

M08 只做路由、生命周期与准入 ④⑤⑦（M08-FR-218）；处理函数在准入时同步校验并排程，命令在 apply_tick 之后的首次 sensors
stage 调用生效（确定性：重仿真时同一输入日志得到同一结果）。签名同 `register_command_handler`：
`fn(msg, apply_tick, ctx) -> {status: accepted|rejected, code, detail?, result?}`；也可直接调用
`vehicle_command(rt, op, vehicle_id, args, apply_tick, ctx)`。

参数（线上角度为弧度）：

- `gimbal{mode ∈ angles|look_at|track|stow|release, pitch_rad?, yaw_rad?, look_at_enu_m?, target?, gimbal_id?}`：operator 层，
  保持 60 s【仿真】或至 `release`（`release` 为本模块追加的取值，已提请 M00 登记）；`yaw_rad` 相对机头、左为正，`pitch_rad`
  向上为正，超出机型云台限位时钳制并在载荷中置 limited；无云台 560；参数缺失 110；`track` 的识别物不存在 543。
- `zoom{sensor, f_mm | zoom_x | hfov_rad}`：越出镜头范围 561（17 §17.7.6；M13-FR-204 起草时写 110，以 17 为准）；无光学变焦
  （LWIR、雷达等）560。
- `sensor/mode{sensor, active?, mode?, illum?{builtin?, ext?}, resolution?, palette?, cue?}`：`nir` 需要该云台上有照明器（560），
  `illum.ext = on` 而未挂载外挂照明器 563，`resolution = 4k` 需要机型能力 `compute.orin_nx`（561），`palette` 只用于 LWIR（560）。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

from .enums import IllumCfg, ImagingCmd, SensorKind
from .gimbal import SensorError

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["OPS", "handle_message", "vehicle_command"]

OPS = ("gimbal", "zoom", "sensor/mode")
_META = frozenset({"op", "cid", "args", "service", "name", "vehicle", "vehicle_id", "uav", "id", "principal", "principal_id", "role",
                   "batch_id", "timeout_ms", "_internal", "lease", "source"})
_MODE_CMD = {"auto": ImagingCmd.AUTO, "day": ImagingCmd.DAY, "lowlight": ImagingCmd.LOWLIGHT, "nir": ImagingCmd.NIR}
_ILL = {"auto": IllumCfg.AUTO, "on": IllumCfg.ON, "off": IllumCfg.OFF}


def _ok(**result: Any) -> dict:
    return {"status": "accepted", "code": 0, "result": result or None}


def _rej(code: int, detail: str, **extra: Any) -> dict:
    return {"status": "rejected", "code": int(code), "detail": {"detail": detail, **extra}}


def _vehicle_and_args(op: str, msg: dict) -> tuple[str | None, dict]:
    args = msg.get("args") if isinstance(msg.get("args"), dict) else {k: v for k, v in msg.items() if k not in _META}
    vid = msg.get("vehicle") or msg.get("vehicle_id") or msg.get("uav") or msg.get("id")
    if vid is None:
        svc = str(msg.get("service") or msg.get("name") or "")
        parts = svc.split("/")
        if len(parts) >= 4 and parts[0] == "uav" and parts[2] == "cmd":
            vid = parts[1]
    if vid is None:
        vid = args.get("vehicle") or args.get("vehicle_id")
    return (None if vid is None else str(vid)), dict(args)


def handle_message(rt: SensorRuntime, op: str, msg: dict, apply_tick: int, ctx: Any = None) -> dict:
    """`register_command_handler` 的回调体（op 为 `gimbal`、`zoom` 或 `sensor/mode`）。"""
    vid, args = _vehicle_and_args(op, msg if isinstance(msg, dict) else {})
    if vid is None:
        return _rej(110, "VEHICLE_MISSING")
    return vehicle_command(rt, op, vid, args, apply_tick, ctx)


def vehicle_command(rt: SensorRuntime, op: str, vehicle_id: str, args: dict, apply_tick: int, ctx: Any = None) -> dict:
    s = rt.slot_of(str(vehicle_id))
    if s < 0 or not rt._ready(s):
        return _rej(107, "NO_VEHICLE", vehicle=vehicle_id)
    try:
        if op == "gimbal":
            return _gimbal(rt, s, args, apply_tick)
        if op == "zoom":
            return _zoom(rt, s, args, apply_tick)
        if op in ("sensor/mode", "sensor_mode"):
            return _sensor_mode(rt, s, args, apply_tick, ctx)
    except SensorError as e:
        return _rej(e.code, e.detail, **{k: _plain(v) for k, v in e.extra.items()})
    return _rej(110, "OP_UNKNOWN", op=op)


def _plain(v: Any) -> Any:
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    return v


def _finite3(v: Any) -> list[float] | None:
    try:
        a = [float(x) for x in v]
    except (TypeError, ValueError):
        return None
    return a if len(a) == 3 and all(math.isfinite(x) for x in a) else None


# ---------------------------------------------------------------- gimbal
def _gimbal(rt: SensorRuntime, s: int, args: dict, apply_tick: int) -> dict:
    k = rt.col_for(s, args.get("gimbal_id", args.get("sensor")))
    if k is None:
        raise SensorError(560, "NO_GIMBAL")
    mode = str(args.get("mode", "angles"))
    if mode not in ("angles", "fixed", "look_at", "track", "stow", "release"):
        raise SensorError(110, "GIMBAL_MODE_UNKNOWN", field="mode", value=mode)
    a: dict[str, Any] = {}
    if mode in ("angles", "fixed"):
        if args.get("pitch_rad") is None and args.get("yaw_rad") is None:
            raise SensorError(110, "GIMBAL_ARGS_MISSING", field="pitch_rad")
        for f in ("pitch_rad", "yaw_rad"):
            if args.get(f) is not None:
                v = float(args[f])
                if not math.isfinite(v):
                    raise SensorError(110, "GIMBAL_ARGS_INVALID", field=f)
                a[f] = v
    elif mode == "look_at":
        p = _finite3(args.get("look_at_enu_m"))
        if p is None:
            raise SensorError(110, "GIMBAL_TARGET_MISSING", field="look_at_enu_m")
        a["look_at_enu_m"] = p
    elif mode == "track":
        tid = args.get("target")
        r = None if tid is None else rt.resolve_target(str(tid))
        if r is None:
            raise SensorError(543, "TARGET_NOT_FOUND", target=tid)
        a["target_no"], a["p_enu_m"] = int(r[0]), np.asarray(r[1], np.float64)

    def go() -> None:
        if mode == "track":
            p = rt.target_pos(int(a["target_no"]))
            if p is not None:
                a["p_enu_m"] = p
        rt.gimbal.set_operator(s, k, "angles" if mode == "fixed" else mode, a, rt.tick_ns())

    rt.schedule(apply_tick, go)
    return _ok(gimbal=f"g{k}", mode=mode)


# ---------------------------------------------------------------- zoom
def _zoom(rt: SensorRuntime, s: int, args: dict, apply_tick: int) -> dict:
    name = args.get("sensor")
    rig = rt.rig_of(s)
    sp = None if rig is None or name is None else rig.by_name(str(name))
    if sp is None:
        raise SensorError(560, "SENSOR_NOT_MOUNTED", sensor=name)
    k = rig.col_of(sp.name)
    if sp.zoom is None or k is None:
        raise SensorError(560, "NO_OPTICAL_ZOOM", sensor=sp.name)
    z = sp.zoom
    if args.get("f_mm") is not None:
        f = float(args["f_mm"])
    elif args.get("zoom_x") is not None:
        f = z.f_min_mm * float(args["zoom_x"])
    elif args.get("hfov_rad") is not None:
        f = z.f_for_hfov(float(args["hfov_rad"]))
    elif args.get("hfov_deg") is not None:
        f = z.f_for_hfov(math.radians(float(args["hfov_deg"])))
    else:
        raise SensorError(110, "ZOOM_ARGS_MISSING", field="f_mm")
    tol = 1e-9 * z.f_max_mm
    if not (math.isfinite(f) and z.f_min_mm - tol <= f <= z.f_max_mm + tol):
        raise SensorError(561, "F_OUT_OF_RANGE", field="f_mm", value=f, min=z.f_min_mm, max=z.f_max_mm)
    f = min(max(f, z.f_min_mm), z.f_max_mm)
    rt.schedule(apply_tick, lambda: rt.payload.zoom_to(s, k, f))
    return _ok(sensor=sp.name, f_mm=round(f, 6), eta_s=round(abs(math.log(f / float(rt.pblk["f"][s, k]))) / z.log_rate, 3)
               if math.isfinite(float(rt.pblk["f"][s, k])) else None)


# ---------------------------------------------------------------- sensor/mode
def _sensor_mode(rt: SensorRuntime, s: int, args: dict, apply_tick: int, ctx: Any) -> dict:
    name = args.get("sensor")
    rig = rt.rig_of(s)
    sp = None if rig is None or name is None else rig.by_name(str(name))
    if sp is None:
        raise SensorError(560, "SENSOR_NOT_MOUNTED", sensor=name)
    P = rt.pblk
    k = rig.col_of(sp.name)
    owner = None if k is None else rig.col_specs[k]
    todo: list = []
    if "active" in args and args["active"] is not None:
        on = bool(args["active"])

        def act(on=on) -> None:
            if on:
                P["off"][s] = int(P["off"][s]) & ~(1 << sp.sensor_no)
            else:
                P["off"][s] = int(P["off"][s]) | (1 << sp.sensor_no)
            if sp.bit:
                rt.gimbal._apply_active(s, sp.name, on)
            if k is not None:
                rt.payload._apply_illum(s, rig, k)

        todo.append(act)
    if args.get("mode") is not None:
        cmd = _MODE_CMD.get(str(args["mode"]))
        if cmd is None:
            raise SensorError(110, "MODE_UNKNOWN", field="mode", value=args["mode"])
        if owner is None or owner.zoom is None or owner.name != sp.name:
            raise SensorError(560, "MODE_UNSUPPORTED", sensor=sp.name)
        if cmd == ImagingCmd.NIR and not rt.payload.has_illuminator(rig, k):
            raise SensorError(560, "NIR_NEEDS_ILLUMINATOR", sensor=sp.name)
        todo.append(lambda cmd=cmd: rt.payload.set_mode(s, k, cmd, rt.ctx))
    il = args.get("illum")
    if il is not None:
        if not isinstance(il, dict) or k is None:
            raise SensorError(560, "ILLUM_UNSUPPORTED", sensor=sp.name)
        sets: list[tuple[int, IllumCfg]] = []
        for key, idx in (("builtin", 0), ("ext", 1)):
            v = il.get(key)
            if v is None:
                continue
            c = _ILL.get(str(v))
            if c is None:
                raise SensorError(110, "ILLUM_VALUE_UNKNOWN", field=f"illum.{key}", value=v)
            if idx == 0 and (owner is None or owner.illum is None) and c == IllumCfg.ON:
                raise SensorError(560, "NO_BUILTIN_ILLUMINATOR", sensor=sp.name)
            if idx == 1 and c == IllumCfg.ON and not any(f.illum is not None for f in rig.followers(k)):
                raise SensorError(563, "ILLUMINATOR_UNAVAILABLE", sensor=sp.name)
            sets.append((idx, c))

        def ill(sets=sets) -> None:
            for idx, c in sets:
                P["ill_cfg"][s, k, idx] = int(c)
            rt.payload._apply_illum(s, rig, k)

        todo.append(ill)
    if args.get("resolution") is not None:
        res = str(args["resolution"])
        if owner is None or owner.zoom is None or owner.name != sp.name:
            raise SensorError(560, "RESOLUTION_UNSUPPORTED", sensor=sp.name)
        if res not in ("1080p", "4k"):
            raise SensorError(561, "RESOLUTION_UNKNOWN", field="resolution", value=res)
        if res == "4k":
            z = owner.zoom
            caps = rt._caps_for_pidx(int(rt.S.profile_id[s]))
            cfg = rt.vcfg.get(rt.uav_id(rt.S, s)) or {}
            caps = list(caps) + list(cfg.get("capabilities") or [])
            if "4k" not in [o.name for o in z.outputs] or (z.k4_requires and z.k4_requires not in caps):
                raise SensorError(561, "ANALYSIS_4K_NEEDS_CAPABILITY", field="resolution", requires=z.k4_requires)
        todo.append(lambda res=res: P["res"].__setitem__((s, k), 1 if res == "4k" else 0))
    if args.get("palette") is not None:
        pal = str(args["palette"])
        if sp.kind != SensorKind.THERMAL or k is None:
            raise SensorError(560, "PALETTE_UNSUPPORTED", sensor=sp.name)
        if pal not in ("white_hot", "black_hot"):
            raise SensorError(561, "PALETTE_UNKNOWN", field="palette", value=pal)
        todo.append(lambda pal=pal: P["pal"].__setitem__((s, k), 0 if pal == "white_hot" else 1))
    if args.get("cue") is not None:
        cv = str(args["cue"])
        if cv not in ("on", "off"):
            raise SensorError(110, "CUE_VALUE_UNKNOWN", field="cue", value=cv)
        cols = [k] if k is not None else [c for c in range(len(rig.col_specs)) if rig.col_specs[c] is not None
                                          and rig.col_specs[c].gimbal is not None]
        if not cols:
            raise SensorError(560, "NO_GIMBAL")

        def cue(cols=cols, cv=cv) -> None:
            for c in cols:
                P["cue"][s, c] = 1 if cv == "on" else 2

        todo.append(cue)
    if not todo:
        raise SensorError(110, "SENSOR_MODE_EMPTY")

    def go() -> None:
        for fn in todo:
            fn()

    rt.schedule(apply_tick, go)
    return _ok(sensor=sp.name)
