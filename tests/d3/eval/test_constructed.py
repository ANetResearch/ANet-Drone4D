"""D3-AC-18 评测构造用例（M27-AC-014、015、017、044；合成世界、运动学开发档）：

- CE v0：机队中单机取得全部覆盖 `Ĝ = 0`；3 机按时间均分全覆盖 `Ĝ = 1`；超预算 3 倍 `f_res = 0`（另核 1 倍、2 倍）；
- 安全：构造穿越演员包围体的运行 `ranked_valid = false`、`invalid_reasons = [actor_penetration]` 且四项分量记 0；
- EU v0：L2 框证据 IoU 0.31 关联、0.29 不关联；L2 点证据落在膨胀 3 px 的可见掩膜内关联（掩膜孔洞与 4 px 外不关联，
  等距平票为未解析）；L1 `det_id` 关联、伪造 `det_id` 不关联；点证据经 report 匹配到事件；
- AO v0：覆盖扫描策略 `AO_real ≤ 0.05` 且 `AO_pos ≥ 0.85`（正式数字在 `crop-handoff-001` 上由 A10 采集）；
- `run_manifest.json` 通过 schema 校验，排名运行的清单只含 `seed_commit`（任何位置无种子明文）；
- `evaluation.json` 对同一输入逐字节一致（含换目录复评）。"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from awr.agentsdk.schemas import bench_validator
from awr.bench.eval.ce import collaborative
from awr.bench.eval.eu import decode_mask, event_understanding, point_actor, resolve
from awr.bench.eval.evaluator import check_run_manifest, dev_seed_commit, evaluate_run, run_manifest, write_run_manifest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "env"))

U3 = ["uav-01", "uav-02", "uav-03"]
E3 = [{"id": "m3", "type": "proximity_encounter", "participants": ["a", "b", "c"], "a_s": 0.0, "b_s": 5.0, "multi": True, "params": {}}]
N = 60                                             # tick（10 Hz）：事件 [0, 5] s 覆盖 6 个 1 s 桶（0–5）


def _present(ids=("a", "b", "c"), n: int = N) -> dict[str, np.ndarray]:
    return {p: np.ones(n + 1, bool) for p in ids}


def _ce_run(U: list[str], *, km_per_uav: float = 0.0, kb_per_uav: float = 0.0, budget_km: float = 10.0, budget_kb: float = 2000.0,
            n: int = N) -> SimpleNamespace:
    """CE 的运行替身：每机沿 x 匀速直线飞行 `km_per_uav`，发送 `kb_per_uav` KB。"""
    pos = np.zeros((n + 1, len(U), 3))
    pos[:, :, 0] = np.linspace(0.0, km_per_uav * 1000.0, n + 1)[:, None]
    return SimpleNamespace(fleet={"pos": pos}, manifest={"agents": {u: {"comm": {"bytes": int(kb_per_uav * 1000)}} for u in U}}, net=[],
                           task={"budget": {"distance_km_per_uav": budget_km, "comm_kb_per_uav": budget_kb}})


# ---------------------------------------------------------------------------------------------------------------- CE v0


def test_ce_single_uav_full_coverage_gives_g_hat_0() -> None:
    """3 机机队中 uav-01 独自在全部 tick 识别全部参与者，uav-02 冗余识别其中一段：联合覆盖不超过最佳单机，Ĝ = 0。"""
    ident = {k: {("uav-01", p) for p in "abc"} for k in range(N + 1)}
    for k in range(10, 30):
        ident[k] |= {("uav-02", "a"), ("uav-02", "b")}
    raw, ce, norm = collaborative(E3, U3, ident, _present(), N, _ce_run(U3))
    assert norm["V_fleet"] == 1.0 and norm["V_best_single"] == 1.0
    assert norm["g_hat"] == 0.0 and ce == 0.0
    assert raw["gain"] == 0.0 and raw["redundancy"] > 1.0                             # 冗余照常报告


def test_ce_three_uav_equal_split_gives_g_hat_1() -> None:
    """3 机按时间均分：每机负责 2 个 1 s 桶并在其中同时识别全部参与者 → V(U) = 1、max V({u}) = 1/3，Ĝ = (1 − 1/3)/(1 − 1/3) = 1。"""
    ident: dict[int, set] = {}
    for i, u in enumerate(U3):
        for k in range(20 * i, 20 * i + 20):
            ident[k] = {(u, p) for p in "abc"}
    raw, ce, norm = collaborative(E3, U3, ident, _present(), N, _ce_run(U3))
    assert norm["V_fleet"] == 1.0 and norm["V_best_single"] == pytest.approx(1 / 3)
    assert norm["g_hat"] == 1.0 and norm["f_res"] == 1.0 and ce == 1.0
    assert raw["gain"] == 0.0                                                         # 每机在自己的时段内都单独捕获事件：EC 增益为 0，Ĝ 衡量联合覆盖
    # 按参与者均分（每机只识别一名）：V(U) = 1、max V({u}) = 0，截断到 1
    ident = {k: {(u, p) for u, p in zip(U3, "abc", strict=True)} for k in range(N + 1)}
    _raw, ce, norm = collaborative(E3, U3, ident, _present(), N, _ce_run(U3))
    assert norm["V_best_single"] == 0.0 and norm["g_hat"] == 1.0 and ce == 1.0


@pytest.mark.parametrize(("ratio", "f_res"), [(1.0, 1.0), (2.0, 0.5), (3.0, 0.0), (4.0, 0.0)])
def test_ce_budget_ratio(ratio: float, f_res: float) -> None:
    """航程与通信同为预算的 `ratio` 倍（rho = ratio）：f_res = clip(1 − 0.5·(rho − 1), 0, 1)，超预算 3 倍 f_res = 0、CE = 0。"""
    ident: dict[int, set] = {}
    for i, u in enumerate(U3):
        for k in range(20 * i, 20 * i + 20):
            ident[k] = {(u, p) for p in "abc"}
    run = _ce_run(U3, km_per_uav=2.0 * ratio, kb_per_uav=100.0 * ratio, budget_km=2.0, budget_kb=100.0)
    raw, ce, norm = collaborative(E3, U3, ident, _present(), N, run)
    assert norm["rho"] == pytest.approx(ratio)
    assert norm["f_res"] == pytest.approx(f_res, abs=1e-12) and ce == pytest.approx(f_res, abs=1e-12)
    assert raw["flight_km"] == pytest.approx(3 * 2.0 * ratio) and raw["comm_kb"] == pytest.approx(3 * 100.0 * ratio)


def test_ce_single_uav_fleet_is_undefined() -> None:
    ident = {k: {("uav-01", p) for p in "abc"} for k in range(N + 1)}
    _raw, ce, norm = collaborative(E3, ["uav-01"], ident, _present(), N, _ce_run(["uav-01"]))
    assert ce is None and norm["g_hat"] is None


# ---------------------------------------------------------------------------------------------------------------- EU v0


def _iou_run(**kw) -> SimpleNamespace:
    return SimpleNamespace(det_actor=kw.get("det_actor", {}), vis_boxes=kw.get("vis_boxes", {}))


def _ev(**kw) -> dict:
    return {"uav": "uav-01", "sensor": "eo", "frame_seq": 7, **kw}


def test_eu_l2_box_iou_threshold() -> None:
    """可见框 [0, 0, 100, 100]：证据框 [0, 0, w, 100] 的 IoU = w / 100；0.31 关联成功，0.29 失败（阈值 0.3）。"""
    run = _iou_run(vis_boxes={("uav-01", 7): [{"actor": "person_a", "bbox_xywh": [0, 0, 100, 100]}]})
    assert resolve({"evidence": [_ev(bbox_xywh=[0, 0, 31, 100])]}, run) == "person_a"
    assert resolve({"evidence": [_ev(bbox_xywh=[0, 0, 29, 100])]}, run) is None
    assert resolve({"evidence": [_ev(bbox_xywh=[0, 0, 31, 100], frame_seq=8)]}, run) is None     # 证据帧无该演员


def _masks() -> tuple[np.ndarray, list[dict]]:
    """720 × 1280 实例图：演员 1（person_a）为 30 × 14 的 U 形（x 200–213、y 100–129，中间 x 202–211、y 100–119 为 10 px 宽的缺口），
    演员 2（person_b）为 x 222–227、y 100–129 的矩形（与 a 相隔 8 px）。"""
    from awr.agentsdk.env.l2 import mask_code

    inst = np.zeros((720, 1280), np.uint16)
    inst[100:130, 200:214] = 1
    inst[100:120, 202:212] = 0
    inst[100:130, 222:228] = 2
    rows = []
    for i, actor in ((1, "person_a"), (2, "person_b")):
        ys, xs = np.nonzero(inst == i)
        box = [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
        rows.append({"actor": actor, "bbox_xywh": box, "mask": mask_code(inst, i, box)})
    return inst, rows


def test_eu_mask_code_roundtrip() -> None:
    from awr.agentsdk.env.l2 import mask_code

    inst, rows = _masks()
    for i, r in enumerate(rows, start=1):
        x, y, w, h = r["bbox_xywh"]
        assert np.array_equal(decode_mask(r["mask"], w, h), inst[y:y + h, x:x + w] == i)
    assert mask_code(inst, 1, [0, 0, 0, 0]) is None                                       # 不可见：无掩膜


def test_eu_l2_point_in_dilated_mask() -> None:
    """点证据取所在像素 (⌊x⌋, ⌊y⌋)：在掩膜内或距掩膜 ≤ 3 px（圆盘膨胀）关联，4 px 外不关联；可见框内但距掩膜 > 3 px 的缺口
    中心不关联（按掩膜而非框）；两名演员之间按最近者关联，均超过 3 px 为未解析。"""
    _inst, rows = _masks()
    assert point_actor([201.5, 125.5], rows) == "person_a"                              # 掩膜内
    assert point_actor([197.9, 110.0], rows) == "person_a"                              # ⌊197.9⌋ = 197：距左缘 x = 200 为 3
    assert point_actor([196.9, 110.0], rows) is None                                    # 196：距 4
    assert point_actor([205.5, 132.0], rows) == "person_a"                              # 距下缘 y = 129 为 3
    assert point_actor([205.5, 133.0], rows) is None                                    # 距 4
    assert point_actor([204.0, 100.0], rows) == "person_a"                              # 缺口内距左壁 x = 201 为 3
    assert point_actor([206.0, 100.0], rows) is None                                    # 缺口中心：框内，距左壁 5、右壁 6、底 20
    assert point_actor([216.0, 115.0], rows) == "person_a"                              # 两演员之间：距 a（213）3、距 b（222）6
    assert point_actor([217.0, 115.0], rows) is None                                    # 距 a 4、距 b 5
    assert point_actor([219.0, 115.0], rows) == "person_b"                              # 距 b 3
    assert point_actor([500.0, 500.0], rows) is None
    assert point_actor([201.0, 110.0], [{**rows[0], "mask": None}]) is None             # 未记录掩膜的旧运行：不参与
    run = _iou_run(vis_boxes={("uav-01", 7): rows})
    assert resolve({"evidence": [_ev(point_xy=[216.0, 115.0])]}, run) == "person_a"
    assert resolve({"evidence": [_ev(point_xy=[206.0, 100.0])]}, run) is None
    # 多数表决：两条点证据指向 b、一条指向 a → b；一比一为未解析
    assert resolve({"evidence": [_ev(point_xy=[201.5, 125.5]), _ev(point_xy=[224.0, 110.0]), _ev(point_xy=[225.0, 111.0])]}, run) == "person_b"
    assert resolve({"evidence": [_ev(point_xy=[201.5, 125.5]), _ev(point_xy=[224.0, 110.0])]}, run) is None


def test_eu_l2_point_tie_is_unresolved() -> None:
    from awr.agentsdk.env.l2 import mask_code

    inst = np.zeros((64, 64), np.uint16)
    inst[10:20, 10:14] = 1                            # a：x 10–13
    inst[10:20, 18:22] = 2                            # b：x 18–21；x = 15/16 处两者距离为 2
    rows = [{"actor": a, "bbox_xywh": b, "mask": mask_code(inst, i, b)} for i, a, b in ((1, "a", [10, 10, 4, 10]), (2, "b", [18, 10, 4, 10]))]
    assert point_actor([15.5, 12.0], rows) == "a"                                         # 距 a 2、距 b 3
    assert point_actor([16.5, 12.0], rows) == "b"
    run = _iou_run(vis_boxes={("uav-01", 7): rows})
    assert resolve({"evidence": [_ev(point_xy=[15.5, 12.0])]}, run) == "a"
    inst2 = np.zeros((64, 64), np.uint16)
    inst2[10:20, 10:14] = 1
    inst2[10:20, 17:21] = 2                           # x = 15 处距 a 2、距 b 2：平票
    rows2 = [{"actor": a, "bbox_xywh": b, "mask": mask_code(inst2, i, b)} for i, a, b in ((1, "a", [10, 10, 4, 10]), (2, "b", [17, 10, 4, 10]))]
    assert point_actor([15.0, 12.0], rows2) is None


def test_eu_l1_det_id_and_forged_det_id() -> None:
    run = _iou_run(det_actor={("uav-01", 7, "0123456789abcdef"): {"actor": "person_a"}})
    assert resolve({"evidence": [_ev(det_id="0123456789abcdef")]}, run) == "person_a"
    assert resolve({"evidence": [_ev(det_id="fedcba9876543210")]}, run) is None                        # 伪造 det_id
    assert resolve({"evidence": [_ev(det_id="0123456789abcdef", frame_seq=8)]}, run) is None           # 帧号不符
    assert resolve({"evidence": [_ev(det_id="0123456789abcdef", uav="uav-02")]}, run) is None          # 机号不符


def test_eu_point_evidence_matches_report() -> None:
    """report 的两名参与者各以点证据给出：经掩膜关联为 {person_a, person_b}，与计分事件一对一匹配。"""
    _inst, rows = _masks()
    run = _iou_run(vis_boxes={("uav-01", 7): rows})
    E = [{"id": "m", "type": "proximity_encounter", "participants": ["person_a", "person_b"], "a_s": 1.0, "b_s": 2.0, "multi": True}]
    rep = {"report": {"type": "meet", "window": {"t_start_ns": 1_000_000_000, "t_end_ns": 2_000_000_000},
                      "participants": [{"track_id": "t0", "evidence": [_ev(point_xy=[201.5, 125.5])]},
                                       {"track_id": "t1", "evidence": [_ev(point_xy=[224.0, 110.0])]}]}}
    eu = event_understanding([rep], E, run)
    assert [m["event_id"] for m in eu["matches"]] == ["m"] and eu["f1"] == 1.0
    assert eu["participants"] == {"resolved": 2, "unresolved": 0}
    rep["report"]["participants"][1]["evidence"] = [_ev(point_xy=[240.0, 110.0])]       # 远离掩膜：参与者未解析 → 不匹配
    eu = event_understanding([rep], E, run)
    assert eu["matches"] == [] and eu["participants"] == {"resolved": 1, "unresolved": 1}


# ---------------------------------------------------------------------------------------- Env 运行（安全、AO、清单、逐字节）


@pytest.fixture(scope="module")
def track_run(synth_event, tmp_path_factory):
    """track_share 100 s（2 机，运动学开发档）的运行目录（EC > 0，作安全用例的对照）。"""
    from anet4d.baselines import BASELINES
    from test_env import _run, _spec

    d = tmp_path_factory.mktemp("constructed") / "track"
    m = _run(_spec(synth_event, d, dur=100.0), lambda u: BASELINES["track_share"]())
    return d, m


def _as_realistic(src: Path, dst: Path) -> Path:
    """复制运行目录并把物理档标为 realistic（构造用例：排除 `physics_not_ranked`，只看安全判定）。"""
    shutil.copytree(src, dst)
    p = dst / "manifest.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m["tier"] = "realistic"
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return dst


def _penetrate(run_dir: Path, ev: Path, actor: str = "person_a") -> int:
    """把 uav-01 在一个空中 tick 的真值位置放到演员胸口高度（人员胶囊内）→ 该 tick。"""
    from awr.sim.events.tape import open_tape

    tape = open_tape(ev)
    info = {a["id"]: a for a in tape.actors_info()}
    fz = np.load(run_dir / "truth" / "fleet.npz")
    f = {k: fz[k] for k in fz.files}
    air = np.isin(f["status"][:, 0], ("AIRBORNE", "LANDING"))
    for k in np.nonzero(air)[0]:
        st = tape.actor_states(int(k) * 100_000_000, [info[actor]["idx"]])
        if st.state[0] in (1, 2):
            f["pos"][k, 0] = st.pos_enu_m[0] + np.array([0.0, 0.0, 0.9])
            np.savez_compressed(run_dir / "truth" / "fleet.npz", **f)
            return int(k)
    raise AssertionError("no airborne tick with the actor present")


def test_actor_penetration_invalidates_and_zeroes_components(synth_event, track_run, tmp_path: Path) -> None:
    from awr.agentsdk.env.core import _open_world

    w, ev = synth_event
    world = _open_world(w)
    clean = evaluate_run(_as_realistic(track_run[0], tmp_path / "clean"), ev, world)
    assert clean["invalid_reasons"] == [] and clean["ranked_valid"] is True and clean["safety"]["penetrations"] == 0
    assert clean["components"]["ec"] > 0.0                                                # 对照：未穿越时分量非 0
    pen_dir = _as_realistic(track_run[0], tmp_path / "pen")
    _penetrate(pen_dir, ev)
    doc = evaluate_run(pen_dir, ev, world)
    assert not [e.message for e in bench_validator("evaluation").iter_errors(doc)]
    assert doc["safety"]["penetrations"] >= 1 and doc["safety"]["min_sep_m"]["person"] <= 0.0
    assert doc["safety"]["violations"]["person"] >= 1                                     # 间距计入安全指标
    assert doc["invalid_reasons"] == ["actor_penetration"] and doc["ranked_valid"] is False
    assert doc["components"] == {"ec": 0.0, "eu": 0.0, "ao": 0.0, "ce": 0.0}
    assert doc["ec"]["value"] == clean["ec"]["value"]                                    # 原始指标保留供诊断，只有分量记 0
    kin = evaluate_run(track_run[0], ev, world)                                          # 开发档：只标不进正式榜，分量照常
    assert kin["invalid_reasons"] == ["physics_not_ranked"] and kin["components"] == clean["components"]


def test_coverage_scan_ao_real_low_ao_pos_high(synth_event, tmp_path: Path) -> None:
    """覆盖扫描（近正下视、不跟踪）：机位常在 oracle 最优区内（AO_pos 高），实际证据质量近 0（AO_real 低）。"""
    from anet4d.baselines import BASELINES
    from test_env import _run, _spec

    from awr.agentsdk.env.core import _open_world

    w, ev = synth_event
    _run(_spec(synth_event, tmp_path / "scan", dur=60.0), lambda u: BASELINES["coverage_scan"]())
    doc = evaluate_run(tmp_path / "scan", ev, _open_world(w))
    assert doc["ao"]["ao_real"] <= 0.05 and doc["ao"]["ao_pos"] >= 0.85
    assert doc["ao"]["oracle"]["version"] == "ao-oracle-v0-grid@1"


def test_evaluation_json_is_byte_identical(synth_event, track_run, tmp_path: Path) -> None:
    from awr.agentsdk.env.core import _open_world

    w, ev = synth_event

    def dump(d: Path) -> bytes:
        return (json.dumps(evaluate_run(d, ev, _open_world(w)), ensure_ascii=False, indent=1) + "\n").encode()

    a = dump(track_run[0])
    assert dump(track_run[0]) == a
    shutil.copytree(track_run[0], tmp_path / "moved")
    assert dump(tmp_path / "moved") == a                                                   # 与运行目录路径无关


def _commit(run_id: str, seed: int) -> str:
    return hmac.new(b"k-eval-test", f"{run_id}:{seed}".encode(), hashlib.sha256).hexdigest()


def test_run_manifest_dev_run(synth_event, track_run, tmp_path: Path) -> None:
    from test_env import _spec

    from awr.agentsdk.env.core import _open_world

    w, ev = synth_event
    d = track_run[0]
    spec = _spec(synth_event, None, dur=100.0)
    evdoc = evaluate_run(d, ev, _open_world(w))
    man = run_manifest(spec, d, evdoc, experiment_id="exp-aaaaaaaaaa")
    assert check_run_manifest(man) == [] and not list(bench_validator("run_manifest").iter_errors(man))
    assert man["seed"] == 5 and man["seed_commit"] == dev_seed_commit(spec.run_id, 5)
    assert man["ranked"] is False and man["ranked_valid"] is False and man["invalid_reasons"] == ["physics_not_ranked"]
    assert man["run_hash"] == spec.run_hash() and man["spec"]["physics"] == "fast" and man["determinism_level"] == "1b"
    assert man["timing"]["sim_s"] == 100.0 and set(man["agents"]) == {"uav-01", "uav-02"}
    b1 = write_run_manifest(tmp_path, man).read_bytes()
    b2 = write_run_manifest(tmp_path, run_manifest(spec, d, evdoc, experiment_id="exp-aaaaaaaaaa")).read_bytes()
    assert b1 == b2
    assert run_manifest(spec, d, None, experiment_id="exp-aaaaaaaaaa")["ranked_valid"] is None        # 评测回填前


def test_run_manifest_ranked_carries_seed_commit_only(synth_event, track_run, tmp_path: Path) -> None:
    from awr.agentsdk.env.spec import EnvSpec

    w, ev = synth_event
    seed = 7_391_204_557_118_421
    rs = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": "ranked-check"}, "world": {"id": w.name},
          "event": {"id": ev.parent.name, "version": 1},
          "fleet": [{"uav_id": u, "vehicle_ref": "x500", "start": {"kind": "pose", "pos_enu_m": [x, 50.0, 0.0], "yaw_deg": 0.0}}
                    for u, x in (("uav-01", -10.0), ("uav-02", 10.0))],
          "policy": {"kind": "reference", "name": "track_share"}, "physics": "realistic", "obs_level": "L1", "ranked": True,
          "seed_count": 10, "duration_s": 100.0}
    kw = {"run_id": "run-abcdefghij", "seed": seed, "worlds_root": w.parent, "events_root": ev.parent.parent, "out_dir": None}
    with pytest.raises(ValueError, match="seed_commit"):
        run_manifest(EnvSpec.from_run_spec(rs, **kw), track_run[0], experiment_id="exp-abcdefghij")
    c = _commit("run-abcdefghij", seed)
    spec = EnvSpec.from_run_spec(rs, **kw, seed_commit=c)
    evdoc = {"ranked_valid": True, "invalid_reasons": []}
    man = run_manifest(spec, track_run[0], evdoc, experiment_id="exp-abcdefghij", split="test")
    assert check_run_manifest(man) == [] and not list(bench_validator("run_manifest").iter_errors(man))
    assert man["seed_commit"] == c and "seed" not in man and not {"seed", "seeds", "seed_count"} & set(man["spec"])
    text = write_run_manifest(tmp_path, man).read_text(encoding="utf-8")
    assert str(seed) not in text and format(seed, "x") not in text                       # 十进制与十六进制明文均不出现
    assert man["spec"]["physics"] == "realistic" and man["ranked"] is True and man["ranked_valid"] is True
    leak = {**man, "seed": seed}
    assert any("'seed'" in e for e in check_run_manifest(leak))
    leak = {**man, "spec": {**man["spec"], "seeds": [seed]}}
    assert any("'seeds'" in e for e in check_run_manifest(leak))
    assert check_run_manifest({**man, "seed_commit": "x" * 64})                             # schema：承诺值须为 64 位十六进制


def test_l2_rows_with_masks_load_into_run(synth_event, track_run, tmp_path: Path) -> None:
    """truth/l2.jsonl 的 `mask` 经 `load_run` 进入 `vis_boxes`，点证据在真实运行目录上关联。"""
    from awr.bench.eval.run import load_run

    _w, ev = synth_event
    d = tmp_path / "l2"
    shutil.copytree(track_run[0], d)
    _inst, rows = _masks()
    with (d / "truth" / "l2.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"uav": "uav-01", "sensor": "eo", "seq": 7, "t_ns": 700_000_000, "h_unocc_px": 30, "vis": 1.0, **r}) + "\n")
    run = load_run(d, ev)
    assert resolve({"evidence": [_ev(point_xy=[201.5, 125.5])]}, run) == "person_a"
    assert resolve({"evidence": [_ev(point_xy=[224.0, 110.0])]}, run) == "person_b"
