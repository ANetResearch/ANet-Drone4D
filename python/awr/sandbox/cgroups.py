"""沙盒的 cgroup 叶子组与统计（M17-FR-026、FR-023、FR-032；AWR-04 §4.4.4）。系统调用经 `awr.runtime.cgroup`（M11）。

- 每会话叶子组 `sandbox/sb/<sid>/`：`cpu.weight 100`、`memory.high` S 级 230M / L 级 250M、`memory.max 320M`，结束时删除；
- 共享规划 worker 的叶子组 `sandbox/plan/`：`cpu.max 0.5` 核、`memory.max 250M`、`cpu.weight 50`（PlanServer 的 worker 自己 join）；
- 会话父组 `sandbox/sb/`：`cpu.max = sandbox/cpu.max − C_ctl`（D2-FX-R2；`cgroup-exec.sh` 建立时写入，池启动时复写）：会话合计超出
  预算时在本组内限流，sandbox-pool 与 sandbox-api 所在的 `sandbox/ctl/` 不随整个 `sandbox/` 组的带宽耗尽而停顿；
- 拉起加权（D2-FX-R2，D2-AC-02）：SPAWNING 期间叶子组 `cpu.weight` 取 `SPAWN_WEIGHT`（冷启动的 import、numba 缓存加载与装配是
  单核突发，与满载会话同权时拉起时长随负载成倍增加），就绪或失败后恢复 100；同一时刻至多一个会话处于拉起（M17-FR-007）；
- 沙盒组统计（降级阶梯的输入）：`sandbox/cpu.stat`（nr_periods、nr_throttled）与 `sandbox/sb/cpu.stat`（会话父组的限流）、
  `memory.stat` 的 anon + shmem、PSI；
- 无委派（父组 `sandbox/sb` 不存在或不可写，Q-D2-07）：不写入任何 cgroup 文件，沙盒 sim-core 以 `nice +5` 运行，池以 RSS 看门狗
  兜底（匿名页 > `rss_watchdog_mb` 即终止，按崩溃处理），统计改读 `/proc/<pid>`。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from awr.runtime import cgroup as cg
from awr.runtime.procutil import sample

__all__ = ["CgroupManager"]

MB = 1 << 20
SPAWN_WEIGHT = 1000      # 拉起中叶子组的 cpu.weight（cgroup v2 上限 10000；满载会话为 100）
LEAF_WEIGHT = 100
SB_PARENT = "sandbox/sb"


class CgroupManager:
    def __init__(self, sb: Any, *, env: dict[str, str] | None = None, delegated: bool | None = None) -> None:
        self.sb = sb
        self.env = env or None
        if delegated is None:
            p = cg.leaf_path("sandbox/sb", self.env)
            delegated = p is not None and p.is_dir() and os.access(p, os.W_OK)
        self.delegated = bool(delegated)

    @staticmethod
    def leaf_rel(sid: str) -> str:
        return f"sandbox/sb/{sid}"

    def create(self, sid: str, world_class: str, *, spawning: bool = False) -> Path | None:
        if not self.delegated:
            return None
        c = self.sb.cgroup
        high = c.leaf_mem_high_mb.L if world_class == "L" else c.leaf_mem_high_mb.S
        return cg.create_leaf(self.leaf_rel(sid), {"cpu_weight": SPAWN_WEIGHT if spawning else LEAF_WEIGHT,
                                                   "memory_high": f"{int(high)}M",
                                                   "memory_max": f"{int(c.leaf_mem_max_mb)}M"}, env=self.env)

    def settle(self, sid: str) -> bool:
        """拉起结束（就绪或失败）：叶子组 `cpu.weight` 回到 100。"""
        if not self.delegated:
            return True
        return cg.set_limits(self.leaf_rel(sid), cpu_weight=LEAF_WEIGHT, env=self.env)

    def ensure_sessions_parent(self, cores: float) -> bool:
        """会话父组 `sandbox/sb/` 的 `cpu.max`（核）；无委派或写入失败时 False（不影响拉起）。"""
        if not self.delegated or cores <= 0:
            return False
        return cg.set_limits(SB_PARENT, cpu_max=max(0.2, float(cores)), env=self.env)

    def remove(self, sid: str) -> bool:
        if not self.delegated:
            return True
        return cg.remove_leaf(self.leaf_rel(sid), env=self.env)

    def ensure_plan_leaf(self) -> str | None:
        """规划 worker 的叶子组（PlanServer 构造前建好；无委派时 None，worker 以 nice 运行）。"""
        if not self.delegated:
            return None
        c = self.sb.cgroup
        p = cg.create_leaf("sandbox/plan", {"cpu_max": float(c.plan_cpu_max), "memory_max": f"{int(c.plan_mem_max_mb)}M",
                                            "cpu_weight": 50}, env=self.env)
        return "sandbox/plan" if p is not None else None

    def group_stats(self) -> dict[str, Any] | None:
        """沙盒组读数：cpu_stat、mem_mb（anon + shmem）、psi_cpu、psi_mem（some avg10）。无委派时 None。"""
        if not self.delegated:
            return None
        r = cg.read("sandbox", env=self.env)
        if r is None:
            return None
        ms = r.get("memory_stat") or {}
        sbr = cg.read(SB_PARENT, env=self.env)
        return {"cpu_stat": r.get("cpu_stat") or {}, "cpu_stat_sb": (sbr or {}).get("cpu_stat") or None,
                "mem_mb": (int(ms.get("anon", 0)) + int(ms.get("shmem", 0))) / MB if ms else None,
                "psi_cpu": ((r.get("cpu_pressure") or {}).get("some") or {}).get("avg10"),
                "psi_mem": ((r.get("memory_pressure") or {}).get("some") or {}).get("avg10")}

    def leaf_stats(self, sid: str, pid: int | None) -> dict[str, Any]:
        """每会话：CPU 秒与匿名页（叶子组优先，否则 `/proc/<pid>`）。"""
        if self.delegated:
            r = cg.read(self.leaf_rel(sid), env=self.env)
            if r is not None:
                ms = r.get("memory_stat") or {}
                cs = r.get("cpu_stat") or {}
                return {"cpu_s": int(cs.get("usage_usec", 0)) / 1e6,
                        "anon_mb": (int(ms.get("anon", 0)) + int(ms.get("shmem", 0))) / MB}
        if pid:
            s = sample(pid)
            if s is not None:
                return {"cpu_s": s[0], "rss_mb": s[1]}
        return {}
