"""sim-core 扩展点注册表（M08 §7.1.1，签名在 D1-MS1 冻结；AWR-03 §4.3；M08-FR-011、FR-012、FR-055）。

M07、M09、M10、M13 在各自插件模块被组合根导入时调用下列函数登记，不修改 `awr/sim/runtime`：

- `@register_stage(name, every, phase, order)`：pipeline stage；`owner` 缺省按函数所在包推断（`budgets.PACKAGE_OWNER`），
  推断不出时按 order 所在区段推断；`budget_core` 缺省取 `budgets.BUDGET_CORE`，stage 名不在表中且未显式给出时拒绝；
- `register_state_block(name, owner, fields)`：他模块的 SoA 由 M08 统一分配（FleetState.blocks[name]）；
- `register_admission_check(step, name, fn)`：准入第 ④ 或 ⑧ 步的检查（也可作装饰器）；
- `register_slow_task(name, fn, ...)`：慢任务轮转（主循环剩余预算内执行，FR-004）；
- `register_query(name, fn)`：`ctl/sim-core/query` 的路由（M07 `env/query`）；
- `register_energy_model(model)`、`register_safety_hooks(hooks)`：M09 各调用一次，重复登记即失败。

度量注册表在 `awr/sim/core/metrics.py`，运动提供者注册在 `awr/sim/core/command.py`（M08 §7.1.1）。

校验（M08-FR-012；M08-AC-003）：`tick_hz % every == 0`、`0 ≤ phase < every`、name 与 order 唯一、order 落在所有者区段、
Σbudget ≤ 0.40、同一字段只有一个写者。登记时即可判定的项在登记时抛 ValueError/KeyError，其余在 `Pipeline.build` 时判定。
本模块只保存登记，不执行任何 stage；不使用墙钟（ADR-049）。
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from enum import IntFlag
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from . import budgets as B

if TYPE_CHECKING:
    from awr.sim.core.interfaces import EnergyModel, SafetyHooks
    from awr.sim.fleet.pipeline import StageCtx
    from awr.sim.fleet.state import FleetState

__all__ = [
    "AdmissionCheck",
    "CommandHandlerSpec",
    "Fidelity",
    "QuerySpec",
    "Registry",
    "SlowTaskSpec",
    "StageFn",
    "StageSpec",
    "StateBlockSpec",
    "energy_model",
    "infer_owner",
    "isolated_registry",
    "register_admission_check",
    "register_command_handler",
    "register_energy_model",
    "register_query",
    "register_safety_hooks",
    "register_slow_task",
    "register_stage",
    "register_state_block",
    "register_state_ext_hook",
    "registry",
    "reset_registry",
    "safety_hooks",
]


class Fidelity(IntFlag):
    L1 = 1
    L2 = 2
    EXT = 4
    L0 = 8
    ALL = 15


StageFn = Callable[["FleetState", "StageCtx"], None]


@dataclass(frozen=True)
class StageSpec:
    name: str
    every: int
    phase: int
    order: int
    fn: Callable[..., None]
    owner: str
    fidelity: Fidelity = Fidelity.ALL
    budget_core: float = 0.0
    writes: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    shards: int = 1
    shard: tuple[int, int] = (0, 1)  # (k, n)：分片展开后的第 k 片
    builtin: bool = False


@dataclass(frozen=True)
class StateBlockSpec:
    name: str
    owner: str
    fields: dict[str, tuple[np.dtype, tuple[int, ...]]]
    checkpoint: bool = True


@dataclass(frozen=True)
class AdmissionCheck:
    step: int
    name: str
    fn: Callable[..., Any]
    owner: str | None


@dataclass(frozen=True)
class SlowTaskSpec:
    name: str
    fn: Callable[..., None]
    period_wall_s: float | None
    period_sim_s: float | None
    budget_us: int


@dataclass(frozen=True)
class QuerySpec:
    name: str
    fn: Callable[..., bytes]


@dataclass(frozen=True)
class CommandHandlerSpec:
    op: str
    fn: Callable[..., dict]
    owner: str | None
    need_seat: bool = True


@dataclass
class Registry:
    stages: list[StageSpec] = field(default_factory=list)
    blocks: dict[str, StateBlockSpec] = field(default_factory=dict)
    admission: list[AdmissionCheck] = field(default_factory=list)
    slow: list[SlowTaskSpec] = field(default_factory=list)
    queries: dict[str, QuerySpec] = field(default_factory=dict)
    commands: dict[str, CommandHandlerSpec] = field(default_factory=dict)
    state_ext_hooks: list[tuple[str | None, Callable[..., None]]] = field(default_factory=list)
    energy: Any = None
    hooks: Any = None

    def stage_names(self) -> set[str]:
        return {s.name for s in self.stages}

    def admission_checks(self, step: int) -> list[AdmissionCheck]:
        return [c for c in self.admission if c.step == step]


_REG = Registry()


def registry() -> Registry:
    """当前进程的登记表（组合根导入插件之后读取）。"""
    return _REG


def reset_registry() -> None:
    """清空全部登记（只用于测试；sim-core 进程内不调用）。"""
    global _REG
    _REG = Registry()


@contextlib.contextmanager
def isolated_registry() -> Iterator[Registry]:
    """测试用：在一段代码内使用空登记表，结束后恢复原表。"""
    global _REG
    saved = _REG
    _REG = Registry()
    try:
        yield _REG
    finally:
        _REG = saved


# ---------------------------------------------------------------- owner 推断
def infer_owner(module: str | None, order: int | None = None) -> str | None:
    """按包前缀推断所有者；推断不出时按 order 所在区段推断（测试桩插件与第三方位置）。"""
    if module:
        for prefix, owner in B.PACKAGE_OWNER:
            if module == prefix or module.startswith(prefix + "."):
                return owner
    if order is not None:
        for owner, ranges in B.ORDER_RANGES.items():
            if any(lo <= order <= hi for lo, hi in ranges):
                return owner
    return None


def _check_timing(name: str, every: int, phase: int, order: int, shards: int) -> None:
    if not isinstance(every, int) or every < 1 or B.TICK_HZ % every != 0:
        raise ValueError(f"stage {name!r}: every={every} 必须整除 tick_hz={B.TICK_HZ}（M08-FR-012）")
    if not isinstance(phase, int) or not 0 <= phase < every:
        raise ValueError(f"stage {name!r}: phase={phase} 必须满足 0 ≤ phase < every={every}")
    if not isinstance(order, int) or not 0 <= order <= 199:
        raise ValueError(f"stage {name!r}: order={order} 超出 0..199")
    if not isinstance(shards, int) or shards < 1:
        raise ValueError(f"stage {name!r}: shards={shards} 必须 ≥ 1")


def _check_owner_range(name: str, owner: str, order: int, shards: int) -> None:
    ranges = B.ORDER_RANGES.get(owner)
    if ranges is None:
        raise ValueError(f"stage {name!r}: 未知所有者 {owner!r}")
    for k in range(shards):
        o = order + k
        if not any(lo <= o <= hi for lo, hi in ranges):
            raise ValueError(f"stage {name!r}: order {o} 不在 {owner} 的区段 {ranges} 内（M08 §6.4.2）")


def _resolve_budget(name: str, budget_core: float | None) -> float:
    if budget_core is not None:
        if budget_core < 0:
            raise ValueError(f"stage {name!r}: budget_core 不能为负")
        return float(budget_core)
    key = B.base_name(name)
    if key not in B.BUDGET_CORE:
        raise KeyError(f"stage {name!r} 不在预算表（budgets.BUDGET_CORE）中且未显式给出 budget_core（M08-FR-012）")
    return B.BUDGET_CORE[key]


def make_stage(name: str, every: int, phase: int, order: int, fn: Callable[..., None], *, owner: str | None = None,
               fidelity: Fidelity = Fidelity.ALL, budget_core: float | None = None, writes: tuple[str, ...] = (),
               reads: tuple[str, ...] = (), shards: int = 1, builtin: bool = False) -> list[StageSpec]:
    """构造（并在 shards > 1 时展开）StageSpec；不登记。M08 自身的 stage 经此函数构造（builtin = True）。"""
    _check_timing(name, every, phase, order, shards)
    own = owner or infer_owner(getattr(fn, "__module__", None), order)
    if own is None:
        raise ValueError(f"stage {name!r}: 无法推断所有者（请显式给出 owner）")
    _check_owner_range(name, own, order, shards)
    budget = _resolve_budget(name, budget_core)
    base = StageSpec(name, every, phase, order, fn, own, Fidelity(fidelity), budget, tuple(writes), tuple(reads), shards,
                     (0, 1), builtin)
    if shards == 1:
        return [base]
    # 分片相位间隔：every 为 50 Hz 周期（5 tick）的整数倍时取 5 的倍数，各片与首片落在同一 tick % 5 相位类（不与 50 Hz stage
    # 同 tick；every 25 且 ≤ 4 片时为 5，与此前 fleet_guard 的 4、9、14、19 相同，ADR-070）；否则 every // (shards + 1)
    if every % 5 == 0 and every // 5 > shards:
        step = 5 * max(1, (every // 5) // (shards + 1))
    else:
        step = max(1, every // (shards + 1))
    per = budget / shards
    return [replace(base, name=f"{name}.{k}", order=order + k, phase=(phase + k * step) % every, budget_core=per,
                    shard=(k, shards)) for k in range(shards)]


# ---------------------------------------------------------------- 登记函数（签名冻结，M08 §7.1.1）
def register_stage(name: str, every: int, phase: int, order: int, *, owner: str | None = None,
                   fidelity: Fidelity = Fidelity.ALL, budget_core: float | None = None,
                   writes: tuple[str, ...] = (), reads: tuple[str, ...] = (),
                   shards: int = 1) -> Callable[[StageFn], StageFn]:
    """装饰器：登记 pipeline stage。`(tick - phase) % every == 0` 时执行；shards > 1 时展开为 `<name>.<k>`。"""

    def deco(fn: StageFn) -> StageFn:
        specs = make_stage(name, every, phase, order, fn, owner=owner, fidelity=fidelity, budget_core=budget_core,
                           writes=writes, reads=reads, shards=shards)
        names = _REG.stage_names()
        orders = {s.order for s in _REG.stages}
        for s in specs:
            if s.name in names:
                raise ValueError(f"stage 重名：{s.name!r}")
            if s.order in orders:
                raise ValueError(f"stage {s.name!r}: order {s.order} 已被占用")
        _REG.stages.extend(specs)
        return fn

    return deco


def register_state_block(name: str, owner: str, fields: dict[str, tuple[np.dtype, tuple[int, ...]]], *,
                         checkpoint: bool = True) -> None:
    """登记状态块；FleetState 构造时按容量分配 `blocks[name][field]`，形状 (capacity, *shape)。"""
    if name in _REG.blocks:
        raise ValueError(f"状态块重复登记：{name!r}")
    norm: dict[str, tuple[np.dtype, tuple[int, ...]]] = {}
    for f, spec in fields.items():
        dt, shape = spec
        norm[f] = (np.dtype(dt), tuple(int(x) for x in shape))
    _REG.blocks[name] = StateBlockSpec(name, owner, norm, checkpoint)


def register_admission_check(step: Literal[4, 8], name: str, fn: Callable[..., Any] | None = None, *,
                             owner: str | None = None) -> Callable:
    """准入第 ④（状态）或 ⑧（围栏粗校验）步的检查；`fn(req, ctx) -> AdmitResult`。fn 缺省时作装饰器使用。"""
    if step not in (4, 8):
        raise ValueError(f"准入检查只能登记在第 4 或第 8 步：{step}")

    def add(f: Callable[..., Any]) -> Callable[..., Any]:
        if any(c.name == name for c in _REG.admission):
            raise ValueError(f"准入检查重名：{name!r}")
        _REG.admission.append(AdmissionCheck(int(step), name, f, owner or infer_owner(getattr(f, "__module__", None))))
        return f

    if fn is None:
        return add
    return add(fn)


def register_slow_task(name: str, fn: Callable[[StageCtx], None], *, period_wall_s: float | None = None,
                       period_sim_s: float | None = None, budget_us: int = 200) -> None:
    """慢任务：主循环剩余预算内轮转执行（FR-004）；可按墙钟或仿真时钟设置周期，缺省每轮都有机会执行。"""
    if any(t.name == name for t in _REG.slow):
        raise ValueError(f"慢任务重名：{name!r}")
    if budget_us <= 0:
        raise ValueError(f"慢任务 {name!r}: budget_us 必须 > 0")
    _REG.slow.append(SlowTaskSpec(name, fn, period_wall_s, period_sim_s, int(budget_us)))


def register_query(name: str, fn: Callable[..., bytes]) -> None:
    """`ctl/sim-core/query` 的 op 路由（例如 `env/query`）；处理在慢任务中执行。"""
    if name in _REG.queries:
        raise ValueError(f"查询重复登记：{name!r}")
    _REG.queries[name] = QuerySpec(name, fn)


def register_command_handler(op: str, fn: Callable[..., dict], *, owner: str | None = None, need_seat: bool = True) -> None:
    """非机体命令的 `ctl/sim-core/cmd` 路由（追加的扩展点，M08 §7.1.1 之外；例如 M07 `env/set`、`env/preset`、`env/gust`，
    M10 `mission/start|pause|resume|abort`，M09 `fault/inject`）。

    CommandEngine 在验签与角色检查（外部写操作须 operator/admin 且持席位，`need_seat = False` 时只验签）之后，于步顶同步调用
    `fn(msg, apply_tick, ctx) -> {status: accepted|rejected, code, detail?, warnings?, result?}`，把结果作为 Admission 回复，
    并按 cid 进入幂等表。处理函数不得阻塞（重活放慢任务）；同一 op 只允许一个处理者。"""
    if op in _REG.commands:
        raise ValueError(f"命令处理者重复登记：{op!r}")
    if not op or op in ("fleet/add", "fleet/remove", "scenario/metric") or op.startswith("fleet/cmd"):
        raise ValueError(f"保留的 op：{op!r}")
    _REG.commands[op] = CommandHandlerSpec(op, fn, owner or infer_owner(getattr(fn, "__module__", None)), bool(need_seat))


def register_state_ext_hook(fn: Callable[..., None], *, owner: str | None = None) -> None:
    """`state/sim-core/ext` 打包钩子（追加的扩展点；M13-to-M08 第 2 条）：`fn(slots, t_sim_ns, out)` 在每片（≤ 16 架）打包时于
    主循环线程调用，`out[i]` 为第 i 个 slot 正在装配的 state_ext 对象，钩子原地合并本模块负责的字段（M13：`loc.gnss_fix/sats/
    eph_m/epv_m/hdop`、`loc.err_enu_m`、`sens`）；字段须已在 `uav_state_ext.schema.json` 登记。钩子不得使用 RNG 或阻塞。
    同一函数重复登记被忽略（插件对登记表幂等）。"""
    if not callable(fn):
        raise TypeError("state_ext 钩子不可调用")
    if any(f is fn for _o, f in _REG.state_ext_hooks):
        return
    _REG.state_ext_hooks.append((owner or infer_owner(getattr(fn, "__module__", None)), fn))


def register_energy_model(model: EnergyModel) -> None:
    """M09 调用一次；重复登记即失败（M08 §7.1.1）。"""
    if _REG.energy is not None:
        raise ValueError("EnergyModel 已登记（只允许一个实现）")
    _check_protocol(model, ("estimate", "rtl_plan", "path_wh"), "EnergyModel")
    _REG.energy = model


def register_safety_hooks(hooks: SafetyHooks) -> None:
    """M09 调用一次；重复登记即失败。"""
    if _REG.hooks is not None:
        raise ValueError("SafetyHooks 已登记（只允许一个实现）")
    _check_protocol(hooks, ("on_stream_watchdog", "on_spawn", "on_remove", "apply_operator", "on_lease_event",
                            "on_gcs_beacon", "on_agent_liveliness", "matrix_verdict"), "SafetyHooks")
    _REG.hooks = hooks


def energy_model() -> EnergyModel | None:
    return _REG.energy


def safety_hooks() -> SafetyHooks | None:
    return _REG.hooks


def _check_protocol(obj: Any, methods: tuple[str, ...], what: str) -> None:
    missing = [m for m in methods if not callable(getattr(obj, m, None))]
    if missing:
        raise TypeError(f"{what} 缺少方法：{missing}")


def describe(spec: StageSpec) -> dict[str, Any]:
    """构建日志与 meta.json 用的 stage 描述。"""
    return {"name": spec.name, "every": spec.every, "phase": spec.phase, "order": spec.order, "owner": spec.owner,
            "budget_core": spec.budget_core, "fidelity": int(spec.fidelity), "shard": list(spec.shard),
            "fn": f"{getattr(spec.fn, '__module__', '?')}.{getattr(spec.fn, '__qualname__', type(spec.fn).__name__)}",
            "coroutine": inspect.iscoroutinefunction(spec.fn)}
