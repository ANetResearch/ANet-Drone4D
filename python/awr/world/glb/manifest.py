"""D3 GLB 世界的对外清单与署名（M22-FR-064、FR-065、M22 §7.6.4；M27-FR-065；ADR-154；05 §14；D3-AC-24）。

- `notice_text(world, lang)`：署名段落全文（原作者、来源链接、许可、修改说明；OSM 派生层的 ODbL 声明与各派生来源署名），
  供 `world_manifest.json`、THIRD_PARTY_NOTICES、数据集页面、`/lab/` 页面与 public 切片使用，各处取同一文本。
- `world_manifest(world_dir, public=False)`：M27 写入数据集的 `world_manifest.json`（世界 id、`contentVersion`、`coordinate_sha256`、
  尺度与坐标约定、网格与 NavMesh 与锚点 sha256、`sensorHints`、许可与署名全文）。`public = True` 时不含内部路径与构建信息。
- `check_world_manifest(doc)`：清单的许可与署名完整性检查（与 V-GLB-08 同一判据），返回问题列表，空表即通过。

文本为英文与简体中文两份，企业级书面表述；不读取网络，不含任何内部路径。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from awr.world.package.jsonio import read_json

__all__ = ["MANIFEST_SCHEMA", "check_world_manifest", "notice_text", "world_manifest"]

MANIFEST_SCHEMA = "awr.world_manifest.v1"
ODBL = "ODbL-1.0"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_LICENSE_NAMES = {"CC-BY-4.0": ("Creative Commons Attribution 4.0 International", "知识共享署名 4.0 国际许可协议"),
                  "ODbL-1.0": ("Open Database License 1.0", "开放数据库许可协议 1.0"),
                  "public-domain": ("public domain", "公有领域")}


def _lic(code: str, zh: bool) -> str:
    n = _LICENSE_NAMES.get(code)
    return f"{code}（{n[1]}）" if (n and zh) else f"{code} ({n[0]})" if n else code


def notice_text(world: dict, lang: str = "en") -> str:
    """署名段落（M22-AC-009：含原作者、来源、许可与修改说明）。`world` 为 world.json 文档。"""
    d = world.get("dataset") or {}
    zh = lang == "zh"
    df = list(d.get("derivedFrom") or [])
    title = world.get("nameZh") if zh and world.get("nameZh") else world.get("name") or world.get("id")
    src = d.get("name", "")
    code = str(d.get("license", ""))
    author = str(d.get("citation", ""))
    author = author[: -len(code) - 2] if code and author.endswith(f", {code}") else author     # citation 形如 "作者, 许可"
    lines = []
    if zh:
        lines.append(f"世界 {world.get('id')}（{title}）的网格与纹理来自 {src}，作者 {author}，"
                     f"以 {_lic(str(d.get('license', '')), True)} 授权使用；来源：{d.get('url', '未登记')}。")
        lines.append("本世界包相对原始资产经过修改：米制标定、调平、世界坐标系重表达及派生资产（点云、栅格、求交网格、外观网格等）。")
        if any(e.get("license") == ODBL for e in df):
            lines.append("由 OpenStreetMap 派生的图层（锚点、道路图与相关质量多边形）以 ODbL-1.0 提供，\u00a9 OpenStreetMap contributors。")
        lines.extend(f"派生来源：{e.get('name')}（{e.get('version')}），许可 {_lic(str(e.get('license')), True)}，"
                     f"署名 {e.get('attribution')}，用途 {e.get('use')}" + (f"，{e['url']}" if e.get("url") else "") + "。" for e in df)
    else:
        lines.append(f"The mesh and textures of world {world.get('id')} ({title}) are from {src} by {author}, "
                     f"licensed under {_lic(str(d.get('license', '')), False)}; source: {d.get('url', 'not recorded')}.")
        lines.append("This world package is modified from the original asset: metric calibration, levelling, re-expression in the world "
                     "frame and derived assets (point cloud, rasters, ray-cast mesh, appearance mesh and others).")
        if any(e.get("license") == ODBL for e in df):
            lines.append("Layers derived from OpenStreetMap (anchors, road graph and related quality polygons) are provided under ODbL-1.0, "
                         "\u00a9 OpenStreetMap contributors.")
        lines.extend(f"Derived from {e.get('name')} ({e.get('version')}), licence {_lic(str(e.get('license')), False)}, "
                     f"attribution: {e.get('attribution')}, used for {e.get('use')}" + (f", {e['url']}" if e.get("url") else "") + "."
                     for e in df)
    return "\n".join(lines)


def _file_sha(world: dict) -> dict[str, dict]:
    return {f["path"]: f for f in world.get("files", [])}


def _layer_files(world: dict, L: dict) -> list[dict]:
    files = _file_sha(world)
    href = str(L.get("href", ""))
    if href.endswith("/"):
        return [files[p] for p in sorted(files) if p.startswith(href)]
    base = href.rsplit("/", 1)[0] + "/" if "/" in href else ""
    stem = href.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return [files[p] for p in sorted(files) if p == href or (p.startswith(base + stem + ".") and "/" not in p[len(base):])]


def world_manifest(world_dir: Path, *, public: bool = False) -> dict[str, Any]:
    """`world_manifest.json`（M22-FR-064；M27-FR-065）。内容 sha256 取自 `world.json files[]`（与 contentVersion 同源）。"""
    world_dir = Path(world_dir)
    w = read_json(world_dir / "world.json")
    c = read_json(world_dir / (w.get("coordinate") or {}).get("href", "coordinate.json"))
    d = w.get("dataset") or {}
    layers = []
    for L in w.get("layers", []):
        fs = _layer_files(w, L)
        row = {"id": L["id"], "type": L["type"], "format": L["format"], "files": [{"path": f["path"], "bytes": f["bytes"],
                                                                                   "sha256": f["sha256"]} for f in fs]}
        if L.get("note"):
            row["note"] = L["note"]
        layers.append(row)
    a = c.get("anchor") or {}
    src = c.get("source") or {}
    out: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA, "schema_version": "1.0.0", "world_id": w["id"], "name": w.get("name"), "nameZh": w.get("nameZh"),
        "contentVersion": w["contentVersion"], "coordinate_sha256": (w.get("coordinate") or {}).get("sha256"),
        "profile": w.get("profile"), "scaleStatus": w.get("scaleStatus"), "public": bool(public),
        "frame": {**(c.get("frame") or {}), "conventions": c.get("conventions"),
                  "anchor": {k: a.get(k) for k in ("kind", "georeferenced", "datum", "latDeg", "lonDeg", "hMslM", "uncertaintyM", "label")},
                  "unitsToMeters": src.get("unitsToMeters"), "yawDeg": src.get("yawDeg"), "leveledDeg": src.get("leveledDeg"),
                  "registration": (c.get("registration") or {}).get("method")},
        "bounds": w.get("bounds"), "sensorHints": w.get("sensorHints"), "layers": layers,
        "license": {"dataset": d.get("license"), "citation": d.get("citation"), "url": d.get("url"), "notice": d.get("notice"),
                    "redistribution": d.get("redistribution"), "sourceFiles": d.get("sourceFiles", []),
                    "derivedFrom": list(d.get("derivedFrom") or []),
                    "attribution": {"en": notice_text(w, "en"), "zh": notice_text(w, "zh")}},
    }
    if not public:
        out["build"] = {"createdAt": w.get("createdAt"), "generator": {k: (w.get("generator") or {}).get(k) for k in ("name", "version", "commit")},
                        "qa": {"status": (w.get("qa") or {}).get("status"), "href": (w.get("qa") or {}).get("href")}}
    return out


def check_world_manifest(doc: dict) -> list[str]:
    """许可与署名完整性（V-GLB-08 同一判据；M22-AC-009）：返回问题列表。"""
    lic = doc.get("license")
    if not isinstance(lic, dict):
        return ["license missing"]
    out = [f"license.{k} missing" for k in ("dataset", "citation", "notice")
           if not str(lic.get(k) or "").strip() or str(lic.get(k)).strip().lower() == "unknown"]
    code = str(lic.get("dataset") or "")
    if code.startswith("CC-BY"):
        if code not in str(lic.get("citation") or ""):
            out.append(f"license.citation must name the author and {code}")
        if not lic.get("url"):
            out.append(f"license.url missing for {code} source")
    if not lic.get("sourceFiles") or not all(_SHA.match(str(f.get("sha256", ""))) for f in lic.get("sourceFiles", [])):
        out.append("license.sourceFiles missing or without sha256")
    df = lic.get("derivedFrom")
    if not isinstance(df, list):
        out.append("license.derivedFrom missing")
        df = []
    for i, e in enumerate(df):
        miss = [k for k in ("name", "version", "license", "use", "attribution") if not str(e.get(k) or "").strip()]
        if miss:
            out.append(f"license.derivedFrom[{i}] missing {miss}")
        if not _SHA.match(str(e.get("sha256", ""))):
            out.append(f"license.derivedFrom[{i}].sha256 missing")
    osm = any(e.get("license") == ODBL for e in df)
    if (doc.get("frame") or {}).get("registration") in ("osm-road-correlation", "osm-footprint-correlation", "control-points",
                                                         "inherit-frame") and not osm:
        out.append("license.derivedFrom has no OpenStreetMap (ODbL-1.0) entry")
    if osm and "ODbL" not in str(lic.get("notice") or ""):
        out.append("license.notice must state that OpenStreetMap-derived layers are ODbL-1.0")
    att = lic.get("attribution") or {}
    for lang in ("en", "zh"):
        t = str(att.get(lang) or "")
        if not t:
            out.append(f"license.attribution.{lang} missing")
            continue
        if code and code not in t:
            out.append(f"license.attribution.{lang} does not name the licence {code}")
        if lic.get("citation") and str(lic["citation"]).split(",")[0] not in t:
            out.append(f"license.attribution.{lang} does not name the original author")
        if osm and "OpenStreetMap contributors" not in t:
            out.append(f"license.attribution.{lang} lacks 'OpenStreetMap contributors'")
    out.extend(f"layer {L.get('id')}: ODbL layer note missing" for L in doc.get("layers", [])
               if L.get("type") in ("anchors", "navgraph") and osm and "ODbL" not in str(L.get("note") or ""))
    if "build" in doc and doc.get("public"):
        out.append("public manifest carries build information")
    return out
