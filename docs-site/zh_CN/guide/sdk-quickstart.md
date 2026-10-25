# ANet4D Python SDK 快速上手

<!-- 本页正文由仓库 docs/sdk/quickstart.zh-CN.md 引入，修改请编辑该文件。 -->

本文在 15 min 内完成一次完整的上手流程：安装 `anet4d` SDK，编写一个示例 Agent，在高保真仿真档（`physics = realistic`，Gazebo Sim 锁步）上以 3 架机运行一个 60 s 的 L1 回合，并核对线协议接入与进程内运行的动作流逐字节一致，最后以快速仿真档（`physics = fast`）不改代码地重复同一回合。文中每条命令的输出均来自实际运行，测量条件见第 0 节。

```{include} ../../../docs/sdk/quickstart.zh-CN.md
:start-line: 3
```
