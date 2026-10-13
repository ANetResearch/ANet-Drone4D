"""M20 组合根入口（M20 §7.3 `install()`；M08 §15.3.1 扩展点；d2-WP-02-to-M20、d2-WP-05-to-M20 第 4 条）。

导入本模块即调用 `install()`（sim-core 的 `plugins:` 写 `awr.sim.tasking.plugin`；`AWR_PLUGINS` 列出包名 `awr.sim.tasking` 时包初始化也会
转而导入本模块）。`install()` 对当前登记表幂等，登记：

- stage `tasking_guard`（5 Hz，order 185，预算 0.0008 核）与 `tasking`（1 Hz，order 186，预算 0.0012 核）；
- 实体块 `tasking`（`tgt_relay`：处于接力中的识别物，M08 `tap_targets` 打包进 TargetLite32 `best` 的 RELAY 位）；
- 命令 `nest/add|update|remove`、`task/create|start|pause|resume|cancel`、`tasking/plan`；
- 查询 `nest/home`、`tasking/vehicle_work`（M08 调用）与 `tasking/*`（`ctl/sim-core/tasking`，REST `rest/tasking.py`）；
- 会话配置段 `nests`（order 30）与 `tasks`（order 60）的装配与导出；批处理结果 `relay_stats`；checkpoint 扩展段 `x.tasking`
  （`ckpt.py`，M20-FR-075）；`sim/reset` 时清空。
M20 的四类规划作业由 `awr.tasking.plan_kinds` 登记（`AWR_PLAN_PLUGINS` 缺省清单已含）。`runtime()` 返回当前登记表对应的 TaskingRuntime。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import numpy as np

from awr.tasking import plan_kinds as _plan_kinds  # noqa: F401  作业类别登记（导入即登记）
from awr.tasking.model import TaskingError

from .queries import COMMANDS, QUERIES, command_handlers, query_handlers
from .runtime import TaskingRuntime

__all__ = ["BLOCK", "OWNER", "install", "runtime", "uninstall"]

log = logging.getLogger("awr.sim.tasking")
OWNER = "M20"
BLOCK = "tasking"
METRIC_RTK = "tasking.rtk_cover"
FIELDS = {"tgt_relay": (np.uint8, ())}
_INSTALLED: dict[int, TaskingRuntime] = {}


def _registry():
    from awr.sim.fleet.stages import registry as R

    return R


def runtime(create: bool = True) -> TaskingRuntime | None:
    reg = _registry().registry()
    rt = _INSTALLED.get(id(reg))
    if rt is not None and "tasking" not in reg.stage_names():
        rt = None
    if rt is None and create:
        return install()
    return rt


def uninstall() -> None:
    R = _registry()
    reg = R.registry()
    rt = _INSTALLED.pop(id(reg), None)
    if rt is None:
        return
    reg.stages[:] = [s for s in reg.stages if s.name not in ("tasking", "tasking_guard")]
    (reg.entity_blocks.get("target") or {}).pop(BLOCK, None)
    for op in COMMANDS:
        h = reg.commands.get(op)
        if h is not None and h.owner == OWNER:
            reg.commands.pop(op, None)
    for q in QUERIES:
        reg.queries.pop(q, None)
    for sec in ("nests", "tasks"):
        reg.session_loaders.pop(sec, None)
        reg.snapshot_exporters.pop(sec, None)
    reg.batch_outputs.pop("relay_stats", None)
    (getattr(reg, "ckpt_exts", None) or {}).pop("tasking", None)
    with contextlib.suppress(Exception):
        from awr.sim.core.metrics import unregister_metric

        unregister_metric(METRIC_RTK)


def install() -> TaskingRuntime:
    R = _registry()
    reg = R.registry()
    rt = _INSTALLED.get(id(reg))
    if rt is not None and "tasking" in reg.stage_names() and "task/create" in reg.commands:
        return rt
    rt = TaskingRuntime()

    def st_guard(S: Any, ctx: Any) -> None:
        rt.st_guard(S, ctx)

    def st_tasking(S: Any, ctx: Any) -> None:
        rt.st_tasking(S, ctx)
        _write_relay_col(rt, ctx)

    st_guard.__module__ = st_tasking.__module__ = __name__
    R.register_stage("tasking_guard", hz=5.0, order=185, owner=OWNER, budget_core=0.0008, phase_slot=3)(st_guard)
    R.register_stage("tasking", hz=1.0, order=186, owner=OWNER, budget_core=0.0012, phase_slot=11)(st_tasking)
    with contextlib.suppress(ValueError):
        R.register_entity_block("target", BLOCK, OWNER, dict(FIELDS))
    for op, fn in command_handlers(rt).items():
        if op not in reg.commands:
            R.register_command_handler(op, fn, owner=OWNER)
    for name, fn in query_handlers(rt).items():
        if name not in reg.queries:
            R.register_query(name, fn)

    def load_nests(items: list, api: Any) -> list:
        _bind_api(rt, api)
        probs = []
        for k, d in enumerate(items or []):
            try:
                rt.add_nest(dict(d))
            except TaskingError as e:
                probs.append({"section": "nests", "index": k, "code": e.code, "detail": e.detail})
        return probs

    def load_tasks(items: list, api: Any) -> list:
        _bind_api(rt, api)
        rt.sync_nests()
        probs = []
        for k, d in enumerate(items or []):
            try:
                rt.create_task({kk: d.get(kk) for kk in ("type", "label", "params", "common") if kk in d}, auto_start=True)
            except TaskingError as e:
                probs.append({"section": "tasks", "index": k, "code": e.code, "detail": e.detail})
        return probs

    def export_nests(api: Any) -> list:
        out = []
        for _nid, n in sorted(rt.nests.items()):
            c = n.config([])
            c.pop("inventory", None)
            c.pop("vehicle_ids", None)
            out.append(c)
        return out

    def export_tasks(api: Any) -> list:
        return [{"type": T.type, "label": T.spec.get("label"), "params": T.params, "common": T.common}
                for T in sorted(rt.tasks.values(), key=lambda x: x.tid) if T.parent is None and T.active]

    def relay_stats(ctx: Any) -> dict:
        items = []
        for T in sorted(rt.tasks.values(), key=lambda x: x.tid):
            if T.relay is None:
                continue
            rt.relays.finalize(T)        # 批处理结束：闭合进行中的间隙并发 relay.gap（统计与事件流一致）
            items.append({**rt.relays.stats_wire(T), "ext": rt.relays.stats_ext(T)})
        return {"v": 1, "items": items}

    for fn in (load_nests, load_tasks, export_nests, export_tasks, relay_stats):
        fn.__module__ = __name__
    with contextlib.suppress(ValueError):
        R.register_session_loader("nests", load_nests, owner=OWNER, order=30)
    with contextlib.suppress(ValueError):
        R.register_session_loader("tasks", load_tasks, owner=OWNER, order=60)
    with contextlib.suppress(ValueError):
        R.register_snapshot_exporter("nests", export_nests)
    with contextlib.suppress(ValueError):
        R.register_snapshot_exporter("tasks", export_tasks)
    with contextlib.suppress(ValueError):
        R.register_batch_output("relay_stats", relay_stats)
    _register_ckpt(R, rt)
    _register_rtk_metric(rt)
    _INSTALLED[id(reg)] = rt
    return rt


def _register_ckpt(R: Any, rt: TaskingRuntime) -> None:
    """checkpoint 扩展段 `x.tasking`（M20-FR-075；D2-AC-45 驻留恢复后任务与接力状态一致）：M08 捕获时编码、恢复时解码（`ckpt.py`）。"""
    fn = getattr(R, "register_checkpoint_extension", None)
    if not callable(fn):
        return
    from . import ckpt as CK

    def ck_save() -> bytes:
        return CK.encode(rt)

    def ck_restore(blob: bytes) -> None:
        CK.decode_into(rt, blob)

    ck_save.__module__ = ck_restore.__module__ = __name__
    with contextlib.suppress(ValueError):
        fn("tasking", ck_save, ck_restore, owner=OWNER)


def _register_rtk_metric(rt: TaskingRuntime) -> None:
    """M13-FR-211（P1，d2-WP-10-to-M20 第 3 条）：度量 `tasking.rtk_cover(x_m, y_m)` = 1.0 当该点在某个带 `rtk_base` 的机巢的链路距离内，
    否则 0.0；会话没有任何机巢时抛 LookupError（M13 按"度量不可用"保持机型 GNSS 配置，D1 共享展示行为不变）。M13 据此给出
    `sensor/nav` 的定位档。重名（另一登记表的同一进程）时替换为当前运行时。"""
    try:
        from awr.sim.core import metrics as MT
    except Exception:  # pragma: no cover - M08 未装配
        return

    def rtk_cover(x_m: float = 0.0, y_m: float = 0.0, **_: Any) -> float:
        cur = runtime(create=False) or rt
        if not cur.nests:
            raise LookupError("no nests")
        for n in cur.nests.values():
            if not n.cfg.get("rtk_base"):
                continue
            rng = n.cfg.get("link_range_m")
            d = float(np.hypot(float(x_m) - n.pos3[0], float(y_m) - n.pos3[1]))
            if rng is None or d <= float(rng):
                return 1.0
        return 0.0

    rtk_cover.__module__ = __name__
    MT.unregister_metric(METRIC_RTK)
    MT.register_metric(METRIC_RTK, rtk_cover, owner=OWNER)


def _bind_api(rt: TaskingRuntime, api: Any) -> None:
    fleet = getattr(getattr(api, "core", None), "fleet", None)
    rt.bind(getattr(fleet, "S", None), api.ctx)


def _write_relay_col(rt: TaskingRuntime, ctx: Any) -> None:
    ents = getattr(ctx, "entities", None)
    tab = ents.get("target") if ents is not None and hasattr(ents, "get") else None
    if tab is None or "tgt_relay" not in getattr(tab, "cols", {}):
        return
    active = {T.relay.target for T in rt.tasks.values() if T.relay is not None and T.state == "RUNNING" and T.relay.state not in ("ENDED",)}
    col = tab.cols["tgt_relay"]
    with contextlib.suppress(Exception):
        for m in tab.ordered():
            col[m.row] = 1 if m.id in active else 0


install()
