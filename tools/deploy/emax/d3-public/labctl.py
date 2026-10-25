#!/usr/bin/env python3
"""tools/deploy/emax/d3-public/labctl.py：公开样例发布线（扩展点 16；AWR-05 §7.9、AWR-17 §18.8.5、M27-FR-134、FR-138；D3-AC-21）。

emax 上的目录（缺省 `--root /srv/drone4d`；nginx 只读，见 lab-server.conf）：

    lab/<build>/               /lab/ 独立静态构建（build-info.json 的 release 为目录名）
    lab/current -> <build>     当前构建（符号链接，原子替换）
    lab/.staging/<build>/      push 写入、activate 移入
    lab-static/index.json      当前版本的 drone4d.lab_static_index.v1（no-cache；写临时文件后 rename）
    lab-static/v/<ver>/        版本目录（YYYY.MM.DD-n，immutable）；其中 .index.json 为该版本的索引（不直出，回滚时复制回 index.json）
    lab-static/.staging/<ver>/ push 写入（rsync --link-dest 上一版本，未变的文件为硬链接）、activate 校验后移入 v/
    .state/state.json          当前与上一版本、当前与上一构建、站点模式（lab | d2）
    .state/history.jsonl       每次 activate、rollback、enable-site、withdraw 的记录（UTC、耗时、操作者）
    .state/d2-site.conf        enable-site 时保存的 D2 站点文件（withdraw 恢复它）
    bin/                       push 一并同步的本目录脚本与 scan_public.py（emax 上的 D2 发布不含 D3 工具）

子命令（退出码 0 成功、1 校验不通过或操作失败、2 参数错误）：

    index    --dir VDIR --draft DRAFT [--slices slices.json …] [--lab-build B]   开发机或 dmax：生成 SHA256SUMS 与 VDIR/.index.json
    check    --dir VDIR [--lab-dist DIST] [--test-ids FILE]                       任意机器：发布前与激活前的内容校验
    push     --static VDIR --lab-dist DIST --dest [HOST:]ROOT                     dmax → emax 单向 rsync 到 .staging（--link-dest）
    activate --version V [--lab-build B]                                          emax：校验 staging、移入、原子切换、保留 2 个版本
    rollback [--to V]                                                             emax：恢复上一版本的 index.json 与 /lab/ 构建
    enable-site --site-file F                                                     emax：安装"D2 + /lab/"站点文件（nginx -t 后 reload）
    withdraw --site-file F [--d2-release R]                                       emax：撤下 /lab/，恢复 D2 站点文件（回滚到 D2）
    status | budget                                                               emax：当前状态、磁盘账（Q-D3-02）
    pin      --bench-root R --index FILE                                          dmax：被引用的切片置 pinned（static:<ver>）

只用标准库；`check` 与 `activate` 的 test 集与真值扫描调用 scan_public.py（与本脚本同目录的副本，或仓库 tools/bench/d3/）。
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import labsite  # noqa: E402

SCHEMA_INDEX = "drone4d.lab_static_index.v1"
SCHEMA_STATE = "awr.deploy.d3_public.state.v1"
SCHEMA_REPORT = "awr.deploy.d3_public.labctl.v1"
DEFAULT_ROOT = Path("/srv/drone4d")
SITE_FILE = Path("/etc/nginx/sites-available/drone4d.agentnetwork.org.cn.conf")
MIB = 1 << 20
MAX_FILE_BYTES = 64 * MIB                 # 单文件 ≤ 64 MB（Q-D3-02；与 scan_public.py 同一口径 1 MB = 2^20 B）
BUDGET_BYTES = 3 * (1 << 30)               # D3 在 emax 的总占用（含保留版本，硬链接去重）≤ 3 GB
KEEP_VERSIONS = 2                          # 保留最近 2 个版本目录与构建（当前 + 上一个，回滚目标）
INDEX_IN_VERSION = ".index.json"
SUMS = "SHA256SUMS"
VERSION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}-\d+$")
BUILD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
RUN_ID_RE = re.compile(r"^run-[a-z2-7]{10}$")          # 与 apps/web/src/app/lab-public/staticIndex.ts 一致
EVENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
SAFE_SEG_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")  # 与 lab-server.conf 的直出正则一致
EXTERNAL_URL_RE = re.compile(r"^https://(www\.)?modelscope\.cn/")
#: 公开组合根不应出现的 D3 进程（AWR-05 §8.4 d3 profile 与动态容器；verify.py 共用）
TOOL_FILES = ("labctl.py", "labsite.py", "verify.py", "lab-http.conf", "lab-server.conf", "README.md")


class LabError(RuntimeError):
    """发布线错误（退出码 1）。"""


# ---------------------------------------------------------------------------------------------------------------------
# 公共


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(p: Path, chunk: int = 4 * MIB) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def walk_files(root: Path, *, skip_top: frozenset[str] = frozenset()) -> Iterator[tuple[str, Path]]:
    """root 下的全部条目（不跟随符号链接），按相对路径排序 → (相对路径, 路径)；skip_top 为跳过的顶层名。"""
    out: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if d == root:
            dirnames[:] = [x for x in dirnames if x not in skip_top]
            filenames = [x for x in filenames if x not in skip_top]
        dirnames.sort()
        for name in [*filenames, *(x for x in dirnames if (d / x).is_symlink())]:
            p = d / name
            out.append((p.relative_to(root).as_posix(), p))
    yield from sorted(out)


def unique_bytes(paths: list[Path]) -> int:
    """paths（文件或目录）下普通文件的总字节数，同一 inode 只计一次（rsync --link-dest 的硬链接去重，Q-D3-02）。"""
    seen: set[tuple[int, int]] = set()
    total = 0
    for root in paths:
        if not root.exists():
            continue
        items = [root] if root.is_file() else [p for _, p in walk_files(root)]
        for p in items:
            st = p.lstat()
            if not os.path.isfile(p) or p.is_symlink():
                continue
            k = (st.st_dev, st.st_ino)
            if k not in seen:
                seen.add(k)
                total += st.st_size
    return total


def load_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def write_json_atomic(p: Path, doc: Any, *, mode: int = 0o644) -> None:
    """写临时文件、fsync 后 rename（同目录，原子替换；17 §18.8.5 规则 2）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def replace_symlink(link: Path, target: str) -> None:
    """原子替换符号链接 link → target（先建临时链接再 rename）。"""
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


def load_scan_public() -> ModuleType:
    """scan_public.py：本目录副本（emax bin/）优先，其次仓库 tools/bench/d3/。"""
    for cand in (HERE / "scan_public.py", HERE.parents[2] / "bench" / "d3" / "scan_public.py"):
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("d3_scan_public", cand)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise LabError("找不到 scan_public.py（push 会把它同步到 <root>/bin/；仓库中位于 tools/bench/d3/）")


def build_name(dist: Path) -> str:
    """/lab/ 构建的目录名：build-info.json 的 release（vite 构建写入）。"""
    try:
        rel_ = str(load_json(dist / "build-info.json")["release"])
    except (OSError, ValueError, KeyError) as e:
        raise LabError(f"{dist}/build-info.json 缺失或无 release：先 make lab-build") from e
    if not BUILD_RE.match(rel_):
        raise LabError(f"构建名 {rel_!r} 不合法")
    return rel_


def is_safe_rel(p: str) -> bool:
    """版本目录内相对路径（与 staticIndex.ts isSafeRelPath 一致；尾部 / 表示目录）。"""
    if not p or len(p) > 512 or p.startswith("/") or "\\" in p or re.match(r"^[a-z][a-z0-9+.-]*:", p, re.I):
        return False
    segs = p.split("/")
    return all(s not in ("..", ".") and (s != "" or i == len(segs) - 1) for i, s in enumerate(segs))


# ---------------------------------------------------------------------------------------------------------------------
# index：SHA256SUMS 与 .index.json


def write_sums(vdir: Path) -> tuple[dict[str, tuple[int, str]], int]:
    """版本目录的 SHA256SUMS（sha256sum 文本格式；不含 SHA256SUMS 自身与 .index.json）→ ({rel: (bytes, sha)}, 总字节）。"""
    entries: dict[str, tuple[int, str]] = {}
    for r, p in walk_files(vdir, skip_top=frozenset({SUMS, INDEX_IN_VERSION})):
        if p.is_symlink() or not p.is_file():
            raise LabError(f"{vdir.name}/{r}：版本目录只能含普通文件（不得有符号链接）")
        entries[r] = (p.stat().st_size, sha256_file(p))
    text = "".join(f"{sha}  {r}\n" for r, (_, sha) in entries.items())
    (vdir / SUMS).write_text(text, encoding="utf-8")
    total = sum(b for b, _ in entries.values()) + len(text.encode("utf-8"))
    entries[SUMS] = (len(text.encode("utf-8")), hashlib.sha256(text.encode("utf-8")).hexdigest())
    return entries, total


def read_sums(vdir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    p = vdir / SUMS
    if not p.is_file():
        raise LabError(f"{vdir.name}：缺少 {SUMS}")
    for ln, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"^([0-9a-f]{64}) [ *](.+)$", line)
        if not m:
            raise LabError(f"{vdir.name}/{SUMS} 第 {ln} 行格式不对")
        out[m.group(2)] = m.group(1)
    return out


def merge_slices(draft: dict, manifests: list[dict]) -> None:
    """R04 `slices.json`（drone4d.bench.public_slice.v1）的 index 片段并入草稿中同一 run_id 的运行条目。"""
    runs = {r["run_id"]: r for e in draft.get("events", []) for r in e.get("runs", [])}
    for man in manifests:
        rid = man.get("run_id")
        frag = man.get("index") or {}
        if rid not in runs:
            raise LabError(f"slices.json 的 run_id {rid!r} 不在草稿的任何事件中")
        run = runs[rid]
        base = f"runs/{rid}/slices/"
        run["slices"] = [{**{k: s[k] for k in ("event_id", "t_from_s", "t_to_s") if k in s},
                          "rrd": s["rrd"] if "/" in s["rrd"] else base + s["rrd"]} for s in frag.get("slices", [])]
        if frag.get("basemap"):
            bm = frag["basemap"]
            run["basemap"] = bm if "/" in bm else base + bm


def _fill(entry: dict, key: str, sums: dict[str, tuple[int, str]], where: str, *, bytes_key: str = "bytes",
          sha_key: str = "sha256") -> None:
    path = entry.get(key)
    if path is None:
        return
    if not is_safe_rel(path):
        raise LabError(f"{where}.{key}：路径 {path!r} 不合法")
    if path in sums:
        entry[bytes_key], entry[sha_key] = sums[path]
    elif path.endswith("/"):
        sub = [b for r, (b, _) in sums.items() if r.startswith(path)]
        if not sub:
            raise LabError(f"{where}.{key}：目录 {path} 为空或不存在")
        if bytes_key == "bytes" and key == "pointcloud":
            entry["bytes"] = sum(sub)
    elif entry.get("external"):
        if bytes_key not in entry or sha_key not in entry:
            raise LabError(f"{where}.{key}：经分发渠道提供的制品须在草稿中给出 {bytes_key} 与 {sha_key}")
    else:
        raise LabError(f"{where}.{key}：版本目录中没有 {path}")


def build_index(vdir: Path, draft: dict, *, version: str | None = None, lab_build: str | None = None,
                built_unix_ns: int | None = None) -> dict:
    """草稿 + 版本目录 → 完整索引（每条制品的 bytes 与 sha256、files_bytes_total、features）；同时写出 SHA256SUMS。"""
    ver = version or vdir.name
    if not VERSION_RE.match(ver):
        raise LabError(f"版本名 {ver!r} 不合法（YYYY.MM.DD-n）")
    feats = dict(draft.get("features") or {})
    if feats.get("leaderboard") or feats.get("submit"):
        raise LabError("公开站不提供排行榜与提交（R-D3-46）：草稿中 features.leaderboard 与 features.submit 必须为 false")
    sums, total = write_sums(vdir)
    events = json.loads(json.dumps(draft.get("events") or []))
    worlds = json.loads(json.dumps(draft.get("worlds") or []))
    any_slice = any_cmp = False
    for i, e in enumerate(events):
        w = f"events[{i}]"
        _fill(e, "tape", sums, w)
        for k in ("tape_json", "brief"):
            if e.get(k) and e[k] not in sums:
                raise LabError(f"{w}.{k}：版本目录中没有 {e[k]}")
        for j, r in enumerate(e.get("runs") or []):
            wr = f"{w}.runs[{j}]"
            for k, v in enumerate(r.get("videos") or []):
                _fill(v, "path", sums, f"{wr}.videos[{k}]")
            for k, s in enumerate(r.get("slices") or []):
                _fill(s, "rrd", sums, f"{wr}.slices[{k}]")
                any_slice = True
            if r.get("basemap"):
                _fill(r, "basemap", sums, wr, bytes_key="basemap_bytes", sha_key="basemap_sha256")
            for k in ("metrics", "tracks_obs"):
                if r.get(k) and r[k] not in sums:
                    raise LabError(f"{wr}.{k}：版本目录中没有 {r[k]}")
            if r.get("lidar_compare"):
                _fill(r, "lidar_compare", sums, wr)
                any_cmp = True
    for i, wd in enumerate(worlds):
        if wd.get("pointcloud"):
            _fill(wd, "pointcloud", sums, f"worlds[{i}]")
    lic = draft.get("license") or {"files": [], "attribution": []}
    for f in lic.get("files", []):
        if f not in sums:
            raise LabError(f"license.files：版本目录中没有 {f}")
    doc: dict[str, Any] = {
        "schema": SCHEMA_INDEX, "version": ver,
        "built_unix_ns": str(built_unix_ns if built_unix_ns is not None else time.time_ns()),
        "license": {"files": list(lic.get("files", [])), "attribution": list(lic.get("attribution", []))},
        "worlds": worlds, "events": events, "files_sha256": SUMS, "files_bytes_total": total,
        "features": {"inspect": bool(feats.get("inspect", any_slice)), "compare": bool(feats.get("compare", any_cmp)),
                     "leaderboard": False, "submit": False},
    }
    if lab_build:
        if not BUILD_RE.match(lab_build):
            raise LabError(f"构建名 {lab_build!r} 不合法")
        doc["lab_build"] = lab_build
    write_json_atomic(vdir / INDEX_IN_VERSION, doc)
    return doc


# ---------------------------------------------------------------------------------------------------------------------
# check：发布前与激活前的内容校验


def _index_problems(doc: Any, vdir: Path, sums: dict[str, str], max_file: int) -> tuple[list[str], list[str]]:
    """索引的结构（契约 bench/lab_static_index.v1 的要点与前端解析器的硬性规则）与引用一致性 → (问题, 告警)。"""
    probs: list[str] = []
    warns: list[str] = []
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_INDEX:
        return [f"索引 schema 不是 {SCHEMA_INDEX}"], warns
    if doc.get("version") != vdir.name:
        probs.append(f"索引 version {doc.get('version')!r} 与目录名 {vdir.name} 不一致")
    feats = doc.get("features") or {}
    if feats.get("leaderboard") is not False or feats.get("submit") is not False:
        probs.append("features.leaderboard 与 features.submit 必须为 false（R-D3-46）")
    if doc.get("files_sha256") != SUMS:
        probs.append(f"files_sha256 应为 {SUMS}")
    total = sum((vdir / r).stat().st_size for r in sums if (vdir / r).is_file()) + (vdir / SUMS).stat().st_size
    if doc.get("files_bytes_total") != total:
        probs.append(f"files_bytes_total {doc.get('files_bytes_total')} 与实际 {total} 不一致")

    def ref(path: Any, where: str, *, bytes_: Any = None, sha: Any = None, external: Any = None, required: bool = True) -> None:
        if path is None:
            if required:
                probs.append(f"{where}：缺少路径")
            return
        if not isinstance(path, str) or not is_safe_rel(path):
            probs.append(f"{where}：路径 {path!r} 不合法")
            return
        if path.endswith("/"):
            if not any(r.startswith(path) for r in sums):
                probs.append(f"{where}：目录 {path} 为空或不存在")
            return
        if path not in sums:
            if external:
                if not isinstance(external, dict) or external.get("channel") != "modelscope" or \
                        not EXTERNAL_URL_RE.match(str(external.get("url", ""))):
                    probs.append(f"{where}：external 须为 ModelScope 链接（ADR-201）")
                elif not isinstance(bytes_, int) or bytes_ <= max_file:
                    probs.append(f"{where}：≤ 64 MB 的制品应放在版本目录中，而不是只经分发渠道")
                return
            probs.append(f"{where}：版本目录中没有 {path}")
            return
        if external:
            probs.append(f"{where}：{path} 已在版本目录中，不应再标 external")
        if not (vdir / path).is_file():
            probs.append(f"{where}：{path} 不存在")
            return
        st = (vdir / path).stat().st_size
        if bytes_ is not None and bytes_ != st:
            probs.append(f"{where}：bytes {bytes_} 与实际 {st} 不一致")
        if sha is not None and sha != sums[path]:
            probs.append(f"{where}：sha256 与 SHA256SUMS 不一致")

    for i, f in enumerate((doc.get("license") or {}).get("files") or []):
        ref(f, f"license.files[{i}]")
    if not (doc.get("license") or {}).get("files"):
        warns.append("license.files 为空：公开样例应附 LICENSE 与 NOTICE")
    for i, w in enumerate(doc.get("worlds") or []):
        ref(w.get("pointcloud"), f"worlds[{i}].pointcloud", required=False)
    for i, e in enumerate(doc.get("events") or []):
        we = f"events[{i}]"
        if not EVENT_ID_RE.match(str(e.get("id", ""))):
            probs.append(f"{we}.id {e.get('id')!r} 不合法（前端会拒绝整份索引）")
        ref(e.get("tape"), f"{we}.tape", bytes_=e.get("bytes"), sha=e.get("sha256"))
        ref(e.get("tape_json"), f"{we}.tape_json", required=False)
        ref(e.get("brief"), f"{we}.brief", required=False)
        for j, r in enumerate(e.get("runs") or []):
            wr = f"{we}.runs[{j}]"
            if not RUN_ID_RE.match(str(r.get("run_id", ""))):
                probs.append(f"{wr}.run_id {r.get('run_id')!r} 不合法（run-[a-z2-7]{{10}}，前端会拒绝整份索引）")
            for k, v in enumerate(r.get("videos") or []):
                ref(v.get("path"), f"{wr}.videos[{k}]", bytes_=v.get("bytes"), sha=v.get("sha256"), external=v.get("external"))
            for k, s in enumerate(r.get("slices") or []):
                ref(s.get("rrd"), f"{wr}.slices[{k}]", bytes_=s.get("bytes"), sha=s.get("sha256"), external=s.get("external"))
            ref(r.get("basemap"), f"{wr}.basemap", bytes_=r.get("basemap_bytes"), sha=r.get("basemap_sha256"), required=False)
            ref(r.get("tracks_obs"), f"{wr}.tracks_obs", required=False)
            ref(r.get("lidar_compare"), f"{wr}.lidar_compare", required=False)
            if r.get("metrics"):
                ref(r.get("metrics"), f"{wr}.metrics", required=False)
                warns.append(f"{wr}.metrics：公开静态目录含指标文件；样例页不得显示主得分与分量数值（AWR-05 §7.9）")
    return probs, warns


def check_version_dir(vdir: Path, *, test_ids: frozenset[str] = frozenset(), max_file: int = MAX_FILE_BYTES,
                      verify_hashes: bool = True) -> dict:
    """版本目录：SHA256SUMS 完整且一致、只含普通文件、路径可直出、单文件 ≤ 64 MB、索引一致、test 集与真值文件为 0。"""
    probs: list[str] = []
    warns: list[str] = []
    if not VERSION_RE.match(vdir.name):
        probs.append(f"版本目录名 {vdir.name!r} 不合法（YYYY.MM.DD-n）")
    sums: dict[str, str] = {}
    try:
        sums = read_sums(vdir)
    except LabError as e:
        probs.append(str(e))
    on_disk = {}
    for r, p in walk_files(vdir, skip_top=frozenset({SUMS, INDEX_IN_VERSION})):
        if p.is_symlink():
            probs.append(f"{r}：符号链接（nginx 会跟随链接直出任意文件，版本目录只允许普通文件）")
            continue
        if not p.is_file():
            probs.append(f"{r}：不是普通文件")
            continue
        if not all(SAFE_SEG_RE.match(s) for s in r.split("/")):
            probs.append(f"{r}：路径段不可直出（只允许 [A-Za-z0-9._-] 且不以点开头，见 lab-server.conf）")
        st = p.stat()
        if st.st_size > max_file:
            probs.append(f"{r}：{st.st_size} B > 64 MB（应经 ModelScope 分发，ADR-201）")
        on_disk[r] = p
    if sums:
        probs += [f"{r}：不在 {SUMS} 中" for r in sorted(set(on_disk) - set(sums))]
        probs += [f"{r}：{SUMS} 列出但不存在" for r in sorted(set(sums) - set(on_disk))]
        if verify_hashes:
            probs += [f"{r}：sha256 与 {SUMS} 不一致" for r in sorted(set(sums) & set(on_disk)) if sha256_file(on_disk[r]) != sums[r]]
    if (vdir / "leaderboard").exists():
        probs.append("版本目录含 leaderboard/（公开站不展示排行榜，R-D3-46）")
    idx = None
    ip = vdir / INDEX_IN_VERSION
    if not ip.is_file():
        probs.append(f"缺少 {INDEX_IN_VERSION}（labctl.py index 生成）")
    else:
        try:
            idx = load_json(ip)
        except ValueError as e:
            probs.append(f"{INDEX_IN_VERSION} 不是 JSON：{e}")
        if idx is not None and sums:
            p2, w2 = _index_problems(idx, vdir, sums, max_file)
            probs += p2
            warns += w2
    scan = load_scan_public().scan_static(vdir, test_ids=test_ids, max_file_mb=max_file / MIB)
    if scan["test_files"]:
        probs.append(f"test 集文件 {scan['test_files']} 个（D3-AC-21 要求 0）：{scan['test_detail'][:5]}")
    if scan["truth_files"]:
        probs.append(f"真值文件 {scan['truth_files']} 个（M27-FR-136 要求 0）：{scan['truth_detail'][:5]}")
    return {"kind": "version", "dir": str(vdir), "version": vdir.name, "files": len(on_disk), "bytes": scan["bytes_total"],
            "test_files": scan["test_files"], "truth_files": scan["truth_files"], "problems": probs, "warnings": warns,
            "lab_build": (idx or {}).get("lab_build") if isinstance(idx, dict) else None, "ok": not probs}


def check_lab_dist(dist: Path) -> dict:
    """/lab/ 构建：index.html 与 assets/、只含普通文件、scan_public build --profile lab 通过（禁用字符串、test 集与真值 0）。"""
    probs: list[str] = []
    if not (dist / "index.html").is_file() or not (dist / "assets").is_dir():
        probs.append(f"{dist} 不是 /lab/ 构建（缺少 index.html 或 assets/）")
    for r, p in walk_files(dist):
        if p.is_symlink():
            probs.append(f"{r}：符号链接")
        elif any(s.startswith(".") for s in r.split("/")):
            probs.append(f"{r}：以点开头的路径（nginx 一律 404）")
    rep = load_scan_public().scan_build(dist, "lab")
    bad = {k: v for k, v in rep["strings"].items() if v}
    if bad:
        probs.append(f"禁用字符串：{bad}")
    if rep["test_files"] or rep["truth_files"]:
        probs.append(f"test 集文件 {rep['test_files']}、真值文件 {rep['truth_files']}")
    name = None
    try:
        name = build_name(dist)
    except LabError as e:
        probs.append(str(e))
    return {"kind": "lab-dist", "dir": str(dist), "build": name, "files": rep["files"], "strings": rep["strings"],
            "problems": probs, "ok": not probs}


# ---------------------------------------------------------------------------------------------------------------------
# 状态


class Layout:
    """emax 上的发布目录（--root）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lab = root / "lab"
        self.static = root / "lab-static"
        self.versions = self.static / "v"
        self.index = self.static / "index.json"
        self.state_dir = root / ".state"
        self.state_file = self.state_dir / "state.json"
        self.history = self.state_dir / "history.jsonl"
        self.d2_site = self.state_dir / "d2-site.conf"
        self.bin = root / "bin"

    def staging_version(self, ver: str) -> Path:
        return self.static / ".staging" / ver

    def staging_build(self, build: str) -> Path:
        return self.lab / ".staging" / build

    def init(self) -> None:
        for d in (self.lab, self.lab / ".staging", self.versions, self.static / ".staging", self.state_dir, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        for d in (self.root, self.lab, self.static, self.versions):
            os.chmod(d, 0o755)
        os.chmod(self.state_dir, 0o700)

    def state(self) -> dict:
        try:
            st = load_json(self.state_file)
        except (OSError, ValueError):
            st = {}
        st.setdefault("schema", SCHEMA_STATE)
        st.setdefault("lab_static", {"current": None, "previous": None})
        st.setdefault("lab", {"current": None, "previous": None})
        st.setdefault("site", None)
        return st

    def save_state(self, st: dict) -> None:
        st["updated"] = utc_now()
        write_json_atomic(self.state_file, st, mode=0o600)

    def log(self, rec: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        rec = {"t": utc_now(), "by": _operator(), **rec}
        with open(self.history, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def current_build(self) -> str | None:
        link = self.lab / "current"
        return os.readlink(link) if link.is_symlink() else None

    def listed_versions(self) -> list[str]:
        return sorted(p.name for p in self.versions.iterdir() if p.is_dir() and VERSION_RE.match(p.name)) if self.versions.is_dir() else []

    def listed_builds(self) -> list[str]:
        if not self.lab.is_dir():
            return []
        return sorted(p.name for p in self.lab.iterdir() if p.is_dir() and not p.is_symlink() and BUILD_RE.match(p.name)
                      and not p.name.startswith("."))


def _operator() -> str:
    with contextlib.suppress(Exception):
        return os.environ.get("SUDO_USER") or getpass.getuser()
    return "unknown"


def budget_report(lay: Layout, *, budget: int = BUDGET_BYTES, max_file: int = MAX_FILE_BYTES,
                  keep: list[Path] | None = None) -> dict:
    """磁盘账（Q-D3-02）：D3 在 emax 的全部占用（构建、版本目录、staging、工具与状态），同一 inode 只计一次；keep 给出时为
    "只保留这些目录"后的预计占用。"""
    parts = keep if keep is not None else [lay.root]
    total = unique_bytes(parts)
    big = []
    for root in parts:
        if root.is_dir():
            big += [{"file": str(p), "bytes": p.stat().st_size} for _, p in walk_files(root)
                    if p.is_file() and not p.is_symlink() and p.stat().st_size > max_file]
    return {"bytes_total": total, "budget_bytes": budget, "oversize": big, "ok": total <= budget and not big,
            "scope": [str(p) for p in parts]}


def _chmod_public(root: Path) -> None:
    """nginx（www-data）只读：目录 0755、文件 0644。"""
    os.chmod(root, 0o755)
    for _, p in walk_files(root):
        if p.is_symlink():
            continue
        os.chmod(p, 0o755 if p.is_dir() else 0o644)


# ---------------------------------------------------------------------------------------------------------------------
# activate / rollback


def activate(lay: Layout, version: str, *, build: str | None = None, keep: int = KEEP_VERSIONS, budget: int = BUDGET_BYTES,
             max_file: int = MAX_FILE_BYTES, test_ids: frozenset[str] = frozenset(), chown: str | None = None) -> dict:
    """staging（或已存在的版本目录）→ 校验 → 移入 → 原子切换 /lab/ 构建与 index.json → 保留 keep 个版本。"""
    t0 = time.monotonic()
    lay.init()
    st = lay.state()
    vdir = lay.versions / version
    stg = lay.staging_version(version)
    if stg.is_dir():
        if vdir.exists():
            raise LabError(f"版本 {version} 已存在：版本目录不可变（只增不改），请换新版本名")
        src_v = stg
    elif vdir.is_dir():
        src_v = vdir                                         # 重新激活已有版本（例如回滚后再前滚）
    else:
        raise LabError(f"没有 {stg} 或 {vdir}：先 push")
    rep = check_version_dir(src_v, test_ids=test_ids, max_file=max_file)
    if not rep["ok"]:
        raise LabError("版本目录校验不通过：" + "；".join(rep["problems"][:10]))
    idx = load_json(src_v / INDEX_IN_VERSION)
    build = build or idx.get("lab_build") or lay.current_build()
    if not build:
        raise LabError("没有 /lab/ 构建：push --lab-dist 后以 --lab-build 指定，或在索引中写 lab_build")
    bdir, bstg = lay.lab / build, lay.staging_build(build)
    src_b = bstg if bstg.is_dir() and not bdir.exists() else bdir
    if not src_b.is_dir():
        raise LabError(f"没有构建 {build}（{bstg} 或 {bdir}）")
    brep = check_lab_dist(src_b)
    if not brep["ok"]:
        raise LabError("/lab/ 构建校验不通过：" + "；".join(brep["problems"][:10]))
    # 磁盘账：切换并清理后只剩新版本与上一版本（各自的构建）+ 工具与状态
    prev_v = st["lab_static"]["current"] if st["lab_static"]["current"] != version else st["lab_static"]["previous"]
    prev_b = st["lab"]["current"] if st["lab"]["current"] != build else st["lab"]["previous"]
    keep_paths = [src_v, src_b, lay.bin, lay.state_dir]
    if keep >= 2:
        keep_paths += [p for p in (lay.versions / prev_v if prev_v else None, lay.lab / prev_b if prev_b else None) if p]
    bud = budget_report(lay, budget=budget, max_file=max_file, keep=keep_paths)
    if not bud["ok"]:
        raise LabError(f"磁盘账超出（Q-D3-02）：预计 {bud['bytes_total']} B > {budget} B，或有 > 64 MB 的文件 {bud['oversize'][:3]}")
    for p in (src_v, src_b):
        if chown:
            subprocess.run(["chown", "-R", chown, str(p)], check=True)
        _chmod_public(p)
    if src_v is stg:
        os.rename(stg, vdir)
    if src_b is bstg:
        os.rename(bstg, bdir)
    elif bstg.is_dir():
        shutil.rmtree(bstg)                                  # 同名构建已在位（构建名含提交，内容相同）：丢弃重复上传
    old = {"version": st["lab_static"]["current"], "build": lay.current_build()}
    replace_symlink(lay.lab / "current", build)              # 先切构建（新页面兼容旧索引的字段），再切索引
    write_json_atomic(lay.index, load_json(vdir / INDEX_IN_VERSION))
    if old["version"] != version:
        st["lab_static"] = {"current": version, "previous": old["version"]}
    if old["build"] != build:
        st["lab"] = {"current": build, "previous": old["build"]}
    st["builds"] = {**(st.get("builds") or {}), version: build}
    lay.save_state(st)
    pruned = prune(lay, st, keep=keep)
    dt = round(time.monotonic() - t0, 3)
    rec = {"op": "activate", "version": version, "build": build, "from": old, "pruned": pruned, "seconds": dt,
           "bytes_total": unique_bytes([lay.root]), "warnings": rep["warnings"]}
    lay.log(rec)
    return rec


def prune(lay: Layout, st: dict, *, keep: int = KEEP_VERSIONS) -> list[str]:
    """保留当前与上一版本（keep = 2）及其构建；其余版本目录与构建删除（staging 不动）。"""
    keep_v = {v for v in (st["lab_static"]["current"], st["lab_static"]["previous"] if keep >= 2 else None) if v}
    keep_b = {b for b in (st["lab"]["current"], st["lab"]["previous"] if keep >= 2 else None) if b}
    keep_b |= {(st.get("builds") or {}).get(v) for v in keep_v} - {None}
    out = []
    for v in lay.listed_versions():
        if v not in keep_v:
            shutil.rmtree(lay.versions / v)
            out.append(f"lab-static/v/{v}")
    for b in lay.listed_builds():
        if b not in keep_b:
            shutil.rmtree(lay.lab / b)
            out.append(f"lab/{b}")
    st["builds"] = {v: b for v, b in (st.get("builds") or {}).items() if v in keep_v}
    lay.save_state(st)
    return out


def rollback(lay: Layout, *, to: str | None = None) -> dict:
    """恢复上一版本（或 --to 指定的保留版本）的 index.json 与它发布时的 /lab/ 构建（17 §18.8.5 规则 2，≤ 10 min）。"""
    t0 = time.monotonic()
    st = lay.state()
    cur = st["lab_static"]["current"]
    target = to or st["lab_static"]["previous"]
    if not target:
        raise LabError("没有可回滚的版本（state.json 无 previous）")
    vdir = lay.versions / target
    if not (vdir / INDEX_IN_VERSION).is_file():
        raise LabError(f"版本 {target} 不在保留期内（{vdir} 不存在）：现有 {lay.listed_versions()}")
    idx = load_json(vdir / INDEX_IN_VERSION)
    build = (st.get("builds") or {}).get(target) or idx.get("lab_build") or st["lab"]["previous"] or lay.current_build()
    if not build or not (lay.lab / build).is_dir():
        raise LabError(f"版本 {target} 对应的 /lab/ 构建 {build} 不存在")
    old_build = lay.current_build()
    write_json_atomic(lay.index, idx)                        # 先切索引（旧页面兼容），再切构建
    replace_symlink(lay.lab / "current", build)
    st["lab_static"] = {"current": target, "previous": cur if cur != target else st["lab_static"]["previous"]}
    if old_build != build:
        st["lab"] = {"current": build, "previous": old_build}
    lay.save_state(st)
    rec = {"op": "rollback", "from": {"version": cur, "build": old_build}, "to": {"version": target, "build": build},
           "seconds": round(time.monotonic() - t0, 3)}
    lay.log(rec)
    return rec


# ---------------------------------------------------------------------------------------------------------------------
# 站点：enable-site / withdraw


def _run(cmd: str, what: str) -> subprocess.CompletedProcess:
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    if r.returncode != 0:
        raise LabError(f"{what} 失败（{cmd}，退出码 {r.returncode}）：{(r.stderr or r.stdout).strip()[-800:]}")
    return r


def install_site(site_file: Path, text: str, *, nginx_test: str, nginx_reload: str) -> dict:
    """写站点文件（临时文件 + rename）→ nginx -t → reload；nginx -t 失败时恢复原文件且不 reload。"""
    old = site_file.read_text(encoding="utf-8") if site_file.exists() else None
    fd, tmp = tempfile.mkstemp(prefix=f".{site_file.name}.", dir=site_file.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, site_file)
    try:
        _run(nginx_test, "nginx -t")
    except LabError:
        if old is None:
            site_file.unlink()
        else:
            site_file.write_text(old, encoding="utf-8")
        raise
    _run(nginx_reload, "nginx reload")
    return {"site_file": str(site_file), "sha256": labsite.sha256_text(text)}


def enable_site(lay: Layout, site_file: Path, *, base: Path | None = None, nginx_test: str, nginx_reload: str) -> dict:
    """安装"D2 + /lab/"站点文件：以当前站点文件（不含 /lab/ 时）或 --base 为 D2 站点文件，保存到 .state/d2-site.conf。"""
    t0 = time.monotonic()
    lay.init()
    if base is not None:
        d2 = base.read_text(encoding="utf-8")
    elif site_file.exists() and not labsite.has_lab(site_file.read_text(encoding="utf-8")):
        d2 = site_file.read_text(encoding="utf-8")
    elif lay.d2_site.exists():
        d2 = lay.d2_site.read_text(encoding="utf-8")
    else:
        raise LabError("找不到 D2 站点文件：以 --base 指定 D2 标签构建自带的 drone4d.agentnetwork.org.cn.conf")
    if labsite.has_lab(d2):
        raise LabError("D2 站点文件不应包含 /lab/ 增补段")
    text = labsite.render(d2)
    lay.d2_site.write_text(d2, encoding="utf-8")
    os.chmod(lay.d2_site, 0o600)
    res = install_site(site_file, text, nginx_test=nginx_test, nginx_reload=nginx_reload)
    st = lay.state()
    st["site"] = "lab"
    lay.save_state(st)
    rec = {"op": "enable-site", **res, "d2_sha256": labsite.sha256_text(d2), "seconds": round(time.monotonic() - t0, 3)}
    lay.log(rec)
    return rec


def withdraw(lay: Layout, site_file: Path, *, d2_site: Path | None = None, nginx_test: str, nginx_reload: str,
             d2_release: str | None = None, install_sh: Path | None = None) -> dict:
    """回滚到 D2：恢复 D2 站点文件（撤下 /lab/、/lab-static/ 与 /api/bench 的 nginx 段），可选同时 install.sh --rollback 到指定
    D2 发布；版本目录与构建保留在磁盘上（之后 enable-site 即可恢复）。"""
    t0 = time.monotonic()
    if d2_site is not None:
        d2 = d2_site.read_text(encoding="utf-8")
    elif lay.d2_site.exists():
        d2 = lay.d2_site.read_text(encoding="utf-8")
    elif site_file.exists() and labsite.has_lab(site_file.read_text(encoding="utf-8")):
        d2 = labsite.strip(site_file.read_text(encoding="utf-8"))
    else:
        raise LabError("找不到 D2 站点文件（.state/d2-site.conf 或 --d2-site）")
    if labsite.has_lab(d2):
        raise LabError("D2 站点文件不应包含 /lab/ 增补段")
    res = install_site(site_file, d2, nginx_test=nginx_test, nginx_reload=nginx_reload)
    rel_out = None
    if d2_release:
        sh = install_sh or Path("/opt/anet-drone4d/current/tools/deploy/emax/install.sh")
        r = subprocess.run(["bash", str(sh), "--rollback", d2_release], capture_output=True, text=True)
        rel_out = {"release": d2_release, "exit": r.returncode, "tail": (r.stdout + r.stderr).strip().splitlines()[-5:]}
        if r.returncode != 0:
            raise LabError(f"install.sh --rollback {d2_release} 失败：{rel_out['tail']}")
    st = lay.state()
    st["site"] = "d2"
    lay.save_state(st)
    rec = {"op": "withdraw", **res, "d2_release": rel_out, "seconds": round(time.monotonic() - t0, 3)}
    lay.log(rec)
    return rec


# ---------------------------------------------------------------------------------------------------------------------
# push（dmax 或开发机 → emax）


def _split_dest(dest: str) -> tuple[str | None, str]:
    m = re.match(r"^([A-Za-z0-9_.@-]+):(/.*)$", dest)
    return (m.group(1), m.group(2)) if m else (None, dest)


def push(vdir: Path, dist: Path, dest: str, *, rsync: str = "rsync", ssh: str = "ssh", dry_run: bool = False,
         runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> dict:
    """本机校验后单向 rsync：版本目录 → <root>/lab-static/.staging/<ver>/（--link-dest 上一版本），/lab/ 构建 →
    <root>/lab/.staging/<build>/（--link-dest 当前构建），工具 → <root>/bin/。emax 上随后 activate。"""
    run = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True))
    rep_v = check_version_dir(vdir)
    rep_b = check_lab_dist(dist)
    if not rep_v["ok"] or not rep_b["ok"]:
        raise LabError("本机校验不通过，不上传：" + "；".join((rep_v["problems"] + rep_b["problems"])[:10]))
    build = build_name(dist)
    idx = load_json(vdir / INDEX_IN_VERSION)
    if idx.get("lab_build") not in (None, build):
        raise LabError(f"索引的 lab_build {idx.get('lab_build')} 与构建 {build} 不一致")
    host, root = _split_dest(dest)
    if host:
        q = shlex.quote(root)
        r = run([ssh, host, f"ls -1 {q}/lab-static/v 2>/dev/null; echo ::; readlink {q}/lab/current 2>/dev/null || true"])
        vs, _, cb = (r.stdout or "").partition("::")
        versions, cur_build = vs.split(), (cb.strip() or None)
    else:
        lay = Layout(Path(root))
        versions, cur_build = lay.listed_versions(), lay.current_build()
    # --link-dest：最新的已有版本目录与当前构建（未变的文件在 emax 上成为硬链接，Q-D3-02 磁盘账只计一次）
    prev_v = max((x for x in versions if VERSION_RE.match(x) and x != vdir.name), default=None)
    prev_b = cur_build if cur_build and BUILD_RE.match(cur_build) and cur_build != build else None

    def target(sub: str) -> str:
        return f"{host}:{root}/{sub}" if host else f"{root}/{sub}"

    # 不保留 mtime（不带 -t）并按内容比较（--checksum）：dmax 每次重新生成的制品 mtime 不同，带 -t 时 --link-dest 只认 mtime 相同的
    # 文件，内容未变的制品也不会成为硬链接；权限统一为 D755、F644（--link-dest 还要求权限一致）
    flags = ["-r", "--delete", "--checksum", "--chmod=D755,F644", "--no-links"] + (["--dry-run"] if dry_run else [])
    if host:
        mk = run([ssh, host, f"mkdir -p {shlex.quote(root)}/lab-static/.staging {shlex.quote(root)}/lab/.staging {shlex.quote(root)}/bin"])
        if mk.returncode != 0:
            raise LabError(f"{host}: 建立 staging 目录失败：{mk.stderr.strip()}")
    else:
        Layout(Path(root)).init()
    cmds = [
        [rsync, *flags, *([f"--link-dest=../../v/{prev_v}/"] if prev_v else []), f"{vdir}/", target(f"lab-static/.staging/{vdir.name}/")],
        [rsync, *flags, *([f"--link-dest=../../{prev_b}/"] if prev_b else []), f"{dist}/", target(f"lab/.staging/{build}/")],
        [rsync, "-rt", "--chmod=D755,F644", *(["--dry-run"] if dry_run else []),
         *[str(HERE / f) for f in TOOL_FILES if (HERE / f).exists()], str(_scan_public_path()), target("bin/")],
    ]
    out = []
    t0 = time.monotonic()
    for c in cmds:
        r = run(c)
        out.append({"cmd": " ".join(shlex.quote(x) for x in c), "exit": r.returncode})
        if r.returncode != 0:
            raise LabError(f"rsync 失败（退出码 {r.returncode}）：{(r.stderr or '').strip()[-400:]}")
    return {"op": "push", "version": vdir.name, "build": build, "dest": dest, "link_dest": {"version": prev_v, "build": prev_b},
            "rsync": out, "seconds": round(time.monotonic() - t0, 3), "dry_run": dry_run}


def _scan_public_path() -> Path:
    for cand in (HERE / "scan_public.py", HERE.parents[2] / "bench" / "d3" / "scan_public.py"):
        if cand.is_file():
            return cand
    raise LabError("找不到 scan_public.py")


# ---------------------------------------------------------------------------------------------------------------------
# pin（dmax：retention.sync_references）


def pin_targets(idx: dict) -> list[tuple[str, str | None]]:
    """索引引用的派生物：有切片或底图的运行 → (run_id, "slices")；有视频的运行 → (run_id, "video")。"""
    out: list[tuple[str, str | None]] = []
    for e in idx.get("events") or []:
        for r in e.get("runs") or []:
            if r.get("slices") or r.get("basemap"):
                out.append((r["run_id"], "slices"))
            if r.get("videos"):
                out.append((r["run_id"], "video"))
    return out


def pin(bench_root: Path, idx: dict) -> dict:
    from awr.bench.retention import sync_references  # dmax 上的仓库环境（PYTHONPATH=python）

    ref = f"static:{idx['version']}"
    return {"op": "pin", "ref": ref, **sync_references(bench_root, ref, pin_targets(idx))}


# ---------------------------------------------------------------------------------------------------------------------
# 命令行


def _status(lay: Layout) -> dict:
    st = lay.state()
    idx = None
    with contextlib.suppress(OSError, ValueError):
        idx = load_json(lay.index)
    return {"root": str(lay.root), "state": st, "index_version": (idx or {}).get("version"), "lab_current": lay.current_build(),
            "versions": lay.listed_versions(), "builds": lay.listed_builds(), "budget": budget_report(lay)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="labctl.py", description="公开样例发布线（D3-AC-21）")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="emax 发布根目录（缺省 /srv/drone4d）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="生成 SHA256SUMS 与 .index.json")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--draft", type=Path, required=True)
    p.add_argument("--slices", type=Path, action="append", default=[])
    p.add_argument("--lab-build")
    p.add_argument("--version")
    p = sub.add_parser("check", help="版本目录与 /lab/ 构建校验")
    p.add_argument("--dir", type=Path)
    p.add_argument("--lab-dist", type=Path)
    p.add_argument("--test-ids", type=Path)
    p = sub.add_parser("push", help="单向 rsync 到 emax 的 staging")
    p.add_argument("--static", type=Path, required=True)
    p.add_argument("--lab-dist", type=Path, required=True)
    p.add_argument("--dest", required=True, help="HOST:/srv/drone4d 或本机目录")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("activate", help="校验 staging 并原子切换")
    p.add_argument("--version", required=True)
    p.add_argument("--lab-build")
    p.add_argument("--keep", type=int, default=KEEP_VERSIONS)
    p.add_argument("--budget-gb", type=float, default=3.0)
    p.add_argument("--test-ids", type=Path)
    p.add_argument("--chown", help="移入前 chown -R（例如 root:root）")
    p = sub.add_parser("rollback", help="恢复上一版本")
    p.add_argument("--to")
    for name in ("enable-site", "withdraw"):
        p = sub.add_parser(name)
        p.add_argument("--site-file", type=Path, default=SITE_FILE)
        p.add_argument("--nginx-test", default="nginx -t")
        p.add_argument("--nginx-reload", default="systemctl reload nginx")
        if name == "enable-site":
            p.add_argument("--base", type=Path, help="D2 标签构建自带的站点文件")
        else:
            p.add_argument("--d2-site", type=Path)
            p.add_argument("--d2-release", help="同时 install.sh --rollback 到该 D2 发布")
            p.add_argument("--install-sh", type=Path)
    sub.add_parser("status")
    p = sub.add_parser("budget")
    p.add_argument("--budget-gb", type=float, default=3.0)
    p = sub.add_parser("pin", help="dmax：被引用的切片置 pinned")
    p.add_argument("--bench-root", type=Path, required=True)
    p.add_argument("--index", type=Path, required=True)
    a = ap.parse_args(argv)
    lay = Layout(a.root)
    try:
        if a.cmd == "index":
            draft = load_json(a.draft)
            merge_slices(draft, [load_json(p) for p in a.slices])
            out: Any = build_index(a.dir, draft, version=a.version, lab_build=a.lab_build)
            out = {"op": "index", "version": out["version"], "files_bytes_total": out["files_bytes_total"],
                   "index": str(a.dir / INDEX_IN_VERSION)}
        elif a.cmd == "check":
            if not a.dir and not a.lab_dist:
                ap.error("check 需要 --dir 或 --lab-dist")
            tids = load_scan_public().load_test_ids(a.test_ids)
            out = {"schema": SCHEMA_REPORT, "t": utc_now(), "checks": []}
            if a.dir:
                out["checks"].append(check_version_dir(a.dir, test_ids=tids))
            if a.lab_dist:
                out["checks"].append(check_lab_dist(a.lab_dist))
            out["ok"] = all(c["ok"] for c in out["checks"])
        elif a.cmd == "push":
            out = push(a.static, a.lab_dist, a.dest, dry_run=a.dry_run)
        elif a.cmd == "activate":
            tids = load_scan_public().load_test_ids(a.test_ids)
            out = activate(lay, a.version, build=a.lab_build, keep=a.keep, budget=int(a.budget_gb * (1 << 30)), test_ids=tids,
                           chown=a.chown)
        elif a.cmd == "rollback":
            out = rollback(lay, to=a.to)
        elif a.cmd == "enable-site":
            out = enable_site(lay, a.site_file, base=a.base, nginx_test=a.nginx_test, nginx_reload=a.nginx_reload)
        elif a.cmd == "withdraw":
            out = withdraw(lay, a.site_file, d2_site=a.d2_site, nginx_test=a.nginx_test, nginx_reload=a.nginx_reload,
                           d2_release=a.d2_release, install_sh=a.install_sh)
        elif a.cmd == "status":
            out = _status(lay)
        elif a.cmd == "budget":
            out = budget_report(lay, budget=int(a.budget_gb * (1 << 30)))
        else:
            out = pin(a.bench_root, load_json(a.index))
    except (LabError, labsite.SiteError, OSError, ValueError) as e:
        print(f"labctl {a.cmd}: {e}", file=sys.stderr)
        return 1
    ok = out.get("ok", True) if isinstance(out, dict) else True
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        _print_human(a.cmd, out)
    return 0 if ok else 1


def _print_human(cmd: str, out: dict) -> None:
    if cmd == "check":
        for c in out["checks"]:
            head = f"{'通过' if c['ok'] else '不通过'} {c['kind']} {c['dir']}（{c['files']} 个文件）"
            print(head)
            for p in c["problems"]:
                print(f"  问题：{p}")
            for w in c.get("warnings", []):
                print(f"  告警：{w}")
        return
    if cmd == "budget":
        print(f"D3 总占用 {out['bytes_total']} B / 预算 {out['budget_bytes']} B，> 64 MB 文件 {len(out['oversize'])} 个："
              f"{'通过' if out['ok'] else '不通过'}")
        return
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    raise SystemExit(main())
