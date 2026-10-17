"""事件 DSL `drone4d.event.v1`（M23 §6.2）：解析、规范化与门 1 静态校验（§6.4 错误目录）。

规范化：时刻换算为整数毫秒（`floor(t·1000 + 0.5)`），补全缺省值（§6.16），类别模板写入尺寸、步态；结果为 `plan`（纯 JSON），
后续门 2、生成与哈希只读取 `plan`。`event_sha256` 按 §6.7.5 的磁带输入子集求 JCS 哈希；`dsl_sha256` 覆盖规范化后的完整 DSL。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from awr.sim.events.jcs import jcs

__all__ = ["CLASS_DEFAULTS", "LIMITS", "Issue", "event_sha256", "gate1", "load_doc", "ms", "normalize"]

LIMITS = {"actors": 30, "background": 1000, "route": 64, "annotations": 256, "relations": 512, "zones": 32, "keyframes": 256,
          "duration_s": 3600.0, "dsl_bytes": 1 << 20}
# l × w × h（m）、NavMesh profile、步态 [lo, hi]、加速度、中值速度（§6.2.3、§6.16）
CLASS_DEFAULTS: dict[str, dict[str, Any]] = {
    "person": {"size": (0.5, 0.3, 1.75), "profile": "person", "speed": (0.9, 1.8), "accel": 1.0, "median": 1.35, "asset": "person-proxy@1"},
    "vehicle": {"size": (4.5, 1.8, 1.5), "profile": "vehicle", "speed": (2.0, 11.0), "accel": 2.5, "median": 6.6, "asset": "vehicle-proxy@1"},
    "dog": {"size": (0.9, 0.3, 0.6), "profile": "person", "speed": (0.8, 3.0), "accel": 1.5, "median": 1.4, "asset": "dog-proxy@1"},
    "machine": {"size": (1.0, 0.7, 0.8), "profile": None, "speed": None, "accel": None, "median": None, "asset": "machine-proxy@1"},
    "object": {"size": (0.6, 0.4, 0.4), "profile": None, "speed": None, "accel": None, "median": None, "asset": "object-proxy@1"},
}
P0A_CLASSES = frozenset(CLASS_DEFAULTS)
STATIC_CLASSES = frozenset({"machine", "object"})
ACTIONS_BY_CLASS = {"person": {"spawn", "pass", "wait", "enter_building", "exit_building", "board", "alight", "despawn"},
                    "vehicle": {"spawn", "pass", "wait", "despawn"}, "dog": {"spawn", "pass", "wait", "despawn"},
                    "machine": {"spawn", "despawn"}, "object": {"spawn", "despawn"}}
ENV_DEFAULTS = {"wind_mps": 3.0, "wind_dir_deg": 270.0, "gust_sigma_mps": 0.5, "rain_mm_h": 0.0, "visibility_m": 30000.0, "fog": 0.0,
                "cloud": 0.05}
BG_DEFAULTS = {"person": {"speed_mps": [0.8, 1.6]}, "vehicle": {"speed_mps": [4.0, 11.0]}, "dog": {"speed_mps": [0.8, 3.0]}}


@dataclass(frozen=True)
class Issue:
    """§7.1 Issue；`reason` 为原因码（告警为 None），`path` 为 JSON 指针。"""
    code: str
    reason: int | None
    path: str
    message_zh: str
    message_en: str
    detail: dict = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.code[0] in "EIS"

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason, "path": self.path, "message_zh": self.message_zh, "message_en": self.message_en,
                "detail": self.detail}


def ms(t_s: float) -> int:
    """秒 → 整数毫秒（§6.6.6：`floor(v·k + 0.5)`，不用 round）。"""
    return math.floor(float(t_s) * 1000.0 + 0.5)


def load_doc(src: Path | str | dict) -> dict:
    """事件文档：路径（.yaml、.yml、.json）、文本或已解析对象。"""
    import yaml

    if isinstance(src, dict):
        return src
    text = Path(src).read_text(encoding="utf-8") if isinstance(src, Path) or (isinstance(src, str) and "\n" not in src
                                                                              and Path(src).exists()) else str(src)
    if len(text.encode("utf-8")) > LIMITS["dsl_bytes"]:
        raise ValueError("event DSL exceeds 1 MiB")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError("event DSL is not a mapping")
    return json.loads(json.dumps(doc))                          # YAML 日期等非 JSON 类型在此暴露


def _schema_issues(doc: dict) -> list[Issue]:
    import jsonschema

    from awr.contracts._paths import schema_path
    from awr.sim.schemas import contracts_registry

    sch = json.loads(schema_path("event/event.v1.schema.json").read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(sch, registry=contracts_registry())
    out = []
    for e in sorted(v.iter_errors(doc), key=lambda e: (list(map(str, e.absolute_path)), e.message)):
        ptr = "/" + "/".join(str(p) for p in e.absolute_path)
        out.append(Issue("E-SCHEMA", 611, ptr, f"不符合 drone4d.event.v1：{e.message}", f"Does not match drone4d.event.v1: {e.message}"))
    return out


def normalize(doc: dict) -> dict:
    """规范化（假定已通过 schema）：缺省值、毫秒时刻、类别模板。"""
    dur = ms(doc["duration_s"])
    actors = []
    for a in doc["actors"]:
        c = CLASS_DEFAULTS.get(a["class"], CLASS_DEFAULTS["object"])
        ap = dict(a.get("appearance") or {})
        size = list(c["size"])
        if "height_m" in ap:
            size[2] = float(ap["height_m"])
        if "length_m" in ap:
            size[0] = float(ap["length_m"])
        gait = a.get("gait") or {}
        sp = gait.get("speed_mps", c["speed"])
        actors.append({
            "id": a["id"], "class": a["class"], "profile": c["profile"],
            "size_mm": [ms(s) for s in size],
            "appearance": {"asset": ap.get("asset") or ap.get("preset") or c["asset"],
                           **{k: list(ap[k]) for k in ("top_rgb", "bottom_rgb", "body_rgb") if k in ap},
                           **({"height_mm": ms(size[2])} if a["class"] in ("person", "dog") else {}),
                           **({"accessories": list(ap["accessories"])} if ap.get("accessories") else {})},
            "gait": None if sp is None else {"speed_mps": [float(sp[0]), float(sp[1])], "accel_mps2": float(gait.get("accel_mps2", c["accel"])),
                                             "median_mps": float(c["median"] if c["median"] else (sp[0] + sp[1]) / 2)},
            "route": [{"t_ms": ms(w["t"]), "at": w["at"], "do": w.get("do", "pass"), "dwell_ms": ms(w.get("dwell_s", 0)),
                       "via": list(w.get("via", [])), "target": w.get("target"), "tol_ms": ms(w.get("tol_s", 3))} for w in a["route"]],
        })
    bg = []
    for i, g in enumerate(doc.get("background", [])):
        bg.append({"class": g["class"], "count": int(g["count"]), "zone": g["zone"],
                   "speed_mps": [float(x) for x in g.get("speed_mps", BG_DEFAULTS[g["class"]]["speed_mps"])],
                   "pause_ms": [ms(x) for x in g.get("pause_s", [0, 20])], "stream": int(g.get("stream", i)),
                   "distractor_frac": float((g.get("appearance") or {}).get("distractor_frac", 0.1))})
    env = doc.get("environment") or {}
    kfs, prev, authored = [], dict(ENV_DEFAULTS), False
    for k in env.get("keyframes", [{"t": 0}]):
        cur = {**prev, **{n: float(v) for n, v in k.items() if n != "t"}}
        authored = authored or "cloud" in k
        if "cloud" not in k and not authored:                    # 未给出云量：有雨补 0.9（§6.11，W-ENV-CLOUD 在门 1 报告），无雨取缺省
            cur["cloud"] = 0.9 if cur["rain_mm_h"] > 0 else ENV_DEFAULTS["cloud"]
        kfs.append({"t_ms": ms(k["t"]), **cur})
        prev = cur
    zones = [{"id": z["id"], "polygon": [[float(x), float(y)] for x, y in z["polygon"]],
              "t_ms": [ms(t) for t in z.get("t", [0, doc["duration_s"]])], "extra_loss": float(z.get("extra_loss", 0.0)),
              "extra_latency_ms": float(z.get("extra_latency_ms", 0.0)), "bandwidth_scale": float(z.get("bandwidth_scale", 1.0))}
             for z in (doc.get("comms") or {}).get("zones", [])]
    return {
        "schema": "drone4d.plan.v1", "event_id": doc["event_id"], "version": int(doc.get("version", 1)),
        "event_type": doc.get("event_type", "rendezvous_handoff"), "world": dict(doc["world"]), "anchors_ref": doc.get("anchors_ref", "world"),
        "seed": int(doc["seed"]), "duration_ms": dur, "tick_hz": int(doc.get("tick_hz", 10)), "mode": doc.get("mode", "exogenous"),
        "time_origin_utc": doc.get("time_origin_utc", "2026-06-15T09:00:00Z"), "actors": actors, "background": bg,
        "environment": {"interpolation": env.get("interpolation", "smooth"), "keyframes": kfs}, "comms": {"zones": zones},
        "annotations": list(doc.get("annotations", [])), "relations": list(doc.get("relations", [])), "brief": doc.get("brief"),
        "interactive": doc.get("interactive", {}), "labels": doc.get("labels", {}),
    }


def event_sha256(plan: dict, anchors_sha256: str) -> str:
    """§6.7.5 磁带输入子集的 JCS 哈希。"""
    sub = {"world_id": plan["world"]["id"], "anchors_sha256": anchors_sha256, "seed": plan["seed"], "duration_ms": plan["duration_ms"],
           "tick_hz": plan["tick_hz"], "mode": plan["mode"], "actors": plan["actors"], "background": plan["background"],
           "environment": plan["environment"], "comms": plan["comms"], "interactive": plan["interactive"]}
    return hashlib.sha256(jcs(sub)).hexdigest()


def _e(code: str, reason: int | None, path: str, zh: str, en: str, **detail) -> Issue:
    return Issue(code, reason, path, zh, en, detail)


def gate1(doc: dict, anchors: dict[str, dict] | None) -> tuple[dict | None, list[Issue], list[Issue]]:
    """门 1：schema、id、引用、时间、类别与动作、上车目标、限额、模式。`anchors` 为解析后的锚点表（id → 锚点；None 表示
    世界无锚点层且无覆盖层，锚点引用一律报未知）。返回 (plan 或 None, errors, warnings)。"""
    errs = _schema_issues(doc)
    if errs:
        return None, errs, []
    warns: list[Issue] = []
    anchors = anchors or {}
    dur_s = float(doc["duration_s"])
    if dur_s > LIMITS["duration_s"]:
        errs.append(_e("E-LIMIT", 618, "/duration_s", "事件时长超过 3600 s", "Event duration exceeds 3600 s"))
    if ms(dur_s) % 100:
        errs.append(_e("E-TIME-ORDER", 611, "/duration_s", "时长须为 0.1 s 的整数倍", "duration_s must be a multiple of 0.1 s"))
    if doc.get("tick_hz", 10) != 10:
        errs.append(_e("E-CLASS-UNSUPPORTED", 619, "/tick_hz", "当前只支持 10 Hz 磁带", "Only 10 Hz tapes are supported"))
    if doc.get("mode", "exogenous") == "interactive":
        errs.append(_e("E-MODE", 619, "/mode", "Interactive 模式在 P2 提供", "Interactive mode is available from P2"))
    acts = doc["actors"]
    if len(acts) > LIMITS["actors"]:
        errs.append(_e("E-LIMIT", 618, "/actors", "脚本演员超过 30 名", "More than 30 scripted actors"))
    n_bg = sum(int(g["count"]) for g in doc.get("background", []))
    if n_bg > LIMITS["background"]:
        errs.append(_e("E-LIMIT", 618, "/background", "背景演员合计超过 1000 名", "More than 1000 background actors in total"))
    for key, lim in (("annotations", "annotations"), ("relations", "relations")):
        if len(doc.get(key, [])) > LIMITS[lim]:
            errs.append(_e("E-LIMIT", 618, f"/{key}", f"{key} 数量超限", f"Too many {key}"))
    if len((doc.get("comms") or {}).get("zones", [])) > LIMITS["zones"]:
        errs.append(_e("E-LIMIT", 618, "/comms/zones", "通信区超过 32 个", "More than 32 comms zones"))
    ids: dict[str, int] = {}
    for i, a in enumerate(acts):
        if a["id"] in ids:
            errs.append(_e("E-DUP-ID", 611, f"/actors/{i}/id", f"演员 id 重复：{a['id']}", f"Duplicate actor id: {a['id']}"))
        ids.setdefault(a["id"], i)
    cls_of = {a["id"]: a["class"] for a in acts}
    for i, a in enumerate(acts):
        base = f"/actors/{i}"
        if a["class"] not in P0A_CLASSES:
            errs.append(_e("E-CLASS-UNSUPPORTED", 619, f"{base}/class", f"类别 {a['class']} 在当前阶段不支持",
                           f"Class {a['class']} is not supported in this phase"))
            continue
        route = a["route"]
        if len(route) > LIMITS["route"]:
            errs.append(_e("E-LIMIT", 618, f"{base}/route", "路点超过 64 个", "More than 64 waypoints"))
        prof = CLASS_DEFAULTS[a["class"]]["profile"]
        last_t = -1.0
        for j, w in enumerate(route):
            p = f"{base}/route/{j}"
            do = w.get("do", "pass")
            if do not in ACTIONS_BY_CLASS[a["class"]]:
                errs.append(_e("E-CLASS-UNSUPPORTED", 619 if do in ("pickup", "drop") else 611, f"{p}/do",
                               f"类别 {a['class']} 不支持动作 {do}", f"Class {a['class']} does not support action {do}"))
            if w["t"] > dur_s:
                errs.append(_e("E-TIME-ORDER", 611, f"{p}/t", "路点时刻超出事件时长", "Waypoint time exceeds the event duration"))
            if w["t"] <= last_t:
                errs.append(_e("E-TIME-ORDER", 611, f"{p}/t", "路点时刻须严格递增", "Waypoint times must be strictly increasing"))
            last_t = float(w["t"])
            for k, ref in [("at", w["at"]), *[(f"via/{m}", x) for m, x in enumerate(w.get("via", []))]]:
                an = anchors.get(ref)
                if an is None:
                    errs.append(_e("E-ANCHOR-UNKNOWN", 611, f"{p}/{k}", f"未知锚点：{ref}", f"Unknown anchor: {ref}", anchor=ref))
                elif prof and an.get("kind") != "zone" and an.get("profile", "person") != prof:
                    errs.append(_e("E-CLASS-PROFILE", 611, f"{p}/{k}", f"锚点 {ref} 的 profile 与类别 {a['class']} 不符",
                                   f"Anchor {ref} profile does not match class {a['class']}", anchor=ref))
            if do in ("enter_building", "exit_building") and (an := anchors.get(w["at"])) and an.get("kind") != "door":
                errs.append(_e("E-FIRST-WAYPOINT", 611, f"{p}/at", "进出建筑须使用门锚点", "Building entry and exit require a door anchor"))
            if do in ("board", "alight"):
                tgt = w.get("target")
                if cls_of.get(tgt) != "vehicle":
                    errs.append(_e("E-BOARD-TARGET", 611, f"{p}/target", "上下车目标须为车辆演员", "Board/alight target must be a vehicle actor"))
                else:
                    vr = acts[ids[tgt]]["route"]
                    cover = any(v.get("do", "pass") == "wait" or v.get("dwell_s", 0) > 0 for v in vr)
                    if not cover:
                        errs.append(_e("E-BOARD-TARGET", 611, f"{p}/target", f"车辆 {tgt} 没有等待路点", f"Vehicle {tgt} has no waiting waypoint"))
        if route:
            first = route[0].get("do", "pass")
            if first not in ("spawn", "exit_building"):
                errs.append(_e("E-FIRST-WAYPOINT", 611, f"{base}/route/0/do", "首个路点须为 spawn 或 exit_building",
                               "The first waypoint must be spawn or exit_building"))
            for j, w in enumerate(route[:-1]):
                if w.get("do") in ("despawn",) or (w.get("do") == "enter_building" and route[j + 1].get("do") != "exit_building"):
                    errs.append(_e("E-FIRST-WAYPOINT", 611, f"{base}/route/{j + 1}", "despawn 或 enter_building 之后仍有路点",
                                   "Waypoints follow despawn or enter_building"))
    for gi, g in enumerate(doc.get("background", [])):
        if g["class"] == "dog":
            errs.append(_e("E-CLASS-UNSUPPORTED", 619, f"/background/{gi}/class", "背景狗在 P1 提供", "Background dogs are available from P1"))
        z = g["zone"]
        if isinstance(z, str) and (z not in anchors or anchors[z].get("kind") != "zone"):
            errs.append(_e("E-ANCHOR-UNKNOWN", 611, f"/background/{gi}/zone", f"未知 zone 锚点：{z}", f"Unknown zone anchor: {z}", anchor=z))
    env = doc.get("environment") or {}
    kf = env.get("keyframes", [])
    if kf and kf[0]["t"] != 0:
        errs.append(_e("E-TIME-ORDER", 611, "/environment/keyframes/0/t", "首个关键帧须在 t = 0", "The first keyframe must be at t = 0"))
    for j in range(1, len(kf)):
        if kf[j]["t"] <= kf[j - 1]["t"] or kf[j]["t"] > dur_s:
            errs.append(_e("E-TIME-ORDER", 611, f"/environment/keyframes/{j}/t", "关键帧时刻须严格递增且不超出时长",
                           "Keyframe times must be strictly increasing within the duration"))
    if len(kf) > LIMITS["keyframes"]:
        errs.append(_e("E-LIMIT", 618, "/environment/keyframes", "关键帧超过 256 个", "More than 256 keyframes"))
    cloud_given = any("cloud" in k for k in kf)
    if any(k.get("rain_mm_h", 0) > 0 for k in kf):
        if not cloud_given:
            warns.append(_e("W-ENV-CLOUD", None, "/environment", "有降雨而未给出云量，已补为 0.9", "Rain without cloud cover: cover set to 0.9"))
        elif any(k.get("rain_mm_h", 0) > 0 and k.get("cloud", 1.0) < 0.85 for k in kf):
            warns.append(_e("W-ENV-CLOUD", None, "/environment", "有降雨而云量 < 0.85，M07 将抑制降雨", "Rain with cloud cover below 0.85"))
    ann_ids = set()
    for j, an in enumerate(doc.get("annotations", [])):
        if an["id"] in ann_ids:
            errs.append(_e("E-DUP-ID", 611, f"/annotations/{j}/id", f"注释 id 重复：{an['id']}", f"Duplicate annotation id: {an['id']}"))
        ann_ids.add(an["id"])
        for m, pid in enumerate(an.get("participants", [])):
            if pid not in ids and pid not in anchors:
                errs.append(_e("E-PARTICIPANT-UNKNOWN", 611, f"/annotations/{j}/participants/{m}", f"未知参与者：{pid}", f"Unknown participant: {pid}"))
        for pk in ("anchor", "zone"):
            ref = (an.get("params") or {}).get(pk)
            if isinstance(ref, str) and ref not in anchors:
                errs.append(_e("E-ANCHOR-UNKNOWN", 611, f"/annotations/{j}/params/{pk}", f"未知锚点：{ref}", f"Unknown anchor: {ref}", anchor=ref))
    for j, r in enumerate(doc.get("relations", [])):
        for side in ("src", "dst"):
            if r[side] not in ann_ids:
                errs.append(_e("E-RELATION-REF", 611, f"/relations/{j}/{side}", f"关系引用未知注释：{r[side]}", f"Relation references unknown annotation: {r[side]}"))
    if errs:
        return None, errs, warns
    return normalize(doc), errs, warns
