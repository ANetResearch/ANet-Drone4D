"""tests/d3/lab 的共用夹具（D3-AC-51）：bench-api 应用、四类角色的 bench token、dev 与 test 事件、三个项目的受管运行。

角色与数据范围（AWR-17 §18.3.5、§18.23.3）：受邀成员 ivan（团队 team-x）、研究者 alice（项目 d3）、评测员 eve、管理员 root；
另有研究者 zed（项目 other），用于"他人项目"用例。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

# D2 环境（.venv）无 D3 依赖：bench API 与回放读取经 awr.bench.recorder.foxglove 导入 foxglove_schemas_protobuf
pytest.importorskip("foxglove_schemas_protobuf")

from awr.api.bench_main import create_app
from awr.bench.runs_store import RunStore
from awr.bench.settings import BenchSettings

P = "/api/bench/v1"
ORIGIN = "https://lab.example"
ROOT = Path(__file__).resolve().parents[3]
CROP = ROOT / "worlds" / "oryahovo-crop1"
ROLE_PRINCIPALS = {"invited": ("ivan", ("team-x",)), "researcher": ("alice", ("d3",)), "evaluator": ("eve", ()), "admin": ("root", ())}

RUN_D3 = "run-a000000001"          # 项目 d3（alice 提交）
RUN_TEAM = "run-b000000001"        # 团队 team-x（受邀成员可见）
RUN_OTHER = "run-c000000001"       # 项目 other（zed 提交）
RUN_TEST = "run-d000000001"        # team-x 的 test 划分运行（受邀成员不可见）
ROLES = ("invited", "researcher", "evaluator", "admin")

# 权限矩阵的测试侧口径（不从实现读取）：已交付操作 → 最低角色。依据 AWR-05 §7.11 角色与权限矩阵、AWR-17 §18.3.5、§18.4、§18.23.3、
# 14 §21.2.5：世界、事件、dev 运行的只读接口与会话读取为 invited；运行取消与重试、短期令牌、帧与评测结果为 researcher；
# 真值帧与事件磁带为 evaluator；内部提交与排行榜为 invited；建立会话无需身份（none）。
EXPECTED: dict[tuple[str, str], str] = {
    ("GET", "/meta"): "invited",
    ("POST", "/session"): "none",
    ("GET", "/session"): "invited",
    ("DELETE", "/session"): "invited",
    ("POST", "/session/short-token"): "researcher",
    ("GET", "/worlds"): "invited",
    ("GET", "/worlds/{wid}"): "invited",
    ("GET", "/events"): "invited",
    ("GET", "/events/{eid}"): "invited",
    ("GET", "/runs"): "invited",
    ("GET", "/runs/{rid}"): "invited",
    ("DELETE", "/runs/{rid}"): "researcher",
    ("POST", "/runs/{rid}/retry"): "researcher",
    ("GET", "/runs/{rid}/evaluation"): "researcher",
    ("GET", "/runs/{rid}/frames/{uav}/{sensor}"): "researcher",
    ("GET", "/runs/{rid}/frames/{uav}/{sensor}/{seq}"): "researcher",
    ("GET", "/runs/{rid}/frames/{uav}/{sensor}:range"): "researcher",
    ("GET", "/runs/{rid}/truth/frames/{uav}/{sensor}/{seq}"): "evaluator",
    ("GET", "/runs/{rid}/truth/event/tape"): "evaluator",
    ("GET", "/runs/{rid}/artifacts"): "researcher",
    ("GET", "/runs/{rid}/artifacts/{path}"): "researcher",
    ("GET", "/samples"): "researcher",
    ("GET", "/runs/{rid}/viewer_url"): "researcher",
    ("GET", "/board"): "invited",
    ("GET", "/board/{track}/entries/{entry_id}"): "invited",
    ("GET", "/leaderboard"): "invited",
    ("GET", "/submissions"): "invited",
    ("POST", "/submissions"): "invited",
    ("GET", "/submissions/{sid}"): "invited",
    ("DELETE", "/submissions/{sid}"): "invited",
    ("POST", "/submissions/{sid}/visibility"): "invited",
    ("POST", "/registry/token"): "invited",
}


def pytest_generate_tests(metafunc) -> None:
    """`op` × `role`：权限矩阵逐项用例（全部已交付操作 × 4 类角色）。"""
    if "op" in metafunc.fixturenames and "role" in metafunc.fixturenames:
        cases = [(op, r) for op in EXPECTED for r in ROLES]
        metafunc.parametrize(("op", "role"), cases, ids=[f"{m} {path} [{r}]" for (m, path), r in cases])


def _world(root: Path, wid: str) -> None:
    d = root / wid
    (d / "qa").mkdir(parents=True)
    for rel in ("world.json", "coordinate.json", "qa/report.json"):
        shutil.copyfile(CROP / rel, d / rel)
    w = json.loads((d / "world.json").read_text(encoding="utf-8"))
    w["id"] = wid
    (d / "world.json").write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")


def _event(root: Path, eid: str, *, split: str | None = None) -> None:
    d = root / eid / "v1"
    d.mkdir(parents=True)
    world = {"id": "oryahovo-crop1", "content_version": "cv-1"}
    (d / "plan.json").write_text(json.dumps({"event_id": eid, "version": 1, "world": world, "duration_ms": 60_000, "actors": []}))
    (d / "event.json").write_text(json.dumps({"event_id": eid, "world": world, **({"split": split} if split else {})}))
    (d / "compile_report.json").write_text(json.dumps({"event_id": eid, "version": 1, "status": "compiled", "gates": []}))


def enqueue(st: RunStore, rid: str, *, tenant: str, by: str, split: str = "dev") -> dict:
    spec = {"schema": "drone4d.bench.run_spec.v1", "policy": {"kind": "reference", "name": "track_share"}, "physics": "realistic",
            "obs_level": "L1", "fleet": [{"model": "x500", "count": 3}], "duration_s": 60.0}
    return st.enqueue(rid, experiment_id="exp-aaaaaaaaaa", event_id="abc-crossing", world_id="oryahovo-crop1", spec=spec, tenant=tenant,
                      submitted_by=by, seed=7, seed_commit="e" * 64, split=split)


@dataclass
class Lab:
    """用例经夹具取得的环境；常量以类属性给出（--import-mode=importlib 下用例不能直接导入 conftest）。"""

    P: ClassVar[str] = P
    ORIGIN: ClassVar[str] = ORIGIN
    RUN_D3: ClassVar[str] = RUN_D3
    RUN_TEAM: ClassVar[str] = RUN_TEAM
    RUN_OTHER: ClassVar[str] = RUN_OTHER
    RUN_TEST: ClassVar[str] = RUN_TEST
    PRINCIPALS: ClassVar[dict] = ROLE_PRINCIPALS
    ROLES: ClassVar[tuple] = ROLES
    EXPECTED: ClassVar[dict] = EXPECTED

    app: object
    client: TestClient
    settings: BenchSettings
    tokens: dict[str, str] = field(default_factory=dict)          # 角色 → 明文 bench token
    token_ids: dict[str, str] = field(default_factory=dict)

    def bearer(self, role: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[role]}"}

    def session_client(self, role: str) -> TestClient:
        """以该角色的 bench token 换取会话 Cookie 的独立客户端。"""
        c = TestClient(self.app)
        assert c.post(f"{P}/session", json={"token": self.tokens[role]}).status_code == 204
        return c

    def audit(self) -> list[dict]:
        return self.app.state.auth.audit.read()

    def runs(self) -> RunStore:
        return self.app.state.runs


def make_lab(tmp: Path, **kw) -> Lab:
    s = BenchSettings(data_root=tmp / "d3", allowed_origins=(ORIGIN,), secure_cookie=kw.pop("secure_cookie", False), **kw)
    _world(s.worlds_dir, "oryahovo-crop1")
    ev = s.data_root / "events"
    _event(ev, "abc-crossing")
    _event(ev, "tst-hidden", split="test")
    app = create_app(s)
    app.state.runs = st = RunStore(s.db_path)
    enqueue(st, RUN_D3, tenant="d3", by="alice")
    enqueue(st, RUN_TEAM, tenant="team-x", by="ivan")
    enqueue(st, RUN_OTHER, tenant="other", by="zed")
    enqueue(st, RUN_TEST, tenant="team-x", by="ivan", split="test")
    lab = Lab(app, TestClient(app), s)
    for role, (name, projects) in ROLE_PRINCIPALS.items():
        lab.token_ids[role], lab.tokens[role] = app.state.auth.issue_token(name, role, projects)
    lab.token_ids["other"], lab.tokens["other"] = app.state.auth.issue_token("zed", "researcher", ("other",))
    return lab


@pytest.fixture()
def lab(tmp_path: Path) -> Lab:
    return make_lab(tmp_path)


@pytest.fixture()
def lab_factory(tmp_path: Path):
    """按设置覆盖项构造独立环境（如 `secure_cookie=True`）。"""
    n = [0]

    def make(**kw) -> Lab:
        n[0] += 1
        return make_lab(tmp_path / f"lab{n[0]}", **kw)
    return make
