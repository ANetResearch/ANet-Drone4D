"""M26 智能体观测 SDK 的 Env 侧（AWR-05 §5.5；`bench/obs.v1` 的唯一出口与 `bench/act.v1` 的唯一入口）。

- `awr.agentsdk.env`：lockstep Env（BenchEnv）、观测投影（L1 检测级）、导航解、动作守卫、链路层与运行写入器。
- `awr.agentsdk.task`：由事件版本目录生成任务契约 `bench/task.v1` 与 Agent 简报。

Agent 侧（研究者实现的 DroneAgent、基线策略）在独立 SDK 包 `drone4d`（tools/sdk/drone4d），不导入本包。
"""

from __future__ import annotations
