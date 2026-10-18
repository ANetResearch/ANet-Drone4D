"""Run 规格组合矩阵求值（AWR-17 §18.5.4 校验第 ④、⑤ 步；M27-FR-159；M26 §7.11；WP-D3-01）。

规则以声明形式登记在 `packages/contracts/bench/run_spec.v1.schema.json` 的 `x-rules`；本模块是 bench-api（WP-D3-30）与测试共用的
求值实现，前端预检由同一规则表生成。条件：`if` 中每个路径的取值都在所列值中时规则生效（`outputs` 等数组字段为"含任一所列成员"，
`"*"` 为"给出了该字段"）；未给出的字段按 schema 的 `default` 求值。检查：`allow`（取值在所列值中）、`forbid`（不得给出）、
`require`（必须给出）、`min`（取值 ≥ n）、`exactly_one`（恰好给出其一）。`clock_profile` 的档名按 `CLOCK_PROFILES` 展开为四元组。

响应（AWR-17 §18.5.4 校验顺序，第一个失败即返回）：`problem` 按 `CODE_PRECEDENCE`（第 ④ 层 630、633、643、662、664，第 ⑤ 层
682、637）取首个失败层的原因码，`detail.violations[]` 列出同一层的全部问题。排名约束（D3-AC-34、36、41、69）：`ranked` 只接受
`physics = realistic`（Fast 档 682 `ranked_requires_realistic`，SITL 档 633 `sitl_ranked`）与 `appearance = mesh`（682
`ranked_appearance`；`appearance_source ∈ {mesh_distilled, generative}` 一律不排名，P3 登记 `aerial` 后仍然有效，ADR-187）。
提交评测（`submission_eval` 作业）的运行恒为排名运行，`check_submission_eval` 以榜单轮次配置按同一规则表求值，SITL 档返回 633。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from typing import Any

from awr.contracts._paths import schema_path

__all__ = ["CLOCK_PROFILES", "CODE_PRECEDENCE", "JOB_SEEDED_RULES", "Violation", "check", "check_ranked_eval", "check_submission_eval",
           "first_layer", "load_rules", "problem", "reason"]

SCHEMA = "bench/run_spec.v1.schema.json"
CLOCK_PROFILES = {"d1_250": {"main_hz": 250, "physics_hz": 250, "l1_hz": 250},
                  "sandbox100": {"main_hz": 100, "physics_hz": 100, "l1_hz": 100}}
# 第 ④ 层（组合矩阵与阶段）在前、第 ⑤ 层（时钟档与输出）在后；同层内按此顺序取主原因码（AWR-17 §18.5.4）
CODE_PRECEDENCE = (630, 633, 643, 662, 664, 682, 637)
_LAYER = {630: 4, 633: 4, 643: 4, 662: 4, 664: 4, 682: 5, 637: 5}
# 作业创建的排名运行由平台注入秘密种子（榜单轮次配置的 `seeds`），外部请求的种子规则对其不适用
JOB_SEEDED_RULES = frozenset({"ranked_seeds", "seeds_xor"})
_MISSING = object()


@dataclass(frozen=True)
class Violation:
    rule: str
    code: int
    why: str
    path: str


@cache
def _schema() -> dict:
    return json.loads(schema_path(SCHEMA).read_text(encoding="utf-8"))


def load_rules() -> tuple[dict, ...]:
    return tuple(_schema()["x-rules"])


def _default(path: str) -> Any:
    node: Any = _schema()
    for seg in path.split("."):
        props = node.get("properties") if isinstance(node, dict) else None
        if not props or seg not in props:
            return _MISSING
        node = props[seg]
    return node.get("default", _MISSING) if isinstance(node, dict) else _MISSING


def _get(spec: dict, path: str, *, defaults: bool = True) -> Any:
    head, _, rest = path.partition(".")
    if head == "clock_profile":
        cp = spec.get("clock_profile", "d1_250")
        cp = {"name": cp, **CLOCK_PROFILES.get(cp, {})} if isinstance(cp, str) else cp
        return cp.get(rest, _MISSING) if rest else cp
    node: Any = spec
    for seg in path.split("."):
        if not isinstance(node, dict) or seg not in node:
            return _default(path) if defaults else _MISSING
        node = node[seg]
    return node


def _matches(spec: dict, cond: dict) -> bool:
    for path, values in cond.items():
        v = _get(spec, path, defaults="*" not in values)
        if "*" in values:
            if v is _MISSING:
                return False
        elif isinstance(v, list):
            if not set(map(json.dumps, v)) & set(map(json.dumps, values)):
                return False
        elif v is _MISSING or v not in values:
            return False
    return True


def check(spec: dict, rules: tuple[dict, ...] | None = None, *, external: bool = True) -> list[Violation]:
    """返回全部违反项（同一层的全部问题，供 `detail.violations[]`）；空表为通过。`external = False` 用于作业创建的运行
    （`submission_eval` 写入 `eval_ref`），跳过 `scope = external` 的规则。"""
    out: list[Violation] = []
    for r in rules if rules is not None else load_rules():
        if (r.get("scope") == "external" and not external) or not _matches(spec, r.get("if", {})):
            continue
        bad = [p for p, allowed in r.get("allow", {}).items() if (v := _get(spec, p)) is not _MISSING and v not in allowed]
        bad += [p for p in r.get("forbid", []) if _get(spec, p, defaults=False) is not _MISSING]
        bad += [p for p in r.get("require", []) if _get(spec, p, defaults=False) is _MISSING]
        bad += [p for p, n in r.get("min", {}).items() if (v := _get(spec, p)) is _MISSING or v < n]
        if "exactly_one" in r and sum(_get(spec, p, defaults=False) is not _MISSING for p in r["exactly_one"]) != 1:
            bad.append("|".join(r["exactly_one"]))
        out += [Violation(r["id"], r["code"], r["why"], p) for p in bad]
    return out


def first_layer(violations: list[Violation]) -> list[Violation]:
    """首个失败层的全部违反项，按 `CODE_PRECEDENCE` 稳定排序（首项即主原因）；空表输入返回空表。"""
    if not violations:
        return []
    layer = min(_LAYER.get(v.code, 5) for v in violations)
    rank = {c: i for i, c in enumerate(CODE_PRECEDENCE)}
    return sorted((v for v in violations if _LAYER.get(v.code, 5) == layer), key=lambda v: rank.get(v.code, len(rank)))


def problem(violations: list[Violation]) -> tuple[int, dict] | None:
    """→ `(code, detail)` 供 problem+json（`detail.why` 为主原因，`detail.violations[]{path, why, code}` 为同层全部问题）；通过时为 None。"""
    vs = first_layer(violations)
    if not vs:
        return None
    return vs[0].code, {"why": vs[0].why, "violations": [{"path": v.path, "why": v.why, "code": v.code} for v in vs]}


def reason(violations: list[Violation]) -> dict | None:
    """→ `bench/submission.v1` 的 `reason{code, name, detail}`（提交被拒时写入）；通过时为 None。"""
    from awr.contracts.reasons import info

    p = problem(violations)
    return None if p is None else {"code": p[0], "name": info(p[0]).name, "detail": p[1]}


def check_ranked_eval(run_spec: dict, *, physics: str | None = None, policy: dict | None = None) -> list[Violation]:
    """作业创建的排名运行（`ranked = true`、`external = False`）的组合规则求值：`physics`、`policy` 给出时覆盖规格中的取值
    （与 `runner.run_job` 的覆盖语义一致），种子规则见 `JOB_SEEDED_RULES`。"""
    spec = {**run_spec, "ranked": True}
    if physics is not None:
        spec["physics"] = physics
    if policy is not None:
        spec["policy"] = policy
    return check(spec, tuple(r for r in load_rules() if r["id"] not in JOB_SEEDED_RULES), external=False)


def check_submission_eval(cfg: dict) -> list[Violation]:
    """榜单轮次配置（`boardstore.load_config`：`run_spec`、`physics`）对提交评测的准入：提交以镜像策略运行且恒为排名运行，
    SITL 档返回 633，Fast 档与非 `mesh` 外观返回 682（D3-AC-34、36、41、69）。"""
    return check_ranked_eval(cfg["run_spec"], physics=cfg.get("physics"), policy={"kind": "image"})
