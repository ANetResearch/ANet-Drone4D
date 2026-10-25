"""榜单版本目录（M27 §6.7.9；D3-AC-57）：`<data_root>/boards/<board_version>/`

- `config.json`：评测轮次配置（run_spec、事件与世界根目录、物理档、分轨参数）；含秘密种子，只在服务端读取，任何接口不返回；
- `rows.jsonl`：评测行（基线与内部提交的每个 Run：entry_id、team、display_name、baseline、track、lane、event、seed、
  `evaluation{components, ranked_valid, invalid_reasons}`、可选 `submission_id`）；
- `board.json`、`details.json`：由 rows 重算（`publish`），同一输入逐字节一致。

上榜规则：基线行全部计入；内部提交只计入 `visibility = board` 且 `state = DONE` 的提交，同一团队在同一"分轨 × 赛道"只取最近完成
的一个提交作为代表条目（D3-AC-57 e）。非排名运行（Fast 档、SITL 与开发运行，`aggregate.ranked_row` 为假）在选代表条目之前剔除，
不进入 board.json 与 details.json，也不会以只含非排名运行的提交顶替团队的代表条目（D3-AC-57 f）；开发榜（`track.dev`）例外。

gz 锁步档限定（D3-AC-43"只收 gz 锁步档的正式运行"）：正式榜（非开发榜）的轮次配置物理档须为 `realistic`（gz-sim 锁步，缺省值；
`run_spec.physics` 与 `config.physics` 同口径），否则 `publish` 与 `compose_board` 拒绝（`BoardPhysicsError`）；评测行显式声明的物理档
（行或 `evaluation` 的 `physics`）不是 `realistic`、或声明 `lockstep = false` 的，`append_rows` 拒绝写入，`publish` 亦不计入；未声明物理档的行
按 `aggregate.ranked_row` 的评测器口径（`physics_not_ranked`、`ranked = false`）剔除。

提交结果同步：每次发布后以 `board.json` 刷新本榜单版本 DONE 提交的 `result`（名次、并列组、层级以榜单为准，不在榜的去掉名次与层级），
见 `awr.bench.board.results`。

撤回下线（D3-AC-43"提交者撤回后 24 h 内条目下线"；M27 §6.7.9"结果可见性"）：`publish` 同时写 `listing.json`（上榜提交条目
`entry_id → submission_id`，同一输入逐字节一致）；读取侧（`awr.bench.api.board`）以之对照提交的当前状态，不再满足上榜规则
（`state = DONE` 且 `visibility = board`）或提交记录缺失的条目立即不返回；`delist_withdrawn` 对账：当前版本重算榜单（他者名次与
层级随之更新），旧版本只读、只由读取侧隐藏；`rows.jsonl` 与提交的评分记录一律保留。

版本冻结（D3-AC-57）：`config.json.score_version`（缺省为当前 `SCORE_VERSION`）不是已登记的评分版本（`aggregate.SCORE_VERSIONS`），或与已发布
`board.json` 的评分版本（旧制品缺省 `d3-score-0`）不同，或重算后的赛道集合与已发布的 `board.json` 不同，`publish` 拒绝（`BoardVersionError`）：
评分版本或赛道集合变更须发布为新榜单版本，旧版本只读。排行榜 v1（`drone4d.bench.board_config.v1`，`d3-score-1`）另比较全部冻结项
（`aggregate.frozen_diff`：分轨、赛道与 test 事件清单、种子集承诺、test 划分版本、评测器主版本、平台摘要、GPU 型号与 CPU ISA 等级），
配置或 EvalStore 不合规（`aggregate.BoardConfigError`）同样以 `BoardVersionError` 拒绝。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from awr.bench.board.results import refresh_results
from awr.bench.eval.aggregate import (
    SCORE_VERSIONS,
    BoardConfigError,
    build_board,
    compose,
    entry_details,
    frozen_diff,
    is_v1,
    ranked_row,
)
from awr.bench.eval.thresholds import SCORE_VERSION

__all__ = ["GZ_PHYSICS", "LISTING", "WITHDRAW_SLA_S", "BoardPhysicsError", "BoardVersionError", "append_rows", "board_physics", "compose_board",
           "current_version", "delist_withdrawn", "entry_listed", "gz_row", "load_config", "load_listing", "load_rows", "on_board", "publish",
           "slim_evaluation", "versions"]

_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GZ_PHYSICS = "realistic"               # gz-sim 锁步档（M24；ADR-176：缺省物理档与正式成绩来源）
LISTING = "listing.json"
WITHDRAW_SLA_S = 86_400                # 撤回后条目下线时限（D3-AC-43、M27 §6.7.9：24 h）


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


class BoardPhysicsError(BoardVersionError):
    """正式榜的轮次配置或评测行不是 gz 锁步档（`realistic`）：正式榜只收 gz 锁步档的正式运行（D3-AC-43）。"""


def slim_evaluation(doc: dict, *, physics: str | None = None) -> dict:
    """评测行的 `evaluation` 摘要；`physics` 给出时（取运行清单的 `physics`）一并写入，供上榜时核对物理档。"""
    out = {"components": doc["components"], "ranked": bool(doc.get("ranked", True)), "ranked_valid": bool(doc["ranked_valid"]),
           "invalid_reasons": list(doc.get("invalid_reasons", []))}
    phys = physics if physics is not None else doc.get("physics")
    if phys is not None:
        out["physics"] = str(phys)
    return out


def board_physics(cfg: dict) -> str:
    """轮次配置的物理档：`config.physics`，其次 `run_spec.physics`，缺省 `realistic`（AWR-17 API-FR-131）。"""
    return str(cfg.get("physics") or (cfg.get("run_spec") or {}).get("physics") or GZ_PHYSICS)


def _dev(cfg: dict) -> bool:
    return bool((cfg.get("track") or {}).get("dev"))


def gz_row(r: dict) -> bool:
    """评测行是否为 gz 锁步档：显式声明的物理档（行或 `evaluation` 的 `physics`）须为 `realistic`，且未声明 `lockstep = false`；
    未声明物理档的行由 `aggregate.ranked_row` 的评测器口径把关。"""
    ev = r.get("evaluation") or {}
    phys = r.get("physics", ev.get("physics"))
    lock = r.get("lockstep", ev.get("lockstep"))
    return phys in (None, GZ_PHYSICS) and lock is not False


def _check_physics(board_dir: Path, cfg: dict) -> None:
    if not _dev(cfg) and board_physics(cfg) != GZ_PHYSICS:
        raise BoardPhysicsError(f"board {Path(board_dir).name} physics is {board_physics(cfg)}, a ranked board only takes {GZ_PHYSICS} "
                                "(gz lockstep) runs: mark it track.dev or use a new board version")


def load_config(board_dir: Path) -> dict:
    return json.loads((Path(board_dir) / "config.json").read_text(encoding="utf-8"))


def load_rows(board_dir: Path) -> list[dict]:
    p = Path(board_dir) / "rows.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def append_rows(board_dir: Path, rows: list[dict]) -> None:
    """追加评测行。正式榜（存在 `config.json` 且不是开发榜）拒绝显式声明非 gz 锁步档的行（`BoardPhysicsError`，整批不写）。"""
    cp = Path(board_dir) / "config.json"
    if cp.is_file() and not _dev(load_config(board_dir)):
        bad = sorted({str(r.get("entry_id")) for r in rows if not gz_row(r)})
        if bad:
            raise BoardPhysicsError(f"board {Path(board_dir).name} only takes {GZ_PHYSICS} (gz lockstep) runs, refused rows of {bad}")
    p = Path(board_dir) / "rows.jsonl"
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def on_board(s: dict | None) -> bool:
    """提交是否满足上榜规则：已完成（DONE）且可见性为 `board`；记录缺失视为不上榜。"""
    return s is not None and s.get("state") == "DONE" and s.get("visibility") == "board"


def _listed(rows: list[dict], subs: dict[str, dict]) -> list[dict]:
    """基线行 + 每团队每（分轨、赛道）最近完成且公开上榜的提交。subs: submission_id → {state, visibility, team, done_unix_ns}。"""
    out = [r for r in rows if r.get("baseline")]
    best: dict[tuple[str, str, str], tuple[int, str]] = {}
    for r in rows:
        sid = r.get("submission_id")
        s = subs.get(sid or "")
        if r.get("baseline") or not on_board(s):
            continue
        key = (s["team"], r["track"], r["lane"])
        cand = (int(s.get("done_unix_ns", 0)), str(sid))
        if key not in best or cand > best[key]:
            best[key] = cand
    keep = {sid for _t, sid in best.values()}
    return out + [r for r in rows if r.get("submission_id") in keep and not r.get("baseline")]


def _board_rows(board_dir: Path, cfg: dict, subs: dict[str, dict] | None) -> list[dict]:
    """上榜的评测行：剔除非排名运行与非 gz 锁步档的行（开发榜除外）后按上榜规则选出基线与代表条目。"""
    dev = _dev(cfg)
    return _listed([r for r in load_rows(board_dir) if dev or (ranked_row(r) and gz_row(r))], subs or {})


def load_listing(board_dir: Path) -> dict[str, str] | None:
    """`listing.json` 的上榜提交条目 `entry_id → submission_id`；未发布过（旧制品）时为 None。"""
    p = Path(board_dir) / LISTING
    return json.loads(p.read_text(encoding="utf-8"))["entries"] if p.is_file() else None


def _sid_of(entry_id: str) -> str | None:
    """无 `listing.json` 的旧制品：提交条目的 entry_id 为 `<team>/<submission_id>`（`awr.bench.subworker`）。"""
    tail = entry_id.rsplit("/", 1)[-1]
    return tail if tail.startswith("sub-") else None


def entry_listed(entry: dict, listing: dict[str, str] | None, subs: dict[str, dict]) -> bool:
    """已发布条目在读取时是否仍可显示：基线参考行恒显示；提交条目须其提交仍满足上榜规则（撤回即不显示）。"""
    if entry.get("baseline"):
        return True
    sid = listing.get(entry["entry_id"]) if listing is not None else _sid_of(entry["entry_id"])
    return sid is None or on_board(subs.get(sid))


def publish(board_dir: Path, subs: dict[str, dict] | None = None, *, n_boot: int = 10_000) -> dict:
    board_dir = Path(board_dir)
    cfg = load_config(board_dir)
    sv = cfg.get("score_version", SCORE_VERSION)
    if sv not in SCORE_VERSIONS:
        raise BoardVersionError(f"board {board_dir.name} is scored with {sv}, platform knows {sorted(SCORE_VERSIONS)}: publish a new board version")
    _check_physics(board_dir, cfg)
    prev = board_dir / "board.json"
    old = json.loads(prev.read_text(encoding="utf-8")) if prev.is_file() else None
    if old is not None and old.get("score_version", SCORE_VERSION) != sv:
        raise BoardVersionError(f"board {board_dir.name} is scored with {old.get('score_version', SCORE_VERSION)}, config says {sv}: "
                                "publish a new board version")
    rows = _board_rows(board_dir, cfg, subs)
    try:
        board = build_board(rows, board_version=board_dir.name, n_boot=n_boot, track=cfg.get("track"), board=cfg)
        details = entry_details(rows, board=cfg)
    except BoardConfigError as e:
        raise BoardVersionError(f"board {board_dir.name}: {e}") from e
    if old is not None:
        if is_v1(cfg):
            diff = frozen_diff(old, board)
            if diff:
                raise BoardVersionError(f"board {board_dir.name} frozen items {diff} changed: publish a new board version")
        else:
            was, now = {x["lane"] for x in old["lanes"]}, {x["lane"] for x in board["lanes"]}
            if was != now:
                raise BoardVersionError(f"board {board_dir.name} lanes {sorted(was)} -> {sorted(now)}: publish a new board version")
    (board_dir / "board.json").write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (board_dir / "details.json").write_text(json.dumps(details, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    listing = {r["entry_id"]: r["submission_id"] for r in rows if not r.get("baseline") and r.get("submission_id")}
    (board_dir / LISTING).write_text(json.dumps({"entries": listing}, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    refresh_results(board_dir, board)                       # 提交结果中的名次与层级以本次发布为准（B21 遗留 10；awr.bench.board.results）
    return board


def delist_withdrawn(root: Path, subs: dict[str, dict], *, n_boot: int = 10_000, now_ns: int | None = None,
                     updated: dict[str, int] | None = None) -> list[dict]:
    """撤回下线对账（D3-AC-43）：逐榜单版本找出已发布但其提交不再满足上榜规则的条目。当前版本重算榜单（`republished`），旧版本只读，
    由读取侧隐藏（`hidden`）；`updated`（submission_id → 最近一次状态或可见性变更的 UNIX ns）给出时报告撤回至对账的时延与是否超过
    `WITHDRAW_SLA_S`。→ [{board_version, action, entries[{entry_id, submission_id, lag_s?, within_sla?}]}]。"""
    root = Path(root)
    now = time.time_ns() if now_ns is None else int(now_ns)
    vs = versions(root)
    cur = current_version(root, vs)
    out = []
    for v in vs:
        d = root / v
        listing = load_listing(d)
        if listing is None:
            b = json.loads((d / "board.json").read_text(encoding="utf-8"))
            listing = {e["entry_id"]: sid for e in b["entries"] if not e.get("baseline") and (sid := _sid_of(e["entry_id"]))}
        gone = sorted((eid, sid) for eid, sid in listing.items() if not on_board(subs.get(sid)))
        if not gone:
            continue
        action = "hidden"
        if v == cur:
            try:
                publish(d, subs, n_boot=n_boot)
                action = "republished"
            except BoardVersionError:                       # 赛道集合因下线而变化等：保持只读，由读取侧隐藏
                pass
        ents = []
        for eid, sid in gone:
            e = {"entry_id": eid, "submission_id": sid}
            if updated is not None and sid in updated:
                e["lag_s"] = round((now - int(updated[sid])) / 1e9, 3)
                e["within_sla"] = e["lag_s"] <= WITHDRAW_SLA_S
            ents.append(e)
        out.append({"board_version": v, "action": action, "entries": ents})
    return out


def compose_board(board_dir: Path, weights: dict[str, float], subs: dict[str, dict] | None = None, *, n_boot: int = 10_000) -> list[dict]:
    """以给定权重由存储的评测行重算合成值（不写文件、不重跑）。"""
    board_dir = Path(board_dir)
    cfg = load_config(board_dir)
    _check_physics(board_dir, cfg)
    try:
        return compose(_board_rows(board_dir, cfg, subs), weights, board_version=board_dir.name, n_boot=n_boot, track=cfg.get("track"), board=cfg)
    except BoardConfigError as e:
        raise BoardVersionError(f"board {board_dir.name}: {e}") from e
