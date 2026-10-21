"""最小基准闭环一键脚本 `tools/bench/d3/mbl_e2e.py`（D3-AC-56 的本机部分）：阶段编排、秘密种子、断点续跑、原型榜发布与内部提交、
逐项判定。

前几项以 Run 替身（按 Env 运行目录布局写 `manifest.json` 与 `evaluation.json`，物理档 realistic，gz 迭代数按锁步不变量）驱动
真实的榜单发布（boardstore）、提交存储与内部提交流水线（subworker.process_one，docker 替身扫描），覆盖 L1 + L2 两组 × 3 基线 ×
秘密种子 × 重复；最后一项在合成世界上以真实 `run_job`（docker 替身、隔离 Agent 子进程、线协议与 obs.v1 审计、Fast 档）跑完全部阶段。
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SDK = REPO / "tools/sdk/drone4d/src"
_spec = importlib.util.spec_from_file_location("mbl_e2e", REPO / "tools/bench/d3/mbl_e2e.py")
mbl = importlib.util.module_from_spec(_spec)
sys.modules["mbl_e2e"] = mbl
_spec.loader.exec_module(mbl)

L1 = REPO / "configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json"
L2 = REPO / "configs/bench.d3/crop-handoff-001.l2-3uav.run_spec.json"
EVENT_SRC = REPO / "configs/events.d3/crop-handoff-001/event.yaml"
DIGEST = "sha256:" + "a" * 64
TAPE = "c" * 64
AUDIT = {"schema_checked": 0, "schema_valid": 0, "schema_violations": 0, "unknown_fields": 0, "jpeg_segment_violations": 0,
         "lidar_tag_violations": 0, "decode_violations": 0, "passed": True}


def _h(*parts) -> float:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


class FakeRunJob:
    """run_job 替身：写 Env 运行目录（manifest.json 含锁步计数与白名单审计摘要）与 evaluation.json；记录每次调用（含种子）。"""

    def __init__(self, *, fail: dict[str, int] | None = None, slow: set[str] = frozenset(), unknown: set[str] = frozenset(),
                 noeval: set[str] = frozenset()) -> None:
        self.calls: list[dict] = []
        self.fail = dict(fail or {})                 # run_id 片段 → 失败次数（-1 为一直失败）
        self.slow, self.unknown, self.noeval = set(slow), set(unknown), set(noeval)

    def __call__(self, rs, *, policy, seed, run_id, run_dir, worlds_root, events_root, physics=None, gz=None, agent_image="d3-agent-py:dev",
                 docker="docker", duration_s=None, **_kw) -> dict:
        self.calls.append({"run_id": run_id, "seed": seed, "policy": policy, "physics": physics, "obs_level": rs["obs_level"], "gz": gz})
        for frag, n in list(self.fail.items()):
            if frag in run_id and n != 0:
                self.fail[frag] = n - 1 if n > 0 else n
                raise RuntimeError(f"platform fault in {run_id}")
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        dur = float(duration_s if duration_s is not None else rs.get("duration_s", 360))
        n = round(dur * 10)
        name = policy.get("name") or policy.get("image_digest")
        uavs = [f["uav_id"] for f in rs["fleet"]]
        audit = {**AUDIT, "schema_checked": 2 * n * len(uavs) + 2 * len(uavs), "schema_valid": 2 * n * len(uavs) + 2 * len(uavs)}
        if any(u in run_id for u in self.unknown):
            audit = {**audit, "unknown_fields": 1, "passed": False}
        pose = hashlib.sha256(f"{name}|{seed}|{rs['obs_level']}".encode()).hexdigest()
        man = {"schema": "drone4d.bench.env_run.v1", "run_id": run_id, "event": {"id": rs["event"]["id"], "version": 1, "content_sha256": TAPE},
               "obs_level": rs["obs_level"], "physics": physics, "tier": "realistic" if physics == "realistic" else "kinematic", "n_ticks": n,
               "pose_sha256": pose, "tier_info": {"ticks": n, "iterations": 25 * n, "gz_rtf_mean": 1.0, "restarts": 0} if physics == "realistic" else {},
               "wall_s": round(dur * 1.5, 2), "agents": {u: {"timeouts": 0} for u in uavs}, "obs_audit": audit}
        (run_dir / "manifest.json").write_text(json.dumps(man))
        base = {"static_guard": 0.2, "coverage_scan": 0.35, "track_share": 0.5}.get(name, 0.6)
        comp = {m: round(base + 0.1 * _h(name, seed, m, rs["obs_level"]), 6) for m in ("ec", "eu", "ao", "ce")}
        ev = {"schema": "drone4d.bench.evaluation.v1", "run_id": run_id, "ranked": True, "ranked_valid": True,
              "invalid_reasons": [] if physics == "realistic" else ["physics_not_ranked"], "components": comp}
        if not any(x in run_id for x in self.noeval):
            (run_dir / "evaluation.json").write_text(json.dumps(ev))
        wall = dur * (4.0 if any(s in run_id for s in self.slow) else 1.8)
        return {"run_id": run_id, "run_dir": str(run_dir), "run_wall_s": round(wall, 1), "eval_wall_s": 0.2, "pose_sha256": pose, "components": comp,
                "ranked_valid": True, "obs_audit": {k: audit[k] for k in ("schema_checked", "schema_violations", "unknown_fields", "passed")},
                "agents": {u: {"acts": n, "protocol_errors": 0, "exited": "bye", "exit_code": 0} for u in uavs}}


class FakeCompile:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, src, world_dir, *, opts) -> dict:
        self.calls += 1
        assert opts.strict is True and opts.ranked is True
        out = Path(opts.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "tape.json").write_text(json.dumps({"content_sha256": TAPE}))
        (out / "plan.json").write_text(json.dumps({"event_type": "rendezvous_handoff", "version": 1}))
        gates = [{"gate": g, "status": "pass", "errors": [], "warnings": []} for g in (1, 2, 3)]
        return {"event_id": "crop-handoff-001", "version": 1, "status": "compiled", "strict": True, "reason": None, "gates": gates,
                "tape": {"content_sha256": TAPE, "n_ticks": 3600, "n_actors": 43}}


def _ok_world(world_dir, *, deep=False) -> dict:
    return {"path": str(world_dir), "world_id": Path(world_dir).name, "ok": True, "deep": deep, "errors": [], "warnings": [],
            "info": {"rules_checked": 100}, "seconds": 0.1}


@pytest.fixture(autouse=True)
def _lidar_backend_env(monkeypatch) -> None:
    """`Round` 把配置的 LiDAR 后端写入进程环境（AWR_ENV_LIDAR_BACKEND）；每个测试结束后恢复，不影响其他测试的 Env。"""
    monkeypatch.setenv("AWR_ENV_LIDAR_BACKEND", "cpu")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "docker-state"))
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).with_name('fake_docker.py')} \"$@\"\n")
    docker.chmod(0o755)
    w = tmp_path / "worlds" / "oryahovo-crop1"
    w.mkdir(parents=True)
    (w / "world.json").write_text(json.dumps({"id": "oryahovo-crop1", "contentVersion": "cv-1"}))
    cfg = {"schema": mbl.CONFIG_SCHEMA, "round": "t-mbl", "event": {"source": str(EVENT_SRC)},
           "groups": [{"obs_level": "L1", "run_spec": str(L1), "jobs": 3}, {"obs_level": "L2", "run_spec": str(L2), "jobs": 2}],
           "seeds": 3, "repeats": 2, "fleet_vehicle": "p600_bench", "board": {"version": "t-mbl", "current": "L2", "n_boot": 200},
           "submission": {"image": DIGEST, "cmd": ["python", "-m", "drone4d.serve", "close_track.agent:Agent"], "group": "L2", "team": "d3-internal",
                          "owner": "alice", "smoke_s": 5.0}}
    p = tmp_path / "mbl.json"
    p.write_text(json.dumps(cfg))
    return {"tmp": tmp_path, "config": p, "docker": str(docker), "out": tmp_path / "round", "data": tmp_path / "bench",
            "worlds": tmp_path / "worlds"}


def _main(e: dict, *extra: str, run_job=None, compile_event=None) -> int:
    hooks = mbl.Hooks(validate_world=_ok_world, compile_event=compile_event or FakeCompile(), run_job=run_job, docker=e["docker"])
    return mbl.main(["--config", str(e["config"]), "--out", str(e["out"]), "--data-root", str(e["data"]), "--worlds-root", str(e["worlds"]),
                     *extra], hooks=hooks)


def _scan(paths: list[Path], seeds: list[int]) -> list[str]:
    hits = []
    for p in paths:
        t = p.read_text(errors="replace").lower()
        hits += [f"{p}:{s}" for s in seeds if str(s) in t or f"{s:x}" in t]
    return hits


def test_full_round_two_groups_board_and_submission(env: dict) -> None:
    rj, comp = FakeRunJob(), FakeCompile()
    assert _main(env, run_job=rj, compile_event=comp) == 0
    out, data = env["out"], env["data"]
    rep = json.loads((out / "mbl_e2e.json").read_text())
    assert rep["schema"] == "drone4d.bench.mbl_e2e.v1" and rep["passed"] and not rep["formal"] and not rep["formal_passed"]
    assert {c["id"]: c["passed"] for c in rep["checks"]} == {k: True for k in ("world_package", "event_strict_compile", "gz_lockstep",
                                                                               "runs_completed", "evaluations", "board_baselines",
                                                                               "internal_submission", "obs_v1_audit", "wall_clock", "secret_seeds")}
    # 计划：2 组 × 3 基线 × 3 种子 × 2 重复 = 36；另有锁步预检 1、内部提交冒烟 1 与正式 3（每种子 1 次）
    assert len(rep["runs"]) == 36 and all(r["status"] == "COMPLETED" for r in rep["runs"])
    formal = [c for c in rj.calls if "-preflight" not in c["run_id"] and not c["run_id"].startswith("sub-")]
    assert len(formal) == 36 and len(rj.calls) == 36 + 1 + 1 + 3
    assert all(c["physics"] == "realistic" and c["gz"] == {"launcher": "docker", "image": "d3-gz:dev"} for c in formal)
    # 秘密种子：CSPRNG、仅存 evalstore（0600）；正式 Run 与提交流水线用同一种子集，重复使用同一种子
    sec = out / "evalstore" / "secrets.json"
    assert stat.S_IMODE(sec.stat().st_mode) == 0o600 and stat.S_IMODE(sec.parent.stat().st_mode) == 0o700
    doc = json.loads(sec.read_text())
    seeds = doc["seeds"]["crop-handoff-001"]
    assert len(set(seeds)) == 3 and all(0 < s < 2 ** 63 for s in seeds)
    assert {c["seed"] for c in formal} == set(seeds)
    sub_seeds = [c["seed"] for c in rj.calls if c["run_id"].startswith("sub-")]
    assert sub_seeds[0] == 0 and sub_seeds[1:] == seeds                                  # 冒烟用公开种子 0
    for r in rep["runs"]:
        same = [c["seed"] for c in formal if c["run_id"] == r["run_id"]]
        assert same == [seeds[r["seed_index"]]]
        assert r["sim_s"] == 360.0 and r["wall_ratio"] == pytest.approx(1.8, abs=0.01) and r["lockstep_ok"] is True
        man = json.loads((out / "runs" / r["key"] / "run_manifest.json").read_text())
        want = hmac.new(bytes.fromhex(doc["k_eval"]), f"{r['run_id']}:{seeds[r['seed_index']]}".encode(), hashlib.sha256).hexdigest()
        assert man["seed_commit"] == want and man["ranked"] is True and "seed" not in man and "seed" not in man["spec"]
        assert man["split"] == "dev" and man["spec"]["fleet"][0]["vehicle_ref"] == "p600_bench"
    # 种子明文不出现在检查点、日志、记录、报告、评测行、榜单、详情与提交文档中
    texts = [out / "e2e_state.json", out / "e2e.log", out / "mbl_e2e.json", *sorted((out / "records").glob("*.json")),
             *sorted((data / "boards").glob("*/rows.jsonl")), *sorted((data / "boards").glob("*/board.json")),
             *sorted((data / "boards").glob("*/details.json")), *sorted((data / "submissions").glob("sub-*.json"))]
    assert len(texts) > 40 and not _scan(texts, seeds)
    assert all(str(s) not in json.dumps(rj.calls[0]) for s in seeds)                     # 预检用公开种子
    # 原型榜：每组一个版本，3 个基线参考行（四项指标与 95% 置信区间），L2 为当前版本，内部提交上榜
    assert (data / "boards" / "current").read_text().strip() == "t-mbl-l2"
    for lv, n_ent in (("l1", 3), ("l2", 4)):
        b = json.loads((data / "boards" / f"t-mbl-{lv}" / "board.json").read_text())
        assert [t["track"] for t in b["tracks"]] == [f"std3-{lv}"] and [x["lane"] for x in b["lanes"]] == ["rendezvous_handoff"]
        assert len(b["entries"]) == n_ent
        for e in b["entries"]:
            for m in ("ec", "eu", "ao", "ce"):
                lo, hi = e["metrics"][m]["ci95"]
                assert lo <= e["metrics"][m]["value"] <= hi
            if e["baseline"]:
                assert e["metrics"]["ec"]["repeat_delta"] == 0.0                         # 同种子两次重复（替身确定）
        cfg = json.loads((data / "boards" / f"t-mbl-{lv}" / "config.json").read_text())
        assert cfg["seeds"] == seeds and cfg["repeats"] == 2 and cfg["track"]["prototype"] and not cfg["track"]["dev"]
        assert "seeds" not in cfg["run_spec"] and cfg["run_spec"]["fleet"][0]["vehicle_ref"] == "p600_bench"
    sub = rep["submission"]
    assert sub["state"] == "DONE" and sub["listed"] and sub["board_version"] == "t-mbl-l2"
    assert sub["history"] == ["RECEIVED", "SCANNING", "VALIDATING", "EVALUATING", "SCORING", "DONE"]
    assert len(rep["submission_runs"]) == 4 and {x["kind"] for x in rep["submission_runs"]} == {"smoke", "submission"}
    assert rep["checks"][7]["detail"]["totals"]["unknown_fields"] == 0 and rep["checks"][7]["detail"]["totals"]["schema_checked"] > 0
    assert len(rep["representative_runs"]) == 6 and rep["repeat_pose_identical"] == {"groups": 18, "identical": 18}
    # 续跑：已完成的阶段与 Run 全部跳过；强制重新发布榜单时保留内部提交的评测行与条目
    n0, c0 = len(rj.calls), comp.calls
    assert _main(env, "--force", "board", run_job=rj, compile_event=comp) == 0
    assert len(rj.calls) == n0 and comp.calls == c0
    b = json.loads((data / "boards" / "t-mbl-l2" / "board.json").read_text())
    assert any(e["entry_id"] == f"d3-internal/{sub['submission_id']}" for e in b["entries"])
    rows = [json.loads(x) for x in (data / "boards" / "t-mbl-l2" / "rows.jsonl").read_text().splitlines()]
    assert sum(not r["baseline"] for r in rows) == 3 and sum(r["baseline"] for r in rows) == 18
    assert json.loads((out / "e2e_state.json").read_text())["stages"]["submit"]["result"]["submission_id"] == sub["submission_id"]


def test_resume_after_failures_reruns_only_unfinished(env: dict) -> None:
    comp = FakeCompile()
    bad = FakeRunJob(fail={"l1-coverage_scan-k01-p2": -1, "l2-static_guard-k00-p1": 1},   # 前者两次都失败，后者首次失败后重跑成功
                     noeval={"l2-coverage_scan-k01-p1"})                                       # run_job 返回但缺 evaluation.json
    assert _main(env, run_job=bad, compile_event=comp) == 1
    out = env["out"]
    st = json.loads((out / "e2e_state.json").read_text())
    assert st["stages"]["runs"]["status"] == "failed" and "l1-coverage_scan-k01-p2" in st["stages"]["runs"]["error"]
    assert "board" not in st["stages"] and "submit" not in st["stages"]
    rec = json.loads((out / "records" / "l1-coverage_scan-k01-p2.json").read_text())
    assert rec["status"] == "FAILED" and rec["attempts"] == 2 and "platform fault" in rec["errors"][0]["error"]
    rec = json.loads((out / "records" / "l2-static_guard-k00-p1.json").read_text())
    assert rec["status"] == "COMPLETED" and rec["attempts"] == 2
    rec = json.loads((out / "records" / "l2-coverage_scan-k01-p1.json").read_text())
    assert rec["status"] == "FAILED" and "outputs" in rec["errors"][-1]["error"]
    rep = json.loads((out / "mbl_e2e.json").read_text())
    assert not rep["passed"] and {c["id"] for c in rep["checks"] if not c["passed"]} >= {"runs_completed", "board_baselines", "internal_submission"}
    # 模拟中断：删去一个已完成 Run 的记录、篡改另一个的 evaluation.json → 续跑时只重做这三个
    (out / "records" / "l2-track_share-k02-p2.json").unlink()
    ev = out / "runs" / "l1-static_guard-k00-p1" / "evaluation.json"
    ev.write_text(ev.read_text().replace('"ranked": true', '"ranked": true '))
    good = FakeRunJob()
    assert _main(env, run_job=good, compile_event=comp) == 0
    redo = sorted(c["run_id"].removeprefix("t-mbl-") for c in good.calls if c["run_id"].startswith("t-mbl-"))
    assert redo == ["l1-coverage_scan-k01-p2", "l1-static_guard-k00-p1", "l2-coverage_scan-k01-p1", "l2-track_share-k02-p2"]
    assert comp.calls == 1                                                                  # 世界、编译、预检不重做
    assert json.loads((out / "mbl_e2e.json").read_text())["passed"]
    log = (out / "e2e.log").read_text()
    assert "[world] done at" in log and "(resume: skipped)" in log and "32 completed earlier (skipped), 4 to run" in log


def test_checks_fail_on_wall_clock_and_unknown_fields(env: dict) -> None:
    rj = FakeRunJob(slow={"l2-track_share-k01-p1"}, unknown={"l1-static_guard-k02-p2"})
    assert _main(env, run_job=rj) == 1
    rep = json.loads((env["out"] / "mbl_e2e.json").read_text())
    failed = {c["id"]: c for c in rep["checks"] if not c["passed"]}
    assert set(failed) == {"wall_clock", "obs_v1_audit"}
    assert failed["wall_clock"]["detail"]["over"] == ["l2-track_share-k01-p1"]
    assert failed["obs_v1_audit"]["detail"]["bad"] == ["l1-static_guard-k02-p2"]
    assert failed["obs_v1_audit"]["detail"]["totals"]["unknown_fields"] == 1


def test_fingerprint_config_errors_and_plan(env: dict, capsys) -> None:
    assert _main(env, "--stages", "world,compile", run_job=FakeRunJob()) == 0
    st = json.loads((env["out"] / "e2e_state.json").read_text())
    assert set(st["stages"]) == {"world", "compile"} and st["stages"]["compile"]["result"]["gates"][2]["status"] == "pass"
    assert _main(env, "--seeds", "4", run_job=FakeRunJob()) == 3                         # 指纹不同：拒绝续跑
    assert "fingerprint" in capsys.readouterr().err
    assert _main(env, "--stages", "board", run_job=FakeRunJob()) == 1                    # 前置阶段未完成
    assert "requires" in json.loads((env["out"] / "e2e_state.json").read_text())["stages"]["board"]["error"]
    assert _main(env, "--baselines", "nope") == 2 and _main(env, "--stages", "deploy") == 2 and _main(env, "--groups", "L3") == 2
    capsys.readouterr()
    assert _main(env, "--plan") == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["runs"]) == 36 and plan["runs"][0]["key"] == "l1-static_guard-k00-p1" and not plan["formal"]
    assert all("seed" not in r for r in plan["runs"])
    # 缺省配置即正式口径：L1 + L2 × 3 基线 × 10 种子 × 2 重复
    cfg = mbl.load_config(None)
    assert mbl.is_formal(cfg) and cfg["wall_ratio_max"] == 3.0 and cfg["seeds"] == 10 and cfg["repeats"] == 2
    assert not mbl.is_formal(mbl.load_config(None, {"duration_s": 30.0}))
    assert not mbl.is_formal(mbl.load_config(None, {"groups": ["L1"]}))


def test_lockstep_check_counts_preroll_ticks() -> None:
    """锁步不变量按档内 tick 计：回合 200 tick + 预滚 20 tick（t_pre = 2 s）时 gz 自报迭代数应为 25 × 220（dmax 实测形态，B10）。"""
    man = {"physics": "realistic", "tier": "realistic", "n_ticks": 200,
           "tier_info": {"ticks": 220, "iterations": 5500, "restarts": 0, "preroll": {"n_ticks": 20, "t_pre_s": 2.0}}}
    r = mbl.lockstep_check(man, 10, 250)
    assert r["ok"] and r["preroll_ticks"] == 20 and r["backend_ticks"] == 220
    assert not mbl.lockstep_check({**man, "tier_info": {**man["tier_info"], "iterations": 5000}}, 10, 250)["ok"]
    assert not mbl.lockstep_check({**man, "tier_info": {**man["tier_info"], "ticks": 200}}, 10, 250)["ok"]
    assert mbl.lockstep_check({**man, "tier_info": {"ticks": 200, "iterations": 5000}}, 10, 250)["ok"]       # 无预滚
    assert not mbl.lockstep_check({**man, "physics": "kinematic"}, 10, 250)["applicable"]
    # 公开冒烟种子 0 不参与脱敏（预检与冒烟的错误文本保持原样），秘密种子照常替换
    assert mbl.redact("[Errno 30] Read-only file system", [0]) == "[Errno 30] Read-only file system"
    assert mbl.redact("seed 123456789 / 75bcd15", [0, 123456789]) == "seed <seed> / <seed>"


def test_formal_config_file(tmp_path: Path, monkeypatch) -> None:
    """B10 正式口径配置 `configs/bench.d3/mbl-formal.yaml`：正式口径 120 个 Run、Realistic 档 gz 锁步、轮次与榜单版本命名、
    本轮专用镜像标签；组内并行度不进入配置指纹（续跑时可调整）。"""
    monkeypatch.chdir(REPO)
    path = REPO / "configs/bench.d3/mbl-formal.yaml"
    cfg = mbl.load_config(path)
    assert mbl.is_formal(cfg) and cfg["physics"] == "realistic" and cfg["gz"]["launcher"] == "docker" and cfg["wall_ratio_max"] == 3.0
    rnd = mbl.Round(cfg, tmp_path / "out", worlds_root=REPO / "worlds", data_root=tmp_path / "br")
    plan = rnd.plan()
    assert len(plan) == 120 and len({p["run_id"] for p in plan}) == 120
    assert all(p["run_id"].startswith("b10-formal-r2-") for p in plan) and cfg["fleet_vehicle"] == "p600_bench"
    assert {(p["group"], p["policy"]) for p in plan} == {(g, b) for g in ("L1", "L2") for b in mbl.BASELINES}
    assert [rnd.board_version(g) for g in ("L1", "L2")] == ["p0a-formal-r2-l1", "p0a-formal-r2-l2"]
    assert all(f["vehicle_ref"] == "p600_bench" for g in ("L1", "L2") for f in rnd.specs[g]["fleet"])
    assert cfg["board"]["current"] == "L2" and cfg["submission"]["group"] == "L2" and cfg["submission"]["worker"] == "inline"
    assert cfg["agent_image"].endswith(":b10") and cfg["gz"]["image"].endswith(":b10") and cfg["submission"]["image"].endswith(":b10")
    assert all(rnd.specs[g]["ranked"] and rnd.specs[g]["duration_s"] == 360 for g in ("L1", "L2"))
    faster = mbl.load_config(path)
    for g in faster["groups"]:
        g["jobs"] = 2
    assert mbl.fingerprint(faster) == mbl.fingerprint(cfg)
    assert cfg["lidar_backend"] == "warp" and os.environ["AWR_ENV_LIDAR_BACKEND"] == "warp"      # L2 网格 LiDAR 用 M25 GPU 产品路径
    other = mbl.load_config(path)
    other["lidar_backend"] = "cpu"
    assert mbl.fingerprint(other) != mbl.fingerprint(cfg)                                           # 后端进入指纹（续跑不能换后端）
    with pytest.raises(mbl.ConfigError, match="lidar_backend"):
        mbl.load_config(path, {"lidar_backend": "auto"})


def test_world_and_compile_gates_stop_the_round(env: dict) -> None:
    def bad_world(world_dir, *, deep=False):
        return {**_ok_world(world_dir), "ok": False, "errors": [{"rule": "V-GLB-08", "where": "world.json", "message": "license"}]}

    hooks = mbl.Hooks(validate_world=bad_world, compile_event=FakeCompile(), run_job=FakeRunJob(), docker=env["docker"])
    args = ["--config", str(env["config"]), "--out", str(env["out"]), "--data-root", str(env["data"]), "--worlds-root", str(env["worlds"])]
    assert mbl.main(args, hooks=hooks) == 1
    st = json.loads((env["out"] / "e2e_state.json").read_text())
    assert st["stages"]["world"]["status"] == "failed" and "V-GLB-08" in st["stages"]["world"]["error"] and "compile" not in st["stages"]

    def lenient(src, world_dir, *, opts):
        d = FakeCompile()(src, world_dir, opts=opts)
        d["gates"][2] = {"gate": 3, "status": "fail", "errors": [{"code": "E-SEM"}], "warnings": []}
        return {**d, "status": "failed", "reason": {"code": 613}}

    shutil.rmtree(env["out"])
    hooks = mbl.Hooks(validate_world=_ok_world, compile_event=lenient, run_job=FakeRunJob(), docker=env["docker"])
    assert mbl.main(args, hooks=hooks) == 1
    st = json.loads((env["out"] / "e2e_state.json").read_text())
    assert st["stages"]["compile"]["status"] == "failed" and "gate" in st["stages"]["compile"]["error"] and "lockstep" not in st["stages"]


# ------------------------------------------------------------------ 端到端：真实 run_job（docker 替身、Fast 档、合成世界）
def test_end_to_end_real_run_job(synth_event, tmp_path: Path, monkeypatch, request) -> None:
    w, ev = synth_event
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "docker-state"))
    sock = Path(tempfile.mkdtemp(prefix="d3m", dir="/tmp"))                    # AF_UNIX 路径上限：套接字放短目录
    request.addfinalizer(lambda: shutil.rmtree(sock, ignore_errors=True))
    monkeypatch.setenv("AWR_AGENT_SOCK_ROOT", str(sock))
    monkeypatch.setenv("PYTHONPATH", f"{SDK}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).with_name('fake_docker.py')} \"$@\"\n")
    docker.chmod(0o755)
    event = json.loads((ev / "compile_report.json").read_text())["event_id"]
    rs = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "mbl"}, "world": {"id": w.name}, "event": {"id": event, "version": 1},
          "fleet": [{"uav_id": "uav-01", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [-10.0, 50.0, 0.0], "yaw_deg": 0.0}},
                    {"uav_id": "uav-02", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [10.0, 50.0, 0.0], "yaw_deg": 0.0}}],
          "policy": {"kind": "reference", "name": "track_share"}, "obs_level": "L1", "physics": "realistic", "duration_s": 6.0, "seeds": [1],
          "ranked": True}
    (tmp_path / "rs.json").write_text(json.dumps(rs))
    src = tmp_path / "evsrc"
    src.mkdir()
    (src / "event.json").write_text((ev.parent / "event.json").read_text())

    def compile_copy(s, world_dir, *, opts):
        # 合成事件门 3 在 strict 下为 613（E01，示意声明）：本用例以夹具的编译产物代替，只验证编排与产物落点
        shutil.copytree(ev, opts.out)
        d = json.loads((ev / "compile_report.json").read_text())
        return {**d, "strict": True, "gates": [{**g, "status": "pass"} for g in d["gates"]], "status": "compiled"}

    cfg = {"schema": mbl.CONFIG_SCHEMA, "round": "e2e", "world": {"id": w.name}, "event": {"source": str(src / "event.json")},
           "groups": [{"obs_level": "L1", "run_spec": str(tmp_path / "rs.json"), "jobs": 3}], "seeds": 2, "repeats": 2, "physics": "kinematic",
           "lockstep": {"preflight_s": 4.0, "policy": "track_share", "group": "L1"}, "board": {"version": "e2e", "current": "L1", "n_boot": 200},
           "submission": {"image": DIGEST, "cmd": ["python", "-m", "drone4d.serve", "--baseline", "track_share"], "group": "L1", "team": "t1",
                          "owner": "bob", "smoke_s": 3.0}}
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    hooks = mbl.Hooks(validate_world=_ok_world, compile_event=compile_copy, docker=str(docker))
    out, data = tmp_path / "round", tmp_path / "bench"
    rc = mbl.main(["--config", str(tmp_path / "cfg.json"), "--out", str(out), "--data-root", str(data), "--worlds-root", str(w.parent)], hooks=hooks)
    rep = json.loads((out / "mbl_e2e.json").read_text())
    failed = {c["id"] for c in rep["checks"] if not c["passed"]}
    assert rc == 1 and failed == {"gz_lockstep"}, rep["checks"]                         # Fast 档开发轮：锁步条款不适用，其余全部通过
    assert len(rep["runs"]) == 3 * 2 * 2 and all(r["status"] == "COMPLETED" for r in rep["runs"])
    for r in rep["runs"]:
        assert r["obs_audit"]["passed"] and r["obs_audit"]["unknown_fields"] == 0 and r["obs_audit"]["schema_checked"] > 100
        assert r["sim_s"] == 6.0 and r["wall_s"] > 0 and r["wall_ratio"] < 3.0
        assert (out / "runs" / r["key"] / "raw.mcap").exists() and (out / "runs" / r["key"] / "agents" / "obs_audit.json").exists()
        assert json.loads((out / "runs" / r["key"] / "run_manifest.json").read_text())["spec"]["physics"] == "fast"
    assert rep["submission"]["state"] == "DONE" and rep["submission"]["listed"]
    b = json.loads((data / "boards" / "e2e-l1" / "board.json").read_text())
    assert {e["entry_id"] for e in b["entries"]} == {"baseline/static_guard", "baseline/coverage_scan", "baseline/track_share",
                                                     f"t1/{rep['submission']['submission_id']}"}
    assert not (data / "boards" / "current").exists()                                    # 开发榜不设为当前版本
    seeds = json.loads((out / "evalstore" / "secrets.json").read_text())["seeds"][event]
    assert rep["checks"][-1]["passed"] and not _scan([out / "e2e.log", out / "mbl_e2e.json", data / "boards" / "e2e-l1" / "details.json"], seeds)
