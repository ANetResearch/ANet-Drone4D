"""M20 任务编排、机巢与接力监控的纯算法包（AWR-04 §8、§9；M20 PRD §6、§9.1；ADR-103 至 ADR-106、ADR-113；所有者 M20，WP-07）。

本包只依赖 numpy、numba（延迟导入）、`awr.swarm` 与调用方注入的世界查询；不导入 `awr.sim.runtime`、`awr.api`（M20-NFR-005）。
M19、M21 的纯函数由 sim-core 插件（`awr.sim.tasking`）或规划 worker 经参数注入，测试中注入桩。模块：

- `model`：机型、机体、机巢、工作项、快照、计划的数据类与线上形状；
- `geom`：多边形、折线、最小外接圆、偏移；
- `keepout`：禁入圆（2.5D 投影）、解析绕行长度、可见图绕行路径、1 m 采样校验；
- `nestmodel`：充电时间（AWR-20 §4.5 同式）、起降位时间线、稳态检查；
- `energy`：分段闭式能量估计；
- `scan`：GSD 反推、条带、可见格覆盖校验与补洞、架数决策（`kernels_cover` 为 numba 核）；
- `decompose`：七类任务的分解与 `register_decomposer`；
- `greedy`、`allocator`、`check`：默认贪心分配、Allocator 协议与注册表、外部计划与可行性检查器；
- `station`：接力与看洞的站位选择、`StationScorer`；
- `sustain`：可持续性评估；
- `plan_kinds`：共享规划服务作业 `tasking.scan`、`tasking.allocate`、`tasking.station`、`tasking.sustain` 的登记（导入轻量）。
"""

from __future__ import annotations

__all__: list[str] = []
