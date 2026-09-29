"""SegmentIndex 构建与 worker 侧 seek（M12 §5.3、§6.7.2、§6.7.6；M12-AC-042、AC-045；NFR-011）。

对一段录制（`--mcap` 给出，或以 synth 生成 N 架、sim_s 秒）：打开建索引（时间与常驻内存）、100 次随机 seek（McapSource.seek：
索引定位、关键块 + 增量合并、复合帧写 LocalRing、backfill 包），统计 p50、p95、max。门禁：索引 ≤ 1.0 s、≤ 40 MB（10 min、
N = 1000），seek p95 ≤ 50 ms。

用法：`python tools/bench/rec/bench_seek.py --n 1000 --sim-s 600`（经 `make bench-seek`）
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _rec import common_args, run_protocol

RUN = "r20260101-000000-0000"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mcap", type=Path, default=None, help="已有录制段（runs/<run>/rec-000.mcap）")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--sim-s", type=float, default=600.0)
    ap.add_argument("--seeks", type=int, default=100)
    common_args(ap)
    a = ap.parse_args()
    from awr.contracts import LAYOUT_ID
    from awr.recorder.config import ReplayCfg
    from awr.recorder.mcap_index import SegmentIndex
    from awr.recorder.mcap_source import McapSource
    from awr.runtime.source import SourceSpec
    from awr.runtime.statering import LocalRing

    tmp = Path(tempfile.mkdtemp(prefix="awr-bench-seek-"))
    if a.mcap is None:
        from awr.recorder.synth import synthesize

        synthesize(tmp / RUN, n=a.n, sim_s=a.sim_s, marked=[f"sim-{i:04d}" for i in range(min(16, a.n))])
        mcap = tmp / RUN / "rec-000.mcap"
    else:
        mcap = a.mcap

    def one() -> dict:
        t0 = time.perf_counter()
        ix = SegmentIndex.build(mcap)
        build_s = time.perf_counter() - t0
        mem_mb = ix.memory_bytes() / 2 ** 20
        entries = ix.stats()["entries"]
        start, end = ix.data_start_ns, ix.data_end_ns
        ix.close()
        ring_path = tmp / f"state.replay-{time.monotonic_ns()}"
        ring = LocalRing.create(ring_path, layout_id=LAYOUT_ID, flags=1)
        src = McapSource(ReplayCfg(), runs_dir=mcap.parent.parent)
        src.open(SourceSpec(run=mcap.parent.name, segment=int(mcap.stem.split("-")[-1]), world_id="shenzhen", layout_id=LAYOUT_ID),
                 ring, None)
        rng = random.Random(7)
        ms = []
        for _ in range(a.seeks):
            t = rng.randrange(start, end)
            s0 = time.perf_counter()
            src.seek(t)
            ms.append((time.perf_counter() - s0) * 1000)
        src.close()
        ring.close()
        LocalRing.remove(ring_path)
        return {"index_build_s": round(build_s, 4), "index_mb": round(mem_mb, 2), "entries": entries,
                "seek_ms_p50": round(float(np.percentile(ms, 50)), 3), "seek_ms_p95": round(float(np.percentile(ms, 95)), 3),
                "seek_ms_max": round(max(ms), 3)}

    try:
        return run_protocol("bench_seek", a, one, lambda m: [("index_build_s", m["index_build_s"], "<=", 1.0),
                                                             ("index_mb", m["index_mb"], "<=", 40.0),
                                                             ("seek_ms_p95", m["seek_ms_p95"], "<=", 50.0)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
