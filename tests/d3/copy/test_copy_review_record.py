"""D3 文案人工评审记录的覆盖与机检项（D3-AC-23、D3-AC-46；AWR-05 §7.10，14 §21.15.9 评审清单；封板任务 H11）。

评审记录 `docs/impl/D3-文案评审记录.md` 由 Opus 起草并先行生效，封板前汇总用户确认（ADR-194）。本文件把记录中可机检的部分
固定为回归用例，新增 D3 页面或词典而未补评审时失败：

- 覆盖：内部工作区（`app/routes/lab*.tsx`、`app/routes/lab/*.route.ts`）与 `/lab/` 公开构建（`lab-public/pages.tsx` 内置路由、
  `lab-public/samples/*.route.tsx` 扩展路由）的每个路由 pattern，以及每个 D3 词典分片，都在记录的抽查覆盖表中出现；
- 记录自洽：覆盖表引用的发现编号（W-xx）都在发现与处置表中登记，状态行写明"先行生效"与"待封板前用户确认"；
- 清单第 1 条（中英文双份，键名与参数）：每个 D3 中文词典都有英文对应，键集合相同，ICU 参数名相同（复数分支不计）；
- 清单第 1 条（AWR-14 §13.7 第 3 条）：D3 页面组件中除注释外没有中文字面量。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RECORD = ROOT / "docs" / "impl" / "D3-文案评审记录.md"
APP = ROOT / "apps" / "web" / "src" / "app"
WEB_SRC = ROOT / "apps" / "web" / "src"

CJK = re.compile(r"[㐀-鿿豈-﫿]")
# ICU 参数名：`{name}` 与 `{name, plural, ...}`；复数分支内的 `{# run}` 不是参数
ICU_ARG = re.compile(r"\{\s*([A-Za-z_]\w*)\s*[,}]")


def _record() -> str:
    assert RECORD.is_file(), f"missing review record {RECORD.relative_to(ROOT)}"
    return RECORD.read_text(encoding="utf-8")


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _d3_routes() -> dict[str, str]:
    """D3 页面路由 pattern → 来源文件（内部工作区与 /lab/ 公开构建）。"""
    out: dict[str, str] = {}
    internal = sorted(APP.glob("routes/lab*.tsx")) + sorted(APP.glob("routes/lab/*.route.ts"))
    for f in internal:
        for m in re.finditer(r"pattern:\s*'([^']+)'", f.read_text(encoding="utf-8")):
            out[m.group(1)] = _rel(f)
    pages = APP / "lab-public" / "pages.tsx"
    text = pages.read_text(encoding="utf-8")
    prefix = re.search(r"LAB_PREFIX\s*=\s*'([^']+)'", (APP / "lab-public" / "router.ts").read_text(encoding="utf-8"))
    assert prefix, "LAB_PREFIX not found in lab-public/router.ts"
    for m in re.finditer(r"pattern:\s*(?:LAB_PREFIX\b|`\$\{LAB_PREFIX\}([^`]*)`|'([^']+)')", text):
        out[m.group(2) or (prefix.group(1) + (m.group(1) or ""))] = _rel(pages)
    for f in sorted((APP / "lab-public" / "samples").glob("*.route.tsx")):
        for m in re.finditer(r"pattern:\s*'([^']+)'", f.read_text(encoding="utf-8")):
            out[m.group(1)] = _rel(f)
    return out


def _d3_dict_pairs() -> list[tuple[Path, Path]]:
    """D3 词典分片的（中文, 英文）对：lab 分片、/lab/ 公开构建外壳与样例页词典。"""
    pairs = [(z, z.with_name(z.name.replace(".zh-CN.json", ".en.json"))) for z in sorted(APP.glob("i18n/lab/*.zh-CN.json"))]
    pairs += [(z, z.with_name("en.json")) for z in sorted((APP / "lab-public").rglob("i18n/zh-CN.json"))]
    return pairs


def _flat(d: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flat(v, key))
        else:
            out[key] = str(v)
    return out


def _load(p: Path) -> dict[str, str]:
    return _flat(json.loads(p.read_text(encoding="utf-8")))


def test_record_status_and_scope() -> None:
    text = _record()
    assert "D3-AC-23" in text and "D3-AC-46" in text
    assert "先行生效" in text and "待封板前用户确认" in text
    assert "§21.15.9" in text  # 评审清单出处


def test_every_d3_route_is_reviewed() -> None:
    text = _record()
    routes = _d3_routes()
    assert len(routes) >= 14, routes  # 内部工作区 12 条 + 公开构建 /lab、/lab/samples、/lab/samples/:sid
    missing = {p: src for p, src in routes.items() if f"`{p}`" not in text}
    assert not missing, f"D3 routes without a review entry in {_rel(RECORD)}: {missing}"


def test_every_d3_dictionary_is_reviewed() -> None:
    text = _record()
    files = [f for pair in _d3_dict_pairs() for f in pair]
    files += [APP / "i18n" / "zh-CN.json", APP / "i18n" / "en.json", APP / "i18n" / "reasons.zh-CN.gen.json"]
    missing = [_rel(f) for f in files if _rel(f) not in text]
    assert not missing, f"D3 dictionaries without a review entry in {_rel(RECORD)}: {missing}"


def test_finding_ids_are_registered() -> None:
    text = _record()
    cited = set(re.findall(r"\bW-\d{2}\b", text))
    registered = set(re.findall(r"^\|\s*(W-\d{2})\s*\|", text, flags=re.M))
    assert registered, "no findings table rows (| W-xx | ...) in the review record"
    assert cited <= registered, f"finding ids cited but not registered: {sorted(cited - registered)}"


def test_d3_dictionaries_are_bilingual_with_matching_args() -> None:
    pairs = _d3_dict_pairs()
    assert len(pairs) >= 8, pairs
    # 应用主词典中的 lab.* 键（登录、观测墙、原型榜、内部提交等）
    main = (APP / "i18n" / "zh-CN.json", APP / "i18n" / "en.json")
    problems: list[str] = []
    for zh_path, en_path in [*pairs, main]:
        assert en_path.is_file(), f"missing English dictionary for {_rel(zh_path)}"
        zh, en = _load(zh_path), _load(en_path)
        if (zh_path, en_path) == main:
            zh = {k: v for k, v in zh.items() if k.startswith("lab.")}
            en = {k: v for k, v in en.items() if k.startswith("lab.")}
        if set(zh) != set(en):
            problems.append(f"{_rel(zh_path)}: keys only zh {sorted(set(zh) - set(en))[:5]}, only en {sorted(set(en) - set(zh))[:5]}")
        for k in set(zh) & set(en):
            if set(ICU_ARG.findall(zh[k])) != set(ICU_ARG.findall(en[k])):
                problems.append(f"{_rel(zh_path)} {k}: args {sorted(set(ICU_ARG.findall(zh[k])))} != {sorted(set(ICU_ARG.findall(en[k])))}")
            if CJK.search(en[k]) and k != "lab.public.lang":  # 语言切换按钮显示另一种语言的名称
                problems.append(f"{_rel(en_path)} {k}: CJK text in the English dictionary")
    assert not problems, "\n".join(problems)


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    lines = []
    for line in src.splitlines():
        line = re.sub(r"^\s*//.*$", "", line)
        line = re.sub(r"\s//\s.*$", "", line)  # 行尾注释（`'//'` 之类的字面量前无空白，不受影响）
        lines.append(line)
    return "\n".join(lines)


def test_no_chinese_literals_in_d3_components() -> None:
    files = [f for d in (WEB_SRC / "ui" / "lab", APP / "lab-public", APP / "routes" / "lab") for f in d.rglob("*.ts*")]
    files += sorted(APP.glob("routes/lab*.tsx"))
    files = [f for f in files if ".test." not in f.name and f.suffix in (".ts", ".tsx")]
    assert len(files) >= 40, len(files)
    hits = []
    for f in files:
        for i, line in enumerate(_strip_comments(f.read_text(encoding="utf-8")).splitlines(), 1):
            if CJK.search(line):
                hits.append(f"{_rel(f)}:{i}: {line.strip()[:80]}")
    assert not hits, "Chinese literals in D3 components (AWR-14 §13.7 rule 3):\n" + "\n".join(hits)
