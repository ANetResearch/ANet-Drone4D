"""评测的真值读取与计分事件集（M27-FR-086、FR-114、FR-124、FR-176；M23 §15.3 规则 1、4；D3-AC-43、D3-AC-62 目标搜索条款）。

真值读取（`TruthAccess`）：评测器读取的事件真值（`truth/r<k>/event_truth.json`）与运行真值（`RUN_TRUTH_FILES`）一律经同一入口：
- 缺省（`TruthAccess()`）直接读文件：dev 事件与 Env 刚写完、尚未移入隐藏区的运行目录；
- `TruthAccess.hidden(hs, event_id=…, run_id=…)`：test 事件经 `awr.bench.split.HiddenStore` 读取，主体为 Evaluator 服务
  （`SERVICE_EVALUATOR`），每个真值文件一条 `truth_read` 审计（op `read`），事件版本目录（磁带、plan.json、锚点）在打开前另写一条
  授权审计（op `evaluate`）；非评测员主体 691。离线重评（`board_v1_formal.py rescore`）由此读取隐藏区中的事件与运行真值。

运行真值的评测口径（`RUN_TRUTH_FILES`，评测器 0.3.0）：只读 `fleet.npz`、`l1.jsonl`、`l2.jsonl`、`contacts.jsonl`，
`evaluation.json` 的 `inputs.truth_sha256` 也只覆盖这四个文件（存在者，按文件名排序，`名称 ‖ 0x00 ‖ 内容`）。它们是
`awr.bench.retention.TRUTH_MIN_FILES` 的子集：排名运行精简（compaction，删除 `truth/run_<id>.mcap` 等）后由保留文件复评逐字节一致
（B33 遗留 3）；`run_seed.json`（种子明文）不读取、不进入摘要。

计分事件集（`scored_event_set`）：plan.json 的注释（`annotations`）之外，加入事件真值 `key_events[]` 中类型属于任务契约
`scored_event_types`、且没有同类型注释的类型化关键事件（目标搜索的 `target_window_k`：每个 60 s 窗口一个计分事件，参与者为目标，
M23 §15.3 第 4 行）。关键事件的不完整区域取其所属真值事件（`ref`）的 `in_incomplete_region`，区间取 `interval_ms`；为与注释共用
`ec.exclusions` 与 `ao.truth_intervals`，以合成真值行 `{annotation: <关键事件 id>, interval_ms, in_incomplete_region}` 给出。
注释类型的事件（交汇与交接、Re-ID、车辆通过）不受影响：其任务契约的计分类型只来自注释，关键事件不进入计分集，评测结果逐字节不变。

EU 的目标搜索口径（`eu_target_set`，AWR-05 §5.2.8 规则 5、M23 §15.3 规则 4、M27-FR-176）：`target_search` 事件的 EU 真值为事件真值 `events[]`
中按 `report_map` 映射的事件（`found` → `target_present`，每个在场区间一个），report 须定位误差 ≤ `eu_found_m`（5 m，水平距离，
目标位置取磁带在 report 窗口末时刻（截断到该在场区间内）的位置）且证据关联到目标；EC 与 AO 仍按 60 s 窗口计。其他事件类型的
EU 事件集与类型映射不变（仍为注释与 `agentsdk.task.EVENT_OF`）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["RUN_TRUTH_FILES", "SEARCH_EVENT_TYPE", "TruthAccess", "eu_target_set", "event_truth", "event_truth_sha256", "scored_event_set",
           "truth_digest"]

# 评测器读取的运行真值文件（`retention.TRUTH_MIN_FILES` 的子集；`inputs.truth_sha256` 只覆盖这些文件）
RUN_TRUTH_FILES = ("contacts.jsonl", "fleet.npz", "l1.jsonl", "l2.jsonl")
SEARCH_EVENT_TYPE = "target_search"


@dataclass
class TruthAccess:
    """评测真值的读取入口：`hs` 为 None 时直接读文件；否则经 HiddenStore 授权并写 `truth_read` 审计（见模块说明）。"""

    hs: object | None = None
    event_id: str | None = None
    run_id: str | None = None
    job: str | None = None
    principal: object | None = None
    reads: list[str] = field(default_factory=list)            # 本次评测读取过的真值路径（审计核对与测试用）

    @classmethod
    def hidden(cls, hs, *, event_id: str, run_id: str | None = None, job: str | None = None, principal=None) -> TruthAccess:
        from awr.bench.split import SERVICE_EVALUATOR

        return cls(hs, event_id, run_id, job, principal or SERVICE_EVALUATOR)

    def read_bytes(self, path: Path) -> bytes:
        """读取一个真值文件（经 HiddenStore 时每次一条审计）；不存在抛 FileNotFoundError。"""
        self.reads.append(str(path))
        if self.hs is None:
            return Path(path).read_bytes()
        return self.hs.read_bytes(self.principal, Path(path), event_id=str(self.event_id), run_id=self.run_id, job=self.job)

    def authorize_dir(self, path: Path) -> None:
        """打开整个目录（磁带、plan.json、锚点）前的授权点：经 HiddenStore 时写一条 op `evaluate` 的审计，越权 691。"""
        if self.hs is not None:
            self.hs.authorize(self.principal, Path(path), event_id=str(self.event_id), run_id=self.run_id, job=self.job, op="evaluate")


def event_truth(event_dir: Path, access: TruthAccess | None = None) -> dict | None:
    """事件版本目录最新修订的事件真值文档（`truth/r<n>/event_truth.json`）；无真值时为 None（例如只取得公开层的事件目录）。"""
    access = access or TruthAccess()
    revs = sorted(int(p.name[1:]) for p in (Path(event_dir) / "truth").glob("r*") if p.name[1:].isdigit())
    for rev in reversed(revs):
        p = Path(event_dir) / "truth" / f"r{rev}" / "event_truth.json"
        if p.is_file():
            return json.loads(access.read_bytes(p).decode("utf-8"))
    return None


def truth_digest(blobs: dict[str, bytes]) -> str:
    """运行真值摘要：`RUN_TRUTH_FILES` 中存在者按文件名排序，逐个 `名称 ‖ 0x00 ‖ 内容` 的 SHA-256。"""
    h = hashlib.sha256()
    for name in sorted(blobs):
        h.update(name.encode() + b"\0" + blobs[name])
    return h.hexdigest()


def scored_event_set(annotations: list[dict], truth: dict | None, scored_types: list[str]) -> tuple[list[dict], list[dict]]:
    """→ (计分事件，合成真值行)。`annotations` 为 `agentsdk.task.scored_events` 的注释事件（已按 `scored_types` 过滤）；
    关键事件按真值中的次序追加在注释之后（规则见模块说明）。没有关键事件进入时合成真值行为空。"""
    have = {e["type"] for e in annotations}
    want = set(scored_types) - have
    if not truth or not want:
        return list(annotations), []
    inc = {str(r["id"]): bool(r.get("in_incomplete_region")) for r in truth.get("events", [])}
    keys, rows = [], []
    for k in truth.get("key_events", []):
        if k.get("type") not in want:
            continue
        a, b = (float(x) / 1000.0 for x in k["interval_ms"])
        keys.append({"id": str(k["id"]), "type": str(k["type"]), "participants": list(k["participants"]), "a_s": a, "b_s": b,
                     "multi": len(k["participants"]) > 1, "params": dict(k.get("params") or {}), "key": True})
        rows.append({"annotation": str(k["id"]), "interval_ms": list(k["interval_ms"]),
                     "in_incomplete_region": inc.get(str(k.get("ref")), False)})
    return [*annotations, *keys], rows


def event_truth_sha256(annotations_raw: list[dict], keys: list[dict]) -> str:
    """`inputs.event_truth_sha256`：只有注释计分时为注释（plan.json `annotations`）的规范 JSON 摘要（与评测器 0.2.0 相同）；
    有关键事件进入计分集时为 `{annotations, key_events}`（关键事件取计分用的 id、type、participants、区间）的规范 JSON 摘要。"""
    doc: object = annotations_raw
    if keys:
        doc = {"annotations": annotations_raw,
               "key_events": [{"id": k["id"], "type": k["type"], "participants": k["participants"], "a_s": k["a_s"], "b_s": k["b_s"]}
                              for k in keys]}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def eu_target_set(task: dict, truth: dict | None) -> tuple[list[dict], dict[str, list[str]]] | None:
    """目标搜索事件的 EU 真值与 report 类型映射 → (EU 事件, {report 类型: [真值事件类型]})；其他事件类型或缺真值、缺
    `report_map` 时为 None（EU 照旧以计分事件集与 `EVENT_OF` 匹配）。"""
    if task.get("event_type") != SEARCH_EVENT_TYPE or not truth or not truth.get("report_map"):
        return None
    rmap = {str(r): [str(t) for t in ts] for r, ts in truth["report_map"].items() if r in set(task.get("report_types") or [])}
    types = {t for ts in rmap.values() for t in ts}
    rows = [*truth.get("events", []), *truth.get("key_events", [])]
    E = [{"id": str(r["id"]), "type": str(r["type"]), "participants": list(r["participants"]), "a_s": float(r["interval_ms"][0]) / 1000.0,
          "b_s": float(r["interval_ms"][1]) / 1000.0, "multi": len(r["participants"]) > 1, "params": dict(r.get("params") or {})}
         for r in rows if r.get("type") in types]
    return E, rmap
