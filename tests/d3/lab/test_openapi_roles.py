"""OpenAPI 最低角色标注（D3-AC-51："每个 `/api/bench/v1` 接口在 OpenAPI 中以 `x-d3-role` 标注最低角色"；AWR-17 §18.4 第 6 条；
API-AC-070；M27-FR-151、M27-AC-036）。

- 每个已交付操作带 `x-d3-role`、`x-d3-bn`、`x-d3-stage`、`x-awr-reasons`；`x-d3-role` 取实际执行的依赖（不是另行登记的文字），
  与设计表 `ENDPOINTS` 及测试侧口径（conftest `EXPECTED`）三者一致；
- 原因码均登记于 reasons.json；鉴权类码按角色与方法补齐；
- 快照 `packages/contracts/bench/openapi/bench.openapi.snapshot.json` 与运行中的 bench-api 逐字节一致
  （`tools/contracts/gen_openapi.py --app bench [--check]`）；`/api/bench/v1/openapi.json` 同样带标注。
"""

from __future__ import annotations

import importlib.util
import re
import warnings
from pathlib import Path

from awr.bench.api.deps import ENDPOINTS, ROLE_NONE, endpoint_for, role_matrix
from awr.contracts.reasons import REASONS

ROOT = Path(__file__).resolve().parents[3]
VALID_ROLES = {ROLE_NONE, "invited", "researcher", "evaluator", "admin"}
STAGES = {"P-1", "P0a", "P0b", "P1", "P2", "P3"}


def _ops(spec: dict):
    for path, ops in spec["paths"].items():
        for m, op in ops.items():
            yield m.upper(), path, op


def test_every_operation_is_annotated(lab) -> None:
    spec = lab.client.get(f"{lab.P}/openapi.json").json()
    ops = list(_ops(spec))
    assert len(ops) == len(lab.EXPECTED)
    for m, path, op in ops:
        rel = path[len(lab.P):]
        assert op["x-d3-role"] in VALID_ROLES and op["x-d3-stage"] in STAGES, (m, path)
        assert re.fullmatch(r"BN-\d\d(\.[a-z]+)?", op["x-d3-bn"]), (m, path)
        assert op["x-awr-reasons"] == sorted(set(op["x-awr-reasons"])) and all(c in REASONS for c in op["x-awr-reasons"]), (m, path)
        assert op["x-d3-role"] == lab.EXPECTED[(m, rel)], (m, path)                          # 与测试侧权限矩阵一致
        if op["x-d3-role"] != ROLE_NONE:
            assert {301, 302} <= set(op["x-awr-reasons"])
            assert (303 in op["x-awr-reasons"]) == (m != "GET")
            assert (115 in op["x-awr-reasons"]) or op["x-d3-role"] == "invited"


def test_enforced_role_matches_design_table(lab) -> None:
    rows = role_matrix(lab.app)
    assert len(rows) == len(lab.EXPECTED)
    assert [r for r in rows if r["bn"] is None] == []                                       # 无未登记操作
    assert [r for r in rows if r["role"] != r["declared"]] == []                            # 执行角色 = 设计表角色
    assert {(r["method"], r["path"]): r["role"] for r in rows} == lab.EXPECTED


def test_design_table_is_well_formed() -> None:
    bns = [e.bn for e in ENDPOINTS]
    assert len(bns) == len(set(bns)) and len({(e.method, re.sub(r"\{[^}]*\}", "{}", e.path)) for e in ENDPOINTS}) == len(ENDPOINTS)
    assert not any(e.bn in {f"BN-{n}" for n in range(86, 95)} for e in ENDPOINTS)            # v1.3 废止的公开提交接口不登记
    for e in ENDPOINTS:
        assert e.role in VALID_ROLES and e.stage in STAGES and e.method in {"GET", "POST", "PUT", "DELETE"}, e
        assert all(c in REASONS for c in e.reasons), e
    assert endpoint_for("get", "/board/{track}/entries/{entry_id:path}").bn == "BN-84"
    assert endpoint_for("GET", "/runs/{x}/frames/{y}/{z}:range").bn == "BN-51" and endpoint_for("GET", "/nope") is None
    # 最低角色分布：真值类只对评测员及以上，配额、构建与健康类只对管理员
    assert {e.bn for e in ENDPOINTS if e.role == "evaluator"} == {"BN-52", "BN-52.tape"}
    assert {e.bn for e in ENDPOINTS if e.role == "admin"} == {"BN-13", "BN-14", "BN-31", "BN-34", "BN-81"}


def _gen():
    spec = importlib.util.spec_from_file_location("gen_openapi", ROOT / "tools" / "contracts" / "gen_openapi.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_snapshot_matches_live_bench_api() -> None:
    g = _gen()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        live = g.canonical(g.bench_schema())
    assert g.BENCH_SNAPSHOT.exists(), "python tools/contracts/gen_openapi.py --app bench"
    assert g.BENCH_SNAPSHOT.read_text(encoding="utf-8") == live, "bench REST 面与快照不一致：python tools/contracts/gen_openapi.py --app bench"
    assert g.main(["--check", "--app", "bench"]) == 0
