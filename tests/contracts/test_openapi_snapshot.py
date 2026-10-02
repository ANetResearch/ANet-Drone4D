"""契约 CI 第 9 条（AWR-17 §10.6；API-FR-046；FX-GW 交付 M00-B-to-M11 第 6 条的快照）：
- 运行中 api 的 OpenAPI 与 `packages/contracts/rest/openapi.snapshot.json` 一致（路由增删、参数或模型变化必须同时更新快照：
  `python tools/contracts/gen_openapi.py` 或 `make contracts`）；
- api 代码中抛出的每个 REST 问题码（`ApiProblem(<code>` 字面量与 `Reason.<NAME>`）都登记在 reasons.json。
"""

from __future__ import annotations

import importlib.util
import re
import warnings
from pathlib import Path

from awr.contracts import reasons as R

ROOT = Path(__file__).resolve().parents[2]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_openapi", ROOT / "tools" / "contracts" / "gen_openapi.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_openapi_matches_snapshot() -> None:
    g = _gen()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # FastAPI 对 GET/HEAD 共用处理函数的 operation id 重复告警
        live = g.canonical(g.live_schema())
    assert g.SNAPSHOT.exists(), "packages/contracts/rest/openapi.snapshot.json 缺失：python tools/contracts/gen_openapi.py"
    snap = g.SNAPSHOT.read_text(encoding="utf-8")
    if live != snap:
        import json

        a, b = json.loads(snap)["paths"], json.loads(live)["paths"]
        added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
        changed = sorted(p for p in set(a) & set(b) if a[p] != b[p])
        raise AssertionError(f"REST 面与快照不一致（新增 {added}，删除 {removed}，变化 {changed[:10]}）；"
                             "确认后运行 python tools/contracts/gen_openapi.py 或 make contracts")


def test_problem_codes_are_registered() -> None:
    lit = re.compile(r"ApiProblem\(\s*(\d+)")
    named = re.compile(r"\bReason\.([A-Z][A-Z0-9_]+)")
    known = {int(c) for c in R.REASONS}
    names = {m.name for m in R.Reason}
    bad: list[str] = []
    for f in sorted((ROOT / "python" / "awr" / "api").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(ROOT)
        bad.extend(f"{rel}: {m.group(1)}" for m in lit.finditer(text) if int(m.group(1)) not in known)
        bad.extend(f"{rel}: Reason.{m.group(1)}" for m in named.finditer(text) if m.group(1) not in names)
    assert bad == [], bad
    assert all(R.http_status(c) >= 400 for c in (110, 115, 116, 211, 213, 305))
