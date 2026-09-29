"""流式回放 CPU（M12 §5.3 `m12_replay_bench.py` 的迁移；M12-AC-043；NFR-010）。

McapSource 在 LocalRing 上以回放倍率 rate 播放整段（假墙钟：每步推进 4 ms 墙钟，与宿主 ≤ 250 Hz 循环一致；预读线程
真实运行），测每仿真秒 CPU（ms）与由此推出的该倍率下的核数、BUFFERING 占比。门禁：≤ 20 ms CPU/仿真秒，20× 下
≤ 1 核（目标 0.3 核），BUFFERING < 1 %。

用法：`python tools/bench/rec/bench_replay.py --n 1000 --sim-s 600 --rate 20`（经 `make bench-replay`）
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

RUN = "r20260101-000000-0000"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mcap", type=Path, default=None)
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--sim-s", type=float, default=600.0)
    ap.add_argument("--rate", type=float, default=20.0)
    common_args(ap)
    a = ap.parse_args()
    from awr.contracts import LAYOUT_ID
    from awr.recorder.config import ReplayCfg
    from awr.recorder.mcap_source import McapSource
    from awr.runtime.source import SourceSpec
    from awr.runtime.statering import LocalRing

    tmp = Path(tempfile.mkdtemp(prefix="awr-bench-replay-"))
    if a.mcap is None:
        from awr.recorder.synth import synthesize

        synthesize(tmp / RUN, n=a.n, sim_s=a.sim_s, marked=[f"sim-{i:04d}" for i in range(min(16, a.n))])
        mcap = tmp / RUN / "rec-000.mcap"
    else:
        mcap = a.mcap

    def one() -> dict:
        ring_path = tmp / f"state.replay-{time.monotonic_ns()}"
        ring = LocalRing.create(ring_path, layout_id=LAYOUT_ID, flags=1)
        src = McapSource(ReplayCfg(), runs_dir=mcap.parent.parent)
        info = src.open(SourceSpec(run=mcap.parent.name, segment=int(mcap.stem.split("-")[-1]), world_id="shenzhen",
                                   layout_id=LAYOUT_ID), ring, None)
        rate, _ = src.set_speed(min(a.rate, info.speed_max))
        src.play()
        now = time.monotonic_ns()
        src.last_ns = now
        c0 = time.process_time()
        steps = 0
        while src.state in ("playing", "buffering") and steps < 10_000_000:
            now += 4_000_000
            src.step(now)
            steps += 1
            if src.state == "buffering":
                time.sleep(0.001)
        cpu = time.process_time() - c0
        sim_s = (info.data_end_ns - info.data_start_ns) / 1e9
        perf = src.perf()
        src.close()
        ring.close()
        LocalRing.remove(ring_path)
        ms_per_sim_s = cpu * 1000 / max(sim_s, 1e-9)
        return {"rate": rate, "cpu_ms_per_sim_s": round(ms_per_sim_s, 3), "cpu_core_at_rate": round(ms_per_sim_s * rate / 1000, 4),
                "buffering_ratio": perf["buffering_ratio"], "steps": steps}

    try:
        return run_protocol("bench_replay", a, one, lambda m: [("cpu_ms_per_sim_s", m["cpu_ms_per_sim_s"], "<=", 20.0),
                                                               ("cpu_replay_core", m["cpu_core_at_rate"], "<=", 1.0),
                                                               ("buffering_ratio", m["buffering_ratio"], "<=", 0.01)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
