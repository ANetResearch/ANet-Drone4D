"""D3-AC-43 排行榜 v1 端到端（本机）：提交 → 镜像接入与扫描 → 冒烟 → 正式评测 → 评分 → 出榜 → 读取、撤回与下线。

全链路只替换两处外部依赖：docker（`tests/d3/submit/fake_scan_docker.py`，镜像 inspect）与 gz 正式 Run（`_Runs`，按排名 Run 规格写出
运行清单与 `evaluation.json`；Realistic 档的真实 gz 锁步运行与正式数字归 B21，在 dmax 独占卡上采集）。其余均为产品代码：
bench-api（BN-82/85/97/83/84/99、BN-22）、`awr.bench.subworker.process_one`（v1 路径）、`boardstore`、`aggregate` 的 v1 封板与聚合、
`split` 的 test 划分登记、HiddenStore 与真值访问审计。

覆盖 D3-AC-43 原文各条款（阈值不放宽）：
- 分轨 `std3-l2`、`std3-l1` 在榜单版本 v1（8 个 test 事件 × 5 种子 × 2 重复）上：3 个基线参考行与 ≥ 2 个提交条目出榜，按赛道显示
  四项指标及其 95% 置信区间、逐项名次与并列组、非支配层级，不显示合成主得分；
- 只收 gz 锁步档的正式运行（轮次配置、评测行与运行清单三处把关）；
- test 集真值访问审计中外部访问为 0，越权访问返回 691；
- 排行榜接口最低角色为 `invited`，匿名与公开站访问返回 404；
- 提交者撤回后 24 h 内条目下线（读取侧即时下线，当前版本重算，对账时延 ≤ 86 400 s）。
另核对：秘密种子只在 EvalStore（评测行、榜单、提交记录、运行目录、日志与接口响应中检索种子与密钥命中 0），运行清单只带
`seed_commit = HMAC(K_eval, run_id:seed)`；版本切换时排队提交的衔接规则（B18 决定）。
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("foxglove_schemas_protobuf")          # bench-api 依赖 D3 环境（.venv-d3）；D2 环境下整模块跳过

from fastapi.testclient import TestClient

from awr.agentsdk.schemas import bench_validator
from awr.api.bench_main import create_app
from awr.bench import split as sp
from awr.bench.boardstore import (
    LISTING,
    WITHDRAW_SLA_S,
    BoardPhysicsError,
    append_rows,
    delist_withdrawn,
    load_rows,
    publish,
)
from awr.bench.eval.aggregate import EVENT_LANES, load_board_template, verify_lock
from awr.bench.settings import BenchSettings
from awr.bench.submissions import SubmissionStore
from awr.bench.subworker import process_one, seed_commit, v1_problems

sys.path.insert(0, str(Path(__file__).parent))
from _board_v1 import LANE_EVENTS, TEST_EVENTS, seal, v1_rows

P = "/api/bench/v1"
N_BOOT = 200
DEV_EV = "dev-smoke-01"
TRACKS = ("std3-l2", "std3-l1")
LANES = tuple(LANE_EVENTS)
BASELINES = {"static_guard": (0.30, 0.10, 0.20, 0.10), "coverage_scan": (0.50, 0.20, 0.40, 0.20), "track_share": (0.60, 0.30, 0.45, 0.25)}
IMG = {k: "sha256:" + c * 64 for k, c in (("a", "a"), ("b", "b"), ("r", "e"), ("c", "c"))}
SKILL = {IMG["a"]: (0.85, 0.55, 0.65, 0.45), IMG["b"]: (0.55, 0.35, 0.40, 0.30), IMG["r"]: (0.70, 0.40, 0.50, 0.35),
         IMG["c"]: (0.40, 0.20, 0.30, 0.20)}
ENTRY = {"cmd": ["python", "-m", "anet4d.serve", "agent:Agent"], "env": {"MODE": "eval"}}
WHO = {"ann": ("invited", ("team-a",)), "bob": ("invited", ("team-b",)), "cid": ("invited", ("team-c",)), "rae": ("researcher", ("d3",)),
       "eve": ("evaluator", ())}
H = 3_600 * 10**9
START = [{"kind": "pose", "pos_enu_m": [76.0, y, 0.0], "yaw_deg": 90.0} for y in (-78.0, -82.0, -86.0)]
SUBMIT = Path(__file__).resolve().parents[1] / "submit" / "fake_scan_docker.py"


# ---------------------------------------------------------------------------------------------------------------------------- 夹具
def _event(root: Path, eid: str, split: str) -> None:
    d = root / eid / "v1"
    d.mkdir(parents=True)
    world = {"id": "oryahovo-crop1", "content_version": "cv-1"}
    etype = TEST_EVENTS.get(eid, "rendezvous_handoff")
    (d / "plan.json").write_text(json.dumps({"event_id": eid, "version": 1, "event_type": etype, "world": world, "duration_ms": 180_000,
                                             "actors": []}))
    (d / "event.json").write_text(json.dumps({"event_id": eid, "event_type": etype, "world": world, "split": split}))
    (d / "brief.json").write_text(json.dumps({"event_id": eid, "task": "find and follow the target"}))
    (d / "compile_report.json").write_text(json.dumps({"event_id": eid, "version": 1, "status": "compiled", "gates": []}))
    tape = b"D4DTAPE\x00" + eid.encode()
    (d / "tape.bin").write_bytes(tape)
    (d / "tape.json").write_text(json.dumps({"content_sha256": hashlib.sha256(tape).hexdigest()}))
    (d / "event_truth.json").write_text(json.dumps({"event_id": eid, "targets": ["p1"]}))


class _Runs:
    """正式 Run 替身（gz 锁步运行 + 评测器）：核对 subworker 交给 runner 的排名 Run 参数，评测员经 HiddenStore 读取 test 真值，
    写运行清单（`physics`、`ranked`、`seed_commit`）与 `evaluation.json`（分量由镜像决定，按 run_id 加确定性扰动）。"""

    def __init__(self, data_root: Path, hidden: sp.HiddenStore) -> None:
        self.root, self.hidden = data_root, hidden
        self.calls: list[dict] = []
        self.physics_of: dict[str, str] = {}             # run_id → 运行清单记录的物理档（模拟平台误用非 gz 档）

    def __call__(self, rs: dict, *, policy: dict, seed: int, run_id: str, run_dir: Path, worlds_root: Path, events_root: Path,
                 physics: str | None = None, gz: dict | None = None, agent_image: str | None = None, docker: str = "docker",
                 duration_s: float | None = None, seed_commit: str | None = None) -> dict:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.calls.append({"run_id": run_id, "seed": int(seed), "seed_commit": seed_commit, "physics": physics, "event": rs["event"]["id"],
                           "rs": copy.deepcopy(rs), "events_root": str(events_root), "image": policy["image_digest"], "gz": gz})
        agents = {f"uav-0{i}": {"acts": 40, "exited": "bye", "exit_code": 0} for i in (1, 2, 3)}
        if run_id.endswith("-smoke"):                   # 冒烟：dev 事件、Fast 档、公开冒烟种子 0
            return {"run_id": run_id, "run_dir": str(run_dir), "run_wall_s": 1.0, "agents": agents}
        eid = rs["event"]["id"]
        assert (Path(events_root) / eid / "v1" / "tape.bin").is_file()             # Env 能经事件根找到隐藏区中的磁带
        self.hidden.read_bytes(sp.SERVICE_EVALUATOR, self.hidden.event_dir(eid, 1) / "event_truth.json", event_id=eid, run_id=run_id,
                               job="evaluate")
        mu = SKILL[policy["image_digest"]]
        g = np.random.default_rng(int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16))
        comp = dict(zip(("ec", "eu", "ao", "ce"), (float(np.clip(m + g.normal(0, 0.03), 0, 1)) for m in mu), strict=True))
        man = {"schema": "drone4d.bench.env_run.v1", "run_id": run_id, "physics": self.physics_of.get(run_id, physics), "ranked": bool(rs.get("ranked")),
               "seed_commit": seed_commit}
        (run_dir / "manifest.json").write_text(json.dumps(man))
        (run_dir / "evaluation.json").write_text(json.dumps({"components": comp, "ranked": True, "ranked_valid": True, "invalid_reasons": [],
                                                             "obs_level": rs["obs_level"]}))
        return {"run_id": run_id, "run_dir": str(run_dir), "run_wall_s": 2.0, "components": comp, "ranked_valid": True, "agents": agents}


def _runtime(cfg: dict, root: Path) -> dict:
    """榜单配置的服务端运行参数（`subworker.v1_problems`）：世界与事件根、test 事件的起飞位姿（事件根经视图指向隐藏区）、冒烟规格。"""
    smoke = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "smoke"}, "world": {"id": "oryahovo-crop1"},
             "event": {"id": DEV_EV, "version": 1}, "fleet": [{"uav_id": f"uav-0{i + 1}", "vehicle_ref": "p600_bench", "start": s}
                                                              for i, s in enumerate(START)],
             "policy": {"kind": "image"}, "obs_level": "L1", "physics": "realistic", "duration_s": 20.0}
    return {**cfg, "worlds_root": str(root / "worlds"), "events_root": str(root / "events"), "gz": {"launcher": "docker", "image": "d3-gz:dev"},
            "agent_image": "d3-agent-py:dev", "smoke": {"run_spec": smoke},
            "events": {e: {"world_id": "oryahovo-crop1", "version": 1, "starts": START, "events_root": str(root / "eval-events")}
                       for e in TEST_EVENTS}}


def _baselines(bdir: Path, cfg: dict) -> None:
    rows = []
    for i, (name, mu) in enumerate(BASELINES.items()):
        for tr in [t["track"] for t in cfg["tracks"]]:
            rows += v1_rows(f"baseline/{name}", mu, track=tr, lanes=LANES, noise=0.04, rng_seed=10 * i + len(tr), baseline=True,
                            team="D3 reference")
    append_rows(bdir, rows)


class E2E:
    """数据根、bench-api、test 划分与隐藏区、封板的 v1 榜单（3 个基线参考行已发布）与提交流水线。"""

    def __init__(self, tmp: Path) -> None:
        self.s = s = BenchSettings(data_root=tmp / "d3", allowed_origins=(), secure_cookie=False)
        self.root = root = s.data_root
        self.app = create_app(s)
        self.st = self.app.state.submissions = SubmissionStore(root / "submissions")
        self.c = TestClient(self.app)
        self.tok = {n: self.app.state.auth.issue_token(n, role, pr) for n, (role, pr) in WHO.items()}
        self.audit = self.app.state.auth.audit
        self.reg = sp.SplitRegistry.for_root(root, audit=self.audit)
        self.hidden = sp.HiddenStore.for_root(root, self.audit)
        for e in TEST_EVENTS:                                                   # test 划分 v1：8 个事件，真值只在隐藏区
            _event(root / "events", e, "test")
            self.reg.register(e, "test", by="root", version="t1")
            self.hidden.seal_event(e, events_root=root / "events", dataset_root=s.dataset_dir)
            (root / "eval-events").mkdir(exist_ok=True)
            (root / "eval-events" / e).symlink_to(self.hidden.root / e / "event")
        _event(root / "events", DEV_EV, "dev")
        self.reg.register(DEV_EV, "dev", by="root")
        self.bdir = self.seal_board("v1")
        (root / "boards" / "current").write_text("v1\n")
        self.docker = tmp / "docker"
        self.docker.write_text(f"#!/bin/sh\nFAKE_SCAN_STATE={tmp / 'scan'} exec {sys.executable} {SUBMIT} \"$@\"\n")
        self.docker.chmod(0o755)
        (tmp / "scan").mkdir()
        (tmp / "scan" / "images.json").write_text(json.dumps({d: {"Os": "linux", "Architecture": "amd64", "Size": 1_200_000_000} for d in IMG.values()}))
        self.runs = _Runs(root, self.hidden)
        self.logs: list[str] = []
        self.responses: list[str] = []

    def seal_board(self, v: str, template: dict | None = None) -> Path:
        cfg = template or load_board_template()
        bdir, full, _store = seal(self.root, template=cfg, test_split_version=self.reg.digest())
        full = _runtime(full, self.root)
        (bdir / "config.json").write_text(json.dumps(full, ensure_ascii=False, indent=1))
        _baselines(bdir, full)
        publish(bdir, self.st.board_view(), n_boot=N_BOOT)
        assert bdir.name == v
        return bdir

    def cfg(self, v: str = "v1") -> dict:
        return json.loads((self.root / "boards" / v / "config.json").read_text())

    def store(self, v: str = "v1") -> dict:
        return json.loads((self.root / "evalstore" / f"{v}.json").read_text())

    def h(self, who: str, **extra) -> dict:
        return {"Authorization": f"Bearer {self.tok[who][1]}", **extra}

    def get(self, path: str, who: str | None = None, **kw):
        r = self.c.get(P + path, headers=self.h(who) if who else kw.pop("headers", {}), **kw)
        self.responses.append(r.text)
        return r

    def submit(self, who: str, img: str, track: str = "std3-l2", lanes: list[str] | None = None, board: bool = True) -> str:
        """BN-85 登记 → BN-99 设为上榜；受邀成员的镜像由宿主 materialize 写接入记录（`registry.json`）。"""
        body = {"track": track, "image_digest": IMG[img], "entry": ENTRY, **({"lanes": lanes} if lanes else {})}
        r = self.c.post(f"{P}/submissions", json=body, headers=self.h(who))
        assert r.status_code == 202, r.text
        sid = r.json()["submission_id"]
        if board:
            r = self.c.post(f"{P}/submissions/{sid}/visibility", json={"visibility": "board"}, headers=self.h(who))
            assert r.status_code == 200 and r.json()["state"] == "RECEIVED", r.text
        return sid

    def materialize(self, sid: str) -> None:
        p = self.root / "submissions" / sid / "registry.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"schema": "awr.d3.registry_materialize.v1", "submission_id": sid, "image_digest": self.st.get(sid)["image_digest"],
                                 "status": "pulled"}))

    def process(self) -> str | None:
        return process_one(self.root, docker=str(self.docker), smoke_s=5.0, n_boot=N_BOOT, run_job=self.runs, log=self.logs.append)

    def board(self, v: str = "v1") -> dict:
        return json.loads((self.root / "boards" / v / "board.json").read_text())

    def lb(self, who: str, **q) -> dict:
        r = self.get("/leaderboard", who, params=q)
        assert r.status_code == 200, r.text
        return r.json()


def _ids(entries: list[dict]) -> set[str]:
    return {e["entry_id"] for e in entries}


def _of(board: dict, eid: str, track: str | None = None) -> dict[tuple[str, str], dict]:
    return {(e["track"], e["lane"]): e for e in board["entries"] if e["entry_id"] == eid and (track is None or e["track"] == track)}


@pytest.fixture(scope="module")
def done(tmp_path_factory) -> dict:
    """全链路一次（每分轨 2 个提交条目，全部赛道）：std3-l2 为 team-a 与 team-b（受邀成员），std3-l1 为 d3（研究员）与 team-c（受邀成员）。"""
    e = E2E(tmp_path_factory.mktemp("e2e"))
    sa = e.submit("ann", "a")
    sb = e.submit("bob", "b")
    sr = e.submit("rae", "r", track="std3-l1")
    sc = e.submit("cid", "c", track="std3-l1")
    order = [e.process(), e.process()]                          # 受邀成员的镜像尚未接入：内部成员的提交先领取，其余等待接入
    for s in (sa, sb, sc):
        e.materialize(s)
    order += [e.process() for _ in range(4)]
    return {"e": e, "ids": {"a": sa, "b": sb, "r": sr, "c": sc}, "order": order}


# -------------------------------------------------------------------------------------------------------------------- 提交到出榜
def test_submissions_flow_through_the_pipeline_onto_board_v1(done) -> None:
    e, ids = done["e"], done["ids"]
    assert done["order"] == [ids["r"], None, ids["a"], ids["b"], ids["c"], None]
    want_runs = dict.fromkeys(ids, 4 * 2 * 5 * 2)                                        # 赛道 × test 事件 × K × R = 80 / 条目
    for k, sid in ids.items():
        d = e.st.get(sid)
        assert d["state"] == "DONE", (k, d.get("reason"), e.logs[-5:])
        assert [x["state"] for x in d["_meta"]["history"]] == ["RECEIVED", "SCANNING", "VALIDATING", "EVALUATING", "SCORING", "DONE"]
        assert not list(bench_validator("submission").iter_errors({k2: v for k2, v in d.items() if not k2.startswith("_")}))
        assert sum(ln["runs"]["total"] for ln in d["result"]["lanes"]) == want_runs[k]
        assert all(ln["status"] == "valid" and ln["runs"]["valid"] == ln["runs"]["total"] and ln["composite"] is None for ln in d["result"]["lanes"])
        assert json.loads((e.root / "submissions" / sid / "scan.json").read_text())["verdict"] == "pass"
    assert all([ln["lane"] for ln in e.st.get(s)["result"]["lanes"]] == list(LANES) for s in ids.values())
    smoke = [c for c in e.runs.calls if c["run_id"].endswith("-smoke")]
    assert len(smoke) == 4 and {c["event"] for c in smoke} == {DEV_EV} and {c["physics"] for c in smoke} == {"kinematic"}
    assert {c["seed"] for c in smoke} == {0}                                                # 冒烟只在 dev 事件上运行
    rows = load_rows(e.bdir)
    for k, sid in ids.items():
        mine = [r for r in rows if r.get("submission_id") == sid]
        assert len(mine) == want_runs[k]
        assert {(r["event"], r["seed"], r["rep"]) for r in mine} == {(ev, s, rep) for ln in {r["lane"] for r in mine} for ev in LANE_EVENTS[ln]
                                                                    for s in range(5) for rep in range(2)}
        assert all(r["evaluation"]["physics"] == "realistic" and r["event_type"] == EVENT_LANES[r["lane"]] for r in mine)


def test_formal_runs_are_ranked_gz_lockstep_with_secret_seeds_from_the_evalstore(done) -> None:
    e, ids = done["e"], done["ids"]
    cfg, store = e.cfg(), e.store()
    assert not verify_lock(cfg["lock"], store, seeds_per_event=5)
    k_eval = bytes.fromhex(store["k_eval"])
    formal = [c for c in e.runs.calls if not c["run_id"].endswith("-smoke")]
    assert len(formal) == 4 * 80
    for c in formal:
        lane = next(ln for ln, evs in LANE_EVENTS.items() if c["event"] in evs)
        k = int(c["run_id"].rsplit("-k", 1)[1].split("r")[0])
        assert c["seed"] == store["seeds"][lane][c["event"]][k] and str(c["seed"]) not in c["run_id"]
        assert c["seed_commit"] == hmac.new(k_eval, f"{c['run_id']}:{c['seed']}".encode(), hashlib.sha256).hexdigest()
        assert c["seed_commit"] == seed_commit(k_eval, c["run_id"], c["seed"])
        rs = c["rs"]
        assert rs["ranked"] is True and rs["physics"] == "realistic" and c["physics"] == "realistic" and c["gz"]["image"] == "d3-gz:dev"
        assert "seed" not in rs and "seeds" not in rs and rs["policy"] == {"kind": "image", "image_digest": c["image"]}
        assert [f["vehicle_ref"] for f in rs["fleet"]] == ["p600_bench"] * 3 and c["events_root"].endswith("eval-events")
        assert rs["obs_level"] == ("L1" if c["image"] in (IMG["r"], IMG["c"]) else "L2")
    for sid in ids.values():                                    # 同一条目的 K 个种子覆盖 EvalStore 中该事件的全部种子
        mine = [c for c in formal if c["run_id"].startswith(sid)]
        for ev in {c["event"] for c in mine}:
            lane = next(ln for ln, evs in LANE_EVENTS.items() if ev in evs)
            assert sorted({c["seed"] for c in mine if c["event"] == ev}) == sorted(store["seeds"][lane][ev])


def test_board_v1_shows_lanes_with_ci_ranks_ties_and_tiers_without_composite(done) -> None:
    e, ids = done["e"], done["ids"]
    b = e.board()
    assert b["score_version"] == "d3-score-1" and [t["track"] for t in b["tracks"]] == list(TRACKS)
    assert all(t["physics"] == "realistic" for t in b["tracks"]) and [ln["lane"] for ln in b["lanes"]] == list(LANES)
    assert sum(len(ln["test_events"]) for ln in b["lanes"]) == 8 and (b["seeds_per_event"], b["repeats"]) == (5, 2)
    assert b["test_split_version"] == e.reg.digest()
    subs = {f"team-a/{ids['a']}", f"team-b/{ids['b']}", f"d3/{ids['r']}", f"team-c/{ids['c']}"}
    assert _ids(b["entries"]) == subs | {f"baseline/{n}" for n in BASELINES}
    for tr in TRACKS:                                           # 每分轨每赛道 3 个基线参考行与 2 个提交条目
        for ln in LANES:
            es = [x for x in b["entries"] if x["track"] == tr and x["lane"] == ln]
            assert sum(x["baseline"] for x in es) == 3 and sum(not x["baseline"] for x in es) == 2
            assert [x["display_order"] for x in es] == list(range(1, len(es) + 1))
            for x in es:
                assert x["status"] == "valid" and isinstance(x["dominance_tier"], int)
                for m in ("ec", "eu", "ao", "ce"):
                    v = x["metrics"][m]
                    assert v["ci95"][0] <= v["value"] <= v["ci95"][1]
                    if not x["baseline"]:                       # 参考行只标注所处层，不参与逐项名次
                        assert v["rank"] >= 1 and v["tie_group"] >= 0
                assert "composite" not in x and "score" not in x
    l2 = [x for x in b["entries"] if x["track"] == "std3-l2" and not x["baseline"]]
    assert {(x["entry_id"].split("/")[0], x["lane"]) for x in l2} == {(t, ln) for t in ("team-a", "team-b") for ln in LANES}
    l1 = [x for x in b["entries"] if x["track"] == "std3-l1" and not x["baseline"]]
    assert {(x["entry_id"].split("/")[0], x["lane"]) for x in l1} == {(t, ln) for t in ("d3", "team-c") for ln in LANES}
    a_h = _of(b, f"team-a/{ids['a']}")[("std3-l2", "handoff")]
    assert a_h["dominance_tier"] == 1 and a_h["metrics"]["ec"]["rank"] == 1
    for sid in ids.values():                                    # 提交记录回填的指标与榜单一致（同一 K_board 置信区间）
        d = e.st.get(sid)
        shown = _of(b, f"{d['_meta']['team']}/{sid}")
        for ln in d["result"]["lanes"]:
            assert ln["metrics"] == shown[(d["track"], ln["lane"])]["metrics"] and ln["dominance_tier"] == shown[(d["track"], ln["lane"])]["dominance_tier"]
    before = (e.bdir / "board.json").read_bytes()
    publish(e.bdir, e.st.board_view(), n_boot=N_BOOT)
    assert (e.bdir / "board.json").read_bytes() == before                             # 重算逐字节一致


def test_board_api_minimum_role_invited_and_entry_detail(done) -> None:
    e, ids = done["e"], done["ids"]
    lb = e.lb("bob", track="std3-l2", lane="handoff")
    assert _ids(lb["entries"]) == {f"team-a/{ids['a']}", f"team-b/{ids['b']}"} | {f"baseline/{n}" for n in BASELINES}
    assert [x["display_order"] for x in lb["entries"]] == [1, 2, 3, 4, 5] and all(x["baseline"] for x in lb["entries"][-3:])
    assert _ids(e.lb("cid", track="std3-l1", lane="reid")["entries"]) == {f"d3/{ids['r']}", f"team-c/{ids['c']}"} | {f"baseline/{n}" for n in BASELINES}
    for who in ("ann", "bob", "rae", "eve"):
        r = e.get("/board", who)
        assert r.status_code == 200 and r.json()["current"] == "v1"
        v = r.json()["versions"][0]
        assert v["score_version"] == "d3-score-1" and v["weights"] is None and v["n_entries"] == len(e.board()["entries"])
    r = e.get(f"/board/std3-l2/entries/team-a/{ids['a']}", "bob")
    assert r.status_code == 200, r.text
    det = r.json()
    assert [x["lane"] for x in det["lanes"]] == list(LANES)
    for ln in det["lanes"]:
        assert [ev["event"] for ev in ln["events"]] == LANE_EVENTS[ln["lane"]]
        assert all(ev["seeds"] == [0, 1, 2, 3, 4] and ev["n_runs"] == 10 for ev in ln["events"])
    body = json.dumps([e.lb("rae"), e.get("/board", "rae").json()])
    for k in ("composite", "k_board", "k_eval", "evalstore", '"weights": {'):
        assert k not in body


def test_anonymous_and_public_site_access_is_404(done) -> None:
    e, ids = done["e"], done["ids"]
    ghost = e.get("/no-such-route")
    assert ghost.status_code == 404 and ghost.json()["code"] == 305
    shape = {k: v for k, v in ghost.json().items() if k != "request_id"}
    paths = ["/board", "/leaderboard", "/leaderboard?track=std3-l1&lane=search", f"/board/std3-l2/entries/team-a/{ids['a']}"]
    revoked = e.app.state.auth.issue_token("old", "invited", ("team-o",))
    assert e.app.state.auth.revoke_token(revoked[0])
    anon = [{}, {"Authorization": f"Bearer {revoked[1]}"}, {"Cookie": "d3_session=forged"}]
    public = ([e.h(w, Host=host) for host in ("anet4d.agentnetwork.org.cn", "drone4d.agentnetwork.org.cn") for w in ("ann", "rae", "eve")]
              + [e.h("ann", **{"X-Forwarded-Host": "anet4d.agentnetwork.org.cn"})])
    for hdr in anon + public:
        for path in paths:
            r = e.c.get(P + path, headers=hdr)
            e.responses.append(r.text)
            assert r.status_code == 404 and {k: v for k, v in r.json().items() if k != "request_id"} == shape, (path, r.text)
    deny = [x for x in sp.read_audit(e.root / "audit") if x["action"] == "deny" and x.get("why") in ("anonymous", "public_site")]
    assert len(deny) == len(paths) * (len(anon) + len(public))


def test_test_split_truth_access_has_zero_external_reads_and_691(done) -> None:
    e = done["e"]
    assert e.get("/events/reid-t01", "rae").json()["code"] == 691                        # 研究员读取 test 事件：691
    assert e.get("/events/reid-t01", "ann").json()["detail"]["why"] == "invited_scope"   # 受邀成员只见 dev 事件：115
    with pytest.raises(sp.SplitAccessDenied):
        e.hidden.read_bytes(sp.Principal("rae", "researcher", ("d3",), "bearer"), e.hidden.event_dir("reid-t01", 1) / "tape.bin", event_id="reid-t01")
    summary = sp.truth_access_summary(sp.read_audit(e.root / "audit"))
    formal = sum(not c["run_id"].endswith("-smoke") for c in e.runs.calls)
    assert summary["external"] == 0 and summary["external_principals"] == []
    assert summary["internal"] == formal and summary["denied"] >= 2
    assert not e.hidden.misplaced(e.reg, events_root=e.root / "events", dataset_root=e.s.dataset_dir)
    for eid in TEST_EVENTS:                                     # 真值只在隐藏区：目录 0700、文件 0600
        d = e.hidden.event_dir(eid, 1)
        assert d.stat().st_mode & 0o777 == 0o700 and (d / "event_truth.json").stat().st_mode & 0o777 == 0o600
        assert not (e.root / "events" / eid / "v1" / "event_truth.json").exists()


def test_secret_seeds_and_keys_never_leave_the_evalstore(done) -> None:
    e = done["e"]
    store = e.store()
    seeds = [s for ln in store["seeds"].values() for ss in ln.values() for s in ss]
    needles = [str(s).encode() for s in seeds] + [f"{s:x}".encode() for s in seeds] + [store["k_eval"].encode(), store["k_board"].encode()]
    hits = []
    for f in e.root.rglob("*"):
        if f.is_file() and not f.is_symlink() and "evalstore" not in f.parts and "hidden" not in f.parts:
            data = f.read_bytes()
            hits += [(str(f.relative_to(e.root)), n[:6]) for n in needles if n in data]
    text = "\n".join(e.logs + e.responses).encode()
    hits += [("logs/responses", n[:6]) for n in needles if n in text]
    assert hits == []
    es = (e.root / "evalstore" / "v1.json").read_bytes()
    assert all(n in es for n in needles[:len(seeds)])                                   # 检索有效：种子明文确在 EvalStore 中
    assert (e.root / "evalstore" / "v1.json").stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------------------------------------------- 只收 gz 锁步档
def test_only_gz_lockstep_runs_reach_board_v1(tmp_path: Path) -> None:
    e = E2E(tmp_path)
    before = {f: (e.bdir / f).read_bytes() for f in ("board.json", "details.json", "rows.jsonl", LISTING)}
    sid = e.submit("rae", "r", lanes=["search"])
    e.runs.physics_of[f"{sid}-search-t02-k3r1"] = "kinematic"                          # 平台误以运动学档运行了其中一个 Run
    assert e.process() == sid
    d = e.st.get(sid)
    assert d["state"] == "FAILED" and d["reason"]["code"] == 320 and d["reason"]["detail"] == {"why": "physics", "runs": 1}
    assert {f: (e.bdir / f).read_bytes() for f in before} == before                     # 评测行整批不写，榜单不变
    q = e.st.quota_status(owner="rae", team="d3", role="researcher")
    assert q["formal_week"]["used"] == 0                                               # 平台原因失败不计入正式提交数
    fast = v1_rows("d3/sub-fast00000001", (0.9, 0.9, 0.9, 0.9), lanes=("search",))
    for r in fast:
        r["submission_id"] = "sub-fast00000001"
        r["evaluation"]["physics"] = "fast"
    with pytest.raises(BoardPhysicsError):
        append_rows(e.bdir, fast)
    nr = v1_rows("d3/sub-fast00000002", (0.9, 0.9, 0.9, 0.9), lanes=("search",))
    for r in nr:                                                                       # 绕过写入检查的非排名行：发布时不计入
        r["submission_id"] = "sub-fast00000002"
        r["evaluation"].pop("physics")
        r["evaluation"]["invalid_reasons"] = ["physics_not_ranked"]
    with (e.bdir / "rows.jsonl").open("a") as f:
        for r in fast + nr:
            f.write(json.dumps(r) + "\n")
    subs = {**e.st.board_view(), **{s: {"state": "DONE", "visibility": "board", "team": "d3", "done_unix_ns": 1}
                                    for s in ("sub-fast00000001", "sub-fast00000002")}}
    b = publish(e.bdir, subs, n_boot=N_BOOT)
    assert all(x["baseline"] for x in b["entries"])
    cfg = e.cfg()
    for bad in ({"physics": "fast"}, {"physics": "kinematic"}):                         # 轮次配置不是 gz 锁步档：拒绝发布
        (e.bdir / "config.json").write_text(json.dumps({**cfg, **bad}))
        with pytest.raises(BoardPhysicsError):
            publish(e.bdir, e.st.board_view(), n_boot=N_BOOT)


def test_incomplete_runtime_config_fails_closed_without_charging_the_submitter(tmp_path: Path) -> None:
    e = E2E(tmp_path)
    cfg = e.cfg()
    assert v1_problems(cfg, "std3-l2") == []
    bad = {**cfg, "events": {k: v for k, v in cfg["events"].items() if k != "vehicle-t02"},
           "smoke": {"run_spec": {**cfg["smoke"]["run_spec"], "event": {"id": "reid-t01", "version": 1}}}}
    probs = v1_problems(bad, "std3-l2")
    assert any("vehicle-t02" in p for p in probs) and any("smoke runs only on dev events" in p for p in probs)
    assert v1_problems(bad, "std3-l2", ["search"]) == [p for p in probs if "smoke" in p]
    (e.bdir / "config.json").write_text(json.dumps(bad))
    sid = e.submit("rae", "r", lanes=["vehicle"])
    assert e.process() == sid
    d = e.st.get(sid)
    assert d["state"] == "FAILED" and d["reason"]["detail"]["why"] == "board_config" and not e.runs.calls
    (e.bdir / "config.json").write_text(json.dumps(cfg))
    es = e.root / "evalstore" / "v1.json"
    doc = json.loads(es.read_text())
    doc["seeds"]["search"]["search-t01"][0] += 1                                        # EvalStore 与封板记录不符：失败关闭
    es.write_text(json.dumps(doc))
    sid2 = e.submit("rae", "r", lanes=["search"])
    assert e.process() == sid2 and e.st.get(sid2)["state"] == "FAILED" and not e.runs.calls
    assert "seed commit does not match" in json.dumps(e.st.get(sid2)["reason"])
    assert e.st.quota_status(owner="rae", team="d3", role="researcher")["formal_week"]["used"] == 0


# ------------------------------------------------------------------------------------------------------------ 撤回与 24 h 下线
def test_withdrawal_takes_the_entry_off_board_v1_within_24h(tmp_path: Path) -> None:
    e = E2E(tmp_path)
    sa = e.submit("ann", "a", lanes=["handoff"])
    sb = e.submit("bob", "b", lanes=["handoff"])
    sr = e.submit("rae", "r", lanes=["handoff"])
    for s in (sa, sb):
        e.materialize(s)
    assert [e.process() for _ in range(4)] == [sa, sb, sr, None]
    ea, eb, er = f"team-a/{sa}", f"team-b/{sb}", f"d3/{sr}"
    assert {ea, eb, er} <= _ids(e.lb("bob", track="std3-l2", lane="handoff")["entries"])
    n_rows = len(load_rows(e.bdir))
    r = e.c.post(f"{P}/submissions/{sa}/visibility", json={"visibility": "private"}, headers=e.h("ann"))       # BN-99 撤回
    assert r.status_code == 200 and r.json()["visibility"] == "private"
    assert ea not in _ids(e.lb("bob")["entries"]) and ea not in _ids(e.board()["entries"])                     # 立即下线并重算
    assert e.get(f"/board/std3-l2/entries/{ea}", "rae").json()["code"] == 305
    assert _of(e.board(), er)[("std3-l2", "handoff")]["metrics"]["ec"]["rank"] == 1                            # 他者名次随之更新
    assert e.c.post(f"{P}/submissions/{sb}/visibility", json={"visibility": "private"}, headers=e.h("ann")).json()["detail"]["why"] == "invited_scope"
    e.st.set_visibility(sb, "private")                                                 # 撤回记录已写入但榜单尚未重算（如接口进程中断）
    t = e.st.get(sb)["_meta"]["updated_unix_ns"]
    assert eb in _ids(e.board()["entries"]) and eb not in _ids(e.lb("rae")["entries"])                         # 读取侧即时隐藏
    cur = next(v for v in e.get("/board", "rae").json()["versions"] if v["board_version"] == "v1")
    assert cur["n_entries"] == len(e.board()["entries"]) - 1
    rep = delist_withdrawn(e.root / "boards", e.st.board_view(), n_boot=N_BOOT, now_ns=t + 23 * H, updated={sb: t})
    assert rep == [{"board_version": "v1", "action": "republished", "entries": [{"entry_id": eb, "submission_id": sb, "lag_s": 23 * 3600.0,
                                                                                   "within_sla": True}]}]
    assert rep[0]["entries"][0]["lag_s"] <= WITHDRAW_SLA_S
    assert eb not in _ids(e.board()["entries"]) and json.loads((e.bdir / LISTING).read_text())["entries"] == {er: sr}
    assert delist_withdrawn(e.root / "boards", e.st.board_view(), n_boot=N_BOOT) == []
    assert len(load_rows(e.bdir)) == n_rows and all("result" in e.st.get(s) for s in (sa, sb))                # 评测行与评分记录保留
    sc = e.submit("cid", "c", lanes=["handoff"])                                       # 尚在排队的提交撤回：不再评测
    assert e.c.delete(f"{P}/submissions/{sc}", headers=e.h("cid")).status_code == 204
    e.materialize(sc)
    n_calls = len(e.runs.calls)
    assert e.process() is None and e.st.get(sc)["state"] == "WITHDRAWN" and len(e.runs.calls) == n_calls
    assert not [r for r in load_rows(e.bdir) if r.get("submission_id") == sc]


# ------------------------------------------------------------------------------------------------------- 榜单版本切换时的排队提交
def test_queued_submissions_follow_the_current_version_or_are_refused(tmp_path: Path) -> None:
    e = E2E(tmp_path)
    sa = e.submit("ann", "a", lanes=["search"])
    sr = e.submit("rae", "r", track="std3-l1", lanes=["search"])
    e.materialize(sa)
    v1_bytes = {f: (e.bdir / f).read_bytes() for f in ("board.json", "rows.jsonl", LISTING)}
    tpl = load_board_template()
    tpl = {**tpl, "board_version": "v2", "tracks": [t for t in tpl["tracks"] if t["track"] == "std3-l2"]}
    v2 = e.seal_board("v2", tpl)
    (e.root / "boards" / "current").write_text("v2\n")
    assert e.process() == sa
    d = e.st.get(sa)
    assert d["state"] == "DONE" and d["_meta"]["board_version"] == "v2" and d["_meta"]["moved"][0]["from"] == "v1"
    assert f"team-a/{sa}" in _ids(e.board("v2")["entries"]) and len([r for r in load_rows(v2) if r.get("submission_id") == sa]) == 20
    k2 = bytes.fromhex(e.store("v2")["k_eval"])                                         # 以当前版本的秘密种子集评测
    for c in (c for c in e.runs.calls if c["run_id"].startswith(sa) and not c["run_id"].endswith("-smoke")):
        assert c["seed"] in e.store("v2")["seeds"]["search"][c["event"]] and c["seed_commit"] == seed_commit(k2, c["run_id"], c["seed"])
    assert e.process() == sr
    r = e.st.get(sr)
    assert r["state"] == "REJECTED" and r["reason"]["code"] == 680 and r["reason"]["detail"] == {"why": "board_frozen", "board_version": "v1"}
    assert e.st.quota_status(owner="rae", team="d3", role="researcher")["formal_week"]["used"] == 0             # 不计入配额
    assert {f: (e.bdir / f).read_bytes() for f in v1_bytes} == v1_bytes                                         # 旧版本只读
