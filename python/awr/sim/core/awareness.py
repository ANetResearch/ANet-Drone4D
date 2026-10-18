"""决策可见性（Decision awareness）：决定各决策环节读取 World 几何、环境场与能见度的哪些信息。

仿真本身（动力学、能量积分、接触、检测抽样）始终完整耦合；这里的开关只作用于**决策层**：返航高度与绕行（M09）、
返航速度的逆风扣除、能量判据、任务能量预检（M10）与检测器报价（M13）。缺省值与开关引入之前的行为逐位一致，
批量实验以 `AWR_AWARENESS`（JSON）或 `set_awareness()` 在进程内切换。

`rtl_policy = "fixed_alt"` 为基线：固定最低返航高度（`fixed_alt_m`，PX4 多旋翼缺省 RTL_RETURN_ALT = 30 m）、直线返航、
只按电量百分比阈值（CRIT 返航、EMERG 就地降落，COM_LOW_BAT_ACT = 3），不使用剩余时间判据。
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, fields, replace
from typing import Any

__all__ = ["DecisionAwareness", "awareness", "get_awareness", "set_awareness"]


@dataclass(frozen=True, slots=True)
class DecisionAwareness:
    geometry: bool = True  # 返航高度取 DSM 走廊上界（否则只取 max(z_now, z_home + alt)）
    detour: bool = True  # 绕行返航候选（ADR-054；geometry 关闭时同时关闭）
    wind: bool = True  # 返航速度扣除逆风；预检的风来自环境
    energy_rtl: bool = True  # 剩余时间判据 t_rem < rtl_margin·t_rtl（否则只按电量百分比）
    precheck_env: bool = False  # 任务预检沿轨迹查询运行期环境场（缺省沿用剧本 env 补丁的廓线风）
    visibility: bool = True  # 检测器报价计入消光透过率
    los: bool = False  # 检测器报价计入 World 视线遮挡（缺省报价不查视线，与开关引入前一致）
    rtl_policy: str = "awr"  # awr | fixed_alt
    fixed_alt_m: float = 30.0
    # 返航能量判据与任务预检共用同一功率模型：地速闭环下逆风计入功率（地速保持，超出空速上限才降速），t_rtl 折算为
    # 等效时间 E_rtl / p_avg；关闭时沿用"返航速度扣除逆风"的时间口径
    shared_energy: bool = False

    @property
    def use_geometry(self) -> bool:
        return self.geometry and self.rtl_policy == "awr"

    @property
    def use_detour(self) -> bool:
        return self.detour and self.use_geometry

    @property
    def use_wind(self) -> bool:
        return self.wind and self.rtl_policy == "awr"

    @property
    def use_energy_rtl(self) -> bool:
        return self.energy_rtl and self.rtl_policy == "awr"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> DecisionAwareness:
        if not d:
            return cls()
        names = {f.name for f in fields(cls)}
        bad = sorted(set(d) - names)
        if bad:
            raise ValueError(f"unknown awareness keys: {bad}")
        return replace(cls(), **d)


def _from_env() -> DecisionAwareness:
    raw = os.environ.get("AWR_AWARENESS")
    return DecisionAwareness.from_dict(json.loads(raw)) if raw else DecisionAwareness()


_CUR: list[DecisionAwareness] = [_from_env()]


def get_awareness() -> DecisionAwareness:
    return _CUR[0]


def set_awareness(a: DecisionAwareness | dict[str, Any] | None) -> DecisionAwareness:
    _CUR[0] = a if isinstance(a, DecisionAwareness) else DecisionAwareness.from_dict(a)
    return _CUR[0]


@contextlib.contextmanager
def awareness(a: DecisionAwareness | dict[str, Any] | None) -> Iterator[DecisionAwareness]:
    old = _CUR[0]
    try:
        yield set_awareness(a)
    finally:
        _CUR[0] = old
