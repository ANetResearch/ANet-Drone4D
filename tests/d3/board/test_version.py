"""排行榜 v1 的榜单版本与秘密种子集（D3-AC-43、D3-AC-57；M27-FR-164、FR-166、M27-AC-046）：`configs/bench.d3/board-v1` 定义
`std3-l2` 与 `std3-l1` 分轨、四个事件类型赛道、K = 5、R = 2 与 `d3-score-1`；封板只留 HMAC 承诺值，种子明文与 K_board 只在 EvalStore；
冻结项变更须新榜单版本，旧版本只读；置信区间以 HMAC(K_board, ·) 派生且同一输入逐字节一致。"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from awr.agentsdk.schemas import bench_validator
from awr.bench import boardstore
from awr.bench.boardstore import BoardVersionError, append_rows, publish
from awr.bench.eval import aggregate
from awr.bench.eval.aggregate import (
    EVENT_LANES,
    SCORE_V1,
    BoardConfigError,
    check_board_config,
    expand_runs,
    lane_metrics,
    load_board_template,
    score_definition,
    seal_board,
    verify_lock,
)

sys.path.insert(0, str(Path(__file__).parent))
from _board_v1 import SEAL, TEST_EVENTS, seal, v1_rows

ROOT = Path(__file__).resolve().parents[3]


def _publish(d: Path, rows: list[dict], n_boot: int = 300) -> dict:
    append_rows(d, rows)
    return publish(d, n_boot=n_boot)


def _rows() -> list[dict]:
    out = []
    for tr in ("std3-l2", "std3-l1"):
        out += v1_rows("baseline/track_share", (0.6, 0.4, 0.3, 0.2), track=tr, lanes=tuple(EVENT_LANES), noise=0.05, rng_seed=1, baseline=True)
        out += v1_rows("baseline/coverage_scan", (0.4, 0.3, 0.5, 0.1), track=tr, lanes=tuple(EVENT_LANES), noise=0.05, rng_seed=2, baseline=True)
    return out


def test_repo_template_defines_board_v1() -> None:
    cfg = load_board_template()
    assert check_board_config(cfg) == []
    assert cfg["board_version"] == "v1" and cfg["score_version"] == SCORE_V1 == "d3-score-1"
    assert [t["track"] for t in cfg["tracks"]] == ["std3-l2", "std3-l1"] and cfg["tracks"][0]["primary"] is True
    l2, l1 = cfg["tracks"]
    assert (l2["obs_level"], l1["obs_level"]) == ("L2", "L1")
    assert {k: l2[k] for k in ("fleet_ref", "vehicle_ref", "n_uav", "sensors")} == {k: l1[k] for k in ("fleet_ref", "vehicle_ref", "n_uav", "sensors")}
    assert l2["fleet_ref"] == "3x-p600_bench" and l2["n_uav"] == 3 and set(l2["sensors"]) == {"rgb1080-rs", "therm512", "mid360"}
    assert all(t["appearance"] == "mesh" for t in cfg["tracks"]) and cfg["physics"] == "realistic" and cfg["mode"] == "exogenous"
    assert {ln["lane"]: ln["event_type"] for ln in cfg["lanes"]} == EVENT_LANES
    assert (cfg["seeds_per_event"], cfg["repeats"], cfg["events_per_lane"]) == (5, 2, 2)          # 8 个 test 事件 × 5 种子 × 2 重复
    assert set(cfg["baselines"]) == {"static_guard", "coverage_scan", "track_share"}
    sd = score_definition(cfg["score_version"])
    assert not list(bench_validator("score").iter_errors(sd)) and sd["weights"] is None                # weights = null：不合成
    for f in (ROOT / "configs" / "bench.d3" / "board-v1").iterdir():                                   # 模板目录不含任何秘密
        doc = json.loads(f.read_text(encoding="utf-8"))
        assert aggregate._secret_paths(doc) == [], f


def test_seal_keeps_only_commitments_and_seeds_never_leave_the_evalstore(tmp_path: Path) -> None:
    d, cfg, store = seal(tmp_path)
    lock = cfg["lock"]
    assert [ln["test_events"] for ln in lock["lanes"]] == [["reid-t01", "reid-t02"], ["handoff-t01", "handoff-t02"], ["vehicle-t01", "vehicle-t02"],
                                                           ["search-t01", "search-t02"]]
    seeds = [s for lane in store["seeds"].values() for ss in lane.values() for s in ss]
    assert len(seeds) == 8 * 5 and len(set(seeds)) == 40 and 0 not in seeds and all(0 < s < 2 ** 63 for s in seeds)
    assert verify_lock(lock, store, seeds_per_event=5) == []
    board = _publish(d, _rows())
    assert not list(bench_validator("board").iter_errors(board))
    assert board["seed_set_commit"] == lock["seed_set_commit"] and [ln["seed_set_commit"] for ln in board["lanes"]] == [
        ln["seed_set_commit"] for ln in lock["lanes"]]
    texts = {p.name: p.read_text(encoding="utf-8") for p in d.iterdir() if p.is_file()}
    assert set(texts) >= {"config.json", "rows.jsonl", "board.json", "details.json", "listing.json"}
    for name, t in texts.items():                                                     # 种子明文与密钥不出现在配置、评测行与榜单制品中
        assert not any(str(s) in t for s in seeds), name
        assert store["k_eval"] not in t and store["k_board"] not in t, name
    assert all(x["seeds"] == [0, 1, 2, 3, 4] for x in json.loads(texts["details.json"])["baseline/track_share"]["tracks"]["std3-l2"]["handoff"])


def test_tampered_evalstore_fails_closed(tmp_path: Path) -> None:
    d, cfg, store = seal(tmp_path)
    bad = copy.deepcopy(store)
    bad["seeds"]["reid"]["reid-t01"][0] ^= 1
    probs = verify_lock(cfg["lock"], bad, seeds_per_event=5)
    assert any("reid/reid-t01" in p for p in probs) and any(p.startswith("reid:") for p in probs) and "board seed_set_commit" in probs[-1]
    assert not any(str(s) in " ".join(probs) for s in bad["seeds"]["reid"]["reid-t01"])               # 问题描述不含种子明文
    Path(cfg["evalstore"]).write_text(json.dumps(bad), encoding="utf-8")
    append_rows(d, _rows())
    with pytest.raises(BoardVersionError, match="does not match the evalstore"):
        publish(d, n_boot=100)
    Path(cfg["evalstore"]).unlink()
    with pytest.raises(BoardVersionError, match="not readable"):
        publish(d, n_boot=100)
    assert not (d / "board.json").exists()


def test_seal_assigns_test_events_by_event_type(tmp_path: Path) -> None:
    cfg = load_board_template()
    lock, _ = seal_board(cfg, TEST_EVENTS, **SEAL)
    assert {ln["lane"]: ln["event_type"] for ln in lock["lanes"]} == EVENT_LANES
    more = {**TEST_EVENTS, "reid-t03": "reid_occlusion"}
    with pytest.raises(BoardConfigError, match="needs 2"):                                     # 多于 2 个须在模板中显式列出
        seal_board(cfg, more, **SEAL)
    pick = copy.deepcopy(cfg)
    pick["lanes"][0]["test_events"] = ["reid-t03", "reid-t01"]
    lock, _ = seal_board(pick, more, **SEAL)
    assert lock["lanes"][0]["test_events"] == ["reid-t03", "reid-t01"]
    pick["lanes"][0]["test_events"] = ["reid-t03", "handoff-t01"]
    with pytest.raises(BoardConfigError, match="not test events of type reid_occlusion"):
        seal_board(pick, more, **SEAL)
    with pytest.raises(BoardConfigError, match="holds 1"):
        seal_board(cfg, {k: v for k, v in TEST_EVENTS.items() if k != "search-t02"}, **SEAL)
    with pytest.raises(BoardConfigError, match="platform"):
        seal_board(cfg, TEST_EVENTS, **{**SEAL, "platform": {"gz": "sha256:" + "1" * 64}})


@pytest.mark.parametrize("mutate, msg", [
    (lambda c: c["tracks"][0]["run_spec"].update(seeds=[1, 2, 3]), "secret key .tracks\\[0\\].run_spec.seeds"),
    (lambda c: c.update(k_board="00" * 32), "secret key .k_board"),
    (lambda c: c.update(physics="fast"), "physics must be realistic"),
    (lambda c: c["tracks"][1].update(appearance="3dgs-aerial"), "appearance mesh"),
    (lambda c: c["tracks"][1].update(track="std3-l2"), "unique ids"),
    (lambda c: c["lanes"][0].update(event_type="vehicle_track"), "one to one"),
    (lambda c: c.update(score_version="d3-score-0"), "score_version must be d3-score-1"),
    (lambda c: c.update(repeats=0), "repeats must be a positive integer"),
])
def test_board_config_rules(mutate, msg) -> None:
    cfg = load_board_template()
    mutate(cfg)
    assert any(__import__("re").search(msg, p) for p in check_board_config(cfg)), check_board_config(cfg)


def test_expansion_is_tracks_x_lanes_x_test_events_x_seeds_x_repeats(tmp_path: Path) -> None:
    _d, cfg, _ = seal(tmp_path)
    runs = expand_runs(cfg)
    assert len(runs) == 2 * 8 * 5 * 2 and len(expand_runs(cfg, "std3-l2")) == 80 and len(expand_runs(cfg, "std3-l1", ["reid"])) == 20
    assert {(r["track"], r["event"], r["seed_index"], r["rep"]) for r in runs} == {
        (t, e, k, rep) for t in ("std3-l2", "std3-l1") for e in TEST_EVENTS for k in range(5) for rep in range(2)}
    assert all(set(r) == {"track", "lane", "event", "seed_index", "rep"} for r in runs)          # 不含种子明文
    with pytest.raises(BoardConfigError, match="lock"):
        expand_runs(load_board_template())


def test_republish_is_byte_identical_and_frozen_items_need_a_new_version(tmp_path: Path, monkeypatch) -> None:
    d, _cfg, store = seal(tmp_path)
    _publish(d, _rows())
    before = (d / "board.json").read_bytes()
    publish(d, n_boot=300)
    assert (d / "board.json").read_bytes() == before                                     # 同一输入逐字节一致
    keep = (d / "config.json").read_text(encoding="utf-8")
    for over in ({"platform": {**SEAL["platform"], "gz": "sha256:" + "9" * 64}}, {"test_split_version": "splits-sha256:" + "b" * 16},
                 {"gpu_model": "NVIDIA L40S"}, {"cpu_isa_level": "x86-64-v4"}):
        seal(tmp_path, store=store, **over)                                              # 同一种子集、其他冻结项变更
        with pytest.raises(BoardVersionError, match="frozen items"):
            publish(d, n_boot=300)
        assert (d / "board.json").read_bytes() == before                                 # 旧版本只读
    seal(tmp_path)                                                                        # 新秘密种子集：承诺值变化
    with pytest.raises(BoardVersionError, match="seed_set_commit"):
        publish(d, n_boot=300)
    (d / "config.json").write_text(keep, encoding="utf-8")
    Path(json.loads(keep)["evalstore"]).write_text(json.dumps(store), encoding="utf-8")
    publish(d, n_boot=300)
    assert (d / "board.json").read_bytes() == before
    monkeypatch.setattr(aggregate, "EVALUATOR_VERSION", "1.0.0")                          # 评测器主版本变更
    with pytest.raises(BoardVersionError, match="evaluator major"):
        publish(d, n_boot=300)
    monkeypatch.undo()
    other = {**json.loads(keep), "score_version": "d3-score-0"}
    (d / "config.json").write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(BoardVersionError, match="publish a new board version"):
        publish(d, n_boot=300)
    assert (d / "board.json").read_bytes() == before


def test_score_1_needs_a_sealed_v1_config(tmp_path: Path) -> None:
    d = tmp_path / "x1"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"score_version": "d3-score-1"}))
    append_rows(d, v1_rows("baseline/a", (0.5, 0.5, 0.5, 0.5), baseline=True))
    with pytest.raises(BoardVersionError, match=r"board_config\.v1"):
        publish(d, n_boot=100)
    (d / "config.json").write_text(json.dumps({"score_version": "d3-score-9"}))
    with pytest.raises(BoardVersionError, match="platform knows"):
        publish(d, n_boot=100)
    unsealed = load_board_template()
    unsealed["board_version"] = "x1"
    (d / "config.json").write_text(json.dumps(unsealed))
    with pytest.raises(BoardVersionError, match="seal the board version"):
        publish(d, n_boot=100)
    assert not (d / "board.json").exists()


def test_bootstrap_rng_is_keyed_by_k_board(tmp_path: Path) -> None:
    d, _cfg, store = seal(tmp_path)
    rows = (v1_rows("t1/a", (0.6, 0.5, 0.4, 0.3), noise=0.1, rng_seed=5, baseline=True)
            + v1_rows("t2/b", (0.5, 0.5, 0.5, 0.3), noise=0.1, rng_seed=6, baseline=True))
    b1 = _publish(d, rows, n_boot=400)
    e1 = next(e for e in b1["entries"] if e["entry_id"] == "t1/a")
    use = [r for r in rows if r["entry_id"] == "t1/a"]
    met, _ = lane_metrics(use, key=("t1/a", "v1", "handoff"), n_boot=400, secret=bytes.fromhex(store["k_board"]))
    assert {m: met[m]["ci95"] for m in met} == {m: e1["metrics"][m]["ci95"] for m in met}           # 显示值 = 存储分量按评分版本聚合
    legacy, _ = lane_metrics(use, key=("t1/a", "v1", "handoff"), n_boot=400)
    assert legacy["ec"]["ci95"] != met["ec"]["ci95"]                                               # 不再用 sha256(entry_id ‖ ·)
    seal(tmp_path, store={**store, "k_board": "5a" * 32})                                           # 同一种子集、不同 K_board：承诺值不变
    b2 = publish(d, n_boot=400)
    e2 = next(e for e in b2["entries"] if e["entry_id"] == "t1/a")
    assert e2["metrics"]["ec"]["value"] == e1["metrics"]["ec"]["value"] and e2["metrics"]["ec"]["ci95"] != e1["metrics"]["ec"]["ci95"]


def test_board_api_reports_the_board_score_version(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("foxglove_schemas_protobuf")
    from fastapi.testclient import TestClient

    from awr.api.bench_main import create_app
    from awr.bench.settings import BenchSettings

    s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=("https://bench.example",), secure_cookie=False)
    d, cfg, _ = seal(s.data_root)
    _publish(d, _rows())
    (s.data_root / "boards" / "current").write_text("v1\n")
    app = create_app(s)
    c = TestClient(app)
    h = {"Authorization": f"Bearer {app.state.auth.issue_token('bob', 'invited')[1]}"}
    v = c.get("/api/bench/v1/board", headers=h).json()["versions"][0]
    assert v["score_version"] == "d3-score-1" and v["weights"] is None and [t["track"] for t in v["tracks"]] == ["std3-l2", "std3-l1"]
    assert v["seed_set_commit"] == cfg["lock"]["seed_set_commit"]
    lb = c.get("/api/bench/v1/leaderboard", headers=h, params={"track": "std3-l1", "lane": "search"}).json()
    assert {(e["track"], e["lane"]) for e in lb["entries"]} == {("std3-l1", "search")} and len(lb["entries"]) == 2
    body = json.dumps(lb)
    assert "composite" not in body and '"weights"' not in body and "k_board" not in body and "evalstore" not in body
    assert boardstore.load_config(d)["evalstore"]                                                  # 只在服务端配置中
