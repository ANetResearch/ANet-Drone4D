"""内部提交流水线（AWR-17 §18.23；D3-AC-56 的本机部分）：扫描、冒烟、正式评测与评分，撤回与上榜（docker 替身 + Fast 档合成世界）。"""

from __future__ import annotations

import functools
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from awr.agentsdk.schemas import bench_validator
from awr.bench.boardstore import append_rows, publish
from awr.bench.runner import run_job
from awr.bench.submissions import SubmissionStore
from awr.bench.subworker import process_one

SDK = Path(__file__).resolve().parents[3] / "tools/sdk/anet4d/src"
GOOD = "sha256:" + "a" * 64
ENTRY = {"cmd": ["python", "-m", "anet4d.serve", "--baseline", "track_share"], "env": {"MODE": "eval"}}


def _ev(ec: float) -> dict:
    return {"components": {"ec": ec, "eu": 0.1, "ao": 0.4, "ce": 0.2}, "ranked_valid": True, "invalid_reasons": []}


@pytest.fixture()
def root(synth_event, tmp_path: Path, monkeypatch, request) -> Path:
    w, ev = synth_event
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "docker-state"))
    sock = Path(tempfile.mkdtemp(prefix="d3a", dir="/tmp"))                    # AF_UNIX 路径上限：套接字放短目录
    request.addfinalizer(lambda: shutil.rmtree(sock, ignore_errors=True))
    monkeypatch.setenv("AWR_AGENT_SOCK_ROOT", str(sock))
    monkeypatch.setenv("PYTHONPATH", f"{SDK}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    d = tmp_path / "d3"
    b = d / "boards" / "proto-t"
    b.mkdir(parents=True)
    rs = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "t"}, "world": {"id": w.name}, "event": {"id": ev.parent.name, "version": 1},
          "fleet": [{"uav_id": "uav-01", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [-10.0, 50.0, 0.0], "yaw_deg": 0.0}},
                    {"uav_id": "uav-02", "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [10.0, 50.0, 0.0], "yaw_deg": 0.0}}],
          "policy": {"kind": "reference", "name": "track_share"}, "obs_level": "L1", "physics": "realistic", "duration_s": 20.0, "seeds": [11, 12]}
    cfg = {"run_spec": rs, "seeds": [11, 12], "events_root": str(ev.parent.parent), "worlds_root": str(w.parent), "physics": "kinematic", "gz": {},
           "track": {"fleet_ref": "2x-x500", "obs_level": "L1", "dev": True}, "track_id": "std2-l1", "lane": "rendezvous_handoff",
           "event_type": "rendezvous_handoff", "event": ev.parent.name}
    (b / "config.json").write_text(json.dumps(cfg))
    append_rows(b, [{"entry_id": "baseline/static_guard", "team": "D3 reference", "display_name": "Static guard", "baseline": True, "track": "std2-l1",
                     "lane": "rendezvous_handoff", "event_type": "rendezvous_handoff", "event": ev.parent.name, "seed": k, "evaluation": _ev(0.5)}
                    for k in range(2)])
    publish(b, n_boot=200)
    return d


def _docker(tmp_path: Path) -> str:
    p = tmp_path / "docker"
    p.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).with_name('fake_docker.py')} \"$@\"\n")
    p.chmod(0o755)
    return str(p)


def _submit(st: SubmissionStore, image: str = GOOD, entry: dict | None = None, team: str = "team-a") -> str:
    return st.create({"track": "std2-l1", "image_digest": image, "entry": entry or ENTRY}, owner="alice", team=team, board_version="proto-t",
                     tracks={"std2-l1"}, lanes={"rendezvous_handoff"})["submission_id"]


def test_pipeline_done_and_board(root: Path, tmp_path: Path) -> None:
    st = SubmissionStore(root / "submissions")
    sid = _submit(st)
    st.set_visibility(sid, "board")
    logs: list[str] = []
    assert process_one(root, docker=_docker(tmp_path), smoke_s=5.0, n_boot=200, log=logs.append) == sid
    d = st.get(sid)
    assert d["state"] == "DONE", logs
    assert [h["state"] for h in d["_meta"]["history"]] == ["RECEIVED", "SCANNING", "VALIDATING", "EVALUATING", "SCORING", "DONE"]
    view = {k: v for k, v in d.items() if not k.startswith("_")}
    assert not list(bench_validator("submission").iter_errors(view))
    lane = d["result"]["lanes"][0]
    assert lane["runs"] == {"total": 2, "valid": 0, "invalid": 2, "platform_rerun": 0}            # Fast 档：physics_not_ranked
    runs = sorted(p.name for p in (root / "dataset").glob("*/runs/*"))
    assert runs == [f"{sid}-r0", f"{sid}-r1"]                                                       # 运行名不含种子值
    board = json.loads((root / "boards/proto-t/board.json").read_text())
    ids = {e["entry_id"] for e in board["entries"]}
    assert ids == {"baseline/static_guard", f"team-a/{sid}"}
    argv = json.loads(next((tmp_path / "docker-state").glob(f"d3-agent-{sid}-r0-uav-01.argv.json")).read_text())
    assert argv["image"] == GOOD and argv["env"]["MODE"] == "eval"
    assert process_one(root, docker=_docker(tmp_path)) is None                                     # 队列为空


def test_scan_and_smoke_rejections(root: Path, tmp_path: Path) -> None:
    st = SubmissionStore(root / "submissions")
    s1 = _submit(st, image="sha256:" + "0" * 64)
    s2 = _submit(st, entry={"cmd": ["python", "-c", "pass"]})
    fast = functools.partial(run_job, hello_timeout_s=2.0)
    process_one(root, docker=_docker(tmp_path), run_job=fast, log=lambda m: None)
    process_one(root, docker=_docker(tmp_path), run_job=fast, smoke_s=5.0, log=lambda m: None)
    a, b = st.get(s1), st.get(s2)
    assert a["state"] == "REJECTED" and a["reason"]["code"] == 692 and a["reason"]["detail"] == {"why": "scan", "check": "image_missing"}
    assert b["state"] == "REJECTED" and b["reason"]["detail"]["why"] == "smoke"


def test_withdraw_skips_processing(root: Path, tmp_path: Path) -> None:
    st = SubmissionStore(root / "submissions")
    sid = _submit(st)
    st.withdraw(sid)
    assert process_one(root, docker=_docker(tmp_path)) is None and st.get(sid)["state"] == "WITHDRAWN"


def test_platform_failure_is_rerun_once_and_not_charged(root: Path, tmp_path: Path) -> None:
    st = SubmissionStore(root / "submissions")
    sid = _submit(st)
    calls: list[str] = []

    def flaky(rs, **kw):                                                  # r0 第一次因平台原因失败（如 gz 实例异常）
        calls.append(kw["run_id"])
        if kw["run_id"].endswith("-r0") and calls.count(kw["run_id"]) == 1:
            raise RuntimeError("gz instance lost")
        return run_job(rs, **kw)

    assert process_one(root, docker=_docker(tmp_path), run_job=flaky, smoke_s=5.0, n_boot=100, log=lambda m: None) == sid
    d = st.get(sid)
    assert d["state"] == "DONE" and d["result"]["lanes"][0]["runs"]["platform_rerun"] == 1
    assert d["result"]["lanes"][0]["runs"]["total"] == 2                                          # 重跑不计入提交方的 Run
    assert calls == [f"{sid}-smoke", f"{sid}-r0", f"{sid}-r0", f"{sid}-r1"]


# ---------------------------------------------------------------------- 镜像接入门（D3-AC-59；B25 遗留）
def _registry(root: Path, sid: str, status: str, digest: str = GOOD, **extra) -> None:
    """宿主 `regctl.py materialize` 写出的接入记录（`awr.d3.registry_materialize.v1`）。"""
    p = root / "submissions" / sid / "registry.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": "awr.d3.registry_materialize.v1", "submission_id": sid, "image_digest": digest, "status": status,
                             **extra}))


def _doc(sid: str, role: str, created_s: float = 0.0, digest: str = GOOD) -> dict:
    return {"submission_id": sid, "image_digest": digest, "_meta": {"role": role, "created_unix_ns": int(created_s * 1e9)}}


@pytest.mark.parametrize(("role", "mode", "status", "age_s", "want"), [
    ("invited", "invited", "pulled", 5, ("ready", None)),
    ("invited", "invited", "present", 5, ("ready", None)),
    ("invited", "invited", None, 5, ("wait", None)),                         # 尚未接入：留在 RECEIVED
    ("invited", "invited", "not_found", 5, ("wait", None)),
    ("invited", "invited", "pull_failed", 5, ("wait", None)),
    ("invited", "invited", "foreign_image", 5, ("reject", "foreign_image")),
    ("invited", "invited", "digest_is_manifest", 5, ("reject", "digest_is_manifest")),
    ("invited", "invited", None, 4000, ("reject", "image_missing")),         # 等待超过 1800 s
    ("invited", "invited", "not_found", 4000, ("reject", "image_missing")),
    ("researcher", "invited", None, 5, ("ready", None)),                     # 内部成员可用宿主本机构建的镜像
    ("researcher", "invited", "not_found", 5, ("wait", None)),               # 有记录时同样按记录判定
    ("researcher", "all", None, 5, ("wait", None)),                          # all：全部角色须先接入
    ("researcher", "all", "present", 5, ("ready", None)),
    ("invited", "off", None, 4000, ("ready", None)),
])
def test_registry_gate_decisions(tmp_path: Path, role: str, mode: str, status: str | None, age_s: float, want: tuple) -> None:
    from awr.bench.subworker import registry_gate

    if status is not None:
        _registry(tmp_path, "sub-x", status, local_image_id="sha256:" + "c" * 64)
    gate, why = registry_gate(tmp_path, _doc("sub-x", role), now_ns=int(age_s * 1e9), mode=mode)
    assert (gate, why and why["detail"]["check"]) == want
    if gate == "reject":
        assert why["code"] == 692 and why["detail"]["why"] == "scan"
    if want == ("reject", "image_missing"):
        assert why["detail"]["registry_status"] == (status or "none") and why["detail"]["waited_s"] == 4000.0
    if want == ("reject", "digest_is_manifest"):
        assert why["detail"]["config_digest"] == "sha256:" + "c" * 64


def test_registry_gate_ignores_record_for_another_digest(tmp_path: Path) -> None:
    from awr.bench.subworker import registry_gate

    _registry(tmp_path, "sub-x", "pulled", digest="sha256:" + "b" * 64)
    assert registry_gate(tmp_path, _doc("sub-x", "invited"), now_ns=5 * 10 ** 9)[0] == "wait"
    with pytest.raises(ValueError):
        registry_gate(tmp_path, _doc("sub-x", "invited"), now_ns=0, mode="sometimes")


def test_invited_submissions_wait_for_materialize(root: Path, tmp_path: Path) -> None:
    """受邀成员：未接入的提交不被领取（不进入 SCANNING，避免 692 image_missing 抢跑），其后的提交照常处理；他队镜像 692
    foreign_image；接入为 pulled 后才领取；等待超时 692 image_missing。"""
    st = SubmissionStore(root / "submissions")
    missing = "sha256:" + "0" * 64                                         # fake docker 中不存在：领取后在扫描阶段 image_missing
    s_wait = st.create({"track": "std2-l1", "image_digest": missing, "entry": ENTRY}, owner="bob", team="team-b", board_version="proto-t",
                       tracks={"std2-l1"}, lanes={"rendezvous_handoff"}, role="invited")["submission_id"]
    s_foreign = st.create({"track": "std2-l1", "image_digest": GOOD, "entry": ENTRY}, owner="bob", team="team-b", board_version="proto-t",
                          tracks={"std2-l1"}, lanes={"rendezvous_handoff"}, role="invited")["submission_id"]
    _registry(root, s_foreign, "foreign_image")
    logs: list[str] = []
    assert process_one(root, docker=_docker(tmp_path), log=logs.append) == s_foreign
    f = st.get(s_foreign)
    assert f["state"] == "REJECTED" and f["reason"]["detail"] == {"why": "scan", "check": "foreign_image"}
    assert [h["state"] for h in f["_meta"]["history"]] == ["RECEIVED", "REJECTED"]          # 未领取、未扫描
    assert process_one(root, docker=_docker(tmp_path), log=logs.append) is None              # 只剩等待接入的提交
    assert st.get(s_wait)["state"] == "RECEIVED" and not (root / "submissions" / s_wait / "scan.json").exists()

    _registry(root, s_wait, "pulled", digest=missing)                                        # 接入完成 → 领取并进入扫描
    assert process_one(root, docker=_docker(tmp_path), log=logs.append) == s_wait
    w = st.get(s_wait)
    assert [h["state"] for h in w["_meta"]["history"]] == ["RECEIVED", "SCANNING", "REJECTED"]
    assert w["reason"]["detail"] == {"why": "scan", "check": "image_missing"}                 # 替身 docker 中确无此镜像

    s_late = st.create({"track": "std2-l1", "image_digest": GOOD, "entry": ENTRY}, owner="bob", team="team-b", board_version="proto-t",
                       tracks={"std2-l1"}, lanes={"rendezvous_handoff"}, role="invited")["submission_id"]
    assert process_one(root, docker=_docker(tmp_path), log=logs.append, registry_wait_s=0.0) == s_late
    late = st.get(s_late)
    assert late["state"] == "REJECTED" and late["reason"]["detail"]["check"] == "image_missing"
    assert late["reason"]["detail"]["registry_status"] == "none"
