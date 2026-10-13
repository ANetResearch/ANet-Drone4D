"""D2.2-W2：对 `scenarios/d22-<city>-watch.json` 跑一段真实的单进程栈（`awr.api.inproc`：真实 sim-core + 真实 uvicorn
网关，世界为已构建的城市世界包，不是合成小世界），以一条“观看者”WS 连接订阅共享展示会用到的全部主题，用
`AWR_RT_CAPTURE` 采集成 `.awrrt`，落到 `worlds/<city>/show/show.awrrt`（D2.2-W1 报告 §7.2 的建议路径；`worlds/` 已在根
`.gitignore` 排除，不进 git）。

这是”任务完成指标”的证据来源（而不是另写一套进程内 mock）：真实装配快照后推进真实墙钟 `--seconds`，采集到的事件流里
`task.*` 状态与 `target.discovered` 计数即可核对任务是否真的运行、识别物是否真的被”发现”（§D2.2-W2 报告第 5 节）。

D2.2-FX-world 补充：生成期的 `tools/sim/_engine_check.py` 已改为真正观察满一段墙钟时间（见该文件修复说明），但录制
本身仍是独立的一次真实 120 秒运行，不是生成期那几秒的简单延续——共享开发机上的瞬时负载仍可能让某次录制里的任务作业
短暂失败（`python/awr/sim/tasking/relay.py` 的 `JOB_FAIL_MAX` 注释：负载高时作业可能超时）。录制结束后现在会扫一遍
采集到的事件流，若出现 `task.state` 终态为 `INFEASIBLE`/`FAILED`（`_bad_task_states`），判为本次录制失败：复用
`gen_city_watch.build_city_validated` 换一批不重叠的候选点位重新生成剧本（`--retries`，默认 2 次，即最多 3 次尝试）
后重新录制，而不是把这次失败原样写进对外展示的 `.awrrt`（见 `docs/impl/D2.2-R-world-报告.md` 第 3 节的教训：
一次性的失败被循环回放后，访客打开城市的任何一次都会重演该失败）。

用法：`python -m tools.sim.capture_city_watch --city shenzhen --seconds 120 [--port 18700] [--retries 2]`
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

PROTO = "awr.rt.v1"

# 共享展示会话配置剧本（S7）用的插件组合（`configs/runtime.yaml` procs.sim-core.plugins + supervisor.py
# SHOW_D2_PLUGINS，装配 awr.sandbox.snapshot.v1 的 nests/targets/tasks 段缺这些插件就是 NO_LOADER）
SIM_PLUGINS = ("awr.environment.stage", "awr.sim.safety", "awr.sim.mission", "awr.sim.sensors.plugin",
               "awr.sim.catalog.plugin", "awr.sim.fixedwing.plugin", "awr.sim.targets.plugin",
               "awr.sim.perception.plugin", "awr.sim.tasking.plugin")

# 共享展示观看者会用到的主题（D2.2-W1 报告 §7.2）
BROAD_TOPICS = ["fleet/roster", "swarm/state", "env/state", "event", "swarm/target/state", "target/*/detail",
                "nest/*/status", "task/*/status", "relay/*/stats", "field/*", "field/volumes"]

TASK_TERMINAL_BAD = ("INFEASIBLE", "FAILED")  # task.state 的 to：这两种不应出现在对外展示的录制里（CANCELED 不算：
                                              # 观看者连接本身不会取消任务，出现也不代表生成或录制有问题）


class CaptureInfeasible(RuntimeError):
    """录制期间事件流里出现了 `task.state → INFEASIBLE`/`FAILED`：这次录制不能用（D2.2-FX-world）。"""


def _iter_events(msgs: list[dict]):
    """把 WS 消息流展平成一串事件对象：单条用 `{"op":"event", ...}`（事件字段在顶层），≥ 2 条合并为
    `{"op":"events","items":[...]}`（`python/awr/api/rt/session.py` `flush_events`）；两种都要看，只看 `op=="event"`
    会漏掉同一 tick 内与其他事件一起合并发送的 `task.state`（本文件原 `kinds` 统计就只看了前一种，这里一并修正）。"""
    for m in msgs:
        op = m.get("op")
        if op == "event":
            yield m
        elif op == "events":
            yield from (m.get("items") or [])


def _bad_task_states(msgs: list[dict]) -> list[dict]:
    """事件流里 `type == "task.state"` 且终态（`data.to`）落在 `TASK_TERMINAL_BAD` 的条目（tid、type、from、to、why）。
    注意 WS 事件对象的分类字段是 `type`，不是 `kind`（`python/awr/api/rt/events.py` `EventRing.append`：
    `encode_event({..., "type": kind, ...})`，这里的局部变量名 `kind`只是发布端的叫法，编码进 JSON 的键是 `type`；
    `data` 里嵌的 `type` 是任务自己的类型如 `relay_watch`，两层同名但含义不同，不要弄混）。"""
    out = []
    for ev in _iter_events(msgs):
        if ev.get("type") != "task.state":
            continue
        data = ev.get("data") or {}
        if data.get("to") in TASK_TERMINAL_BAD:
            out.append({"tid": data.get("tid"), "type": data.get("type"), "from": data.get("from"),
                        "to": data.get("to"), "why": data.get("why")})
    return out


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _run(city: str, seconds: float, port: int, out_path: Path, verbose: bool) -> dict:
    import httpx
    import uvicorn
    from websockets.asyncio.client import connect

    from awr.api.inproc import InprocSim, inproc_settings
    from awr.api.main import create_app
    from awr.runtime.statering import LocalRing

    scenario = ROOT / "scenarios" / f"d22-{city}-watch.json"
    if not scenario.exists():
        raise SystemExit(f"missing {scenario}; run `python -m tools.sim.gen_city_watch` first")
    cap_dir = Path(tempfile.mkdtemp(prefix=f"awr-cap-{city}-"))
    os.environ["AWR_SESSION_CONFIG"] = str(scenario)
    os.environ["AWR_RT_CAPTURE"] = str(cap_dir)

    s = inproc_settings(city, profile="dev")
    sim = InprocSim(s, n=0, autoplay=True, load_world=True, plugins=SIM_PLUGINS).start()
    try:
        app = create_app(s, ring_cls=LocalRing)
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, ws="websockets", log_level="warning", lifespan="on",
                             ws_per_message_deflate=False, ws_max_size=262144)
        server = uvicorn.Server(cfg)
        task = asyncio.ensure_future(server.serve())
        t0 = time.monotonic()
        while not server.started:
            if time.monotonic() - t0 > 20:
                raise TimeoutError("uvicorn did not start")
            await asyncio.sleep(0.02)
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient() as hc:
            r = await hc.post(f"{base}/api/auth/token", json={"role": "viewer"}, timeout=10)
            r.raise_for_status()
            tok = r.json()["token"]

        ws = await connect(f"ws://127.0.0.1:{port}/api/rt", subprotocols=[PROTO, f"bearer.{tok}"],
                           origin=base, max_size=None, compression=None, open_timeout=10)
        texts: list[dict] = []
        n_batches = 0

        import awr.contracts.frame as F

        async def recv_one(timeout: float = 5.0) -> None:
            nonlocal n_batches
            m = await asyncio.wait_for(ws.recv(), timeout)
            if isinstance(m, bytes):
                if m[0] != F.OP_TIME:  # 批帧需要 ack（见 tests/rt/rtc.py Client.recv）
                    n_batches += 1
                    h = F.decode_batch_header(m)
                    await ws.send(json.dumps({"op": "ack", "frame": h.frame_seq}, separators=(",", ":")))
            else:
                j = json.loads(m)
                texts.append(j)

        info = None
        for _ in range(3):
            await recv_one()
            if texts and texts[-1].get("op") == "serverInfo":
                info = texts[-1]
        assert info is not None and info.get("world", {}).get("id") == city, info
        await ws.send(json.dumps({"op": "hello", "client": "d22-capture/0.1", "contracts": info.get("contracts", "1.1.0")},
                                  separators=(",", ":")))
        subs = [{"id": i + 1, "topic": t, "rate": 10 if "field" not in t else 2, "mode": "latest"} for i, t in
                enumerate(BROAD_TOPICS)]
        subs.append({"id": 0, "topic": "event", "rate": 0, "mode": "all"})
        await ws.send(json.dumps({"op": "subscribe", "subs": subs}, separators=(",", ":")))

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            with contextlib.suppress(TimeoutError):
                await recv_one(timeout=max(0.1, end - time.monotonic()))
        await ws.close()
        await asyncio.sleep(1.5)  # Capture.close() 的后台写线程落盘
        server.should_exit = True
        await task

        kinds = Counter(ev.get("type") for ev in _iter_events(texts))
        bad = _bad_task_states(texts)
        if bad:
            # 不写 out_path：这次录制不能用，留着旧的 show.awrrt（如果有）不动，由调用方（main()）决定是否换点位重录，
            # 不能让这次失败覆盖掉之前一份能用的录制（D2.2-R-world-报告.md 第 3 节：失败一旦被循环回放会一直重演）。
            raise CaptureInfeasible(f"{city}: 录制期间出现任务终态异常 {bad}")
        caps = sorted(cap_dir.glob("c-*.awrrt"))
        if not caps:
            raise RuntimeError(f"no .awrrt written under {cap_dir}")
        cap_file = max(caps, key=lambda p: p.stat().st_size)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(cap_file.read_bytes())
        return {"city": city, "seconds": seconds, "texts": len(texts), "batches": n_batches,
                "events_by_kind": dict(kinds), "capture_bytes": cap_file.stat().st_size, "out": str(out_path)}
    finally:
        sim.stop()
        import shutil

        shutil.rmtree(cap_dir, ignore_errors=True)
        os.environ.pop("AWR_SESSION_CONFIG", None)
        os.environ.pop("AWR_RT_CAPTURE", None)


def _regenerate(city: str, retry_no: int) -> None:
    """重录前换一批不与之前任何一次（本次生成、更早的重录）重叠的候选点位重新生成剧本并写回
    `scenarios/d22-<city>-watch.json`（复用 `gen_city_watch.build_city_validated` 的 `GeoRetry` 重试路径，
    `start_attempt` 取一段全新的区间，`max_attempts` 用其默认值）。"""
    from tools.sim.gen_city_watch import build_city_validated, dumps_canonical

    start_attempt = build_city_validated.__kwdefaults__["max_attempts"] * retry_no
    doc = build_city_validated(city, start_attempt=start_attempt)
    (ROOT / "scenarios" / f"d22-{city}-watch.json").write_text(dumps_canonical(doc), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.sim.capture_city_watch")
    ap.add_argument("--city", required=True)
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--retries", type=int, default=2, help="录制期间出现 task.state INFEASIBLE/FAILED 时，换点位"
                    "重新生成剧本并重录的次数上限（默认 2，即最多尝试 3 次）")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else ROOT / "worlds" / a.city / "show" / "show.awrrt"

    last: CaptureInfeasible | None = None
    for attempt in range(a.retries + 1):
        port = a.port or _free_port()
        try:
            summary = asyncio.run(_run(a.city, a.seconds, port, out, a.verbose))
        except CaptureInfeasible as e:
            last = e
            print(json.dumps({"city": a.city, "attempt": attempt, "capture_failed": str(e)}, ensure_ascii=False))
            if attempt < a.retries:
                _regenerate(a.city, attempt + 1)
            continue
        summary["attempts"] = attempt + 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    raise SystemExit(f"{a.city}: 重录 {a.retries} 次后仍判定任务不可行，最后一次：{last}")


if __name__ == "__main__":
    raise SystemExit(main())
