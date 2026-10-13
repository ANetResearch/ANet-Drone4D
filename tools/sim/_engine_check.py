"""子进程：把一份候选剧本真实装配进单进程栈（`awr.api.inproc`），在一段墙钟时间内持续观察任务状态（`gen_city_watch.py`
`build_city_validated` 的最后一道闸：几何复核之外，任务分解阶段的禁入圆核验含运动识别物的预测包络外扩
（`TaskingRuntime.motion_hull`），手工复刻该外扩量级成本较高，不如直接跑一次真实引擎判定准确；放在子进程里跑，
避免模块级单例（`awr.sim.tasking.plugin.runtime()`）跨城市、跨次重试互相污染）。

D2.2-FX-world 修复：原实现一旦全部任务离开 `PLANNING`（通常几十到几百毫秒）即退出观察循环，把那一刻的状态当作最终
判定。`relay_watch` 等任务会先进入 `RUNNING`/`READY`，随后才异步提交“选站”作业（`RelayBook.request_station`），作业
结果在之后的 tick 才回调（`_on_station`），找不到站位时此时才转入 `INFEASIBLE`（终态，`book.py` `_set_state`）——这个
转态发生在“离开 PLANNING”之后，原逻辑在它发生前就已经退出并把任务误判为可行（见 docs/impl/D2.2-R-world-报告.md 第 3
节：shenzhen、newyork 的 relay_watch 因此从未被挡住，固化进了对外展示的录制内容）。现在的 `observe()` 真正观察满
`--seconds` 这段墙钟时间，并记录窗口内任意一次采样出现过的终态（即使之后被覆盖也保留，终态本身也不会再恢复），
不再因为“看起来已经离开 PLANNING”就提前退出。

用法：`python -m tools.sim._engine_check <world_id> <config_path> [--seconds 3]`；stdout 输出一行 JSON
`{"states": {tid: state}, "infeasible": [tid, ...]}`。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

SIM_PLUGINS = ("awr.environment.stage", "awr.sim.safety", "awr.sim.mission", "awr.sim.sensors.plugin",
               "awr.sim.catalog.plugin", "awr.sim.fixedwing.plugin", "awr.sim.targets.plugin",
               "awr.sim.perception.plugin", "awr.sim.tasking.plugin")

# 任务状态契约枚举中的终态（`python/awr/sim/tasking/book.py` 第 3 行），一旦进入不会自行恢复——所以窗口内任意一次
# 采样见到即可认定，不需要等到窗口结束仍是该状态。
TERMINAL_BAD = ("INFEASIBLE", "FAILED", "CANCELED")
POLL_INTERVAL_S = 0.2


def observe(get_states: Callable[[], dict[str, str]], seconds: float, *,
            sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic) -> dict[str, str]:
    """在 `seconds` 整段墙钟时间内持续采样 `get_states()`，不因任务提前离开 `PLANNING` 就提前退出。

    返回“本窗口内见过的最差状态”：某任务一旦在任意一次采样中处于 `TERMINAL_BAD`，之后即使因重新采样覆盖也保留该
    终态（终态本身不会再恢复，这里只是防止末次采样恰好落在两次 tick 回调之间而漏判）；其余任务取窗口结束时的最后
    一次采样。`get_states` 可能在引擎尚未就绪时返回 `{}`，此时不计入 `worst`。
    """
    end = now() + seconds
    last: dict[str, str] = {}
    worst: dict[str, str] = {}
    while True:
        last = get_states()
        for tid, st in last.items():
            if st in TERMINAL_BAD and worst.get(tid) not in TERMINAL_BAD:
                worst[tid] = st
        remaining = end - now()
        if remaining <= 0:
            break
        sleep(min(POLL_INTERVAL_S, remaining))
    return {**last, **worst}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("world")
    ap.add_argument("config")
    ap.add_argument("--seconds", type=float, default=3.0)
    a = ap.parse_args(argv)

    os.environ["AWR_SESSION_CONFIG"] = a.config
    from awr.api.inproc import InprocSim, inproc_settings

    s = inproc_settings(a.world, profile="dev")
    sim = InprocSim(s, n=0, autoplay=True, load_world=True, plugins=SIM_PLUGINS).start()
    try:
        from awr.sim.tasking.plugin import runtime as tasking_runtime

        def get_states() -> dict[str, str]:
            rt = tasking_runtime()
            return {tid: t.state for tid, t in rt.tasks.items()} if rt is not None else {}

        states = observe(get_states, a.seconds)
        infeasible = sorted(tid for tid, st in states.items() if st in TERMINAL_BAD)
        print(json.dumps({"states": states, "infeasible": infeasible}))
    finally:
        sim.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
