"""多机互避的纯算法部分：转场分层（D1-ext）；全局 4D 预约为 V0.6。

所有者：M10（AWR-03 §4.3；M10 §9.1 细化子目录）。
"""

from .layers import transit_layers, transit_ranks

__all__ = ["transit_layers", "transit_ranks"]
