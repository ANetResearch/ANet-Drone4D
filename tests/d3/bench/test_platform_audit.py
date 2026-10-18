"""平台复现审计 `platform_audit`（ADR-177；M27-FR-172；D3-AC-17 末项）：逐帧摘要、每事件抽样、复跑比对、冻结与解冻。

前半部分以夹具运行目录（`awr.agentsdk.env.recorder` 布局）与替身复跑验证摘要、抽样与冻结逻辑；最后一项在合成世界上以真实
`run_job`（docker 替身 + Fast 档）首跑与复跑，验证缺省复跑路径逐帧一致，并以注入的平台偏差验证冻结。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

from awr.bench import platform_audit as pa
from awr.bench.boardstore import append_rows
from awr.bench.submissions import SubmissionStore
from awr.bench.subworker import process_one

UAVS = ("uav-01", "uav-02")
VERSIONS = {"env": "d3-env@0.3.0", "image.gz": "sha256:" + "1" * 64, "python": "3.10.12", "numpy": "1.26.4"}


def _ev(ec: float) -> dict:
    return {"components": {"ec": ec, "eu": 0.25, "ao": 0.5, "ce": 0.125}, "ranked": True, "ranked_valid": True, "invalid_reasons": []}


def _fake_run(d: Path, run_id: str, *, n: int = 20, salt: str = "", versions: dict | None = None, ec: float = 0.5) -> Path:
    """按 Env 运行目录布局写一个确定性的夹具 Run（salt 不同则内容不同）。"""
    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(int(pa._sha(salt.encode())[:8], 16))
    for u in UAVS:
        a = d / "agents" / u
        a.mkdir(parents=True, exist_ok=True)
        (a / "obs.sha256").write_text("".join(f"{k} {pa._sha(f'{salt}|{u}|obs|{k}'.encode())}\n" for k in range(n)))
        (a / "acts.jsonl").write_text("".join(json.dumps({"k": k, "act": {"nav": [k, 0.0]}, "acks": [], "apply_tick": k + 1}, sort_keys=True,
                                                          separators=(",", ":")) + "\n" for k in range(n)))
    (d / "reports.jsonl").write_text(json.dumps({"uav": "uav-01", "k_obs": 3, "report": {"kind": "sighting", "salt": salt}}) + "\n")
    (d / "net").mkdir(exist_ok=True)
    (d / "net" / "records.jsonl").write_text("".join(json.dumps({"src": "uav-01", "k": k}) + "\n" for k in range(5)))
    (d / "truth").mkdir(exist_ok=True)
    (d / "truth" / "l1.jsonl").write_text("".join(json.dumps({"uav": "uav-02", "seq": s, "vis": 0.5}) + "\n" for s in range(4)))
    t = np.arange(n + 1, dtype=np.int64) * 100_000_000
    pos = rng.normal(size=(n + 1, 2, 3))
    np.savez_compressed(d / "truth" / "fleet.npz", uavs=np.array(UAVS), t_ns=t, pos=pos, vel=np.zeros((n + 1, 2, 3), np.float32),
                        yaw=np.zeros((n + 1, 2), np.float32), nav=pos, status=np.full((n + 1, 2), "ARMED", "U10"))
    man = {"schema": "drone4d.bench.env_run.v1", "run_id": run_id, "event": {"id": "ev"}, "pose_sha256": pa._sha(f"pose|{salt}".encode()),
           "wall_s": 12.3, "versions": dict(versions or VERSIONS)}
    (d / "manifest.json").write_text(json.dumps(man, indent=1))
    (d / "evaluation.json").write_text(json.dumps({"schema": "drone4d.bench.evaluation.v1", **_ev(ec)}))
    return d


def _board(root: Path, name: str = "bv-0001-aaaaaa", events=("ev-a", "ev-b"), policies=("static_guard", "track_share"), seeds=(11, 12),
           dev: bool = False) -> Path:
    """榜单版本目录：每个 (事件, 基线, 种子) 一个首跑（行带 run_dir）。"""
    b = root / "boards" / name
    b.mkdir(parents=True)
    rs = {e: {"schema": "drone4d.bench.run_spec.v1", "event": {"id": e, "version": 1}, "fleet": [], "obs_level": "L1"} for e in events}
    cfg = {"run_spec": rs[events[0]], "run_specs": rs, "seeds": list(seeds), "events_root": str(root / "events"), "worlds_root": str(root / "worlds"),
           "physics": "realistic", "gz": {}, "track": {"fleet_ref": "2x-x500", "obs_level": "L1", "dev": dev}, "track_id": "std2-l1",
           "lane": "rendezvous_handoff", "event_type": "rendezvous_handoff", "event": events[0]}
    (b / "config.json").write_text(json.dumps(cfg))
    rows = []
    for e in events:
        for p in policies:
            for s in seeds:
                rd = _fake_run(root / "first" / e / f"{p}-s{s}", f"{e}-{p}-s{s}", salt=f"{e}|{p}|{s}")
                rows.append({"entry_id": f"baseline/{p}", "team": "D3 reference", "display_name": p, "baseline": True, "track": "std2-l1",
                             "lane": "rendezvous_handoff", "event_type": "rendezvous_handoff", "event": e, "seed": s, "run_dir": str(rd),
                             "evaluation": _ev(0.5)})
    rows.append({**rows[0], "entry_id": "team-x/sub-abc", "baseline": False, "submission_id": "sub-abc"})       # 非基线行不抽样
    append_rows(b, rows)
    return b


class _Replay:
    """替身复跑：复制首跑目录（逐字节一致），可按事件注入偏差。"""

    def __init__(self, root: Path, tamper=None) -> None:
        self.root, self.tamper, self.jobs = root, tamper or {}, []

    def __call__(self, job: pa.ReplayJob) -> Path:
        self.jobs.append(job)
        src = self.root / "first" / job.event / f"{job.policy}-s{job.seed}"
        shutil.copytree(src, job.run_dir)
        m = json.loads((job.run_dir / "manifest.json").read_text())
        assert m["run_id"] == job.run_id                       # 复跑沿用首跑 run_id
        m["wall_s"] = 99.0                                     # 墙钟量不进入摘要
        (job.run_dir / "manifest.json").write_text(json.dumps(m))
        f = self.tamper.get(job.event)
        if f:
            f(job.run_dir)
        return job.run_dir


def _flip_obs(k: int):
    def f(d: Path) -> None:
        p = d / "agents" / "uav-01" / "obs.sha256"
        lines = p.read_text().splitlines()
        lines[k] = f"{k} {'0' * 64}"
        p.write_text("\n".join(lines) + "\n")
    return f


# ------------------------------------------------------------------ 摘要
def test_digest_streams_roundtrip_and_compare(tmp_path: Path) -> None:
    ref = _fake_run(tmp_path / "a", "r1")
    fd = pa.frames_digest(ref)
    assert set(fd.streams) == {"obs/uav-01", "obs/uav-02", "act/uav-01", "act/uav-02", "pose", "report", "net", "truth/l1"}
    assert len(fd.streams["obs/uav-01"]) == 20 and len(fd.streams["pose"]) == 21 and fd.head["run_id"] == "r1"
    p = pa.write_digest(fd, ref / pa.DIGEST_NAME)
    back = pa.load_digest(p)
    assert back.streams == fd.streams and back.head == fd.head and back.sha256() == fd.sha256()
    assert json.loads(p.read_text().splitlines()[0][2:])["schema"] == pa.DIGEST_SCHEMA
    # 墙钟量与 run_id 之外完全相同 → 一致
    b = _fake_run(tmp_path / "b", "r1")
    m = json.loads((b / "manifest.json").read_text())
    m["wall_s"] = 1.0
    (b / "manifest.json").write_text(json.dumps(m))
    assert pa.compare_digests(fd, pa.frames_digest(b)) == []
    # 单帧观测不同：报首个不一致的 k
    _flip_obs(7)(b)
    mm = pa.compare_digests(fd, pa.frames_digest(b))
    assert [(x["kind"], x["stream"], x["k"]) for x in mm] == [("frame", "obs/uav-01", 7)]
    # 动作流截断：count；位姿逐 tick 与 gz 位姿摘要；平台镜像摘要
    c = _fake_run(tmp_path / "c", "r1", n=18, salt="x", versions={**VERSIONS, "image.gz": "sha256:" + "2" * 64})
    kinds = {(x["kind"], x.get("stream")) for x in pa.compare_digests(fd, pa.frames_digest(c))}
    assert {("frame", "pose"), ("frame", "obs/uav-02"), ("pose_sha256", None), ("platform_digest", None)} <= kinds
    pd = next(x for x in pa.compare_digests(fd, pa.frames_digest(c)) if x["kind"] == "platform_digest")
    assert pd["keys"] == ["image.gz"]
    shutil.rmtree(c / "agents" / "uav-02")
    kinds = {(x["kind"], x.get("stream")) for x in pa.compare_digests(fd, pa.frames_digest(c))}
    assert ("stream_missing", "obs/uav-02") in kinds and ("stream_missing", "act/uav-02") in kinds
    d = _fake_run(tmp_path / "d", "r1", n=22)
    assert {(x["kind"], x["stream"]) for x in pa.compare_digests(fd, pa.frames_digest(d))} >= {("count", "obs/uav-01"), ("count", "act/uav-01")}


def test_sampling_covers_every_event_and_rotates_daily() -> None:
    rows = [{"entry_id": f"baseline/{p}", "event": e, "seed": s} for e in ("e1", "e2", "e3") for p in ("a", "b", "c") for s in (1, 2)]
    picks = {}
    for day in range(1, 11):
        got = pa.sample_runs(rows, board_version="bv-0001-aaaaaa", date=f"2026-10-{day:02d}")
        assert sorted(got) == ["e1", "e2", "e3"] and all(len(v) == 1 for v in got.values())
        picks[day] = tuple((r["entry_id"], r["seed"]) for v in got.values() for r in v)
    assert len(set(picks.values())) > 1                        # 逐日轮换
    assert pa.sample_runs(rows, board_version="bv-0001-aaaaaa", date="2026-10-01") == pa.sample_runs(rows, board_version="bv-0001-aaaaaa",
                                                                                                    date="2026-10-01")
    two = pa.sample_runs(rows, board_version="bv-0001-aaaaaa", date="2026-10-01", per_event=2)
    assert all(len(v) == 2 and v[0] is not v[1] for v in two.values())
    only = pa.sample_runs(rows, board_version="bv-0001-aaaaaa", date="2026-10-01", has_ref=lambda r: r["entry_id"] == "baseline/c" and r["seed"] == 2)
    assert all([(r["entry_id"], r["seed"]) for r in v] == [("baseline/c", 2)] for v in only.values())
    none = pa.sample_runs(rows, board_version="bv-0001-aaaaaa", date="2026-10-01", has_ref=lambda r: r["event"] != "e2")
    assert none["e2"] == [] and len(none["e1"]) == 1


# ------------------------------------------------------------------ 审计与冻结
def test_consistent_audit_keeps_board_open(tmp_path: Path) -> None:
    b = _board(tmp_path)
    rp = _Replay(tmp_path)
    logs: list[str] = []
    rep = pa.audit_board(b, date="2026-10-18", replay=rp, log=logs.append)
    assert rep["status"] == "consistent" and rep["frozen"] is None and not pa.is_frozen(b)
    assert rep["events"] == ["ev-a", "ev-b"] and len(rep["runs"]) == 2                 # 每个 test 事件 1 个
    assert {r["event"] for r in rep["runs"]} == {"ev-a", "ev-b"}
    assert all(j.entry_id.startswith("baseline/") for j in rp.jobs)                    # 只复跑基线
    assert all(j.seed in (11, 12) and j.seed_index in (0, 1) for j in rp.jobs)
    assert all(r["consistent"] and r["reference"] == "run_dir" and r["digest_sha256"] == r["reference_sha256"] for r in rep["runs"])
    assert "seed" not in json.dumps(rep["runs"]).replace("seed_index", "")             # 报告只含种子序号
    for r in rep["runs"]:                                                              # 一致：复跑目录只留摘要、清单与评测
        assert sorted(p.name for p in Path(r["run_dir"]).iterdir()) == sorted(pa.KEEP_ON_PASS)
    assert json.loads((b / "audit" / "2026-10-18.json").read_text())["status"] == "consistent"
    pa.ensure_open(b)
    # 同日再次调用返回已有报告，不再复跑；force 重跑
    assert pa.audit_board(b, date="2026-10-18", replay=rp, log=logs.append) == rep and len(rp.jobs) == 2
    pa.audit_board(b, date="2026-10-18", replay=rp, force=True, log=logs.append)
    assert len(rp.jobs) == 4
    assert len((b / "audit" / "log.jsonl").read_text().splitlines()) == 2


def test_inconsistency_freezes_new_evaluations(tmp_path: Path) -> None:
    b = _board(tmp_path)
    logs: list[str] = []
    rep = pa.audit_board(b, date="2026-10-18", replay=_Replay(tmp_path, {"ev-b": _flip_obs(7)}), log=logs.append, now_ns=123)
    assert rep["status"] == "inconsistent" and rep["frozen"] == {"since_unix_ns": 123, "audit": "2026-10-18"}
    bad = [r for r in rep["runs"] if not r["consistent"]]
    assert [r["event"] for r in bad] == ["ev-b"]
    assert [(m["kind"], m["stream"], m["k"]) for m in bad[0]["mismatches"]] == [("frame", "obs/uav-01", 7)]
    assert (Path(bad[0]["run_dir"]) / "agents").is_dir()                               # 不一致：复跑目录完整保留
    assert any(m.startswith("ALERT platform_audit") for m in logs)
    st = pa.freeze_state(b)
    assert st["schema"] == pa.FREEZE_SCHEMA and st["board_version"] == b.name
    assert st["events"][0]["reasons"] == [{"event": "ev-b", "entry_id": bad[0]["entry_id"], "seed_index": bad[0]["seed_index"], "kinds": ["frame"]}]
    with pytest.raises(pa.BoardFrozenError):
        pa.ensure_open(b)
    # 次日审计一致：冻结保持（须人工调查后解冻），报告标注仍冻结
    rep2 = pa.audit_board(b, date="2026-10-19", replay=_Replay(tmp_path), log=logs.append)
    assert rep2["status"] == "consistent" and rep2["frozen"]["since_unix_ns"] == 123 and pa.is_frozen(b)
    # 再次不一致：追加原因，首次冻结时间不变
    pa.audit_board(b, date="2026-10-20", replay=_Replay(tmp_path, {"ev-a": _flip_obs(0)}), log=logs.append)
    st = pa.freeze_state(b)
    assert st["since_unix_ns"] == 123 and [e["audit"] for e in st["events"]] == ["2026-10-18", "2026-10-20"]
    with pytest.raises(ValueError):
        pa.unfreeze(b, by="maintainer", note=" ")
    lifted = pa.unfreeze(b, by="maintainer", note="gz plugin rebuilt with a different compiler; board re-pinned")
    assert not pa.is_frozen(b) and lifted["lifted"]["by"] == "maintainer"
    hist = [json.loads(x) for x in (b / "audit" / "freeze_history.jsonl").read_text().splitlines()]
    assert len(hist) == 1 and hist[0]["since_unix_ns"] == 123
    pa.ensure_open(b)
    with pytest.raises(ValueError):
        pa.unfreeze(b, by="maintainer", note="again")


@pytest.mark.parametrize(("case", "kinds"), [
    ("platform", {"platform_digest"}),
    ("pose", {"pose_sha256"}),
    ("evaluation", {"evaluation"}),
    ("truncated", {"count"}),
])
def test_each_kind_of_platform_drift_freezes(tmp_path: Path, case: str, kinds: set[str]) -> None:
    def tamper(d: Path) -> None:
        m = json.loads((d / "manifest.json").read_text())
        if case == "platform":                                     # 人为替换插件构建（M24-AC-029）
            m["versions"]["image.gz"] = "sha256:" + "f" * 64
        elif case == "pose":
            m["pose_sha256"] = "0" * 64
        elif case == "evaluation":
            (d / "evaluation.json").write_text(json.dumps(_ev(0.75)))
        else:
            p = d / "agents" / "uav-02" / "acts.jsonl"
            p.write_text("".join(p.read_text().splitlines(keepends=True)[:-1]))
        (d / "manifest.json").write_text(json.dumps(m))

    b = _board(tmp_path, events=("ev-a",))
    rep = pa.audit_board(b, date="2026-10-18", replay=_Replay(tmp_path, {"ev-a": tamper}), log=lambda _m: None)
    assert rep["status"] == "inconsistent" and pa.is_frozen(b)
    assert {m["kind"] for m in rep["runs"][0]["mismatches"]} == kinds


def test_replay_failure_retries_once_then_freezes(tmp_path: Path) -> None:
    b = _board(tmp_path, events=("ev-a",))
    ok = _Replay(tmp_path)
    calls = {"n": 0}

    def flaky(job):
        calls["n"] += 1
        if calls["n"] == 1:
            job.run_dir.mkdir(parents=True)
            raise RuntimeError("gz instance died")
        return ok(job)

    rep = pa.audit_board(b, date="2026-10-18", replay=flaky, log=lambda _m: None)
    assert rep["status"] == "consistent" and calls["n"] == 2                       # 平台原因重跑一次后一致
    def broken(job):
        raise RuntimeError("gz instance died")

    rep = pa.audit_board(b, date="2026-10-19", replay=broken, log=lambda _m: None)
    assert rep["status"] == "inconsistent" and pa.is_frozen(b)
    assert rep["runs"][0]["mismatches"][0]["kind"] == "replay_failed" and len(rep["runs"][0]["mismatches"][0]["errors"]) == 2


def test_snapshot_references_and_missing_reference(tmp_path: Path) -> None:
    b = _board(tmp_path, events=("ev-a", "ev-b"))
    out = pa.snapshot_references(b)
    assert out == {"written": 8, "kept": 0, "missing": []}
    assert pa.snapshot_references(b)["kept"] == 8                                   # 已固化的不覆盖
    rp = _Replay(tmp_path)
    rows = [json.loads(x) for x in (b / "rows.jsonl").read_text().splitlines()]
    for r in rows:
        r["run_dir"] = str(tmp_path / "gone" / r["event"])                        # 首跑原始目录已清理：以固化摘要为准
    (b / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    rep = pa.audit_board(b, date="2026-10-18", replay=rp, log=lambda _m: None)
    assert rep["status"] == "consistent" and {r["reference"] for r in rep["runs"]} == {"snapshot"}
    # ev-b 的固化摘要与运行目录都不可用：该事件记 no_reference，审计未完成但不冻结
    shutil.rmtree(b / "digests" / "ev-b")
    rep = pa.audit_board(b, date="2026-10-19", replay=rp, log=lambda _m: None)
    assert rep["status"] == "incomplete" and rep["no_reference"] == ["ev-b"] and not pa.is_frozen(b)
    assert [r["event"] for r in rep["runs"]] == ["ev-a"]


def test_reference_from_runs_root_and_index(tmp_path: Path) -> None:
    """proto_board 产出：行不带 run_dir，首跑目录取 runs_root/<policy>-s<seed> 或 index.json 所指位置。"""
    b = _board(tmp_path, events=("ev-a",), policies=("track_share",), seeds=(11,))
    rows = [json.loads(x) for x in (b / "rows.jsonl").read_text().splitlines()]
    src = Path(rows[0]["run_dir"])
    for r in rows:
        r.pop("run_dir")
    (b / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    cfg = pa.load_config(b)
    row = rows[0]
    assert pa.reference_digest(b, cfg, row) is None
    assert pa.reference_digest(b, cfg, row, runs_root=src.parent)[1] == "run_dir"
    (b / "index.json").write_text(json.dumps([{"entry_id": row["entry_id"], "event": "ev-a", "seed": 11, "evaluation": str(src / "evaluation.json")}]))
    fd, how = pa.reference_digest(b, cfg, row)
    assert how == "run_dir" and fd.streams == pa.frames_digest(src).streams
    pa.write_digest(pa.frames_digest(src), src / pa.DIGEST_NAME)                    # 保留策略留下的 frames.digest 优先于现算
    shutil.rmtree(src / "agents")
    assert len(pa.reference_digest(b, cfg, row)[0].streams["obs/uav-01"]) == 20


def test_ranked_only_unless_dev(tmp_path: Path) -> None:
    b = _board(tmp_path, events=("ev-a",))
    rows = [json.loads(x) for x in (b / "rows.jsonl").read_text().splitlines()]
    for r in rows:
        r["evaluation"]["invalid_reasons"] = ["physics_not_ranked"]
    (b / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert pa.audit_board(b, date="2026-10-18", replay=_Replay(tmp_path), log=lambda _m: None)["status"] == "empty"
    cfg = json.loads((b / "config.json").read_text())
    cfg["track"]["dev"] = True
    (b / "config.json").write_text(json.dumps(cfg))
    assert pa.audit_board(b, date="2026-10-19", replay=_Replay(tmp_path), log=lambda _m: None)["status"] == "consistent"


def test_subworker_holds_submissions_of_frozen_board(tmp_path: Path) -> None:
    frozen = _board(tmp_path, "bv-0001-aaaaaa", events=("ev-a",))
    _board(tmp_path, "bv-0002-bbbbbb", events=("ev-a",))
    pa.audit_board(frozen, date="2026-10-18", replay=_Replay(tmp_path, {"ev-a": _flip_obs(3)}), log=lambda _m: None)
    assert pa.is_frozen(frozen)
    st = SubmissionStore(tmp_path / "submissions")
    mk = {"tracks": {"std2-l1"}, "lanes": {"rendezvous_handoff"}}
    req = {"track": "std2-l1", "image_digest": "sha256:" + "a" * 64, "entry": {"cmd": ["python", "-m", "x"]}}
    s1 = st.create(req, owner="alice", team="team-a", board_version="bv-0001-aaaaaa", **mk)["submission_id"]
    s2 = st.create(req, owner="bob", team="team-b", board_version="bv-0002-bbbbbb", **mk)["submission_id"]
    logs: list[str] = []
    assert process_one(tmp_path, docker="/bin/false", log=logs.append) == s2                # 冻结版本的提交被跳过
    assert st.get(s2)["state"] == "REJECTED" and st.get(s1)["state"] == "RECEIVED"
    assert process_one(tmp_path, docker="/bin/false", log=logs.append) is None
    assert st.get(s1)["state"] == "RECEIVED"
    pa.unfreeze(frozen, by="maintainer", note="investigated")
    assert process_one(tmp_path, docker="/bin/false", log=logs.append) == s1
    assert st.get(s1)["state"] == "REJECTED"                                               # 解冻后照常进入流水线（此处扫描替身拒绝）


def test_cli_run_status_unfreeze(tmp_path: Path, monkeypatch, capsys) -> None:
    b = _board(tmp_path, events=("ev-a",))
    (tmp_path / "boards" / "current").write_text(b.name + "\n")
    (b / "board.json").write_text("{}")
    root = ["--data-root", str(tmp_path)]
    monkeypatch.setattr(pa, "replay_baseline", _Replay(tmp_path))
    assert pa.main([*root, "run", "--date", "2026-10-18"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "consistent"
    monkeypatch.setattr(pa, "replay_baseline", _Replay(tmp_path, {"ev-a": _flip_obs(1)}))
    assert pa.main([*root, "run", "--date", "2026-10-19"]) == 1
    capsys.readouterr()
    assert pa.main([*root, "status"]) == 0
    assert json.loads(capsys.readouterr().out)[b.name]["audit"] == "2026-10-19"
    assert pa.main([*root, "unfreeze", b.name, "--by", "maintainer", "--note", "checked"]) == 0
    assert pa.main([*root, "unfreeze", b.name, "--by", "maintainer", "--note", "checked"]) == 2
    shutil.rmtree(tmp_path / "first")
    for r in [json.loads(x) for x in (b / "rows.jsonl").read_text().splitlines()]:
        assert not Path(r["run_dir"]).exists()
    assert pa.main([*root, "run", "--date", "2026-10-20"]) == 3                          # 无首跑摘要：未完成全覆盖
    assert pa.main(["--data-root", str(tmp_path / "nothing"), "run"]) == 2


# ------------------------------------------------------------------ 端到端：真实 run_job 首跑与复跑
def _env_conftest():
    p = Path(__file__).resolve().parents[1] / "env" / "conftest.py"
    spec = importlib.util.spec_from_file_location("_d3_env_conftest", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                                     # 缺 open3d 或 zstandard 时整模块跳过
    return mod


try:
    synth_event = _env_conftest().synth_event
except pytest.skip.Exception:                                        # pragma: no cover - 依赖缺失时只跳过端到端项
    synth_event = None


@pytest.mark.skipif(synth_event is None, reason="open3d / zstandard missing (.venv-d3)")
def test_end_to_end_replay_through_run_job(synth_event, tmp_path: Path, monkeypatch, request) -> None:
    from awr.bench.runner import run_job

    w, ev = synth_event
    sdk = Path(__file__).resolve().parents[3] / "tools/sdk/drone4d/src"
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "docker-state"))
    sock = Path(tempfile.mkdtemp(prefix="d3p", dir="/tmp"))           # AF_UNIX 路径上限：套接字放短目录
    request.addfinalizer(lambda: shutil.rmtree(sock, ignore_errors=True))
    monkeypatch.setenv("AWR_AGENT_SOCK_ROOT", str(sock))
    monkeypatch.setenv("PYTHONPATH", f"{sdk}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).resolve().parents[1] / 'env' / 'fake_docker.py'} \"$@\"\n")
    docker.chmod(0o755)
    event = ev.parent.name
    rs = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "audit"}, "world": {"id": w.name}, "event": {"id": event, "version": 1},
          "fleet": [{"uav_id": "uav-01", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [-10.0, 50.0, 0.0], "yaw_deg": 0.0}},
                    {"uav_id": "uav-02", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [10.0, 50.0, 0.0], "yaw_deg": 0.0}}],
          "policy": {"kind": "reference", "name": "track_share"}, "obs_level": "L1", "physics": "realistic", "duration_s": 12.0, "seeds": [11]}
    b = tmp_path / "d3" / "boards" / "proto-audit"
    b.mkdir(parents=True)
    cfg = {"run_spec": rs, "seeds": [11], "events_root": str(ev.parent.parent), "worlds_root": str(w.parent), "physics": "kinematic", "gz": {},
           "track": {"fleet_ref": "2x-x500", "obs_level": "L1", "dev": True}, "track_id": "std2-l1", "lane": "rendezvous_handoff",
           "event_type": "rendezvous_handoff", "event": event}
    (b / "config.json").write_text(json.dumps(cfg))
    first = b / "runs" / "track_share-s11"                             # proto_board 布局
    r = run_job(rs, policy={"kind": "reference", "name": "track_share"}, seed=11, run_id=f"{event}-track_share-s11", run_dir=first,
                worlds_root=w.parent, events_root=ev.parent.parent, physics="kinematic", docker=str(docker))
    evaluation = json.loads((first / "evaluation.json").read_text())
    append_rows(b, [{"entry_id": "baseline/track_share", "team": "D3 reference", "display_name": "Track and share", "baseline": True,
                     "track": "std2-l1", "lane": "rendezvous_handoff", "event_type": "rendezvous_handoff", "event": event, "seed": 11,
                     "evaluation": {k: evaluation[k] for k in ("components", "ranked_valid", "invalid_reasons")}}])
    assert pa.snapshot_references(b)["written"] == 1
    ref = pa.load_digest(next((b / "digests").rglob("*.digest")))
    assert ref.head["run_id"] == f"{event}-track_share-s11" and ref.head["pose_sha256"] == r["pose_sha256"]
    assert len(ref.streams["obs/uav-01"]) > 50 and len(ref.streams["act/uav-02"]) > 50 and ref.streams["pose"]
    rep = pa.audit_board(b, date="2026-10-18", docker=str(docker), log=lambda _m: None)       # 缺省复跑：run_job
    assert rep["status"] == "consistent", rep["runs"]
    assert rep["runs"][0]["frames"] == {s: len(v) for s, v in sorted(ref.streams.items())}
    # 注入平台偏差：复跑派生出不同的随机流（以种子偏移模拟）→ 逐帧摘要不一致并冻结
    def drift(job: pa.ReplayJob) -> Path:
        from dataclasses import replace
        return pa.replay_baseline(replace(job, seed=job.seed + 1))

    rep = pa.audit_board(b, date="2026-10-19", replay=drift, docker=str(docker), log=lambda _m: None)
    assert rep["status"] == "inconsistent" and pa.is_frozen(b)
    assert {m["kind"] for m in rep["runs"][0]["mismatches"]} & {"frame", "count", "pose_sha256"}
