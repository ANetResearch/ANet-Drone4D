"""存储配额、保留期与 `ranked_eval` 精简（D3-AC-22 存储条款、D3-AC-63"派生物按保留期清理"；M27-FR-057 至 062、FR-171；
M27-AC-010、011（配额部分）、049（保留与去重部分）；AWR-05 ADR-163 v1.3）。

缩短时钟：`RetentionPolicy(day_ns=60 s)`，即 1 天 = 1 min（M27-AC-010 测试配置）。全部用例在临时数据根上构造：

1. Run 级：transient 14 天、standard 180 天、FAILED 14 天到期即清理（只留清单、评测与登记，访问返回 697）；pinned 与运行中 Run
   不清理；`.attempts` 14 天。
2. 派生物级：bag transient 7 天、逐文件导出与 `.rrd` standard 30 天；pinned 派生物不清理；来源 Run 清理时其非 pinned 派生物同时清理；
   `.rrd`（含 evalstore）先注销后删除，注销失败不删除。
3. 发布包、静态制品与排行榜引用自动 pinned（`sync_references`），撤销后按原类别到期。
4. `--dry-run` 不删除、不写审计、不改登记；实际清理逐项写审计（路径、字节数、原因）。
5. 配额：< 80% 准入、≥ 80% 告警、≥ 95% 新 Run 保持 QUEUED 且 `blocked_by = storage`、记录 685，运行中作业不受影响；租户软配额；
   占用回落后恢复准入；存储账与逐文件字节和一致。
6. `ranked_eval`：每"提交 × test 事件"抽 1 个 Run 保留完整录制 30 天，其余评分后只留逐帧摘要、精简真值（含证据帧实例图）、
   Agent 输出、评测与预览；重复运行摘要相同时去重；抽样到期后精简；pinned 豁免；只含交付通道的摘要逐字节可复现。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from pathlib import Path

import numpy as np
import pytest

from awr.bench import retention as R
from awr.bench.retention import RankedRun, RetentionPolicy, StorageGate, Usage

MIN = 60 * 10 ** 9
P = RetentionPolicy(day_ns=MIN)
T0 = 1_760_000_000 * 10 ** 9


def _w(p: Path, data: bytes | str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    return p


def _run(root: Path, rid: str, *, event: str = "ev-01", retention: str = "standard", state: str = "COMPLETED", tenant: str = "team-a",
         derived: tuple[str, ...] = ()) -> Path:
    d = root / "dataset" / event / "runs" / rid
    _w(d / "run_manifest.json", json.dumps({"run_id": rid, "state": state, "retention": retention}))
    _w(d / "evaluation.json", "{}")
    _w(d / "raw.mcap", b"\x89MCAP" + bytes(5000))
    _w(d / "frames.idx", bytes(64))
    _w(root / "dataset" / event / "truth" / f"run_{rid}.mcap", bytes(3000))
    _w(root / "dataset" / event / "truth" / f"run_{rid}.frames.idx", bytes(40))
    for k in derived:
        if k == "files":
            _w(d / "derived" / "export_manifest.json", "{}")
            _w(d / "derived" / "uav-01" / "rgb" / "000001.jpg", bytes(700))
        elif k == "rrd":
            _w(d / "derived" / "rrd" / "obs.uav-01.rrd", bytes(900))
            _w(root / "evalstore" / "rrd" / event / rid / "truth.rrd", bytes(800))
        else:
            _w(d / "derived" / k / "uav-01.bag", bytes(1100))
    R.register_run(d, retention=retention, state=state, tenant=tenant, t0_unix_ns=T0)
    for k in derived:
        R.register_derived(d, k, now=T0)
    return d


def _audit_rows(root: Path) -> list[dict]:
    return [json.loads(x) for p in sorted((root / "audit").glob("retention-*.jsonl")) for x in p.read_text().splitlines()]


# ------------------------------------------------------------------ 1. Run 级
def test_run_level_expiry_pinned_and_active(tmp_path: Path) -> None:
    root = tmp_path
    tr = _run(root, "run-tr", retention="transient")
    st = _run(root, "run-st")
    fl = _run(root, "run-fl", state="FAILED")
    pn = _run(root, "run-pn", retention="pinned")
    ac = _run(root, "run-ac", state="RUNNING")
    _w(st / ".attempts" / "1" / "logs" / "env.log", "x" * 100)
    os.utime(st / ".attempts" / "1", ns=(T0, T0))

    res = R.retention_gc(root, policy=P, now=T0 + 14 * MIN - 1)
    assert res["deleted"] == [] and res["errors"] == []
    res = R.retention_gc(root, policy=P, now=T0 + 14 * MIN)
    assert not (tr / "raw.mcap").exists() and not (fl / "raw.mcap").exists() and not (tr / "frames.idx").exists()
    assert not (root / "dataset/ev-01/truth/run_run-tr.mcap").exists() and not (root / "dataset/ev-01/truth/run_run-fl.frames.idx").exists()
    assert {p.name for p in tr.iterdir()} == {"run_manifest.json", "evaluation.json", "retention.json"}
    assert not (st / ".attempts" / "1").exists()                                       # attempt 14 天
    assert (st / "raw.mcap").exists() and (pn / "raw.mcap").exists() and (ac / "raw.mcap").exists()
    reasons = {(x["run_id"], x["reason"]) for x in res["deleted"]}
    assert ("run-tr", "transient_expired") in reasons and ("run-fl", "failed_expired") in reasons and ("run-st", "attempt_expired") in reasons
    assert R.expired_reason(tr) == {"code": 697, "name": "ARTIFACT_EXPIRED", "detail": {"run_id": "run-tr", "what": "run",
                                                                                         "expired_unix_ns": T0 + 14 * MIN}}
    assert R.expired_reason(st) is None

    R.retention_gc(root, policy=P, now=T0 + 180 * MIN - 1)
    assert (st / "raw.mcap").exists()
    R.retention_gc(root, policy=P, now=T0 + 180 * MIN)
    assert not (st / "raw.mcap").exists()
    R.retention_gc(root, policy=P, now=T0 + 100_000 * MIN)
    assert (pn / "raw.mcap").exists() and (root / "dataset/ev-01/truth/run_run-pn.mcap").exists()
    assert (ac / "raw.mcap").exists()                                                  # 运行中 Run 不清理
    assert R.retention_gc(root, policy=P, now=T0 + 100_000 * MIN)["deleted"] == []      # 幂等


# ------------------------------------------------------------------ 2. 派生物级
def test_derived_expiry_pinned_source_and_rrd_deregister(tmp_path: Path) -> None:
    root = tmp_path
    a = _run(root, "run-a", derived=("bag", "files", "rrd", "video"))
    b = _run(root, "run-b", retention="transient", derived=("files", "video"))
    R.pin(b, "static:v3", derived="video")
    dereg: list[str] = []

    R.retention_gc(root, policy=P, now=T0 + 7 * MIN - 1, deregister_rrd=lambda p: dereg.append(p.name))
    assert (a / "derived/bag").exists()
    res = R.retention_gc(root, policy=P, now=T0 + 7 * MIN, deregister_rrd=lambda p: dereg.append(p.name))
    assert not (a / "derived/bag").exists() and (a / "derived/rrd").exists() and (a / "derived/export_manifest.json").exists()
    assert {x["reason"] for x in res["deleted"]} == {"derived_transient_expired"}
    assert R.expired_reason(a, "bag")["code"] == 697 and R.expired_reason(a, "files") is None

    # 来源 Run（transient，14 天）清理时其非 pinned 派生物同时清理；pinned 派生物保留
    res = R.retention_gc(root, policy=P, now=T0 + 14 * MIN, deregister_rrd=lambda p: dereg.append(p.name))
    assert not (b / "raw.mcap").exists() and not (b / "derived/export_manifest.json").exists() and not (b / "derived/uav-01").exists()
    assert (b / "derived/video/uav-01.bag").exists()
    assert ("run-b", "source_run_expired") in {(x["run_id"], x["reason"]) for x in res["deleted"]}

    # .rrd 注销失败 → 不删除并记错误；注销成功 → 先注销后删除（含 evalstore）
    def fail(p: Path) -> None:
        raise RuntimeError("catalog unavailable")

    res = R.retention_gc(root, policy=P, now=T0 + 30 * MIN, deregister_rrd=fail)
    assert res["errors"] and (a / "derived/rrd/obs.uav-01.rrd").exists() and (root / "evalstore/rrd/ev-01/run-a/truth.rrd").exists()
    assert not (a / "derived/export_manifest.json").exists() and not (a / "derived/video").exists()   # standard 30 天
    res = R.retention_gc(root, policy=P, now=T0 + 30 * MIN, deregister_rrd=lambda p: dereg.append(p.name))
    assert not (a / "derived/rrd").exists() and not (root / "evalstore/rrd/ev-01/run-a").exists()
    assert sorted(dereg) == ["obs.uav-01.rrd", "truth.rrd"]
    assert all(x.get("deregistered") for x in res["deleted"])
    assert (a / "raw.mcap").exists()                                                   # Run 级 standard 180 天未到
    R.retention_gc(root, policy=P, now=T0 + 10_000 * MIN)
    assert (b / "derived/video/uav-01.bag").exists()


# ------------------------------------------------------------------ 3. 引用自动 pinned
def test_references_auto_pin_and_release(tmp_path: Path) -> None:
    root = tmp_path
    a = _run(root, "run-a", retention="transient", derived=("files",))
    b = _run(root, "run-b", retention="transient")
    out = R.sync_references(root, "release:d3-ds-0.1", [("run-a", None), ("run-a", "files"), ("run-x", None)])
    assert out == {"pinned": 2, "unpinned": 0, "missing": ["run-x"]}
    R.retention_gc(root, policy=P, now=T0 + 60 * MIN)
    assert (a / "raw.mcap").exists() and (a / "derived/export_manifest.json").exists() and not (b / "raw.mcap").exists()
    assert R.load_record(a)["pinned_by"] == ["release:d3-ds-0.1"]
    out = R.sync_references(root, "release:d3-ds-0.1", [])
    assert out["unpinned"] == 2
    R.retention_gc(root, policy=P, now=T0 + 60 * MIN)
    assert not (a / "raw.mcap").exists() and not (a / "derived/export_manifest.json").exists()
    with pytest.raises(ValueError):
        R.pin(a, "user:alice")


# ------------------------------------------------------------------ 4. dry-run 与审计
def test_dry_run_and_audit(tmp_path: Path) -> None:
    root = tmp_path
    d = _run(root, "run-t", retention="transient", derived=("bag",))
    _w(d / ".attempts/1/logs/env.log", "y" * 300)
    os.utime(d / ".attempts/1", ns=(T0, T0))
    before = sorted(str(p) for p in root.rglob("*") if p.is_file())
    rec0 = (d / "retention.json").read_text()
    plan = R.retention_gc(root, policy=P, now=T0 + 20 * MIN, dry_run=True)
    assert plan["dry_run"] and plan["deleted"] and plan["bytes"] > 0
    assert sorted(str(p) for p in root.rglob("*") if p.is_file() and p.name != "retention.lock") == before
    assert not (root / "audit").exists() and (d / "retention.json").read_text() == rec0
    real = R.retention_gc(root, policy=P, now=T0 + 20 * MIN)
    assert [x["path"] for x in real["deleted"]] == [x["path"] for x in plan["deleted"]]
    assert real["bytes"] == plan["bytes"]
    rows = _audit_rows(root)
    assert [r["path"] for r in rows] == [x["path"] for x in real["deleted"]]
    assert all(r["action"] == "delete" and r["bytes"] >= 0 and r["reason"] and r["run_id"] == "run-t" for r in rows)
    assert sum(r["bytes"] for r in rows) == real["bytes"]


# ------------------------------------------------------------------ 5. 配额与准入
def test_quota_gate_warn_stop_queue_685(tmp_path: Path) -> None:
    root = tmp_path
    pol = RetentionPolicy(quota_bytes=1000, tenant_soft_bytes=300)
    used = {"n": 790, "t": {"team-a": 100, "team-b": 100}}
    gate = StorageGate(root, pol, usage_fn=lambda: Usage(used["n"], dict(used["t"])), clock=lambda: T0)
    q = [{"run_id": "r1", "tenant": "team-a", "state": "QUEUED"}, {"run_id": "r2", "tenant": "team-b", "state": "QUEUED"},
         {"run_id": "r0", "tenant": "team-a", "state": "RUNNING"}]
    a = gate.check("team-a")
    assert a.ok and not a.warn and a.reason is None
    used["n"] = 800
    assert [r["run_id"] for r in gate.admit_queue(q)] == ["r1", "r2"]
    assert gate.check().warn and gate.check().ok
    assert [r["action"] for r in _audit_rows(root)] == ["storage_warn"]

    used["n"] = 950
    adm = gate.check("team-a")
    assert not adm.ok and adm.reason["code"] == 685 and adm.reason["detail"]["why"] == "quota_stop" and adm.blocked_by == ("storage",)
    assert gate.admit_queue(q) == []
    assert gate.admit_queue(q) == []                                                   # 第二轮不重复记录
    for r in q[:2]:
        assert r["state"] == "QUEUED" and r["blocked_by"] == ["storage"] and r["reason_code"] == 685
    assert q[2] == {"run_id": "r0", "tenant": "team-a", "state": "RUNNING"}            # 运行中作业不受影响
    blocked = [r for r in _audit_rows(root) if r["action"] == "storage_blocked"]
    assert [(r["run_id"], r["code"], r["why"]) for r in blocked] == [("r1", 685, "quota_stop"), ("r2", 685, "quota_stop")]

    # 回落后恢复；租户软配额只阻塞超额租户
    used["n"], used["t"]["team-a"] = 600, 300
    out = gate.admit_queue(q)
    assert [r["run_id"] for r in out] == ["r2"] and q[1]["blocked_by"] == [] and q[1]["reason_code"] is None
    assert q[0]["blocked_by"] == ["storage"] and q[0]["reason_detail"]["why"] == "tenant_soft_quota"
    used["t"]["team-a"] = 299
    assert [r["run_id"] for r in gate.admit_queue(q)] == ["r1", "r2"]


def test_storage_accounting_matches_files(tmp_path: Path) -> None:
    root = tmp_path
    a = _run(root, "run-a", derived=("bag", "files", "rrd"))
    _run(root, "run-b", tenant="team-b")
    b = R.run_bytes(a)
    assert b["raw"] == 5005 and b["frames_idx"] == 64 and b["truth"] == 3040
    assert b["derived"] == {"bag": 1100, "files": 2 + 700, "rrd": 900 + 800}
    files = [p for p in a.rglob("*") if p.is_file()]
    assert b["total"] == sum(p.stat().st_size for p in files) + 3040 + 800
    u = Usage.scan(root)
    assert u.used_bytes == sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    assert set(u.by_tenant) == {"team-a", "team-b"} and u.by_tenant["team-a"] == b["total"]
    gate = StorageGate(root, RetentionPolicy(quota_bytes=u.used_bytes * 100 // 96))
    assert not gate.check().ok                                                          # 实测占用 ≥ 95%


# ------------------------------------------------------------------ 6. ranked_eval
def _jpeg(i: int) -> bytes:
    return b"\xff\xd8" + np.random.default_rng(i).bytes(3000) + b"\xff\xd9"


def _ranked_run(root: Path, rid: str, *, frames_seed: int, event: str = "ev-test") -> Path:
    """一个已评分的评测运行：raw.mcap（交付相机与不交付的 slam_cam）、frames.idx、数据集层真值 mcap、Env Agent 层与真值层、预览。"""
    from awr.bench.recorder.channels import parse_truth_topic
    from awr.bench.recorder.frames_idx import build_frames_idx
    from awr.bench.recorder.writer import RunWriter

    d = root / "dataset" / event / "runs" / rid
    tdir = root / "dataset" / event / "truth"
    tdir.mkdir(parents=True, exist_ok=True)
    with RunWriter(d / "raw.mcap") as w, RunWriter(tdir / f"run_{rid}.mcap") as t:
        for seq in range(1, 21):
            ts = seq * 100_000_000
            for uav in ("uav-01", "uav-02"):
                w.write_camera(uav, "cam0", seq, ts, _jpeg(frames_seed * 1000 + seq + (uav == "uav-02") * 50), period_ns=100_000_000)
                w.write_camera(uav, "slam", seq, ts, _jpeg(int(hashlib.sha256(rid.encode()).hexdigest()[:6], 16) + seq), agent_delivered=False)
                t.write_camera_truth(uav, "cam0", seq, ts, {"schema": "drone4d.frame.v1", "seq": seq}, {"inst": bytes([seq]) * 64})
    build_frames_idx(d / "raw.mcap", d / "frames.idx")
    build_frames_idx(tdir / f"run_{rid}.mcap", tdir / f"run_{rid}.frames.idx", parse=parse_truth_topic)
    _w(d / "manifest.json", json.dumps({"run_id": rid, "ranked": True, "n_ticks": 20}))
    _w(d / "task.json", "{}")
    _w(d / "evaluation.json", json.dumps({"components": {"ec": 0.5}, "ranked_valid": True}))
    _w(d / "agents/uav-01/acts.jsonl", '{"k":1}\n')
    _w(d / "agents/uav-01/config.json", "{}")
    _w(d / "agents/uav-01/obs.sha256", "".join(f"{k} {hashlib.sha256(bytes([frames_seed, k])).hexdigest()}\n" for k in range(5)))
    rep = {"uav": "uav-01", "k_obs": 3, "apply_tick": 4, "t_apply_ns": 1, "report": {"kind": "sighting", "evidence": [
        {"frame_seq": 7, "bbox_xywh": [1, 2, 3, 4]}, {"uav": "uav-02", "frame_seq": 12, "point_xy": [5, 6]}]}}
    _w(d / "reports.jsonl", json.dumps(rep) + "\n")
    _w(d / "net/records.jsonl", '{"m":1}\n')
    np.savez_compressed(_w(d / "truth/fleet.npz", b""), t_ns=np.arange(3))
    for n in ("l1.jsonl", "l2.jsonl", "contacts.jsonl", "run_seed.json"):
        _w(d / "truth" / n, "{}\n")
    _w(d / "truth/instances_full.npz", bytes(20_000))                                 # 非精简真值（应清理）
    _w(d / "preview/uav-01/000010.jpg", bytes(500))
    _w(d / "logs/env.log", "x" * 1000)
    return d


@pytest.fixture()
def ranked(tmp_path: Path):
    pytest.importorskip("mcap_protobuf")
    pytest.importorskip("foxglove_schemas_protobuf")
    root = tmp_path
    # 1 个提交 × 1 个 test 事件 × 2 个种子 × 2 次重复；种子 1 的第 2 次重复帧流不同（非确定性），其余重复与首次相同
    spec = [("s0r0", 0, 0, 10), ("s0r1", 0, 1, 10), ("s1r0", 1, 0, 20), ("s1r1", 1, 1, 21)]
    runs = [RankedRun(_ranked_run(root, f"sub-x-{n}", frames_seed=fs), "sub-x", "ev-test", s, r) for n, s, r, fs in spec]
    return root, runs


def test_frames_digest_reproducible_and_delivered_only(ranked) -> None:
    _, runs = ranked
    a, b, c = (R.frames_digest(r.run_dir) for r in runs[:3])
    assert a == b and a != c
    lines = a.decode().splitlines()
    assert lines[0] == "#drone4d.bench.frames_digest.v1"
    rows = [x.split("\t") for x in lines[1:]]
    assert {r[1] for r in rows} == {"cam0", "obs"}                                      # slam（agent_delivered = false）不计
    cam = [r for r in rows if r[1] == "cam0"]
    assert len(cam) == 2 * 20 and len(rows) == 2 * 20 + 5
    want = hashlib.sha256(_jpeg(10 * 1000 + 7)).hexdigest()
    assert ["uav-01", "cam0", "7", want, str(len(_jpeg(10 * 1000 + 7)))] in cam


@pytest.mark.parametrize(("seed", "want"), [(7, "sub-x-s1r0"), (1, "sub-x-s0r1")])       # 抽中独立运行 / 抽中重复运行
def test_ranked_eval_sample_compact_dedup(ranked, seed: int, want: str) -> None:
    root, runs = ranked
    rng = random.Random(seed)
    pick = sorted(runs, key=lambda r: (r.seed_index, r.rep))[random.Random(seed).randrange(4)].run_dir.name
    assert pick == want
    out = R.finalize_ranked_eval(runs, root=root, policy=P, now=T0, rng=rng, tenant="team-a")
    assert [k for k, v in out.items() if v["phase"] == "sampled"] == [pick]
    by = {r.run_dir.name: r.run_dir for r in runs}
    s = by[pick]
    assert (s / "raw.mcap").exists() and (root / f"dataset/ev-test/truth/run_{pick}.mcap").exists()

    for name, d in by.items():
        if name == pick:
            continue
        assert not (d / "raw.mcap").exists() and not (d / "frames.idx").exists() and not (d / "logs").exists()
        assert not (d / "truth/instances_full.npz").exists() and not (root / f"dataset/ev-test/truth/run_{name}.mcap").exists()
        assert not (root / f"dataset/ev-test/truth/run_{name}.frames.idx").exists()
        for keep in ("manifest.json", "task.json", "evaluation.json", "reports.jsonl", "net/records.jsonl", "agents/uav-01/acts.jsonl",
                     "agents/uav-01/obs.sha256"):
            assert (d / keep).exists(), (name, keep)
        assert R.load_record(d)["retention"] == "ranked_eval" and R.load_record(d)["tenant"] == "team-a"
        assert R.expired_reason(d, "full")["code"] == 697

    # 去重：s0r1 与 s0r0 帧流相同 → 只登记 dup_of；s1r1 与 s1r0 不同 → 独立保存摘要
    first = by["sub-x-s0r0"] if pick != "sub-x-s0r0" else None
    dup = by["sub-x-s0r1"]
    if pick != "sub-x-s0r1":
        rec = R.load_record(dup)["ranked_eval"]
        assert rec["dup_of"] == "sub-x-s0r0" and rec["phase"] == "compacted"
        assert not (dup / "frames.digest").exists() and not (dup / "truth").exists() and not (dup / "preview").exists()
        assert not (dup / "truth_min.json").exists()
    if first is not None:
        _check_full_compact(first, rid="sub-x-s0r0")
        assert R.load_record(first)["ranked_eval"]["frames_digest_sha256"] == hashlib.sha256((first / "frames.digest").read_bytes()).hexdigest()
    if pick != "sub-x-s1r1":
        d = by["sub-x-s1r1"]
        assert "dup_of" not in R.load_record(d)["ranked_eval"] and (d / "frames.digest").exists()
    for name, d in by.items():
        if name != pick:
            assert R.run_bytes(d, root)["total"] < 0.15e9

    # 抽样录制 30 天后精简（含去重）
    R.retention_gc(root, policy=P, now=T0 + 30 * MIN - 1)
    assert (s / "raw.mcap").exists()
    res = R.retention_gc(root, policy=P, now=T0 + 30 * MIN)
    assert not (s / "raw.mcap").exists() and {x["reason"] for x in res["deleted"]} == {"ranked_eval_sample_expired"}
    rr = R.load_record(s)["ranked_eval"]
    assert rr["phase"] == "compacted"
    if pick == "sub-x-s0r1":
        assert rr["dup_of"] == "sub-x-s0r0" and not (s / "frames.digest").exists()
    else:
        _check_full_compact(s, rid=pick)
    assert any(r["reason"] == "ranked_eval_compact" for r in _audit_rows(root))
    assert R.retention_gc(root, policy=P, now=T0 + 10_000 * MIN)["deleted"] == []       # 精简后的评测运行长期保留


def _check_full_compact(d: Path, *, rid: str) -> None:
    assert (d / "frames.digest").exists() and (d / "preview/uav-01/000010.jpg").exists()
    for n in ("fleet.npz", "l1.jsonl", "l2.jsonl", "contacts.jsonl", "run_seed.json"):
        assert (d / "truth" / n).exists()
    tm = json.loads((d / "truth_min.json").read_text())
    assert {f["path"] for f in tm["files"]} == {f"truth/{n}" for n in ("fleet.npz", "l1.jsonl", "l2.jsonl", "contacts.jsonl", "run_seed.json")}
    ev = {(e["uav"], e["cam"], e["seq"]) for e in tm["evidence"]}
    assert ev == {("uav-01", "cam0", 7), ("uav-02", "cam0", 12)}                       # report 证据帧的实例图
    from awr.bench.recorder.truth import unpack_camera_gt

    rec, blocks = unpack_camera_gt((d / "truth/evidence/uav-02/cam0/000012.gt").read_bytes())
    assert rec["seq"] == 12 and blocks["inst"] == bytes([12]) * 64


def test_ranked_eval_pinned_exempt(ranked) -> None:
    root, runs = ranked
    R.pin(runs[0].run_dir, "leaderboard:d3-board-1")
    out = R.finalize_ranked_eval(runs[:2], root=root, policy=P, now=T0, rng=random.Random(0))
    pinned = runs[0].run_dir
    assert (pinned / "raw.mcap").exists()
    if out[pinned.name]["phase"] != "sampled":
        assert out[pinned.name]["phase"] == "pinned"
    R.retention_gc(root, policy=P, now=T0 + 10_000 * MIN)
    assert (pinned / "raw.mcap").exists() and (root / f"dataset/ev-test/truth/run_{pinned.name}.mcap").exists()


# ------------------------------------------------------------------ 命令行
def test_cli_gc_usage_pin(tmp_path: Path, capsys) -> None:
    root = tmp_path
    d = _run(root, "run-a", retention="transient")
    assert R.main(["--data-root", str(root), "pin", "run-a", "--ref", "static:v1"]) == 0
    assert R.load_record(d)["pinned_by"] == ["static:v1"]
    assert R.main(["--data-root", str(root), "unpin", "run-a", "--ref", "static:v1"]) == 0
    capsys.readouterr()
    assert R.main(["--data-root", str(root), "gc", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and out["n_deleted"] > 0 and (d / "raw.mcap").exists()       # 时钟为当前时刻：已过 14 天
    assert R.main(["--data-root", str(root), "usage"]) == 0
    u = json.loads(capsys.readouterr().out)
    assert u["admitting"] and u["by_tenant"]["team-a"] > 0 and u["quota_bytes"] == 10 * 10 ** 12
