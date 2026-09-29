"""守卫策略（M14 §6.14.2；M14-FR-053–055；ADR-027、C35）：op 白名单、能力到 op 的映射、任务包络计算、参数校验。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from awr.contracts.commands import SERVICES

__all__ = ["CONFIRM_OPS", "MAX_AGENT_WAYPOINTS", "OP_WHITELIST", "SAFETY_OPS", "SPATIAL_OPS", "Envelope", "GuardPolicy",
           "args_schema", "check_args", "finite_tree", "r_env_m"]

# FR-053：agent 可用的 op；其余一律 115（含 safety_stop、kill、escalate、velocity、fleet/*、mission/*、sim/*、env/*、seat/*）
OP_WHITELIST = frozenset({"takeoff", "land", "goto", "follow_path", "orbit", "hover", "rtl", "pause", "resume", "arm", "disarm",
                          "cancel", "acquire", "release"})
SAFETY_OPS = frozenset({"land", "hover", "rtl"})  # 减能量命令（A2 安全类、A4 语义前置）
CONFIRM_OPS = frozenset({"kill", "escalate", "override", "seat/takeover"})  # 需要确认令牌：agent 永远拿不到（③）
SPATIAL_OPS = frozenset({"goto", "orbit", "follow_path", "land"})
MAX_AGENT_WAYPOINTS = 64


@cache
def _schemas() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in SERVICES:
        if s.name.startswith("uav/{id}/cmd/"):
            out.setdefault(s.op, s.args_schema)
    return out


def args_schema(op: str) -> dict[str, Any] | None:
    return _schemas().get(op)


def finite_tree(v: Any) -> bool:
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return True
    if isinstance(v, (int, float)):
        return math.isfinite(float(v))
    if isinstance(v, Mapping):
        return all(finite_tree(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return all(finite_tree(x) for x in v)
    return False


@cache
def _validator(op: str) -> Any:
    import jsonschema

    sch = args_schema(op)
    if sch is None:
        return None
    cls = jsonschema.validators.validator_for(sch)
    return cls(sch)


_RANGE_KW = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems", "minLength",
                       "maxLength", "enum", "const"})


def check_args(op: str, args: Mapping[str, Any]) -> tuple[int, str | None]:
    """G0 参数校验：结构错误 300、越界 110、非有限数 110；返回 (code, detail)。"""
    if not finite_tree(args):
        return 110, "NON_FINITE"
    if op in ("acquire", "release"):
        return 0, None
    if op == "follow_path":
        wps = args.get("waypoints")
        if isinstance(wps, list) and len(wps) > MAX_AGENT_WAYPOINTS:
            return 110, "WAYPOINTS"
    v = _validator(op)
    if v is None:
        return 0, None
    errs = sorted(v.iter_errors(dict(args)), key=lambda e: list(e.path))
    if not errs:
        return 0, None
    e = errs[0]
    return (110 if e.validator in _RANGE_KW else 300), f"{e.validator}:{'/'.join(str(p) for p in e.path)}"


def r_env_m(alt_agl_m: float, hfov_deg: float, r_env_max: float = 300.0) -> float:
    """`R_env = min(3·R_foot, 300 m)`，`R_foot = alt_agl_m·tan(hfov/2)`（§6.14.2 A3）。"""
    r_foot = alt_agl_m * math.tan(math.radians(hfov_deg) / 2.0)
    return min(3.0 * r_foot, float(r_env_max))


@dataclass(frozen=True)
class Envelope:
    """某次委派允许的活动范围（A3）：到任务目标的水平距离、离地高度、速度。"""

    center_xy: tuple[float, float]
    r_env_m: float
    ground_z: float
    agl_min_m: float
    agl_max_m: float
    speed_mps_max: float | None = None

    def violates(self, op: str, args: Mapping[str, Any]) -> str | None:
        pts: list[Any] = []
        if op == "goto":
            pts = [args.get("pos")]
        elif op == "orbit":
            pts = [args.get("center")]
        elif op == "follow_path":
            pts = list(args.get("waypoints") or [])
        elif op == "land" and isinstance(args.get("at"), Mapping):
            pts = [args["at"].get("pos")]
        for p in pts:
            if not isinstance(p, (list, tuple)) or len(p) != 3:
                continue
            d = math.hypot(float(p[0]) - self.center_xy[0], float(p[1]) - self.center_xy[1])
            if d > self.r_env_m + 1e-6:
                return f"R_ENV {d:.1f} > {self.r_env_m:.1f}"
            if op != "land":
                agl = float(p[2]) - self.ground_z
                if agl < self.agl_min_m - 1e-6 or agl > self.agl_max_m + 1e-6:
                    return f"AGL {agl:.1f} not in [{self.agl_min_m:.1f}, {self.agl_max_m:.1f}]"
        sp = args.get("speed_mps")
        if self.speed_mps_max is not None and isinstance(sp, (int, float)) and float(sp) > self.speed_mps_max + 1e-9:
            return f"SPEED {float(sp):.2f} > {self.speed_mps_max:.2f}"
        return None


@dataclass(frozen=True)
class GuardPolicy:
    rate_total: float = 20.0
    burst_total: float = 40.0
    rate_aid: float = 5.0
    burst_aid: float = 10.0
    max_waypoints: int = MAX_AGENT_WAYPOINTS
