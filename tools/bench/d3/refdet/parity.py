#!/usr/bin/env python3
"""tools/bench/d3/refdet/parity.py：`ref_det_v1` 定稿权重的 torch 与 numpy 推理逐元素比对（M26-FR-121；D3-AC-58）。

训练结束时 train.py 只在验证集前 8 帧上比对；定稿时在更多验证帧上复核，并把 numpy 侧放在 Agent 基础镜像（d3-agent-py，
推理实际运行的 numpy 与 BLAS 版本）中执行：

- `ref`（pytorch 镜像，CPU）：载入 `ref_det_v1.pt` 到 train.py 的网络（eval，BN 不折叠），对验证帧（段号 % 10 == 0，等间隔取
  `--frames` 帧）计算输出，写 `<out>/parity-ref.npz`（帧号、输入 uint8、torch 输出 float32）与 torch、numpy 版本；
- `check`（d3-agent-py 镜像）：以 `drone4d.perception.ref_det.RefDet`（BN 已折叠的 npz）重算并比对，写 `<out>/parity.json`
  （最大绝对误差、逐帧误差、阈值 1e-3 判定、热图解码的检测是否一致）。

用法：
    python parity.py ref --data out/refdet/data --pt out/refdet/model/ref_det_v1.pt --out out/a05/parity [--frames 64]
    python parity.py check --ref out/a05/parity/parity-ref.npz --out out/a05/parity/parity.json [--weights PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SDK = HERE.parents[2] / "sdk" / "drone4d" / "src"
TOL = 1e-3


def val_frames(rows: list[dict], n: int) -> list[int]:
    """验证帧（段号 % 10 == 0，同 train.py）中等间隔取 n 帧的帧号（升序、确定）。"""
    val = [i for i, r in enumerate(rows) if r["seg"] % 10 == 0]
    if n >= len(val):
        return val
    return [val[(j * len(val)) // n] for j in range(n)]


def _rows(data: Path) -> list[dict]:
    rows = []
    for idx in sorted(data.glob("shard-*.jsonl")):
        rows += [json.loads(ln) for ln in idx.read_text(encoding="utf-8").splitlines()]
    return rows


def ref(a: argparse.Namespace) -> int:
    import torch

    sys.path.insert(0, str(HERE))
    import train as TR

    rows = _rows(Path(a.data))
    mm = np.memmap(Path(a.data) / f"cache-{TR.W_IN}x{TR.H_IN}.u8", dtype=np.uint8, mode="r", shape=(len(rows), TR.H_IN, TR.W_IN, 3))
    idx = val_frames(rows, a.frames)
    net = TR.make_model()
    net.load_state_dict(torch.load(a.pt, map_location="cpu"))
    net.eval()
    outs = []
    with torch.no_grad():
        for i in range(0, len(idx), 8):
            ii = idx[i:i + 8]
            outs.append(net(TR._batch_x(mm, ii, [False] * len(ii))).numpy())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / "parity-ref.npz", frames=np.asarray(idx, np.int64), x=np.stack([np.asarray(mm[i]) for i in idx]),
                        y=np.concatenate(outs).astype(np.float32))
    meta = {"frames": len(idx), "val_frames": sum(1 for r in rows if r["seg"] % 10 == 0), "torch": torch.__version__, "numpy": np.__version__,
            "python": platform.python_version(), "pt_sha256": hashlib.sha256(Path(a.pt).read_bytes()).hexdigest()}
    (out / "parity-ref.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(meta), flush=True)
    return 0


def check_arrays(det, x: np.ndarray, y: np.ndarray) -> dict:
    """det：RefDet；x (N, 540, 960, 3) uint8；y (N, 6, 136, 240) torch 输出 → 比对摘要。"""
    from drone4d.perception import ref_det as R

    errs, same_det = [], 0
    for j in range(len(x)):
        got = det.forward(x[j])
        errs.append(float(np.abs(got - y[j]).max()))
        a = [(c, round(s, 3), round(bx), round(by)) for c, s, bx, by, _w, _h in det.detect(x[j])]
        b = [(c, round(s, 3), round(bx), round(by)) for c, s, bx, by, _w, _h in _decode(R, y[j])]
        same_det += a == b
    mx = max(errs) if errs else float("nan")
    return {"frames": len(errs), "max_abs_err": mx, "per_frame_max_abs_err": [round(e, 8) for e in errs], "tol": TOL,
            "passed": bool(errs) and mx <= TOL, "same_detections_frames": same_det}


def _decode(R, out: np.ndarray):
    """torch 输出走与 RefDet.detect 相同的热图解码（替身 forward）。"""

    class _Fixed(R.RefDet):
        def __init__(self, y):                                # 不载入权重
            self.y = y

        def forward(self, img):
            return self.y

    return _Fixed(out).detect(None)


def check(a: argparse.Namespace) -> int:
    sys.path.insert(0, str(SDK))
    from drone4d.perception import ref_det as R

    z = np.load(a.ref)
    det = R.RefDet(a.weights) if a.weights else R.RefDet()
    w = Path(a.weights) if a.weights else R.WEIGHTS
    doc = {"schema": "drone4d.refdet.parity.v1", "weights_sha256": hashlib.sha256(w.read_bytes()).hexdigest(), "numpy": np.__version__,
           "python": platform.python_version(), **check_arrays(det, z["x"], z["y"])}
    meta = Path(a.ref).with_suffix(".json")
    if meta.exists():
        doc["reference"] = json.loads(meta.read_text(encoding="utf-8"))
    Path(a.out).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: doc[k] for k in ("frames", "max_abs_err", "passed", "same_detections_frames")}), flush=True)
    return 0 if doc["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parity.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ref")
    r.add_argument("--data", required=True)
    r.add_argument("--pt", required=True)
    r.add_argument("--frames", type=int, default=64)
    r.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--ref", required=True)
    c.add_argument("--weights")
    c.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    return {"ref": ref, "check": check}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
