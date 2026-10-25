"""参考检测器 `ref_det_v1` 的定稿与 `std` 档每步墙钟（D3-AC-58；M26-FR-121、FR-126）：随包权重 sha256 固定且载入时校验、
模型卡的 torch/numpy 比对与每步墙钟记录、SDK 常量与模型卡一致；录放工具（tools/bench/d3/refdet/step_wall.py）的线协议回放、
ACT 逐字节比对与分位数汇总；比对工具（parity.py）与定稿工具（freeze.py）的判定。"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PIL")
pytest.importorskip("msgpack")

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "tools/sdk/drone4d/src"), str(ROOT / "tools/bench/d3/refdet")]

import freeze as FZ  # noqa: E402
import parity as PA  # noqa: E402
import step_wall as SW  # noqa: E402
from drone4d.perception import ref_det as R  # noqa: E402

CARD = json.loads(R.WEIGHTS.with_name("model_card.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------- 定稿记录
def test_bundled_weights_frozen_by_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = R.WEIGHTS.read_bytes()
    assert hashlib.sha256(data).hexdigest() == R.WEIGHTS_SHA256 == CARD["release"]["weights"]["sha256"]
    assert CARD["release"]["weights"]["bytes"] == len(data) and CARD["release"]["version"] == R.VERSION
    R.RefDet()
    bad = tmp_path / "ref_det_v1.npz"
    p = dict(np.load(R.WEIGHTS))
    p["h2.b"] = p["h2.b"] + np.float32(1e-3)
    np.savez(bad, **p)
    monkeypatch.setattr(R, "WEIGHTS", bad)
    with pytest.raises(ValueError, match="sha256"):
        R.RefDet()
    R.RefDet(bad)                                                # 显式路径（训练与比对用）不校验


def test_model_card_parity_record() -> None:
    par = CARD["parity"]
    assert par["passed"] and par["max_abs_err"] <= 1e-3 and par["tol"] == 1e-3
    assert par["frames"] >= 64 and par["same_detections_frames"] == par["frames"]
    assert par["reference"]["val_frames"] == CARD["val_frames"]


def test_std_step_wall_record_and_sdk_constants() -> None:
    sw = CARD["std_step_wall"]
    assert R.STEP_BUDGET_S == 0.5 == sw["budget_s"]                     # M26-FR-126：L2 分轨 max(0.5 s, τ × wall_ratio)，τ = 0
    p = sw["pooled"]
    card_const = {"p50": p["p50_s"], "p95": p["p95_s"], "p99": p["p99_s"], "max": sw["worst_step_s"]}
    assert card_const == R.STD_STEP_WALL_S
    assert sw["passed"] and sw["worst_step_s"] <= R.STEP_BUDGET_S and p["over_budget_max"] == 0 and sw["act_mismatches"] == 0
    assert sw["repeats"] >= 3 and len(sw["runs"]) == sw["repeats"]
    cap = sw["capture"]
    assert cap["obs_level"] == "L2" and sw["steps_per_uav"] == round(cap["duration_s"] * 10) == 3600
    c = sw["container"]["agent_container"]                              # M26 §8.1 std：2 核、8 GB、无 GPU
    assert c["NanoCpus"] == 2_000_000_000 and c["Memory"] == c["MemorySwap"] == 8 << 30 and c["PidsLimit"] == 256
    assert c["NetworkMode"] == "none" and c["ReadonlyRootfs"] and not c["DeviceRequests"]
    assert sw["infer"]["frame_total"]["p95_s"] <= 0.25                  # M26 §14.3：std 档单帧推理 p95 ≤ 250 ms


# ---------------------------------------------------------------------------------------------- 录放工具
class _Echo:
    """确定性替身 Agent：动作只取决于观测（k 与相机帧字节摘要）。"""

    def reset(self, config: dict) -> None:
        self.uav = config["uav_id"]

    def step(self, obs: dict, inbox: list[dict]) -> dict:
        from drone4d.agent import act, nav_goto

        h = hashlib.sha256(bytes(obs["sensors"]["eo"][0]["rgb"]["data"])).digest()
        time.sleep(0.002)
        return act(obs["k"], nav=nav_goto((h[0], h[1], 30.0), 5.0))


def _trace(root: Path, uavs: list[str], steps: int) -> Path:
    from drone4d.wire import T, encode_frame

    for n, u in enumerate(uavs):
        d = root / u
        d.mkdir(parents=True)
        (d / "config.bin").write_bytes(SW._encode(SW.T_CONFIG, {"uav_id": u}))
        ag = _Echo()
        ag.reset({"uav_id": u})
        with open(d / "obs.bin", "wb") as fo, open(d / "act.bin", "wb") as fa:
            for k in range(steps):
                obs = {"k": k, "t_ns": k * 100_000_000, "sensors": {"eo": [{"seq": k, "rgb": {"codec": "jpeg", "data": bytes([n, k]) * 4000}}]}}
                fo.write(SW._encode(SW.T_OBS, {"obs": obs, "inbox": []}))
                fa.write(encode_frame(T.ACT, ag.step(obs, [])))
    (root / "capture.json").write_text(json.dumps({"run_id": "t", "policy": "echo", "seed": 1, "physics": "kinematic", "obs_level": "L2",
                                                   "uavs": {u: {} for u in uavs}}), encoding="utf-8")
    return root


def _replay(trace: Path, sock: Path, out: Path, uavs: list[str]) -> dict:
    from drone4d.wire import serve

    rc: list[int] = []
    th = threading.Thread(target=lambda: rc.append(SW.main(["replay", "--trace", str(trace), "--sock-root", str(sock), "--out", str(out),
                                                            "--label", "t", "--hello-timeout-s", "20"])))
    th.start()
    for u in uavs:
        for _ in range(200):
            if (sock / u / "agent.sock").exists():
                break
            time.sleep(0.01)
    agents = [threading.Thread(target=serve, args=(_Echo, str(sock / u / "agent.sock"))) for u in uavs]
    for a in agents:
        a.start()
    for a in [*agents, th]:
        a.join(30)
    assert rc == [0]
    return json.loads(out.read_text(encoding="utf-8"))


def test_replay_lockstep_matches_capture(tmp_path: Path) -> None:
    uavs = ["uav-01", "uav-02"]
    trace = _trace(tmp_path / "trace", uavs, 12)
    doc = _replay(trace, tmp_path / "s", tmp_path / "rep-1.json", uavs)
    assert doc["schema"] == SW.SCHEMA and doc["kind"] == "replay" and doc["budget_s"] == 0.5
    for u in uavs:
        r = doc["uavs"][u]
        assert r["steps"] == 12 and r["act_matches_capture"] and r["act_mismatches"] == 0 and r["sdk_version"]
        assert r["step_wall"]["n"] == 12 and 0.002 <= r["step_wall"]["p50_s"] < 0.5 and r["step_wall"]["over_budget"] == 0
    assert doc["pooled"]["n"] == 24

    fa = trace / "uav-02" / "act.bin"                                   # 篡改第 3 步期望 ACT：回放报告不一致
    frames = [fr for _t, fr in SW.iter_frames(fa)]
    frames[3] = SW._encode(SW.T_ACT, {"schema": "drone4d.bench.act.v1", "k": 3})
    fa.write_bytes(b"".join(frames))
    doc = _replay(trace, tmp_path / "s2", tmp_path / "rep-2.json", uavs)
    assert doc["uavs"]["uav-02"]["act_mismatch_steps"] == [3] and doc["uavs"]["uav-01"]["act_mismatches"] == 0


def test_quantile_and_stats() -> None:
    xs = list(np.random.default_rng(3).exponential(0.1, 997))
    for q in SW.QUANTILES:
        assert SW.quantile(xs, q) == pytest.approx(float(np.quantile(xs, q)), abs=1e-12)
    s = SW.stats([0.1, 0.2, 0.6], budget_s=0.5)
    assert s["n"] == 3 and s["max_s"] == 0.6 and s["over_budget"] == 1 and s["p50_s"] == 0.2


def _rep(p95: float, mx: float, mism: int = 0) -> dict:
    blk = {"n": 10, "mean_s": 0.1, "max_s": mx, "p50_s": 0.1, "p90_s": 0.12, "p95_s": p95, "p99_s": min(mx, p95 + 0.01),
           "over_budget": int(mx > 0.5)}
    return {"budget_s": 0.5, "trace": {"policy": "track_share"}, "utc": "2026-10-18T00:00:00Z", "pooled": blk,
            "uavs": {"uav-01": {"step_wall": blk, "act_mismatches": mism}}}


def test_summarize_takes_median_of_repeats_and_judges_worst_step() -> None:
    s = SW.summarize_docs([_rep(0.20, 0.30), _rep(0.25, 0.40), _rep(0.22, 0.35)])
    assert s["repeats"] == 3 and s["pooled"]["p95_s"] == 0.22 and s["pooled"]["max_s"] == 0.35 and s["worst_step_s"] == 0.40
    assert s["passed"] and s["pooled"]["over_budget_max"] == 0
    assert not SW.summarize_docs([_rep(0.20, 0.30), _rep(0.25, 0.55)])["passed"]          # 任一次重复的最坏单步超预算即不通过
    assert not SW.summarize_docs([_rep(0.20, 0.30, mism=1)])["passed"]                   # ACT 与录制不一致即不通过
    with pytest.raises(ValueError):
        SW.summarize_docs([])


# ---------------------------------------------------------------------------------------------- 比对与定稿工具
def test_parity_check_arrays(tmp_path: Path) -> None:
    rng = np.random.default_rng(4)
    np.savez(tmp_path / "w.npz", **{k: rng.normal(0, 0.2, v.shape).astype(np.float32) for k, v in np.load(R.WEIGHTS).items()})
    det = R.RefDet(tmp_path / "w.npz")
    x = np.random.default_rng(5).integers(0, 256, (2, R.H_IN, R.W_IN, 3), dtype=np.uint8)
    y = np.stack([det.forward(i) for i in x])
    ok = PA.check_arrays(det, x, y)
    assert ok["passed"] and ok["max_abs_err"] == 0.0 and ok["same_detections_frames"] == 2
    bad = PA.check_arrays(det, x, y + np.float32(2e-3))
    assert not bad["passed"] and bad["max_abs_err"] == pytest.approx(2e-3, rel=1e-3)
    rows = [{"seg": s} for s in range(100)]
    assert PA.val_frames(rows, 4) == [0, 20, 50, 70] and PA.val_frames(rows, 50) == list(range(0, 100, 10))


def test_freeze_writes_release_and_rejects_foreign_parity(tmp_path: Path) -> None:
    w = tmp_path / "ref_det_v1.npz"
    w.write_bytes(R.WEIGHTS.read_bytes())
    par = {k: CARD["parity"][k] for k in ("frames", "max_abs_err", "tol", "passed", "same_detections_frames", "numpy", "python")}
    par["weights_sha256"] = R.WEIGHTS_SHA256
    sw = SW.summarize_docs([_rep(0.20, 0.30)] * 3, env={"host": "dmax", "capture": {"run_id": "r", "obs_level": "L2", "duration_s": 1.0}})
    card = {"model": "ref_det_v1", "val_frames": 1}
    new = FZ.freeze(card, w, par, sw, version="1.0.0", utc="2026-10-18T00:00:00Z")
    assert new["model"] == "ref_det_v1" and new["release"]["weights"]["sha256"] == R.WEIGHTS_SHA256
    assert new["std_step_wall"]["passed"] and new["std_step_wall"]["worst_step_s"] == 0.30 and new["std_step_wall"]["container"]["host"] == "dmax"
    with pytest.raises(ValueError, match="parity was run"):
        FZ.freeze(card, w, {**par, "weights_sha256": "0" * 64}, sw, version="1.0.0")
    with pytest.raises(ValueError, match="did not pass"):
        FZ.freeze(card, w, {**par, "passed": False}, sw, version="1.0.0")
