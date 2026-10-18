"""单个基准 Run 的执行与评测（M27 §6.7；WP-D3-09）：按 run_spec 构造 Env，Agent 一律在隔离容器中运行（线协议，AWR-17 §18.9），
完成后写 `evaluation.json`。

策略：`reference`（参考基线，平台镜像 `d3-agent-py` 以 `--baseline <name>` 启动）或 `image`（内部提交镜像，按摘要引用，
入口取 `entry.cmd` 与 `entry.env`）。产出：Env 记录的运行目录、`agents/wire_stats.json`（每机线协议统计与容器退出码）、
`evaluation.json`。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

__all__ = ["AGENT_IMAGE", "run_job"]

AGENT_IMAGE = "d3-agent-py:dev"


def _agent(policy: dict, agent_image: str) -> tuple[str, list[str], dict[str, str]]:
    if policy.get("kind") == "reference":
        return agent_image, ["python", "-m", "drone4d.serve", "--baseline", str(policy["name"])], {}
    if policy.get("kind") == "image":
        e = policy.get("entry") or {}
        return str(policy["image_digest"]), [str(x) for x in e.get("cmd", [])], {str(k): str(v) for k, v in (e.get("env") or {}).items()}
    raise ValueError(f"policy kind {policy.get('kind')!r} does not run in containers")


def run_job(rs: dict, *, policy: dict, seed: int, run_id: str, run_dir: Path, worlds_root: Path, events_root: Path,
            physics: str | None = None, gz: dict | None = None, agent_image: str = AGENT_IMAGE, cpus: float = 1.0, mem_gb: float = 2.0,
            step_budget_s: float | None = None, duration_s: float | None = None, docker: str = "docker",
            hello_timeout_s: float = 60.0) -> dict:
    from awr.agentsdk.env.containers import run_container_episode
    from awr.agentsdk.env.core import BenchEnv, _open_world
    from awr.agentsdk.env.spec import EnvSpec
    from awr.bench.eval.evaluator import evaluate_run

    rs = {**rs, "policy": {k: v for k, v in policy.items() if k in ("kind", "name", "image_digest", "params")}}
    if duration_s is not None:
        rs["duration_s"] = float(duration_s)
    run_dir = Path(run_dir)
    spec = EnvSpec.from_run_spec(rs, run_id=run_id, seed=seed, worlds_root=Path(worlds_root), events_root=Path(events_root), out_dir=run_dir,
                                 physics=physics, gz=gz)
    image, cmd, env = _agent(policy, agent_image)
    t0 = time.time()
    manifest, stats = run_container_episode(BenchEnv(spec), run_dir, image, cmd=cmd, entry_env=env, cpus=cpus, mem_gb=mem_gb,
                                            step_budget_s=step_budget_s, docker=docker, hello_timeout_s=hello_timeout_s)
    t1 = time.time()
    (run_dir / "agents").mkdir(parents=True, exist_ok=True)
    (run_dir / "agents" / "wire_stats.json").write_text(json.dumps(stats, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    doc = evaluate_run(run_dir, spec.event_dir, _open_world(spec.world_dir))
    (run_dir / "evaluation.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"run_id": run_id, "run_dir": str(run_dir), "run_wall_s": round(t1 - t0, 1), "eval_wall_s": round(time.time() - t1, 1),
            "pose_sha256": manifest.get("pose_sha256"), "components": doc["components"], "ranked_valid": doc["ranked_valid"], "agents": stats}
