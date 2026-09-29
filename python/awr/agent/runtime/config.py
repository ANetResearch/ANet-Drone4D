"""AgentRuntimeConfig（M14 §6.18 默认值；§9.1：墙钟参数是本模块常量，不进 `runtime.yaml`）。"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["AgentRuntimeConfig"]


@dataclass(frozen=True)
class AgentRuntimeConfig:
    t_quote_s: float = 3.0  # 【仿真】
    find_latency_s: float = 0.2  # 【仿真】
    latency_s: tuple[float, float] = (0.9, 1.1)  # 【仿真】
    d_resp_s: float = 0.05  # 【仿真】
    quote_max_age_s: float = 30.0  # 【仿真】
    escalation_s: float = 120.0  # 【仿真】
    archive_s: float = 60.0  # 【仿真】
    merge_radius_m: float = 30.0
    max_active_tasks: int = 64
    poll_busy_s: float = 0.005  # 【墙钟】有待触发定时器
    poll_idle_s: float = 0.050  # 【墙钟】空闲
    status_period_s: float = 1.0  # 【墙钟】agent/{aid}/status
    rows_period_s: float = 0.5  # 【墙钟】机体状态读取 2 Hz
    pump_period_s: float = 0.02  # 【墙钟】事件与回复泵
    fsync_period_s: float = 1.0  # 【墙钟】证据与审计
    heartbeat_period_s: float = 0.1  # 【墙钟】hb.agent-runtime 10 Hz
    bus_timeout_s: float = 1.0  # 【墙钟】× 3
    degraded_ring_age_ms: float = 3000.0  # 【墙钟】sim-core 心跳超时判 DEGRADED
    world_seed: int = 0
    zero_latency: bool = False

    @classmethod
    def from_env(cls) -> AgentRuntimeConfig:
        seed = os.environ.get("AWR_WORLD_SEED") or os.environ.get("AWR_SEED") or "0"
        try:
            s = int(seed)
        except ValueError:
            s = 0
        return cls(world_seed=s, zero_latency=os.environ.get("AWR_AGENT_ZERO_LATENCY") == "1")
