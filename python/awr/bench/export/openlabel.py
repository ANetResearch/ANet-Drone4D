"""ASAM OpenLABEL 1.0 导出（M23 §6.10.2 映射，M27-FR-071；D3-AC-62 交汇与交接条款"关系边由验证器产出并导出 OpenLABEL"）。

输入为事件目录的真值层：`truth/r<n>/event_truth.json`、`truth/r<n>/event_graph.json`（缺失时由 `gen.build_graph` 从真值与磁带头
组装）与 `tape.bin`（磁带头演员表；`poses = True` 时另读逐帧记录）。映射（帧号 = t_ms / 100，10 Hz）：

| 事件图 | OpenLABEL | 说明 |
|---|---|---|
| 实体节点（脚本演员，含 `object` 类物体） | `objects`（uid = 磁带演员 idx） | `type` 为类别；`object_data` 含 `actor_id`、`size_m`；`frame_intervals` 为在场区间（状态 1–4），未读帧时为全程 |
| 区间事件节点（真值事件与关键事件，`interval_ms[0] < interval_ms[1]`） | `actions` | `frame_intervals` 为 [t0/100, t1/100] |
| 瞬时事件节点 | `events` | 单帧区间 |
| `participates_in` 边 | `relations`（`rdf_subjects` 为 object，`rdf_objects` 为 action 或 event） | |
| 关系边（作者关系与验证器 `key_relations`） | `relations`（主语与宾语均为 action 或 event） | `type` 为 before、during、causes 等 |

事件属性写入 `action_data` / `event_data`：`text` 含 `event_id`、`layer`（`truth` 为 `events[]`，`key` 为 `key_events[]`）、
`annotation`、`ref` 与字符串证据（物体、交出者、接收者、门、zone、所属 meet）；`num` 含 `salience` 与数值证据；`boolean` 含
`declared` 与 `in_incomplete_region`（§6.10.2：`declared` 与 `salience` 写入属性）。`poses = True` 时 `frames` 按 `stride` 帧写入可见
（状态 1、2）脚本演员的 `cuboid`（world-enu 帧，(x, y, z + h/2, 0, 0, yaw, l, w, h)）。uid 按 idx 与节点次序编号，同一输入两次
导出逐字节相同。

导出物含真值，只放在真值层（缺省 `truth/r<n>/<event_id>.openlabel.json`），访问控制与 `event_truth.json` 相同（test 划分的真值
不发布，M27 §5.6.6）。`check()` 为参考解析器：按 OpenLABEL 1.0 JSON Schema 的结构约束（uid 键、必填字段、帧区间、RDF 引用）
与事件图计数逐项核对；官方 schema 的校验见 `tests/d3/events/test_types_rendezvous.py`（`AWR_OPENLABEL_SCHEMA`）。

命令行：`python -m awr.bench.export.openlabel <event_dir> [--truth-rev N] [--out FILE] [--poses] [--stride K]`。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

__all__ = ["EXPORTER", "SCHEMA_VERSION", "check", "export_event", "main", "to_openlabel"]

SCHEMA_VERSION = "1.0.0"
EXPORTER = "awr.bench.export.openlabel@1"
TICK_MS = 100
UID = re.compile(r"^(-?[0-9]+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
RDF_TYPES = ("object", "action", "event", "context")
_TEXT_KEYS = ("object", "giver", "receiver", "meet", "door", "vehicle", "zone")
_NUM_KEYS = ("min_dist_m", "dist_m", "gap_ms", "sep_m", "after_s", "door_dist_m", "sky_vis_min")


def _frame(t_ms: int) -> int:
    return int(t_ms) // TICK_MS


def _intervals(mask: np.ndarray) -> list[dict]:
    m = np.asarray(mask, bool)
    if not m.any():
        return []
    d = np.diff(np.concatenate([[0], m.view(np.int8), [0]]))
    return [{"frame_start": int(a), "frame_end": int(b) - 1} for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1), strict=True)]


def _element_data(row: dict, layer: str) -> dict:
    text = [{"name": "event_id", "val": row["id"]}, {"name": "layer", "val": layer}]
    num, boolean = [], [{"name": "declared", "val": bool(row.get("declared", False))}]
    text += [{"name": k, "val": str(row[k])} for k in ("annotation", "ref") if row.get(k)]
    if "salience" in row:
        num.append({"name": "salience", "val": float(row["salience"])})
    if "in_incomplete_region" in row:
        boolean.append({"name": "in_incomplete_region", "val": bool(row["in_incomplete_region"])})
    extra = {**(row.get("evidence") or {}), **(row.get("params") or {})}
    text += [{"name": k, "val": str(extra[k])} for k in _TEXT_KEYS if isinstance(extra.get(k), str)]
    num += [{"name": k, "val": float(extra[k])} for k in _NUM_KEYS if isinstance(extra.get(k), (int, float)) and not isinstance(extra[k], bool)
            and math.isfinite(float(extra[k]))]
    return {"text": text, "num": num, "boolean": boolean}


def to_openlabel(graph: dict, truth: dict, header: dict, *, frames: np.ndarray | None = None, poses: bool = False, stride: int = 10,
                 truth_sha256: str | None = None) -> dict:
    """事件图 + 真值 + 磁带头 → OpenLABEL 1.0 JSON（见模块说明）。`frames` 为 tick 主序 RECORD（只用脚本演员列），给出时对象的
    `frame_intervals` 取在场区间；`poses` 需要 `frames`。"""
    n_t = int(header["n_ticks"])
    acts = {a["id"]: a for a in header["actors"]}
    rows = {e["id"]: (e, "truth") for e in truth.get("events", [])}
    rows.update({k["id"]: (k, "key") for k in truth.get("key_events", [])})
    objects, uid_of = {}, {}
    for n in graph["nodes"]:
        if n["kind"] != "entity":
            continue
        a = acts[n["id"]]
        uid = str(a["idx"])
        iv = [{"frame_start": 0, "frame_end": n_t - 1}]
        if frames is not None:
            iv = _intervals(np.isin(frames["state"][:, a["idx"]], (1, 2, 3, 4)))
        objects[uid] = {"name": n["id"], "type": n["class"], "frame_intervals": iv,
                        "object_data": {"text": [{"name": "actor_id", "val": n["id"]}],
                                        "boolean": [{"name": "scripted", "val": bool(n["scripted"])}],
                                        "vec": [{"name": "size_m", "val": [s / 1000.0 for s in a.get("size_mm", [])]}]}}
        uid_of[n["id"]] = ("object", uid)
    actions, events = {}, {}
    for n in graph["nodes"]:
        if n["kind"] != "event":
            continue
        a, b = n["interval_ms"]
        row, layer = rows.get(n["id"], ({"id": n["id"], "declared": n["declared"]}, "truth"))
        el = {"name": n["id"], "type": n["type"], "frame_intervals": [{"frame_start": _frame(a), "frame_end": _frame(b)}]}
        if a < b:
            uid = str(len(actions))
            actions[uid] = {**el, "action_data": _element_data(row, layer)}
            uid_of[n["id"]] = ("action", uid)
        else:
            uid = str(len(events))
            events[uid] = {**el, "event_data": _element_data(row, layer)}
            uid_of[n["id"]] = ("event", uid)
    relations = {}
    for e in graph["edges"]:
        s, o = uid_of.get(e["src"]), uid_of.get(e["dst"])
        if s is None or o is None:
            continue
        relations[str(len(relations))] = {"name": f"{e['src']} {e['rel']} {e['dst']}", "type": e["rel"],
                                          "rdf_subjects": [{"type": s[0], "uid": s[1]}], "rdf_objects": [{"type": o[0], "uid": o[1]}]}
    meta = {"schema_version": SCHEMA_VERSION, "name": truth["event_id"], "file_version": f"v{truth['event_version']}-r{truth['truth_rev']}",
            "annotator": "ANet4D gate 3 semantic verifier (awr.sim.events)", "tagged_file": "tape.bin",
            "comment": "Ground truth of an ANet4D event (truth layer); frame k is t = k x 100 ms from the tape start",
            "exporter": EXPORTER, "frame_rate_hz": 10, "event_type": truth.get("event_type") or header.get("event_type"),
            "tape_content_sha256": truth["tape_content_sha256"], **({"truth_sha256": truth_sha256} if truth_sha256 else {})}
    meta = {k: v for k, v in meta.items() if v is not None}
    doc = {"metadata": meta, "coordinate_systems": {"world": {"type": "scene_cs", "parent": "", "children": []}},
           "frame_intervals": [{"frame_start": 0, "frame_end": n_t - 1}], "objects": objects, "actions": actions, "events": events,
           "relations": relations}
    if poses:
        if frames is None:
            raise ValueError("poses require tape frames")
        doc["frames"] = _frames(frames, header, objects, max(1, int(stride)))
    return {"openlabel": doc}


def _frames(frames: np.ndarray, header: dict, objects: dict, stride: int) -> dict:
    out = {}
    idx = sorted(int(u) for u in objects)
    size = {a["idx"]: [s / 1000.0 for s in a["size_mm"]] for a in header["actors"]}
    for k in range(0, len(frames), stride):
        r = frames[k]
        objs = {}
        for i in idx:
            if int(r["state"][i]) not in (1, 2):
                continue
            sx, sy, sz = size[i]
            objs[str(i)] = {"object_data": {"cuboid": [{"name": "box3d", "coordinate_system": "world", "val": [
                int(r["x_mm"][i]) / 1000.0, int(r["y_mm"][i]) / 1000.0, round(int(r["z_mm"][i]) / 1000.0 + sz / 2.0, 4), 0.0, 0.0,
                round(math.radians(int(r["yaw_cdeg"][i]) / 100.0), 6), sx, sy, sz]}]}}
        out[str(k)] = {"frame_properties": {"timestamp": k * TICK_MS / 1000.0}, "objects": objs}
    return out


def _check_intervals(where: str, ivs, n: int, instant: bool, errs: list[str]) -> None:
    if not isinstance(ivs, list):
        errs.append(f"{where}: frame_intervals is not an array")
        return
    for iv in ivs:
        a, b = iv.get("frame_start"), iv.get("frame_end")
        if not (isinstance(a, int) and isinstance(b, int) and 0 <= a <= b <= n - 1):
            errs.append(f"{where}: bad frame interval {iv}")
        elif instant and a != b:
            errs.append(f"{where}: event interval is not instantaneous {iv}")


def check(doc: dict, graph: dict | None = None) -> list[str]:
    """参考解析器（见模块说明）：返回问题列表，空表为通过。"""
    errs: list[str] = []
    ol = doc.get("openlabel")
    if not isinstance(ol, dict) or set(doc) != {"openlabel"}:
        return ["root must be {openlabel: {...}}"]
    if (ol.get("metadata") or {}).get("schema_version") != SCHEMA_VERSION:
        errs.append("metadata.schema_version must be 1.0.0")
    fi = ol.get("frame_intervals") or [{"frame_start": 0, "frame_end": 0}]
    n = max(iv["frame_end"] for iv in fi) + 1
    maps = {t: ol.get(t + "s", {}) for t in ("object", "action", "event", "context")}
    for t, m in maps.items():
        for uid, el in m.items():
            where = f"{t}s/{uid}"
            if not UID.match(uid):
                errs.append(f"{where}: uid is not numeric or a UUID")
            if not isinstance(el.get("name"), str) or not isinstance(el.get("type"), str):
                errs.append(f"{where}: name and type are required strings")
            if "frame_intervals" in el:
                _check_intervals(where, el["frame_intervals"], n, t == "event", errs)
            for dk in (f"{t}_data",):
                for kind, items in (el.get(dk) or {}).items():
                    if not all(isinstance(x, dict) and "val" in x for x in items):
                        errs.append(f"{where}: {dk}.{kind} entries require val")
    for uid, r in (ol.get("relations") or {}).items():
        where = f"relations/{uid}"
        if not UID.match(uid) or not isinstance(r.get("type"), str):
            errs.append(f"{where}: bad uid or type")
        for side in ("rdf_subjects", "rdf_objects"):
            ag = r.get(side) or []
            if not ag:
                errs.append(f"{where}: {side} is empty")
            errs += [f"{where}: {side} references unknown {x}" for x in ag
                     if x.get("type") not in RDF_TYPES or x.get("uid") not in maps.get(x.get("type"), {})]
    for fk, fr in (ol.get("frames") or {}).items():
        if not fk.isdigit() or int(fk) >= n:
            errs.append(f"frames/{fk}: bad frame number")
        for uid, od in (fr.get("objects") or {}).items():
            if uid not in maps["object"]:
                errs.append(f"frames/{fk}: unknown object {uid}")
            errs += [f"frames/{fk}/objects/{uid}: cuboid requires name and 9 or 10 values" for c in (od.get("object_data") or {}).get("cuboid", [])
                     if not (isinstance(c.get("name"), str) and isinstance(c.get("val"), list) and len(c["val"]) in (9, 10))]
    if graph is not None:
        n_ev = sum(1 for x in graph["nodes"] if x["kind"] == "event")
        n_en = sum(1 for x in graph["nodes"] if x["kind"] == "entity")
        if len(maps["action"]) + len(maps["event"]) != n_ev:
            errs.append(f"actions + events = {len(maps['action']) + len(maps['event'])}, graph has {n_ev} event nodes")
        if len(maps["object"]) != n_en:
            errs.append(f"objects = {len(maps['object'])}, graph has {n_en} entity nodes")
        if len(ol.get("relations") or {}) != len(graph["edges"]):
            errs.append(f"relations = {len(ol.get('relations') or {})}, graph has {len(graph['edges'])} edges")
    return errs


def _latest_rev(event_dir: Path) -> int:
    revs = sorted(int(p.name[1:]) for p in (event_dir / "truth").glob("r*") if p.name[1:].isdigit())
    if not revs:
        raise FileNotFoundError(f"no truth revision under {event_dir / 'truth'}")
    return revs[-1]


def export_event(event_dir: Path, out: Path | None = None, *, truth_rev: int | None = None, poses: bool = False, stride: int = 10) -> Path:
    """读事件目录（见模块说明）并写出 OpenLABEL；返回输出路径。导出前以 `check()` 自检，不通过时抛 ValueError。"""
    from awr.sim.events.gen import build_graph, truth_sha256
    from awr.sim.events.tape import open_tape

    event_dir = Path(event_dir)
    rev = truth_rev if truth_rev is not None else _latest_rev(event_dir)
    tdir = event_dir / "truth" / f"r{rev}"
    truth = json.loads((tdir / "event_truth.json").read_text(encoding="utf-8"))
    tape = open_tape(event_dir)
    gp = tdir / "event_graph.json"
    graph = json.loads(gp.read_text(encoding="utf-8")) if gp.is_file() else build_graph(truth, tape.header["actors"])
    n_s = tape.n_scripted
    frames = np.concatenate([tape.block_frames(i)[:, :n_s] for i in range(len(tape.blocks))])
    doc = to_openlabel(graph, truth, tape.header, frames=frames, poses=poses, stride=stride, truth_sha256=truth_sha256(truth))
    bad = check(doc, graph)
    if bad:
        raise ValueError("OpenLABEL self-check failed: " + "; ".join(bad[:5]))
    out = Path(out) if out else tdir / f"{truth['event_id']}.openlabel.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.bench.export.openlabel", description="Export event truth as ASAM OpenLABEL 1.0 JSON")
    ap.add_argument("event_dir", type=Path)
    ap.add_argument("--truth-rev", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--poses", action="store_true", help="write per-frame cuboids of visible scripted actors")
    ap.add_argument("--stride", type=int, default=10, help="frame stride for --poses (default 10, i.e. 1 Hz)")
    a = ap.parse_args(argv)
    try:
        p = export_event(a.event_dir, a.out, truth_rev=a.truth_rev, poses=a.poses, stride=a.stride)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
