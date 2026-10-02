"""M13 组合根入口（M13-FR-004；M13 §6.2、§7.1；M08 §7.1.1；AWR-10 §3.3 规则 1）。

导入本模块即调用 `install()`（`configs/runtime.yaml` 的 sim-core `plugins:` 写 `awr.sim.sensors.plugin`；在该条目改名之前，
包 `awr.sim.sensors` 只在 `AWR_PLUGINS` 列出它时转而导入本模块，见包文档）。`install()` 对当前登记表幂等，登记：

- 状态块 `sensors`（`block.FIELDS`）与 `sensor_targets`（Mock 目标表，前 64 行），均参与 checkpoint；
- stage `sensors`（every 5、phase 2、order 100，budget 0.010 核）；
- 慢任务 `m13.sensor_pose`（SensorPose48 10 Hz 分片打包，自计时）与 `m13.noisy_obs`（2 Hz【仿真】白噪声观测）；
- `GET /api/fleet/profiles/{id}` 的 `sensors{}` 描述函数（M08 `profiles.register_sensor_describer`，每进程一次）；
- M08 的 roster 传感器组钩子与 state_ext 钩子（`register_state_ext_hook`，GNSS、云台、IMU 字段）；M08 若提供
  `register_estimate_hook`，同时登记 `conf_expected`。

`runtime()` 返回当前登记表对应的 SensorRuntime（M10、M14 经它调用云台、目标、传感器开关与几何 API）。
"""

from __future__ import annotations

import logging
from typing import Any

from .block import BLOCK, FIELDS, TARGET_BLOCK
from .detector import TARGET_FIELDS
from .runtime import SensorRuntime
from .stage import EVERY, ORDER, PHASE, make_stage

__all__ = ["install", "installed", "runtime", "uninstall"]

log = logging.getLogger("awr.sim.sensors")
_INSTALLED: dict[int, SensorRuntime] = {}
_DESCRIBER = [False]

WRITES = tuple(f"{BLOCK}.{f}" for f in FIELDS)


def _registry():
    from awr.sim.fleet.stages import registry as R

    return R


def install() -> SensorRuntime:
    R = _registry()
    reg = R.registry()
    rt = _INSTALLED.get(id(reg))
    if rt is not None:
        return rt
    rt = SensorRuntime()
    R.register_state_block(BLOCK, "M13", dict(FIELDS))
    R.register_state_block(TARGET_BLOCK, "M13", dict(TARGET_FIELDS))
    R.register_stage("sensors", every=EVERY, phase=PHASE, order=ORDER, owner="M13", fidelity=R.Fidelity.ALL,
                     budget_core=0.010, writes=WRITES)(make_stage(rt))
    R.register_slow_task("m13.sensor_pose", rt.packer.run_slow, budget_us=200)
    R.register_slow_task("m13.noisy_obs", rt.obs.run_slow, period_sim_s=0.5, budget_us=300)
    _register_optional_hooks(rt)
    _INSTALLED[id(reg)] = rt
    _warm_kernels()
    return rt


_WARMED = [False]


def _warm_kernels() -> None:
    """云台融合核的 numba 预热（运行期签名：状态块 C 连续数组、ENU 只读视图；M08-FR-005，ADR-070）。装配即预热，
    sim-core 在 ready 之前完成编译或读缓存；同一进程只做一次。失败只记日志（运行期按需编译，只损失时延）。"""
    if _WARMED[0]:
        return
    _WARMED[0] = True
    try:
        from . import kernels_gimbal, kernels_gnss

        kernels_gimbal.warmup()
        kernels_gnss.warmup()
    except Exception:
        log.exception("m13 numba warmup failed")


def _register_optional_hooks(rt: SensorRuntime) -> None:
    if not _DESCRIBER[0]:
        try:
            from awr.sim.fleet import profiles

            from .describe import describe

            profiles.register_sensor_describer(describe)
            _DESCRIBER[0] = True
        except Exception:
            log.warning("sensor describer not registered", exc_info=True)
    R = _registry()
    ext = getattr(R, "register_state_ext_hook", None)  # M13-to-M08 第 2 条：GNSS、云台、IMU 字段并入 state_ext
    if callable(ext):
        try:
            ext(rt.state_ext_fields, owner="M13")
        except Exception:
            log.warning("state_ext hook not registered", exc_info=True)
    hook = getattr(R, "register_estimate_hook", None)
    if callable(hook):
        try:
            hook("conf_expected", _conf_expected)
        except Exception:
            log.warning("estimate hook not registered", exc_info=True)
    for modname in ("awr.sim.core.roster", "awr.sim.fleet.profiles"):
        try:
            import importlib

            mod = importlib.import_module(modname)
        except Exception:
            continue
        fn = getattr(mod, "register_sensor_rig", None)
        if callable(fn):
            try:
                fn(_rig_entries_for_profile)
            except Exception:
                log.warning("roster sensor rig hook not registered", exc_info=True)
            break


def _rig_entries_for_profile(profile_id: str, names: list[str] | None = None) -> list[dict]:
    from .describe import dir_for_profile
    from .spec import rig_entries, rig_for_model

    d = dir_for_profile(profile_id)
    return [] if d is None else rig_entries(rig_for_model(d.name, d.parent), names)


def _conf_expected(profile_id: str, p_uav: Any, p_tgt: Any, capability: str, env: Any = None, t_sim_ns: int = 0) -> float | None:
    """M08 estimate 的 `conf_expected`（M13-FR-045；M14 §6.10.5）：机型上声明该能力的检测器的期望 P_d（FOV 视为 1）。"""
    from .describe import dir_for_profile
    from .detector import expected_pd
    from .spec import rig_for_model

    d = dir_for_profile(profile_id)
    if d is None:
        return None
    for s in rig_for_model(d.name, d.parent).specs:
        if s.detector is not None and s.detector.capability == capability:
            return float(expected_pd(s.detector, p_uav, p_tgt, env, t_sim_ns)[0])
    return None


def runtime() -> SensorRuntime | None:
    R = _registry()
    return _INSTALLED.get(id(R.registry()))


def installed() -> dict[int, SensorRuntime]:
    return dict(_INSTALLED)


def uninstall() -> None:
    """测试用：从当前登记表移除本模块登记的 stage、状态块与慢任务。"""
    R = _registry()
    reg = R.registry()
    rt = _INSTALLED.pop(id(reg), None)
    if rt is None:
        return
    reg.stages[:] = [s for s in reg.stages if s.name != "sensors"]
    reg.blocks.pop(BLOCK, None)
    reg.blocks.pop(TARGET_BLOCK, None)
    reg.slow[:] = [t for t in reg.slow if not t.name.startswith("m13.")]
    hooks = getattr(reg, "state_ext_hooks", None)
    if hooks is not None:
        hooks[:] = [h for h in hooks if h[0] != "M13"]


install()
