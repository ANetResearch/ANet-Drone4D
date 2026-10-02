"""`python -m awr.sim.runtime.warm`：sim-core 全部 numba 核的预热入口（`make run` 前置 `numba-warm`；M08-FR-005）。

与 sim-core 启动序列走同一条路径：导入 `configs/runtime.yaml` 中 sim-core 的插件（导入即装配，各插件在装配时以运行期签名预热
自己的核：M09 安全核、M10 跟踪核、M07 湍流采样核），再预热 M08 融合核（`kernels_l1.warmup`）与 plan-pool 工作进程使用的
M10 规划核（TOPP-lite、2.5D A*）。编译结果写入 `NUMBA_CACHE_DIR`，sim-core 启动时只读缓存。

D1 验收第 1 轮 4.1：此前各模块的预热签名与运行期不一致（只读 ENU 视图、只读 DSM/DTM 网格、float32 记分板），`make run`
的预热目标也只覆盖 M08 与 M10 的部分核，运行期首次进入相应路径时在主循环内编译 3–4 s，被 supervisor 的 2 s 活性阈值
杀掉后形成崩溃循环。`tests/sim/test_numba_signatures.py` 校验预热签名覆盖运行期签名。
"""

from __future__ import annotations

import os
import sys
import time

DEFAULT_PLUGINS = ("awr.environment.stage", "awr.sim.safety", "awr.sim.mission", "awr.sim.sensors.plugin")

__all__ = ["DEFAULT_PLUGINS", "main", "warm_all"]


def _plugins() -> tuple[str, ...]:
    env = os.environ.get("AWR_PLUGINS")
    if env:
        return tuple(p for p in env.split(",") if p)
    try:
        from awr.runtime.config import load_runtime_config

        cfg = load_runtime_config()
        for p in cfg.procs:
            if p.name == "sim-core" and getattr(p, "plugins", None):
                return tuple(p.plugins)
    except Exception:
        pass
    return DEFAULT_PLUGINS


def warm_all(plugins: tuple[str, ...] | None = None) -> dict[str, float]:
    """导入插件并预热全部核；返回各步耗时（秒，墙钟只用于日志）。"""
    from .main import compose_plugins, defer_numba_blas_probe

    out: dict[str, float] = {}
    defer_numba_blas_probe()
    t0 = time.perf_counter()
    compose_plugins(plugins or _plugins())
    out["plugins"] = time.perf_counter() - t0
    from ..fleet import kernels_l1 as KL

    t0 = time.perf_counter()
    KL.warmup()
    out["m08"] = time.perf_counter() - t0
    try:
        # plan-pool 工作进程使用的 M10 规划核：与插件一样按名称装载（组合根，AWR-10 §3.3 规则 1–2），M08 不静态导入 M10
        import importlib

        t0 = time.perf_counter()
        for name in ("awr.sim.planning.smooth", "awr.sim.planning.astar25"):
            importlib.import_module(name).warmup()
        out["m10_plan"] = time.perf_counter() - t0
    except Exception:  # pragma: no cover - 规划核缺失时只影响 plan-pool 首个作业的时延
        pass
    return out


def main(argv: list[str] | None = None) -> int:
    t = warm_all()
    print("numba warmup s " + " ".join(f"{k}={v:.2f}" for k, v in t.items()), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
