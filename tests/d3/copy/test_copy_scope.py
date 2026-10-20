"""copy-lint 扫描范围扩展到 D3 页面与 OpenAPI（D3-AC-23；AWR-05 §7.10 适用范围、14 §21.15.7 扫描范围；封板任务 L05）。

- 范围完整：`apps/web/src` 下每个界面词典（含 D3 lab 分片、`/lab/` 公开构建与样例页的词典、原因码文案）、`/lab/` 公开构建的
  HTML 模板（vite.config.ts 的 LAB_PUBLIC_HTML）与 `packages/contracts` 下每个 OpenAPI 快照都在 copy-lint 的扫描范围内；
- 扩展范围复跑中英文规则集：不使用基线时 0 违规，`--strict` 通过且基线例外为 0（P0b 出口）；
- 并入 `make lint` 与 REL：`lint-copy-lint` 以 `--strict` 运行，`lint-check-release` 以 `--copy-strict` 运行 REL-09，且两者都在
  `make lint` 的目标里；REL-09 对扩展范围无报告；
- OpenAPI 文案：kind openapi 只取 summary、description 与文档标题（生成的 schema title 不是文案，代码片段不是文案），
  违规定位到所在行；FastAPI 由函数名生成的 Title Case 摘要经 `awr.api.openapi_copy` 改为 sentence case；沙盒 API 对外
  提供的实时 OpenAPI 文档同样通过规则集；
- 范围内文件被实际检查：在词典、HTML 模板与 OpenAPI 中植入违规文案时报告对应规则（漏报检查）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COPY_LINT = ROOT / "tools" / "lint" / "copy-lint.mjs"
WEB_SRC = ROOT / "apps" / "web" / "src"
CONTRACTS = ROOT / "packages" / "contracts"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="[platform] node unavailable")


def _node(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    """在仓库根以 ES 模块执行一段脚本（import ./tools/lint/copy-lint.mjs 不触发其命令行入口）。"""
    return subprocess.run(["node", "--input-type=module", "-e", script, "--", *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=120, check=False)


def _lint(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["node", str(COPY_LINT), *args], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)


def _scope() -> dict[str, dict]:
    r = _node("import { loadScope } from './tools/lint/copy-lint.mjs'; console.log(JSON.stringify(loadScope()))")
    assert r.returncode == 0, r.stderr
    return {e["path"]: e for e in json.loads(r.stdout)}


def _check(name: str, raw: str, **opts) -> list[dict]:
    """copy-lint 的 checkText（文件名决定语言与种类，opts 覆盖）。"""
    script = ("import { checkText } from './tools/lint/copy-lint.mjs'; import { readFileSync } from 'node:fs';"
              "const [name, file, opts] = process.argv.slice(1);"
              "console.log(JSON.stringify(checkText(name, readFileSync(file, 'utf8'), JSON.parse(opts))))")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / Path(name).name
        f.write_text(raw, encoding="utf-8")
        r = _node(script, name, str(f), json.dumps(opts))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


# ---- 范围完整 ------------------------------------------------------------------------------------------------------------------

def test_scope_covers_every_ui_dictionary():
    """界面词典（i18n 目录下的 JSON：应用词典、D3 lab 分片、/lab/ 公开构建与样例页、原因码文案）全部在范围内，且语言可判定。"""
    scope = _scope()
    dicts = sorted(_rel(p) for p in WEB_SRC.rglob("*.json") if "i18n" in p.relative_to(WEB_SRC).parts[:-1])
    assert len(dicts) >= 19, dicts
    missing = [d for d in dicts if d not in scope]
    assert missing == [], f"界面词典不在 copy-lint 范围内：{missing}（登记到 tools/lint/copy-lint.scope.json）"
    for d in dicts:
        e = scope[d]
        assert e.get("kind") == "i18n", d
        lang = e.get("lang") or ("en" if re.search(r"(?:^|[./])en\.json$", d) else "zh" if "zh-CN" in d else None)
        assert lang in ("zh", "en"), f"{d}：无法由条目或文件名判定语言"
    # D3 lab 页面按 lab 分片的词典（AWR-14 i18n 规则：按 lab 分片）均为中英文成对
    shards = [d for d in dicts if "/i18n/lab/" in d]
    assert {Path(d).name.split(".")[0] for d in shards} >= {"compare", "inspect", "lidar-compare", "replay", "runs", "worlds"}
    for d in shards:
        twin = d.replace(".en.json", ".zh-CN.json") if d.endswith(".en.json") else d.replace(".zh-CN.json", ".en.json")
        assert twin in scope, twin


def test_scope_covers_the_lab_public_build():
    """/lab/ 公开只读构建（扩展点 15，ADR-129）：HTML 模板与构建引入的全部词典在范围内。"""
    scope = _scope()
    m = re.search(r"LAB_PUBLIC_HTML\s*=\s*'([^']+)'", (ROOT / "apps" / "web" / "vite.config.ts").read_text(encoding="utf-8"))
    assert m, "vite.config.ts 不再导出 LAB_PUBLIC_HTML"
    tpl = "apps/web/" + m.group(1)
    assert tpl in scope and scope[tpl].get("kind") == "html"
    # 构建入口引入的 JSON 词典（相对路径与 @/ 别名）
    imported = set()
    for src in [*(WEB_SRC / "app" / "lab-public").rglob("*.ts*"), *(WEB_SRC / "ui" / "lab").rglob("*.ts*")]:
        if ".test." in src.name:
            continue
        for spec in re.findall(r"""from\s+['"]([^'"]+\.json)['"]""", src.read_text(encoding="utf-8")):
            p = (WEB_SRC / spec[2:]) if spec.startswith("@/") else (src.parent / spec)
            imported.add(_rel(p.resolve()))
    assert any("lab-public/samples/i18n" in p for p in imported) and any("i18n/lab/inspect" in p for p in imported), imported
    assert sorted(p for p in imported if p not in scope) == []


def test_scope_covers_every_openapi_snapshot():
    scope = _scope()
    snaps = sorted(_rel(p) for p in CONTRACTS.rglob("*openapi*.json"))
    assert {"packages/contracts/rest/openapi.snapshot.json", "packages/contracts/rest/sandbox.openapi.snapshot.json",
            "packages/contracts/bench/openapi/bench.openapi.snapshot.json"} <= set(snaps)
    for s in snaps:
        assert s in scope and scope[s].get("kind") == "openapi", s


# ---- 扩展范围复跑与 P0b 出口 ----------------------------------------------------------------------------------------------------

def test_extended_scope_passes_without_baseline():
    r = _lint("--no-baseline")
    assert r.returncode == 0, r.stdout + r.stderr


def test_extended_scope_strict_and_baseline_cleared():
    r = _lint("--strict")
    assert r.returncode == 0, r.stdout + r.stderr
    reg = json.loads((ROOT / "tools" / "lint" / "copy-lint.baseline.json").read_text(encoding="utf-8"))
    assert reg["exceptions"] == []
    assert all(e.get("reason") and e.get("reviewed") for e in reg.get("allow", []))


def test_make_lint_runs_copy_lint_strict_and_rel_copy_strict():
    if shutil.which("make") is None:
        pytest.skip("[platform] make unavailable")
    r = subprocess.run(["make", "--no-print-directory", "-n", "_lint"], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert "node tools/lint/copy-lint.mjs --strict" in r.stdout
    assert "node tools/lint/check-release.mjs --copy-strict" in r.stdout


def test_rel09_reports_nothing_over_the_extended_scope():
    """REL-09（check-release 内的 copy-lint，严格模式）对扩展范围无报告；其他 REL 规则的结果与本项无关。"""
    script = ("import { checkCopy } from './tools/lint/check-release.mjs';"
              "const out = []; const R = { add: (...a) => out.push(a) };"
              "const stats = checkCopy(R, { strict: true }); console.log(JSON.stringify({ out, stats }))")
    r = _node(script)
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout.strip().splitlines()[-1])
    assert doc["out"] == [] and doc["stats"]["baseline_exceptions"] == 0
    assert doc["stats"]["files"] >= len(_scope()) - 1


# ---- OpenAPI 文案 --------------------------------------------------------------------------------------------------------------

def test_openapi_kind_reads_summaries_descriptions_and_title():
    doc = {
        "components": {"schemas": {"TaskIn": {"properties": {"task_id": {"title": "Task Id", "type": "string"}}, "title": "TaskIn"}}},
        "info": {"title": "ANet Drone4D Bench Api", "version": "1"},
        "paths": {"/a": {"get": {"summary": "List Tasks", "description": "返回 `awesome_lidar` 字段。超快返回！"}},
                  "/b": {"get": {"summary": "List tasks", "description": "Reads `Gazebo sim` state."}}},
    }
    raw = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False)
    got = _check("x.openapi.json", raw, kind="openapi", lang="auto")
    rules = sorted((f["rule"], f["anchor"]) for f in got)
    assert rules == [("COPY-01", "paths//a/get/description"), ("COPY-02", "paths//a/get/description"),
                     ("COPY-07", "info/title"), ("COPY-07", "info/title"), ("COPY-07", "paths//a/get/summary")]
    lines = raw.split("\n")
    t = next(f for f in got if f["anchor"] == "info/title")
    assert '"title": "ANet Drone4D Bench Api"' in lines[t["line"] - 1], "info.title 定位到 schema title 所在行"


def test_sentence_case_summaries():
    from fastapi import APIRouter, FastAPI

    from awr.api.openapi_copy import apply_sentence_case_summaries, sentence_case

    assert sentence_case("list_tasks") == "List tasks"
    assert sentence_case("viewer_url") == "Viewer URL"
    assert sentence_case("get_run_ids") == "Get run IDs"
    assert sentence_case("whoami") == "Whoami"

    r = APIRouter()

    @r.get("/tasks")
    def list_tasks() -> dict:
        return {}

    @r.get("/tasks/{tid}/viewer", summary="Open the task in the Viewer")
    def task_viewer_url(tid: str) -> dict:
        return {}

    @r.get("/hidden", include_in_schema=False)
    def hidden_route() -> dict:
        return {}

    app = FastAPI(title="t", version="1")
    app.include_router(r, prefix="/api")

    @app.get("/api/meta")
    def get_meta_info() -> dict:
        return {}

    assert apply_sentence_case_summaries(app) == 2
    assert apply_sentence_case_summaries(app) == 0                 # 重复调用无副作用
    ops = {p: o["get"]["summary"] for p, o in app.openapi()["paths"].items()}
    assert ops == {"/api/tasks": "List tasks", "/api/tasks/{tid}/viewer": "Open the task in the Viewer", "/api/meta": "Get meta info"}


def test_committed_openapi_summaries_are_sentence_case():
    """三份快照中由函数名生成的 Title Case 摘要已清零（D1 运行时 API、沙盒设计快照、事件沙盘 bench API）。"""
    title_case = re.compile(r"^(?:[A-Z][a-z0-9]*)(?: [A-Z][a-z0-9]*)+$")
    for p in sorted(CONTRACTS.rglob("*openapi*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        bad = [o["summary"] for ops in doc.get("paths", {}).values() for o in ops.values()
               if isinstance(o, dict) and title_case.match(o.get("summary") or "")]
        assert bad == [], f"{_rel(p)}: {bad}"
    bench = json.loads((CONTRACTS / "bench" / "openapi" / "bench.openapi.snapshot.json").read_text(encoding="utf-8"))
    assert bench["info"]["title"] == "ANet Drone4D Event Lab API"


def test_live_sandbox_openapi_passes(tmp_path):
    """沙盒 API 对外提供的 OpenAPI（`/api/sandbox/v1/openapi.json`，SB-20）由实时路由生成，不是仓库快照：生成后复跑规则集。"""
    sandbox_main = pytest.importorskip("awr.api.sandbox_main")
    spec = sandbox_main.live_openapi()
    assert "AWR-17" not in spec["info"].get("description", "")
    f = tmp_path / "sandbox.live.openapi.json"
    f.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    r = _lint("--no-baseline", "--kind", "openapi", str(f))
    assert r.returncode == 0, r.stdout + r.stderr


# ---- 范围内文件被实际检查（漏报） ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("path", "key", "bad", "rule"), [
    ("apps/web/src/app/i18n/lab/runs.en.json", None, "Export Run Bundle", "COPY-07"),
    ("apps/web/src/app/i18n/lab/runs.zh-CN.json", None, "运行跑起来了！", "COPY-02"),
    ("apps/web/src/app/lab-public/samples/i18n/en.json", None, "The first-ever public benchmark", "COPY-08"),
    ("apps/web/src/app/lab-public/i18n/zh-CN.json", None, "业界领先的事件沙盘", "COPY-08"),
    ("apps/web/src/app/i18n/reasons.zh-CN.gen.json", None, "秒出图，搞定", "COPY-01"),
])
def test_seeded_violations_are_reported(path, key, bad, rule):
    scope = _scope()
    e = scope[path]
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    obj = data
    while isinstance(obj, dict) and isinstance(next(iter(obj.values())), dict):
        obj = next(iter(obj.values()))
    k = key or next(iter(obj))
    obj[k] = bad
    got = _check(path, json.dumps(data, ensure_ascii=False, indent=1), kind=e["kind"], **({"lang": e["lang"]} if e.get("lang") else {}))
    assert rule in {f["rule"] for f in got}, got


def test_seeded_violation_in_lab_public_template():
    path = "apps/web/src/app/lab-public/index.html"
    raw = (ROOT / path).read_text(encoding="utf-8").replace("<title>", "<title>极致丝滑的", 1)
    got = _check(path, raw, kind="html", lang="zh")
    assert {f["rule"] for f in got} == {"COPY-01"}, got
