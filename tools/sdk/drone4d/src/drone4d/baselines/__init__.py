"""基线策略（M26 §14.3，`policy.kind = reference`）：`static_guard`、`coverage_scan`、`track_share`（L1 检测级观测）。

同一种子两次运行的动作流逐字节一致：策略内无随机数、无墙钟依赖，集合按确定次序遍历。
"""

from drone4d.baselines.coverage_scan import CoverageScan
from drone4d.baselines.static_guard import StaticGuard
from drone4d.baselines.track_share import TrackShare

BASELINES = {"static_guard": StaticGuard, "coverage_scan": CoverageScan, "track_share": TrackShare}

__all__ = ["BASELINES", "CoverageScan", "StaticGuard", "TrackShare"]
