"""M23 sim-core 插件（§6.12.1；只出现在 D3_PLUGINS，D2 组合根不导入）：导入即 `install()`，对当前登记表幂等。

登记：会话配置段 `event`（order 45，vehicles 40 之后、targets 50 之前）、查询 `event/status`、慢任务 `m23.status`（1 s 墙钟，
只刷新状态缓存，不影响仿真）、批处理输出 `event_run`、stage `event_clock`（every 1、order 199、预算 0.0005 核）。
`tape_uri` 为相对路径时按 `AWR_D3_EVENTS_DIR`（缺省 var/d3/events）解析。识别物经 `target/add` 以内部 principal
`event:<event_id>` 加入（M18 须已安装）；运动源经 M18 扩展点 3 登记。世界版本取 `ctx.world` 的 `content_version`
（或 `meta["contentVersion"]`）；世界查询没有版本信息时跳过 615 校验并记问题 `EVENT_WORLD_UNCHECKED`。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from awr.sim.events.errors import EventError
from awr.sim.events.runtime import EventRuntime

__all__ = ["install", "runtime", "uninstall"]

OWNER = "M23"
ORDER = 199
_INSTALLED: dict[int, EventRuntime] = {}
_STATUS: dict[int, dict] = {}


def _registry():
    from awr.sim.fleet.stages import registry as R

    return R


def runtime(create: bool = True) -> EventRuntime | None:
    reg = _registry().registry()
    rt = _INSTALLED.get(id(reg))
    if rt is None and create:
        return install()
    return rt


def _principal(rt: EventRuntime) -> dict:
    eid = (rt.spec or {}).get("event_id", "unbound")
    return {"principal_id": f"event:{eid}", "role": "event", "entry": "internal"}


def _world_version(world: Any) -> str | None:
    v = getattr(world, "content_version", None)
    if v is None:
        meta = getattr(world, "meta", None)
        if isinstance(meta, dict):
            v = meta.get("contentVersion")
    return str(v) if v else None


def install() -> EventRuntime:
    R = _registry()
    reg = R.registry()
    rt = _INSTALLED.get(id(reg))
    if rt is not None and "event" in reg.session_loaders:
        return rt
    rt = EventRuntime()

    def load_event(items: Any, api: Any) -> list[dict]:
        if not isinstance(items, dict) or not items:
            return []
        probs: list[dict] = []
        wv = _world_version(getattr(api, "world", None))
        if wv is None:
            probs.append({"section": "event", "code": "EVENT_WORLD_UNCHECKED"})
        try:
            rt.load(items, world_content_version=wv, data_root=Path(os.environ.get("AWR_D3_EVENTS_DIR", "var/d3/events")))
        except EventError as e:
            return [*probs, {"section": "event", "code": e.code, "detail": str(e)}]
        tg = sys.modules.get("awr.sim.targets.plugin")
        trt = tg.runtime(create=False) if tg is not None else None
        if trt is None:
            return [*probs, {"section": "event", "code": "EVENT_NO_TARGETS"}]
        trt.set_motion_source(rt.source)
        for cfg in rt.target_configs():
            r = api.submit("target/add", {"target": cfg}, principal=_principal(rt)["principal_id"])
            if (r or {}).get("status") != "accepted":
                probs.append({"section": "event", "code": "EVENT_TARGET_REJECTED", "detail": {"id": cfg["id"], "result": r}})
        return probs

    def event_clock(S: Any, ctx: Any) -> None:
        if rt.reader is None:
            return
        calls = getattr(ctx, "calls", None)
        p = _principal(rt)

        def submit(cid: str, args: dict) -> dict:
            if calls is None:
                return {"status": "skipped"}
            return calls.submit_internal({"cid": cid, "op": "env/set", "args": args}, p)

        dt = round(float(getattr(ctx, "stage_dt", 0.0) or getattr(ctx, "dt_tick", 0.01)) * 1e9)
        rt.event_clock(int(ctx.t_ns), dt, submit)

    def q_status(m: dict, ctx: Any = None) -> dict:
        return _STATUS.get(id(reg)) or rt.status()

    def publish_status(ctx: Any) -> None:
        _STATUS[id(reg)] = rt.status()

    def event_run(ctx: Any) -> dict:
        return rt.run_record()

    for fn in (load_event, event_clock, q_status, publish_status, event_run):
        fn.__module__ = __name__
    R.register_session_loader("event", load_event, owner=OWNER, order=45)
    if "event/status" not in reg.queries:
        R.register_query("event/status", q_status)
    R.register_slow_task("m23.status", publish_status, period_wall_s=1.0)
    R.register_batch_output("event_run", event_run)
    R.register_stage("event_clock", every=1, phase=0, order=ORDER, owner=OWNER, budget_core=0.0005)(event_clock)
    _INSTALLED[id(reg)] = rt
    return rt


def uninstall() -> None:
    reg = _registry().registry()
    _INSTALLED.pop(id(reg), None)
    _STATUS.pop(id(reg), None)
    reg.session_loaders.pop("event", None)
    reg.queries.pop("event/status", None)
    reg.batch_outputs.pop("event_run", None)
    reg.slow[:] = [t for t in reg.slow if t.name != "m23.status"]
    reg.stages[:] = [s for s in reg.stages if s.name != "event_clock"]


install()
