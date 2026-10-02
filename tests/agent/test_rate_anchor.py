"""多进程倍速一致（D1-AC-16、M14-FR-050、M14 §6.13 规则 ⑥；FX2-R3）：agent-runtime 的仿真时间锚点与 sim-core 往返合并。

多进程下 agent-runtime 收到 sim-core 事件有滞后（合批与事件泵），每次 sim-core 往返在 ×10 时折合 0.2–0.6 s【仿真】。
本文件用 FakeSim 的多进程时序模型（`rtt_ns`、`event_lag_ns`）验证：
- 检出触发的 find 自检出事件的仿真时刻起算；
- 观测点高度缓存、环境查询合并且与估价并行，报价在 t_send + L 之前完成；
- 委派在途预取执行前复核估价；
- 驻留自到站（goto 终态事件）起算，结果回传自 hover 终态事件起算，不含交还租约的往返；
- ×1（无滞后）与 ×10（往返 0.4 s、事件滞后 0.3 s、推进粒度 60 ms）的决策一致，accepted 时刻差 ≤ 1.0 s。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fakes.s3 import run_s3

from awr.agent.runtime.drone_agent import SharedReads

X10 = {"rtt_ns": 400_000_000, "event_lag_ns": 300_000_000}


def _coord(core: Any, typ: str) -> dict:
    return next(r for r in core.ledger(core.coord_aid).rows if r["type"] == typ)


def _first_suspect(fake: Any) -> int:
    return next(d["t_sim_ns"] for d in fake.detect_log if d["state"] == "suspect")


def _b1_calls(fake: Any) -> dict[str, tuple[int, int]]:
    """b1 的命令：op → (请求到达 sim-core 的时刻, 终态事件时刻)。"""
    out: dict[str, tuple[int, int]] = {}
    for ts, uav, op, cid in fake.cmd_log:
        if uav == "p600-b1" and op not in out:
            out[op] = (ts, int((fake.results.get(cid) or {}).get("t_sim_ns") or 0))
    return out


# ---------------------------------------------------------------- SharedReads
class _CountingBridge:
    def __init__(self) -> None:
        self.geo = 0
        self.env = 0
        self.fail_geo = False

    async def geo_height(self, op: str, xy: list) -> list[float | None]:
        self.geo += 1
        await asyncio.sleep(0)
        if self.fail_geo:
            raise TimeoutError("bus")
        return [12.5 if op == "ground_dtm" else 40.0 for _ in xy]

    async def env_at(self, pos: Any) -> Any:
        self.env += 1
        await asyncio.sleep(0)
        return ("env", tuple(pos))


def test_shared_reads_geo_cached_and_merged() -> None:
    br = _CountingBridge()
    reads = SharedReads(br)  # type: ignore[arg-type]

    async def main() -> None:
        a, b = await asyncio.gather(reads.geo("ground_dtm", 1.0, 2.0), reads.geo("ground_dtm", 1.0, 2.0))
        assert a == b == 12.5 and br.geo == 1  # 并发同键合并
        assert await reads.geo("ground_dtm", 1.0, 2.0) == 12.5 and br.geo == 1  # 静态数据缓存
        assert await reads.geo("height_dsm", 1.0, 2.0) == 40.0 and br.geo == 2
        br.fail_geo = True
        for _ in range(2):  # 失败不缓存：每次重新发起
            with contextlib.suppress(TimeoutError):
                await reads.geo("ground_dtm", 9.0, 9.0)
        assert br.geo == 4 and reads.stats["geo_hits"] == 1

    asyncio.run(main())


def test_shared_reads_env_merged_not_cached() -> None:
    br = _CountingBridge()
    reads = SharedReads(br)  # type: ignore[arg-type]

    async def main() -> None:
        r = await asyncio.gather(*(reads.env((5.0, 6.0, 70.0)) for _ in range(3)))
        assert len(set(r)) == 1 and br.env == 1 and reads.stats["env_shared"] == 2
        await reads.env((5.0, 6.0, 70.0))
        assert br.env == 2  # 环境随时间变化：只合并并发请求，不跨时间缓存

    asyncio.run(main())


# ---------------------------------------------------------------- 锚点
def test_find_anchored_to_detection_time() -> None:
    """事件滞后 0.1 s（小于 find 的 0.2 s）时，find 恰在检出时刻 + 0.2 s，与滞后无关。"""
    fake, core = run_s3(sim_kw={"event_lag_ns": 100_000_000})
    t_det = _first_suspect(fake)
    find = _coord(core, "agent.task.find")["t_sim_ns"]
    assert find == t_det + 200_000_000
    task = core.tm.tasks["T-0001"]
    assert task.t_anchor_ns == t_det


def test_quotes_on_time_under_latency() -> None:
    """往返 0.4 s：每个候选的报价工作为观测点高度（缓存，任务提交时预热）与"估价 ∥ 环境"一次往返，在 t_send + L 之前完成，
    报价收齐时刻与无滞后时相对 find 的偏移相同（报价回复投递 max(t_done + d_resp, t_send + L)）。"""
    _f1, c1 = run_s3()
    f2, c2 = run_s3(chunk_ns=60_000_000, sim_kw={"rtt_ns": 400_000_000})
    d1 = _coord(c1, "agent.task.quote")["t_sim_ns"] - _coord(c1, "agent.task.find")["t_sim_ns"]
    d2 = _coord(c2, "agent.task.quote")["t_sim_ns"] - _coord(c2, "agent.task.find")["t_sim_ns"]
    assert d1 == d2
    assert f2.counts["geo"] == 2  # 目标处 ground_dtm、height_dsm 各一次：3 个报价与执行前复核共用（合并或缓存命中）


def test_prefetch_used_and_estimate_count_unchanged() -> None:
    fake, core = run_s3(chunk_ns=60_000_000, sim_kw=X10)
    b1 = core.agents[core.by_vehicle["p600-b1"]]
    assert b1.stats["prepared"] == 1 and b1.stats["prepared_used"] == 1
    quotes = sum(r["payload"]["estimate_calls"] for r in core.ledger(core.coord_aid).rows if r["type"] == "agent.task.quote")
    assert fake.counts["estimate"] == quotes + 1  # 预取的就是执行前复核估价，不额外增加估价


def test_dwell_from_arrival_and_result_from_hover_event() -> None:
    """驻留自 goto 终态事件的仿真时刻起算（事件滞后 0.3 s 不推迟 hover）；结果回传自 hover 终态时刻起算，
    不含 0.4 s 往返的租约交还。"""
    fake, core = run_s3(chunk_ns=20_000_000, sim_kw=X10)
    calls = _b1_calls(fake)
    _goto_req, goto_done = calls["goto"]
    hover_req, hover_done = calls["hover"]
    # hover 定时器在到站 + 10 s 触发，请求半个往返（0.2 s）后到达 sim-core；推进粒度 20 ms
    assert abs(hover_req - (goto_done + 10_000_000_000 + 200_000_000)) <= 20_000_000, (goto_done, hover_req)
    task = core.tm.tasks["T-0001"]
    relay = core.net.relay
    acc = _coord(core, "agent.task.accepted")["t_sim_ns"]
    worst = max(relay.L_ns(f"{task.ix}/res"), max(relay.L_ns(f"{task.ix}/upd/{n}") for n in range(1, 8))) - relay.d_resp_ns
    assert hover_done < acc <= hover_done + worst + 20_000_000, (hover_done, acc)


# ---------------------------------------------------------------- 倍速一致（多进程时序模型）
def _decisions(core: Any) -> dict:
    t = core.tm.tasks["T-0001"]
    return {"winner": t.provider_aid, "cands": sorted(q.aid for q in t.quotes),
            "feasible": {q.aid: q.feasible for q in t.quotes},
            "types": [r["type"] for r in core.ledger(core.coord_aid).rows],
            "pred": (t.predicate_ok, t.scope_ok), "conf": core.tm.target_confidence("t1"), "state": t.state}


def test_rate_equivalence_with_multiprocess_latency() -> None:
    """×1（推进粒度 20 ms、无滞后）对 ×10（60 ms、往返 0.4 s、事件滞后 0.3 s，取多进程实测量级）：决策一致，
    accepted 时刻差 ≤ 1.0 s（M14-FR-050）。锚点之前同一模型下的差约为 2 s 量级（报价、执行前复核与驻留各吃一次往返或滞后）。"""
    _f1, c1 = run_s3(chunk_ns=20_000_000)
    _f10, c10 = run_s3(chunk_ns=60_000_000, sim_kw=X10)
    assert _decisions(c1) == _decisions(c10)
    a1 = _coord(c1, "agent.task.accepted")["t_sim_ns"]
    a10 = _coord(c10, "agent.task.accepted")["t_sim_ns"]
    assert abs(a1 - a10) <= 1_000_000_000, (a1, a10)
