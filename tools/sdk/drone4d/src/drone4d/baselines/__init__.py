"""基线策略（M26 §14.3，`policy.kind = reference`）：`static_guard`、`coverage_scan`、`track_share`（L1 检测级观测），以及 r6 §12.3
参考行补充的 `hover_plaza`、`track_solo`；6 种参考策略（M26-FR-081）以 r6 行名登记在 `REFERENCE6`（`drone4d.baselines.reference`），
行名同时是 `BASELINES` 的键，指向该行的策略类。

同一种子两次运行的动作流逐字节一致：策略内无随机数、无墙钟依赖，集合按确定次序遍历。
"""

from drone4d.baselines.coverage_scan import CoverageScan
from drone4d.baselines.hover_plaza import HoverPlaza
from drone4d.baselines.reference import REFERENCE6, Reference, check_reference_spec, reference_run_spec
from drone4d.baselines.static_guard import StaticGuard
from drone4d.baselines.track_share import TrackShare
from drone4d.baselines.track_solo import TrackSolo

POLICIES = {"static_guard": StaticGuard, "coverage_scan": CoverageScan, "track_share": TrackShare, "hover_plaza": HoverPlaza,
            "track_solo": TrackSolo}
BASELINES = {**POLICIES, **{r.name: POLICIES[r.policy] for r in REFERENCE6.values()}}

__all__ = ["BASELINES", "POLICIES", "REFERENCE6", "CoverageScan", "HoverPlaza", "Reference", "StaticGuard", "TrackShare", "TrackSolo",
           "check_reference_spec", "reference_run_spec"]
