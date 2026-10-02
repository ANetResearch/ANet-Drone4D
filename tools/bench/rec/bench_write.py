"""recorder 写入开销与体积（M12 §5.3 `m12_rec_bench.py` 的迁移；M12-AC-034；D1-AC-18 录制部分；NFR-007）。

以 `awr.recorder.synth` 用 recorder 相同的 RecorderCore（桶规则、标记机、KeyDelta、writer 线程 + zstd、`.ovw`/`.evx`）写出
N 架、sim_s 秒的录制，测进程 CPU；再以"丢弃型 writer"的空跑测合成本身的开销，两者之差 ÷ 仿真秒即写入开销（核，×1）。
门禁：写入开销 ≤ 0.1 核、写入量 ≤ 60 MB/min、Full64 与块无丢弃（`meta.json` 的 `gaps` 为空，且块消息数 = 仿真秒 × 25 + 1，
FX2-R2-gateway 补齐此前缺失的"缺口为 0"判定）。D1-AC-18 的口径为 N = 1000、×1 连续 10 min（`--sim-s 600`）。

用法：`python tools/bench/rec/bench_write.py --n 1000 --sim-s 600 [--runs 3]`（经 `make bench-rec`，持性能锁执行）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _rec import common_args, run_protocol


def _block_stats(mcap: Path) -> tuple[int, float]:
    """块消息条数与相邻块的最大仿真间隔（ms），经逐 channel 索引（不解压数据）。"""
    import numpy as np

    from awr.recorder.formats import T_BLOCK
    from awr.recorder.mcap_index import SegmentIndex

    ix = SegmentIndex.build(mcap)
    try:
        ch = ix.ch(T_BLOCK)
        if ch is None or ch not in ix.idx:
            return 0, float("nan")
        t = np.asarray(ix.idx[ch][0], np.int64)
        return int(t.size), float(np.diff(t).max() / 1e6) if t.size > 1 else 0.0
    finally:
        ix.close()


def _one(n: int, sim_s: float, marked: int, dry: bool) -> dict:
    from awr.recorder import synth as S
    from awr.recorder.writer import McapSegmentWriter

    tmp = Path(tempfile.mkdtemp(prefix="awr-bench-rec-"))
    orig_put = McapSegmentWriter.put
    try:
        if dry:
            McapSegmentWriter.put = lambda self, kind, topic, t_ns, data, seq=0: True  # type: ignore[method-assign]
        c0, w0 = time.process_time(), time.perf_counter()
        out = S.synthesize(tmp / "r20260101-000000-0000", n=n, sim_s=sim_s, marked=[f"sim-{i:04d}" for i in range(min(marked, n))])
        cpu, wall = time.process_time() - c0, time.perf_counter() - w0
        size = sum(p.stat().st_size for p in (tmp / "r20260101-000000-0000").glob("rec-*.mcap"))
        gaps = blocks = max_dt_ms = -1
        if not dry:
            try:
                gaps = len(json.loads((tmp / "r20260101-000000-0000" / "meta.json").read_text(encoding="utf-8")).get("gaps") or [])
            except (OSError, ValueError):
                gaps = -1
            blocks, max_dt_ms = _block_stats(tmp / "r20260101-000000-0000" / "rec-000.mcap")
        return {"cpu_s": cpu, "wall_s": wall, "bytes": size, "frames": out["frames"], "gaps": gaps, "blocks": blocks,
                "block_max_dt_ms": max_dt_ms}
    finally:
        McapSegmentWriter.put = orig_put  # type: ignore[method-assign]
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--sim-s", type=float, default=60.0)
    ap.add_argument("--marked", type=int, default=16)
    common_args(ap)
    a = ap.parse_args()

    def one() -> dict:
        full = _one(a.n, a.sim_s, a.marked, dry=False)
        dry = _one(a.n, a.sim_s, a.marked, dry=True)
        write_core = max(0.0, (full["cpu_s"] - dry["cpu_s"]) / a.sim_s)
        mb_min = full["bytes"] / 1e6 / a.sim_s * 60
        # 缺口：meta.json 记录的缺口（writer 队列丢弃等）+ 块消息缺帧（×1 时 25 Hz 仿真等间隔，含 t = 0 的首块；
        # 相邻块间隔 > 1.5 个桶宽即缺帧）
        missing = max(0, round(a.sim_s * 25) + 1 - full["blocks"]) + (1 if not full["block_max_dt_ms"] <= 60.0 else 0)
        return {"recorder_cpu_core": round(write_core, 4), "rec_write_mb_per_min": round(mb_min, 3),
                "mb_per_sim_s": round(full["bytes"] / 1e6 / a.sim_s, 4), "synth_cpu_core": round(dry["cpu_s"] / a.sim_s, 4),
                "frames": full["frames"], "blocks": full["blocks"], "block_max_dt_ms": full["block_max_dt_ms"],
                "rec_gaps": (full["gaps"] if full["gaps"] >= 0 else 1) + missing}

    return run_protocol("bench_write", a, one, lambda m: [("cpu_recorder_core", m["recorder_cpu_core"], "<=", 0.1),
                                                           ("rec_write_mb_per_min", m["rec_write_mb_per_min"], "<=", 60.0),
                                                           ("rec_gaps", m["rec_gaps"], "==", 0)])


if __name__ == "__main__":
    sys.exit(main())
