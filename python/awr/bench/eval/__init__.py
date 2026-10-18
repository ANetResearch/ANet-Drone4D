"""M27 评测（§6.7.6；P0a）：EC 与覆盖、EU v0、AO_real v0 与 AO_pos、CE v0（归一化）、安全间距，输出 `bench/evaluation.v1`；
聚合与原型榜（`d3-score-0`，四项指标与 95% 置信区间，不含合成权重）。

输入为 Env 运行目录（awr.agentsdk.env.recorder 的布局）、事件版本目录（磁带与计分标注）与世界包（oracle 视线）。
"""

from __future__ import annotations
