"""TSIR 验收谓词（M14 §6.7；M14-FR-028–031；移植 `ANetCore@v0.14.0/tsir/predicate.go`）。

- 封闭文法：op 1 AND、2 OR、3 NOT、10 ARTIFACT、11 TEST、12 THRESHOLD、13 SCOPE；比较 LT 1–GT 5；动词 CREATE 1–GET 5；
  匹配 GLOB 1、SET 2、PREFIX 3。结构上限：深度 ≤ 16，AND/OR 子项 2–64；validate 失败一律 MALFORMED（失败即关闭）。
- 资源种类 `kind` 不做范围检查（与 v0.14 源码一致）；本项目私有编号 101 zone、102 vehicle、103 capability（§6.7.3）。
- glob 方言 C-D4：`*` 在段内（不跨 `/`），`**` 跨段。
- 线上 JSON 形式：`{op, children}`、`artifact{path_glob, exists?, min_size_bytes?}`、`test{test_id, expect}`、
  `thresh{metric, op, value}`、`scope{verb, kind, match{kind, val}}`（`val` 为字符串；SET 可为逗号串或字符串数组）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "AND", "ARTIFACT", "DEFAULT_NEGATIVE_SCOPE", "EQ", "GE", "GT", "KIND_CAPABILITY", "KIND_VEHICLE", "KIND_ZONE", "LE", "LT",
    "MAX_CLAUSES", "MAX_DEPTH", "NOT", "OR", "SCOPE", "TEST", "THRESHOLD", "VERB_CALL", "EffectRecord", "Malformed",
    "combine_negative", "evaluate", "evaluate_scope", "glob_match", "tighten_accept", "validate",
]

AND, OR, NOT, ARTIFACT, TEST, THRESHOLD, SCOPE = 1, 2, 3, 10, 11, 12, 13
LT, LE, EQ, GE, GT = 1, 2, 3, 4, 5
VERB_CREATE, VERB_MODIFY, VERB_DELETE, VERB_CALL, VERB_GET = 1, 2, 3, 4, 5
MATCH_GLOB, MATCH_SET, MATCH_PREFIX = 1, 2, 3
STATUS_PASS, STATUS_FAIL, STATUS_ABSENT = 1, 2, 3
MAX_CLAUSES, MAX_DEPTH = 64, 16
KIND_ZONE, KIND_VEHICLE, KIND_CAPABILITY = 101, 102, 103

# 默认负向范围：本世界全部禁飞区（M14 §6.7.2；FR-030）
DEFAULT_NEGATIVE_SCOPE: dict[str, Any] = {"op": SCOPE, "scope": {"verb": VERB_CALL, "kind": KIND_ZONE,
                                                                   "match": {"kind": MATCH_GLOB, "val": "zone/nofly/**"}}}


class Malformed(ValueError):
    """谓词不合文法（对外 121，detail = PREDICATE_MALFORMED）。"""

    code = 121


@dataclass
class EffectRecord:
    """TSIR 求值域（tsir-spec §3.3a；M14 §6.7.3）。"""

    metrics: dict[str, float] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)  # {path, size_bytes, content_cid?, media_type?}
    tests: list[dict[str, Any]] = field(default_factory=list)  # {id, status: 1 pass、2 fail、3 absent}
    resources: list[dict[str, Any]] = field(default_factory=list)  # {kind, id}
    effects: list[dict[str, Any]] = field(default_factory=list)  # {verb, resource{kind, id}}

    def to_dict(self) -> dict[str, Any]:
        return {"metrics": dict(self.metrics), "artifacts": list(self.artifacts), "tests": list(self.tests),
                "resources": list(self.resources), "effects": list(self.effects)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> EffectRecord:
        return cls(dict(d.get("metrics") or {}), list(d.get("artifacts") or []), list(d.get("tests") or []),
                   list(d.get("resources") or []), list(d.get("effects") or []))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def validate(p: Any, depth: int = 0) -> None:
    """结构校验（predicate.go `validate`）；失败抛 Malformed。"""
    if depth > MAX_DEPTH:
        raise Malformed("depth")
    if not isinstance(p, Mapping):
        raise Malformed("node")
    op = p.get("op")
    if not _is_int(op):
        raise Malformed("op")
    if op in (AND, OR):
        ch = p.get("children")
        if not isinstance(ch, list) or not (2 <= len(ch) <= MAX_CLAUSES):
            raise Malformed("arity")
        for c in ch:
            validate(c, depth + 1)
    elif op == NOT:
        ch = p.get("children")
        if not isinstance(ch, list) or len(ch) != 1:
            raise Malformed("not-arity")
        validate(ch[0], depth + 1)
    elif op == ARTIFACT:
        a = p.get("artifact")
        if not isinstance(a, Mapping) or not isinstance(a.get("path_glob"), str):
            raise Malformed("artifact")
        if a.get("schema_ref") or a.get("contains"):
            raise Malformed("artifact-cas")  # fail-loud（tsir-spec §3.4）
        if "exists" in a and not isinstance(a["exists"], bool):
            raise Malformed("artifact-exists")
        if "min_size_bytes" in a and not (_is_int(a["min_size_bytes"]) and a["min_size_bytes"] >= 0):
            raise Malformed("artifact-size")
    elif op == TEST:
        t = p.get("test")
        if not isinstance(t, Mapping) or not isinstance(t.get("test_id"), str) or t.get("expect") not in (STATUS_PASS, STATUS_FAIL) \
                or isinstance(t.get("expect"), bool):
            raise Malformed("test")
    elif op == THRESHOLD:
        t = p.get("thresh")
        if not isinstance(t, Mapping) or not isinstance(t.get("metric"), str) or not _is_int(t.get("op")) \
                or not (LT <= t["op"] <= GT) or not _is_num(t.get("value")):
            raise Malformed("thresh")
    elif op == SCOPE:
        s = p.get("scope")
        if not isinstance(s, Mapping) or not _is_int(s.get("verb")) or not (VERB_CREATE <= s["verb"] <= VERB_GET):
            raise Malformed("scope-verb")
        if not _is_int(s.get("kind")):
            raise Malformed("scope-kind")
        m = s.get("match")
        if not isinstance(m, Mapping) or not _is_int(m.get("kind")) or not (MATCH_GLOB <= m["kind"] <= MATCH_PREFIX):
            raise Malformed("scope-match")
        v = m.get("val")
        if not (isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v))):
            raise Malformed("scope-val")
    else:
        raise Malformed(f"unknown op {op}")  # fail-closed（§3.3b）


def _cmp(a: float, op: int, b: float) -> bool:
    if op == LT:
        return a < b
    if op == LE:
        return a <= b
    if op == EQ:
        return a == b
    if op == GE:
        return a >= b
    if op == GT:
        return a > b
    return False


def glob_match(pat: str, s: str) -> bool:
    """C-D4 glob：`*` 不跨 `/`，`**` 跨 `/`（predicate.go `globHelper`）。"""
    while pat:
        if pat.startswith("**"):
            rest = pat[2:].lstrip("/")
            if rest == "":
                return True
            return any(glob_match(rest, s[i:]) for i in range(len(s) + 1))
        if pat[0] == "*":
            rest = pat[1:]
            for i in range(len(s) + 1):
                if i > 0 and s[i - 1] == "/":
                    break
                if glob_match(rest, s[i:]):
                    return True
            return False
        if not s or pat[0] != s[0]:
            return False
        pat, s = pat[1:], s[1:]
    return s == ""


def _match_resource(m: Mapping[str, Any], rid: str) -> bool:
    k, v = m["kind"], m["val"]
    if k == MATCH_GLOB:
        return glob_match(v if isinstance(v, str) else ",".join(v), rid)
    if k == MATCH_PREFIX:
        return rid.startswith((v if isinstance(v, str) else ",".join(v)).removesuffix("/"))
    if k == MATCH_SET:
        toks = v.split(",") if isinstance(v, str) else list(v)
        return rid in toks
    return False


def evaluate(p: Mapping[str, Any], rec: EffectRecord) -> bool:
    """求值（predicate.go `Evaluate`）；谓词必须已通过 validate。"""
    op = p["op"]
    if op == AND:
        return all(evaluate(c, rec) for c in p["children"])
    if op == OR:
        return any(evaluate(c, rec) for c in p["children"])
    if op == NOT:
        return not evaluate(p["children"][0], rec)
    if op == ARTIFACT:
        a = p["artifact"]
        for art in rec.artifacts:
            if not glob_match(a["path_glob"], str(art.get("path", ""))):
                continue
            if a.get("exists") is False:
                continue
            if "min_size_bytes" in a and int(art.get("size_bytes", 0)) < int(a["min_size_bytes"]):
                continue
            return True
        return False
    if op == TEST:
        t = p["test"]
        for tr in rec.tests:
            if tr.get("id") == t["test_id"]:
                return tr.get("status") == t["expect"]
        return False
    if op == THRESHOLD:
        t = p["thresh"]
        if t["metric"] not in rec.metrics:
            return False  # 度量缺失为假
        v = rec.metrics[t["metric"]]
        return _is_num(v) and _cmp(float(v), t["op"], float(t["value"]))
    if op == SCOPE:
        s = p["scope"]
        for ef in rec.effects:
            res = ef.get("resource") or {}
            if ef.get("verb") == s["verb"] and res.get("kind") == s["kind"] and _match_resource(s["match"], str(res.get("id", ""))):
                return True
        return False
    return False


def evaluate_scope(negative: Mapping[str, Any] | None, committed: EffectRecord) -> Literal["OK", "VIOLATION"]:
    """负向范围硬闸门（tsir-spec §5.5）：谓词为真即违反。"""
    if negative is None:
        return "OK"
    return "VIOLATION" if evaluate(negative, committed) else "OK"


def _depth(p: Mapping[str, Any]) -> int:
    ch = p.get("children") if isinstance(p, Mapping) else None
    return 1 + max((_depth(c) for c in ch), default=0) if isinstance(ch, list) and ch else 1


def tighten_accept(submitted: Mapping[str, Any] | None, default: Mapping[str, Any] | None) -> dict[str, Any]:
    """验收谓词只能比目录默认值更严（§7.1 TaskSubmit；§6.14.3）：提交值与默认值做 AND；二者相同或缺省时取其一。"""
    if submitted is None:
        if default is None:
            raise Malformed("no acceptance predicate")
        return dict(default)
    if default is None or dict(submitted) == dict(default):
        return dict(submitted)
    return {"op": AND, "children": [dict(default), dict(submitted)]}


def combine_negative(parts: Sequence[Mapping[str, Any] | None]) -> dict[str, Any] | None:
    """合并负向范围：任一违反即违反（违反谓词取 OR）；默认 nofly 范围由调用方放在首位，提交方不能删除（FR-030）。"""
    ps = [dict(p) for p in parts if p is not None]
    uniq: list[dict[str, Any]] = []
    for p in ps:
        if p not in uniq:
            uniq.append(p)
    if not uniq:
        return None
    if len(uniq) == 1:
        return uniq[0]
    return {"op": OR, "children": uniq[:MAX_CLAUSES]}
