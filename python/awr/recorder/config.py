"""recorder 与 replay-worker 的参数（M12 §9.5、§6.10）。

`configs/runtime.yaml` 的 `recorder:`、`replay:` 段由 M11 所有（pydantic 严格模型，尚未登记这两段，已请求 M11 追加）；
在登记前本模块以 §9.5 的默认值为准，允许经 `AWR_REC_*`、`AWR_REPLAY_*` 环境变量覆盖（测试与基准用）。
控制律常量（主循环 20 ms、flush 1 s、meta 10 s、预读窗口、BUFFERING 100 ms、空闲退出 60 s）按 §6.2 计时器表登记。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

__all__ = ["RecorderCfg", "ReplayCfg", "load_recorder_cfg", "load_replay_cfg"]


@dataclass(frozen=True)
class PolicyCfg:
    swarm_hz: float = 25.0  # 块桶宽 max(40 ms, rate × 20 ms)
    full_hz: float = 125.0  # Full64 桶宽 max(8 ms, rate × 8 ms)
    keyframe_every_s: float = 5.0  # 关键块周期【仿真】
    state_ext_hz: float = 2.0
    safety_hz: float = 5.0
    pose_hz: float = 10.0
    max_marked: int = 16
    full_all_if_n_le: int = 50
    env_heartbeat_hz: float = 1.0


@dataclass(frozen=True)
class RecorderCfg:
    loop_ms: int = 20  # 主循环【墙钟】
    queue_mb: int = 64  # writer 有界队列
    flush_wall_s: float = 1.0  # chunk flush【墙钟】
    meta_every_s: float = 10.0  # meta.json 更新【墙钟】
    disk_check_s: float = 10.0  # 磁盘守卫【墙钟】
    disk_min_gb: float = 5.0  # quota.disk_min_gb（19 OPS-FR-036）
    close_timeout_s: float = 5.0  # 关闭时等待队列排空
    chunk_size: int = 4 << 20  # 4 MiB（16 §13.3）
    ovw_bin_s: float = 1.0  # 概览 bin【仿真】
    perf_every_s: float = 1.0
    interest_debounce_s: float = 0.25
    policy: PolicyCfg = field(default_factory=PolicyCfg)


@dataclass(frozen=True)
class ReplayCfg:
    cache_mb: int = 64
    readahead_mb: int = 64
    idle_exit_s: float = 60.0
    loop_hz_max: float = 250.0
    speed_min: float = 0.1
    speed_max: float = 20.0
    speed_max_bytes_per_s: float = 64 * 1024 * 1024  # 64 MB/s
    speed_max_events_per_s: float = 5000.0
    readahead_min_s: float = 2.0  # 预读窗口 max(2 s, 1.5 s × rate)
    readahead_wall_s: float = 1.5
    buffering_after_s: float = 0.1  # 缺数据超过 100 ms【墙钟】置 BUFFERING
    lowfreq_ext_hz: float = 2.0
    lowfreq_safety_hz: float = 5.0
    sensor_hz: float = 10.0
    perf_every_s: float = 1.0


def _env_num(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def load_recorder_cfg() -> RecorderCfg:
    c = RecorderCfg()
    return replace(c, loop_ms=int(_env_num("AWR_REC_LOOP_MS", c.loop_ms)), queue_mb=int(_env_num("AWR_REC_QUEUE_MB", c.queue_mb)),
                   flush_wall_s=_env_num("AWR_REC_FLUSH_S", c.flush_wall_s), meta_every_s=_env_num("AWR_REC_META_S", c.meta_every_s),
                   disk_min_gb=_env_num("AWR_QUOTA_DISK_MIN_GB", c.disk_min_gb))


def load_replay_cfg() -> ReplayCfg:
    c = ReplayCfg()
    return replace(c, idle_exit_s=_env_num("AWR_REPLAY_IDLE_EXIT_S", c.idle_exit_s),
                   cache_mb=int(_env_num("AWR_REPLAY_CACHE_MB", c.cache_mb)))
