"""红队运行器（M26 §8.3；D3-AC-16）：在排名配置上运行一个回合，录下 Agent 收到的全部消息并给出检索报告。

    env = ProbedEnv(spec)                                       # spec.ranked = True、CSPRNG 种子、seed_commit
    run = record_socket_episode(env, baseline_launcher("track_share", sdk_src))       # 本机：真实 SDK 基线经 Unix 套接字
    run = record_container_episode(env, run_dir, "d3-agent-py:dev", cmd=[...])        # dmax：真实 Agent 容器
    report = leak_report(run)                                   # R1、R2 检索（search.audit_frames）
    write_report(report, run_dir)                               # leak_audit_report.json（只含计数、路径与统计量）

两种路径的下发帧都经 `SocketAgentHost._send` → `CaptureAudit.check_body`（白名单校验通过后录制），与正式运行（`bench.runner`）
同一编码、同一审计、同一发送顺序。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from awr.agentsdk.env.obs_audit import audit_scope
from awr.bench.redteam.capture import CaptureAudit
from awr.bench.redteam.search import audit_frames
from awr.bench.redteam.truth import ProbedEnv, TruthSet

__all__ = ["RedteamRun", "baseline_launcher", "leak_report", "record_container_episode", "record_socket_episode", "write_report"]


@dataclass
class RedteamRun:
    env: ProbedEnv
    capture: CaptureAudit
    manifest: dict
    stats: dict


def baseline_launcher(name: str, sdk_src: Path | str, *, python: str = sys.executable) -> Callable[[Mapping], list]:
    """→ `launch(hosts)`：每架机一个 `python -m anet4d.serve --baseline <name>` 子进程（SDK 源码目录经 PYTHONPATH 提供）。"""

    def launch(hosts: Mapping) -> list[subprocess.Popen]:
        env = {**os.environ, "PYTHONPATH": f"{sdk_src}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}
        return [subprocess.Popen([python, "-m", "anet4d.serve", "--baseline", name, "--sock", str(h.path)], env=env)
                for h in hosts.values()]

    return launch


def _wait(handles: Sequence, timeout_s: float) -> None:
    for h in handles:
        if hasattr(h, "wait"):
            try:
                h.wait(timeout_s)
            except subprocess.TimeoutExpired:
                h.kill()
                h.wait(10)
        elif hasattr(h, "join"):
            h.join(timeout_s)


def record_socket_episode(env: ProbedEnv, launch: Callable[[Mapping], Sequence], *, sock_root: Path | None = None,
                          step_budget_s: float | None = 30.0, capture_dir: Path | None = None, keep: bool = True) -> RedteamRun:
    """本机套接字路径：宿主套接字建在短路径临时目录（AF_UNIX 路径上限），`launch(hosts)` 启动 Agent 并返回可 `wait`/`join` 的句柄。"""
    from awr.agentsdk.env.remote import SocketAgentHost, run_remote_episode

    own_root = sock_root is None
    root = Path(tempfile.mkdtemp(prefix="d3rt", dir="/tmp")) if own_root else Path(sock_root)
    cap = CaptureAudit(out_dir=capture_dir, keep=keep)
    try:
        with audit_scope(cap):
            hosts = {u: SocketAgentHost(u, root / u / "agent.sock") for u in env.possible_agents}
        handles = launch(hosts)
        try:
            for h in hosts.values():
                h.accept()
            manifest, stats = run_remote_episode(env, hosts, step_budget_s=step_budget_s)
        finally:
            for h in hosts.values():                          # 先关闭宿主（CLOSE 并断开），使握手失败时 Agent 不在 recv 上等满超时
                h.close()
            _wait(handles, 60.0)
    finally:
        cap.close()
        if own_root:
            shutil.rmtree(root, ignore_errors=True)
    return RedteamRun(env, cap, manifest, stats)


def record_container_episode(env: ProbedEnv, run_dir: Path, image: str, *, capture_dir: Path | None = None, keep: bool = True,
                             **kw) -> RedteamRun:
    """容器路径（dmax）：`run_container_episode` 的全部下发帧经同一审计录制；`kw` 原样传给 `run_container_episode`。"""
    from awr.agentsdk.env.containers import run_container_episode

    cap = CaptureAudit(out_dir=capture_dir, keep=keep)
    try:
        with audit_scope(cap):
            manifest, stats = run_container_episode(env, Path(run_dir), image, **kw)
    finally:
        cap.close()
    return RedteamRun(env, cap, manifest, stats)


def leak_report(run: RedteamRun, *, frames=None, config: dict | None = None, **kw) -> dict:
    """R1、R2 检索报告；`frames` 缺省取内存中的录制（`keep=False` 时传 `read_capture(capture_dir)`）。"""
    truth = TruthSet.from_env(run.env)
    spec = run.env.spec
    cfg = {"run_id_sha256_8": hashlib.sha256(spec.run_id.encode()).hexdigest()[:8], "ranked": bool(spec.ranked),
           "physics": spec.physics, **(config or {})}
    return audit_frames(run.capture.frames if frames is None else frames, truth, audit_summary=run.capture.summary(), config=cfg, **kw)


def write_report(report: dict, run_dir: Path) -> Path:
    p = Path(run_dir) / "leak_audit_report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return p
