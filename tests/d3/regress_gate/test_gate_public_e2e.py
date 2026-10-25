"""D3 回归门（D3-AC-01）第 6 条的 D2 公开站 e2e 与 Tier S 首屏（builtin `public-e2e`，G03）：

- 配置：gate.json 登记 `public-e2e`（排在 public-build 之后），cgroup 模拟为 emax 口径（AWR-04 §13：CPUQuota 250%、MemoryHigh
  1.8G、MemoryMax 2G、4 核），TTFP 上限取 thresholds.json 的冻结值，`required` 与 spec 记录的检查名一致；
- 判据：Tier S 汇总（时间量取中位、确定量取集合或极值）与 D2 基线比较——首屏字节逐字节相同、reveal 时程序数不增加、绘制
  点数不降、TTFP 不超过冻结阈值与"基线 × 1.05 或基线 + 下限"；GL 错误、失败节点、Tier 漂移即不通过；功能检查缺项即不通过；
- 入库基线：`baseline.json` 的 `public_e2e` 节以 d2@928c57b 代码与 v0.2.1 演示构建记录，世界与 gate.json 一致；
- 门入口：产物不是公开演示构建时经 `main()` 直接判不通过（不起栈）；完整运行为 opt-in（`AWR_D3_GATE_E2E=1`，约 4 min）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "tests" / "e2e" / "d2-public-gate.spec.ts"


def _gate():
    spec = importlib.util.spec_from_file_location("regress_gate", ROOT / "tools/bench/d3/regress_gate.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _sample(world: str, k: int, **kw) -> dict:
    s = {"world": world, "repeat": k, "tier": "S", "deviceClass": "software", "ttfp": 10.0 + k, "ttfpFirstPixel": 300.0, "warmupWaitMs": 290.0,
         "tti": 15000.0, "firstScreenBytes": 321384, "programsAtReveal": 27, "compiledAfterReveal": 0, "glErrors": 0, "drawn": 20000,
         "failed": 0, "budgetViolations": 0}
    s.update(kw)
    return s


BASE = {"tier_s": {"shenzhen": {"first_screen_bytes": 321384, "ttfp_ms": 9.0, "programs_at_reveal": 27, "drawn": 20000.0}}}
CFG = {"worlds": ["shenzhen"], "repeats": 3, "ttfp_ms_max": 1000, "regress_max": 0.05, "regress_floor_ms": 50}


def test_gate_registers_public_e2e() -> None:
    m = _gate()
    g = m.load_gate()
    by_id = {s["id"]: s for s in g["steps"]}
    default = [s["id"] for s in m.select_steps(g, None)]
    assert by_id["public-e2e"]["item"] == 6 and by_id["public-e2e"]["kind"] == "builtin" and "public-e2e" in default
    assert default.index("public-build") < default.index("public-e2e")                  # 先构建再测
    c = g["public_e2e"]
    assert c["dist"] == g["k9_d3"]["dist"] == ".cache/deploy/emax/web-dist"              # make demo-build 的输出
    assert (ROOT / c["spec"]).resolve() == SPEC and SPEC.is_file()
    cg = c["cgroup"]
    assert cg["enabled"] and cg["cpu_quota_pct"] == 250 and cg["mem_high"] == "1.8G" and cg["mem_max"] == "2G" and cg["cpus"] == "0-3"
    assert c["require_cgroup"] is True and c["stack"]["show_scenario"] == "s7-synthcity-relay" and c["stack"]["ws_max_show"] == 24
    t = c["tier_s"]
    thr = json.loads((ROOT / "apps/web/perf/thresholds.json").read_text(encoding="utf-8"))["entries"]["ttfp_ms"]
    assert t["ttfp_ms_max"] == thr["value"] == 1000 and t["regress_max"] == 0.05 and t["repeats"] >= 3
    assert "synthcity" in t["worlds"] and "shenzhen" in t["worlds"]


def test_required_checks_are_recorded_by_the_spec() -> None:
    """`required` 的每一项都是 spec 中 record('<名>', …) 写出的检查名（改名时两边一起改，缺项在门上即不通过）。"""
    c = _gate().load_gate()["public_e2e"]
    names = set(re.findall(r"record\('([a-z0-9-]+)'", SPEC.read_text(encoding="utf-8")))
    assert set(c["required"]) <= names
    assert {"landing", "world-hub", "shared-show-viewer", "visitor-sandbox", "tier-s", "d3-requests"} <= set(c["required"])
    assert "lab-static" in names and "lab-static" not in c["required"]                  # /lab/ 构建存在时才要求（e2e_checks）


def test_tier_s_summary() -> None:
    m = _gate()
    ss = [_sample("shenzhen", k) for k in range(3)] + [_sample("synthcity", 0, firstScreenBytes=490260, ttfp=None)]
    s = m.tier_s_summary(ss)
    assert list(s) == ["shenzhen", "synthcity"]
    assert s["shenzhen"]["n"] == 3 and s["shenzhen"]["ttfp_ms"] == 11.0 and s["shenzhen"]["first_screen_bytes"] == [321384]
    assert s["shenzhen"]["tiers"] == ["S/software"] and s["shenzhen"]["programs_at_reveal"] == 27 and s["shenzhen"]["drawn"] == 20000.0
    assert s["synthcity"]["ttfp_ms"] is None
    assert m._median([1, 2, 3, 4]) == 2.5 and m._median([None]) is None


def test_judge_tier_s_passes_at_the_baseline_and_flags_each_regression() -> None:
    m = _gate()

    def judge(**kw) -> list[str]:
        return m.judge_tier_s(m.tier_s_summary([_sample("shenzhen", k, **kw) for k in range(3)]), BASE, CFG)

    assert judge() == []
    assert judge(ttfp=58.0) == []                                   # 基线 + 50 ms 的计时分辨率下限之内
    assert any("TTFP median" in p for p in judge(ttfp=60.0))       # > max(9 × 1.05, 9 + 50)
    assert any("> 1000 ms" in p for p in m.judge_tier_s(m.tier_s_summary([_sample("shenzhen", 0, ttfp=1200.0)]),
                                                         {"tier_s": {"shenzhen": {**BASE["tier_s"]["shenzhen"], "ttfp_ms": 1190.0}}}, CFG))
    assert any("first screen bytes" in p for p in judge(firstScreenBytes=321000))
    assert any("programs at reveal" in p for p in judge(programsAtReveal=28))
    assert any("drawn points" in p for p in judge(drawn=18000))
    assert any("tier" in p for p in judge(tier="B", deviceClass="dGPU"))
    assert any("gl_errors" in p for p in judge(glErrors=1))
    assert any("compiled_after_reveal" in p for p in judge(compiledAfterReveal=2))
    assert any("failed" in p for p in judge(failed=1))
    # 重复之间首屏字节不同、世界缺采样、缺基线
    mixed = m.tier_s_summary([_sample("shenzhen", 0), _sample("shenzhen", 1, firstScreenBytes=1)])
    assert any("first screen bytes" in p for p in m.judge_tier_s(mixed, BASE, CFG))
    assert m.judge_tier_s({}, BASE, CFG) == ["shenzhen: no Tier S sample"]
    assert m.judge_tier_s(m.tier_s_summary([_sample("shenzhen", 0)]), None, CFG) == ["shenzhen: no D2 baseline"]


def test_e2e_checks() -> None:
    m = _gate()
    c = {"required": ["landing", "tier-s"]}
    ok = {"checks": [{"test": "landing", "ok": True}, {"test": "tier-s", "ok": True}], "d3_requests": [], "lab_dist": None}
    assert m.e2e_checks(ok, c) == []
    assert m.e2e_checks({**ok, "checks": ok["checks"][:1]}, c) == ["tier-s: missing"]          # 用例被跳过也不通过
    bad = {**ok, "checks": [{"test": "landing", "ok": False, "detail": "timeout"}, ok["checks"][1]]}
    assert m.e2e_checks(bad, c) == ["landing: timeout"]
    assert m.e2e_checks({**ok, "lab_dist": "/x"}, c) == ["lab-static: missing"]                 # /lab/ 构建挂载时必须通过
    assert m.e2e_checks({**ok, "d3_requests": ["/world/synthcity -> GET /api/bench/v1/runs"]}, c)


def test_cpus_available() -> None:
    m = _gate()
    have = sorted(os.sched_getaffinity(0))
    assert m.cpus_available(f"{have[0]}-{have[0]}") == f"{have[0]}-{have[0]}"
    assert m.cpus_available(f"{have[-1] + 1}-{have[-1] + 4}") is None
    assert m.cpus_available("") is None and m.cpus_available("a-b") is None


def test_baseline_public_e2e_section_matches_gate() -> None:
    m = _gate()
    b = m.load_baseline()["public_e2e"]
    g = m.load_gate()["public_e2e"]
    assert b["conditions"]["worlds"] == g["tier_s"]["worlds"] and b["conditions"]["cgroup"] == g["cgroup"]
    assert b["source"]["tree"].startswith("d2@928c57b") and b["source"]["build_info"]["commit"].startswith("928c57b")
    assert b["source"]["cgroup_mode"] == "systemd-scope"
    assert b["tier_s"]["shenzhen"]["first_screen_bytes"] == 321_384                           # 深圳 Tier S 首屏（AWR-16 §4.11）
    for w in g["tier_s"]["worlds"]:
        t = b["tier_s"][w]
        assert t["first_screen_bytes"] > 0 and 0 < t["ttfp_ms"] <= g["tier_s"]["ttfp_ms_max"] and t["programs_at_reveal"] > 0
    assert {"digests", "resim", "public_e2e"} <= set(m.load_baseline())                      # 只改写本节，其他节保留


def test_main_rejects_a_non_demo_build_without_starting_the_stack(tmp_path) -> None:
    m = _gate()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (dist / "build-info.json").write_text('{"channel": "dev", "commit": "abc"}', encoding="utf-8")
    out = tmp_path / "out"
    assert m.main(["--steps", "public-e2e", "--public-dist", str(dist), "--out", str(out)]) == 1
    r = json.loads((out / "report.json").read_text(encoding="utf-8"))["steps"]["public-e2e"]
    assert not r["pass"] and "not a public demo build" in r["problems"][0] and "stack" not in r
    assert m.main(["--steps", "public-e2e", "--public-dist", str(tmp_path / "none"), "--out", str(out)]) == 1


@pytest.mark.skipif(os.environ.get("AWR_D3_GATE_E2E") != "1", reason="[optin] full public-e2e gate step (cgroup stack, Chrome; about 4 min): AWR_D3_GATE_E2E=1")
def test_public_e2e_gate_step_runs(tmp_path) -> None:
    m = _gate()
    g = m.load_gate()
    if not (ROOT / g["public_e2e"]["dist"] / "index.html").is_file():
        pytest.skip("[data] no public demo build (make demo-build)")
    code = m.main(["--steps", "public-e2e", "--out", str(tmp_path)])
    r = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["steps"]["public-e2e"]
    assert code == 0, r.get("problems")
    assert r["stack"]["cgroup_mode"] == "systemd-scope" and set(r["tier_s"]) == set(g["public_e2e"]["tier_s"]["worlds"])
