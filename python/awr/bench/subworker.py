"""内部提交流水线（AWR-17 §18.23；M27 §6.7.9；D3-AC-56）：`python -m awr.bench.subworker --data-root <dir> [--once]`。

按创建先后逐个处理 RECEIVED 的提交，每个阶段开始前重读状态（撤回即停止）：

1. SCANNING：`docker image inspect <digest>`：镜像须已在 dmax（私有仓库推送或本机构建）、linux/amd64、≤ 8 GiB；否则 REJECTED 692 `scan`；
2. VALIDATING：冒烟运行（Fast 档、`smoke_s` 秒、公开冒烟种子 0）：每架机须完成握手并返回动作、无协议错误、容器以 0 退出；
   否则 REJECTED 692 `smoke`；
3. EVALUATING：按榜单版本的评测配置（`boards/<ver>/config.json`，秘密种子）逐种子正式运行（Realistic 档、隔离容器）；
   平台原因失败（Env 或物理后端异常）自动重跑一次并计入 `runs.platform_rerun`，仍失败则 FAILED；
4. SCORING：评测行追加到 `rows.jsonl`，按 `score_version` 聚合本提交的四项指标与置信区间；`visibility = board` 时重算榜单并
   回填名次与层级 → DONE。

运行目录 `dataset/<event>/runs/<submission_id>-r<k>`（k 为种子序号，不暴露种子值）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

from awr.bench.boardstore import append_rows, load_config, publish, slim_evaluation
from awr.bench.submissions import SubmissionStore

__all__ = ["process_one", "scan_image"]

IMAGE_MAX_B = 8 * 1024 ** 3


def _reason(why: str, **detail) -> dict:
    return {"code": 692, "name": "SUBMISSION_REJECTED", "detail": {"why": why, **detail}}


def scan_image(digest: str, docker: str = "docker") -> dict | None:
    """→ None 表示通过，否则为 REJECTED 的 reason。"""
    r = subprocess.run([docker, "image", "inspect", digest, "--format", "{{json .}}"], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return _reason("scan", check="image_missing")
    try:
        info = json.loads(r.stdout)
    except ValueError:
        return _reason("scan", check="inspect")
    if info.get("Os") != "linux" or info.get("Architecture") != "amd64":
        return _reason("scan", check="platform")
    if int(info.get("Size", 0)) > IMAGE_MAX_B:
        return _reason("scan", check="size")
    return None


def _alive(st: SubmissionStore, sid: str) -> bool:
    return st.get(sid)["state"] not in ("WITHDRAWN", "REJECTED", "FAILED", "DONE")


def process_one(data_root: Path, *, docker: str = "docker", smoke_s: float = 20.0, n_boot: int = 10_000, run_job=None,
                log=print) -> str | None:
    """处理一个 RECEIVED 提交；→ submission_id 或 None（队列为空）。"""
    from awr.bench.eval.aggregate import lane_metrics
    if run_job is None:
        from awr.bench.runner import run_job

    data_root = Path(data_root)
    st = SubmissionStore(data_root / "submissions")
    d = st.next_received()
    if d is None:
        return None
    sid, team = d["submission_id"], d["_meta"]["team"]
    bdir = data_root / "boards" / d["_meta"]["board_version"]
    cfg = load_config(bdir)
    rs = cfg["run_spec"]
    policy = {"kind": "image", "image_digest": d["image_digest"], "entry": d["entry"]}
    common = {"worlds_root": Path(cfg["worlds_root"]), "events_root": Path(cfg["events_root"]), "agent_image": cfg.get("agent_image", "d3-agent-py:dev"),
              "docker": docker}
    work = data_root / "submissions" / sid
    st.transition(sid, "SCANNING", expect="RECEIVED")
    bad = scan_image(d["image_digest"], docker)
    if bad:
        st.transition(sid, "REJECTED", reason=bad)
        log(f"{sid}: REJECTED scan {bad['detail']}")
        return sid
    if not _alive(st, sid):
        return sid
    st.transition(sid, "VALIDATING", expect="SCANNING")
    try:
        r = run_job(rs, policy=policy, seed=0, run_id=f"{sid}-smoke", run_dir=work / "smoke", physics="kinematic", duration_s=smoke_s, **common)
        ag = r["agents"]
        fail = {u: s for u, s in ag.items() if s.get("acts", 0) == 0 or s.get("protocol_errors") or s.get("exited") not in (None, "bye")
                or s.get("exit_code") not in (0, None)}
    except Exception as e:                                   # 握手超时、容器启动失败等均归为冒烟失败
        fail = {"error": getattr(e, "code", None) or type(e).__name__}
    if fail:
        st.transition(sid, "REJECTED", reason=_reason("smoke", agents=sorted(fail)))
        log(f"{sid}: REJECTED smoke {fail}")
        return sid
    if not _alive(st, sid):
        return sid
    st.transition(sid, "EVALUATING", expect="VALIDATING")
    rows, reruns = [], 0
    event = rs["event"]["id"]
    for k, seed in enumerate(cfg["seeds"]):
        run_dir = data_root / "dataset" / event / "runs" / f"{sid}-r{k}"
        for attempt in (0, 1):
            try:
                res = run_job(rs, policy=policy, seed=int(seed), run_id=f"{sid}-r{k}", run_dir=run_dir, physics=cfg.get("physics"),
                              gz=cfg.get("gz") or None, **common)
                break
            except Exception:                               # 平台原因：重跑一次
                log(f"{sid}: run r{k} attempt {attempt} failed\n{traceback.format_exc()[-1500:]}")
                if attempt == 1:
                    st.transition(sid, "FAILED", reason={"code": 320, "name": "INTERNAL", "detail": {"why": "platform", "run": k}})
                    return sid
                reruns += 1
        ev = json.loads((Path(res["run_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
        rows.append({"entry_id": f"{team}/{sid}", "team": team, "display_name": sid, "baseline": False, "track": cfg["track_id"],
                     "lane": cfg["lane"], "event_type": cfg.get("event_type", cfg["lane"]), "event": event, "seed": k, "submission_id": sid,
                     "evaluation": slim_evaluation(ev)})
    st.transition(sid, "SCORING", expect="EVALUATING")
    append_rows(bdir, rows)
    met, _boots = lane_metrics(rows, key=(f"{team}/{sid}", bdir.name, cfg["lane"]), n_boot=n_boot)
    valid = sum(r["evaluation"]["ranked_valid"] for r in rows)
    lane = {"lane": cfg["lane"], "metrics": met, "runs": {"total": len(rows), "valid": valid, "invalid": len(rows) - valid, "platform_rerun": reruns},
            "status": "valid" if valid >= 0.8 * len(rows) else "invalid", "composite": None}
    st.transition(sid, "DONE", result={"lanes": [lane]})
    if st.get(sid)["visibility"] == "board":
        board = publish(bdir, st.board_view(), n_boot=n_boot)
        e = next((x for x in board["entries"] if x["entry_id"] == f"{team}/{sid}"), None)
        if e is not None:
            lane["metrics"] = e["metrics"]
            if e["dominance_tier"] is not None:
                lane["dominance_tier"] = e["dominance_tier"]
            doc = st.get(sid)
            doc["result"] = {"lanes": [lane]}
            st._put(doc)
    log(f"{sid}: DONE {json.dumps({m: v['value'] for m, v in met.items()})}")
    return sid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.bench.subworker")
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--once", action="store_true", help="process at most one submission and exit")
    ap.add_argument("--poll-s", type=float, default=15.0)
    ap.add_argument("--smoke-s", type=float, default=20.0)
    ap.add_argument("--n-boot", type=int, default=10_000)
    a = ap.parse_args(argv)
    while True:
        sid = process_one(a.data_root, smoke_s=a.smoke_s, n_boot=a.n_boot, log=lambda m: print(m, flush=True))
        if a.once:
            return 0
        if sid is None:
            time.sleep(a.poll_s)


if __name__ == "__main__":
    sys.exit(main())
