#!/usr/bin/env bash
# 产品镜像清单扫描（D3 封板 L04；D3-AC-24、M25-AC-019；AWR-05 §14、ADR-140、ADR-143、ADR-154；M25-FR-181、M25-NFR-11）：
# 在 dmax 上对产品镜像（缺省 d3-gz:dev、d3-sensorsvc:dev、d3-agent-py:dev，另 d3-trivy:dev 存在时一并扫描）逐个取两份清单并断言
# 其中没有 nvdiffrast（NVIDIA Source Code License，研究与评估限定）与 FAST-LIVO2（GPL-2.0，只以独立容器使用）的发行包、文件与链接产物：
#   1. 镜像内清单（inside）：以镜像自身的 python3 运行 `pip list --format json` 与 importlib.metadata 发行包枚举，并检查
#      研究限定模块的可导入性（find_spec，不执行导入）；镜像没有 python3 时记“无解释器”，以文件扫描为准。
#   2. 文件扫描（tar）：`docker create` 后以 `docker export` 导出完整根文件系统，流式送入扫描容器（缺省 d3-sensorsvc:dev 的 python3）：
#      逐文件路径段匹配、全部 site-packages 的 dist-info / egg-info 元数据、全部文件内容匹配（小写字节，分块带重叠）；
#      ELF 与 ar 归档中的命中视为链接产物，Python 源码中的 import 视为使用，其余文本提及只告警。文件清单另存为 TSV。
# 自检对照（control）：缺省扫描研究镜像 d3-livo:dev（应检出 FAST-LIVO2）与 d3-r3-camera-render:v1（应检出 nvdiffrast），
# 对照未检出即判扫描器失效、整体不通过；CONTROLS= 置空可跳过（结论中注明无对照）。
#
# 判定：产品镜像（product）与可选镜像（optional）错误级命中为 0 且导出流完整即通过；产品镜像不存在即不通过，可选镜像不存在记跳过；
# 对照镜像须有所属术语组的错误级命中。错误级：package（发行包名）、path（路径段）、binary（ELF 或 ar 内容）、import（Python 导入、
# 镜像内可导入）；告警级：mention（其他文件内容提及，如文档与 .pyc 字符串）。
#
# 产物：dmax $D/out/scan_images/<tag>/ 与本机副本 var/scan_images/<tag>/（var/ 不入库）。<tag> = <UTC 时间>-<HEAD 12 位 sha>[-dirty]（-dirty：本脚本有未提交改动或尚未入库；所用扫描器另存 bin/）。
# 文件：summary.json（awr.d3.image_scan.v1）、summary.txt；逐镜像 <k>.inspect.json（docker image inspect）、<k>.inside.json、
# <k>.tar.json、<k>.files.tsv.gz（路径、类型、大小、权限）、<k>.rc（导出与扫描退出码）；bin/image_scan.py（本次所用扫描器）。
#
# 用法（本机仓库根目录）：
#   tools/deploy/dmax/scan_images.sh                     # 缺省产品镜像、可选镜像与对照
#   tools/deploy/dmax/scan_images.sh d3-gz:dev ...       # 只扫描给出的镜像（均按 product 判定；对照仍按 CONTROLS）
#   tools/deploy/dmax/scan_images.sh --selftest          # 本机以合成归档自测扫描与判定逻辑（不连接 dmax）
# 环境变量：
#   PRODUCT、OPTIONAL、CONTROLS  镜像表（CONTROLS 项为 <镜像>=<术语组>，术语组 nvdiffrast 或 fast-livo2）
#   RUNTIME=d3-sensorsvc:dev     扫描容器镜像（python3 ≥ 3.8）
#   TASK_ID=main                 实例名：容器 d3-test-imgscan-<id>-<k>[-pip|-src]、d3-test-imgscan-<id>-sum
#   CPUS=2 MEM=2g                扫描容器资源上限（镜像内清单与汇总容器 1 核、1 GB）；全部容器在 CPU 池（d3.slice），不挂载 GPU
#   OUT_LOCAL                    本机副本目录（缺省 var/scan_images/<tag>）；PY 本机自测用的解释器（缺省 .venv/bin/python 或 python3）
# 退出码：0 通过，1 有不通过项，2 参数错误。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
PRODUCT=${PRODUCT-d3-gz:dev d3-sensorsvc:dev d3-agent-py:dev}
OPTIONAL=${OPTIONAL-d3-trivy:dev}
CONTROLS=${CONTROLS-d3-livo:dev=fast-livo2 d3-r3-camera-render:v1=nvdiffrast}
RUNTIME=${RUNTIME:-d3-sensorsvc:dev}
TASK_ID=$(printf '%s' "${TASK_ID:-main}" | tr 'A-Z' 'a-z')
CPUS=${CPUS:-2}
MEM=${MEM:-2g}
case "$TASK_ID" in ""|*[!a-z0-9._-]*) echo "TASK_ID must match [a-z0-9._-]+ (got '$TASK_ID')" >&2; exit 2;; esac
case "$CPUS$MEM" in *[!0-9.a-zA-Z]*) echo "CPUS and MEM must be plain docker sizes" >&2; exit 2;; esac

PYF=$(mktemp --suffix=.py)
trap 'rm -f "$PYF"' EXIT
cat > "$PYF" <<'PY'
"""image_scan：产品镜像清单扫描器（tools/deploy/dmax/scan_images.sh 生成；D3-AC-24、M25-AC-019；AWR-05 §14）。

子命令：
  inside     在被扫描镜像内以其自身解释器运行：pip list、importlib.metadata 发行包清单、研究限定模块的可导入性（find_spec）
  tar        读取标准输入的 `docker export` 归档流：路径段、dist-info / egg-info 元数据、文件内容逐项匹配，另写文件清单 TSV
  summarize  按计划表合并各镜像记录，按角色判定，写 summary.json 与 summary.txt，退出码 0 通过、1 不通过
  selftest   以合成归档自测 tar、inside 记录与 summarize 的判定
语法保持 Python 3.8 可用（inside 在 ROS Noetic 等旧镜像内运行）。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time

SCHEMA = "awr.d3.image_scan.v1"
# 术语组 → (路径段与发行包名正则（小写文本），内容正则（小写字节）)；FAST-LIVO2 组含其依赖 rpg_vikit 与主类 LIVMapper
TERMS = {
    "nvdiffrast": (re.compile(r"nvdiffrast"), re.compile(rb"nvdiffrast")),
    "fast-livo2": (re.compile(r"fast[-_]?livo|rpg[-_]vikit|vikit[-_](?:common|ros|py)|livmapper"),
                   re.compile(rb"fast[-_]?livo|rpg[-_]vikit|vikit[-_](?:common|ros|py)|livmapper")),
}
PREFILTER = (b"nvdiff", b"livo", b"vikit", b"livmap")          # 内容预筛的字面量（bytes.find），命中再以正则复核
IMPORT = re.compile(r"^[ \t]*(?:import|from)[ \t]+(nvdiffrast)\b", re.M)
PY_EXT = (".py", ".pyx", ".pyi")
CHUNK = 4 << 20
OVERLAP = 64
SMALL = 4 << 20                                                # 不超过此大小的 Python 源码与发行包元数据整体读入
MAX_EXAMPLES = 40                                              # 每个（术语组, 类别）保留的命中样例数；计数不截断
LEVEL = {"package": "error", "path": "error", "binary": "error", "import": "error", "mention": "warn"}
META_FILE = re.compile(r"(?:^|/)([^/]+)\.(?:dist-info/METADATA|egg-info/PKG-INFO|egg-info)$")
SKIP_TREES = ("proc/", "sys/", "dev/")                         # docker export 中的伪文件系统挂载点（通常为空目录）


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm(name: str) -> str:
    """发行包名按 PEP 503 归一。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _meta(text: str) -> tuple:
    """METADATA / PKG-INFO 头部的 (Name, Version)。"""
    name = ver = ""
    for ln in text.splitlines():
        if not ln.strip():
            break
        if ln.startswith("Name:") and not name:
            name = ln[5:].strip()
        elif ln.startswith("Version:") and not ver:
            ver = ln[8:].strip()
    return name, ver


def name_terms(name: str) -> list:
    """发行包名（或模块名）命中的术语组。"""
    n = _norm(name)
    return [t for t, (rx, _) in TERMS.items() if rx.search(n) or rx.search(n.replace("-", "_"))]


def path_terms(path: str) -> list:
    """路径中任一段命中的术语组（小写匹配）。"""
    segs = [s for s in path.lower().split("/") if s]
    return [t for t, (rx, _) in TERMS.items() if any(rx.search(s) for s in segs)]


class Hits:
    """命中表：每个（术语组, 类别）计数并保留至多 MAX_EXAMPLES 个样例。"""

    def __init__(self) -> None:
        self.counts: dict = {}
        self.items: list = []

    def add(self, term: str, kind: str, path: str, detail: str = "") -> None:
        key = term + "/" + kind
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] <= MAX_EXAMPLES:
            self.items.append({"term": term, "kind": kind, "level": LEVEL[kind], "path": path, "detail": detail[:160]})

    def errors(self) -> int:
        return sum(n for k, n in self.counts.items() if LEVEL[k.split("/", 1)[1]] == "error")


def _content_terms(buf: bytes) -> dict:
    """小写字节块中命中的术语组 → 首个匹配片段。"""
    low = buf.lower()
    if not any(low.find(p) >= 0 for p in PREFILTER):
        return {}
    out = {}
    for t, (_, rx) in TERMS.items():
        m = rx.search(low)
        if m:
            out[t] = m.group(0).decode("latin1")
    return out


def _file_kind(head: bytes, path: str) -> str:
    if head[:4] == b"\x7fELF" or head[:8] == b"!<arch>\n":
        return "binary"
    if path.endswith(PY_EXT):
        return "python"
    return "other"


class _Tap:
    """读流包装：计数并保留最近几块原始字节（不复制拼接），用于确认归档以全零头块结束。

    流式 tarfile 在头块边界处截断时与正常结束一样静默停止迭代，须以结束标记区分完整与截断的 `docker export` 流。"""

    KEEP = 8

    def __init__(self, f) -> None:
        self.f, self.n, self.recent = f, 0, []

    def read(self, size: int = -1) -> bytes:
        b = self.f.read(size)
        if b:
            self.n += len(b)
            self.recent.append(b)
            if len(self.recent) > self.KEEP:
                del self.recent[0]
        return b

    def zero_block_at(self, off: int) -> bool:
        """偏移 off 处的 512 字节已读入且全为零。"""
        win = b"".join(self.recent)
        i = off - (self.n - len(win))
        blk = win[i:i + tarfile.BLOCKSIZE] if i >= 0 else b""
        return len(blk) == tarfile.BLOCKSIZE and blk.count(0) == tarfile.BLOCKSIZE


def scan_stream(stream, files_out=None) -> dict:
    """扫描 `docker export` 的 tar 流 → 记录（counts、distributions、hits、complete）；files_out 为文本流时逐行写文件清单。"""
    hits = Hits()
    counts = {"members": 0, "files": 0, "dirs": 0, "symlinks": 0, "hardlinks": 0, "other": 0, "bytes": 0, "binary_files": 0,
              "python_files": 0}
    dists: list = []
    complete, error = False, None
    sha = hashlib.sha256()
    lines = 0
    tap = _Tap(stream)
    try:
        with tarfile.open(fileobj=tap, mode="r|") as tf:
            for m in tf:
                counts["members"] += 1
                name = m.name[2:] if m.name.startswith("./") else m.name
                path = "/" + name.lstrip("/")
                rel = path[1:]
                if m.isdir():
                    counts["dirs"] += 1
                    typ = "d"
                elif m.issym():
                    counts["symlinks"] += 1
                    typ = "l"
                elif m.islnk():
                    counts["hardlinks"] += 1
                    typ = "h"
                elif m.isreg():
                    counts["files"] += 1
                    typ = "f"
                else:
                    counts["other"] += 1
                    typ = "o"
                row = f"{path}\t{typ}\t{m.size if typ == 'f' else 0}\t{m.mode & 0o7777:04o}\n"
                sha.update(row.encode("utf-8", "surrogateescape"))
                lines += 1
                if files_out is not None:
                    files_out.write(row)
                if rel.startswith(SKIP_TREES):
                    continue
                for t in path_terms(path):
                    hits.add(t, "path", path, "type=" + typ)
                if typ in ("l", "h"):
                    for t in path_terms(m.linkname):
                        hits.add(t, "path", path, "link -> " + m.linkname)
                    continue
                if typ != "f":
                    continue
                counts["bytes"] += m.size
                _scan_file(tf.extractfile(m), path, m.size, hits, dists, counts)
            end = tf.offset
        complete = True
    except (tarfile.TarError, EOFError, OSError) as e:
        error = f"{type(e).__name__}: {e}"
    if complete and counts["members"] == 0:
        error, complete = "empty archive", False
    elif complete and not tap.zero_block_at(end):
        error, complete = "archive truncated (no end-of-archive marker)", False
    return {"complete": complete, "error": error, "counts": counts, "distributions": dists, "hits": hits.items,
            "hit_counts": hits.counts, "errors": hits.errors(), "files_list": {"lines": lines, "sha256": sha.hexdigest()}}


def _scan_file(f, path: str, size: int, hits: Hits, dists: list, counts: dict) -> None:
    """逐块读取一个常规文件：内容匹配、发行包元数据与 Python 导入。"""
    head = f.read(min(size, CHUNK))
    kind = _file_kind(head, path)
    if kind == "binary":
        counts["binary_files"] += 1
    elif kind == "python":
        counts["python_files"] += 1
    meta = META_FILE.search(path)
    whole = head if (size <= SMALL and (kind == "python" or meta)) else None
    found: dict = {}
    buf = head
    while True:
        for t, s in _content_terms(buf).items():
            found.setdefault(t, s)
        nxt = f.read(CHUNK)
        if not nxt:
            break
        if whole is not None:
            whole += nxt
        buf = buf[-OVERLAP:] + nxt
    if meta and whole is not None:
        name, ver = _meta(whole.decode("utf-8", "replace"))
        if name:
            dists.append({"name": name, "version": ver, "path": path})
            for t in name_terms(name):
                hits.add(t, "package", path, f"{name} {ver}")
    for t, s in found.items():
        if kind == "binary":
            hits.add(t, "binary", path, f"contains {s!r}")
        elif kind == "python" and whole is not None and t == "nvdiffrast" and IMPORT.search(whole.decode("utf-8", "replace")):
            hits.add(t, "import", path, "imports nvdiffrast")
        else:
            hits.add(t, "mention", path, f"contains {s!r}")


# ==================================================================== 子命令


def cmd_tar(a) -> int:
    t0 = time.monotonic()
    rec = {"schema": SCHEMA, "kind": "tar", "ref": a.ref, "image_id": a.image_id, "role": a.role, "expect": a.expect or None,
           "started_utc": _utc(), "scanner": {"python": sys.version.split()[0]}}
    if a.files:
        with gzip.open(a.files, "wt", encoding="utf-8", errors="surrogateescape", newline="") as gz:
            rec.update(scan_stream(sys.stdin.buffer, gz))
    else:
        rec.update(scan_stream(sys.stdin.buffer))
    rec["elapsed_s"] = round(time.monotonic() - t0, 3)
    _write(a.out, rec)
    state = "complete" if rec["complete"] else f"INCOMPLETE ({rec['error']})"
    print(f"[scan] {a.ref}: {state}, {rec['counts']['members']} members, {rec['counts']['bytes']} bytes, {rec['errors']} error hits")
    return 0 if rec["complete"] else 2


def inside_record() -> dict:
    """当前解释器视角的清单：pip list、importlib.metadata、研究限定模块可导入性。"""
    import importlib.util
    try:
        import importlib.metadata as md
    except ImportError:                                        # Python < 3.8：只记 pip
        md = None
    hits = Hits()
    rec = {"schema": SCHEMA, "kind": "inside", "python": sys.version.split()[0], "executable": sys.executable, "sys_path": sys.path[1:]}
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "list", "--format", "json", "--disable-pip-version-check"],
                           capture_output=True, timeout=300, check=False)
        pk = json.loads(r.stdout.decode("utf-8", "replace") or "null") if r.returncode == 0 else None
        rec["pip"] = {"ok": pk is not None, "rc": r.returncode, "packages": pk or [],
                      "stderr": r.stderr.decode("utf-8", "replace")[-300:] if r.returncode else ""}
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        rec["pip"] = {"ok": False, "rc": None, "packages": [], "stderr": str(e)[-300:]}
    dists = []
    if md is not None:
        for d in md.distributions():
            try:
                dists.append({"name": d.metadata["Name"] or "", "version": d.version or "", "path": str(d.locate_file(""))})
            except Exception as e:  # 损坏的元数据只记录，不中断
                dists.append({"name": "", "version": "", "path": "", "error": str(e)[:160]})
    rec["distributions"] = dists
    for p in rec["pip"]["packages"]:
        for t in name_terms(p.get("name", "")):
            hits.add(t, "package", "pip list", f"{p.get('name')} {p.get('version')}")
    for d in dists:
        for t in name_terms(d["name"]):
            hits.add(t, "package", d["path"] or "importlib.metadata", f"{d['name']} {d['version']}")
    imp = {}
    for mod in ("nvdiffrast",):
        try:
            spec = importlib.util.find_spec(mod)
        except (ImportError, ValueError):
            spec = None
        imp[mod] = getattr(spec, "origin", None) if spec else None
        if spec:
            hits.add("nvdiffrast", "import", imp[mod] or mod, f"module {mod} is importable")
    rec["importable"] = imp
    rec["hits"], rec["hit_counts"], rec["errors"] = hits.items, hits.counts, hits.errors()
    return rec


def cmd_inside(a) -> int:
    rec = inside_record()
    _write(a.out, rec)
    pip = "ok" if rec["pip"]["ok"] else "unavailable"
    print(f"[inside] python {rec['python']}: pip {pip} ({len(rec['pip']['packages'])} packages), "
          f"{len(rec['distributions'])} distributions, {rec['errors']} error hits")
    return 0


def _load(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path: str, rec: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def judge(entry: dict, tar, inside, inspect, rc) -> dict:
    """一个镜像的判定 → 记录（status：pass、fail、skipped；reasons 列出不通过原因）。"""
    role, expect = entry["role"], entry.get("expect") or None
    out = {"k": entry["k"], "ref": entry["ref"], "role": role, "expect": expect, "status": "fail", "reasons": []}
    if inspect is None and tar is None:
        if role == "optional":
            out.update(status="skipped", reasons=["image not present on host"])
        else:
            out["reasons"].append("image not present on host" if role == "product" else "control image not present; scanner cannot be validated")
        return out
    info = (inspect or [{}])[0] if isinstance(inspect, list) else (inspect or {})
    out["image"] = {"id": info.get("Id") or (tar or {}).get("image_id"), "repo_digests": info.get("RepoDigests") or [],
                    "created": info.get("Created"), "size": info.get("Size"),
                    "labels": (info.get("Config") or {}).get("Labels") or {}}
    if rc is not None:
        out["export_rc"], out["scan_rc"] = rc
    if tar is None:
        out["reasons"].append("file scan record missing")
    elif not tar.get("complete"):
        out["reasons"].append(f"file scan incomplete: {tar.get('error')}")
    if rc is not None and rc[0] != 0:
        out["reasons"].append(f"docker export exited {rc[0]}")
    hits = list((tar or {}).get("hits") or []) + [dict(h, source="inside") for h in ((inside or {}).get("hits") or [])]
    counts: dict = {}
    for src in (tar, inside):
        for k, n in ((src or {}).get("hit_counts") or {}).items():
            counts[k] = counts.get(k, 0) + n
    out["hit_counts"] = counts
    out["errors"] = sum(n for k, n in counts.items() if LEVEL[k.split("/", 1)[1]] == "error")
    out["warnings"] = sum(n for k, n in counts.items() if LEVEL[k.split("/", 1)[1]] == "warn")
    hits.sort(key=lambda h: h["level"] != "error")              # 错误级在前（稳定排序，保持扫描顺序）
    out["hits"] = hits[:2 * MAX_EXAMPLES]
    if tar:
        out["counts"] = tar.get("counts")
        out["files_list"] = tar.get("files_list")
        out["distributions"] = len(tar.get("distributions") or [])
        out["research_distributions"] = [d for d in tar.get("distributions") or [] if name_terms(d["name"])]
    if inside is not None:
        pip = inside.get("pip") or {}
        out["inside"] = {"python": inside.get("python"), "pip_ok": pip.get("ok"), "pip_packages": len(pip.get("packages") or []),
                         "distributions": len(inside.get("distributions") or []), "importable": inside.get("importable")}
    else:
        out["inside"] = {"python": None, "note": "no python3 interpreter in the image (or it failed); the file scan is authoritative"}
    if role == "control":
        got = sum(n for k, n in counts.items() if k.startswith((expect or "?") + "/") and LEVEL[k.split("/", 1)[1]] == "error")
        out["detected"] = got
        if not got:
            out["reasons"].append(f"control image: expected {expect} artefacts were not detected (scanner is not effective)")
    elif out["errors"]:
        terms = sorted({k.split("/", 1)[0] for k, n in counts.items() if LEVEL[k.split("/", 1)[1]] == "error" and n})
        out["reasons"].append(f"research-only or GPL artefacts found: {', '.join(terms)}")
    if not out["reasons"]:
        out["status"] = "pass"
    return out


def parse_plan(plan: str) -> list:
    out = []
    for item in plan.split():
        k, ref, role, expect = ([*item.split("|"), "", "", "", ""])[:4]
        out.append({"k": k, "ref": ref, "role": role, "expect": expect})
    return out


def summarize(d: str, plan: list, meta: dict) -> dict:
    images = []
    for e in plan:
        k = e["k"]
        rc = None
        try:
            with open(os.path.join(d, k + ".rc"), encoding="utf-8") as f:
                v = [int(x) for x in f.read().split()]
            rc = ([*v, None, None])[:2]
        except (OSError, ValueError):
            pass
        images.append(judge(e, _load(os.path.join(d, k + ".tar.json")), _load(os.path.join(d, k + ".inside.json")),
                            _load(os.path.join(d, k + ".inspect.json")), rc))
    scanned = [x for x in images if x["status"] != "skipped"]
    controls = [x for x in images if x["role"] == "control"]
    rep = {"schema": SCHEMA, "kind": "summary", "generated_utc": _utc(), **meta,
           "acceptance": "D3-AC-24 (product images contain no nvdiffrast); M25-AC-019; AWR-05 §14 (FAST-LIVO2 only as a separate container)",
           "terms": {t: rx.pattern for t, (rx, _) in TERMS.items()}, "levels": LEVEL, "images": images,
           "summary": {"images": len(images), "pass": sum(x["status"] == "pass" for x in images),
                       "fail": sum(x["status"] == "fail" for x in images), "skipped": sum(x["status"] == "skipped" for x in images),
                       "controls": len(controls), "controls_detected": sum(x["status"] == "pass" for x in controls),
                       "product_error_hits": sum(x.get("errors", 0) for x in images if x["role"] != "control")},
           "pass": bool(scanned) and all(x["status"] == "pass" for x in scanned)}
    return rep


def render_text(rep: dict) -> str:
    dirty = " dirty" if rep.get("dirty") else ""
    rows = [f"image scan {rep.get('tag')}  ({rep['generated_utc']}, sha {rep.get('sha')}{dirty})"]
    for x in rep["images"]:
        c = x.get("counts") or {}
        pip = (x.get("inside") or {}).get("pip_packages", "-")
        rows.append(f"{x['role']:<8} {x['ref']:<30} {x['status'].upper():<7} files={c.get('files', '-')!s:<7} "
                    f"bytes={c.get('bytes', '-')!s:<11} dists={x.get('distributions', '-')!s:<4} pip={pip!s:<4} "
                    f"errors={x.get('errors', '-')!s:<5} warnings={x.get('warnings', '-')!s:<4} {'; '.join(x['reasons'])}")
        rows += [f"           {h['level']:<5} {h['term']:<10} {h['kind']:<8} {h['path']}  {h['detail']}" for h in (x.get("hits") or [])[:6]]
    s = rep["summary"]
    rows.append(f"image scan: {'PASS' if rep['pass'] else 'FAIL'} ({s['images']} images: {s['pass']} pass, {s['fail']} fail, "
                f"{s['skipped']} skipped; controls detected {s['controls_detected']}/{s['controls']}; "
                f"product error hits {s['product_error_hits']})")
    return "\n".join(rows) + "\n"


def cmd_summarize(a) -> int:
    meta = {"tag": a.tag, "sha": a.sha, "dirty": a.dirty == "1", "started_utc": a.started, "runtime_image": a.runtime or None}
    rep = summarize(a.dir, parse_plan(a.plan), meta)
    _write(os.path.join(a.dir, "summary.json"), rep)
    txt = render_text(rep)
    with open(os.path.join(a.dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    sys.stdout.write(txt)
    return 0 if rep["pass"] else 1


# ==================================================================== 自测


def _tar(entries: list, truncate: bool = False) -> io.BytesIO:
    """合成归档：entries 为 (路径, 内容 bytes | None 目录 | ('sym', 目标))。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for path, data in entries:
            ti = tarfile.TarInfo(path)
            if data is None:
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(ti)
            elif isinstance(data, tuple):
                ti.type, ti.linkname = tarfile.SYMTYPE, data[1]
                tf.addfile(ti)
            else:
                ti.size, ti.mode = len(data), 0o644
                tf.addfile(ti, io.BytesIO(data))
    b = raw.getvalue()
    return io.BytesIO(b[: len(b) // 2] if truncate else b)


def _meta_bytes(name: str, ver: str) -> bytes:
    return (f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n\nlong description\n").encode()


def selftest() -> int:
    nv = "nvdiff" + "rast"                                     # 拼接，避免本文件自身成为提及样例
    elf = b"\x7fELF\x02\x01\x01" + b"\0" * 64 + b"libc.so.6\0libvikit_common.so\0_ZN9LIVMapper3runEv\0"
    big = b"\x7fELF" + b"\0" * (CHUNK - 8) + nv.upper().encode() + b"\0" * 100   # 匹配跨越分块边界
    checks = []

    def case(label, entries, want_err, want_kinds=(), truncate=False, want_complete=True):
        r = scan_stream(_tar(entries, truncate))
        kinds = set(r["hit_counts"])
        ok = (r["errors"] > 0) == want_err and set(want_kinds) <= kinds and r["complete"] == want_complete
        checks.append((label, ok, r["hit_counts"], r["error"]))
        return r

    clean = [("usr", None), ("usr/lib/python3/site-packages/numpy-2.1.0.dist-info/METADATA", _meta_bytes("numpy", "2.1.0")),
             ("usr/lib/python3/site-packages/numpy/core.py", b"import math\n"), ("usr/bin/tool", b"\x7fELF\x02" + b"\0" * 200),
             ("proc", None), ("usr/share/doc/a.txt", (f"compatible with {nv} renderers\n").encode())]
    r = case("clean image with a doc mention", clean, False, ["nvdiffrast/mention"])
    checks.append(("clean image lists distributions", [d["name"] for d in r["distributions"]] == ["numpy"], r["distributions"], None))
    case("nvdiffrast dist-info", [*clean, (f"usr/local/lib/python3.10/dist-packages/{nv}-0.4.0.dist-info/METADATA",
                                           _meta_bytes(nv, "0.4.0"))], True, ["nvdiffrast/package", "nvdiffrast/path"])
    case("nvdiffrast egg-info file", [*clean, (f"usr/lib/python3/dist-packages/{nv.replace('rast', 'Rast')}-0.3.egg-info",
                                               _meta_bytes(nv, "0.3"))], True, ["nvdiffrast/package"])
    case("product module imports nvdiffrast", [*clean, ("opt/awr/python/awr/render.py", (f"x = 1\nfrom {nv} import torch as dr\n").encode())],
         True, ["nvdiffrast/import"])
    case("FAST-LIVO2 source tree", [*clean, ("ws/src/FAST-LIVO2/CMakeLists.txt", b"project(fast_livo)\n")], True, ["fast-livo2/path"])
    case("ELF linked against vikit", [*clean, ("opt/x/lib/libnav.so", elf)], True, ["fast-livo2/binary"])
    case("ELF string across chunk boundary", [*clean, ("opt/x/lib/libbig.so", big)], True, ["nvdiffrast/binary"])
    case("symlink into a research tree", [*clean, ("usr/lib/libv.so", ("sym", "/ws/devel/lib/libvikit_common.so"))], True,
         ["fast-livo2/path"])
    case("truncated export stream", clean * 30, False, (), truncate=True, want_complete=False)
    case("empty export stream", [], False, (), want_complete=False)

    with tempfile.TemporaryDirectory(prefix="d3-imgscan-") as td:
        def put(k, tar=None, inside=None, inspect=None, rc=None):
            if tar is not None:
                _write(os.path.join(td, k + ".tar.json"), dict(tar, complete=tar.get("complete", True)))
            if inside is not None:
                _write(os.path.join(td, k + ".inside.json"), inside)
            if inspect is not None:
                _write(os.path.join(td, k + ".inspect.json"), inspect)
            if rc is not None:
                with open(os.path.join(td, k + ".rc"), "w", encoding="utf-8") as f:
                    f.write(rc)
        good = scan_stream(_tar(clean))
        bad = scan_stream(_tar([*clean, (f"usr/lib/python3/site-packages/{nv}/__init__.py", b"")]))
        livo = scan_stream(_tar([*clean, ("ws/devel/lib/fast_livo/fastlivo_mapping", elf)]))
        insp = [{"Id": "sha256:" + "0" * 64, "RepoDigests": [], "Created": "2026-10-18T00:00:00Z", "Size": 1, "Config": {"Labels": None}}]
        inside_bad = {"hits": [{"term": "nvdiffrast", "kind": "package", "level": "error", "path": "pip list", "detail": nv}],
                      "hit_counts": {"nvdiffrast/package": 1}, "pip": {"ok": True, "packages": [{"name": nv}]}}
        put("1", good, None, insp, "0 0")
        put("2", livo, None, insp, "0 0")
        base = [{"k": "1", "ref": "p:dev", "role": "product", "expect": ""}, {"k": "2", "ref": "c:dev", "role": "control", "expect": "fast-livo2"},
                {"k": "9", "ref": "o:dev", "role": "optional", "expect": ""}]
        rep = summarize(td, base, {})
        checks.append(("summary passes clean product + detected control + absent optional",
                       rep["pass"] and [x["status"] for x in rep["images"]] == ["pass", "pass", "skipped"], rep["summary"], None))
        put("3", bad, None, insp, "0 0")
        rep = summarize(td, [*base, {"k": "3", "ref": "q:dev", "role": "product", "expect": ""}], {})
        checks.append(("summary fails a product image with a research path", not rep["pass"] and rep["images"][3]["status"] == "fail",
                       rep["images"][3]["reasons"], None))
        put("4", good, inside_bad, insp, "0 0")
        rep = summarize(td, [{"k": "4", "ref": "r:dev", "role": "product", "expect": ""}], {})
        checks.append(("summary fails on an in-image pip list hit", not rep["pass"], rep["images"][0]["reasons"], None))
        rep = summarize(td, [{"k": "1", "ref": "c2:dev", "role": "control", "expect": "nvdiffrast"}], {})
        checks.append(("summary fails a control that detects nothing", not rep["pass"], rep["images"][0]["reasons"], None))
        rep = summarize(td, [{"k": "8", "ref": "m:dev", "role": "product", "expect": ""}], {})
        checks.append(("summary fails an absent product image", not rep["pass"], rep["images"][0]["reasons"], None))
        put("5", good, None, insp, "1 0")
        rep = summarize(td, [{"k": "5", "ref": "e:dev", "role": "product", "expect": ""}], {})
        checks.append(("summary fails a failed docker export", not rep["pass"], rep["images"][0]["reasons"], None))
        rep = summarize(td, [{"k": "9", "ref": "o:dev", "role": "optional", "expect": ""}], {})
        checks.append(("summary with nothing scanned does not pass", not rep["pass"], rep["summary"], None))
        txt = render_text(summarize(td, base, {"tag": "selftest"}))
        checks.append(("text summary renders", "image scan: PASS" in txt, txt.splitlines()[-1], None))
    me = inside_record()
    checks.append(("inside record of this interpreter", me["kind"] == "inside" and isinstance(me["distributions"], list), me["errors"], None))
    bad_n = 0
    for label, ok, info, err in checks:
        bad_n += not ok
        print(("ok    " if ok else "FAIL  ") + label + ("" if ok else f"  -> {info} {err or ''}"))
    print(f"selftest: {'FAIL' if bad_n else 'PASS'} ({len(checks)} checks, {bad_n} failed)")
    return 1 if bad_n else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="D3 product image manifest scan (D3-AC-24)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("tar")
    p.add_argument("--ref", required=True)
    p.add_argument("--image-id", default="")
    p.add_argument("--role", default="product", choices=("product", "optional", "control"))
    p.add_argument("--expect", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--files", default="")
    p = sub.add_parser("inside")
    p.add_argument("--out", required=True)
    p = sub.add_parser("summarize")
    p.add_argument("--dir", required=True)
    p.add_argument("--plan", required=True)
    for k in ("tag", "sha", "dirty", "started", "runtime"):
        p.add_argument("--" + k, default="")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    fn = {"tar": cmd_tar, "inside": cmd_inside, "summarize": cmd_summarize, "selftest": lambda _a: selftest()}.get(a.cmd)
    if fn is None:
        ap.print_help()
        return 2
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
PY

if [ "${1:-}" = --selftest ]; then
  PY=${PY:-$ROOT/.venv/bin/python}
  [ -x "$PY" ] || PY=python3
  exec "$PY" -I "$PYF" selftest
fi

# ---------------------------------------------------------------- 镜像表：<k>|<镜像>|<角色>|<期望术语组>
if [ $# -gt 0 ]; then PRODUCT="$*" OPTIONAL=""; fi
ENTRIES=()
for x in $PRODUCT; do ENTRIES+=("$x|product|"); done
for x in $OPTIONAL; do ENTRIES+=("$x|optional|"); done
for x in $CONTROLS; do
  case "$x" in *=nvdiffrast|*=fast-livo2) ENTRIES+=("${x%=*}|control|${x##*=}");;
    *) echo "CONTROLS item must be <image>=nvdiffrast|fast-livo2 (got '$x')" >&2; exit 2;; esac
done
[ ${#ENTRIES[@]} -gt 0 ] || { echo "no images to scan" >&2; exit 2; }
for e in "${ENTRIES[@]}" "$RUNTIME||"; do
  case "${e%%|*}" in ""|*[!A-Za-z0-9._/:@-]*) echo "invalid image reference '${e%%|*}'" >&2; exit 2;; esac
done

# ---------------------------------------------------------------- 标签与产物目录
cd "$ROOT"
if [ ! -e .git ]; then export GIT_DIR=${GIT_DIR:-/data/projs/.anet-drone4d.git} GIT_WORK_TREE=${GIT_WORK_TREE:-$ROOT}; fi
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if command -v git >/dev/null 2>&1 && git rev-parse --verify -q HEAD >/dev/null 2>&1; then
  SHA=$(git rev-parse --short=12 HEAD)
  DIRTY=0; [ -z "$(git status --porcelain -- tools/deploy/dmax/scan_images.sh)" ] || DIRTY=1   # 有改动或未入库
else
  SHA=nogit DIRTY=1
fi
TAG=$(date -u +%Y%m%dT%H%M%SZ)-$SHA$([ "$DIRTY" = 1 ] && echo -dirty || true)
OUT=$D/out/scan_images/$TAG
OUT_LOCAL=${OUT_LOCAL:-$ROOT/var/scan_images/$TAG}
N=d3-test-imgscan-$TASK_ID
echo "[scan-images] tag $TAG  images: ${ENTRIES[*]}  runtime $RUNTIME"
rsync -a --mkpath "$PYF" "dmax:$OUT/bin/image_scan.py"

# ---------------------------------------------------------------- 远端：逐镜像 inspect、镜像内清单、导出流扫描；最后汇总
# 容器：CPU 池（d3.slice），不联网、只读根文件系统、宿主用户身份；被扫描镜像的导出用 docker create（不启动）
CT="--network none --read-only --tmpfs /tmp --cgroup-parent d3.slice --user \$(id -u):\$(id -g) -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -v $OUT/bin:/scan:ro -v $OUT:/out"
R="set -o pipefail; mkdir -p $OUT; "
PLAN=""
k=0
for e in "${ENTRIES[@]}"; do
  k=$((k + 1))
  IFS='|' read -r ref role exp <<< "$e"
  PLAN="$PLAN $k|$ref|$role|$exp"
  n=$N-$k
  R="$R docker rm -f $n $n-pip $n-src >/dev/null 2>&1; \
if docker image inspect $ref >/dev/null 2>&1; then \
I=\$(docker image inspect -f '{{.Id}}' $ref); I=\${I:-$ref}; docker image inspect \$I > $OUT/$k.inspect.json; \
docker run --rm --name $n-pip $CT --cpus 1 --memory 1g --entrypoint python3 \$I /scan/image_scan.py inside --out /out/$k.inside.json; \
r=\$?; echo \$r > $OUT/$k.inside.rc; [ \$r = 0 ] || echo \"[inside] $ref: exited \$r (no python3 in the image?); the file scan is authoritative\"; \
C=\$(docker create --name $n-src --network none \$I /nonexistent); \
docker export \$C | docker run -i --rm --name $n $CT --cpus $CPUS --memory $MEM $RUNTIME python3 -I /scan/image_scan.py tar \
  --ref $ref --image-id \$I --role $role --expect '$exp' --out /out/$k.tar.json --files /out/$k.files.tsv.gz; \
echo \${PIPESTATUS[*]} > $OUT/$k.rc; docker rm -f $n-src >/dev/null 2>&1; \
else echo '[scan] $ref: not present'; fi; "
done
R="$R RT=\$(docker image inspect -f '{{.Id}}' $RUNTIME 2>/dev/null); docker rm -f $N-sum >/dev/null 2>&1; \
docker run --rm --name $N-sum $CT --cpus 1 --memory 1g $RUNTIME python3 -I /scan/image_scan.py summarize --dir /out \
  --plan '${PLAN# }' --tag $TAG --sha $SHA --dirty $DIRTY --started $STARTED --runtime \"$RUNTIME \$RT\""
set +e
ssh dmax "$R"
RC=$?
set -e
rsync -a --mkpath "dmax:$OUT/" "$OUT_LOCAL/"
echo "[scan-images] results: dmax:$OUT  local: $OUT_LOCAL"
exit $RC
