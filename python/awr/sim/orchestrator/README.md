# awr.sim.orchestrator（V0.2）

SIH 等外部飞控后端的控制面（M08 §6.12）：独立进程 `sim-orch`（唯一持有 docker.sock，只操作标签 `awr.role=px4-sitl`、
`awr.run=<run>` 的容器），SessionSpec → Planner（sysid、`PX4_HOME_*`、`SIH_LOC_*`、`PX4_PARAM_*`）→ DockerSihDriver（每机一容器
`px4 -i 0`）→ HealthMonitor L0–L4 → 重启预算与退避 → Teardown。数据面由 px4-bridge-k 实现的 SimBackend 承担。

D1 只交付：本目录、`session.py`（SessionSpec 草案与容量准入纯函数，schema `packages/contracts/sim/session_spec.schema.json`）。
实现与验收见 M08 §6.12、§2.2（V0.2：N = 4 时 READY < 15 s；8 架 RTF ≥ 0.92）。
