"""M21 机型目录（AWR-04 §5.4；AWR-20；ADR-095、ADR-096、ADR-097、ADR-101、ADR-115）。

纯函数模块（不登记任何扩展点，可被工具、测试与 sim-core 插件共用）：`signature`（谱形与签名、SG 检查）、`derive`（固定翼与
VTOL 派生值、盘旋半径、质量与能量修正）、`charge`（充电时间与稳态）、`loader`（`catalog.json` 与 CI 检查、能力合成、机巢兼容）、
`checks`（FW、VT、SG、MT、SN 检查及其向 M08 的登记）、`clone`（会话克隆补丁）、`view`（目录 REST 视图）、`check`（CLI
`python -m awr.sim.catalog.check`）。sim-core 组合根入口为 `awr.sim.catalog.plugin`（导入即登记）。
"""
