"""D3 测试统一入口的跳过分层与计数（D3 封板 I01；docs/05 §11 证据规则：done 以统一入口的测试计数为证据）。

每个跳过按原因归入一层，结果写入摘要 JSON，供 tools/deploy/dmax/run_d3_tests.sh 汇总到 out/tests/<sha>/：

- ``dep``：缺 Python 依赖（``pytest.importorskip`` / ``No module named``），细项为模块名；
- ``gpu``：无 GPU / EGL / CUDA 上下文；
- ``optin``：需显式开启的用例（``set AWR_*=`` 环境变量、性能界关闭等）；
- ``data``：缺已构建世界或原始数据（``worlds/*``、``data/raw``、``needs_data``）；
- ``platform``：缺外部运行时（gz-sim、指定内核、docker 等，由专用镜像入口覆盖）；
- ``other``：未归类。

跳过原因以 ``[层名] `` 开头时按显式层名归类（如 ``pytest.skip("[gpu] EGL unavailable")``），否则按关键字推断。

环境变量（只用环境变量而非命令行选项：本文件在 ``pytest tests`` 下不是初始 conftest，``pytest_addoption`` 不生效）：

- ``AWR_D3_SUMMARY=<path>``：会话结束时写摘要 JSON（计数、分层、逐用例结果、跳过明细）；
- ``AWR_D3_REQUIRE=dep,gpu``：所列层中的跳过改判为失败（统一入口在完整镜像与 GPU 上运行时启用，
  防止依赖或 GPU 缺失使验收用例以跳过冒充通过）；``all`` 表示全部层。

只作用于 tests/d3 下的用例与收集项（runtest / collect 钩子按目录生效），不改变 D2 测试行为。
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
from collections import Counter
from pathlib import Path

import pytest

TIERS = ("dep", "gpu", "optin", "data", "platform", "other")
SUMMARY_VERSION = 1

_EXPLICIT = re.compile(r"^\[(" + "|".join(TIERS) + r")\]\s*")
_DEP = re.compile(r"could not import '([^']+)'|No module named '([^']+)'")
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gpu", re.compile(r"\b(EGL|CUDA|GPU|OpenGL|nvidia|moderngl context|warp device)\b", re.I)),
    ("optin", re.compile(r"\bset AWR_\w+|\bAWR_\w+=|\bAWR_\w+\*?\s+unset\b|\bdisabled\b", re.I)),
    ("data", re.compile(r"needs worlds|worlds/|data/raw|needs_data|dataset|world dir|AWR_WORLDS_DIR|make worlds|未构建|世界包", re.I)),
    ("platform", re.compile(r"gz-sim|\bgz\b|kernel|docker|unavailable|not supported|platform", re.I)),
)


def skip_reason(longrepr) -> str:
    """跳过报告的原因文本：runtest / collect 报告的 longrepr 为 (path, lineno, "Skipped: …") 或字符串。"""
    msg = str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr or "")
    return msg[len("Skipped: "):] if msg.startswith("Skipped: ") else msg


def classify_skip(reason: str) -> tuple[str, str | None]:
    """跳过原因 → (层, 细项)。细项对 ``dep`` 为缺失的顶层模块名，其余为 None。"""
    m = _EXPLICIT.match(reason)
    if m:
        return m.group(1), None
    m = _DEP.search(reason)
    if m:
        return "dep", (m.group(1) or m.group(2)).split(".")[0]
    for tier, pat in _RULES:
        if pat.search(reason):
            return tier, None
    return "other", None


def required_tiers(value: str | None) -> frozenset[str]:
    """``AWR_D3_REQUIRE`` 取值 → 层集合；未知层名报错，避免拼写错误使严格模式静默失效。"""
    items = {s.strip() for s in (value or "").split(",") if s.strip()}
    if "all" in items:
        return frozenset(TIERS)
    unknown = items - set(TIERS)
    if unknown:
        raise pytest.UsageError(f"AWR_D3_REQUIRE: unknown tier(s) {sorted(unknown)}; expected subset of {list(TIERS)} or 'all'")
    return frozenset(items)


class _D3Ledger:
    """tests/d3 用例与收集项的结果台账（只记本目录：钩子经节点 ihook 调用）。"""

    def __init__(self, config: pytest.Config, required: frozenset[str]) -> None:
        self.config = config
        self.required = required
        self.outcomes: dict[str, str] = {}
        self.skips: list[dict] = []
        self.collect_errors: list[str] = []
        self.t0 = time.time()

    def skip(self, nodeid: str, reason: str, *, kind: str) -> tuple[str, str | None]:
        tier, detail = classify_skip(reason)
        self.skips.append({"nodeid": nodeid, "kind": kind, "tier": tier, "detail": detail, "reason": reason[:500],
                           "required": tier in self.required})
        return tier, detail

    def summary(self, exitstatus: int) -> dict:
        counts = Counter(self.outcomes.values())
        by_tier = Counter(s["tier"] for s in self.skips if not s["required"])
        deps = Counter(s["detail"] for s in self.skips if s["tier"] == "dep" and s["detail"])
        return {
            "version": SUMMARY_VERSION,
            "scope": "tests/d3",
            "exitstatus": int(exitstatus),
            "duration_s": round(time.time() - self.t0, 3),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "required_tiers": sorted(self.required),
            "counts": {k: counts.get(k, 0) for k in ("passed", "failed", "error", "skipped", "xfailed", "xpassed")}
            | {"collect_errors": len(self.collect_errors),
               "skipped_modules": sum(1 for s in self.skips if s["kind"] == "collect" and not s["required"])},
            "skipped_by_tier": {t: by_tier.get(t, 0) for t in TIERS},
            "missing_deps": dict(sorted(deps.items())),
            "required_skip_failures": sum(1 for s in self.skips if s["required"]),
            "skips": self.skips,
            "collect_errors": self.collect_errors,
            "tests": dict(sorted(self.outcomes.items())),
        }


_LEDGER: _D3Ledger | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _LEDGER
    _LEDGER = _D3Ledger(config, required_tiers(os.environ.get("AWR_D3_REQUIRE")))


def _fail_required(report, tier: str, reason: str) -> None:
    report.outcome = "failed"
    report.longrepr = f"[AWR_D3_REQUIRE] skip in required tier '{tier}' counts as failure: {reason}"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    rep = yield
    if _LEDGER is not None and rep.skipped and not hasattr(rep, "wasxfail"):
        reason = skip_reason(rep.longrepr)
        tier, _ = _LEDGER.skip(rep.nodeid, reason, kind=rep.when)
        if tier in _LEDGER.required:
            _fail_required(rep, tier, reason)
    return rep


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    rep = yield
    if _LEDGER is not None:
        if rep.skipped:
            reason = skip_reason(rep.longrepr)
            tier, _ = _LEDGER.skip(rep.nodeid, reason, kind="collect")
            if tier in _LEDGER.required:
                _fail_required(rep, tier, reason)
        if rep.failed:
            _LEDGER.collect_errors.append(rep.nodeid)
        elif rep.skipped:
            _LEDGER.outcomes[rep.nodeid] = "skipped"
    return rep


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """逐用例结果按 pytest 终端口径归类（同一用例 setup 失败记 error，call 结果优先于 setup 通过）。"""
    if _LEDGER is None:
        return
    cfg = _LEDGER.config
    cat = cfg.hook.pytest_report_teststatus(report=report, config=cfg)[0]
    if not cat:
        return
    prev = _LEDGER.outcomes.get(report.nodeid)
    if prev in ("failed", "error") and cat not in ("failed", "error"):
        return                                                               # 已失败的用例不被后续阶段覆盖
    if cat == "failed" and report.when != "call":
        cat = "error"
    _LEDGER.outcomes[report.nodeid] = cat


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    path = os.environ.get("AWR_D3_SUMMARY")
    if _LEDGER is None or not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(_LEDGER.summary(exitstatus), ensure_ascii=False, indent=1) + "\n")
    tmp.replace(p)


def pytest_terminal_summary(terminalreporter, exitstatus: int, config: pytest.Config) -> None:
    if _LEDGER is None or not (_LEDGER.outcomes or _LEDGER.skips):
        return
    s = _LEDGER.summary(exitstatus)
    c = s["counts"]
    terminalreporter.section("D3 skip tiers")
    terminalreporter.write_line(
        f"passed {c['passed']}  failed {c['failed']}  error {c['error']}  skipped {c['skipped']}  "
        f"xfailed {c['xfailed']}  collect_errors {c['collect_errors']}  required={','.join(s['required_tiers']) or '-'}")
    terminalreporter.write_line("skipped by tier: " + "  ".join(f"{t} {n}" for t, n in s["skipped_by_tier"].items()))
    if s["missing_deps"]:
        terminalreporter.write_line("missing deps: " + "  ".join(f"{m} {n}" for m, n in s["missing_deps"].items()))
    if s["required_skip_failures"]:
        terminalreporter.write_line(f"required-tier skips counted as failures: {s['required_skip_failures']}", red=True)

