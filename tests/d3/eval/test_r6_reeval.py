"""`r6_ec.py reeval`（K07）：以当前评测器重评已有的完整运行目录（不重跑仿真），用于 AO 关键时刻修正后重评 A10 的 ref6（D3-AC-18）。

核对：新 evaluation.json 与直接调用 `evaluate_run` 逐字节一致；`reeval.json` 记录新旧评测器版本与 EC、AO 分量；源运行目录不被修改；
运行清单为开发档（kinematic）时世界查询按非正式运行打开。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _r6():
    sys.path.insert(0, str(ROOT / "tools" / "sdk" / "anet4d" / "src"))
    spec = importlib.util.spec_from_file_location("r6_ec_reeval", ROOT / "tools" / "bench" / "d3" / "r6_ec.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["r6_ec_reeval"] = mod                     # 进程池按模块名序列化工作函数
    spec.loader.exec_module(mod)
    return mod


def test_reeval_rewrites_evaluations_without_touching_runs(synth_event, tmp_path: Path) -> None:
    from anet4d.baselines import BASELINES
    from anet4d.runner import run_episode

    from awr.agentsdk.env.core import BenchEnv, _open_world
    from awr.agentsdk.env.spec import EnvSpec, FleetEntry
    from awr.bench.eval.evaluator import evaluate_run
    from awr.bench.eval.thresholds import EVALUATOR_VERSION

    r6 = _r6()
    w, ev = synth_event
    src = tmp_path / "old"
    rd = src / "runs" / "lawn3_cover-s1"
    spec = EnvSpec("rv-1", 1, w, ev, (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0))),
                   physics="kinematic", duration_s=40.0, out_dir=rd)
    run_episode(BenchEnv(spec), lambda _u: BASELINES["track_share"]())
    old = evaluate_run(rd, ev, _open_world(w))
    old["evaluator_version"] = "0.1.0"
    (rd / "evaluation.json").write_text(json.dumps(old, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    before = {p.relative_to(rd).as_posix(): p.stat().st_mtime_ns for p in rd.rglob("*") if p.is_file()}
    base = tmp_path / "base.run_spec.json"
    base.write_text(json.dumps({"world": {"id": w.name}}))
    cfg = {"base_run_spec": str(base), "event": {"id": ev.parent.name, "version": 1}, "rows": ["lawn3_cover", "hover1_plaza"], "seeds": [1]}
    out = tmp_path / "new"
    doc = r6.reeval(cfg, src, out, events_root=ev.parent.parent, worlds_root=w.parent, jobs=1)
    assert doc["failures"] == [] and [r["run"] for r in doc["runs"]] == ["lawn3_cover-s1"]       # 缺运行目录的行跳过（report 记为缺运行）
    r = doc["runs"][0]
    assert r["formal"] is False and r["old"]["evaluator_version"] == "0.1.0" and r["new"]["evaluator_version"] == EVALUATOR_VERSION
    got = (out / "runs" / "lawn3_cover-s1" / "evaluation.json").read_text(encoding="utf-8")
    assert got == json.dumps(evaluate_run(rd, ev, _open_world(w)), ensure_ascii=False, indent=1) + "\n"
    assert json.loads((out / "runs" / "lawn3_cover-s1" / "manifest.json").read_text()) == json.loads((rd / "manifest.json").read_text())
    assert {p.relative_to(rd).as_posix(): p.stat().st_mtime_ns for p in rd.rglob("*") if p.is_file()} == before
    assert json.loads((out / "reeval.json").read_text())["schema"] == "drone4d.bench.r6_ec_reeval.v1"
