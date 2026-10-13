"""sandbox-pool：多租户沙盒会话池（M17；AWR-04 §4、ADR-089、ADR-091、ADR-093、ADR-113；17 §17.3、§17.4、§17.10）。

一个会话 = 一个 run = 一个沙盒 sim-core 进程（sandbox100）。本包是池进程 `python -m awr.sandbox.pool` 的全部逻辑：

| 模块 | 内容 |
|---|---|
| `model` | 会话记录、排队票、世界成本、标识生成与结束原因的线上映射（M17 §6.2） |
| `config` | 由 `sandbox:` 段（`awr.runtime.config.SandboxCfg`）推出的池参数、会话上限对象 `awr.sandbox.limits.v1` |
| `prefix`、`pow`、`tokens` | 地址前缀与聚合前缀、自托管工作量证明（17 §17.3.5）、沙盒 token 与排队票令牌（17 §17.3.1、§17.3.2） |
| `admission`、`governor`、`ladder` | N_max、世界加权准入、倍速授予与回调、降级阶梯 L0 至 L4（M17 §6.6.1、§6.6.4 至 §6.6.6） |
| `queue`、`broker` | 排队票状态机与 ETA、会话状态机、配额、续期、抢占、空闲回收（M17 §6.3、§6.4、§6.6.1 至 §6.6.3） |
| `state` | `var/sandbox/state.json` 合批落盘与重启加载（M17-FR-027） |
| `worlds`、`templates`、`snapshot` | 世界表（可用性、成本、授权说明）、场景模板目录、配置快照校验 |
| `spawner`、`cgroups`、`adopt` | 拉起沙盒 sim-core（setsid、叶子组、日志文件）、叶子组与统计、池重启后的进程接管 |
| `sessionbus`、`planhost` | 每会话总线会话（perf、时钟、快照、重置）、共享规划服务宿主（M10 `PlanServer`） |
| `pool` | 进程入口：单 asyncio 事件循环装配上述组件，池命名空间 `sys/session/*` 服务 |
| `ctl`、`batch` | 运维 CLI 与本机批处理（P1） |

broker、admission、governor、ladder、queue 为纯逻辑（时间由注入时钟给出），便于单测；进程与系统调用集中在 spawner、cgroups、
adopt、sessionbus 与 pool。
"""

from __future__ import annotations

__all__: list[str] = []
