"""D2 仿真日历、太阳位置、照度档、地表热背景与声学背景剖面（AWR-04 §6.2、§6.5、§7.3；ADR-101、ADR-102；M18-M19 PRD §6.11）。

纯函数（不读墙钟，ADR-049）与 sim-core 内的 SkyModel；与 `apps/web/src/engine/environment/sky/*.ts` 逐式对应
（golden `tests/environment/golden/d2_sky.json`）。所有者：M07（WP-09）。
"""
