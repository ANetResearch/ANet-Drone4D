"""`python -m awr.sim.runtime.warm`：sim-core 全部 numba 核的预热入口（`make run` 前置 `numba-warm`；M08-FR-005）。

与 sim-core 启动序列走同一条路径：导入 `configs/runtime.yaml` 中 sim-core 的插件（导入即装配，各插件在装配时以运行期签名预热
自己的核：M09 安全核、M10 跟踪核、M07 湍流采样核、M13 云台与 LiDAR 核），再预热 M08 融合核（`kernels_l1.warmup`，含
`kernels_sel`）、plan-pool 工作进程使用的 M10 规划核（TOPP-lite、2.5D A*）与 M20 覆盖核（`tasking.kernels_cover`）。编译结果写入
`NUMBA_CACHE_DIR`，sim-core 启动时只读缓存。

D1 验收第 1 轮 4.1：此前各模块的预热签名与运行期不一致（只读 ENU 视图、只读 DSM/DTM 网格、float32 记分板），`make run`
的预热目标也只覆盖 M08 与 M10 的部分核，运行期首次进入相应路径时在主循环内编译 3–4 s，被 supervisor 的 2 s 活性阈值
杀掉后形成崩溃循环。`tests/sim/test_numba_signatures.py` 校验预热签名覆盖运行期签名。
"""

from __future__ import annotations

import os
import sys
import time

DEFAULT_PLUGINS = ("awr.environment.stage", "awr.sim.safety", "awr.sim.mission", "awr.sim.sensors.plugin")
# 沙盒 sim-core 与共享展示 S7 追加的 D2 插件（与 sandbox-pool `spawner.D2_PLUGINS`、supervisor `SHOW_D2_PLUGINS` 同一组；按名称装载，
# 缺失时跳过）：其核同样在装配时按运行期签名预热（D2-ACC-1 第 4.7 节：预热只覆盖 D1 组合根时首个沙盒会话在 tick 内编译）
D2_PLUGINS = ("awr.sim.catalog.plugin", "awr.sim.fixedwing.plugin", "awr.sim.targets.plugin", "awr.sim.perception.plugin",
              "awr.sim.tasking.plugin")

__all__ = ["D2_PLUGINS", "DEFAULT_PLUGINS", "main", "warm_all"]


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
    base = plugins or _plugins()
    compose_plugins(tuple(base) + tuple(p for p in D2_PLUGINS if p not in base))
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
    try:
        # M20 GSD 扫描的覆盖核（规划进程与批处理内的 `tasking.scan` 作业使用；D2-ACC-1 第 4.7 节运行期编译）
        import importlib

        t0 = time.perf_counter()
        importlib.import_module("awr.tasking.kernels_cover").warmup()
        out["m20_cover"] = time.perf_counter() - t0
    except Exception:  # pragma: no cover - M20 未交付时跳过
        pass
    try:
        # M04 几何核（los_batch；D2 WP-14）：sim-core 在 start() 内装载世界时使用，按名称装载
        import importlib

        t0 = time.perf_counter()
        importlib.import_module("awr.world.geometry").warm_kernels()
        out["m04_geom"] = time.perf_counter() - t0
    except Exception:  # pragma: no cover - 核缺失时 los_batch 走 numpy 路径（结果相同）
        pass
    return out


def main(argv: list[str] | None = None) -> int:
    t = warm_all()
    print("numba warmup s " + " ".join(f"{k}={v:.2f}" for k, v in t.items()), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
