"""sim-core 插件：env stage、心跳与 EnvSample32 慢任务、`env/query` 路由（M07-FR-017、FR-022–FR-024；M07 §7.1；ADR-021、ADR-025）。

组合根按 `AWR_PLUGINS`（runtime.yaml `plugins`）导入本模块，导入即登记（签名以 M08 §7.1.1 为准）：

- `env` stage：every 5、phase 0、order 20（250 Hz 主时钟下 50 Hz，子步零阶保持），写 FleetState 的 wind（经
  `set_wind_from_enu`，ENU 去向 -> NED 空气速度）、rho、env_flags、env_gust；
- 慢任务 `env.heartbeat`（1 Hz【墙钟】，`state/sim-core/env` 完整关键帧字节）与 `env.detail`（10 Hz【仿真】，兴趣集
  EnvSample32，`state/sim-core/detail`）；
- 查询 `env/query`（`ctl/sim-core/query`）。

EnvironmentService 由首个调用（stage 或慢任务）按世界构造并挂到 `ctx.env`（M08 组合根可改为显式注入，见
`.cache/impl/requests/M07-to-M08.md`）；`env/set`、`env/preset`、`env/gust` 经 M08 的 `register_command_handler` 登记到
`handle_command`（CommandEngine 验签、角色与席位检查之后在步顶同步调用）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from awr.contracts import bus_keys
from awr.sim.fleet.stages import registry as _R
from awr.sim.fleet.stages.registry import register_query, register_slow_task, register_stage

from .field import EnvironmentServiceImpl
from .query import Fields
from .weather.presets import EnvError

__all__ = ["ENV_STAGE_FIELDS", "ensure_service", "env_stage", "handle_command", "publish_detail", "publish_heartbeat", "service"]

log = logging.getLogger("awr.environment.stage")

ROOT = Path(__file__).resolve().parents[3]
ENV_STAGE_FIELDS = int(Fields.WIND | Fields.WIND_PARTS | Fields.TURB_SPEC | Fields.OPTICS | Fields.PRECIP | Fields.THERMO)
DT_ENV_S = 0.02
TICK_NS = 4_000_000

_st: dict[str, Any] = {"env": None, "ctx": None, "S": None, "pub_hb": None, "pub_detail": None, "bus": None, "keyframe_sent": 0}


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_service(world: Any, *, world_id: str | None = None, worlds_dir: Path | None = None, world_seed: int | None = None,
                  epoch: int = 1, capacity: int = 1024, rng: dict | None = None, load_assets: bool = True) -> EnvironmentServiceImpl:
    """按世界目录构造服务：env.json（粗糙度、廓线、默认预设）、coordinate.json（ground.zM、anchor.hMslM）、共享资产。"""
    wid = world_id or getattr(world, "world_id", None) or os.environ.get("AWR_WORLD", "shenzhen")
    wdir = Path(worlds_dir or os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds")
    seed = int(world_seed if world_seed is not None else (os.environ.get("AWR_WORLD_SEED") or 0))
    env_world = _read_json(wdir / wid / "environment" / "env.json")
    coord = _read_json(wdir / wid / "coordinate.json")
    csha = getattr(world, "coordinate_sha256", None)
    if env_world and csha and str(env_world.get("coordinate_hash", "")).removeprefix("sha256:") != str(csha).removeprefix("sha256:"):
        log.warning("env.json coordinate_hash does not match the world (352 COORDINATE_MISMATCH); using defaults",
                    extra={"kv": {"world": wid}})
        env_world = None
    rng = rng or {}
    return EnvironmentServiceImpl(world_id=wid, world_query=world, env_world=env_world, coordinate=coord, world_seed=seed, epoch=epoch,
                                  shared_dir=wdir / "_shared", rng_gust=rng.get("gust_schedule"), rng_dryden=rng.get("dryden"),
                                  capacity=capacity, load_assets=load_assets)


def service() -> EnvironmentServiceImpl | None:
    return _st["env"]


def ensure_service(ctx: Any, S: Any = None) -> EnvironmentServiceImpl:
    env = getattr(ctx, "env", None)
    if isinstance(env, EnvironmentServiceImpl):
        _st["env"] = env
        return env
    env = _st["env"] if _st["ctx"] is ctx else None
    if env is None:
        _st["ctx"] = ctx
        ev = getattr(ctx, "events", None)
        cap = int(getattr(S, "capacity", 0) or getattr(getattr(ctx, "cfg", None), "capacity", 0) or 1024)
        env = build_service(getattr(ctx, "world", None), epoch=int(getattr(ev, "epoch", 1) or 1), capacity=cap,
                            rng=getattr(ctx, "rng", None))
        _st["env"] = env
        _st["keyframe_sent"] = 0
        log.info("environment service ready", extra={"kv": {"world": env.world_id, "seed": env.seed, "assets": env.assets,
                                                            "turbulence": env.kf.config["wind"]["turbulence"]["model"]}})
    with contextlib.suppress(AttributeError):
        ctx.env = env
    return env


def reset_plugin_state() -> None:
    """测试用：丢弃进程内的服务单例。"""
    for k in ("env", "ctx", "S", "pub_hb", "pub_detail", "bus"):
        _st[k] = None
    _st["keyframe_sent"] = 0


def _emit_frames(ctx: Any, env: EnvironmentServiceImpl, frames: list) -> None:
    ev = getattr(ctx, "events", None)
    if ev is None:
        env.outbox.clear()
        return
    t = int(getattr(ctx, "t_ns", 0))
    if _st["keyframe_sent"] == 0 and not frames:
        frames = [env.keyframe()]
    for kf in frames:
        ev.emit("env.keyframe", t_sim_ns=t, severity=0, fields=kf.to_wire())
        _st["keyframe_sent"] = kf.version
    for kind, data in env.outbox:
        ev.emit(kind, t_sim_ns=t, severity=1 if kind == "env.warning" else 0, fields=data)
    env.outbox.clear()


@register_stage("env", every=5, phase=0, order=20, owner="M07", budget_core=0.08, writes=("wind", "rho", "env_flags", "env_gust"))
def env_stage(S: Any, ctx: Any) -> None:
    env = ensure_service(ctx, S)
    _st["S"] = S
    frames = env.on_env_tick_all(int(ctx.tick))
    if frames or env.outbox or _st["keyframe_sent"] == 0:
        _emit_frames(ctx, env, frames)
    act = S.active_idx()
    if act.size == 0:
        return
    pos = S.enu.pos[act]
    vel = S.enu.vel[act]
    env.step_dryden(act, pos, vel, DT_ENV_S)
    q = env.query(pos, int(ctx.t_ns), fields=ENV_STAGE_FIELDS, vel=vel, agent_idx=act, out=env.buf)
    n = q.n
    S.set_wind_from_enu(q.wind_mps[:n], act)
    S.rho[act] = q.rho_kgm3[:n]
    S.env_flags[act] = q.flags[:n]
    S.env_gust[act] = q.gust_long_mps[:n]
    env.store_rows(act, q)


def _bus(ctx: Any) -> Any:
    ev = getattr(ctx, "events", None)
    return getattr(ev, "bus", None)


def publish_heartbeat(ctx: Any) -> None:
    """1 Hz【墙钟】：完整关键帧字节（t_ns 与 anchors 为最近已推进的网格时刻），暂停时照发。"""
    env = ensure_service(ctx)
    bus = _bus(ctx)
    if bus is None:
        return
    if _st["bus"] is not bus or _st["pub_hb"] is None:
        _st["bus"] = bus
        _st["pub_hb"] = bus.publisher(bus_keys.STATE_ENV)
        _st["pub_detail"] = None
    _st["pub_hb"].put(env.heartbeat_bytes())


def detail_message(env: EnvironmentServiceImpl, S: Any, agent_nos: np.ndarray, t_sim_ns: int) -> bytes | None:
    if S is None or agent_nos.size == 0:
        return None
    act = S.active_idx()
    if act.size == 0:
        return None
    nos = np.asarray(S.agent_no[act])
    want = np.isin(nos, agent_nos.astype(nos.dtype))
    slots = act[want & env.row_valid[act]]
    if slots.size == 0:
        return None
    rows = env.sample32(slots)
    return msgpack.packb({"v": 1, "t_sim_ns": int(t_sim_ns), "agent_no": [int(x) for x in S.agent_no[slots]], "rows": rows.tobytes()},
                         use_bin_type=True)


def publish_detail(ctx: Any) -> None:
    """10 Hz【仿真】：兴趣集（≤ 64 架，含标记集）的 EnvSample32。"""
    env = _st["env"]
    bus = _bus(ctx)
    interest = np.asarray(getattr(ctx, "interest", np.zeros(0, np.uint16)))
    if env is None or bus is None or interest.size == 0:
        return
    raw = detail_message(env, _st["S"], interest, int(getattr(ctx, "t_ns", 0)))
    if raw is None:
        return
    if _st["bus"] is not bus or _st["pub_detail"] is None:
        _st["bus"] = bus
        _st["pub_detail"] = bus.publisher(bus_keys.STATE_DETAIL)
    _st["pub_detail"].put(raw)


def query_route(req: Any, ctx: Any) -> dict:
    env = ensure_service(ctx)
    msg = req if isinstance(req, dict) else {}
    return env.handle_query(msg)


def handle_command(msg: dict, apply_tick: int, ctx: Any = None) -> dict:
    """CommandEngine 接线（M07-to-M08）：`env/set`、`env/preset`、`env/gust` -> {status, code, detail?, result?}。"""
    env = ensure_service(ctx) if ctx is not None else _st["env"]
    if env is None:
        return {"status": "rejected", "code": 211, "detail": {"why": "ENV_NOT_READY"}}
    p = msg.get("principal") if isinstance(msg.get("principal"), dict) else {}
    by = "scenario" if p.get("_internal") and p.get("principal_id") in (None, "scenario", "director") else str(p.get("principal_id") or "")
    try:
        op = env.op_from_msg(str(msg.get("op", "")), msg.get("args") or {}, by=by)
        r = env.apply(op, int(apply_tick))
    except EnvError as ex:
        return {"status": "rejected", "code": ex.code, "detail": ex.detail}
    return {"status": "accepted", "code": 0, "warnings": r.warnings,
            "result": {"version": r.version, "t_apply_ns": r.t_apply_ns, "route": r.route, "warnings": r.warnings}}


def state_route(req: Any, ctx: Any) -> dict:
    """REST `GET /api/env/state?t_ns=`（实时）：当前版本在 t 的帧，锚点推进到 t。"""
    env = ensure_service(ctx)
    args = (req.get("args") if isinstance(req, dict) else None) or {}
    t = args.get("t_ns")
    if not isinstance(t, int) or isinstance(t, bool):
        return {"v": 1, "code": 300, "detail": {"field": "t_ns"}}
    try:
        return {"v": 1, "code": 0, "frame": env.state_at(t)}
    except EnvError as ex:
        return {"v": 1, "code": ex.code, "detail": ex.detail}


def streamlines_route(req: Any, ctx: Any) -> dict:
    """REST `GET /api/env/streamlines/{lib}/d{deg}.awsl`（D1-ext）：解析场 AWSL 字节。"""
    env = ensure_service(ctx)
    args = (req.get("args") if isinstance(req, dict) else None) or {}
    try:
        return {"v": 1, "code": 0, "awsl": env.streamlines(str(args.get("lib", "")), int(args.get("dir_deg", 0)) % 360)}
    except EnvError as ex:
        return {"v": 1, "code": 305 if ex.code == 404 else ex.code, "detail": ex.detail}


register_slow_task("env.heartbeat", publish_heartbeat, period_wall_s=1.0, budget_us=300)
register_slow_task("env.detail", publish_detail, period_sim_s=0.1, budget_us=300)
register_query("env/query", query_route)
register_query("env/state", state_route)
register_query("env/streamlines", streamlines_route)
# CommandEngine routes non-vehicle commands to registered handlers (M08 extension point `register_command_handler`,
# signature fn(msg, apply_tick, ctx) -> {status, code, detail?, warnings?, result?}); guarded while M08 lands it
_reg_cmd = getattr(_R, "register_command_handler", None)
if _reg_cmd is not None:
    for _op in ("env/set", "env/preset", "env/gust"):
        _reg_cmd(_op, handle_command, owner="M07")
