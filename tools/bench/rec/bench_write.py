"""recorder 写入开销与体积（M12 §5.3 `m12_rec_bench.py` 的迁移；M12-AC-034；D1-AC-18 录制部分；NFR-007）。

以 `awr.recorder.synth` 用 recorder 相同的 RecorderCore（桶规则、标记机、KeyDelta、writer 线程 + zstd、`.ovw`/`.evx`）写出
N 架、sim_s 秒的录制，测进程 CPU；再以"丢弃型 writer"的空跑测合成本身的开销，两者之差 ÷ 仿真秒即写入开销（核，×1）。
门禁：写入开销 ≤ 0.1 核、写入量 ≤ 60 MB/min、Full64 与块无丢弃。

用法：`python tools/bench/rec/bench_write.py --n 1000 --sim-s 60 [--runs 3]`（经 `make bench-rec`，持性能锁执行）
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _rec import common_args, run_protocol


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
        return {"cpu_s": cpu, "wall_s": wall, "bytes": size, "frames": out["frames"]}
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
        return {"recorder_cpu_core": round(write_core, 4), "rec_write_mb_per_min": round(mb_min, 3),
                "mb_per_sim_s": round(full["bytes"] / 1e6 / a.sim_s, 4), "synth_cpu_core": round(dry["cpu_s"] / a.sim_s, 4),
                "frames": full["frames"]}

    return run_protocol("bench_write", a, one, lambda m: [("cpu_recorder_core", m["recorder_cpu_core"], "<=", 0.1),
                                                           ("rec_write_mb_per_min", m["rec_write_mb_per_min"], "<=", 60.0)])


if __name__ == "__main__":
    sys.exit(main())
