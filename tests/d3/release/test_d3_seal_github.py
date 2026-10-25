"""D3 封板的 GitHub 议题与里程碑处置脚本 `tools/release/d3_seal_github.py`（Z03；ADR-194 决策 (2)(5) ⑧；D3-AC-01 封板收尾）。

1. docs/05 解析：§12.1 的 42 个工作包按阶段分为封板 25 个、顺延 17 个，人周 105.3 与 50.75；§23.3 的 27 条顺延验收（阻塞 13、
   应交付 14；D3.x 22、D4 候选 5）、括注中的部分份额（WP-D3-37 随 D3-AC-68 顺延）与 ADR-219 子条款；一致性检查全部通过。
2. 计划：以 2026-10-22 仓库结构（里程碑 #1–#7、工作包议题 #17–#58、D2 议题与 PR）生成 96 项动作：标签与 D3.x 里程碑先建，
   顺延 17 个（留言 + 转入，3 个加 D4-candidate），关闭 25 个（留言 + 关闭），里程碑 #2–#7 关闭，#1 改写置顶路线图；不触碰 D2 议题。
3. 正文：留言带幂等标记与指向 `v0.3.0` 的报告链接；顺延留言列出验收与去向；#1 正文含封板报告、D3.x 顺延与 D4 入口；不含 D4 的
   禁用称谓（CL-ALL-03）与 emoji。
4. 阻塞与检查：里程碑中有未映射的打开议题时不关闭并列为阻塞；离线快照无标签信息时检查为"未判定"，计划不就绪。
5. 执行（假 GitHub）：前置检查通过时 `apply` 按序写入，占位 `{D3X}` 替换为新里程碑编号（字段为整数、正文为链接），已有标记留言跳过；
   执行后重新生成计划为 0 项（幂等）；令牌不出现在输出与计划中；`--confirm` 不符、离线快照、计划未就绪时拒绝执行。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("d3_seal_github", REPO / "tools/release/d3_seal_github.py")
sg = importlib.util.module_from_spec(_spec)
sys.modules["d3_seal_github"] = sg
_spec.loader.exec_module(sg)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
CFG = sg.Config(seal_date="2026-10-30")
TOKEN = "TESTTOKEN_never_printed_0123456789"
MS_TITLES = {
    1: "V0.2.x · Maintenance（D2 维护）",
    2: "D3 P-1 · Prerequisites（前置）",
    3: "D3 P0a · Minimal benchmark loop（最小基准闭环）",
    4: "D3 P0b · Benchmark v1（基准 v1）",
    5: "D3 P1 · Editor, LLM command and private maps（编辑器、大模型指挥层、私有地图）",
    6: "D3 P2 · SITL, interactive mode and calibration（SITL、交互模式、标定）",
    7: "D3 P3 · 3DGS appearance layer and datasets（3DGS 外观层与数据集）",
}
PHASE_MS = {"P-1": 2, "P0a": 3, "P0b": 4, "P1": 5, "P2": 6, "P3": 7}
# 2026-10-22 仓库中工作包议题的编号（按 §12.1 行序创建，P1 至 P3 依阶段分组）
WP_ISSUE = {
    "WP-D3-00": 17, "WP-D3-01": 18, "WP-D3-02": 19, "WP-D3-03": 20, "WP-D3-13": 21, "WP-D3-04": 22, "WP-D3-05": 23,
    "WP-D3-06": 24, "WP-D3-07": 25, "WP-D3-08": 26, "WP-D3-09": 27, "WP-D3-30": 28, "WP-D3-10": 29, "WP-D3-11": 30,
    "WP-D3-12": 31, "WP-D3-20": 32, "WP-D3-25": 33, "WP-D3-26": 34, "WP-D3-27": 35, "WP-D3-31": 36, "WP-D3-32": 37,
    "WP-D3-33": 38, "WP-D3-34": 39, "WP-D3-35": 40, "WP-D3-37": 41, "WP-D3-14": 42, "WP-D3-15": 43, "WP-D3-16": 44,
    "WP-D3-17": 45, "WP-D3-18": 46, "WP-D3-19": 47, "WP-D3-24": 48, "WP-D3-36": 49, "WP-D3-21": 50, "WP-D3-28": 51,
    "WP-D3-22": 52, "WP-D3-23": 53, "WP-D3-29": 54, "WP-D3-39": 55, "WP-D3-40": 56, "WP-D3-41": 57, "WP-D3-42": 58,
}
REPORTS = [*(p for p, _ in sg.STAGE_REPORTS.values()), sg.SUMMARY_REPORT[0],
           *(p for v in sg.WP_EXTRA_REPORTS.values() for p, _ in v)]
FORBIDDEN_D4 = re.compile(r"(?:ANet4D|Drone4D)\s*2(\.0)?\b|(?:ANet4D|Drone4D)\s*v2\b|2\.0\s*版|二代|新一代\s*(?:ANet4D|Drone4D)|D3\.5|D3\s*Plus", re.I)
EMOJI = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf]")


@pytest.fixture(scope="module")
def model():
    return sg.load_docs()


def _state(model, *, tag: bool | None = True, reports: bool | None = True) -> dict:
    ms = [{"number": n, "title": t, "state": "open", "description": f"{t} 说明。", "due_on": None} for n, t in MS_TITLES.items()]
    issues = [{"number": 1, "title": "Roadmap · V0.3 D3 Event Lab 与 V0.2.x 维护", "state": "open", "body": "旧正文",
               "labels": [{"name": "D3"}, {"name": "D2"}], "milestone": None}]
    issues += [{"number": n, "title": f"D2 已知问题 {n}", "state": "open" if n % 2 else "closed", "body": "",
                "labels": [{"name": "D2"}], "milestone": {"number": 1, "title": MS_TITLES[1]}} for n in range(2, 17)]
    for wp, n in WP_ISSUE.items():
        w = model.wps[wp]
        m = PHASE_MS[w.phases[0]]
        issues.append({"number": n, "title": f"{wp} · {w.name}", "state": "open", "body": "范围",
                       "labels": [{"name": "D3"}, {"name": "work-package"}], "milestone": {"number": m, "title": MS_TITLES[m]}})
    issues.append({"number": 61, "title": "feat(events): stage 1", "state": "closed", "body": "", "labels": [],
                   "milestone": None, "pull_request": {"url": "x"}})
    st = {"milestones": ms, "issues": issues, "labels": [{"name": n} for n in ("D2", "D3", "work-package", "M25")]}
    st["tag"] = {"exists": tag, "detail": "abc123" if tag else "远端不存在"} if tag is not None else {}
    st["reports_at_tag"] = {p: reports for p in REPORTS} if reports is not None else {}
    st["prepared_at_tag"] = False if tag else None
    return st


@pytest.fixture
def local_root(tmp_path):
    for p in REPORTS:
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# 报告\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- 1. 解析


def test_parse_work_packages_and_scope(model):
    seal = [w for w in model.wps.values() if w.scope == "seal"]
    defer = [w for w in model.wps.values() if w.scope == "defer"]
    assert len(model.wps) == 42 and len(seal) == 25 and len(defer) == 17
    assert set(model.wps) == set(WP_ISSUE)
    assert model.wps["WP-D3-01"].phases == ("P-1", "P0a")
    assert model.wps["WP-D3-06"].phases == ("P0a", "P0b") and model.wps["WP-D3-06"].total_pw == 7.5
    assert model.wps["WP-D3-36"].phases == ("P2",)
    assert round(sum(w.total_pw for w in seal), 2) == 105.3 and round(sum(w.total_pw for w in defer), 2) == 50.75


def test_parse_deferred_list(model):
    d = model.deferred
    assert len(d) == 27 and sum(a.blocking for a in d) == 13
    assert sorted(a.ac for a in d if a.d4_candidate) == ["D3-AC-27", "D3-AC-28", "D3-AC-30", "D3-AC-42", "D3-AC-65"]
    ac68 = next(a for a in d if a.ac == "D3-AC-68")
    assert ac68.wps == ("WP-D3-42",) and ac68.noted_wps == ("WP-D3-37",) and "gs_eval" in ac68.wp_note
    assert [a.ac for a in model.shares_of("WP-D3-37")] == ["D3-AC-68"]
    assert [a.ac for a in model.acs_of("WP-D3-15")] == ["D3-AC-27", "D3-AC-28", "D3-AC-65"]
    assert {s.ac.split("（")[0] for s in model.subclauses} == {"D3-AC-25", "D3-AC-21"}
    assert all(c.ok for c in sg._consistency_checks(model))


def test_consistency_check_flags_mismatch(model):
    bad = sg.DocsModel(dict(model.wps), model.deferred[:-1], model.subclauses)
    checks = {c.name: c.ok for c in sg._consistency_checks(bad)}
    assert checks["§23.3 顺延验收计数"] is False and checks["§23.3 推荐去向计数"] is False


def test_missing_section_raises():
    with pytest.raises(sg.SealError, match=r"12\.1"):
        sg.parse_work_packages("# 标题\n\n### 12.2 路径\n")


# ---------------------------------------------------------------- 2. 计划


def test_plan_ops_on_repository_layout(model, local_root):
    plan = sg.build_plan(model, _state(model), CFG, local_root=local_root, now=NOW)
    kinds = [o.kind for o in plan.ops]
    assert plan.ready and not plan.warnings and not plan.blocked
    assert len(plan.ops) == 96
    assert kinds.count("create_label") == 1 and kinds.count("create_milestone") == 1
    assert kinds.count("move_milestone") == 17 and kinds.count("close_issue") == 25 and kinds.count("comment") == 42
    assert kinds.count("close_milestone") == 6 and kinds.count("update_issue") == 1
    assert kinds[:2] == ["create_label", "create_milestone"] and kinds[-1] == "update_issue"
    assert max(i for i, k in enumerate(kinds) if k in ("close_issue", "move_milestone")) < kinds.index("close_milestone")
    assert {o.target for o in plan.ops if o.kind == "add_label"} == {"#43", "#44", "#53"}
    assert {o.target for o in plan.ops if o.kind == "close_milestone"} == {f"milestone #{n}" for n in range(2, 8)}
    closed = {int(o.target[1:]) for o in plan.ops if o.kind == "close_issue"}
    moved = {int(o.target[1:]) for o in plan.ops if o.kind == "move_milestone"}
    assert closed == set(range(17, 42)) and moved == set(range(42, 59))
    assert all(o.payload == {"milestone": "{D3X}"} for o in plan.ops if o.kind == "move_milestone")
    touched = {o.target for o in plan.ops}
    assert not any(f"#{n}" in touched for n in range(2, 17)), "D2 维护议题不应被处置"
    assert plan.stats["d4_candidates"] == ["WP-D3-15", "WP-D3-16", "WP-D3-23"]


def test_comment_bodies(model, local_root):
    plan = sg.build_plan(model, _state(model), CFG, local_root=local_root, now=NOW)
    body = {o.target: o.payload["body"] for o in plan.ops if o.kind == "comment"}
    b24 = body["#24"]
    assert b24.startswith("<!-- d3-seal:WP-D3-06 -->") and "完成并关闭" in b24 and "2026-10-30" in b24
    assert "/blob/v0.3.0/docs/impl/D3-P0a-%E5%AE%9E%E7%8E%B0" in b24 and "D3-P0b-" in b24 and "D3-%E4%BA%A4%E4%BB%98" in b24
    assert "D3-%E6%96%87%E6%A1%88%E8%AF%84%E5%AE%A1" in body["#21"]  # WP-D3-13 附文案评审记录
    assert "D3-AC-68" in body["#41"] and "gs_eval" in body["#41"]  # WP-D3-37 的顺延份额
    b43 = body["#43"]
    assert "顺延至 D3.x" in b43 and "D4-candidate" in b43 and "| D3-AC-65 大模型指挥层与 ANet 委派 | P1 | 应交付 |" in b43
    assert "milestone/{D3X}" in b43
    assert "D4-candidate" not in body["#42"]


def test_roadmap_body(model, local_root):
    plan = sg.build_plan(model, _state(model), CFG, local_root=local_root, now=NOW)
    op = next(o for o in plan.ops if o.kind == "update_issue")
    b = op.payload["body"]
    assert op.payload["title"] == sg.ROADMAP_TITLE and op.path == "/issues/1"
    for s in ("### D3 封板（V0.3，`v0.3.0`）", "### D3.x 顺延", "### D4 入口", "41 条验收", "G0", "V0.2.x · Maintenance",
              "| #43 WP-D3-15 链路、私有地图与大模型指挥层 | P1 | AC-27、AC-28、AC-65 | D4 候选 |", "D3-AC-25（首帧两项）",
              "docs/06-D4-", "`v0.2.2`", "milestone/{D3X}"):
        assert s in b, s
    assert b.index("milestone/2") < b.index("milestone/7")


def test_generated_copy_rules(model, local_root):
    plan = sg.build_plan(model, _state(model), CFG, local_root=local_root, now=NOW)
    texts = [o.payload.get("body") or o.payload.get("description") or o.payload.get("title") or "" for o in plan.ops]
    texts += [sg.ROADMAP_TITLE, sg.D3X_TITLE, sg.D4_LABEL_DESC]
    for t in texts:
        assert not FORBIDDEN_D4.search(t), t[:80]
        assert not EMOJI.search(t)
    desc = next(o.payload["description"] for o in plan.ops if o.kind == "create_milestone")
    assert "27 条" in desc and "50.75" in desc and len(sg.D4_LABEL_DESC) <= 100


def test_existing_d3x_milestone_and_partial_progress(model, local_root):
    st = _state(model)
    st["milestones"].append({"number": 8, "title": sg.D3X_TITLE, "state": "open", "description": ""})
    st["labels"].append({"name": sg.D4_LABEL})
    for it in st["issues"]:
        if it["number"] == 43:  # 已转入但缺标签
            it["milestone"] = {"number": 8}
        if it["number"] == 17:
            it["state"] = "closed"
    plan = sg.build_plan(model, st, CFG, local_root=local_root, now=NOW)
    kinds = [o.kind for o in plan.ops]
    assert "create_milestone" not in kinds and "create_label" not in kinds
    assert [o.target for o in plan.ops if o.target == "#43"] == ["#43"]  # 只补标签
    assert all(o.payload["milestone"] == 8 for o in plan.ops if o.kind == "move_milestone")
    assert "#17" not in {o.target for o in plan.ops}
    assert "milestone/8" in next(o for o in plan.ops if o.kind == "update_issue").payload["body"]
    assert plan.stats["wp_already_done"] == 2


# ---------------------------------------------------------------- 4. 阻塞与检查


def test_unmapped_issue_blocks_milestone(model, local_root):
    st = _state(model)
    st["issues"].append({"number": 70, "title": "P0a 补测记录", "state": "open", "body": "", "labels": [],
                         "milestone": {"number": 3}})
    st["issues"].append({"number": 71, "title": "WP-D3-99 · 未登记", "state": "open", "body": "", "labels": [],
                         "milestone": {"number": 3}})
    plan = sg.build_plan(model, st, CFG, local_root=local_root, now=NOW)
    assert "milestone #3" not in {o.target for o in plan.ops if o.kind == "close_milestone"}
    assert any("#3" in b and "#70" in b and "#71" in b for b in plan.blocked)
    assert any("WP-D3-99" in w for w in plan.warnings)
    assert not plan.ready
    assert "打开（存在未处置议题）" in next(o for o in plan.ops if o.kind == "update_issue").payload["body"]


def test_offline_snapshot_checks_not_ready(model, tmp_path):
    plan = sg.build_plan(model, _state(model, tag=None, reports=None), CFG, local_root=tmp_path, now=NOW)
    checks = {c.name: c.ok for c in plan.checks}
    assert checks["远端标签 v0.3.0 存在"] is None and checks["报告在 v0.3.0 处存在"] is None
    assert checks["报告在工作树中存在"] is False and not plan.ready
    md = sg.render_markdown(plan)
    assert "（演练）" in md and "| 远端标签 v0.3.0 存在 | 未判定 |" in md and "未写入 GitHub" in md
    plan2 = sg.build_plan(model, _state(model, tag=False, reports=False), CFG, local_root=tmp_path, now=NOW)
    assert {c.name: c.ok for c in plan2.checks}["报告在 v0.3.0 处存在"] is False


def test_warnings_for_mismatch(model, local_root):
    st = _state(model)
    st["issues"] = [i for i in st["issues"] if i["number"] != 30]  # WP-D3-11 无议题
    for it in st["issues"]:
        if it["number"] == 24:
            it["milestone"] = {"number": 4}
    plan = sg.build_plan(model, st, CFG, local_root=local_root, now=NOW)
    assert any("WP-D3-11 没有对应议题" in w for w in plan.warnings)
    assert any("WP-D3-06（#24）挂在里程碑 #4" in w for w in plan.warnings)


# ---------------------------------------------------------------- 5. 执行（假 GitHub）


class FakeGitHub:
    """内存中的 GitHub REST 子集：读取分页、标签与内容存在性，写入标签、里程碑、留言与议题字段。"""

    def __init__(self, state: dict, *, tag: bool = True):
        self.ms = copy.deepcopy(state["milestones"])
        self.issues = {i["number"]: copy.deepcopy(i) for i in state["issues"]}
        self.labels = [dict(lb) for lb in state["labels"]]
        self.comments: dict[int, list[dict]] = {}
        self.tag = tag
        self.prepared = False  # copy.prepared.json 是否仍在标签处
        self.repo = {"description": "Real-world 4D simulation for multi-agent drones: point-cloud digital twins, fleet "
                                    "simulation and an interactive sandbox.",
                     "topics": ["anet", "digital-twin", "drone-simulation", "multi-agent", "point-cloud", "px4",
                                "simulation", "swarm-robotics", "threejs", "uav", "urbanscene3d", "webgl"]}
        self.writes: list[tuple[str, str, dict]] = []
        self.auth: list[str | None] = []

    def __call__(self, method, url, headers, data):
        self.auth.append(headers.get("Authorization"))
        u = urlsplit(url)
        path = unquote(u.path).removeprefix(f"/repos/{sg.REPO}")
        qs = parse_qs(u.query)
        body = json.loads(data) if data else None
        page = int(qs.get("page", ["1"])[0])

        def ok(obj, status=200):
            return status, json.dumps(obj, ensure_ascii=False).encode()

        def paged(items):
            return ok(items[(page - 1) * 100 : page * 100])

        if method == "GET":
            if path == "/milestones":
                return paged(self.ms)
            if path == "/issues":
                return paged(sorted(self.issues.values(), key=lambda i: -i["number"]))
            if path == "/labels":
                return paged(self.labels)
            if path.startswith("/git/ref/tags/"):
                return ok({"object": {"sha": "f" * 40}}) if self.tag else ok({"message": "Not Found"}, 404)
            if path.startswith("/contents/"):
                found = self.tag and (path != f"/contents/{sg.PREPARED_COPY}" or self.prepared)
                return ok({"name": path}) if found else ok({"message": "Not Found"}, 404)
            if path == "":
                return ok(dict(self.repo))
            m = re.fullmatch(r"/issues/(\d+)/comments", path)
            if m:
                return paged(self.comments.get(int(m.group(1)), []))
            return ok({"message": "Not Found"}, 404)
        assert headers.get("Authorization") == f"Bearer {TOKEN}"
        self.writes.append((method, path, body))
        if method == "POST" and path == "/labels":
            self.labels.append({"name": body["name"]})
            return ok(body, 201)
        if method == "POST" and path == "/milestones":
            n = max(m["number"] for m in self.ms) + 1
            self.ms.append({"number": n, **body})
            return ok({"number": n}, 201)
        if method == "PATCH" and path == "":
            self.repo.update(body)
            return ok(self.repo)
        if method == "PUT" and path == "/topics":
            self.repo["topics"] = list(body["names"])
            return ok({"names": body["names"]})
        m = re.fullmatch(r"/issues/(\d+)(/comments|/labels)?", path)
        if m:
            it = self.issues[int(m.group(1))]
            if m.group(2) == "/comments":
                self.comments.setdefault(it["number"], []).append({"body": body["body"]})
                return ok({"id": 1}, 201)
            if m.group(2) == "/labels":
                it["labels"] = [*it["labels"], *({"name": n} for n in body["labels"])]
                return ok(it["labels"])
            assert method == "PATCH"
            if "milestone" in body:
                assert isinstance(body["milestone"], int)
                it["milestone"] = {"number": body["milestone"]}
            it.update({k: v for k, v in body.items() if k != "milestone"})
            return ok(it)
        m = re.fullmatch(r"/milestones/(\d+)", path)
        if m and method == "PATCH":
            ms = next(x for x in self.ms if x["number"] == int(m.group(1)))
            ms.update(body)
            return ok(ms)
        return ok({"message": "unsupported"}, 422)


def _live_plan(model, fake, local_root):
    gh = sg.GitHub(sg.REPO, TOKEN, fake)
    return gh, sg.build_plan(model, sg.fetch_state(gh, CFG), CFG, local_root=local_root, now=NOW)


def test_apply_with_fake_github_is_idempotent(model, local_root):
    fake = FakeGitHub(_state(model))
    gh, plan = _live_plan(model, fake, local_root)
    assert plan.ready, [c for c in plan.checks if c.ok is not True]
    logs: list[str] = []
    n = sg.apply_plan(gh, plan, log=logs.append)
    assert n == 96 and len(fake.writes) == 96
    d3x = next(m for m in fake.ms if m["title"] == sg.D3X_TITLE)["number"]
    assert d3x == 8
    assert all(fake.issues[n]["milestone"]["number"] == 8 for n in range(42, 59))
    assert all(fake.issues[n]["state"] == "closed" and fake.issues[n]["state_reason"] == "completed" for n in range(17, 42))
    assert all(m["state"] == "closed" for m in fake.ms if 2 <= m["number"] <= 7)
    assert next(m for m in fake.ms if m["number"] == 1)["state"] == "open"
    assert "封板" in next(m for m in fake.ms if m["number"] == 3)["description"]
    road = fake.issues[1]
    assert road["title"] == sg.ROADMAP_TITLE and "milestone/8)" in road["body"] and "{D3X}" not in road["body"]
    assert "milestone/8)" in fake.comments[43][0]["body"]
    assert {lb["name"] for lb in fake.issues[53]["labels"]} >= {sg.D4_LABEL}
    assert all(fake.issues[n]["state"] == ("open" if n % 2 else "closed") for n in range(2, 17))
    # 幂等：执行后重新生成计划为 0 项
    _, again = _live_plan(model, fake, local_root)
    assert again.ops == [] and not again.blocked
    assert any("→ #8" in ln for ln in logs)


def test_apply_skips_existing_marker_comment(model, local_root):
    fake = FakeGitHub(_state(model))
    fake.comments[24] = [{"body": "<!-- d3-seal:WP-D3-06 -->\n先前的留言"}]
    gh, plan = _live_plan(model, fake, local_root)
    logs: list[str] = []
    sg.apply_plan(gh, plan, log=logs.append)
    assert len(fake.comments[24]) == 1 and fake.issues[24]["state"] == "closed"
    assert any("跳过 #24" in ln for ln in logs)


def test_apply_refuses_when_not_ready(model, local_root):
    fake = FakeGitHub(_state(model), tag=False)
    gh, plan = _live_plan(model, fake, local_root)
    assert not plan.ready
    with pytest.raises(sg.SealError, match="未就绪"):
        sg.apply_plan(gh, plan)
    assert fake.writes == []


def test_cli_apply_guards_and_token_hygiene(model, local_root, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    fake = FakeGitHub(_state(model))
    assert sg.main(["apply", "--confirm", "v0.3.1"], transport=fake) == 1
    assert sg.main(["apply", "--confirm", "v0.3.0", "--state-file", str(tmp_path / "x.json")], transport=fake) == 1
    assert fake.writes == []
    js = tmp_path / "plan.json"
    rc = sg.main(["apply", "--confirm", "v0.3.0", "--seal-date", "2026-10-30", "--root", str(local_root),
                  "--json-out", str(js)], transport=fake)
    out = capsys.readouterr()
    assert rc == 0 and len(fake.writes) == 98 and "写请求 98 个" in out.out
    assert fake.repo["description"].startswith("Real-world") and "Event Lab" in fake.repo["description"]
    assert {"benchmark", "gazebo", "lidar", "rerun"} <= set(fake.repo["topics"]) and len(fake.repo["topics"]) == 16
    assert TOKEN not in out.out + out.err and TOKEN not in js.read_text(encoding="utf-8")
    # plan 缺省匿名读取
    fake2 = FakeGitHub(_state(model))
    assert sg.main(["plan", "--root", str(local_root)], transport=fake2) == 0
    assert set(fake2.auth) == {None}
    capsys.readouterr()


def test_cli_plan_from_snapshot(model, local_root, tmp_path, capsys):
    snap = tmp_path / "state.json"
    fake = FakeGitHub(_state(model), tag=False)
    assert sg.main(["snapshot", "--out", str(snap)], transport=fake) == 0
    assert fake.writes == [] and set(fake.auth) == {None}
    md, js = tmp_path / "plan.md", tmp_path / "plan.json"
    rc = sg.main(["plan", "--state-file", str(snap), "--seal-date", "2026-10-30", "--root", str(local_root),
                  "--md-out", str(md), "--json-out", str(js), "--full"])
    assert rc == 0
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["schema"] == sg.PLAN_SCHEMA and data["ready"] is False and len(data["ops"]) == 98
    text = md.read_text(encoding="utf-8")
    assert text.count("````markdown") == 42 + 4  # --full：全部留言、新里程碑说明、#1 正文、About 与 topics
    assert "| 远端标签 v0.3.0 存在 | 未通过 | 远端不存在 |" in text
    assert sg.main(["plan", "--state-file", str(snap), "--seal-date", "30-10-2026"]) == 1
    capsys.readouterr()


def test_load_token_from_git_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sg.load_token() is None
    (tmp_path / ".git-credentials").write_text("https://u:other@gitlab.com\nhttps://inksong:abc%2Fdef@github.com\n",
                                               encoding="utf-8")
    assert sg.load_token() == "abc/def"
    monkeypatch.setenv("GH_TOKEN", "env-token")
    assert sg.load_token() == "env-token"


# ---------------------------------------------------------------- 6. 仓库 About、topics 与发布文案（C-51、W-14）


def test_repo_meta_from_records():
    meta = sg.load_repo_meta()
    assert meta.about.startswith("Real-world 4D simulation") and "Event Lab" in meta.about and len(meta.about) <= 120
    assert len(meta.topics) == 16 and meta.topics[:2] == ("drone-simulation", "multi-agent")
    assert meta.topics[-4:] == ("benchmark", "gazebo", "lidar", "rerun")
    assert not FORBIDDEN_D4.search(meta.about)


def test_meta_ops_and_checks(model, local_root):
    meta = sg.RepoMeta("Real-world 4D simulation.", ("uav", "lidar"))
    st = _state(model)
    st["prepared_at_tag"] = False
    st["repo_meta"] = {"description": "old", "topics": ["uav", "webgl"]}
    plan = sg.build_plan(model, st, CFG, local_root=local_root, now=NOW, meta=meta)
    ops = {o.kind: o for o in plan.ops if o.target == "repository"}
    assert ops["update_repo"].payload == {"description": meta.about} and ops["update_repo"].path == ""
    assert ops["replace_topics"].payload == {"names": ["uav", "lidar"]} and "新增 `lidar`" in ops["replace_topics"].summary
    assert "移除 `webgl`" in ops["replace_topics"].summary and plan.ready
    st["repo_meta"] = {"description": meta.about, "topics": ["lidar", "uav"]}
    assert not [o for o in sg.build_plan(model, st, CFG, local_root=local_root, now=NOW, meta=meta).ops
                if o.target == "repository"]
    bad = sg.build_plan(model, st, CFG, local_root=local_root, now=NOW, meta=sg.RepoMeta("x" * 121, ("Bad Topic",)))
    assert {c.name: c.ok for c in bad.checks}["About 与 topics 符合规范"] is False and not bad.ready


def test_prepared_copy_must_be_applied(model, local_root):
    st = _state(model)
    st["prepared_at_tag"] = False
    assert sg.build_plan(model, st, CFG, local_root=local_root, now=NOW).ready
    (local_root / sg.PREPARED_COPY).parent.mkdir(parents=True, exist_ok=True)
    (local_root / sg.PREPARED_COPY).write_text("{}", encoding="utf-8")
    plan = sg.build_plan(model, st, CFG, local_root=local_root, now=NOW)
    c = {c.name: c for c in plan.checks}["V0.3 发布文案已应用"]
    assert c.ok is False and "未应用" in c.detail and not plan.ready
    (local_root / sg.PREPARED_COPY).unlink()
    st["prepared_at_tag"] = True
    assert not sg.build_plan(model, st, CFG, local_root=local_root, now=NOW).ready
    st.pop("prepared_at_tag")
    assert {c.name: c.ok for c in sg.build_plan(model, st, CFG, local_root=local_root, now=NOW).checks}[
        "V0.3 发布文案已应用"] is None
