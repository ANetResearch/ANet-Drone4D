"""外观库 `appearance_lib@1`（AWR-05 §5.2.8、ADR-181；M23 §15.2、§15.3、M23-FR-072、M23-AC-038；M25 §15.7）。

外观库为项目自制的程序化资产（无第三方模型、纹理或扫描数据，许可为项目许可 `LicenseRef-ANet-OSL`）：生成规格
`configs/appearance.d3/spec.yaml` 定义属性、调色板与几何参数，`build_library` 以计数器哈希（`sha256(lib_id|class|seed|k)`）
逐个抽取候选属性向量，按"库内两两属性距离 ≥ min_pair_distance、取值均衡、上下装不同色"接受，确定性生成
`configs/appearance.d3/appearance_lib.v1.json`（`event/appearance_lib.v1.schema.json`；同一规格两次生成逐字节一致）。

- 人物（≥ 24 个）：上装颜色、下装颜色、上装图案、配饰、身高档、体型、步态速度七个类别属性；车辆（≥ 8 个）：车型、车身颜色、
  车顶、车身标识四个类别属性。属性向量为各属性取值在 `classes.<class>.attributes[].values` 中的下标。
- 属性距离：同类别两个外观属性向量的汉明距离（不同取值的属性个数）；共享属性为取值相同的属性。可区分性只用属性向量判定
  （M23 §15.3 规则 3）；离线外观嵌入 `awr-attr-descriptor@1`（CIELAB 颜色、类别独热与尺寸的归一化描述子）只写入编译报告作为
  诊断，不下发 Agent（AWR-05 §5.2.8 规则 2）。
- 门 1（`gate1_issues`，由 `dsl.gate1` 调用）：`actors[].appearance_ref` 须解析到本库且类别一致，否则 611
  `detail.why = appearance_unresolved`（或 `appearance_class`）；按 `rules.<event_type>` 对 `roles` 含 `target` 与 `distractor`
  的演员逐对校验：属性距离 ≥ `min_distance`（Re-ID 为 2）、共享属性 ≥ `min_shared`（难负样本，≥ 1），车辆追踪另要求同车型或
  同色（`shared_any_of`），不满足时 611（`appearance_distance`、`appearance_hard_negative`、`appearance_similarity`）。
- 颜色可区分：同一颜色属性的任意两种取值 CIEDE2000 色差 ≥ `min_delta_e00`（15），由库自检 `library_issues` 校验。

规范化（`plan_fields`，由 `dsl.normalize` 调用）：引用外观的演员在 plan 中的外观为 `{asset: "appearance_lib@1/<id>", top_rgb,
bottom_rgb | body_rgb, height_mm?, accessories?}`，尺寸取外观的 `size_m`，未给出步态时取外观的步态档；M25 按 `asset` 查本库取
完整渲染参数（图案、配饰、车顶、标识、动画片段与热成像材质分区）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from awr.sim.events.dsl import Issue
from awr.sim.events.errors import EventError
from awr.sim.events.jcs import jcs

__all__ = ["EMBED_MODEL", "GENERATOR", "GENERATOR_VERSION", "LIB_ID", "LIB_PATH", "MIN_COUNT", "SCHEMA", "SPEC_PATH", "Appearance",
           "Library", "appearance_report", "build_library", "ciede2000", "describe", "gate1_issues", "lib_bytes", "library_issues",
           "load_library", "load_spec", "plan_fields", "report_for", "resolve", "srgb_to_lab"]

LIB_ID = "appearance_lib@1"
SCHEMA = "drone4d.appearance_lib.v1"
GENERATOR = "awr.sim.events.appearance"
GENERATOR_VERSION = "1.0.0"
EMBED_MODEL = "awr-attr-descriptor@1"
_CFG = Path(__file__).resolve().parents[4] / "configs" / "appearance.d3"
SPEC_PATH = _CFG / "spec.yaml"
LIB_PATH = _CFG / "appearance_lib.v1.json"
MIN_COUNT = {"person": 24, "vehicle": 8}                       # AWR-05 §5.2.8、Q-D3-08：≥ 24 个人物外观、≥ 8 个车辆外观
MAX_CANDIDATES = 200_000
# 外观引用与 `appearance` 段互斥：外观段中的着色、尺寸与配饰会使属性向量与渲染不一致
_OVERRIDE_KEYS = ("preset", "asset", "top_rgb", "bottom_rgb", "body_rgb", "height_m", "length_m", "accessories")


# ---------------------------------------------------------------- 颜色
def srgb_to_lab(rgb) -> tuple[float, float, float]:
    """sRGB 0–255 → CIELAB（D65，2° 观察者）。"""
    lin = []
    for c in rgb:
        x = float(c) / 255.0
        lin.append(x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4)
    r, g, b = lin
    X = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    Y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    Z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883
    d = 6.0 / 29.0

    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > d ** 3 else t / (3.0 * d * d) + 4.0 / 29.0

    fx, fy, fz = f(X), f(Y), f(Z)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def ciede2000(lab1, lab2) -> float:
    """CIEDE2000 色差（kL = kC = kH = 1；Sharma、Wu、Dalal 2005 的公式与约定）。"""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    cb7 = ((math.hypot(a1, b1) + math.hypot(a2, b2)) / 2.0) ** 7
    G = 0.5 * (1.0 - math.sqrt(cb7 / (cb7 + 25.0 ** 7)))
    a1p, a2p = (1.0 + G) * a1, (1.0 + G) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = 0.0 if c1p == 0 else math.degrees(math.atan2(b1, a1p)) % 360.0
    h2p = 0.0 if c2p == 0 else math.degrees(math.atan2(b2, a2p)) % 360.0
    dLp, dCp = L2 - L1, c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180.0:
            dhp -= 360.0
        elif dhp < -180.0:
            dhp += 360.0
    dHp = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2.0))
    Lbp, Cbp = (L1 + L2) / 2.0, (c1p + c2p) / 2.0
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        hbp = (h1p + h2p) / 2.0
    elif h1p + h2p < 360.0:
        hbp = (h1p + h2p + 360.0) / 2.0
    else:
        hbp = (h1p + h2p - 360.0) / 2.0
    T = (1.0 - 0.17 * math.cos(math.radians(hbp - 30.0)) + 0.24 * math.cos(math.radians(2.0 * hbp))
         + 0.32 * math.cos(math.radians(3.0 * hbp + 6.0)) - 0.20 * math.cos(math.radians(4.0 * hbp - 63.0)))
    dtheta = 30.0 * math.exp(-(((hbp - 275.0) / 25.0) ** 2))
    cbp7 = Cbp ** 7
    Rc = 2.0 * math.sqrt(cbp7 / (cbp7 + 25.0 ** 7))
    Sl = 1.0 + 0.015 * (Lbp - 50.0) ** 2 / math.sqrt(20.0 + (Lbp - 50.0) ** 2)
    Sc = 1.0 + 0.045 * Cbp
    Sh = 1.0 + 0.015 * Cbp * T
    Rt = -math.sin(math.radians(2.0 * dtheta)) * Rc
    tl, tc, th = dLp / Sl, dCp / Sc, dHp / Sh
    return math.sqrt(tl * tl + tc * tc + th * th + Rt * tc * th)


# ---------------------------------------------------------------- 生成
def load_spec(path: Path | str | None = None) -> dict:
    import yaml

    return json.loads(json.dumps(yaml.safe_load(Path(path or SPEC_PATH).read_text(encoding="utf-8"))))


def _hamming(u, v) -> int:
    return sum(1 for x, y in zip(u, v, strict=True) if x != y)


def _onehot(i: int, n: int, w: float) -> list[float]:
    return [w if k == i else 0.0 for k in range(n)]


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / n, 6) for x in v]


_ZH = {"solid": "纯色", "striped": "条纹", "checked": "格纹", "slim": "体型偏瘦", "average": "体型中等", "sturdy": "体型偏壮",
       "none": "无明显配饰", "backpack": "背双肩包", "cap": "戴浅色鸭舌帽", "shoulder_bag": "挎单肩包", "slow": "步速较慢",
       "normal": "步速中等", "brisk": "步速较快", "sedan": "轿车", "hatchback": "两厢车", "suv": "SUV", "van": "厢式车", "pickup": "皮卡"}
_EN = {"solid": "plain", "striped": "striped", "checked": "checked", "slim": "slim build", "average": "medium build",
       "sturdy": "sturdy build", "none": "no visible accessories", "backpack": "backpack", "cap": "light cap",
       "shoulder_bag": "shoulder bag", "slow": "slow gait", "normal": "moderate gait", "brisk": "brisk gait", "sedan": "sedan",
       "hatchback": "hatchback", "suv": "SUV", "van": "van", "pickup": "pickup truck"}
_ROOF = {"body": ("车顶与车身同色", "body-colour roof"), "black": ("黑色车顶", "black roof"), "rack": ("带车顶行李架", "roof rack")}
_MARK = {"none": ("车身无标识", "no markings"), "side_stripe": ("车身侧面白色饰条", "white side stripe"),
         "door_panel": ("前门黄色标识板", "yellow door panel")}


def describe(entry: dict, palette: dict) -> dict:
    """由属性向量生成的规范描述文本（M23-FR-083）：只含外观，不含身份、路线与时刻。"""
    a = entry["attrs"]
    if entry["class"] == "person":
        h = entry["size_m"][2]
        zh = (f"身高约 {h:.2f} m，{_ZH[a['build']]}；上身{palette[a['top_color']]['zh']}{_ZH[a['top_pattern']]}上衣，"
              f"下身{palette[a['bottom_color']]['zh']}长裤；{_ZH[a['accessory']]}；{_ZH[a['gait']]}")
        en = (f"Approx. {h:.2f} m, {_EN[a['build']]}; {_EN[a['top_pattern']]} {palette[a['top_color']]['en']} top, "
              f"{palette[a['bottom_color']]['en']} trousers; {_EN[a['accessory']]}; {_EN[a['gait']]}")
    else:
        zh = f"{palette[a['body_color']]['zh']}{_ZH[a['body_type']]}，{_ROOF[a['roof']][0]}，{_MARK[a['marking']][0]}"
        en = f"{palette[a['body_color']]['en'].capitalize()} {_EN[a['body_type']]}, {_ROOF[a['roof']][1]}, {_MARK[a['marking']][1]}"
    return {"zh": zh, "en": en}


def _lab_feat(rgb) -> list[float]:
    L, A, B = srgb_to_lab(rgb)
    return [L / 100.0, A / 100.0, B / 100.0]


def _entry(spec: dict, cls: str, no: int, vec: tuple[int, ...]) -> dict:
    cs = spec["classes"][cls]
    pal = spec["palette"]
    attrs = cs["attributes"]
    a = {at["name"]: at["values"][i] for at, i in zip(attrs, vec, strict=True)}
    p = cs["params"]
    vals = {at["name"]: at["values"] for at in attrs}
    lic = {k: spec["license"][k] for k in ("kind", "spdx", "holder")}
    if cls == "person":
        top, bot = pal[a["top_color"]]["rgb"], pal[a["bottom_color"]]["rgb"]
        pat: dict[str, Any] = {"kind": a["top_pattern"]}
        if a["top_pattern"] != "solid":
            accent = pal["white"]["rgb"] if srgb_to_lab(top)[0] < 60.0 else pal["black"]["rgb"]
            pat.update(accent_rgb=list(accent), period_m=p["pattern_period_m"][a["top_pattern"]])
        acc: dict[str, Any] = {"kind": a["accessory"]}
        if a["accessory"] != "none":
            acc["rgb"] = list(p["accessory_rgb"][a["accessory"]])
        s = p["build_scale"][a["build"]]
        h = p["height_m"][a["height_class"]]
        gait = p["gait"][a["gait"]]
        render = {"top_rgb": list(top), "bottom_rgb": list(bot), "skin_rgb": list(p["skin_rgb"]), "pattern": pat, "accessory": acc}
        size = [round(p["base_size_m"][0] * s, 4), round(p["base_size_m"][1] * s, 4), float(h)]
        emb = (_lab_feat(top) + _lab_feat(bot) + _onehot(vec[2], len(vals["top_pattern"]), 0.5)
               + _onehot(vec[3], len(vals["accessory"]), 0.5)
               + [(h - 1.74) / 0.12 * 0.3, (s - 1.0) / 0.12 * 0.3, (gait["median_mps"] - 1.35) / 0.25 * 0.3])
        extra = {"gait": {"speed_mps": [float(x) for x in gait["speed_mps"]], "median_mps": float(gait["median_mps"])}}
    else:
        body = pal[a["body_color"]]["rgb"]
        roof = p["roof_rgb"]["black"] if a["roof"] == "black" else body
        render = {"body_type": a["body_type"], "body_rgb": list(body), "roof_rgb": list(roof), "glass_rgb": list(p["glass_rgb"]),
                  "marking": {"kind": a["marking"], **({"rgb": list(p["marking_rgb"][a["marking"]])} if a["marking"] != "none" else {})}}
        if a["roof"] == "rack":
            render["roof_rack"] = {"rgb": list(p["rack_rgb"])}
        size = [float(x) for x in p["size_m"][a["body_type"]]]
        emb = (_lab_feat(body) + _lab_feat(roof) + _onehot(vec[0], len(vals["body_type"]), 0.5)
               + _onehot(vec[2], len(vals["roof"]), 0.3) + _onehot(vec[3], len(vals["marking"]), 0.5)
               + [(size[0] - 4.7) / 0.6 * 0.3, (size[2] - 1.7) / 0.3 * 0.3])
        extra = {}
    e = {"id": f"{cls}-{no:02d}", "class": cls, "attrs": a, "vector": list(vec),
         "asset": {"kind": "procedural", "generator": cs["asset"], "rig": cs["rig"], "render": render}, "size_m": size, **extra,
         "anim_clips": [dict(c) for c in cs["anim_clips"]], "thermal_zones": [dict(z) for z in cs["thermal_zones"]],
         "embedding": {"model": EMBED_MODEL, "values": _unit(emb)}, "license": lic}
    e["description"] = describe(e, pal)
    return e


def _vectors(spec: dict, cls: str, rnd: int = 0) -> list[tuple[int, ...]] | None:
    """计数器哈希抽样 + 贪心接受：库内两两属性距离 ≥ min_pair_distance、每个取值至多 ⌈N/n⌉ + balance_slack 次（缺省 1）、
    `distinct` 属性对不同取值、不命中 `exclude` 中的取值组合（外观上不可见的差异）。`rnd > 0` 为重抽轮次（键追加 `|r<rnd>`）。
    候选耗尽时返回 None。"""
    cs = spec["classes"][cls]
    attrs = cs["attributes"]
    n, dmin = int(cs["count"]), int(cs["min_pair_distance"])
    if len(attrs) > 8:
        raise ValueError("at most 8 attributes per class")
    names = [a["name"] for a in attrs]
    caps = [math.ceil(n / len(a["values"])) + int(cs.get("balance_slack", 1)) for a in attrs]
    pairs = [(names.index(x), names.index(y)) for x, y in cs.get("distinct", [])]
    excl = [[(names.index(an), av) for an, av in ex.items()] for ex in cs.get("exclude", [])]
    out: list[tuple[int, ...]] = []
    used = [Counter() for _ in attrs]
    tag = f"|r{rnd}" if rnd else ""
    for k in range(MAX_CANDIDATES):
        h = hashlib.sha256(f"{spec['lib_id']}|{cls}|{int(spec['seed'])}|{k}{tag}".encode()).digest()
        v = tuple(int.from_bytes(h[4 * j:4 * j + 4], "big") % len(a["values"]) for j, a in enumerate(attrs))
        if any(attrs[i]["values"][v[i]] == attrs[j]["values"][v[j]] for i, j in pairs):
            continue
        if any(all(attrs[j]["values"][v[j]] == av for j, av in ex) for ex in excl):
            continue
        if any(used[j][v[j]] >= caps[j] for j in range(len(attrs))):
            continue
        if any(_hamming(v, u) < dmin for u in out):
            continue
        out.append(v)
        for j in range(len(attrs)):
            used[j][v[j]] += 1
        if len(out) == n:
            return out
    return None


def _partners_min(spec: dict, cls: str, vecs: list[tuple[int, ...]]) -> int | None:
    """该类别规则下每个外观的合格干扰外观数的最小值；类别无规则时为 None。"""
    rule = next((r for r in spec["rules"].values() if r["class"] == cls), None)
    if rule is None:
        return None
    names = [a["name"] for a in spec["classes"][cls]["attributes"]]
    any_of = {names.index(x) for x in rule.get("shared_any_of", [])}

    def ok(u, v) -> bool:
        sh = {j for j in range(len(u)) if u[j] == v[j]}
        return (len(u) - len(sh) >= int(rule["min_distance"]) and len(sh) >= int(rule["min_shared"])
                and (not any_of or bool(any_of & sh)))

    return min(sum(1 for j, v in enumerate(vecs) if j != i and ok(u, v)) for i, u in enumerate(vecs))


def _class_vectors(spec: dict, cls: str) -> tuple[list[tuple[int, ...]], int]:
    """按轮次 0, 1, … 抽样，取首个满足 `min_hard_negatives`（每个外观在其类别规则下的合格干扰外观数）的结果。"""
    need = int(spec.get("min_hard_negatives", 0))
    for rnd in range(64):
        vecs = _vectors(spec, cls, rnd)
        if vecs is None:
            continue
        pm = _partners_min(spec, cls, vecs)
        if pm is None or pm >= need:
            return vecs, rnd
    raise ValueError(f"cannot draw {spec['classes'][cls]['count']} {cls} appearances satisfying the spec within 64 rounds")


def build_library(spec: dict) -> dict:
    """生成规格 → 外观库文档（确定性）。"""
    drawn = {cls: _class_vectors(spec, cls) for cls in spec["classes"]}
    entries = [_entry(spec, cls, i + 1, v) for cls, (vecs, _r) in drawn.items() for i, v in enumerate(vecs)]
    classes = {cls: {"count": int(cs["count"]), "min_pair_distance": int(cs["min_pair_distance"]), "asset": cs["asset"], "rig": cs["rig"],
                     "attributes": [{"name": a["name"], "kind": a["kind"], "values": list(a["values"])} for a in cs["attributes"]]}
               for cls, cs in spec["classes"].items()}
    return {"schema": SCHEMA, "lib_id": spec["lib_id"],
            "generator": {"name": GENERATOR, "version": GENERATOR_VERSION, "spec_sha256": hashlib.sha256(jcs(spec)).hexdigest(),
                          "seed": int(spec["seed"]), "rounds": {cls: r for cls, (_v, r) in drawn.items()}, "embedding_model": EMBED_MODEL},
            "license": dict(spec["license"]), "palette": spec["palette"], "min_delta_e00": float(spec["min_delta_e00"]),
            "classes": classes, "rules": spec["rules"], "min_hard_negatives": int(spec.get("min_hard_negatives", 0)), "entries": entries}


def lib_bytes(lib: dict) -> bytes:
    """外观库文件字节（UTF-8、缩进 1、键序保持生成顺序、末尾换行）。"""
    return (json.dumps(lib, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


# ---------------------------------------------------------------- 外观库
@dataclass(frozen=True)
class Appearance:
    id: str
    cls: str
    vector: tuple[int, ...]
    entry: dict

    @property
    def attrs(self) -> dict[str, str]:
        return self.entry["attrs"]


class Library:
    """已装载的外观库：按 id 查找、属性距离、共享属性、嵌入距离与规则校验。"""

    def __init__(self, doc: dict, sha256: str | None = None) -> None:
        self.doc = doc
        self.lib_id: str = doc["lib_id"]
        self.sha256 = sha256 or hashlib.sha256(lib_bytes(doc)).hexdigest()
        self.rules: dict[str, dict] = doc.get("rules", {})
        self.attr_names = {c: [a["name"] for a in cs["attributes"]] for c, cs in doc["classes"].items()}
        self.by_id = {e["id"]: Appearance(e["id"], e["class"], tuple(e["vector"]), e) for e in doc["entries"]}

    def get(self, ref: str) -> Appearance | None:
        """`<id>` 或 `<lib_id>/<id>`；其他库前缀不解析。"""
        lib, sep, eid = str(ref).rpartition("/")
        if sep and lib != self.lib_id:
            return None
        return self.by_id.get(eid)

    def ids(self, cls: str) -> list[str]:
        return [k for k, a in self.by_id.items() if a.cls == cls]

    def distance(self, a: Appearance, b: Appearance) -> int:
        if a.cls != b.cls:
            raise ValueError("attribute distance is defined within one class")
        return _hamming(a.vector, b.vector)

    def shared(self, a: Appearance, b: Appearance) -> list[str]:
        return [n for n, x, y in zip(self.attr_names[a.cls], a.vector, b.vector, strict=True) if x == y]

    @staticmethod
    def embed_distance(a: Appearance, b: Appearance) -> float:
        u, v = a.entry["embedding"]["values"], b.entry["embedding"]["values"]
        return round(math.sqrt(sum((x - y) ** 2 for x, y in zip(u, v, strict=True))), 6)

    def pair_whys(self, rule: dict, t: Appearance, d: Appearance) -> list[str]:
        """目标与干扰者的规则违例（`detail.why`），空表为合格。"""
        if t.cls != d.cls:
            return ["appearance_class"]
        sh = self.shared(t, d)
        out = []
        if self.distance(t, d) < int(rule["min_distance"]):
            out.append("appearance_distance")
        if len(sh) < int(rule["min_shared"]):
            out.append("appearance_hard_negative")
        if rule.get("shared_any_of") and not set(rule["shared_any_of"]) & set(sh):
            out.append("appearance_similarity")
        return out

    def stats(self, cls: str) -> dict:
        """两两属性距离与嵌入距离统计（M23-AC-038：写入编译报告）。"""
        xs = [self.by_id[i] for i in self.ids(cls)]
        dist = Counter()
        emb: list[float] = []
        hard = Counter()
        rule = next((r for r in self.rules.values() if r["class"] == cls), None)
        for i, a in enumerate(xs):
            for b in xs[i + 1:]:
                dist[self.distance(a, b)] += 1
                emb.append(self.embed_distance(a, b))
                if rule is not None and not self.pair_whys(rule, a, b):
                    hard[a.id] += 1
                    hard[b.id] += 1
        n_pairs = sum(dist.values())
        emb.sort()
        out = {"n": len(xs), "n_pairs": n_pairs, "distance_hist": {str(k): dist[k] for k in sorted(dist)},
               "distance_min": min(dist) if dist else None, "distance_max": max(dist) if dist else None,
               "distance_mean": round(sum(k * v for k, v in dist.items()) / n_pairs, 4) if n_pairs else None,
               "embed_distance": {"min": emb[0], "median": emb[len(emb) // 2], "max": emb[-1]} if emb else None}
        if rule is not None:
            out["rule_pairs"] = sum(hard.values()) // 2
            out["rule_partners_min"] = min((hard[a.id] for a in xs), default=0)
        return out


def _lib_path() -> Path:
    return Path(os.environ.get("AWR_APPEARANCE_LIB") or LIB_PATH)


@lru_cache(maxsize=8)
def _load(path: str, mtime_ns: int) -> Library:
    import jsonschema

    from awr.contracts._paths import schema_path

    raw = Path(path).read_bytes()
    doc = json.loads(raw)
    sch = json.loads(schema_path("event/appearance_lib.v1.schema.json").read_text(encoding="utf-8"))
    errs = sorted(jsonschema.Draft202012Validator(sch).iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    if errs:
        e = errs[0]
        raise ValueError(f"{path}: does not match {SCHEMA}: /{'/'.join(map(str, e.absolute_path))}: {e.message}")
    return Library(doc, hashlib.sha256(raw).hexdigest())


def load_library(path: Path | str | None = None) -> Library:
    """装载外观库（缺省 `configs/appearance.d3/appearance_lib.v1.json`，`AWR_APPEARANCE_LIB` 覆盖），按路径与修改时间缓存。"""
    p = Path(path) if path else _lib_path()
    return _load(str(p.resolve()), p.stat().st_mtime_ns)


def resolve(ref: str, cls: str | None = None, lib: Library | None = None) -> Appearance:
    """解析外观引用；无法解析或类别不符时抛 EventError 611（`appearance_unresolved`、`appearance_class`）。"""
    lib = lib or load_library()
    ap = lib.get(ref)
    if ap is None:
        raise EventError(611, f"appearance reference {ref!r} is not in {lib.lib_id}", {"why": "appearance_unresolved", "ref": ref})
    if cls is not None and ap.cls != cls:
        raise EventError(611, f"appearance {ref!r} is a {ap.cls}, not a {cls}", {"why": "appearance_class", "ref": ref})
    return ap


def plan_fields(ref: str, lib: Library | None = None) -> dict:
    """规范化用：引用外观在 plan 中的外观段、尺寸（m）与步态档（人物；车辆为 None）。"""
    lib = lib or load_library()
    ap = resolve(ref, lib=lib)
    r = ap.entry["asset"]["render"]
    look: dict[str, Any] = {"asset": f"{lib.lib_id}/{ap.id}"}
    if ap.cls == "person":
        look.update(top_rgb=list(r["top_rgb"]), bottom_rgb=list(r["bottom_rgb"]))
        look["height_mm"] = math.floor(float(ap.entry["size_m"][2]) * 1000.0 + 0.5)
        if r["accessory"]["kind"] != "none":
            look["accessories"] = [r["accessory"]["kind"]]
    else:
        look["body_rgb"] = list(r["body_rgb"])
    return {"appearance": look, "size_m": [float(x) for x in ap.entry["size_m"]], "gait": ap.entry.get("gait")}


# ---------------------------------------------------------------- 门 1
def _iss(path: str, zh: str, en: str, **detail) -> Issue:
    return Issue("E-APPEARANCE", 611, path, zh, en, detail)


def _pair_text(why: str, tid: str, did: str, dist: int, sh: list[str], rule: dict) -> tuple[str, str]:
    if why == "appearance_distance":
        return (f"干扰者 {did} 与目标 {tid} 的属性距离 {dist} 小于 {rule['min_distance']}",
                f"Attribute distance {dist} between distractor {did} and target {tid} is below {rule['min_distance']}")
    if why == "appearance_hard_negative":
        return (f"干扰者 {did} 与目标 {tid} 共享属性 {len(sh)} 个，少于 {rule['min_shared']}（难负样本）",
                f"Distractor {did} shares {len(sh)} attributes with target {tid}, fewer than {rule['min_shared']} (hard negative)")
    if why == "appearance_similarity":
        return (f"干扰者 {did} 与目标 {tid} 在 {'、'.join(rule['shared_any_of'])} 上均不相同",
                f"Distractor {did} differs from target {tid} in all of {', '.join(rule['shared_any_of'])}")
    return f"干扰者 {did} 与目标 {tid} 类别不同", f"Distractor {did} and target {tid} belong to different classes"


def gate1_issues(doc: dict, lib: Library | None = None) -> list[Issue]:
    """门 1 外观检查（M23-FR-072）：引用解析、与外观段互斥、步态档一致，以及按事件类型规则校验目标与干扰者的可区分性。
    假定 `doc` 已通过 schema；一次报出全部问题。"""
    acts = doc.get("actors", [])
    if not any(a.get("appearance_ref") for a in acts) and doc.get("event_type", "rendezvous_handoff") not in ("reid_occlusion",
                                                                                                             "vehicle_track"):
        return []
    out: list[Issue] = []
    if lib is None:
        try:
            lib = load_library()
        except (OSError, ValueError) as e:
            return [_iss(f"/actors/{i}/appearance_ref", f"外观库 {LIB_ID} 无法装载：{e}", f"Cannot load {LIB_ID}: {e}",
                         why="appearance_unresolved", ref=a["appearance_ref"]) for i, a in enumerate(acts) if a.get("appearance_ref")]
    res: dict[int, Appearance] = {}
    for i, a in enumerate(acts):
        ref = a.get("appearance_ref")
        if not ref:
            continue
        p = f"/actors/{i}/appearance_ref"
        ap = lib.get(ref)
        if ap is None:
            out.append(_iss(p, f"外观引用 {ref} 不在 {lib.lib_id} 中", f"Appearance reference {ref} is not in {lib.lib_id}",
                            why="appearance_unresolved", ref=ref))
            continue
        if ap.cls != a["class"]:
            out.append(_iss(p, f"外观 {ref} 属于类别 {ap.cls}，与演员类别 {a['class']} 不符",
                            f"Appearance {ref} is a {ap.cls}, the actor is a {a['class']}", why="appearance_class", ref=ref))
            continue
        res[i] = ap
        over = sorted(k for k in (a.get("appearance") or {}) if k in _OVERRIDE_KEYS)
        if over:
            out.append(_iss(f"/actors/{i}/appearance", "给出外观引用时不得再以 appearance 覆写着色、尺寸或配饰",
                            "appearance must not override colours, size or accessories when appearance_ref is given",
                            why="appearance_override", ref=ref, keys=over))
        g, sp = ap.entry.get("gait"), (a.get("gait") or {}).get("speed_mps")
        if g and sp is not None and not (g["speed_mps"][0] <= sp[0] <= sp[1] <= g["speed_mps"][1]):
            out.append(_iss(f"/actors/{i}/gait/speed_mps", f"步速 {sp} 超出外观 {ref} 的步态档 {g['speed_mps']}",
                            f"speed_mps {sp} lies outside the gait class {g['speed_mps']} of appearance {ref}",
                            why="appearance_gait", ref=ref, gait_speed_mps=g["speed_mps"]))
    rule = lib.rules.get(doc.get("event_type", "rendezvous_handoff"))
    if rule is None:
        return out
    role_of = {i: set(a.get("roles") or ()) for i, a in enumerate(acts)}
    tgt = [i for i, r in role_of.items() if "target" in r]
    dis = [i for i, r in role_of.items() if "distractor" in r and "target" not in r]
    out.extend(_iss(f"/actors/{i}/appearance_ref", f"{doc['event_type']} 事件的目标与干扰者须给出外观引用",
                    f"Targets and distractors of {doc['event_type']} events require appearance_ref", why="appearance_required",
                    actor=acts[i]["id"]) for i in tgt + dis if not acts[i].get("appearance_ref"))
    for i in tgt:
        for j in dis:
            if i not in res or j not in res:
                continue
            t, d = res[i], res[j]
            dist, sh = lib.distance(t, d), lib.shared(t, d)
            info = {"target": acts[i]["id"], "distractor": acts[j]["id"], "distance": dist, "shared": sh, "rule": rule}
            for why in lib.pair_whys(rule, t, d):
                zh, en = _pair_text(why, acts[i]["id"], acts[j]["id"], dist, sh, rule)
                out.append(_iss(f"/actors/{j}/appearance_ref", zh, en, why=why, **info))
    return out


def appearance_report(plan: dict, lib: Library | None = None) -> dict:
    """编译报告的外观段（M23-AC-038、M23 §15.3 规则 3）：外观库版本与两两属性距离统计、本事件目标与干扰者逐对的属性距离、
    共享属性与嵌入距离（诊断）。`plan` 为规范化结果（引用外观的演员 `appearance.asset` 为 `<lib_id>/<id>`）。"""
    lib = lib or load_library()
    out: dict[str, Any] = {"lib_id": lib.lib_id, "lib_sha256": lib.sha256, "embedding_model": lib.doc["generator"].get("embedding_model"),
                           "library": {c: lib.stats(c) for c in lib.doc["classes"]}, "actors": {}, "pairs": []}
    res: dict[str, Appearance] = {}
    for a in plan.get("actors", []):
        asset = str((a.get("appearance") or {}).get("asset", ""))
        ap = lib.get(asset) if asset.startswith(lib.lib_id + "/") else None
        if ap is not None:
            res[a["id"]] = ap
            out["actors"][a["id"]] = ap.id
    rule = lib.rules.get(plan.get("event_type", "rendezvous_handoff"))
    if rule is not None:
        tgt = [a["id"] for a in plan.get("actors", []) if "target" in (a.get("roles") or ())]
        dis = [a["id"] for a in plan.get("actors", []) if "distractor" in (a.get("roles") or ()) and "target" not in a["roles"]]
        for t in tgt:
            for d in dis:
                if t in res and d in res:
                    out["pairs"].append({"target": t, "distractor": d, "distance": lib.distance(res[t], res[d]),
                                         "shared": lib.shared(res[t], res[d]), "embed_distance": lib.embed_distance(res[t], res[d]),
                                         "ok": not lib.pair_whys(rule, res[t], res[d])})
    return out


def report_for(plan: dict) -> dict | None:
    """编译报告用：事件引用了外观库或属于有外观规则的类型时给出 `appearance_report`，否则 None（不装载外观库）。"""
    if plan.get("event_type") not in ("reid_occlusion", "vehicle_track") and not any(
            str((a.get("appearance") or {}).get("asset", "")).startswith("appearance_lib@") for a in plan.get("actors", [])):
        return None
    try:
        return appearance_report(plan)
    except (OSError, ValueError) as e:                           # 外观库不可用：报告中记录原因，不影响已通过门 1 的编译
        return {"lib_id": LIB_ID, "error": str(e)}


# ---------------------------------------------------------------- 库自检
def library_issues(lib_doc: dict, spec: dict | None = None) -> list[str]:
    """外观库自检：schema、数量下限、id 唯一、属性与向量一致、调色板色差、库内两两距离、规则下的干扰外观数量、许可，
    给出 `spec` 时另核对与重新生成的结果逐字节一致。返回问题列表（空表为通过）。"""
    import jsonschema

    from awr.contracts._paths import schema_path

    sch = json.loads(schema_path("event/appearance_lib.v1.schema.json").read_text(encoding="utf-8"))
    errs = [f"schema: /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in jsonschema.Draft202012Validator(sch).iter_errors(lib_doc)]
    if errs:
        return errs
    out: list[str] = []
    if lib_doc["lib_id"] != LIB_ID:
        out.append(f"lib_id {lib_doc['lib_id']} != {LIB_ID}")
    pal, de_min = lib_doc["palette"], float(lib_doc.get("min_delta_e00", 0.0))
    ids = Counter(e["id"] for e in lib_doc["entries"])
    out += [f"duplicate id {k}" for k, v in ids.items() if v > 1]
    for cls, cs in lib_doc["classes"].items():
        if cls in MIN_COUNT and sum(1 for e in lib_doc["entries"] if e["class"] == cls) < MIN_COUNT[cls]:
            out.append(f"{cls}: fewer than {MIN_COUNT[cls]} appearances")
        for at in cs["attributes"]:
            if at["kind"] != "color":
                continue
            out += [f"palette: {at['values'][i]} is not defined" for i in range(len(at["values"])) if at["values"][i] not in pal]
            vs = [v for v in at["values"] if v in pal]
            for i, x in enumerate(vs):
                for y in vs[i + 1:]:
                    de = ciede2000(srgb_to_lab(pal[x]["rgb"]), srgb_to_lab(pal[y]["rgb"]))
                    if de < de_min:
                        out.append(f"{cls}.{at['name']}: CIEDE2000({x}, {y}) = {de:.2f} < {de_min}")
    for e in lib_doc["entries"]:
        attrs = lib_doc["classes"][e["class"]]["attributes"]
        if len(e["vector"]) != len(attrs) or any(
                e["vector"][k] >= len(at["values"]) or e["attrs"].get(at["name"]) != at["values"][e["vector"][k]] for k, at in enumerate(attrs)):
            out.append(f"{e['id']}: attrs and vector disagree")
        lic = e["license"]
        if lic["kind"] == "original" and lic.get("third_party"):
            out.append(f"{e['id']}: original asset lists third-party sources")
    if out:
        return out
    lib = Library(lib_doc)
    for cls, cs in lib_doc["classes"].items():
        st = lib.stats(cls)
        if st["distance_min"] is not None and st["distance_min"] < int(cs["min_pair_distance"]):
            out.append(f"{cls}: pairwise attribute distance {st['distance_min']} < {cs['min_pair_distance']}")
        if "rule_partners_min" in st and st["rule_partners_min"] < int(lib_doc.get("min_hard_negatives", 0)):
            out.append(f"{cls}: an appearance has only {st['rule_partners_min']} rule-compliant distractor appearances")
    if spec is not None and lib_bytes(build_library(spec)) != lib_bytes(lib_doc):
        out.append("library differs from a fresh build of the spec (run: python -m awr.sim.events.appearance build)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.sim.events.appearance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="由生成规格生成外观库文件")
    b.add_argument("--check", action="store_true", help="只比对，不一致时退出码 1")
    for p in (b, sub.add_parser("check", help="外观库自检"), sub.add_parser("stats", help="两两属性距离统计")):
        p.add_argument("--spec", type=Path, default=SPEC_PATH)
        p.add_argument("--lib", type=Path, default=LIB_PATH)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        data = lib_bytes(build_library(load_spec(a.spec)))
        if a.check:
            same = a.lib.exists() and a.lib.read_bytes() == data
            print("up to date" if same else f"{a.lib} differs from a fresh build")
            return 0 if same else 1
        a.lib.write_bytes(data)
        print(f"wrote {a.lib} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()})")
        return 0
    doc = json.loads(a.lib.read_text(encoding="utf-8"))
    if a.cmd == "check":
        iss = library_issues(doc, load_spec(a.spec))
        print(json.dumps({"ok": not iss, "issues": iss}, ensure_ascii=False, indent=1))
        return 1 if iss else 0
    lib = Library(doc)
    print(json.dumps({c: lib.stats(c) for c in doc["classes"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
