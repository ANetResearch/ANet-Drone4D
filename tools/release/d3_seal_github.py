#!/usr/bin/env python3
"""tools/release/d3_seal_github.py：D3 封板的 GitHub 议题与里程碑处置（Z03；封板顺序 ⑧；D3-AC-01 封板判定的仓库侧收尾）。

依据：docs/05 ADR-194 决策 (2)(5) ⑧（关闭封板范围内的工作包议题与里程碑，P1 至 P3 的议题转入 D3.x 顺延里程碑，置顶议题 #1
更新为 D3 封板与 D4 入口）、§12.1 工作包表、§23.3 D3.x 顺延清单与去向（用户 2026-10-22 确认，R-D3-71）；AWR-06 §10.1 启动门
G0-3、G0-7。工作包与议题的映射以议题标题前缀 `WP-D3-xx` 为准，阶段以 §12.1"阶段与人周"列为准，不在脚本中另行登记。

处置规则：
- 封板范围（§12.1 阶段全部属于 P-1、P0a、P0b）：在议题中留言给出阶段报告与 D3 交付总结的链接（指向标签 `v0.3.0` 的文件），
  然后以 completed 关闭；§23.3 中由顺延条目分走的份额（如 WP-D3-37 的 `gs_eval` 产品化随 WP-D3-42 顺延）在留言中注明；
- 顺延（阶段全部属于 P1、P2、P3）：留言列出该工作包对应的顺延验收、优先级与推荐去向，转入 D3.x 顺延里程碑，议题保持打开；
  任一验收的去向为"D4 候选"时加 `D4-candidate` 标签（D4 概念设计评审决定并入 D4 或留在 D3.x，决定前按 D3.x 登记）；
- 里程碑：P-1、P0a、P0b（#2–#4）在其中议题全部关闭后关闭；P1、P2、P3（#5–#7）在议题全部转出后关闭，说明中注明去向；
  里程碑中仍有未映射的打开议题时不关闭，列为阻塞项；
- 置顶议题 #1：标题与正文改写为 D3 封板（`v0.3.0`）、D3.x 顺延与 D4 入口，保留 V0.2.x 维护里程碑与约定；
- 仓库 About 与 topics（C-51，`--no-metadata` 跳过）：About 取文案评审记录 §5 的 V0.3 定稿（≤ 120 字符），topics 为 v0.2.2 的
  12 个加 `benchmark`、`gazebo`、`lidar`、`rerun`；release 正文与 README、落地页的状态翻转（`copy.prepared.json`）不由本脚本处理，
  前置检查只核对该文件已在打标签前应用并删除；
- 幂等：已关闭、已转入或正文相同的条目不再生成动作；执行时留言带标记 `<!-- d3-seal:<键> -->`，已存在同标记留言的不重复发表。

缺省只做演练：读取仓库当前状态（只读 GET，可匿名）或离线快照，输出变更清单（Markdown）与计划（JSON），不写入 GitHub。
在线读取缺省匿名（公开仓库，约 10 个 GET），`--auth` 时带令牌。`apply` 只在 H14 用户授权后执行：须 `--confirm <标签>` 与 `--tag` 一致、以在线状态重新生成计划、全部前置检查通过（远端标签
存在、各报告在标签处存在、V0.3 发布文案已应用、§12.1 与 §23.3 的计数与 ADR-194 一致）。令牌取 `GITHUB_TOKEN`、`GH_TOKEN` 或 `~/.git-credentials`
的 github.com 条目，只放入请求头，不写入输出、日志或计划文件。

子命令：
  plan      生成计划并打印变更清单（缺省；`--state-file` 用离线快照，否则在线只读读取）；`--md-out`、`--json-out` 另存；
            `--full` 附全部留言正文；退出码 0（有阻塞项或前置检查未通过时仍为 0，阻塞与检查结果见清单）
  snapshot  在线只读读取仓库状态并写入 `--out`（供离线演练与复核）
  apply     执行计划（H14；须 `--confirm`）；逐条打印动作与 HTTP 状态，任一请求失败即停止，退出码 1

用法：
  python3 tools/release/d3_seal_github.py plan --md-out out/release/d3-seal-github.md --json-out out/release/d3-seal-github.json
  python3 tools/release/d3_seal_github.py snapshot --out out/release/github-state.json
  python3 tools/release/d3_seal_github.py plan --state-file out/release/github-state.json --seal-date 2026-10-30
  python3 tools/release/d3_seal_github.py apply --confirm v0.3.0 --seal-date 2026-10-30
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DOCS05 = ROOT / "docs" / "05-D3-设计增补与决策记录.md"
REPO = "ANetResearch/ANet-Drone4D"
API = "https://api.github.com"
TAG = "v0.3.0"
D2_TAG = "v0.2.2"
PLAN_SCHEMA = "drone4d.release.d3_seal_github.v1"

SEAL_PHASES = ("P-1", "P0a", "P0b")
DEFER_PHASES = ("P1", "P2", "P3")
PHASES = SEAL_PHASES + DEFER_PHASES
# 里程碑按标题前缀识别（2026-10-15 创建）；编号只用于核对，不一致时给出警告
MILESTONE_PREFIX = {p: f"D3 {p} " for p in PHASES}
MILESTONE_EXPECTED_NUMBER = {"P-1": 2, "P0a": 3, "P0b": 4, "P1": 5, "P2": 6, "P3": 7}
D3X_PREFIX = "D3.x"
D3X_TITLE = "D3.x · Deferred items（D3.x 顺延）"
D4_LABEL = "D4-candidate"
D4_LABEL_COLOR = "6e7781"
D4_LABEL_DESC = "D3.x 顺延条目中与 D4 范围重叠的候选，去向由 D4 概念设计评审决定"
ROADMAP_ISSUE = 1
ROADMAP_TITLE = "Roadmap · V0.3 D3 封板、D3.x 顺延、D4 入口与 V0.2.x 维护"
MARKER = "<!-- d3-seal:{key} -->"
D3X_PLACEHOLDER = "{D3X}"  # 新建的 D3.x 里程碑编号；apply 在创建后替换（含正文中的链接）
WP_TITLE_RE = re.compile(r"^(WP-D3-\d{2})\b")

# 阶段报告（封板顺序 ①）与交付总结（Z01）；路径相对仓库根
STAGE_REPORTS: dict[str, tuple[str, str]] = {
    "P-1": ("docs/impl/D3-P-1-实测报告.md", "P-1 实测报告"),
    "P0a": ("docs/impl/D3-P0a-实现与验收报告.md", "P0a 实现与验收报告"),
    "P0b": ("docs/impl/D3-P0b-实现与验收报告.md", "P0b 实现与验收报告"),
}
SUMMARY_REPORT = ("docs/impl/D3-交付总结.md", "D3 交付总结")
# 工作包专属的评审记录（R-D3-69：由 Opus 起草、用户 2026-10-22 确认）
WP_EXTRA_REPORTS: dict[str, tuple[tuple[str, str], ...]] = {
    "WP-D3-13": (("docs/impl/D3-文案评审记录.md", "文案评审记录（D3-AC-23、46）"),),
    "WP-D3-37": (("docs/impl/D3-G3DGS-评审记录.md", "高斯外观层决策评审记录（D3-AC-64）"),),
}
DOCS05_PATH = "docs/05-D3-设计增补与决策记录.md"
D4_DOC_PATH = "docs/06-D4-协同实验平台概念设计.md"
D4_SET_PATH = "docs/d4"
DOCS_SITE = "https://docs.agentnetwork.org.cn/anet4d/zh-cn/latest/project/roadmap.html"
# V0.3 发布文案的状态翻转（W-14）须在打标签前应用并删除该文件（文案评审记录 §9.2）；本脚本只核对，不应用
PREPARED_COPY = "docs/impl/v0.3-release/copy.prepared.json"
# 仓库 About 与 topics 的来源（C-51，用户 2026-10-22 确认，R-D3-71）：V0.3 About 草案见文案评审记录 §5 第 2 条，
# topics 为 v0.2.2 的 12 个（D2 发布说明 §2）加"随 V0.3 发布追加"的 4 个
COPY_RECORD = "docs/impl/D3-文案评审记录.md"
D2_NOTES = "docs/impl/D2-v0.2.2-发布说明.md"
ABOUT_MAX = 120  # 14 §21.15.8
TOPICS_MAX = 20  # GitHub 上限
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")

# ADR-194 决策 (1)(2) 与 §23.4 的计数（解析结果须与之一致，否则前置检查不通过）
EXPECT_DEFERRED_ACS = 27
EXPECT_DEFERRED_BLOCKING = 13
EXPECT_DEFERRED_DELIVERABLE = 14
EXPECT_DEST_D3X = 22
EXPECT_DEST_D4 = 5
EXPECT_SEAL_PW = 105.3
EXPECT_DEFER_PW = 50.75


class SealError(RuntimeError):
    """计划生成或执行中的不可继续错误（文档结构不符、请求失败、执行条件不满足）。"""


# ---------------------------------------------------------------- docs/05 解析


@dataclass(frozen=True)
class WorkPackage:
    wp: str  # WP-D3-06
    name: str  # 相机与调度
    owner: str
    phases: tuple[str, ...]  # 按出现顺序去重
    phase_text: str  # "P0a 5.5；P0b 2.0"
    total_pw: float

    @property
    def scope(self) -> str:
        """seal：全部阶段在封板范围；defer：全部阶段已移出；mixed：跨越两侧（ADR-194 时点不存在，出现即警告）。"""
        if all(p in SEAL_PHASES for p in self.phases):
            return "seal"
        if all(p in DEFER_PHASES for p in self.phases):
            return "defer"
        return "mixed"


@dataclass(frozen=True)
class DeferredAC:
    stage: str  # P1
    ac: str  # D3-AC-26
    title: str  # 事件编辑器
    priority: str  # 阻塞 / 应交付 / 阻塞（资产到位为前提）
    wps: tuple[str, ...]  # 主要工作包（括注外）
    wp_note: str  # 主要工作包列的括注（如 WP-D3-37 份额说明），无则空
    noted_wps: tuple[str, ...]  # 括注中提到的其他工作包（部分份额随本条顺延）
    dest: str  # "D3.x" / "D4 候选"
    dest_text: str  # 原文

    @property
    def blocking(self) -> bool:
        return self.priority.startswith("阻塞")

    @property
    def d4_candidate(self) -> bool:
        return self.dest == "D4 候选"


@dataclass(frozen=True)
class SubClause:
    stage: str
    ac: str
    content: str
    dest: str


@dataclass
class DocsModel:
    wps: dict[str, WorkPackage]
    deferred: list[DeferredAC]
    subclauses: list[SubClause]

    def acs_of(self, wp: str) -> list[DeferredAC]:
        return [a for a in self.deferred if wp in a.wps]

    def shares_of(self, wp: str) -> list[DeferredAC]:
        """括注中提到 `wp` 的顺延条目（该工作包的部分份额随之顺延）。"""
        return [a for a in self.deferred if wp in a.noted_wps and wp not in a.wps]


def _section(text: str, heading_prefix: str) -> str:
    """取以 `heading_prefix` 开头的标题到下一个同级或更高级标题之间的正文。"""
    lines = text.splitlines()
    level = heading_prefix.split(" ", 1)[0]
    start = next((i for i, ln in enumerate(lines) if ln.startswith(heading_prefix)), None)
    if start is None:
        raise SealError(f"docs/05 缺少章节：{heading_prefix}")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = re.match(r"^(#+) ", lines[j])
        if m and len(m.group(1)) <= len(level):
            end = j
            break
    return "\n".join(lines[start + 1 : end])


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


_PHASE_RE = re.compile(r"P-1|P0a|P0b|P1|P2|P3")
_PAREN_RE = re.compile(r"（[^）]*）")


def _dedupe(seq: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(seq))


def parse_work_packages(text: str) -> dict[str, WorkPackage]:
    out: dict[str, WorkPackage] = {}
    for ln in _section(text, "### 12.1 ").splitlines():
        if not ln.startswith("| WP-D3-"):
            continue
        c = _cells(ln)
        if len(c) < 5:
            raise SealError(f"§12.1 行列数不足：{ln[:60]}")
        m = re.match(r"(WP-D3-\d{2})\s+(.*)$", c[0])
        if not m:
            raise SealError(f"§12.1 工作包编号无法识别：{c[0]}")
        wp, name = m.group(1), m.group(2).strip()
        phases = _dedupe(_PHASE_RE.findall(c[3]))
        if not phases:
            raise SealError(f"§12.1 {wp} 的阶段列无法识别：{c[3]}")
        try:
            total = float(c[4])
        except ValueError as e:
            raise SealError(f"§12.1 {wp} 的合计列不是数字：{c[4]}") from e
        if wp in out:
            raise SealError(f"§12.1 工作包重复：{wp}")
        out[wp] = WorkPackage(wp, name, c[1], phases, c[3], total)
    if not out:
        raise SealError("§12.1 未解析到工作包")
    return out


def parse_deferred(text: str) -> tuple[list[DeferredAC], list[SubClause]]:
    body = _section(text, "### 23.3 ")
    acs: list[DeferredAC] = []
    subs: list[SubClause] = []
    for ln in body.splitlines():
        if not ln.startswith("| P"):
            continue
        c = _cells(ln)
        if len(c) == 5 and c[1].startswith("D3-AC-"):
            m = re.match(r"(D3-AC-\d{2})\s*(.*)$", c[1])
            if not m:
                raise SealError(f"§23.3 验收编号无法识别：{c[1]}")
            outer = _PAREN_RE.sub("", c[3])
            wps = _dedupe(re.findall(r"WP-D3-\d{2}", outer))
            notes = _PAREN_RE.findall(c[3])
            noted = _dedupe([w for n in notes for w in re.findall(r"WP-D3-\d{2}", n) if w not in wps])
            dest = "D4 候选" if c[4].startswith("D4 候选") else "D3.x" if c[4].startswith("D3.x") else ""
            if not dest or not wps:
                raise SealError(f"§23.3 {m.group(1)} 的工作包或去向无法识别：{c[3]} / {c[4]}")
            acs.append(DeferredAC(c[0], m.group(1), m.group(2).strip(), c[2], wps, " ".join(notes), noted, dest, c[4]))
        elif len(c) == 4 and c[1].startswith("D3-AC-"):
            subs.append(SubClause(c[0], c[1], c[2], c[3]))
    if not acs:
        raise SealError("§23.3 未解析到顺延验收")
    return acs, subs


def load_docs(path: Path = DOCS05) -> DocsModel:
    text = path.read_text(encoding="utf-8")
    wps = parse_work_packages(text)
    deferred, subs = parse_deferred(text)
    return DocsModel(wps, deferred, subs)


@dataclass(frozen=True)
class RepoMeta:
    about: str
    topics: tuple[str, ...]


def _next_text_line(lines: list[str], i: int) -> str:
    for ln in lines[i + 1 :]:
        if ln.strip():
            return ln.strip()
    return ""


def parse_repo_meta(record_text: str, d2_notes_text: str) -> RepoMeta:
    """About 取文案评审记录 §5"V0.3 About 草案"之后的第一行正文；topics 取 D2 发布说明的 V0.2 列表加 V0.3 追加项。"""
    sec = _section(record_text, "## 5. ").splitlines()
    i = next((k for k, ln in enumerate(sec) if "V0.3 About 草案" in ln), None)
    if i is None:
        raise SealError(f"{COPY_RECORD} §5 缺少 V0.3 About 草案")
    about = _next_text_line(sec, i)
    lines = d2_notes_text.splitlines()
    j = next((k for k, ln in enumerate(lines) if ln.startswith("### Topics (V0.2)")), None)
    k = next((k for k, ln in enumerate(lines) if ln.startswith("Topics to add with the V0.3 release")), None)
    if j is None or k is None:
        raise SealError(f"{D2_NOTES} 缺少 Topics (V0.2) 或 V0.3 追加项")
    topics = _dedupe(re.findall(r"`([^`]+)`", _next_text_line(lines, j)) + re.findall(r"`([^`]+)`", lines[k]))
    if not about or not topics:
        raise SealError("About 或 topics 解析为空")
    return RepoMeta(about, topics)


def load_repo_meta(root: Path = ROOT) -> RepoMeta:
    return parse_repo_meta((root / COPY_RECORD).read_text(encoding="utf-8"), (root / D2_NOTES).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 计划


@dataclass
class Config:
    repo: str = REPO
    tag: str = TAG
    d2_tag: str = D2_TAG
    seal_date: str = ""  # YYYY-MM-DD（UTC）；空为当天
    d3_branch: str = "d3-dev"
    roadmap_issue: int = ROADMAP_ISSUE

    def blob(self, path: str) -> str:
        return f"https://github.com/{self.repo}/blob/{self.tag}/{urllib.parse.quote(path)}"

    def tree(self, path: str) -> str:
        return f"https://github.com/{self.repo}/tree/{self.tag}/{urllib.parse.quote(path)}"

    def milestone_url(self, number: int | str) -> str:
        return f"https://github.com/{self.repo}/milestone/{number}"


@dataclass
class Check:
    name: str
    ok: bool | None  # None：离线演练无法判定
    detail: str


@dataclass
class Op:
    seq: int
    kind: str  # create_label / create_milestone / comment / move_milestone / add_label / close_issue / close_milestone / update_issue
    target: str  # "#24"、"milestone #3"、"label D4-candidate"
    summary: str
    method: str
    path: str  # 相对 /repos/{repo}；新里程碑编号以 {D3X} 占位
    payload: dict[str, Any]
    marker: str = ""  # comment 的幂等标记


@dataclass
class Plan:
    schema: str
    generated_at: str
    config: dict[str, Any]
    source: str  # "live" / 快照路径
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    ops: list[Op] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def add(self, kind: str, target: str, summary: str, method: str, path: str, payload: dict[str, Any],
            marker: str = "") -> Op:
        op = Op(len(self.ops) + 1, kind, target, summary, method, path, payload, marker)
        self.ops.append(op)
        return op

    @property
    def ready(self) -> bool:
        return all(c.ok is True for c in self.checks) and not self.blocked

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["ready"] = self.ready
        return d


def _fmt_pw(x: float) -> str:
    return f"{x:g}"


def _wp_issue_index(issues: list[dict[str, Any]], warnings: list[str]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for it in issues:
        m = WP_TITLE_RE.match(it.get("title", ""))
        if not m:
            continue
        wp = m.group(1)
        if wp in idx:
            warnings.append(f"工作包 {wp} 对应多个议题：#{idx[wp]['number']}、#{it['number']}；只处置编号较小者")
            if it["number"] > idx[wp]["number"]:
                continue
        idx[wp] = it
    return idx


def _ms_number(it: dict[str, Any]) -> int | None:
    ms = it.get("milestone")
    return ms.get("number") if ms else None


def _label_names(it: dict[str, Any]) -> list[str]:
    return [lb["name"] if isinstance(lb, dict) else str(lb) for lb in it.get("labels", [])]


def _report_lines(cfg: Config, wp: WorkPackage) -> list[str]:
    lines = []
    for p in wp.phases:
        path, title = STAGE_REPORTS[p]
        lines.append(f"- [{title}]({cfg.blob(path)})")
    for path, title in WP_EXTRA_REPORTS.get(wp.wp, ()):
        lines.append(f"- [{title}]({cfg.blob(path)})")
    path, title = SUMMARY_REPORT
    lines.append(f"- [{title}]({cfg.blob(path)})（封板范围 41 条验收的通过状态汇总、证据链接与 D3.x 顺延清单）")
    return lines


def render_close_comment(cfg: Config, model: DocsModel, wp: WorkPackage, date: str) -> str:
    out = [
        MARKER.format(key=wp.wp),
        "**D3 封板处置：完成并关闭**",
        "",
        f"本工作包的阶段与人周为 {wp.phase_text}（合计 {_fmt_pw(wp.total_pw)} 人周），在 D3 封板范围内（docs/05 ADR-194 决策 (1)）。"
        f"D3 已于 {date} 封板，标签 `{cfg.tag}`；按 ADR-194 决策 (5) ⑧ 关闭本议题。",
        "",
        "封板范围内各验收的阈值、实测值、测试计数与产物路径见：",
        "",
        *_report_lines(cfg, wp),
    ]
    shares = model.shares_of(wp.wp)
    if shares:
        out += ["", "以下份额不随本议题关闭，随所列验收顺延至 D3.x（docs/05 §23.3）："]
        for a in shares:
            out.append(f"- {a.ac} {a.title}（{a.stage}，主要工作包 {'、'.join(a.wps)}）：{a.wp_note}")
    return "\n".join(out) + "\n"


def render_defer_comment(cfg: Config, model: DocsModel, wp: WorkPackage, d3x_ref: str) -> str:
    acs = model.acs_of(wp.wp)
    out = [
        MARKER.format(key=wp.wp),
        "**D3 封板处置：顺延至 D3.x**",
        "",
        f"本工作包的阶段与人周为 {wp.phase_text}（合计 {_fmt_pw(wp.total_pw)} 人周），按 docs/05 ADR-194 决策 (2) 移出 D3 封板"
        f"（`{cfg.tag}`），转入 {d3x_ref}。编号、阈值与依赖原样保留，交付时按 docs/05 §11 原阈值验收；顺延不是豁免。",
        "",
    ]
    if acs:
        out += ["| 验收 | 阶段 | 优先级 | 推荐去向（§23.3，用户 2026-10-22 确认） |", "|---|---|---|---|"]
        out += [f"| {a.ac} {a.title} | {a.stage} | {a.priority} | {a.dest_text} |" for a in acs]
    else:
        out.append("§23.3 未列出以本工作包为主要工作包的验收；本工作包随所在阶段整体顺延。")
    if any(a.d4_candidate for a in acs):
        out += ["", f"含 D4 候选条目，已加 `{D4_LABEL}` 标签：由 D4 概念设计评审（AWR-06）决定并入 D4 或留在 D3.x，决定前按 D3.x 登记。"]
    out += ["", "D3.x 推荐以 `v0.3.x`（x ≥ 1）发布（ADR-194 决策 (6)）。"]
    return "\n".join(out) + "\n"


def _milestone_close_note(cfg: Config, phase: str, date: str, d3x_ref: str) -> str:
    if phase in SEAL_PHASES:
        path, title = STAGE_REPORTS[phase]
        return (f"【{date} 关闭】D3 封板（`{cfg.tag}`）完成，本阶段工作包议题已全部关闭（docs/05 ADR-194 决策 (5) ⑧）。"
                f"验收报告：{title}（{path}）；汇总：{SUMMARY_REPORT[1]}（{SUMMARY_REPORT[0]}）。")
    return (f"【{date} 关闭】本阶段按 docs/05 ADR-194 决策 (2) 移出 D3 封板，工作包议题已转入 {d3x_ref}；"
            "验收编号与阈值保留，去向见 docs/05 §23.3。")


def _d3x_description(cfg: Config) -> str:
    return (f"D3 封板（{cfg.tag}）移出的 P1 至 P3 工作包与验收：27 条（阻塞 13 条、应交付 14 条），50.75 人周；编号、阈值与依赖保留，"
            "交付时按 docs/05 §11 原阈值验收（ADR-194 决策 (2)、§23.3）。推荐以 v0.3.x 发布；标 D4-candidate 的条目由 D4 "
            "概念设计评审决定并入 D4 或留在 D3.x。另含 D3-AC-21、25 的 Tier B 参考设备正式测量（ADR-219）。")


def render_roadmap(cfg: Config, model: DocsModel, date: str, rows: dict[str, Any]) -> str:
    """置顶议题 #1 的新正文。`rows` 由 build_plan 提供：里程碑编号与标题、各阶段工作包议题、D3.x 里程碑引用。"""
    q = cfg.blob
    seal_acs = "阻塞 39 条、应交付 2 条"
    lines = [
        "本 Issue 汇总 ANet4D 的路线图。每个阶段对应一个 Milestone，每个工作包或待办对应一个 Issue；进度以 Milestone 的完成度为准。",
        "",
        f"### D3 封板（V0.3，`{cfg.tag}`）",
        "",
        f"D3 已于 {date} 封板，标签 `{cfg.tag}` 打在 `main` 的合并提交上（docs/05 ADR-194 决策 (5) ⑧）。封板范围为 P-1、P0a、P0b "
        f"三个出口的 41 条验收（{seal_acs}），封板范围内不设豁免，性能类条款以独占卡正式数字判定。逐条阈值、实测值、测试计数与产物"
        "路径见以下报告：",
        "",
        "| 报告 | 范围 |",
        "|---|---|",
        f"| [{STAGE_REPORTS['P-1'][1]}]({q(STAGE_REPORTS['P-1'][0])}) | D3-AC-47；P-1 冻结 ADR（ADR-213 至 ADR-217） |",
        f"| [{WP_EXTRA_REPORTS['WP-D3-13'][0][1]}]({q(WP_EXTRA_REPORTS['WP-D3-13'][0][0])}) | D3-AC-23、46 的文案抽查与裁决 |",
        f"| [{STAGE_REPORTS['P0a'][1]}]({q(STAGE_REPORTS['P0a'][0])}) | D3-AC-01 至 10、17 至 19、34、51、56 至 58、60 |",
        f"| [{STAGE_REPORTS['P0b'][1]}]({q(STAGE_REPORTS['P0b'][0])}) | D3-AC-11 至 16、20 至 25、33、35、43、59、61 至 64 |",
        f"| [{WP_EXTRA_REPORTS['WP-D3-37'][0][1]}]({q(WP_EXTRA_REPORTS['WP-D3-37'][0][0])}) | D3-AC-64 的决策评审 |",
        f"| [{SUMMARY_REPORT[1]}]({q(SUMMARY_REPORT[0])}) | 41 条验收的通过状态、证据链接、D3.x 顺延清单与 D4 遗留事项 |",
        "",
        "### Milestone",
        "",
        "| Milestone | 状态 | 工作包 Issue |",
        "|---|---|---|",
    ]
    lines += [f"| [{r['title']}]({cfg.milestone_url(r['number'])}) | {r['status']} | {r['issues']} |" for r in rows["milestones"]]
    lines += [
        "",
        "### D3.x 顺延",
        "",
        f"P1 至 P3 的 27 条验收（阻塞 13 条、应交付 14 条）与 50.75 人周按 ADR-194 决策 (2) 移出 D3 封板，登记为 D3.x 顺延清单"
        f"（[docs/05 §23.3]({q(DOCS05_PATH)})）。顺延不是豁免：编号、阈值与依赖原样保留，交付时按原阈值验收；推荐以 `v0.3.x` 发布。"
        f"推荐去向为 D3.x 22 条、D4 候选 5 条；D4 候选由 D4 概念设计评审决定并入 D4 或留在 D3.x，相应 Issue 带 `{D4_LABEL}` 标签。",
        "",
        "| 工作包 | 阶段 | 顺延验收 | 去向 |",
        "|---|---|---|---|",
    ]
    for r in rows["deferred"]:
        lines.append(f"| {r['issue']} {r['wp']} {r['name']} | {r['phases']} | {r['acs']} | {r['dest']} |")
    if model.subclauses:
        lines += ["", "封板范围内另有以下子条款的正式测量顺延到 D3.x（ADR-219），所在验收仍在封板范围内、计数不变："]
        lines += [f"- {s.ac}：{s.content}" for s in model.subclauses]
    lines += [
        "",
        "### D4 入口",
        "",
        "下一大版本统一称 D4（协同实验平台，AWR-06），当前处于调研与概念设计阶段。D3 封板是 D4 实现启动门 G0 的前提"
        f"（AWR-06 §10.1 G0-1 至 G0-8）；G0 通过前只进行设计评审与 RFC 准备，D4 开发分支自 `{cfg.tag}` 提交创建（G0-7）。"
        "D4 的 Milestone 与工作包 Issue 在 G0 通过后建立。",
        "",
        f"- 概念设计：[{D4_DOC_PATH}]({q(D4_DOC_PATH)})",
        f"- 设计文档集：[{D4_SET_PATH}/]({cfg.tree(D4_SET_PATH)})",
        "",
        "### 分支与发布（docs/05 ADR-194、ADR-209）",
        "",
        f"- `main`：已发布版本，当前 `{cfg.tag}`（D3 封板，`d3-dev` 以合并提交合入）。公开站主构建为 D2 标签构建 `{cfg.d2_tag}`，"
        "`/lab/` 为 `main` 合并提交的独立静态构建；公开样例中超过 64 MB 的文件与样例包经 ModelScope 分发。",
        f"- `d2`：D2 维护分支（`v0.2.1`、`{cfg.d2_tag}`）；D2 修复只在该分支进行，再合入 D3 维护分支，不反向合并。",
        f"- `{cfg.d3_branch}`：封板后改作 D3 维护分支，D3.x 顺延条目在其上交付。",
        "- 标签只增不改，不删除、不重打（ADR-194 决策 (6)）。",
        "",
        "### 约定",
        "",
        f"- D3 工作包的 Issue 标题以 `WP-D3-xx` 开头，内容与 [docs/05 §12.1]({q(DOCS05_PATH)}) 一致；范围变化先改设计文档，再更新 Issue。",
        f"- 标签：`D3`、`D2` 区分版本；`M22`–`M27` 等标明所有模块；`work-package`、`known-issue`、`acceptance`、`research`、"
        f"`infra`、`frontend`、`sdk` 标明类型；`{D4_LABEL}` 标明去向待 D4 概念设计评审决定的顺延条目。",
        "- Pull Request 在描述中以 `Closes #<编号>` 关联 Issue；验收以 docs/05 §11 为准，验收报告写入 `docs/impl/`。",
        "",
        "### 文档",
        "",
        f"- D3 设计：[docs/05（{cfg.tag}）]({q(DOCS05_PATH)})",
        f"- 模块 PRD：[docs/modules/]({cfg.tree('docs/modules')})（M22–M27）",
        f"- D3 调研：[docs/research/d3/]({cfg.tree('docs/research/d3')})",
        f"- 文档站：{DOCS_SITE}",
    ]
    return "\n".join(lines) + "\n"


def _consistency_checks(model: DocsModel) -> list[Check]:
    d = model.deferred
    n_block = sum(a.blocking for a in d)
    n_d4 = sum(a.d4_candidate for a in d)
    seal_pw = round(sum(w.total_pw for w in model.wps.values() if w.scope == "seal"), 2)
    defer_pw = round(sum(w.total_pw for w in model.wps.values() if w.scope == "defer"), 2)
    defer_wps = {w for a in d for w in a.wps}
    bad_wp = sorted(w for w in defer_wps if w not in model.wps or model.wps[w].scope != "defer")
    mixed = sorted(w.wp for w in model.wps.values() if w.scope == "mixed")
    orphan = sorted(w.wp for w in model.wps.values() if w.scope == "defer" and not model.acs_of(w.wp))
    return [
        Check("§23.3 顺延验收计数", len(d) == EXPECT_DEFERRED_ACS and n_block == EXPECT_DEFERRED_BLOCKING
              and len(d) - n_block == EXPECT_DEFERRED_DELIVERABLE,
              f"{len(d)} 条（阻塞 {n_block}、应交付 {len(d) - n_block}）；ADR-194 为 27（13、14）"),
        Check("§23.3 推荐去向计数", n_d4 == EXPECT_DEST_D4 and len(d) - n_d4 == EXPECT_DEST_D3X,
              f"D3.x {len(d) - n_d4}、D4 候选 {n_d4}；§23.3 为 22、5"),
        Check("§12.1 封板与顺延人周", abs(seal_pw - EXPECT_SEAL_PW) < 1e-6 and abs(defer_pw - EXPECT_DEFER_PW) < 1e-6,
              f"封板 {_fmt_pw(seal_pw)}、顺延 {_fmt_pw(defer_pw)}；§23.4 为 105.3、50.75"),
        Check("§23.3 工作包均属顺延阶段", not bad_wp and not mixed,
              "一致" if not bad_wp and not mixed else f"不属顺延阶段：{bad_wp}；跨封板与顺延：{mixed}"),
        Check("顺延工作包均有顺延验收", not orphan, "一致" if not orphan else f"无对应验收：{orphan}"),
    ]


def build_plan(model: DocsModel, state: dict[str, Any], cfg: Config, *, source: str = "live",
               local_root: Path | None = ROOT, now: datetime | None = None, meta: RepoMeta | None = None) -> Plan:
    """由 docs/05 模型与仓库状态生成处置计划（纯函数：不访问网络；`local_root` 为 None 时跳过本地文件检查；
    `meta` 为 None 时不处置仓库 About 与 topics）。"""
    now = now or datetime.now(UTC)
    date = cfg.seal_date or now.strftime("%Y-%m-%d")
    plan = Plan(PLAN_SCHEMA, now.strftime("%Y-%m-%dT%H:%M:%SZ"), {**asdict(cfg), "seal_date": date}, source)

    # 前置检查：文档一致性、本地报告、远端标签与标签处报告
    plan.checks += _consistency_checks(model)
    reports = [*(p for p, _ in STAGE_REPORTS.values()), SUMMARY_REPORT[0],
               *(p for v in WP_EXTRA_REPORTS.values() for p, _ in v)]
    if local_root is not None:
        missing = [p for p in reports if not (local_root / p).is_file()]
        plan.checks.append(Check("报告在工作树中存在", not missing, "全部存在" if not missing else "缺：" + "、".join(missing)))
    tag = state.get("tag") or {}
    plan.checks.append(Check(f"远端标签 {cfg.tag} 存在", tag.get("exists"),
                             tag.get("detail") or ("未读取（离线快照未含标签信息）" if tag.get("exists") is None else "")))
    at_tag = state.get("reports_at_tag") or {}
    unknown = [p for p in reports if at_tag.get(p) is None]
    absent = [p for p in reports if at_tag.get(p) is False]
    plan.checks.append(Check(f"报告在 {cfg.tag} 处存在", None if unknown and not absent else not absent and not unknown,
                             "全部存在" if not unknown and not absent else
                             ("缺：" + "、".join(absent) if absent else "未读取：" + "、".join(unknown))))
    local_left = local_root is not None and (local_root / PREPARED_COPY).exists()
    remote_left = state.get("prepared_at_tag")
    plan.checks.append(Check(
        "V0.3 发布文案已应用", False if local_left or remote_left else (None if remote_left is None else True),
        f"{PREPARED_COPY} " + ("仍在工作树中（W-14 状态翻转未应用）" if local_left else
                               f"仍在 {cfg.tag} 中" if remote_left else
                               f"{cfg.tag} 处未读取" if remote_left is None else "已删除（已应用）")))
    if meta is not None:
        bad = [t for t in meta.topics if not TOPIC_RE.match(t)]
        plan.checks.append(Check("About 与 topics 符合规范", len(meta.about) <= ABOUT_MAX and len(meta.topics) <= TOPICS_MAX
                                 and not bad, f"About {len(meta.about)} 字符（上限 {ABOUT_MAX}）；topics {len(meta.topics)} 个"
                                 f"（上限 {TOPICS_MAX}）" + (f"；不合规：{bad}" if bad else "")))

    milestones = state.get("milestones", [])
    issues = [i for i in state.get("issues", []) if "pull_request" not in i]
    labels = {lb["name"] for lb in state.get("labels", [])}

    # 里程碑识别
    ms_by_phase: dict[str, dict[str, Any]] = {}
    for p in PHASES:
        cand = [m for m in milestones if m.get("title", "").startswith(MILESTONE_PREFIX[p])]
        if not cand:
            plan.warnings.append(f"未找到 {p} 里程碑（标题前缀 \"{MILESTONE_PREFIX[p]}\"）")
            continue
        if len(cand) > 1:
            plan.warnings.append(f"{p} 对应多个里程碑：{[m['number'] for m in cand]}；取编号最小者")
        m = min(cand, key=lambda x: x["number"])
        if m["number"] != MILESTONE_EXPECTED_NUMBER[p]:
            plan.warnings.append(f"{p} 里程碑编号为 #{m['number']}，路线图约定为 #{MILESTONE_EXPECTED_NUMBER[p]}")
        ms_by_phase[p] = m
    d3x = next((m for m in sorted(milestones, key=lambda x: x["number"]) if m.get("title", "").startswith(D3X_PREFIX)), None)

    wp_issue = _wp_issue_index(issues, plan.warnings)
    for wp in sorted(set(wp_issue) - set(model.wps)):
        plan.warnings.append(f"议题 #{wp_issue[wp]['number']} 的工作包 {wp} 不在 docs/05 §12.1，未处置")
    for wp in sorted(set(model.wps) - set(wp_issue)):
        plan.warnings.append(f"docs/05 §12.1 的 {wp} 没有对应议题，未处置")
    for w in model.wps.values():
        if w.scope == "mixed":
            plan.warnings.append(f"{w.wp} 的阶段跨封板与顺延两侧（{w.phase_text}），按封板范围处置，顺延份额须另行登记")

    # 1. 标签与 D3.x 里程碑
    need_d4 = any(a.d4_candidate for a in model.deferred)
    if need_d4 and D4_LABEL not in labels:
        plan.add("create_label", f"label {D4_LABEL}", f"新建标签 `{D4_LABEL}`（D4 候选）", "POST", "/labels",
                 {"name": D4_LABEL, "color": D4_LABEL_COLOR, "description": D4_LABEL_DESC})
    if d3x is None:
        plan.add("create_milestone", "milestone D3.x（新建）", f"新建里程碑 \"{D3X_TITLE}\"", "POST", "/milestones",
                 {"title": D3X_TITLE, "state": "open", "description": _d3x_description(cfg)})
        d3x_num: int | str = D3X_PLACEHOLDER
        d3x_ref = f"D3.x 顺延 Milestone [#{D3X_PLACEHOLDER}]({cfg.milestone_url(D3X_PLACEHOLDER)})"
    else:
        d3x_num = d3x["number"]
        d3x_ref = f"D3.x 顺延 Milestone [#{d3x_num}]({cfg.milestone_url(d3x_num)})"
        if d3x.get("state") != "open":
            plan.warnings.append(f"D3.x 里程碑 #{d3x_num} 已关闭；转入的议题仍会挂在该里程碑下")

    leaving: set[int] = set()  # 计划中关闭或转出的议题编号
    deferred_rows: list[dict[str, str]] = []
    phase_issue_nums: dict[str, list[int]] = {p: [] for p in PHASES}
    n_close = n_defer = n_done = 0

    # 2. 顺延工作包：留言、转入 D3.x、D4 候选标签
    def _order(w: WorkPackage) -> tuple[int, int, str]:
        it = wp_issue.get(w.wp)
        return PHASES.index(w.phases[0]), it["number"] if it else 1 << 30, w.wp

    for w in sorted((w for w in model.wps.values() if w.scope == "defer"), key=_order):
        it = wp_issue.get(w.wp)
        acs = model.acs_of(w.wp)
        d4 = any(a.d4_candidate for a in acs)
        dest = "、".join(_dedupe([a.dest for a in acs])) or "D3.x"
        deferred_rows.append({"issue": f"#{it['number']}" if it else "—", "wp": w.wp, "name": w.name,
                              "phases": "、".join(w.phases), "acs": "、".join(a.ac.removeprefix("D3-") for a in acs) or "—",
                              "dest": dest})
        if not it:
            continue
        n = it["number"]
        phase_issue_nums[w.phases[0]].append(n)
        if it.get("state") != "open":
            plan.warnings.append(f"顺延工作包 {w.wp} 的议题 #{n} 已关闭，未处置（顺延条目应保持打开）")
            continue
        in_d3x = d3x is not None and _ms_number(it) == d3x["number"]
        if not in_d3x:
            plan.add("comment", f"#{n}", f"{w.wp} 留言：顺延至 D3.x（{len(acs)} 条验收，去向 {dest}）", "POST",
                     f"/issues/{n}/comments", {"body": render_defer_comment(cfg, model, w, d3x_ref)},
                     marker=MARKER.format(key=w.wp))
            plan.add("move_milestone", f"#{n}", f"{w.wp} 转入 D3.x 里程碑（自 #{_ms_number(it)}）", "PATCH", f"/issues/{n}",
                     {"milestone": d3x_num})
            n_defer += 1
        else:
            n_done += 1
        if d4 and D4_LABEL not in _label_names(it):
            plan.add("add_label", f"#{n}", f"{w.wp} 加标签 `{D4_LABEL}`", "POST", f"/issues/{n}/labels", {"labels": [D4_LABEL]})
        leaving.add(n)

    # 3. 封板范围工作包：留言并关闭
    for w in sorted((w for w in model.wps.values() if w.scope != "defer"), key=_order):
        it = wp_issue.get(w.wp)
        if not it:
            continue
        n = it["number"]
        phase_issue_nums[w.phases[0]].append(n)
        exp = ms_by_phase.get(w.phases[0])
        if exp and _ms_number(it) != exp["number"]:
            plan.warnings.append(f"{w.wp}（#{n}）挂在里程碑 #{_ms_number(it)}，按 §12.1 应为 #{exp['number']}（开始阶段 {w.phases[0]}）")
        if it.get("state") != "open":
            n_done += 1
            continue
        plan.add("comment", f"#{n}", f"{w.wp} 留言：完成并关闭，附 {len(_report_lines(cfg, w))} 个报告链接", "POST",
                 f"/issues/{n}/comments", {"body": render_close_comment(cfg, model, w, date)},
                 marker=MARKER.format(key=w.wp))
        plan.add("close_issue", f"#{n}", f"关闭 {w.wp}（completed）", "PATCH", f"/issues/{n}",
                 {"state": "closed", "state_reason": "completed"})
        leaving.add(n)
        n_close += 1

    # 4. 阶段里程碑：议题全部关闭或转出后关闭
    ms_rows: list[dict[str, Any]] = []
    m1 = next((m for m in milestones if m["number"] == 1), None)
    if m1:
        ms_rows.append({"number": 1, "title": m1["title"], "status": "进行中" if m1.get("state") == "open" else "已关闭",
                        "issues": "D2 已知问题与缺陷"})
    for p in PHASES:
        m = ms_by_phase.get(p)
        if not m:
            continue
        remaining = sorted(i["number"] for i in issues if _ms_number(i) == m["number"] and i.get("state") == "open"
                           and i["number"] not in leaving)
        wp_nums = "、".join(f"#{x}" for x in sorted(phase_issue_nums[p])) or "—"
        if p in SEAL_PHASES:
            status, row_issues = f"已关闭（{date} 封板）", f"{wp_nums}（已关闭）"
        else:
            status, row_issues = "已关闭（顺延至 D3.x）", f"{wp_nums}（已转入 D3.x）"
        if remaining:
            plan.blocked.append(f"{p} 里程碑 #{m['number']} 仍有未映射的打开议题 {'、'.join(f'#{x}' for x in remaining)}，不能关闭")
            status = "打开（存在未处置议题）"
        elif m.get("state") == "open":
            note = _milestone_close_note(cfg, p, date, f"D3.x 顺延里程碑（{D3X_TITLE}）")
            desc = (m.get("description") or "").rstrip()
            plan.add("close_milestone", f"milestone #{m['number']}", f"关闭 {m['title'].split(' · ')[0]} 里程碑", "PATCH",
                     f"/milestones/{m['number']}",
                     {"state": "closed", "description": (desc + "\n\n" + note).strip() if note not in desc else desc})
        ms_rows.append({"number": m["number"], "title": m["title"], "status": status, "issues": row_issues})
    d3x_deferred = "、".join(r["issue"] for r in deferred_rows if r["issue"] != "—")
    ms_rows.append({"number": d3x_num, "title": d3x["title"] if d3x else D3X_TITLE,
                    "status": "进行中", "issues": d3x_deferred or "—"})

    # 5. 置顶议题 #1
    road = next((i for i in issues if i["number"] == cfg.roadmap_issue), None)
    body = render_roadmap(cfg, model, date, {"milestones": ms_rows, "deferred": deferred_rows})
    if road is None:
        plan.blocked.append(f"未找到路线图议题 #{cfg.roadmap_issue}")
    elif road.get("body", "") != body or road.get("title") != ROADMAP_TITLE:
        plan.add("update_issue", f"#{cfg.roadmap_issue}", f"改写置顶路线图：标题 \"{ROADMAP_TITLE}\"，正文 {len(body)} 字符",
                 "PATCH", f"/issues/{cfg.roadmap_issue}", {"title": ROADMAP_TITLE, "body": body})

    # 6. 仓库 About 与 topics（C-51）
    if meta is not None:
        rm = state.get("repo_meta")
        if rm is None:
            plan.warnings.append("快照未含仓库 About 与 topics，按目标值生成动作")
        if rm is None or rm.get("description") != meta.about:
            plan.add("update_repo", "repository", f"About 改为 V0.3 定稿（{len(meta.about)} 字符）", "PATCH", "",
                     {"description": meta.about})
        if rm is None or sorted(rm.get("topics") or []) != sorted(meta.topics):
            added = sorted(set(meta.topics) - set((rm or {}).get("topics") or []))
            removed = sorted(set((rm or {}).get("topics") or []) - set(meta.topics))
            def _codes(xs: list[str]) -> str:
                return "、".join(f"`{x}`" for x in xs) or "无"

            plan.add("replace_topics", "repository", f"topics 共 {len(meta.topics)} 个（新增 {_codes(added)}；"
                     f"移除 {_codes(removed)}）", "PUT", "/topics", {"names": list(meta.topics)})

    plan.stats = {"wp_close": n_close, "wp_defer": n_defer, "wp_already_done": n_done,
                  "milestones_close": sum(o.kind == "close_milestone" for o in plan.ops),
                  "ops": len(plan.ops), "d4_candidates": sorted({w for a in model.deferred if a.d4_candidate for w in a.wps})}
    return plan


# ---------------------------------------------------------------- 输出


def render_markdown(plan: Plan, *, full: bool = False) -> str:
    cfg = plan.config
    mark = {True: "通过", False: "未通过", None: "未判定"}
    lines = [
        f"# D3 封板 GitHub 处置清单（{'可执行' if plan.ready else '演练'}）",
        "",
        f"- 仓库：`{cfg['repo']}`；封板标签：`{cfg['tag']}`；封板日期：{cfg['seal_date']}（UTC）",
        f"- 状态来源：{plan.source}；生成时间：{plan.generated_at}",
        f"- 动作 {plan.stats.get('ops', 0)} 项：关闭工作包议题 {plan.stats.get('wp_close', 0)} 个，转入 D3.x "
        f"{plan.stats.get('wp_defer', 0)} 个，关闭里程碑 {plan.stats.get('milestones_close', 0)} 个；已处置 "
        f"{plan.stats.get('wp_already_done', 0)} 个",
        f"- D4 候选工作包：{'、'.join(plan.stats.get('d4_candidates', [])) or '无'}",
        "- 本清单为演练输出，未写入 GitHub；执行须 H14 用户授权后运行 `apply --confirm <标签>`。",
        "",
        "## 前置检查",
        "",
        "| 检查 | 结果 | 说明 |",
        "|---|---|---|",
        *(f"| {c.name} | {mark[c.ok]} | {c.detail} |" for c in plan.checks),
        "",
    ]
    if plan.blocked:
        lines += ["## 阻塞项", "", *(f"- {b}" for b in plan.blocked), ""]
    if plan.warnings:
        lines += ["## 警告", "", *(f"- {w}" for w in plan.warnings), ""]
    lines += ["## 变更清单", "", "| 序号 | 动作 | 对象 | 内容 | 请求 |", "|---|---|---|---|---|"]
    lines += [f"| {o.seq} | {o.kind} | {o.target} | {o.summary} | `{o.method} {o.path or '/'}` |" for o in plan.ops]
    lines += ["", f"请求路径相对 `{API}/repos/{cfg['repo']}`；`{{D3X}}` 为新建 D3.x 里程碑的编号，执行时替换。"]
    lines.append("")
    shown: set[str] = set()
    bodies = []
    for o in plan.ops:
        if o.kind not in ("comment", "update_issue", "create_milestone", "update_repo", "replace_topics"):
            continue
        key = o.kind + ("-close" if "完成并关闭" in o.summary else "-defer" if "顺延" in o.summary else "")
        if not full and key in shown:
            continue
        shown.add(key)
        text = o.payload.get("body") or o.payload.get("description") or ", ".join(f"`{t}`" for t in o.payload.get("names", []))
        bodies += [f"### {o.seq}. {o.target} {o.summary}", "", "````markdown", text.rstrip(), "````", ""]
    if bodies:
        lines += ["## 正文" + ("" if full else "（每类示例一份，`--full` 列出全部）"), "", *bodies]
    return "\n".join(lines)


# ---------------------------------------------------------------- GitHub 访问

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def _urllib_transport(method: str, url: str, headers: dict[str, str], data: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def load_token() -> str | None:
    """令牌：GITHUB_TOKEN、GH_TOKEN，或 ~/.git-credentials 中 github.com 的条目；不打印、不落盘。"""
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    cred = Path.home() / ".git-credentials"
    try:
        for ln in cred.read_text(encoding="utf-8").splitlines():
            u = urllib.parse.urlsplit(ln.strip())
            if u.hostname == "github.com" and u.password:
                return urllib.parse.unquote(u.password)
    except OSError:
        return None
    return None


class GitHub:
    def __init__(self, repo: str, token: str | None = None, transport: Transport | None = None):
        self.repo = repo
        self._token = token
        self._t = transport or _urllib_transport
        self.calls: list[tuple[str, str, int]] = []  # (method, path, status)，不含令牌与正文

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, ok: tuple[int, ...] = (200, 201)
                ) -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{API}/repos/{self.repo}{path}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "anet4d-d3-seal"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        status, raw = self._t(method, url, headers, data)
        self.calls.append((method, path, status))
        try:
            payload = json.loads(raw.decode() or "null")
        except ValueError:
            payload = None
        if status not in ok:
            msg = payload.get("message") if isinstance(payload, dict) else ""
            raise SealError(f"{method} {path} 返回 {status}：{msg}")
        return status, payload

    def paged(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        sep = "&" if "?" in path else "?"
        for page in range(1, 51):
            _, items = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            out += items or []
            if not items or len(items) < 100:
                return out
        raise SealError(f"{path} 超过 50 页")


def fetch_state(gh: GitHub, cfg: Config) -> dict[str, Any]:
    """只读读取：里程碑、议题（含已关闭）、标签、远端封板标签、各报告与发布文案文件在标签处是否存在、仓库 About 与 topics。"""
    state: dict[str, Any] = {
        "repo": cfg.repo,
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "milestones": gh.paged("/milestones?state=all"),
        "issues": [i for i in gh.paged("/issues?state=all") if "pull_request" not in i],
        "labels": [{"name": lb["name"]} for lb in gh.paged("/labels")],
    }
    status, ref = gh.request("GET", f"/git/ref/tags/{urllib.parse.quote(cfg.tag)}", ok=(200, 404))
    exists = status == 200
    state["tag"] = {"exists": exists, "detail": (ref.get("object", {}).get("sha", "")[:12] if exists and isinstance(ref, dict)
                                                 else "远端不存在")}
    reports = [*(p for p, _ in STAGE_REPORTS.values()), SUMMARY_REPORT[0], *(p for v in WP_EXTRA_REPORTS.values() for p, _ in v)]
    at: dict[str, bool | None] = {}
    for p in reports:
        if not exists:
            at[p] = False
            continue
        st, _ = gh.request("GET", f"/contents/{urllib.parse.quote(p)}?ref={urllib.parse.quote(cfg.tag)}", ok=(200, 404))
        at[p] = st == 200
    state["reports_at_tag"] = at
    if exists:
        st, _ = gh.request("GET", f"/contents/{urllib.parse.quote(PREPARED_COPY)}?ref={urllib.parse.quote(cfg.tag)}",
                           ok=(200, 404))
        state["prepared_at_tag"] = st == 200
    else:
        state["prepared_at_tag"] = None
    _, repo = gh.request("GET", "")
    state["repo_meta"] = {"description": repo.get("description"), "topics": list(repo.get("topics") or [])}
    # 只保留计划所需字段，快照不含用户资料以外的冗余信息
    keep_i = ("number", "title", "state", "body", "labels", "milestone", "pull_request")
    state["issues"] = [{k: i.get(k) for k in keep_i if k in i} for i in state["issues"]]
    for i in state["issues"]:
        i["labels"] = [{"name": n} for n in _label_names(i)]
        if i.get("milestone"):
            i["milestone"] = {"number": i["milestone"]["number"], "title": i["milestone"].get("title", "")}
    keep_m = ("number", "title", "state", "description", "due_on", "open_issues", "closed_issues")
    state["milestones"] = [{k: m.get(k) for k in keep_m} for m in state["milestones"]]
    return state


def apply_plan(gh: GitHub, plan: Plan, *, log: Callable[[str], None] = print) -> int:
    """按序执行；新建里程碑后以其编号替换占位 `{D3X}`（`milestone` 字段为整数，正文中为链接文本）；留言前检查同标记留言（幂等）。返回执行的写请求数。"""
    if not plan.ready:
        raise SealError("计划未就绪：前置检查未全部通过或存在阻塞项")
    d3x: int | None = None
    done = 0
    for op in plan.ops:
        raw = json.dumps(op.payload, ensure_ascii=False)
        if D3X_PLACEHOLDER in raw:
            if d3x is None:
                raise SealError("D3.x 里程碑尚未创建")
            payload = json.loads(raw.replace(f'"{D3X_PLACEHOLDER}"', str(d3x)).replace(D3X_PLACEHOLDER, str(d3x)))
        else:
            payload = json.loads(raw)
        if op.kind == "comment" and op.marker:
            n = op.path.split("/")[2]
            existing = gh.paged(f"/issues/{n}/comments")
            if any(op.marker in (c.get("body") or "") for c in existing):
                log(f"[{op.seq}] 跳过 {op.target} 留言：已存在标记 {op.marker}")
                continue
        status, res = gh.request(op.method, op.path, payload)
        if op.kind == "create_milestone":
            d3x = int(res["number"])
            log(f"[{op.seq}] {op.summary} → #{d3x}（{status}）")
        else:
            log(f"[{op.seq}] {op.kind} {op.target}（{status}）")
        done += 1
    return done


# ---------------------------------------------------------------- 命令行


def _cfg(a: argparse.Namespace) -> Config:
    if a.seal_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.seal_date):
        raise SealError(f"--seal-date 须为 YYYY-MM-DD：{a.seal_date}")
    return Config(repo=a.repo, tag=a.tag, d2_tag=a.d2_tag, seal_date=a.seal_date or "", d3_branch=a.d3_branch)


def _write(path: str | None, text: str) -> None:
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None, *, transport: Transport | None = None) -> int:
    ap = argparse.ArgumentParser(description="D3 封板的 GitHub 议题与里程碑处置（缺省演练）")
    ap.add_argument("action", nargs="?", default="plan", choices=("plan", "snapshot", "apply"))
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--tag", default=TAG)
    ap.add_argument("--d2-tag", default=D2_TAG)
    ap.add_argument("--d3-branch", default="d3-dev")
    ap.add_argument("--seal-date", default="", help="封板日期 YYYY-MM-DD（UTC），缺省为当天")
    ap.add_argument("--docs05", default=str(DOCS05))
    ap.add_argument("--root", default=str(ROOT), help="核对报告文件的工作树根（缺省本仓库）")
    ap.add_argument("--no-metadata", action="store_true", help="不处置仓库 About 与 topics")
    ap.add_argument("--state-file", help="plan：离线快照（snapshot 的输出）")
    ap.add_argument("--out", help="snapshot：输出路径")
    ap.add_argument("--md-out")
    ap.add_argument("--json-out")
    ap.add_argument("--full", action="store_true", help="清单中列出全部留言正文")
    ap.add_argument("--confirm", default="", help="apply：须与 --tag 相同")
    ap.add_argument("--auth", action="store_true", help="plan、snapshot：带令牌读取（缺省匿名只读）")
    a = ap.parse_args(argv)
    try:
        cfg = _cfg(a)
        model = load_docs(Path(a.docs05))
        meta = None if a.no_metadata else load_repo_meta()
        if a.action == "snapshot":
            if not a.out:
                raise SealError("snapshot 须指定 --out")
            state = fetch_state(GitHub(cfg.repo, load_token() if a.auth else None, transport), cfg)
            _write(a.out, json.dumps(state, ensure_ascii=False, indent=1) + "\n")
            print(f"已写入 {a.out}：里程碑 {len(state['milestones'])}，议题 {len(state['issues'])}")
            return 0
        if a.action == "apply":
            if a.state_file:
                raise SealError("apply 只使用在线状态，不接受 --state-file")
            if a.confirm != cfg.tag:
                raise SealError(f"apply 须以 --confirm {cfg.tag} 确认（H14 用户授权后执行）")
            token = load_token()
            if not token:
                raise SealError("未找到 GitHub 令牌（GITHUB_TOKEN、GH_TOKEN 或 ~/.git-credentials）")
            gh = GitHub(cfg.repo, token, transport)
            plan = build_plan(model, fetch_state(gh, cfg), cfg, source="live", local_root=Path(a.root), meta=meta)
            _write(a.md_out, render_markdown(plan, full=a.full))
            _write(a.json_out, json.dumps(plan.to_json(), ensure_ascii=False, indent=1) + "\n")
            n = apply_plan(gh, plan)
            print(f"完成：写请求 {n} 个")
            return 0
        if a.state_file:
            state, source = json.loads(Path(a.state_file).read_text(encoding="utf-8")), a.state_file
        else:
            state, source = fetch_state(GitHub(cfg.repo, load_token() if a.auth else None, transport), cfg), "live（只读）"
        plan = build_plan(model, state, cfg, source=source, local_root=Path(a.root), meta=meta)
        md = render_markdown(plan, full=a.full)
        _write(a.md_out, md)
        _write(a.json_out, json.dumps(plan.to_json(), ensure_ascii=False, indent=1) + "\n")
        print(md)
        return 0
    except SealError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
