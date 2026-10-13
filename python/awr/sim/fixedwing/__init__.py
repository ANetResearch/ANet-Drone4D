"""M21 固定翼与 VTOL 运动学（AWR-04 §5.3；ADR-096；AWR-20 §5）。

`params`（运行参数）、`kinematics`（协调转弯积分与向量场导引，纯函数）、`energy`（功率模型与能量口径）、`vtol`（模式判据与任务
剖面）、`runtime`（`fw_kin` stage、构型运动提供者、安全动作、准入第 ⑥ 步与 `fw/state` 查询）。sim-core 组合根入口为
`awr.sim.fixedwing.plugin`（导入即登记）。
"""
