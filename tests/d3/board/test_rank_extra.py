"""D3-AC-57 缺口用例：非排名运行不进榜 (f)、外观层不混排 (g)、同一输入跨进程逐字节一致、平台原因失败的自动重跑与榜单联测 (d)。

(f)、(g) 走 `boardstore.publish` 的完整上榜路径（rows.jsonl → 代表条目 → board.json、details.json），评测文档按评测器的口径构造
（Fast 档 `physics_not_ranked`；SITL 与开发运行 `ranked = false`；非 `mesh` 外观的运行被组合规则拒绝排名，只能以 `ranked = false` 运行）。
(d) 以 docker 与 `run_job` 替身驱动 `subworker.process_one`，再经 bench-api 读回榜单与条目详情。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from awr.agentsdk.schemas import bench_validator
from awr.api.bench_main import create_app
from awr.bench.boardstore import append_rows, compose_board, load_rows, publish, slim_evaluation
from awr.bench.eval.aggregate import METRICS, build_board
from awr.bench.settings import BenchSettings
from awr.bench.spec_rules import check_ranked_eval, problem
from awr.bench.submissions import ACTIVE_PER_TEAM, SubmissionStore
from awr.bench.subworker import process_one

sys.path.insert(0, str(Path(__file__).parent))
from _rows import entries, row

P = "/api/bench/v1"
REPO = Path(__file__).resolve().parents[3]
W = {"ec": 0.4, "eu": 0.3, "ao": 0.2, "ce": 0.1}


def _evaluation(ec: float, *, tier: str = "gz-sim", ranked: bool = True) -> dict:
    """评测器口径的 evaluation.v1 摘要：kinematic（Fast）档标注 `physics_not_ranked`，`ranked` 取运行清单。"""
    reasons = ["physics_not_ranked"] if tier == "kinematic" else []
    doc = {"schema": "drone4d.bench.evaluation.v1", "ranked": ranked, "ranked_valid": not reasons, "invalid_reasons": reasons,
           "components": {"ec": ec, "eu": 0.4, "ao": 0.5, "ce": 0.3}}
    return slim_evaluation(doc)


def _sub_rows(team: str, sid: str, ec: float, n: int = 4, **ev) -> list[dict]:
    return [{**row(f"{team}/{sid}", s, 0, 0, 0, 0, team=team, submission_id=sid), "evaluation": _evaluation(ec + 0.01 * s, **ev)} for s in range(n)]


def _board_dir(tmp_path: Path, cfg: dict | None = None) -> Path:
    d = tmp_path / "boards" / "v1"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(cfg or {}))
    append_rows(d, [row("baseline/x", s, 0.5, 0.2, 0.3, 0.2, baseline=True) for s in range(4)])
    return d


def _done(team: str, t: int, visibility: str = "board") -> dict:
    return {"state": "DONE", "visibility": visibility, "team": team, "done_unix_ns": t}


# ---- (f) Fast 档、SITL 与开发运行不进入排行榜 ------------------------------------------------------------------------------------

def test_fast_sitl_and_dev_runs_never_reach_the_published_board(tmp_path: Path) -> None:
    d = _board_dir(tmp_path)
    append_rows(d, _sub_rows("ta", "sub-a1", 0.6)                                    # 正式（Realistic、排名）运行
                + _sub_rows("ta", "sub-a2", 0.9, tier="kinematic", ranked=False)     # 同团队较晚完成的 Fast 档运行
                + _sub_rows("tb", "sub-b1", 0.9, tier="sitl", ranked=False)           # SITL 档：恒为非排名运行
                + _sub_rows("tc", "sub-c1", 0.9, ranked=False)                        # 开发运行
                + _sub_rows("td", "sub-d1", 0.9, tier="kinematic"))                   # 误标为排名的 Fast 档：仍按 physics_not_ranked 剔除
    subs = {"sub-a1": _done("ta", 1), "sub-a2": _done("ta", 2), "sub-b1": _done("tb", 3), "sub-c1": _done("tc", 4), "sub-d1": _done("td", 5)}
    b = publish(d, subs, n_boot=300)
    assert not list(bench_validator("board").iter_errors(b))
    assert set(entries(b)) == {"baseline/x", "ta/sub-a1"}                               # 非排名提交不顶替团队的代表条目
    det = json.loads((d / "details.json").read_text())
    assert set(det) == {"baseline/x", "ta/sub-a1"}                                     # 条目详情同样不含非排名运行
    ev = det["ta/sub-a1"]["tracks"]["std3-l1"]["rendezvous_handoff"][0]
    assert ev["n_runs"] == 4 and ev["invalid_runs"] == 0 and ev["invalid_reasons"] == []
    assert entries(b)["ta/sub-a1"]["metrics"]["ec"]["rank"] == 1
    assert {c["entry_id"] for c in compose_board(d, W, subs, n_boot=300)} == {"baseline/x", "ta/sub-a1"}   # 合成接口同一口径
    text = (d / "board.json").read_text() + (d / "details.json").read_text()
    assert "physics_not_ranked" not in text and not any(s in text for s in ("sub-a2", "sub-b1", "sub-c1", "sub-d1"))


def test_development_board_lists_unranked_runs_and_is_never_mixed_into_a_ranked_one(tmp_path: Path) -> None:
    rows = _sub_rows("ta", "sub-a1", 0.6) + _sub_rows("ta", "sub-a2", 0.9, tier="kinematic", ranked=False)
    subs = {"sub-a1": _done("ta", 1), "sub-a2": _done("ta", 2)}
    dev = _board_dir(tmp_path / "dev", {"track": {"dev": True}})
    append_rows(dev, rows)
    assert set(entries(publish(dev, subs, n_boot=200))) == {"baseline/x", "ta/sub-a2"}          # 开发榜（Fast 档自检）：最近完成者代表
    ranked = _board_dir(tmp_path / "ranked")
    append_rows(ranked, rows)
    assert set(entries(publish(ranked, subs, n_boot=200))) == {"baseline/x", "ta/sub-a1"}


# ---- (g) 不同外观层的条目不混排、不合并 ---------------------------------------------------------------------------------------

def test_appearance_layers_are_never_mixed_or_merged() -> None:
    spec = json.loads((REPO / "configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json").read_text())
    for source in ("mesh_distilled", "generative", "aerial"):                                 # 排名运行只接受 mesh（P3 前 aerial 亦然）
        vs = check_ranked_eval({**spec, "appearance": "3dgs", "appearance_source": source}, physics="realistic")
        assert problem(vs)[1]["why"] == "ranked_appearance"
    mesh = [row("t1/a", s, 0.6, 0.5, 0.5, 0.5) for s in range(6)] + [row("t2/b", s, 0.4, 0.5, 0.5, 0.5) for s in range(6)]
    gs = [{**r, "evaluation": {**r["evaluation"], "ranked": False}}                         # 同一条目以 3dgs 外观运行：只能为非排名运行
          for r in [row("t1/a", s, 0.0, 0.0, 0.0, 0.0, event="ev1") for s in range(6)]]
    aerial = [row("t1/a", s, 0.1, 0.5, 0.5, 0.5, track="std3-l1-aerial") for s in range(6)]  # P3 经榜单版本登记的外观分轨
    aerial += [row("t2/b", s, 0.9, 0.5, 0.5, 0.5, track="std3-l1-aerial") for s in range(6)]
    only_mesh = build_board(mesh, board_version="v1", n_boot=400)
    b = build_board(mesh + gs + aerial, board_version="v1", n_boot=400)
    by = {(e["track"], e["entry_id"]): e for e in b["entries"]}
    assert set(by) == {("std3-l1", "t1/a"), ("std3-l1", "t2/b"), ("std3-l1-aerial", "t1/a"), ("std3-l1-aerial", "t2/b")}
    for e in only_mesh["entries"]:                                                          # 外观层运行不并入 mesh 分轨的条目
        assert json.dumps(by[("std3-l1", e["entry_id"])]) == json.dumps(e)
    assert by[("std3-l1", "t1/a")]["metrics"]["ec"]["rank"] == 1 and by[("std3-l1-aerial", "t1/a")]["metrics"]["ec"]["rank"] == 2
    assert by[("std3-l1-aerial", "t1/a")]["metrics"]["ec"]["value"] == 0.1                  # 两分轨各自聚合，不合并
    assert {t["track"]: t["appearance"] for t in b["tracks"]} == {"std3-l1": "mesh", "std3-l1-aerial": "mesh"}   # D3 只有 mesh 外观
    for tr in ("std3-l1", "std3-l1-aerial"):                                                # 层级只在分轨内计算
        assert sorted(e["dominance_tier"] for e in b["entries"] if e["track"] == tr) == [1, 2]


# ---- 同一输入两次计算逐字节一致（独立进程、不同哈希种子） -----------------------------------------------------------------

_PUBLISH = "import sys; from pathlib import Path; from awr.bench.boardstore import publish; publish(Path(sys.argv[1]), n_boot=2000)"


def test_publish_is_byte_identical_across_processes(tmp_path: Path) -> None:
    rows = []
    for i, (lane, track) in enumerate((("rendezvous_handoff", "std3-l1"), ("vehicle_track", "std3-l1"), ("rendezvous_handoff", "std3-l2"))):
        for k in range(5):
            for s in range(6):
                for rep in range(2):
                    v = (0.2 + 0.1 * k + 0.013 * s + 0.004 * rep + 0.01 * i) % 1.0
                    rows.append(row(f"baseline/b{k}", s, v, 1 - v, 0.5 * v, 0.3, baseline=True, lane=lane, track=track,
                                    event=f"{lane}-00{s % 2}", valid=not (k == 4 and s == 0)))
    digests = []
    for n, seed in enumerate(("0", "4242")):
        d = tmp_path / f"run{n}" / "v1"
        d.mkdir(parents=True)
        (d / "config.json").write_text("{}")
        append_rows(d, rows)
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO / "python")}
        subprocess.run([sys.executable, "-c", _PUBLISH, str(d)], check=True, env=env, cwd=tmp_path)
        digests.append({f: hashlib.sha256((d / f).read_bytes()).hexdigest() for f in ("board.json", "details.json")})
    assert digests[0] == digests[1]
    b = json.loads((tmp_path / "run0/v1/board.json").read_text())
    assert len(b["entries"]) == 15 and all(e["metrics"][m]["ci95"][0] <= e["metrics"][m]["ci95"][1] for e in b["entries"] for m in METRICS)
    d = tmp_path / "run0" / "v1"
    publish(d, n_boot=2000)                                                                  # 本进程重算：与子进程逐字节一致
    assert hashlib.sha256((d / "board.json").read_bytes()).hexdigest() == digests[0]["board.json"]


# ---- (d) 平台原因失败的 Run 自动重跑，不计入提交方；与榜单联测 ----------------------------------------------------------------

def _fake_docker(tmp_path: Path) -> str:
    p = tmp_path / "docker"
    p.write_text("#!/bin/sh\necho '{\"Os\": \"linux\", \"Architecture\": \"amd64\", \"Size\": 1048576}'\n")
    p.chmod(0o755)
    return str(p)


class _Jobs:
    """`run_job` 替身：冒烟运行返回握手统计；正式运行写 evaluation.json。`fail` 为 {run_id: 失败次数}（平台原因，例如 gz 实例丢失），
    失败前先写一份残缺的评测文档，以证明失败的尝试不留下任何计分痕迹。"""

    def __init__(self, fail: dict[str, int]) -> None:
        self.fail, self.calls = dict(fail), []

    def __call__(self, rs: dict, *, run_id: str, run_dir: Path, seed: int, **kw) -> dict:
        self.calls.append(run_id)
        if run_id.endswith("-smoke"):
            return {"agents": {"uav-01": {"acts": 10, "exited": "bye", "exit_code": 0}}}
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        if self.fail.get(run_id, 0) > 0:
            self.fail[run_id] -= 1
            (run_dir / "evaluation.json").write_text(json.dumps({**_evaluation(0.0), "ranked_valid": False, "invalid_reasons": ["actor_penetration"]}))
            raise RuntimeError("gz instance lost")
        (run_dir / "evaluation.json").write_text(json.dumps({"schema": "drone4d.bench.evaluation.v1", **_evaluation(0.7 + 0.01 * (seed % 7))}))
        return {"run_dir": str(run_dir), "agents": {"uav-01": {"acts": 100}}}


def test_platform_failures_are_rerun_and_not_charged_to_the_submitter(tmp_path: Path) -> None:
    root = tmp_path / "d3"
    b = root / "boards" / "proto-0"
    b.mkdir(parents=True)
    cfg = {"run_spec": {"event": {"id": "crop-handoff-001"}}, "seeds": [101, 102, 103], "events_root": str(tmp_path), "worlds_root": str(tmp_path),
           "physics": "realistic", "track_id": "std3-l1", "lane": "rendezvous_handoff", "event_type": "rendezvous_handoff"}
    (b / "config.json").write_text(json.dumps(cfg))
    append_rows(b, [{**row("baseline/x", s, 0.5, 0.2, 0.3, 0.2, baseline=True, event="crop-handoff-001")} for s in range(3)])
    publish(b, n_boot=200)
    st = SubmissionStore(root / "submissions")
    mk = {"track": "std3-l1", "image_digest": "sha256:" + "a" * 64, "entry": {"cmd": ["python", "-m", "agent"]}}
    sa = st.create(mk, owner="ann", team="team-a", board_version="proto-0", tracks={"std3-l1"}, lanes={"rendezvous_handoff"})["submission_id"]
    sb = st.create(mk, owner="bob", team="team-b", board_version="proto-0", tracks={"std3-l1"}, lanes={"rendezvous_handoff"})["submission_id"]
    st.set_visibility(sa, "board")
    st.set_visibility(sb, "board")
    docker = _fake_docker(tmp_path)

    jobs = _Jobs({f"{sa}-r1": 1})                                                            # r1 第一次失败、重跑成功
    assert process_one(root, docker=docker, run_job=jobs, n_boot=200, log=lambda m: None) == sa
    a = st.get(sa)
    assert a["state"] == "DONE" and jobs.calls == [f"{sa}-smoke", f"{sa}-r0", f"{sa}-r1", f"{sa}-r1", f"{sa}-r2"]
    lane = a["result"]["lanes"][0]
    assert lane["runs"] == {"total": 3, "valid": 3, "invalid": 0, "platform_rerun": 1} and lane["status"] == "valid"
    mine = [r for r in load_rows(b) if r.get("submission_id") == sa]
    assert [r["seed"] for r in mine] == [0, 1, 2] and all(r["evaluation"]["ranked_valid"] for r in mine)   # 失败的尝试不计入
    board_a = (b / "board.json").read_bytes()
    e = entries(json.loads(board_a))[f"team-a/{sa}"]
    assert e["status"] == "valid" and e["metrics"]["ec"]["rank"] == 1 and lane["dominance_tier"] == e["dominance_tier"] == 1
    assert lane["metrics"] == e["metrics"]                                                    # 提交结果回填的是榜单上的名次与区间

    jobs = _Jobs({f"{sb}-r0": 2})                                                            # 重跑仍失败：FAILED，不出成绩
    assert process_one(root, docker=docker, run_job=jobs, n_boot=200, log=lambda m: None) == sb
    f = st.get(sb)
    assert f["state"] == "FAILED" and f["reason"]["code"] == 320 and f["reason"]["detail"] == {"why": "platform", "run": 0}
    assert jobs.calls == [f"{sb}-smoke", f"{sb}-r0", f"{sb}-r0"] and "result" not in f
    assert not [r for r in load_rows(b) if r.get("submission_id") == sb]
    assert (b / "board.json").read_bytes() == board_a                                        # 榜单不因平台失败改变
    for _ in range(ACTIVE_PER_TEAM):                                                         # 平台失败不占用团队的提交配额
        st.create(mk, owner="bob", team="team-b", board_version="proto-0", tracks={"std3-l1"}, lanes={"rendezvous_handoff"})

    s = BenchSettings(data_root=root, allowed_origins=(), secure_cookie=False)
    app = create_app(s)
    c = TestClient(app)
    h = {"Authorization": f"Bearer {app.state.auth.issue_token('rae', 'researcher', ('d3',))[1]}"}
    lb = c.get(f"{P}/leaderboard", headers=h).json()
    assert {x["entry_id"] for x in lb["entries"]} == {"baseline/x", f"team-a/{sa}"}
    det = c.get(f"{P}/board/std3-l1/entries/team-a/{sa}", headers=h).json()
    ev = det["lanes"][0]["events"][0]
    assert ev["n_runs"] == 3 and ev["invalid_runs"] == 0 and ev["seeds"] == [0, 1, 2]
    assert c.get(f"{P}/board/std3-l1/entries/team-b/{sb}", headers=h).json()["code"] == 305
    view = c.get(f"{P}/submissions/{sa}", headers=h).json()
    assert view["result"]["lanes"][0]["runs"]["platform_rerun"] == 1 and view["result"]["lanes"][0]["composite"] is None
