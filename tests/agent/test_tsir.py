"""M14-AC-017、018：TSIR 与 ANetCore v0.14 转写向量逐条一致；负向范围硬闸门；默认 nofly 范围被服务端补回。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awr.agent.runtime import tsir as T
from awr.agent.runtime.tsir import EffectRecord, Malformed, evaluate, evaluate_scope, validate

VEC = json.loads((Path(__file__).parent / "golden" / "tsir_vectors.json").read_text(encoding="utf-8"))["vectors"]


def test_vector_count() -> None:
    assert len(VEC) >= 30
    assert sum(1 for v in VEC if v["source"].startswith("ANetCore")) >= 15


@pytest.mark.parametrize("v", VEC, ids=[v["name"] for v in VEC])
def test_vector(v: dict) -> None:
    p = v["predicate"]
    if v["expect"] == "MALFORMED":
        with pytest.raises(Malformed):
            validate(p)
        return
    validate(p)
    if v["kind"] == "validate":
        return
    rec = EffectRecord.from_dict(v.get("record") or {})
    if v["kind"] == "scope":
        assert evaluate_scope(p, rec) == v["expect"]
    else:
        assert evaluate(p, rec) is v["expect"]


def test_enums_match_anetcore() -> None:
    """op、比较符、动词、匹配类型与 `ANetCore@v0.14.0/tsir/predicate.go` 常量逐一相等（M14-AC-003 的 M14 侧起草）。"""
    assert (T.AND, T.OR, T.NOT, T.ARTIFACT, T.TEST, T.THRESHOLD, T.SCOPE) == (1, 2, 3, 10, 11, 12, 13)
    assert (T.LT, T.LE, T.EQ, T.GE, T.GT) == (1, 2, 3, 4, 5)
    assert (T.VERB_CREATE, T.VERB_MODIFY, T.VERB_DELETE, T.VERB_CALL, T.VERB_GET) == (1, 2, 3, 4, 5)
    assert (T.MATCH_GLOB, T.MATCH_SET, T.MATCH_PREFIX) == (1, 2, 3)
    assert (T.MAX_CLAUSES, T.MAX_DEPTH) == (64, 16)


def test_scope_nofly_default_cannot_be_removed() -> None:
    sub = {"op": 13, "scope": {"verb": 4, "kind": 102, "match": {"kind": 2, "val": "uav/p600-b9"}}}
    neg = T.combine_negative([T.DEFAULT_NEGATIVE_SCOPE, sub])
    validate(neg)
    viol = EffectRecord(effects=[{"verb": 4, "resource": {"kind": 101, "id": "zone/nofly/pier-3"}}])
    assert evaluate_scope(neg, viol) == "VIOLATION"
    # 提交方给 None（试图删除）：默认范围仍在
    assert T.combine_negative([T.DEFAULT_NEGATIVE_SCOPE, None]) == T.DEFAULT_NEGATIVE_SCOPE
    assert evaluate_scope(None, viol) == "OK"


def test_tighten_accept_never_looser() -> None:
    default = {"op": 12, "thresh": {"metric": "confidence", "op": 4, "value": 0.8}}
    loose = {"op": 12, "thresh": {"metric": "confidence", "op": 4, "value": 0.1}}
    acc = T.tighten_accept(loose, default)
    validate(acc)
    assert not evaluate(acc, EffectRecord(metrics={"confidence": 0.5}))
    assert evaluate(acc, EffectRecord(metrics={"confidence": 0.85}))
    assert T.tighten_accept(None, default) == default


def test_glob_dialect() -> None:
    assert T.glob_match("a/*/c", "a/b/c") and not T.glob_match("a/*/c", "a/b/x/c")
    assert T.glob_match("a/**/c", "a/b/x/c") and T.glob_match("**", "") and T.glob_match("a*", "abc")
    assert not T.glob_match("a*", "a/b")
