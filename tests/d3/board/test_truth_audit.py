"""test 划分登记与真值访问审计（D3-AC-43"test 集真值访问审计日志中外部访问为 0，越权访问返回 691"；D3-AC-63"test 划分与评测运行
不可导出（691）"；M27-FR-073、FR-113、FR-114、FR-124、§7.2 V15；M23 §7.3.3；AWR-17 §18.3.5）。

- 划分登记 `splits.json`：一个事件只有一个划分、变更写审计、登记与事件声明取更严格者；
- HiddenStore：test 真值只经它读取，评测员与管理员以外 691，每次访问写 `truth_read`；`seal_event` 迁移与 `misplaced` 检查；
- bench-api：研究者读取 test 事件详情（BN-22）、test 事件受限制品（BN-27）、test Run 观测帧（BN-49 至 BN-51）返回 403 `691` 并审计；
  评测员读取 test 磁带与真值经 hidden 并审计；
- 红队序列：四类角色对全部 test 真值入口逐一访问后，审计日志中外部主体的成功读取计数为 0（`truth_access_summary`、
  `python -m awr.bench.split audit`）。
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# D2 环境（.venv）无 D3 依赖：bench API 与回放读取经 awr.bench.recorder.foxglove 导入 foxglove_schemas_protobuf
pytest.importorskip("foxglove_schemas_protobuf")

from awr.api.bench_main import create_app
from awr.bench import split as sp
from awr.bench.api import truth
from awr.bench.api.app import PREFIX as P
from awr.bench.auth import AuditLog, Principal
from awr.bench.runs_store import RunStore
from awr.bench.settings import BenchSettings

DEV_EV, TEST_EV, REG_EV = "abc-crossing", "tst-hidden", "tst-registry"   # REG_EV：事件声明 dev，登记表登记为 test
DEV_RUN, TEST_RUN, MANAGED_TEST = "run-dev0000001", "run-tst0000001", "run-mgd0000001"
TAPE = b"D4DTAPE\x00" + bytes(range(64))
TAPE_SHA = hashlib.sha256(TAPE).hexdigest()
ROLES = {"invited": ("ivan", ("team-x",)), "researcher": ("alice", ("d3",)), "evaluator": ("eve", ()), "admin": ("root", ())}


def _event(root: Path, eid: str, *, split: str | None = None, tape: bool = True) -> Path:
    d = root / eid / "v1"
    d.mkdir(parents=True)
    world = {"id": "oryahovo-crop1", "content_version": "cv-1"}
    (d / "plan.json").write_text(json.dumps({"event_id": eid, "version": 1, "world": world, "duration_ms": 60_000, "actors": []}))
    (d / "event.json").write_text(json.dumps({"event_id": eid, "world": world, **({"split": split} if split else {})}))
    (d / "compile_report.json").write_text(json.dumps({"event_id": eid, "version": 1, "status": "compiled", "gates": [{"gate": 1, "errors": []}],
                                                       "segments": [{"actor": "p1", "route": [[0, 0], [10, 0]]}]}))
    (d / "brief.json").write_text(json.dumps({"event_id": eid, "task": "track the target", "duration_s": 60}))
    if tape:
        (d / "tape.bin").write_bytes(TAPE)
        (d / "tape.json").write_text(json.dumps({"content_sha256": TAPE_SHA}))
        (d / "event_truth.json").write_text(json.dumps({"targets": ["p1"]}))
    return d


def _run(dataset: Path, eid: str, rid: str, *, split: str) -> Path:
    d = dataset / eid / "runs" / rid
    d.mkdir(parents=True)
    (d / "run_manifest.json").write_text(json.dumps({"schema": "drone4d.bench.run_manifest.v1", "run_id": rid, "split": split,
                                                     "event": {"event_id": eid, "version": 1, "content_sha256": TAPE_SHA, "offset_s": 0.0}}))
    return d


class Env:
    def __init__(self, tmp: Path) -> None:
        self.s = s = BenchSettings(data_root=tmp / "d3", allowed_origins=("https://lab.example",), secure_cookie=False)
        ev = s.data_root / "events"
        _event(ev, DEV_EV)
        _event(ev, TEST_EV, split="test")
        _event(ev, REG_EV)
        _run(s.dataset_dir, DEV_EV, DEV_RUN, split="dev")
        _run(s.dataset_dir, TEST_EV, TEST_RUN, split="test")
        _run(s.dataset_dir, DEV_EV, MANAGED_TEST, split="dev")       # 清单写 dev，RunStore 记录为 test：取更严格者
        self.app = app = create_app(s)
        if not any(getattr(r, "path", "").endswith("/artifacts/{name}") for r in app.routes):
            app.include_router(truth.router, prefix=P)               # BN-27 尚未挂载到 create_bench_app 时由用例挂载
        app.state.runs = st = RunStore(s.db_path)
        spec = {"schema": "drone4d.bench.run_spec.v1", "policy": {"kind": "reference", "name": "track_share"}, "physics": "gz-sim",
                "obs_level": "L1", "fleet": [{"model": "x500", "count": 1}], "duration_s": 60.0}
        st.enqueue(MANAGED_TEST, experiment_id="exp-aaaaaaaaaa", event_id=DEV_EV, world_id="oryahovo-crop1", spec=spec, tenant="d3",
                   submitted_by="alice", seed=7, seed_commit="e" * 64, split="test")
        self.reg = sp.SplitRegistry.for_root(s.data_root, audit=app.state.auth.audit)
        self.reg.register(TEST_EV, "test", by="root", version="t1")
        self.reg.register(REG_EV, "test", by="root")
        self.reg.register(DEV_EV, "dev", by="root")
        self.hidden = sp.HiddenStore.for_root(s.data_root, app.state.auth.audit)
        self.hidden.seal_event(TEST_EV, events_root=ev, dataset_root=s.dataset_dir)
        self.c = TestClient(app)
        self.tokens = {r: app.state.auth.issue_token(n, r, pr)[1] for r, (n, pr) in ROLES.items()}

    def h(self, role: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[role]}"}

    def audit(self) -> list[dict]:
        return sp.read_audit(self.s.data_root / "audit")


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _deny691(env: Env, r, before: int, principal: str) -> dict:
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["code"] == 691 and body["detail"]["why"] == "test_split", body
    new = [a for a in env.audit()[before:] if a["action"] == "deny"]
    assert [(a["code"], a["status"], a["principal"], a["why"]) for a in new] == [(691, 403, principal, "test_split")], new
    return body


# ── 划分登记 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_registry_one_split_per_event_and_change_audit(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit")
    reg = sp.SplitRegistry(tmp_path / "bench" / "splits.json", audit=audit)
    assert reg.split_of("e1") == "dev" and reg.split_of("e1", "test") == "test" and reg.events("test") == []
    d0 = reg.digest()
    reg.register("e1", "dev", by="root")
    reg.register("e1", "test", by="root", version="t1")                 # 改登记：同一事件只保留一个划分
    assert reg.load()["events"] == {"e1": "test"} and reg.events("dev") == [] and reg.events("test") == ["e1"]
    assert reg.digest() != d0 and reg.load()["test_split_version"] == "t1"
    assert stat.S_IMODE(reg.path.stat().st_mode) == 0o600
    ch = [a for a in audit.read() if a["action"] == "split_change"]
    assert [(a["event_id"], a["previous"], a["split"], a["principal"]) for a in ch] == [("e1", None, "dev", "root"), ("e1", "dev", "test", "root")]
    with pytest.raises(ValueError):
        reg.register("e2", "holdout", by="root")


def test_registry_strictest_rule_and_conflicts(tmp_path: Path) -> None:
    reg = sp.SplitRegistry(tmp_path / "splits.json")
    reg.register("a", "dev", by="root")
    reg.register("b", "test", by="root")
    assert reg.split_of("a") == "dev" and reg.split_of("a", "test") == "test"      # 事件自身声明 test：不因登记 dev 而放宽
    assert reg.split_of("b", "dev") == "test" and reg.split_of("c", "weird") == "test"  # 未知取值按 test
    assert reg.conflicts({"a": "test", "b": "test", "c": None}) == [{"event_id": "a", "registered": "dev", "declared": "test", "effective": "test"}]
    assert sp.strictest([None, "", "dev"]) == "dev" and sp.strictest(["dev", "test"]) == "test"


def test_registry_rejects_duplicates_and_bad_values(tmp_path: Path) -> None:
    p = tmp_path / "splits.json"
    p.write_text('{"schema": "drone4d.bench.splits.v1", "events": {"e1": "dev", "e1": "test"}}')
    with pytest.raises(sp.SplitRegistryError, match="duplicate"):
        sp.SplitRegistry(p).load()
    p.write_text(json.dumps({"schema": "drone4d.bench.splits.v1", "events": {"e1": "both"}}))
    with pytest.raises(sp.SplitRegistryError):
        sp.SplitRegistry(p).load()
    p.write_text(json.dumps({"events": {}}))
    with pytest.raises(sp.SplitRegistryError):
        sp.SplitRegistry(p).load()


# ── 提交与导出（V15；M27-FR-073；D3-AC-63） ───────────────────────────────────────────────────────────────────────────

def test_submit_check_v15(tmp_path: Path) -> None:
    reg = sp.SplitRegistry(tmp_path / "splits.json")
    reg.register("t", "test", by="root")
    for role in ("invited", "researcher"):
        with pytest.raises(sp.SplitAccessDenied) as e:
            sp.submit_check(reg, "t", role=role)
        assert e.value.code == 691 and e.value.detail == {"why": "test_split", "event_id": "t", "split": "test"}
    sp.submit_check(reg, "t", role="evaluator")
    sp.submit_check(reg, "t", role="invited", channel="submission")          # 提交通道（测试套件）可运行 test 事件
    sp.submit_check(reg, "d", role="researcher")
    with pytest.raises(sp.SplitAccessDenied):
        sp.submit_check(reg, "d", role="researcher", declared="test")


def test_export_check_test_split_and_eval_runs() -> None:
    sp.export_check(split="dev", run_id="r1", retention_class="standard", kind="bench")
    with pytest.raises(sp.SplitAccessDenied) as e:
        sp.export_check(split="test", run_id="r2")
    assert e.value.code == 691 and e.value.detail["why"] == "test_split"
    for kw in ({"retention_class": "ranked_eval"}, {"kind": "submission"}):
        with pytest.raises(sp.SplitAccessDenied) as e:
            sp.export_check(split="dev", run_id="r3", **kw)
        assert e.value.detail["why"] == "eval_run"


def test_run_split_takes_strictest_declaration(tmp_path: Path) -> None:
    reg = sp.SplitRegistry(tmp_path / "splits.json")
    reg.register("t", "test", by="root")
    d = _run(tmp_path / "dataset", "d", "r1", split="dev")
    assert sp.run_split(reg, event_id="d", declared=sp.declared_run_splits(d)) == "dev"
    assert sp.run_split(reg, event_id="t", declared=sp.declared_run_splits(d)) == "test"
    assert sp.run_split(reg, event_id="d", declared=["test", *sp.declared_run_splits(d)]) == "test"
    (d / "evaluation.json").write_text("{not json")
    assert sp.declared_run_splits(d)[-1] == "test"                             # 清单不可读：宁严勿松


# ── HiddenStore ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_hidden_store_authorizes_and_audits_every_access(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit")
    hs = sp.HiddenStore(tmp_path / "hidden", audit)
    f = hs.event_dir("t", 1) / "tape.bin"
    hs._mkdir(f.parent)
    f.write_bytes(TAPE)
    for p in (Principal("ivan", "invited", ("team-x",)), Principal("alice", "researcher", ("d3",)), None):
        with pytest.raises(sp.SplitAccessDenied) as e:
            hs.read_bytes(p, f, event_id="t", job="probe")
        assert e.value.code == 691
    assert hs.read_bytes(Principal("eve", "evaluator"), f, event_id="t") == TAPE
    assert hs.read_bytes(sp.SERVICE_EVALUATOR, f, event_id="t", run_id="r1", job="evaluate") == TAPE
    recs = [a for a in audit.read() if a["action"] == sp.TRUTH_READ]
    assert [(a["principal"], a["result"]) for a in recs] == [("ivan", "denied"), ("alice", "denied"), (None, "denied"), ("eve", "ok"),
                                                             ("svc:evaluator", "ok")]
    assert all(a["path"] == "t/event/v1/tape.bin" and a["process"]["pid"] > 0 and a["event_id"] == "t" for a in recs)
    assert recs[0]["job"] == "probe" and recs[-1]["run_id"] == "r1" and recs[0]["code"] == 691 and recs[3]["code"] is None
    s = sp.truth_access_summary(recs)
    assert s == {"reads": 5, "internal": 2, "external": 0, "external_principals": [], "denied": 3, "denied_principals": ["None", "alice", "ivan"]}
    for d in (hs.root, hs.root / "t", hs.root / "t" / "event", f.parent):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_seal_event_moves_truth_into_hidden(tmp_path: Path) -> None:
    root = tmp_path / "d3"
    ev, ds = root / "events", root / "dataset"
    vdir = _event(ev, "t", split="test")
    _event(ev, "d")
    (ds / "t" / "truth").mkdir(parents=True)
    (ds / "t" / "truth" / "run_r1.mcap").write_bytes(b"mcap")
    rd = _run(ds, "t", "r1", split="test")
    (rd / "truth").mkdir()
    (rd / "truth" / "fleet.npz").write_bytes(b"npz")
    audit = AuditLog(root / "audit")
    reg = sp.SplitRegistry.for_root(root, audit=audit)
    reg.register("t", "test", by="root")
    hs = sp.HiddenStore.for_root(root, audit)
    before = hs.misplaced(reg, events_root=ev, dataset_root=ds)
    hidden_names = ["compile_report.json", "event.json", "event_truth.json", "plan.json", "tape.bin", "tape.json"]
    assert sorted(Path(x).name for x in before) == sorted([*hidden_names, "fleet.npz", "run_r1.mcap"])
    acts = hs.seal_event("t", events_root=ev, dataset_root=ds)
    assert sorted(a["dst"] for a in acts if a["action"] == "move") == [*(f"t/event/v1/{n}" for n in hidden_names), "t/truth/run_r1.mcap"]
    assert sorted(f.name for f in vdir.iterdir()) == ["brief.json"]                       # 编译目录只留简报（K07：路线、锚点、编译报告也封存）
    assert hs.event_dir("t", 1).joinpath("tape.bin").read_bytes() == TAPE
    assert stat.S_IMODE(hs.event_dir("t", 1).joinpath("tape.bin").stat().st_mode) == 0o600
    assert [Path(x).name for x in hs.misplaced(reg, events_root=ev, dataset_root=ds)] == ["fleet.npz"]   # Env 运行目录内的真值：只报告
    assert (ev / "d" / "v1" / "tape.bin").exists() and (ev / "d" / "v1" / "plan.json").exists()          # dev 事件不动
    # 重复迁移：目标已存在且内容相同则去重，不同则报告冲突
    (vdir / "tape.bin").write_bytes(TAPE)
    (vdir / "tape.json").write_text("{}")
    acts = hs.seal_event("t", events_root=ev, dataset_root=ds)
    assert sorted((a["action"], Path(a["src"]).name) for a in acts) == [("conflict", "tape.json"), ("dedup", "tape.bin")]
    assert [a for a in audit.read() if a["action"] == "hidden_seal"][-1]["conflicts"] == 1


def _compiled(vdir: Path, *, rev: int = 1) -> None:
    """按编译器的实际布局补齐版本目录：真值在 `truth/r<k>/`，另有锚点、事件源与发布标记。"""
    t = vdir / "truth" / f"r{rev}"
    t.mkdir(parents=True)
    (t / "event_truth.json").write_text(json.dumps({"events": [{"id": "a", "interval_ms": [1000, 2000]}]}))
    (t / "event_graph.json").write_text(json.dumps({"nodes": []}))
    (vdir / "event_truth.json").unlink()
    (vdir / "anchors.json").write_text(json.dumps({"anchors": [{"id": "search_target", "xy": [12.5, -3.0]}]}))
    (vdir / "anchors.overlay.json").write_text("{}")
    (vdir / "event.yaml").write_text("event_id: t\n")
    (vdir / "published.json").write_text(json.dumps({"by": "root", "content_sha256": TAPE_SHA}))


def test_seal_is_recursive_for_published_then_registered_test_event(tmp_path: Path) -> None:
    """K07（E10 遗留 F-72）：事件先以 dev 发布到 `events/`（真值在 `truth/r<k>/`），之后才登记为 test；`misplaced` 递归列出
    全部隐藏制品（含 `truth/r1/event_truth.json`、`event_graph.json` 与 `plan.json`、`anchors.json`、`compile_report.json`），
    `seal_event` 按相对路径移入 hidden，编译目录只留简报与发布标记，hidden 内可按名称取到最新修订的真值。"""
    root = tmp_path / "d3"
    ev, ds = root / "events", root / "dataset"
    vdir = _event(ev, "pub", split=None)
    _compiled(vdir)
    audit = AuditLog(root / "audit")
    reg = sp.SplitRegistry.for_root(root, audit=audit)
    hs = sp.HiddenStore.for_root(root, audit)
    reg.register("pub", "dev", by="root")
    assert hs.misplaced(reg, events_root=ev, dataset_root=ds) == []                          # dev：全部制品可留在编译目录
    reg.register("pub", "test", by="root")
    rel = sorted(Path(x).relative_to(vdir).as_posix() for x in hs.misplaced(reg, events_root=ev, dataset_root=ds))
    assert rel == ["anchors.json", "anchors.overlay.json", "compile_report.json", "event.json", "event.yaml", "plan.json", "tape.bin",
                   "tape.json", "truth/r1/event_graph.json", "truth/r1/event_truth.json"]
    acts = hs.seal_event("pub", events_root=ev, dataset_root=ds)
    assert sorted(a["dst"] for a in acts) == [f"pub/event/v1/{r}" for r in rel] and {a["action"] for a in acts} == {"move"}
    assert sorted(p.relative_to(vdir).as_posix() for p in vdir.rglob("*")) == ["brief.json", "published.json"]   # truth/ 子目录随之删除
    assert hs.misplaced(reg, events_root=ev, dataset_root=ds) == []
    f = hs.event_file("pub", 1, "event_truth.json")
    assert f == hs.event_dir("pub", 1) / "truth" / "r1" / "event_truth.json" and json.loads(f.read_text())["events"][0]["id"] == "a"
    assert hs.event_file("pub", 1, "plan.json") == hs.event_dir("pub", 1) / "plan.json"
    for d in (hs.event_dir("pub", 1) / "truth", hs.event_dir("pub", 1) / "truth" / "r1"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((hs.event_dir("pub", 1) / "truth" / "r1" / "event_graph.json").stat().st_mode) == 0o600
    # 新修订 r2 只写进 hidden：按名称取最新修订
    r2 = hs.event_dir("pub", 1) / "truth" / "r2"
    r2.mkdir()
    (r2 / "event_truth.json").write_text("{}")
    assert hs.event_file("pub", 1, "event_truth.json") == r2 / "event_truth.json"
    # 发布后又被写回的子目录真值（如重编译）同样被列出
    (vdir / "truth" / "r3").mkdir(parents=True)
    (vdir / "truth" / "r3" / "event_truth.json").write_text("{}")
    assert [Path(x).relative_to(vdir).as_posix() for x in hs.misplaced(reg, events_root=ev, dataset_root=ds)] == ["truth/r3/event_truth.json"]


def test_seal_reports_symlinks_and_declared_test(tmp_path: Path) -> None:
    """编译目录中的符号链接不搬进 hidden，只报告冲突并保留在违规清单；只在事件声明（未登记）为 test 的事件同样计入。"""
    root = tmp_path / "d3"
    ev, ds = root / "events", root / "dataset"
    vdir = _event(ev, "s", split="test", tape=False)
    (vdir / "observability.npz").symlink_to(vdir / "plan.json")
    audit = AuditLog(root / "audit")
    reg = sp.SplitRegistry.for_root(root, audit=audit)
    hs = sp.HiddenStore.for_root(root, audit)
    assert {Path(x).name for x in hs.misplaced(reg, events_root=ev, dataset_root=ds, declared={"s": "test"})} == {
        "compile_report.json", "event.json", "observability.npz", "plan.json"}
    acts = hs.seal_event("s", events_root=ev, dataset_root=ds)
    assert [(a["action"], a.get("why")) for a in acts if Path(a["src"]).name == "observability.npz"] == [("conflict", "symlink")]
    assert [Path(x).name for x in hs.misplaced(reg, events_root=ev, dataset_root=ds, declared={"s": "test"})] == ["observability.npz"]


def test_hidden_permissions_check_and_fix(tmp_path: Path, capsys) -> None:
    """hidden 属主与权限（M27-FR-114，K07）：目录 0700、文件 0600、不允许符号链接；`perms --fix` 整理模式（属主需 root，此处用当前
    用户核对 uid 判据），并写审计 `hidden_perms`。"""
    import os

    root = tmp_path / "d3"
    audit = AuditLog(root / "audit")
    hs = sp.HiddenStore.for_root(root, audit)
    d = hs.event_dir("x", 1) / "truth" / "r1"
    d.mkdir(parents=True)
    (d / "event_truth.json").write_text("{}")
    (hs.event_dir("x", 1) / "tape.bin").write_bytes(TAPE)
    os.chmod(hs.root, 0o755)
    os.chmod(d / "event_truth.json", 0o644)
    bad = {x["path"]: x["problems"] for x in hs.permission_problems()}
    assert bad["."] == ["mode 0755 != 0700"] and bad["x/event/v1/truth/r1/event_truth.json"] == ["mode 0644 != 0600"]
    assert "x/event/v1/tape.bin" in bad                                                  # write_bytes 按 umask 建为 0644
    uid, gid = os.getuid(), os.getgid()
    assert any("uid" in p for x in hs.permission_problems(uid=uid + 1) for p in x["problems"])
    args = ["--data-root", str(root), "perms", "--uid", str(uid), "--gid", str(gid)]
    assert sp.main(args) == 1
    capsys.readouterr()
    assert sp.main([*args, "--fix"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["problems"] == [] and out["fixed"] >= 3
    assert stat.S_IMODE(hs.root.stat().st_mode) == 0o700 and stat.S_IMODE((d / "event_truth.json").stat().st_mode) == 0o600
    assert [a for a in audit.read() if a["action"] == "hidden_perms"][-1]["uid"] == uid
    (hs.event_dir("x", 1) / "link.json").symlink_to(d / "event_truth.json")
    assert [x["problems"] for x in hs.permission_problems() if x["path"].endswith("link.json")] == [["symlink not allowed"]]
    assert sp.main(["--data-root", str(root), "perms", "--fix"]) == 1                     # 链接不自动处理：仍报告


# ── bench-api：691 与 hidden 读取 ────────────────────────────────────────────────────────────────────────────────────

def test_event_detail_of_test_event_is_691_for_researcher(env: Env) -> None:
    for eid in (TEST_EV, REG_EV):                                          # REG_EV：事件声明 dev，登记表 test
        before = len(env.audit())
        body = _deny691(env, env.c.get(f"{P}/events/{eid}", headers=env.h("researcher")), before, "alice")
        assert body["detail"] == {"why": "test_split", "event_id": eid, "split": "test"}
        r = env.c.get(f"{P}/events/{eid}", headers=env.h("evaluator"))
        assert r.status_code == 200 and r.json()["split"] == "test"
        if eid == TEST_EV:                                                 # K07：编译报告已封存，详情由 hidden 组装并审计
            assert r.json()["segments"] and r.json()["status"] == "compiled" and "plan.json" in {a["name"] for a in r.json()["artifacts"]}
            assert not (env.s.data_root / "events" / eid / "v1" / "compile_report.json").exists()
            last = [a for a in env.audit() if a["action"] == "truth_read"][-1]
            assert (last["op"], last["principal"], last["result"], last["path"]) == ("detail", "eve", "ok", f"{eid}/event/v1")
    r = env.c.get(f"{P}/events/{TEST_EV}", headers=env.h("invited"))
    assert r.status_code == 403 and r.json()["code"] == 115 and r.json()["detail"]["why"] == "invited_scope"
    assert env.c.get(f"{P}/events/{DEV_EV}", headers=env.h("researcher")).status_code == 200
    items = {e["event_id"]: e["split"] for e in env.c.get(f"{P}/events", headers=env.h("researcher")).json()["items"]}
    assert items == {DEV_EV: "dev", TEST_EV: "test", REG_EV: "test"}           # 列表的划分取登记表
    inv = [e["event_id"] for e in env.c.get(f"{P}/events", headers=env.h("invited")).json()["items"]]
    assert inv == [DEV_EV]


def test_event_artifacts_follow_m23_table(env: Env) -> None:
    url = f"{P}/events/{{}}/versions/1/artifacts/{{}}"
    r = env.c.get(url.format(DEV_EV, "tape.bin"), headers=env.h("researcher"))
    assert r.status_code == 200 and r.content == TAPE and r.headers["etag"] == f'"{TAPE_SHA}"'
    assert env.c.get(url.format(DEV_EV, "tape.bin"), headers={**env.h("researcher"), "If-None-Match": f'"{TAPE_SHA}"'}).status_code == 304
    r = env.c.get(url.format(TEST_EV, "brief.json"), headers=env.h("researcher"))            # 任务简报：非真值
    assert r.status_code == 200 and r.json()["task"] == "track the target"
    for name in ("tape.bin", "tape.json", "event_truth.json", "plan.json", "event.yaml", "compile_report.json"):
        before = len(env.audit())
        _deny691(env, env.c.get(url.format(TEST_EV, name), headers=env.h("researcher")), before, "alice")
    assert env.c.get(url.format(TEST_EV, "tape.bin"), headers=env.h("invited")).json()["code"] == 115
    assert env.c.get(url.format(TEST_EV, "secrets.txt"), headers=env.h("researcher")).status_code == 404
    assert env.c.get(url.format("../x", "tape.bin"), headers=env.h("researcher")).status_code == 404
    before = len(env.audit())
    r = env.c.get(url.format(TEST_EV, "tape.bin"), headers=env.h("evaluator"))
    assert r.status_code == 200 and r.content == TAPE and r.headers["cache-control"] == "private, no-store"
    a = [x for x in env.audit()[before:] if x["action"] == sp.TRUTH_READ]
    assert [(x["principal"], x["role"], x["result"], x["path"]) for x in a] == [("eve", "evaluator", "ok", f"{TEST_EV}/event/v1/tape.bin")]
    r = env.c.get(url.format(REG_EV, "tape.bin"), headers=env.h("admin"))                     # 未迁移：回落到编译目录，审计标 outside:
    assert r.status_code == 200
    assert [x for x in env.audit() if x["action"] == sp.TRUTH_READ][-1]["path"].startswith("outside:")


def test_test_run_frames_are_691_for_researcher(env: Env) -> None:
    for rid in (TEST_RUN, MANAGED_TEST):
        for path in (f"/runs/{rid}/frames/uav-01/cam0", f"/runs/{rid}/frames/uav-01/cam0/0",
                     f"/runs/{rid}/frames/uav-01/cam0:range?seq_from=0&seq_to=1"):
            before = len(env.audit())
            body = _deny691(env, env.c.get(P + path, headers=env.h("researcher")), before, "alice")
            assert body["detail"]["run_id"] == rid and body["detail"]["split"] == "test"
            assert env.c.get(P + path, headers=env.h("evaluator")).status_code == 404    # 评测员：通过划分检查（无帧数据）
    assert env.c.get(f"{P}/runs/{DEV_RUN}/frames/uav-01/cam0", headers=env.h("researcher")).status_code == 404


def test_test_run_truth_reads_go_through_hidden(env: Env) -> None:
    before = len(env.audit())
    r = env.c.get(f"{P}/runs/{TEST_RUN}/truth/event/tape", headers=env.h("evaluator"))
    assert r.status_code == 200 and r.content == TAPE
    a = [x for x in env.audit()[before:] if x["action"] == sp.TRUTH_READ]
    assert [(x["principal"], x["run_id"], x["path"], x["result"]) for x in a] == [("eve", TEST_RUN, f"{TEST_EV}/event/v1/tape.bin", "ok")]
    before = len(env.audit())
    r = env.c.get(f"{P}/runs/{TEST_RUN}/truth/frames/uav-01/cam0/0", headers=env.h("evaluator"))
    assert r.status_code == 404                                                # hidden 中无真值帧索引
    a = [x for x in env.audit()[before:] if x["action"] == sp.TRUTH_READ]
    assert [(x["path"], x["result"]) for x in a] == [(f"{TEST_EV}/truth/run_{TEST_RUN}.mcap", "ok")]
    assert env.c.get(f"{P}/runs/{TEST_RUN}/truth/event/tape", headers=env.h("researcher")).json()["code"] == 115
    before = len(env.audit())                                                  # dev 磁带：不写 truth_read
    assert env.c.get(f"{P}/runs/{DEV_RUN}/truth/event/tape", headers=env.h("evaluator")).status_code == 200
    assert not [x for x in env.audit()[before:] if x["action"] == sp.TRUTH_READ]


def test_red_team_sequence_leaves_zero_external_truth_access(env: Env, capsys) -> None:
    """四类角色对全部 test 真值入口逐一访问；外部主体（受邀成员、研究者）全部被拒，审计中外部成功读取计数为 0。"""
    paths = [f"/events/{TEST_EV}", *(f"/events/{TEST_EV}/versions/1/artifacts/{n}" for n in sorted(sp.EVENT_ARTIFACTS - sp.PUBLIC_ARTIFACTS)),
             f"/runs/{TEST_RUN}/frames/uav-01/cam0", f"/runs/{TEST_RUN}/frames/uav-01/cam0/0", f"/runs/{TEST_RUN}/truth/event/tape",
             f"/runs/{TEST_RUN}/truth/frames/uav-01/cam0/0", f"/runs/{MANAGED_TEST}/frames/uav-01/cam0"]
    codes: dict[str, set] = {}
    for role in ROLES:
        for path in paths:
            r = env.c.get(P + path, headers=env.h(role))
            codes.setdefault(role, set()).add(r.json().get("code") if r.status_code >= 400 else r.status_code)
    assert codes["invited"] == {115} and codes["researcher"] <= {115, 691} and 691 in codes["researcher"]
    assert not ({115, 691} & (codes["evaluator"] | codes["admin"]))
    for p in (Principal("ivan", "invited", ("team-x",)), Principal("alice", "researcher", ("d3",))):      # 进程内绕过接口直接读 hidden
        with pytest.raises(sp.SplitAccessDenied):
            env.hidden.read_bytes(p, env.hidden.event_dir(TEST_EV, 1) / "tape.bin", event_id=TEST_EV)
    summ = sp.truth_access_summary(env.audit())
    assert summ["external"] == 0 and summ["external_principals"] == [] and summ["internal"] >= 4
    assert {"alice", "ivan"} <= set(summ["denied_principals"]) and summ["denied"] >= 2 + len(paths) - 3
    assert sp.main(["--data-root", str(env.s.data_root), "audit"]) == 0
    assert json.loads(capsys.readouterr().out)["external"] == 0
    # 反向：伪造一条外部主体的成功读取，统计与命令行都必须报出
    AuditLog(env.s.data_root / "audit").write(sp.TRUTH_READ, principal="mallory", role="researcher", result="ok", path=f"{TEST_EV}/truth/x")
    assert sp.truth_access_summary(env.audit())["external_principals"] == ["mallory"]
    assert sp.main(["--data-root", str(env.s.data_root), "audit"]) == 1


def test_audit_never_contains_tokens_or_content(env: Env) -> None:
    env.c.get(f"{P}/events/{TEST_EV}/versions/1/artifacts/tape.bin", headers=env.h("evaluator"))
    env.c.get(f"{P}/events/{TEST_EV}/versions/1/artifacts/tape.bin", headers=env.h("researcher"))
    raw = "".join(f.read_text(encoding="utf-8") for f in (env.s.data_root / "audit").glob("access-*.jsonl"))
    assert all(t not in raw for t in env.tokens.values()) and "d3bt_" not in raw and TAPE_SHA not in raw


def test_cli_set_show_seal_misplaced(tmp_path: Path, capsys) -> None:
    root = tmp_path / "d3"
    _event(root / "events", "t")
    args = ["--data-root", str(root)]
    assert sp.main([*args, "seal", "t"]) == 2                                   # 未登记为 test：拒绝迁移
    assert sp.main([*args, "set", "t", "test", "--by", "root", "--version", "t1"]) == 0
    capsys.readouterr()
    assert sp.main([*args, "misplaced"]) == 1 and len(json.loads(capsys.readouterr().out)["misplaced"]) == 6   # 磁带、真值、路线、编译报告与事件源
    assert sp.main([*args, "seal", "t"]) == 0
    capsys.readouterr()
    assert sp.main([*args, "misplaced"]) == 0
    capsys.readouterr()
    assert sp.main([*args, "show"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["events"] == {"t": "test"} and doc["test_split_version"] == "t1" and len(doc["digest"]) == 64
