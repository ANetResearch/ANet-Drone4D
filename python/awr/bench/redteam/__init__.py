"""泄漏红队（M26 §8.2、§8.3；D3-AC-16）：录制排名运行中 Agent 收到的全部消息（`capture`），汇总运行的保密对象与真值量
（`truth`），按结构化检索 R1 与高熵字节检索 R2 输出 `leak_audit_report`（`search`）；`harness` 给出本机套接字与 dmax 容器
两种运行路径。种子与噪声反演（R3）与行为攻击（R4）在同一框架上扩展（`tests/d3/leak_redteam/`）。"""

from awr.bench.redteam.capture import CaptureAudit, CapturedFrame, read_capture
from awr.bench.redteam.harness import (
    RedteamRun,
    baseline_launcher,
    leak_report,
    record_container_episode,
    record_socket_episode,
    write_report,
)
from awr.bench.redteam.search import audit_frames
from awr.bench.redteam.truth import ProbedEnv, Secrets, TruthSet

__all__ = ["CaptureAudit", "CapturedFrame", "ProbedEnv", "RedteamRun", "Secrets", "TruthSet", "audit_frames", "baseline_launcher",
           "leak_report", "read_capture", "record_container_episode", "record_socket_episode", "write_report"]
