"""评测器 0.3.0（K16；D3-AC-43、D3-AC-57、D3-AC-62 目标搜索条款；M27-FR-114、FR-124、FR-176；B21 遗留 1、B33 遗留 3）：

- 目标搜索的计分事件取事件真值 `key_events[]` 的 `target_window_k`（评测器 0.2.0 只读 plan.json 注释，search 赛道四项恒为 0）；
- 目标搜索的 EU 按 `report_map`（`found` → `target_present`）匹配，定位误差 ≤ 5 m；
- 注释类型的事件不受影响（关键事件不进入其计分集，结果与去掉关键事件的真值逐字节一致）；
- 隐藏真值经 HiddenStore 读取并逐文件审计，越权 691，结果与直接读取逐字节一致；
- `inputs.truth_sha256` 只覆盖评测读取的运行真值：排名运行精简（删除 `truth/run_<id>.mcap`、只留 `TRUTH_MIN_FILES`）后复评逐字节一致。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from awr.agentsdk.schemas import bench_validator
from awr.bench.eval.eu import event_understanding
from awr.bench.eval.thresholds import EVALUATOR_VERSION
from awr.bench.eval.truth import (
    RUN_TRUTH_FILES,
    TruthAccess,
    eu_target_set,
    event_truth_sha256,
    scored_event_set,
    truth_digest,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "env"))

WIN_MS = 60_000


def _truth(n_ms: int, *, target: str = "person_a", incomplete: bool = False) -> dict:
    """目标搜索的事件真值（门 3 search 验证器的形状）：单个在场区间 → `target_present` 与 60 s 窗口关键事件。"""
    keys, k, a = [], 0, 0
    while a < n_ms:
        k += 1
        b = min(a + WIN_MS, n_ms)
        keys.append({"id": f"target_window_{k}", "type": "target_window", "participants": [target], "interval_ms": [a, b], "window_s": 60.0,
                     "declared": False, "ref": "target_present", "params": {"k": k}})
        a = b
    return {"events": [{"id": "target_present", "type": "target_present", "participants": [target], "interval_ms": [0, n_ms], "declared": True,
                        "salience": 1.0, "in_incomplete_region": incomplete, "evidence": {}}],
            "key_events": keys, "report_map": {"found": ["target_present"]}}


# ---------------------------------------------------------------------------------------------------------- 单元：事件集与 EU
def test_scored_event_set_adds_target_windows_only_for_scored_types() -> None:
    t = _truth(150_000)
    E, rows = scored_event_set([], t, ["target_window"])
    assert [e["id"] for e in E] == ["target_window_1", "target_window_2", "target_window_3"]
    assert [(e["a_s"], e["b_s"]) for e in E] == [(0.0, 60.0), (60.0, 120.0), (120.0, 150.0)] and all(e["key"] for e in E)
    assert [r["annotation"] for r in rows] == [e["id"] for e in E] and not any(r["in_incomplete_region"] for r in rows)
    ann = [{"id": "m", "type": "proximity_encounter", "participants": ["a", "b"], "a_s": 1.0, "b_s": 2.0, "multi": True, "params": {}}]
    t2 = {**t, "key_events": [*t["key_events"], {"id": "meet-1", "type": "proximity_encounter", "participants": ["a", "b"],
                                                  "interval_ms": [0, 1000]}]}
    assert scored_event_set(ann, t2, ["proximity_encounter"]) == (ann, [])      # 注释类型：关键事件不进入计分集（结果不变）
    assert scored_event_set(ann, None, ["proximity_encounter", "target_window"]) == (ann, [])
    _E, rows = scored_event_set([], _truth(90_000, incomplete=True), ["target_window"])
    assert all(r["in_incomplete_region"] for r in rows)                         # 不完整区域取所属 target_present
    assert event_truth_sha256([], []) == event_truth_sha256([], [])
    assert event_truth_sha256(ann, []) != event_truth_sha256(ann, E)


def test_eu_target_set_only_for_target_search() -> None:
    t = _truth(120_000)
    got = eu_target_set({"event_type": "target_search", "report_types": ["found"]}, t)
    assert got is not None and [e["id"] for e in got[0]] == ["target_present"] and got[1] == {"found": ["target_present"]}
    assert eu_target_set({"event_type": "reid_occlusion", "report_types": ["reacquire"]}, t) is None
    assert eu_target_set({"event_type": "target_search", "report_types": ["found"]}, {**t, "report_map": {}}) is None


def test_eu_found_matches_target_present_within_5_m() -> None:
    run = SimpleNamespace(det_actor={("u1", 10, "1" * 16): {"actor": "person_a"}})
    E = [{"id": "target_present", "type": "target_present", "participants": ["person_a"], "a_s": 0.0, "b_s": 120.0, "multi": False, "params": {}}]

    def rep(xy):
        return {"report": {"type": "found", "participants": [{"track_id": "t0", "evidence": [{"uav": "u1", "sensor": "eo", "frame_seq": 10,
                                                                                              "det_id": "1" * 16}]}],
                           "window": {"t_start_ns": 1_000_000_000, "t_end_ns": 2_000_000_000}, "position_enu_m": xy}}

    def locate(r, e):
        return float(np.hypot(*(np.asarray(r["position_enu_m"][:2]) - np.array([10.0, 0.0])))) <= 5.0

    rm = {"found": ["target_present"]}
    assert event_understanding([rep([0.0, 0.0])], E, run)["matches"] == []                       # 0.2.0 映射：found → target_found
    eu = event_understanding([rep([12.0, 3.0])], E, run, report_map=rm, locate=locate)
    assert [m["event_id"] for m in eu["matches"]] == ["target_present"] and eu["f1"] == 1.0
    eu = event_understanding([rep([16.0, 0.0])], E, run, report_map=rm, locate=locate)           # 定位误差 6 m：不匹配
    assert eu["matches"] == [] and eu["f1"] == 0.0


# ---------------------------------------------------------------------------------------------------------- 运行目录端到端
@pytest.fixture(scope="module")
def track_run(synth_event, tmp_path_factory):
    """track_share 100 s（2 机，运动学开发档）的运行目录与世界查询。"""
    from drone4d.baselines import BASELINES
    from test_env import _run, _spec

    from awr.agentsdk.env.core import _open_world

    d = tmp_path_factory.mktemp("k16") / "track"
    _run(_spec(synth_event, d, dur=100.0), lambda u: BASELINES["track_share"]())
    return d, _open_world(synth_event[0])


def _search_variant(ev: Path, run: Path, tmp: Path) -> tuple[Path, Path, dict]:
    """把合成事件改造成目标搜索（目标 person_a）：plan.json 无注释、事件类型 target_search，真值写 target_present 与 60 s 窗口；
    运行目录的任务契约改为 `scored_event_types = [target_window]`、`report_types = [found]`。"""
    ev2, run2 = tmp / "ev" / "v1", tmp / "run"
    shutil.copytree(ev, ev2)
    shutil.copytree(run, run2)
    plan = json.loads((ev2 / "plan.json").read_text())
    plan.update(event_type="target_search", annotations=[])
    (ev2 / "plan.json").write_text(json.dumps(plan))
    man = json.loads((run2 / "manifest.json").read_text())
    n_ms = int(man["n_ticks"]) * 100
    for r in sorted((ev2 / "truth").glob("r*")):
        shutil.rmtree(r)
    (ev2 / "truth" / "r1").mkdir(parents=True)
    t = _truth(n_ms)
    (ev2 / "truth" / "r1" / "event_truth.json").write_text(json.dumps(t))
    task = json.loads((run2 / "task.json").read_text())
    task.update(event_type="target_search", scored_event_types=["target_window"], report_types=["found"])
    (run2 / "task.json").write_text(json.dumps(task))
    return ev2, run2, t


def _found(run_dir: Path, ev: Path, *, off_m: float) -> dict:
    """一条 found report：证据为 person_a 的一个检测（L1 det_id），位置为目标在窗口末时刻的磁带位置 + 偏移。"""
    from awr.sim.events.tape import open_tape

    row = next(json.loads(x) for x in (run_dir / "truth" / "l1.jsonl").read_text().splitlines() if json.loads(x).get("actor") == "person_a")
    t_end = (int(row["seq"]) + 5) * 100_000_000
    tape = open_tape(ev)
    idx = {a["id"]: a["idx"] for a in tape.actors_info()}
    p = tape.actor_states(t_end, [idx["person_a"]]).pos_enu_m[0]
    return {"report": {"type": "found", "participants": [{"track_id": "t1", "evidence": [{"uav": row["uav"], "sensor": "eo",
                                                                                         "frame_seq": int(row["seq"]), "det_id": row["det_id"]}]}],
                       "window": {"t_start_ns": int(row["seq"]) * 100_000_000, "t_end_ns": t_end},
                       "position_enu_m": [float(p[0]) + off_m, float(p[1]), 0.0], "confidence": 0.6}}


def test_search_lane_scores_target_windows_and_found(synth_event, track_run, tmp_path: Path) -> None:
    from awr.agentsdk.task import scored_events
    from awr.bench.eval.evaluator import evaluate_run

    _w, ev = synth_event
    ev2, run2, t = _search_variant(ev, track_run[0], tmp_path)
    assert scored_events(ev2) == []                                             # 0.2.0 的计分集：空（search 赛道四项恒为 0 的成因）
    with (run2 / "reports.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(_found(run2, ev2, off_m=3.0)) + "\n")
        f.write(json.dumps(_found(run2, ev2, off_m=8.0)) + "\n")              # 定位误差 8 m：不匹配
    doc = evaluate_run(run2, ev2, track_run[1])
    assert not [e.message for e in bench_validator("evaluation").iter_errors(doc)]
    assert doc["evaluator_version"] == EVALUATOR_VERSION == "0.3.0"
    assert [e["id"] for e in doc["ec"]["events"]] == [k["id"] for k in t["key_events"]] and len(t["key_events"]) == 2
    assert all(e["type"] == "target_window" for e in doc["ec"]["events"])
    assert doc["components"]["ec"] > 0.0 and doc["ec"]["coverage"] > 0.0      # track_share 跟踪 person_a：窗口被捕获
    assert doc["ao"]["ao_real"] > 0.0 and doc["components"]["ce"] is not None
    assert [m["event_id"] for m in doc["eu"]["matches"]] == ["target_present"]
    n_rep = len((run2 / "reports.jsonl").read_text().splitlines())
    assert doc["eu"]["recall"] == 1.0 and doc["eu"]["precision"] == pytest.approx(1 / n_rep, abs=1e-6)
    assert doc["inputs"]["event_truth_sha256"] == event_truth_sha256([], [
        {"id": k["id"], "type": "target_window", "participants": k["participants"], "a_s": k["interval_ms"][0] / 1000, "b_s": k["interval_ms"][1] / 1000}
        for k in t["key_events"]])


def test_annotation_events_unchanged_by_key_events(synth_event, track_run, tmp_path: Path) -> None:
    """注释类型的事件：事件真值另有关键事件（含同名类型）时评测结果与去掉关键事件逐字节一致（其他赛道重评不变的依据）。"""
    from awr.bench.eval.evaluator import evaluate_run

    _w, ev = synth_event
    base = evaluate_run(track_run[0], ev, track_run[1])
    ev2 = tmp_path / "ev" / "v1"
    shutil.copytree(ev, ev2)
    p = sorted((ev2 / "truth").glob("r*/event_truth.json"))[-1]
    t = json.loads(p.read_text())
    t["key_events"] = [{"id": "meet-k1", "type": "proximity_encounter", "participants": ["person_a", "person_b"], "interval_ms": [0, 4000]},
                       {"id": "target_reacquire-1", "type": "target_reacquire", "participants": ["person_a"], "interval_ms": [1000, 2000]}]
    t["report_map"] = {"meet": ["proximity_encounter"]}
    p.write_text(json.dumps(t))
    assert json.dumps(evaluate_run(track_run[0], ev2, track_run[1])) == json.dumps(base)


def test_hidden_truth_read_through_hidden_store(synth_event, track_run, tmp_path: Path) -> None:
    from awr.bench.auth import AuditLog, Principal
    from awr.bench.eval.evaluator import evaluate_run
    from awr.bench.split import HiddenStore, SplitAccessDenied, read_audit

    _w, ev = synth_event
    direct = evaluate_run(track_run[0], ev, track_run[1])
    root = tmp_path / "d3"
    hs = HiddenStore.for_root(root, AuditLog(root / "audit"))
    eid, rid = "crop-search-901", "run-k16"
    shutil.copytree(ev, hs.event_dir(eid, 1))
    run = root / "dataset" / eid / "runs" / rid
    shutil.copytree(track_run[0], run)
    td = hs.truth_dir(eid) / "runs" / rid
    shutil.move(str(run / "truth"), str(td))                                  # seal-truth 之后的布局：运行真值只在隐藏区
    acc = TruthAccess.hidden(hs, event_id=eid, run_id=rid, job="k16-test")
    doc = evaluate_run(run, hs.event_dir(eid, 1), track_run[1], truth_dir=td, access=acc)
    assert json.dumps(doc) == json.dumps(direct)                               # 与直接读取逐字节一致
    recs = [r for r in read_audit(root / "audit") if r["action"] == "truth_read"]
    files = {Path(r["path"]).name for r in recs if r["op"] == "read"}
    assert files == {"event_truth.json", *(n for n in RUN_TRUTH_FILES if (td / n).is_file())}
    assert all(r["role"] == "evaluator" and r["result"] == "ok" and r["job"] == "k16-test" and r["run_id"] == rid for r in recs)
    assert any(r["op"] == "evaluate" and r["path"] == f"{eid}/event/v1" for r in recs)
    bad = TruthAccess.hidden(hs, event_id=eid, run_id=rid, principal=Principal("r1", "researcher", (), "token"))
    with pytest.raises(SplitAccessDenied) as ei:
        evaluate_run(run, hs.event_dir(eid, 1), track_run[1], truth_dir=td, access=bad)
    assert ei.value.code == 691


def test_truth_digest_survives_ranked_compaction(synth_event, track_run, tmp_path: Path) -> None:
    """`inputs.truth_sha256` 只覆盖 `RUN_TRUTH_FILES`（⊆ retention.TRUTH_MIN_FILES）：真值录制 `run_<id>.mcap` 与种子文件不进入摘要，
    精简后只剩 `TRUTH_MIN_FILES` 时复评逐字节一致。"""
    from awr.bench.eval.evaluator import evaluate_run
    from awr.bench.retention import TRUTH_MIN_FILES

    assert set(RUN_TRUTH_FILES) <= TRUTH_MIN_FILES and "run_seed.json" not in RUN_TRUTH_FILES
    _w, ev = synth_event
    full = tmp_path / "full"
    shutil.copytree(track_run[0], full)
    (full / "truth" / "run_t-5.mcap").write_bytes(b"\x89MCAP0\r\n" + b"x" * 4096)
    (full / "truth" / "run_seed.json").write_text(json.dumps({"run_seed": 5}))
    a = json.dumps(evaluate_run(full, ev, track_run[1]), ensure_ascii=False, indent=1)
    for p in (full / "truth").iterdir():                                       # 精简：truth/ 只保留 TRUTH_MIN_FILES
        if p.name not in TRUTH_MIN_FILES:
            p.unlink() if p.is_file() else shutil.rmtree(p)
    b = json.dumps(evaluate_run(full, ev, track_run[1]), ensure_ascii=False, indent=1)
    assert a == b
    blobs = {n: (full / "truth" / n).read_bytes() for n in RUN_TRUTH_FILES if (full / "truth" / n).is_file()}
    assert json.loads(a)["inputs"]["truth_sha256"] == truth_digest(blobs)
