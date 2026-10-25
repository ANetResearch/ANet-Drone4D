"""排行榜 v1 正式评测工具 `tools/bench/d3/board_v1_formal.py`（B21；D3-AC-43、D3-AC-57）的本机端到端：封板 → 基线正式 Run → 内部提交
（真实 `subworker.process_one`）→ 发布与连算 → 真值入隐藏区 → 报告 → 重算计时。

替身只有两处：docker（提交镜像 inspect，`tests/d3/submit/fake_scan_docker.py`）与单个 Run 的执行（`awr.bench.runner.run_job`：写 Env
运行清单与 `evaluation.json`，真值层含 `run_seed.json`）；封板、EvalStore、评测视图、审计包装、`mbl_e2e.execute_run`、提交流水线、
榜单聚合与发布、隐藏区与审计均为产品代码。
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("foxglove_schemas_protobuf")          # 提交流水线依赖 D3 环境（.venv-d3）

from awr.bench import split as sp
from awr.bench.auth import AuditLog
from awr.bench.boardstore import load_rows
from awr.bench.eval.aggregate import verify_lock
from awr.bench.subworker import v1_problems

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "bench" / "d3"))
import board_v1_formal as F  # noqa: E402
import mbl_e2e as E  # noqa: E402

N_BOOT = 200
TEST_EVENTS = {"crop-reid-101": "reid_occlusion", "crop-reid-102": "reid_occlusion", "crop-handoff-101": "rendezvous_handoff",
               "crop-handoff-102": "rendezvous_handoff", "crop-vehicle-101": "vehicle_track", "crop-vehicle-102": "vehicle_track",
               "crop-search-101": "target_search", "crop-search-102": "target_search"}
PLATFORM = {k: "sha256:" + c * 64 for k, c in (("gz", "1"), ("m25_worker", "2"), ("env", "2"), ("baseline", "4"))}
IMG = {"close": "sha256:" + "c" * 64, "relay": "sha256:" + "d" * 64}
SKILL = {"static_guard": (0.30, 0.10, 0.20, 0.10), "coverage_scan": (0.50, 0.20, 0.40, 0.20), "track_share": (0.60, 0.30, 0.45, 0.25),
         IMG["close"]: (0.65, 0.35, 0.50, 0.30), IMG["relay"]: (0.80, 0.25, 0.60, 0.35)}
SUBMIT = ROOT / "tests" / "d3" / "submit" / "fake_scan_docker.py"


def _event(hidden: sp.HiddenStore, eid: str, etype: str) -> None:
    d = hidden.event_dir(eid, 1)
    (d / "truth" / "r1").mkdir(parents=True)
    (d / "plan.json").write_text(json.dumps({"event_id": eid, "version": 1, "event_type": etype, "duration_ms": 180_000}))
    (d / "tape.bin").write_bytes(b"D4DTAPE\x00" + eid.encode())
    (d / "truth" / "r1" / "event_truth.json").write_text(json.dumps({"event_id": eid}))
    for p in [hidden.root, *hidden.root.rglob("*")]:
        os.chmod(p, 0o700 if p.is_dir() else 0o600)


class FakeRun:
    """单个 Run 的替身：核对排名 Run 的种子承诺值与事件视图，写 Env 运行清单（gz 锁步、p600_bench、白名单审计通过）、真值层与评测。"""

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.calls: list[dict] = []

    def __call__(self, rs, *, policy, seed, run_id, run_dir, worlds_root, events_root, physics=None, gz=None, agent_image=None, docker="docker",
                 duration_s=None, seed_commit=None, **_kw) -> dict:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        agents = {f"uav-0{i}": {"acts": 40, "exited": "bye", "exit_code": 0} for i in (1, 2, 3)}
        self.calls.append({"run_id": run_id, "event": rs["event"]["id"], "seed": int(seed), "seed_commit": seed_commit, "physics": physics,
                           "ranked": rs.get("ranked"), "obs_level": rs["obs_level"]})
        if run_id.endswith("-smoke"):
            return {"run_id": run_id, "run_dir": str(run_dir), "run_wall_s": 1.0, "agents": agents}
        k_eval = bytes.fromhex(json.loads(self.store_path.read_text())["k_eval"])
        assert seed_commit == hmac.new(k_eval, f"{run_id}:{int(seed)}".encode(), hashlib.sha256).hexdigest()
        assert (Path(events_root) / rs["event"]["id"] / "v1" / "tape.bin").is_file()      # Env 经评测视图读到隐藏区中的磁带
        key = policy.get("name") or policy.get("image_digest")
        g = np.random.default_rng(int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16))
        comp = dict(zip(E.METRICS, (float(np.clip(m + g.normal(0, 0.03), 0, 1)) for m in SKILL[key]), strict=True))
        man = {"run_id": run_id, "physics": physics, "tier": "realistic", "n_ticks": 1800, "wall_s": 400.0, "ranked": True,
               "tier_info": {"iterations": 25 * 1820, "ticks": 1820, "preroll": {"n_ticks": 20}, "vehicle": {"name": "p600_bench"}},
               "obs_audit": {"passed": True, "unknown_fields": 0, "schema_violations": 0}, "event": {"content_sha256": "0" * 64}}
        (run_dir / "manifest.json").write_text(json.dumps(man))
        (run_dir / "truth").mkdir(exist_ok=True)
        (run_dir / "truth" / "run_seed.json").write_text(json.dumps({"run_seed": int(seed)}))
        (run_dir / "evaluation.json").write_text(json.dumps({"components": comp, "ranked": True, "ranked_valid": True, "invalid_reasons": []}))
        return {"run_id": run_id, "run_dir": str(run_dir), "run_wall_s": 400.0, "components": comp, "ranked_valid": True, "agents": agents}


class WorkerCrash(BaseException):
    """模拟处理进程意外终止（段错误等）：不是 Exception，subworker 的平台重跑路径不会捕获。"""


def _crashing(fake: FakeRun, *, after: int):
    """前 `after` 个正式 Run 正常，下一个 Run 写出部分产物（含运行真值）后终止处理进程。"""
    n = {"runs": 0}

    def run(rs, **kw):
        if not str(kw["run_id"]).endswith("-smoke"):
            n["runs"] += 1
            if n["runs"] > after:
                d = Path(kw["run_dir"])
                (d / "truth").mkdir(parents=True, exist_ok=True)
                (d / "truth" / "run_seed.json").write_text(json.dumps({"run_seed": int(kw["seed"])}))
                raise WorkerCrash
        return fake(rs, **kw)

    return run


@pytest.fixture(scope="module")
def formal(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("b21")
    root, out = tmp / "d3", tmp / "out"
    audit = AuditLog(root / "audit")
    reg = sp.SplitRegistry.for_root(root, audit=audit)
    hidden = sp.HiddenStore.for_root(root, audit)
    for e, t in TEST_EVENTS.items():
        _event(hidden, e, t)
        reg.register(e, "test", by="test", version="v1")
    reg.register("crop-handoff-002", "dev", by="test")
    docker = tmp / "docker"
    docker.write_text(f"#!/bin/sh\nFAKE_SCAN_STATE={tmp / 'scan'} exec {sys.executable} {SUBMIT} \"$@\"\n")
    docker.chmod(0o755)
    (tmp / "scan").mkdir()
    (tmp / "scan" / "images.json").write_text(json.dumps({d: {"Os": "linux", "Architecture": "amd64", "Size": 900_000_000} for d in IMG.values()}))
    logs: list[str] = []
    seal = F.seal_board_version(root, board_version="v1", worlds_root=tmp / "worlds", platform_refs={"gz": "d3-gz:t", "m25_worker": "d3-compile:t",
                                "env": "d3-compile:t", "baseline": "d3-agent-py:t"}, gz_image="d3-gz:t", agent_image="d3-agent-py:t", gpu=None,
                                lock_out=tmp / "lock.json", eval_uid=None, digests=PLATFORM, gpu_name="NVIDIA A40", isa="x86-64-v4", n_boot=N_BOOT,
                                log=logs.append)
    fake = FakeRun(F._store_path(root, "v1"))
    mp = pytest.MonkeyPatch()
    mp.setattr("awr.bench.runner.run_job", fake)
    import awr.bench.subworker as sw

    mp.setattr(sw, "append_rows", sw.append_rows)            # run_submission 包装的加锁写入在测试结束时还原
    mp.setattr(sw, "publish", sw.publish)
    base = {t: F.run_baselines(root, out, board_version="v1", track=t, jobs=1, docker=str(docker), log=logs.append) for t in ("std3-l2", "std3-l1")}
    subs, resumed = {}, None
    for name, track, img, team in (("close-l2", "std3-l2", "close", "d3-internal"), ("relay-l2", "std3-l2", "relay", "d3-research"),
                                   ("close-l1", "std3-l1", "close", "d3-internal"), ("relay-l1", "std3-l1", "relay", "d3-research")):
        if name == "relay-l2":                               # 处理进程在第 31 个正式 Run 中意外终止，之后由 resume 续跑
            mp.setattr("awr.bench.runner.run_job", _crashing(fake, after=30))
        try:
            subs[name] = F.run_submission(root, out, board_version="v1", track=track, name=name, image=f"d3-example-{img}:t",
                                          cmd=["python", "-m", "anet4d.serve", f"{img}.agent:Agent"], team=team, owner="b21", n_boot=N_BOOT,
                                          docker=str(docker), digest=IMG[img], poll_s=0.01, log=logs.append)
        except WorkerCrash:
            mp.setattr("awr.bench.runner.run_job", fake)
            resumed = F.resume_submission(root, out, board_version="v1", name=name, docker=str(docker), n_boot=N_BOOT, log=logs.append)
            subs[name] = json.loads((out / "submissions" / f"{name}.result.json").read_text())
    pub = F.publish_board(root, out, board_version="v1", n_boot=N_BOOT, log=logs.append)
    sealed = F.seal_run_truth(root, board_version="v1", eval_uid=None, log=logs.append)
    gpu_log = tmp / "gpu.jsonl"
    gpu_log.write_text("".join(json.dumps({"t": f"2026-10-22T00:00:{i:02d}Z", "gpu": 3, "util": 40,
                                           "procs": [{"pid": 10 + i, "name": "python3", "mem_mib": 900, "container": "d3-b21-l2base"}]}) + "\n"
                               for i in range(5)))
    rep = F.build_report(root, out, board_version="v1", gpu_log=gpu_log, n_boot=N_BOOT, eval_uid=os.getuid(), log=logs.append)
    yield {"root": root, "out": out, "tmp": tmp, "seal": seal, "base": base, "subs": subs, "pub": pub, "sealed": sealed, "rep": rep, "fake": fake,
           "logs": logs, "hidden": hidden, "reg": reg, "resumed": resumed}
    mp.undo()


def test_seal_writes_config_evalstore_view_and_lock_without_secrets(formal) -> None:
    root, tmp = formal["root"], formal["tmp"]
    cfg = json.loads((root / "boards" / "v1" / "config.json").read_text())
    store_p = F._store_path(root, "v1")
    store = json.loads(store_p.read_text())
    assert stat.S_IMODE(store_p.stat().st_mode) == 0o600 and stat.S_IMODE(store_p.parent.stat().st_mode) == 0o700
    assert not verify_lock(cfg["lock"], store, seeds_per_event=5)
    assert cfg["lock"]["test_split_version"] == formal["reg"].digest() and cfg["lock"]["platform"] == PLATFORM
    assert {ln["lane"]: sorted(ln["test_events"]) for ln in cfg["lock"]["lanes"]} == {
        "reid": ["crop-reid-101", "crop-reid-102"], "handoff": ["crop-handoff-101", "crop-handoff-102"],
        "vehicle": ["crop-vehicle-101", "crop-vehicle-102"], "search": ["crop-search-101", "crop-search-102"]}
    assert all(v1_problems(cfg, t) == [] for t in ("std3-l2", "std3-l1"))
    assert all(x["starts"] == F.STARTS and x["world_id"] == "oryahovo-crop1" for x in cfg["events"].values())
    assert cfg["smoke"]["run_spec"]["event"]["id"] == "crop-handoff-002" and cfg["smoke"]["run_spec"]["event"]["id"] not in TEST_EVENTS
    for e in TEST_EVENTS:                                                            # 评测视图：eval-events/<eid> → hidden/<eid>/event
        assert (root / "eval-events" / e).resolve() == formal["hidden"].root.resolve() / e / "event"
    lock = json.loads((tmp / "lock.json").read_text())                             # 入库形式：只含承诺值，不含 test 事件 id（M23-FR-082）
    assert lock == F.public_lock(cfg["lock"]) and lock["lock_sha256"] == F.lock_sha256(cfg["lock"])
    assert lock["schema"] == F.PUBLIC_LOCK_SCHEMA and lock["seed_set_commit"] == cfg["lock"]["seed_set_commit"]
    assert not [e for e in TEST_EVENTS if e in (tmp / "lock.json").read_text()]
    for pub, full in zip(lock["lanes"], cfg["lock"]["lanes"], strict=True):
        assert pub["seed_set_commit"] == full["seed_set_commit"] and pub["n_test_events"] == len(full["test_events"]) == 2
        assert pub["event_commits"] == sorted(full["event_commits"].values())
    txt = (tmp / "lock.json").read_text() + (root / "boards" / "v1" / "config.json").read_text()
    assert store["k_eval"] not in txt and store["k_board"] not in txt
    assert not [s for s in F._all_seeds(store) if str(s) in txt]
    assert (root / "boards" / "current").read_text().strip() == "v1"
    with pytest.raises(F.FormalError, match="already sealed"):                     # 榜单版本不原地重新封板
        F.seal_board_version(root, board_version="v1", worlds_root=tmp, platform_refs={}, gz_image="x", agent_image="y", gpu=None, eval_uid=None,
                             digests=PLATFORM, gpu_name="g", isa="x86-64-v4")


def test_formal_runs_use_evalstore_seeds_commits_and_audit(formal) -> None:
    root, fake = formal["root"], formal["fake"]
    store = json.loads(F._store_path(root, "v1").read_text())
    formal_calls = [c for c in fake.calls if not c["run_id"].endswith("-smoke")]
    assert len(formal_calls) == 3 * 160 + 4 * 80                                     # 3 基线 × 2 分轨 × 80 + 4 个提交 × 80
    assert all(c["ranked"] and c["physics"] == "realistic" for c in formal_calls)
    lane_of = {e: ln for ln, evs in (("reid", ("crop-reid-101", "crop-reid-102")), ("handoff", ("crop-handoff-101", "crop-handoff-102")),
                                     ("vehicle", ("crop-vehicle-101", "crop-vehicle-102")), ("search", ("crop-search-101", "crop-search-102")))
              for e in evs}
    for c in formal_calls:
        k = int(c["run_id"].rsplit("-k", 1)[1].split("r")[0])
        assert c["seed"] == store["seeds"][lane_of[c["event"]]][c["event"]][k] and str(c["seed"]) not in c["run_id"]
    smokes = [c for c in fake.calls if c["run_id"].endswith("-smoke")]
    assert len(smokes) == 4 and all(c["event"] == "crop-handoff-002" and c["seed"] == 0 for c in smokes)
    assert {r["completed"] for r in formal["base"].values()} == {240}
    recs = sorted((formal["out"] / "records").glob("*.json"))
    assert len(recs) == 480 and all(json.loads(p.read_text())["lockstep"]["ok"] for p in recs)
    summ = sp.truth_access_summary(sp.read_audit(root / "audit"))
    assert summ["external"] == 0 and summ["internal"] >= 2 * len(formal_calls) + len(TEST_EVENTS)
    ops = {r.get("op") for r in sp.read_audit(root / "audit") if r.get("action") == "truth_read"}
    assert {"env", "evaluate"} <= ops


def test_submissions_reach_done_and_board_lists_three_baselines_and_two_entries(formal) -> None:
    root = formal["root"]
    assert {r["state"] for r in formal["subs"].values()} == {"DONE"}
    assert all(r["history"][:4] == ["RECEIVED", "SCANNING", "VALIDATING", "EVALUATING"] for r in formal["subs"].values())
    b = json.loads((root / "boards" / "v1" / "board.json").read_text())
    assert b["score_version"] == "d3-score-1" and len(b["entries"]) == 2 * 4 * 5
    for t in ("std3-l2", "std3-l1"):
        for ln in ("reid", "handoff", "vehicle", "search"):
            es = [e for e in b["entries"] if e["track"] == t and e["lane"] == ln]
            assert sum(e["baseline"] for e in es) == 3 and sum(not e["baseline"] for e in es) == 2
            assert [e["display_order"] for e in es] == list(range(1, 6)) and all(e["baseline"] for e in es[-3:])
            for e in es:
                assert e["status"] == "valid" and e["dominance_tier"] is not None
                assert all(v["ci95"][0] <= v["value"] <= v["ci95"][1] for v in e["metrics"].values())
                if not e["baseline"]:
                    assert all("rank" in v and "tie_group" in v for v in e["metrics"].values())
    assert len(load_rows(root / "boards" / "v1")) == 3 * 160 + 4 * 80
    assert all(formal["pub"]["twice_identical"].values())
    b2 = F.publish_board(root, formal["out"], board_version="v1", n_boot=N_BOOT, log=lambda m: None)   # 幂等：再次执行不重复写入基线行
    assert b2["sha256"] == formal["pub"]["sha256"] and len(load_rows(root / "boards" / "v1")) == 3 * 160 + 4 * 80


def test_run_truth_moves_into_hidden_and_report_passes(formal) -> None:
    root, rep = formal["root"], formal["rep"]
    assert formal["sealed"]["moved"] == 3 * 160 + 4 * 80 and formal["sealed"]["conflicts"] == 0
    assert not list((root / "dataset").glob("*/runs/*/truth/*"))
    assert len(list(formal["hidden"].root.glob("*/truth/runs/*/run_seed.json"))) == 3 * 160 + 4 * 80
    bad = [c for c in rep["checks"] if not c["passed"]]
    assert rep["passed"], bad
    ids = {c["id"] for c in rep["checks"]}
    assert ids >= {"entries", "runs_complete", "gz_lockstep", "obs_audit", "recompute", "twice_identical", "no_composite", "ranks_ties_tiers",
                   "submissions", "truth_audit", "api_visibility", "hidden", "secrets", "exclusive_gpu"}
    ta = next(c for c in rep["checks"] if c["id"] == "truth_audit")
    assert ta["probe_code"] == 691 and ta["audit"]["external"] == 0 and ta["audit"]["denied"] >= 1
    api = next(c for c in rep["checks"] if c["id"] == "api_visibility")["api"]
    assert all(v == {"http": 404, "code": 305} for v in api["anonymous"].values())
    assert len(api["invited"]) == 8 and all(v["http"] == 200 and v["entries"] == v["expected"] == 5 for v in api["invited"].values())


def test_report_fails_on_seed_leak_and_external_gpu_process(formal, tmp_path: Path) -> None:
    root, out = formal["root"], formal["out"]
    store = json.loads(F._store_path(root, "v1").read_text())
    leak = root / "dataset" / "leak.log"
    leak.write_text(f"debug seed {F._all_seeds(store)[0]}\n")
    try:
        g = tmp_path / "gpu.jsonl"
        g.write_text(json.dumps({"t": "x", "gpu": 3, "util": 90, "procs": [{"pid": 1, "name": "vllm", "container": None}]}) + "\n")
        rep = F.build_report(root, out, board_version="v1", gpu_log=g, probe=False, n_boot=N_BOOT, eval_uid=os.getuid(), log=lambda m: None)
        failed = {c["id"] for c in rep["checks"] if not c["passed"]}
        assert failed == {"secrets", "exclusive_gpu"} and not rep["passed"]
        assert str(F._all_seeds(store)[0]) not in json.dumps(rep)                    # 报告不回显秘密
    finally:
        leak.unlink()


def test_resume_after_worker_crash_reuses_completed_runs(formal) -> None:
    """处理进程在 EVALUATING 中意外终止：已完成的 30 个 Run 沿用，中断的 Run 删除残留后重跑并计入平台重跑，提交照常 DONE 并上榜。"""
    res, root = formal["resumed"], formal["root"]
    assert res is not None and res["reused"] == 30 and len(res["interrupted"]) == 1 and res["rerun"] == 50 and res["state"] == "DONE"
    assert res["history"] == ["RECEIVED", "SCANNING", "VALIDATING", "EVALUATING", "SCORING", "DONE"]
    sid = res["submission_id"]
    calls = [c["run_id"] for c in formal["fake"].calls if c["run_id"].startswith(sid) and not c["run_id"].endswith("-smoke")]
    assert len(calls) == 80 and len(set(calls)) == 80 and res["interrupted"][0] in calls     # 中断的 Run 只在续跑中完整执行一次
    doc = json.loads((root / "submissions" / f"{sid}.json").read_text())
    assert all(ln["runs"]["total"] == 20 and ln["runs"]["platform_rerun"] == 1 for ln in doc["result"]["lanes"])
    rows = [r for r in load_rows(root / "boards" / "v1") if r.get("submission_id") == sid]
    assert len(rows) == 80 and len({r["run_id"] for r in rows}) == 80
    with pytest.raises(F.FormalError):
        F.resume_submission(root, formal["out"], board_version="v1", name="relay-l2", log=lambda m: None)


def test_gpu_log_attributes_exited_process_by_pid(tmp_path: Path) -> None:
    """短命进程在读取 cgroup 前退出时按同一 pid 的其他采样归属；从未归属的进程计为外部进程。"""
    rows = [{"t": "2026-10-22T00:00:00Z", "gpu": 3, "util": 30, "procs": [{"pid": 7, "name": "python3", "mem_mib": 680, "container": "d3-b21-base-l2"}]},
            {"t": "2026-10-22T00:00:01Z", "gpu": 3, "util": 0, "procs": [{"pid": 7, "name": "[No data]", "mem_mib": 680, "container": None, "exited": True}]}]
    g = tmp_path / "gpu.jsonl"
    g.write_text("".join(json.dumps(r) + "\n" for r in rows))
    s = F.summarize_gpu_log(g)
    assert s["external_samples"] == 0 and s["attributed_by_pid"] == 1 and s["samples"] == 2
    rows.append({"t": "2026-10-22T00:00:02Z", "gpu": 3, "util": 0, "procs": [{"pid": 8, "name": "python3", "mem_mib": 500, "container": None, "exited": True}]})
    g.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert F.summarize_gpu_log(g)["external_samples"] == 1


def test_rebuild_timing_and_helpers(formal) -> None:
    res = F.rebuild_timing(formal["root"], board_version="v1", n_entries=6, repeat=2, n_boot=N_BOOT, log=lambda m: None)
    assert res["board_entries"] == 24 and res["rows"] == 6 * 4 * 20 and len(res["seconds"]) == 2 and res["passed"]
    assert F.cpu_isa_level("flags : sse4_2 popcnt ssse3 avx2 fma bmi2 movbe avx512f avx512bw avx512cd avx512dq avx512vl\n") == "x86-64-v4"
    assert F.cpu_isa_level("flags : sse4_2 popcnt ssse3 avx2 fma bmi2 movbe\n") == "x86-64-v3"
    calls = []
    locked = F._locked(lambda d, x: calls.append(x), lambda d: Path(d))
    assert F._locked(locked, lambda d: Path(d)) is locked                            # 不重复包装（同一进程内重入 flock 即死锁）
    locked(formal["tmp"], 1)
    assert calls == [1]


def test_audited_run_job_records_env_and_evaluate_reads(formal, monkeypatch) -> None:
    root = formal["root"]
    got = {}

    def run_job(rs, **kw):
        got.update(kw)
        return {"ok": True}

    monkeypatch.setattr("awr.bench.runner.run_job", run_job)
    rs = {"event": {"id": "crop-vehicle-101", "version": 1}}
    n0 = len(sp.read_audit(root / "audit"))
    F.audited_run_job(rs, data_root=str(root), test_events=list(TEST_EVENTS), seed_commit="ab" * 32, run_id="x-k0r0", run_dir="/tmp/x")
    new = sp.read_audit(root / "audit")[n0:]
    assert [(r["action"], r["op"], r["role"], r["result"]) for r in new] == [("truth_read", "env", "evaluator", "ok"),
                                                                           ("truth_read", "evaluate", "evaluator", "ok")]
    assert got["seed_commit"] == "ab" * 32
    F.audited_run_job({"event": {"id": "crop-handoff-002", "version": 1}}, data_root=str(root), test_events=list(TEST_EVENTS), run_id="dev")
    assert len(sp.read_audit(root / "audit")) == n0 + 2                              # dev 事件不写 test 真值审计
    assert functools.partial(F.audited_run_job, data_root=str(root), test_events=[]).func is F.audited_run_job


# ---------------------------------------------------------------------------------------------------------- K16：重评版本、平台重跑、实验室发布
def _fake_rescore(run_dir: Path, event_dir: Path, truth_dir: Path | None, access) -> dict:
    """评测器替身（重评）：经 HiddenStore 读取事件真值与运行真值（审计），search 赛道的分量改变，其余赛道原样（只换评测器版本）。"""
    access.read_bytes(Path(event_dir) / "truth" / "r1" / "event_truth.json")
    assert truth_dir is not None and (Path(truth_dir) / "run_seed.json").is_file()            # seal-truth 之后运行真值只在隐藏区
    access.authorize_dir(Path(event_dir))
    old = json.loads((Path(run_dir) / "evaluation.json").read_text())
    doc = {**old, "evaluator_version": "0.3.0"}
    if "crop-search-" in Path(run_dir).name:
        doc["components"] = {k: round(min(1.0, v + 0.2), 6) for k, v in old["components"].items()}
    return doc


@pytest.fixture(scope="module")
def rescored(formal, tmp_path_factory):
    """formal 数据根的副本上执行 rescore v1 → v1.1（不改动 formal 的数据根）。"""
    tmp = tmp_path_factory.mktemp("k16")
    root, out = tmp / "d3", tmp / "out"
    shutil.copytree(formal["root"], root, symlinks=True)
    logs: list[str] = []
    rep = F.rescore_board(root, out, src="v1", dst="v1.1", n_boot=N_BOOT, eval_uid=None, evaluate=_fake_rescore, log=logs.append)
    return {"root": root, "out": out, "rep": rep, "logs": logs, "tmp": tmp}


def test_rescore_publishes_v1_1_with_unchanged_lanes_byte_identical(formal, rescored) -> None:
    from awr.bench.eval.aggregate import SCORE_V2

    root, rep = rescored["root"], rescored["rep"]
    assert rep["passed"] and rep["runs"] == 3 * 160 + 4 * 80 and rep["reused"] == 0
    assert rep["changed_runs"] == 5 * 2 * 20                                        # search 赛道：5 个条目 × 2 分轨 × 20 次
    for k, v in rep["lanes"].items():
        if k.endswith("/search"):
            assert not v["evaluations_unchanged"] and not v["board_identical"]
        else:
            assert v["evaluations_unchanged"] and v["board_identical"] and v["identical"] == v["runs"], k
    cfg1 = json.loads((root / "boards" / "v1" / "config.json").read_text())
    cfg2 = json.loads((root / "boards" / "v1.1" / "config.json").read_text())
    store2 = json.loads(F._store_path(root, "v1.1").read_text())
    store1 = json.loads(F._store_path(formal["root"], "v1").read_text())
    assert cfg2["score_version"] == SCORE_V2 and cfg2["lock"]["rescore_of"] == "v1" and cfg2["rescore"]["of"] == "v1"
    assert not verify_lock(cfg2["lock"], store2, seeds_per_event=5)
    assert store2["seeds"] == store1["seeds"] and store2["k_board"] == store1["k_board"] and store2["board_version"] == "v1.1"
    assert [ln["seed_set_commit"] for ln in cfg2["lock"]["lanes"]] == [ln["seed_set_commit"] for ln in cfg1["lock"]["lanes"]]
    assert (root / "boards" / "current").read_text().strip() == "v1.1"
    b1 = json.loads((root / "boards" / "v1" / "board.json").read_text())
    assert (root / "boards" / "v1" / "board.json").read_bytes() == (formal["root"] / "boards" / "v1" / "board.json").read_bytes()   # 原版本只读
    b2 = json.loads((root / "boards" / "v1.1" / "board.json").read_text())
    assert b2["score_version"] == SCORE_V2 and b1["score_version"] == "d3-score-1"
    assert all(rep["publish"]["twice_identical"].values())
    ev = next((root / "dataset" / "crop-search-101" / "runs").glob("*k0r0"))
    assert json.loads((ev / "evaluation.json").read_text())["evaluator_version"] == "0.3.0" and (ev / "evaluation.unknown.json").is_file()
    ops = [r for r in sp.read_audit(root / "audit") if r.get("job") == F.RESCORE_JOB]
    assert len({r["run_id"] for r in ops}) == rep["runs"] and {r["role"] for r in ops} == {"evaluator"}       # 每个 Run 的真值读取均有审计
    again = F.rescore_board(root, rescored["out"], src="v1", dst="v1.1", n_boot=N_BOOT, eval_uid=None, log=lambda m: None)
    assert again["passed"] and again["reused"] == again["runs"] and again["publish"]["sha256"] == rep["publish"]["sha256"]   # 续跑幂等


def test_rescore_moves_submissions_and_refreshes_results(rescored) -> None:
    root = rescored["root"]
    b2 = json.loads((root / "boards" / "v1.1" / "board.json").read_text())
    ent = {(e["entry_id"], e["track"], e["lane"]): e for e in b2["entries"] if not e["baseline"]}
    docs = [json.loads(p.read_text()) for p in (root / "submissions").glob("sub-*.json")]
    assert len(docs) == 4
    for d in docs:
        assert d["_meta"]["board_version"] == "v1.1" and d["_meta"]["board_history"][0]["board_version"] == "v1"
        assert d["_meta"]["result_board"]["board_version"] == "v1.1"
        for ln in d["result"]["lanes"]:
            e = ent[(f"{d['_meta']['team']}/{d['submission_id']}", d["track"], ln["lane"])]
            assert ln["metrics"] == e["metrics"] and ln["status"] == e["status"] and ln.get("dominance_tier") == e["dominance_tier"]
    rep = F.build_report(root, rescored["out"], board_version="v1.1", n_boot=N_BOOT, eval_uid=os.getuid(), log=lambda m: None)
    assert rep["passed"], [c for c in rep["checks"] if not c["passed"]]


def test_rerun_platform_replaces_the_run_and_keeps_the_superseded_one(rescored) -> None:
    from awr.bench.boardstore import load_rows

    root, out = rescored["root"], rescored["tmp"] / "out-rerun"
    rid = "v1-l2-static_guard-crop-handoff-102-k4r0"
    n0 = len(load_rows(root / "boards" / "v1.1"))
    res = F.rerun_platform(root, out, board_version="v1.1", run_ids=[rid], reason="all agents timed out on the first tick (661) under host load",
                           n_boot=N_BOOT, eval_uid=None, log=lambda m: None)
    rows = load_rows(root / "boards" / "v1.1")
    assert len(rows) == n0 and sum(r["run_id"] == rid for r in rows) == 1
    sup = [json.loads(x) for x in (root / "boards" / "v1.1" / "rows.superseded.jsonl").read_text().splitlines()]
    assert [r["run_id"] for r in sup] == [rid] and "first tick" in sup[0]["superseded"]["reason"]
    runs = root / "dataset" / "crop-handoff-102" / "runs"
    assert (runs / rid / "evaluation.json").is_file() and (runs / f"{rid}.superseded-1" / "evaluation.json").is_file()
    assert (root / "hidden" / "crop-handoff-102" / "truth" / "runs" / f"{rid}.superseded-1").is_dir()
    assert (root / "hidden" / "crop-handoff-102" / "truth" / "runs" / rid / "run_seed.json").is_file()      # 新 Run 的真值已入隐藏区
    assert not (runs / rid / "truth").exists() and all(res["publish"]["twice_identical"].values())
    assert res["runs"][0]["run_id"] == rid and res["runs"][0]["superseded"] == f"{rid}.superseded-1"


def test_rerun_platform_replaces_a_submission_run_without_charging_gpu(formal, rescored) -> None:
    from awr.bench.submissions import SubmissionStore

    root, out = rescored["root"], rescored["tmp"] / "out-rerun-sub"
    bdir = root / "boards" / "v1.1"
    st = SubmissionStore(root / "submissions")
    d = next(x for x in st.all() if x["image_digest"] == IMG["close"] and x["track"] == "std3-l2")
    sid = d["submission_id"]
    rid = f"{sid}-crop-search-102-k0r1"
    old = next(r for r in load_rows(bdir) if r["run_id"] == rid)
    gpu0, n0, lanes0 = list(d["_meta"].get("gpu", [])), len(load_rows(bdir)), {ln["lane"]: ln["runs"] for ln in d["result"]["lanes"]}
    calls: list[dict] = []

    def failing(rs, **kw):                                   # 第一次重跑失败：残留改名为 failed，评测行与 superseded 保持不变
        calls.append(kw)
        Path(kw["run_dir"]).mkdir(parents=True, exist_ok=True)
        (Path(kw["run_dir"]) / "truth").mkdir(exist_ok=True)
        (Path(kw["run_dir"]) / "truth" / "contacts.jsonl").write_text("")
        raise RuntimeError(f"gz lost lockstep (seed {kw['seed']})")

    with pytest.raises(F.FormalError) as ei:
        F.rerun_platform(root, out, board_version="v1.1", run_ids=[rid], reason="first-tick timeout", n_boot=N_BOOT, eval_uid=None,
                         run_job=failing, log=lambda m: None)
    assert str(calls[0]["seed"]) not in str(ei.value) and "superseded run is kept" in str(ei.value)
    runs = root / "dataset" / "crop-search-102" / "runs"
    assert (runs / f"{rid}.superseded-1").is_dir() and (runs / f"{rid}.failed-1").is_dir() and not (runs / rid).exists()
    assert len(load_rows(bdir)) == n0 and next(r for r in load_rows(bdir) if r["run_id"] == rid) == old

    res = F.rerun_platform(root, out, board_version="v1.1", run_ids=[rid], reason="all agents timed out on the first tick (661)", n_boot=N_BOOT,
                           eval_uid=None, log=lambda m: None)                       # 再次重跑：沿用 superseded-1，经 audited_run_job 运行
    assert res["runs"][0]["kind"] == "submission" and res["runs"][0]["superseded"] == f"{rid}.superseded-1"
    assert not (runs / f"{rid}.superseded-2").exists() and (runs / rid / "evaluation.json").is_file()
    assert (root / "hidden" / "crop-search-102" / "truth" / "runs" / rid / "run_seed.json").is_file()
    assert (root / "hidden" / "crop-search-102" / "truth" / "runs" / f"{rid}.failed-1" / "contacts.jsonl").is_file()   # 失败残留的真值也入隐藏区
    rows = load_rows(bdir)
    new = next(r for r in rows if r["run_id"] == rid)
    assert len(rows) == n0 and sum(r["run_id"] == rid for r in rows) == 1
    assert {k: v for k, v in new.items() if k != "evaluation"} == {k: v for k, v in old.items() if k != "evaluation"}
    fake = [c for c in formal["fake"].calls if c["run_id"] == rid][-1]               # 提交方镜像、EvalStore 种子与种子承诺值（替身已核对承诺值）
    assert fake["seed"] == calls[0]["seed"] and fake["seed_commit"] == calls[0]["seed_commit"] and fake["obs_level"] == "L2"
    sup = [json.loads(x) for x in (bdir / "rows.superseded.jsonl").read_text().splitlines()]
    assert sup[-1]["run_id"] == rid and sup[-1]["submission_id"] == sid
    d2 = st.get(sid)
    assert d2["_meta"].get("gpu", []) == gpu0                                        # GPU 用量不记入提交方（D3-AC-57 (d)）
    assert [x["run_id"] for x in d2["_meta"]["platform_reruns"]] == [rid] and d2["_meta"]["platform_reruns"][0]["gpu_charged"] is False
    for ln in d2["result"]["lanes"]:
        assert ln["runs"]["platform_rerun"] == lanes0[ln["lane"]]["platform_rerun"] + 1
    b2 = json.loads((bdir / "board.json").read_text())
    e = next(x for x in b2["entries"] if x["entry_id"] == f"{d['_meta']['team']}/{sid}" and x["lane"] == "search")
    ln = next(x for x in d2["result"]["lanes"] if x["lane"] == "search")
    assert ln["metrics"] == e["metrics"] and all(res["publish"]["twice_identical"].values())   # 名次与点值由发布同步
    rep = F.build_report(root, out, board_version="v1.1", n_boot=N_BOOT, eval_uid=os.getuid(), log=lambda m: None)
    assert rep["passed"], [c for c in rep["checks"] if not c["passed"]]


def test_rerun_platform_refuses_unknown_runs(rescored) -> None:
    with pytest.raises(F.FormalError, match=r"not runs of v1\.1"):
        F.rerun_platform(rescored["root"], rescored["tmp"] / "out-x", board_version="v1.1", run_ids=["v1-l2-nobody-crop-reid-101-k0r0"],
                         reason="x", n_boot=N_BOOT, eval_uid=None, log=lambda m: None)


def test_publish_lab_copies_board_artifacts_and_submissions_only(rescored, tmp_path: Path) -> None:
    root = rescored["root"]
    lab = tmp_path / "lab"
    (lab / "boards").mkdir(parents=True)
    (lab / "submissions").mkdir()
    (lab / "boards" / "current").write_text("p0a-formal-r2-l2\n")
    res = F.publish_lab(root, rescored["out"], board_versions=["v1", "v1.1"], lab_root=lab, current="v1.1", log=lambda m: None)
    assert res["current"] == {"before": "p0a-formal-r2-l2", "after": "v1.1"} and not res["config_json_in_lab"]
    for v in ("v1", "v1.1"):
        assert sorted(p.name for p in (lab / "boards" / v).iterdir()) == sorted(F.LAB_FILES)
        assert (lab / "boards" / v / "board.json").read_bytes() == (root / "boards" / v / "board.json").read_bytes()
    assert len(list((lab / "submissions").glob("sub-*.json"))) == 4
    assert not (lab / "hidden").exists() and not (lab / "bench").exists() and not list(lab.rglob("config.json"))
    b2 = json.loads((lab / "boards" / "v1.1" / "board.json").read_text())
    api = F.api_visibility(lab, "v1.1", b2)                                         # 以实验室副本启动 bench API：匿名 404，invited 可读
    assert api["passed"], api
