#!/usr/bin/env python3
"""tools/bench/d3/refdet/freeze.py：把 `ref_det_v1` 的定稿记录写入模型卡（M26-FR-121；D3-AC-58）。

写入 `tools/sdk/anet4d/src/anet4d/perception/model_card.json` 的三个块（其余字段为 train.py 的训练记录，不改动）：
- `release`：模型版本、权重文件名、字节数与 sha256、定稿时间（UTC）与格式说明；
- `parity`：parity.py `check` 的结果（torch 参考与 numpy 推理逐元素最大绝对误差，阈值 1e-3；推理侧镜像的 numpy 版本）；
- `std_step_wall`：step_wall.py `summarize` 的结果（L2 基线在 `std` 档容器内的每步墙钟分位数、预算与判定、容器参数与单帧推理）。

SDK 常量 `anet4d.perception.ref_det.WEIGHTS_SHA256` 与 `STD_STEP_WALL_S` 须与模型卡一致（tests/d3/sdk/test_refdet_freeze.py 校验），
本工具打印两者的取值供核对。

用法：python freeze.py --parity out/a05/parity.json --step-wall out/a05/step_wall.json [--card PATH] [--version 1.0.0]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CARD = HERE.parents[2] / "sdk" / "anet4d" / "src" / "anet4d" / "perception" / "model_card.json"
STEP_KEYS = ("p50_s", "p90_s", "p95_s", "p99_s", "max_s", "mean_s")


def step_wall_block(sw: dict) -> dict:
    """step_wall.json（summary）→ 模型卡块：合并分位数（各次重复取中位）、最坏单步、预算与判定、逐机、单帧推理与环境。"""
    if sw.get("kind") != "summary":
        raise ValueError("step-wall input must be a step_wall.py summarize output")
    env = sw.get("env") or {}
    cap = env.get("capture") or {}
    blk = {"budget_s": sw["budget_s"], "repeats": sw["repeats"], "passed": sw["passed"], "steps_per_uav": sw["pooled"]["n"] // max(1, len(sw["uavs"])),
           "pooled": {k: sw["pooled"][k] for k in (*STEP_KEYS, "n", "over_budget_max")}, "worst_step_s": sw["worst_step_s"],
           "act_mismatches": sw["act_mismatches"], "uavs": {u: {k: v[k] for k in ("p50_s", "p95_s", "p99_s", "max_s")} for u, v in sw["uavs"].items()},
           "trace": sw["trace"], "runs": sw["runs"],
           "container": {k: env.get(k) for k in ("host", "cpu_model", "host_cpus", "image", "image_id", "gpu", "agent_container", "blas_threads")},
           "capture": {k: cap.get(k) for k in ("run_id", "policy", "seed", "physics", "obs_level", "duration_s", "pose_sha256")}}
    if "infer" in sw:
        inf = sw["infer"]
        blk["infer"] = {"uav": inf["uav"], "frames": inf["frames"], "detections": inf["detections"],
                        **{part: {k: inf[part][k] for k in ("p50_s", "p95_s", "p99_s", "max_s")} for part in
                           ("decode_small", "detect", "decode_full", "frame_total")}}
    return blk


def freeze(card: dict, weights: Path, parity: dict, step_wall: dict, *, version: str, utc: str | None = None) -> dict:
    """→ 新模型卡（不改动训练记录字段）。parity 未通过或权重 sha256 与比对所用不一致时拒绝。"""
    data = weights.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if parity.get("weights_sha256") != sha:
        raise ValueError(f"parity was run on weights {parity.get('weights_sha256')}, bundled weights are {sha}")
    if not parity.get("passed") or parity["max_abs_err"] > parity.get("tol", 1e-3):
        raise ValueError("torch/numpy parity did not pass")
    out = dict(card)
    out["release"] = {"model": card["model"], "version": version, "frozen_utc": utc or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "weights": {"file": weights.name, "bytes": len(data), "sha256": sha,
                                  "format": "numpy npz, float32, BatchNorm folded into convolutions"}}
    out["parity"] = {k: parity[k] for k in ("frames", "max_abs_err", "tol", "passed", "same_detections_frames", "numpy", "python")}
    if "reference" in parity:
        out["parity"]["reference"] = parity["reference"]
    out["std_step_wall"] = step_wall_block(step_wall)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="freeze.py")
    ap.add_argument("--parity", type=Path, required=True)
    ap.add_argument("--step-wall", type=Path, required=True)
    ap.add_argument("--card", type=Path, default=CARD)
    ap.add_argument("--version", default="1.0.0")
    a = ap.parse_args(argv)
    card = json.loads(a.card.read_text(encoding="utf-8"))
    new = freeze(card, a.card.with_name("ref_det_v1.npz"), json.loads(a.parity.read_text(encoding="utf-8")),
                 json.loads(a.step_wall.read_text(encoding="utf-8")), version=a.version)
    a.card.write_text(json.dumps(new, indent=1) + "\n", encoding="utf-8")
    p = new["std_step_wall"]["pooled"]
    print(f'WEIGHTS_SHA256 = "{new["release"]["weights"]["sha256"]}"')
    sw = new["std_step_wall"]
    print("STD_STEP_WALL_S = {" + ", ".join(f'"{k[:-2]}": {p[k]}' for k in ("p50_s", "p95_s", "p99_s")) + f', "max": {sw["worst_step_s"]}}}')
    print(f'budget_s = {sw["budget_s"]}  passed = {sw["passed"]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
