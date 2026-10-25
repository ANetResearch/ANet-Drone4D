"""提交结果与已发布榜单的同步（M27 §6.7.9"结果可见性"；D3-AC-43、D3-AC-57；B21 遗留 10）。

提交文档的 `result.lanes[]`（`bench/submission.v1`）在评分时由 subworker 写入，上榜提交另回填当时榜单上的名次、并列组与非支配层级。
名次与层级取决于同一"分轨 × 赛道"的其他条目，之后的每次发布（其他提交上榜、基线入榜、撤回下线、重评版本）都可能改变它们，
评分时的快照因此会与榜单不一致（B21：relay-l2 文档为第 1 层，榜单 reid、vehicle 为第 2 层）。

`refresh_results(board_dir, board)` 在每次发布后（`boardstore.publish` 写完 `board.json` 之后调用）以已发布的榜单为准刷新本榜单版本的
提交结果：
- 已完成（DONE）且在榜（条目 `<team>/<submission_id>` 出现在 `board.json`）的提交：逐赛道把 `metrics`（点值、置信区间、`repeat_delta`、
  名次、并列组）、`status` 与 `dominance_tier` 改为榜单上的值（层级为空时去掉该字段）；
- 已完成但不在榜（可见性为 private、或被同团队更新的提交取代为代表条目）的提交：去掉各赛道的名次、并列组与层级（只在榜单上有意义），
  点值与置信区间保留评分时的值；
- 其他状态与其他榜单版本的提交不变。

同时在服务端字段 `_meta.result_board` 记录快照来源 `{board_version, board_sha256, refreshed_unix_ns}`（`_meta` 不经接口返回）。
只在内容变化时写回；写回在 `SubmissionStore.lock` 内重读文档后只替换 `result` 与 `_meta.result_board`，不覆盖并发的状态或可见性变更
（同一进程内互斥；跨进程的读改写窗口为单个文件的重读至原子替换）。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

__all__ = ["RANK_KEYS", "refresh_results"]

RANK_KEYS = ("rank", "tie_group")


def _sha(board: dict) -> str:
    return hashlib.sha256((json.dumps(board, ensure_ascii=False, indent=1) + "\n").encode("utf-8")).hexdigest()


def _lanes(doc: dict, entries: dict[str, dict] | None) -> list[dict]:
    out = []
    for ln in (doc.get("result") or {}).get("lanes", []):
        ln = dict(ln)
        e = (entries or {}).get(ln.get("lane"))
        if e is not None:
            ln["metrics"], ln["status"] = e["metrics"], e["status"]
            if e.get("dominance_tier") is not None:
                ln["dominance_tier"] = int(e["dominance_tier"])
            else:
                ln.pop("dominance_tier", None)
        else:
            ln["metrics"] = {m: {k: v for k, v in x.items() if k not in RANK_KEYS} for m, x in (ln.get("metrics") or {}).items()}
            ln.pop("dominance_tier", None)
        out.append(ln)
    return out


def refresh_results(board_dir: Path, board: dict, *, store=None, now_ns: int | None = None) -> list[str]:
    """以已发布的 `board`（`<board_dir>/board.json` 的内容）刷新本榜单版本的提交结果（规则见模块说明）。`store` 缺省为
    `<data_root>/submissions`（`board_dir` 为 `<data_root>/boards/<版本>`；目录不存在时不做任何事）。→ 改写过的 submission_id。"""
    from awr.bench.submissions import SubmissionStore

    board_dir = Path(board_dir)
    if store is None:
        root = board_dir.parent.parent / "submissions"
        if not root.is_dir():
            return []
        store = SubmissionStore(root)
    bv = board_dir.name
    by_entry: dict[tuple[str, str], dict[str, dict]] = {}
    for e in board.get("entries", []):
        if not e.get("baseline"):
            by_entry.setdefault((e["entry_id"], e["track"]), {})[e["lane"]] = e
    sha = _sha(board)
    now = time.time_ns() if now_ns is None else int(now_ns)
    changed = []
    for d in store.all():
        if d.get("state") != "DONE" or d["_meta"].get("board_version") != bv or not (d.get("result") or {}).get("lanes"):
            continue
        sid = d["submission_id"]
        want = _lanes(d, by_entry.get((f"{d['_meta']['team']}/{sid}", d["track"])))
        if want == d["result"]["lanes"] and (d["_meta"].get("result_board") or {}).get("board_sha256") == sha:
            continue
        with store.lock:
            cur = store.get(sid)
            if cur.get("state") != "DONE":
                continue
            cur["result"] = {**cur["result"], "lanes": _lanes(cur, by_entry.get((f"{cur['_meta']['team']}/{sid}", cur["track"])))}
            cur["_meta"]["result_board"] = {"board_version": bv, "board_sha256": sha, "refreshed_unix_ns": now}
            store._put(cur)
        changed.append(sid)
    return changed
