"""榜单版本目录（M27 §6.7.9；D3-AC-57）：`<data_root>/boards/<board_version>/`

- `config.json`：评测轮次配置（run_spec、事件与世界根目录、物理档、分轨参数）；含秘密种子，只在服务端读取，任何接口不返回；
- `rows.jsonl`：评测行（基线与内部提交的每个 Run：entry_id、team、display_name、baseline、track、lane、event、seed、
  `evaluation{components, ranked_valid, invalid_reasons}`、可选 `submission_id`）；
- `board.json`、`details.json`：由 rows 重算（`publish`），同一输入逐字节一致。

上榜规则：基线行全部计入；内部提交只计入 `visibility = board` 且 `state = DONE` 的提交，同一团队在同一"分轨 × 赛道"只取最近完成
的一个提交作为代表条目（D3-AC-57 e）。

版本冻结（D3-AC-57）：`config.json.score_version`（缺省为当前 `SCORE_VERSION`）与本平台评分版本不同，或重算后的赛道集合与已发布的
`board.json` 不同，`publish` 拒绝（`BoardVersionError`）：评分版本或赛道集合变更须发布为新榜单版本，旧版本只读。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from awr.bench.eval.aggregate import build_board, compose, entry_details
from awr.bench.eval.thresholds import SCORE_VERSION

__all__ = ["BoardVersionError", "append_rows", "compose_board", "current_version", "load_config", "load_rows", "publish", "slim_evaluation", "versions"]

_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def versions(root: Path) -> list[str]:
    """已发布的榜单版本（`<root>/<ver>/board.json` 存在），按名称排序。"""
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and _VERSION.match(d.name) and (d / "board.json").is_file())


def current_version(root: Path, vs: list[str]) -> str | None:
    """`<root>/current` 所指版本；缺失或无效时取最新版本。"""
    p = root / "current"
    if p.is_file():
        v = p.read_text(encoding="utf-8").strip()
        if v in vs:
            return v
    return vs[-1] if vs else None


class BoardVersionError(Exception):
    """评分版本或赛道集合与已发布的榜单版本不一致（须新建榜单版本）。"""


def slim_evaluation(doc: dict) -> dict:
    return {"components": doc["components"], "ranked": bool(doc.get("ranked", True)), "ranked_valid": bool(doc["ranked_valid"]),
            "invalid_reasons": list(doc.get("invalid_reasons", []))}


def load_config(board_dir: Path) -> dict:
    return json.loads((Path(board_dir) / "config.json").read_text(encoding="utf-8"))


def load_rows(board_dir: Path) -> list[dict]:
    p = Path(board_dir) / "rows.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def append_rows(board_dir: Path, rows: list[dict]) -> None:
    p = Path(board_dir) / "rows.jsonl"
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def _listed(rows: list[dict], subs: dict[str, dict]) -> list[dict]:
    """基线行 + 每团队每（分轨、赛道）最近完成且公开上榜的提交。subs: submission_id → {state, visibility, team, done_unix_ns}。"""
    out = [r for r in rows if r.get("baseline")]
    best: dict[tuple[str, str, str], tuple[int, str]] = {}
    for r in rows:
        sid = r.get("submission_id")
        s = subs.get(sid or "")
        if r.get("baseline") or s is None or s.get("state") != "DONE" or s.get("visibility") != "board":
            continue
        key = (s["team"], r["track"], r["lane"])
        cand = (int(s.get("done_unix_ns", 0)), str(sid))
        if key not in best or cand > best[key]:
            best[key] = cand
    keep = {sid for _t, sid in best.values()}
    return out + [r for r in rows if r.get("submission_id") in keep and not r.get("baseline")]


def publish(board_dir: Path, subs: dict[str, dict] | None = None, *, n_boot: int = 10_000) -> dict:
    board_dir = Path(board_dir)
    cfg = load_config(board_dir)
    sv = cfg.get("score_version", SCORE_VERSION)
    if sv != SCORE_VERSION:
        raise BoardVersionError(f"board {board_dir.name} is scored with {sv}, platform is {SCORE_VERSION}: publish a new board version")
    rows = _listed(load_rows(board_dir), subs or {})
    board = build_board(rows, board_version=board_dir.name, n_boot=n_boot, track=cfg.get("track"))
    prev = board_dir / "board.json"
    if prev.is_file():
        old = {x["lane"] for x in json.loads(prev.read_text(encoding="utf-8"))["lanes"]}
        new = {x["lane"] for x in board["lanes"]}
        if old != new:
            raise BoardVersionError(f"board {board_dir.name} lanes {sorted(old)} -> {sorted(new)}: publish a new board version")
    (board_dir / "board.json").write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (board_dir / "details.json").write_text(json.dumps(entry_details(rows), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return board


def compose_board(board_dir: Path, weights: dict[str, float], subs: dict[str, dict] | None = None, *, n_boot: int = 10_000) -> list[dict]:
    """以给定权重由存储的评测行重算合成值（不写文件、不重跑）。"""
    board_dir = Path(board_dir)
    cfg = load_config(board_dir)
    return compose(_listed(load_rows(board_dir), subs or {}), weights, board_version=board_dir.name, n_boot=n_boot, track=cfg.get("track"))
