"""sim-core 后台线程的 CPU 亲和性（ADR-017 CPU 分区的细化；FX2-R3-sim，ADR-070）。

supervisor 把 sim-core 进程钉在单核（`configs/runtime.yaml` `cpus: [1]`），之后创建的全部线程继承这一亲和性：zenoh 的 I/O
线程、checkpoint 写线程、输入日志写线程、plan-pool 的管理与队列线程都与 250 Hz 主循环挤在同一个核上，它们一旦可运行，
内核就从主循环切走时间片，单步墙钟随之抬高（D1 验收第 2 轮：同一负载下单步 p99 进程内 CPU 时间约 4 ms、多进程墙钟 5–6.5 ms；
FX2-R3 自测：checkpoint 写线程约 0.06 核，全部在主循环的核上）。

做法：主循环线程保持 supervisor 给定的亲和性；其余线程改到"后台核"集合：`AWR_SIM_AUX_CPUS`（逗号分隔）给出时用之，
否则取主循环亲和性之外的全部在线 CPU。主循环未被钉核（亲和性已是全部 CPU，例如 ci profile）时什么都不做。新线程会
继承创建它的线程（多数是主循环）的亲和性，因此由慢任务周期性（2 s【墙钟】）重扫 `/proc/self/task`；单次扫描约 30 个线程，
耗时约 0.1 ms。CPU 用量的统计口径不变（线程仍属 sim-core 进程，D1-AC-07 的 CPU ≤ 0.6 核照常计入）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading

__all__ = ["AuxPinner", "aux_cpus_for"]

log = logging.getLogger("awr.sim.runtime.cpuaff")


def aux_cpus_for(main_cpus: set[int], ncpu: int | None = None, env: str | None = None) -> set[int] | None:
    """后台线程的 CPU 集合；不需要调整（主循环未钉核、信息缺失或配置无效）时返回 None。"""
    n = int(ncpu or os.cpu_count() or 0)
    if not main_cpus or n <= 0 or len(main_cpus) >= n:
        return None
    if env:
        try:
            cpus = {int(x) for x in env.split(",") if x.strip()}
        except ValueError:
            log.warning("AWR_SIM_AUX_CPUS invalid", extra={"kv": {"value": env}})
            return None
        cpus = {c for c in cpus if 0 <= c < n}
        return cpus or None
    rest = set(range(n)) - main_cpus
    return rest or None


class AuxPinner:
    """把主循环线程以外的线程改到后台核；`scan()` 返回本次改动的线程数。"""

    def __init__(self, main_tid: int | None = None, aux: set[int] | None = None) -> None:
        self.main_tid = int(main_tid if main_tid is not None else threading.get_native_id())
        main_cpus: set[int] = set()
        with contextlib.suppress(OSError, AttributeError):
            main_cpus = set(os.sched_getaffinity(self.main_tid))
        self.main_cpus = main_cpus
        self.aux = aux if aux is not None else aux_cpus_for(main_cpus, env=os.environ.get("AWR_SIM_AUX_CPUS"))
        self.moved = 0
        self.errors = 0

    @property
    def active(self) -> bool:
        return self.aux is not None and hasattr(os, "sched_setaffinity")

    def scan(self) -> int:
        if not self.active:
            return 0
        try:
            tids = [int(t) for t in os.listdir("/proc/self/task")]
        except OSError:
            return 0
        n = 0
        for tid in tids:
            if tid == self.main_tid:
                continue
            try:
                cur = set(os.sched_getaffinity(tid))
                if cur == self.aux or not (cur & self.main_cpus):
                    continue  # 已在后台核，或线程自行设置过与主循环无交集的亲和性
                os.sched_setaffinity(tid, self.aux)
                n += 1
            except (OSError, ValueError):
                self.errors += 1  # 线程已退出等
        self.moved += n
        return n
