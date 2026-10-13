#!/usr/bin/env python3
"""tools/deploy/emax/worldpack.py：公开演示站的世界包与派生缓存包（AWR-04 §4.4.7、ADR-109；M17-FR-064、M17-AC-020）。

部署套件的脚本，不是 Python 包，不被任何代码 import（测试经 importlib 按路径加载）。三个子命令：

  pack     在开发机上把已构建的世界打成内容寻址的包（build-local.sh 调用；需要仓库 .venv 以读取派生缓存状态）：
             world-<id>-<cv>-<wj8>.tar.gz    世界包 `<id>/**` 与构建状态 `.status/<id>.json`（目录服务据此报 ready）；
                                             wj8 为 world.json 摘要前 8 位（只改元数据时也换包）
             geo-<id>-<cv>-<key>.tar.gz      当前派生缓存条目 `.geo-cache/<id>/<cv>-<key>/**`（含 Grid25，M04 `m04-derive@2`）
             shared-env-<sha8>.tar.gz        共享环境资产 `_shared/env/**`
           并写索引 WORLDS.json（`awr.deploy.worlds.v1`）与 WORLDS.sha256；同名包（内容版本与派生键相同）直接复用，
           索引不再引用的旧包删除。
  verify   校验索引、每个包的 sha256 与成员（只允许普通文件与目录、路径限定在各自前缀内、无绝对路径与 ..）。
  install  verify 之后把包装进世界目录（install.sh 以 root 调用，--owner awr）：世界目录与派生缓存条目先解到同一文件系统
           的临时目录再原子改名；已是同一内容版本或同一条目者跳过；每个世界的派生缓存保留当前条目与最近 KEEP-1 个旧条目
           （回滚到上一发布时仍可用）。--worlds-dir 指向临时目录即为 install.sh --dry-run 的本机模拟。

纳入条件：世界在 public profile 白名单内、构建状态为 ready 且与 world.json 的 contentVersion 一致、派生缓存当前键的条目
可用；数据集为 synthcity（可再分发）或 UrbanScene3D 六城（经数据集权利方授权使用，ADR-109，R-D2-28；world.json 须带
`dataset.terms`，ADR-109 之前构建的世界先 `make worlds-dataset`）。
退出码：0 成功；1 校验或安装失败；2 参数错误。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import time
from collections.abc import Iterable
from pathlib import Path

SCHEMA = "awr.deploy.worlds.v1"
INDEX = "WORLDS.json"
SHOW_REL = "show/show.awrrt"  # D2.2-W2 city show recording; not part of contentVersion (awr/world/package/manifest.py)
SUMS = "WORLDS.sha256"
WORLD_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
HEX = re.compile(r"^[0-9a-f]+$")
URBANSCENE3D = "UrbanScene3D"
TERMS_SYNTH = "ANet Synthetic City: generated content without third-party data, free to redistribute"
# ADR-109 第 3 条：六城的 world.json 自带 `dataset.terms`（"经数据集权利方授权使用"，awr.world.ingest.urbanscene3d.DATASET）。
# ADR-109 之前构建的世界没有 terms、许可仍写科研用途；公开站直出 world.json，世界面板会照此显示，因此打包时拒绝并提示只重写元数据。
STALE_LICENSE = re.compile(r"research\s+only|non-commercial\s+research", re.IGNORECASE)
ROOT = Path(__file__).resolve().parents[3]


class PackError(Exception):
    """包、索引或世界目录不满足条件（退出码 1）。"""


# ---------------------------------------------------------------- 通用


def sha256_file(p: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(p: Path) -> dict:
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise PackError(f"{p}: 不是 JSON 对象")
    return d


def _atomic_write(p: Path, text: str) -> None:
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def public_worlds(config: Path | None = None) -> list[str]:
    """public profile 的世界白名单（`profiles.public.net.worlds_allow`，七个世界，ADR-109）。"""
    import yaml  # 仓库 .venv 与发布 .venv 都有 pyyaml（requirements.lock）

    cfg = yaml.safe_load((config or ROOT / "configs" / "runtime.yaml").read_text(encoding="utf-8"))
    ids = (((cfg.get("profiles") or {}).get("public") or {}).get("net") or {}).get("worlds_allow") or []
    return [str(x) for x in ids]


# ---------------------------------------------------------------- 打包


def _add_tree(tar: tarfile.TarFile, base: Path, rel: str) -> None:
    """把 base/rel 以 rel 为成员名加入（排序、属主 0、文件 0644、目录 0755、不跟随符号链接）。"""
    src = base / rel
    if src.is_symlink():
        raise PackError(f"{src}: 包内不允许符号链接")
    ti = tar.gettarinfo(str(src), arcname=rel)
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    if src.is_dir():
        ti.mode = 0o755
        tar.addfile(ti)
        for child in sorted(os.listdir(src)):
            if child == "__pycache__":
                continue
            _add_tree(tar, base, f"{rel}/{child}")
    elif src.is_file():
        ti.mode = 0o644
        with open(src, "rb") as f:
            tar.addfile(ti, f)
    else:
        raise PackError(f"{src}: 只允许普通文件与目录")


def _write_tar(out: Path, base: Path, rels: Iterable[str], level: int) -> dict:
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    with tarfile.open(tmp, "w:gz", compresslevel=level, format=tarfile.PAX_FORMAT) as tar:
        for rel in rels:
            _add_tree(tar, base, rel)
    os.replace(tmp, out)
    return {"file": out.name, "sha256": sha256_file(out), "bytes": out.stat().st_size}


def _reuse(out: Path, old: dict[str, dict]) -> dict | None:
    """同名包已在输出目录且旧索引记录的 sha256 与文件一致时复用（内容寻址：名字含内容版本与派生键）。"""
    rec = old.get(out.name)
    if rec and out.is_file() and out.stat().st_size == rec.get("bytes") and sha256_file(out) == rec.get("sha256"):
        return {"file": out.name, "sha256": rec["sha256"], "bytes": rec["bytes"]}
    return None


def _old_records(out_dir: Path) -> dict[str, dict]:
    p = out_dir / INDEX
    if not p.is_file():
        return {}
    try:
        idx = _read_json(p)
    except (OSError, ValueError, PackError):
        return {}
    recs: dict[str, dict] = {}
    for w in idx.get("worlds") or []:
        for k in ("world_pack", "cache_pack"):
            r = w.get(k) or {}
            if r.get("file"):
                recs[r["file"]] = r
    sh = idx.get("shared") or {}
    if sh.get("file"):
        recs[sh["file"]] = sh
    return recs


def _env_digest(env_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(env_dir.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(env_dir).as_posix().encode("utf-8") + b"\0")
            h.update(sha256_file(p).encode("ascii") + b"\n")
    return h.hexdigest()


def world_terms(world_json: dict) -> str:
    """索引中记录的使用条件：可再分发的合成世界，或 world.json 自带授权说明（`dataset.terms`）的 UrbanScene3D 六城。"""
    ds = world_json.get("dataset") or {}
    if ds.get("redistribution") is True:
        return str(ds.get("terms") or "").strip() or TERMS_SYNTH
    if ds.get("name") == URBANSCENE3D:
        terms = str(ds.get("terms") or "").strip()
        if not terms or STALE_LICENSE.search(str(ds.get("license") or "")):
            raise PackError(f"世界 {world_json.get('id')} 的 world.json 仍是 ADR-109 之前的数据集说明（缺少 dataset.terms 或许可仍写"
                            "科研用途），公开站的世界面板会照此显示：先 make worlds-dataset（只重写元数据，contentVersion 与派生缓存不变）")
        return terms
    raise PackError(f"世界 {world_json.get('id')} 的数据集 {ds.get('name')!r} 既不可再分发也不在 ADR-109 的授权范围内")


def describe_world(worlds_dir: Path, wid: str, cache_root: Path | None, status_fn=None) -> dict:
    """读取一个世界的打包信息：world.json、构建状态与当前派生缓存条目（`awr.world.geometry.cache_status`）。"""
    if not WORLD_ID.match(wid):
        raise PackError(f"非法世界 id {wid!r}")
    wd = worlds_dir / wid
    wj = wd / "world.json"
    if not wj.is_file():
        raise PackError(f"{wj} 不存在：先构建该世界（make worlds 或 make demo-world）")
    w = _read_json(wj)
    if w.get("id") != wid:
        raise PackError(f"{wj}: id 为 {w.get('id')!r}，不是 {wid}")
    cv = str(w.get("contentVersion") or "")
    if not HEX.match(cv):
        raise PackError(f"{wj}: contentVersion {cv!r} 非法")
    st_path = worlds_dir / ".status" / f"{wid}.json"
    if not st_path.is_file():
        raise PackError(f"{st_path} 不存在：目录服务只在构建状态为 ready 且版本一致时列出该世界")
    st = _read_json(st_path)
    if st.get("status") != "ready" or str(st.get("content_version") or "") != cv:
        raise PackError(f"{st_path}: 状态 {st.get('status')!r}、版本 {st.get('content_version')!r}，需要 ready 与 {cv}")
    if status_fn is None:
        from awr.world.geometry import cache_status as status_fn  # 只在开发机打包时需要
    cs = status_fn(wd, cache_root)
    if not cs.available or not cs.entry:
        raise PackError(f"{wid}: 派生缓存不可用（{cs.reason}/{cs.detail}）：先 make geo-warm")
    entry = Path(cs.entry)
    show = wd / SHOW_REL
    return {"world_id": wid, "content_version": cv, "world_json_sha256": sha256_file(wj), "coordinate_sha256": cs.coordinate_sha256,
            "show_sha256": sha256_file(show) if show.is_file() else None,
            "derive_sha8": cs.derive_sha8, "derive_version": cs.derive_version, "entry": entry,
            "cache_bytes": int(cs.cache_bytes), "grid25_bytes": int(cs.grid25_bytes), "terms": world_terms(w),
            "dataset": (w.get("dataset") or {}).get("name")}


def pack(worlds_dir: Path, out_dir: Path, ids: list[str], *, cache_root: Path | None = None, level: int = 1,
         status_fn=None, log=print) -> dict:
    worlds_dir = Path(worlds_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    old = _old_records(out_dir)
    entries: list[dict] = []
    keep: set[str] = set()
    derive_version = None
    for wid in ids:
        d = describe_world(worlds_dir, wid, cache_root, status_fn)
        derive_version = derive_version or d["derive_version"]
        cv, key = d["content_version"], d["derive_sha8"]
        # 包名含 world.json 的摘要：只改元数据（例如数据集说明）而 contentVersion 不变时也换包
        # 包名再含城市演示录制的摘要（show/ 不计入 contentVersion，只换录制时也必须换包，否则复用旧包而漏发录制）
        show8 = f"-s{d['show_sha256'][:8]}" if d["show_sha256"] else ""
        wp = out_dir / f"world-{wid}-{cv}-{d['world_json_sha256'][:8]}{show8}.tar.gz"
        rec_w = _reuse(wp, old)
        if rec_w is None:
            t0 = time.monotonic()
            rec_w = _write_tar(wp, worlds_dir, [wid, f".status/{wid}.json"], level)
            log(f"    {wp.name}  {rec_w['bytes'] / 2**20:.1f} MiB  {time.monotonic() - t0:.1f} s")
        else:
            log(f"    {wp.name}  复用")
        entry: Path = d["entry"]
        if entry.name != f"{cv}-{key}":
            raise PackError(f"{entry}: 条目名与 {cv}-{key} 不符")
        cp = out_dir / f"geo-{wid}-{cv}-{key}.tar.gz"
        rec_c = _reuse(cp, old)
        if rec_c is None:
            t0 = time.monotonic()
            tmp = cp.with_name(f".{cp.name}.tmp-{os.getpid()}")
            # 成员名统一为 .geo-cache/<id>/<条目>（开发机的缓存根可以在任何位置，例如 AWR_GEO_CACHE_DIR）
            with tarfile.open(tmp, "w:gz", compresslevel=level, format=tarfile.PAX_FORMAT) as tar:
                _add_entry(tar, entry, f".geo-cache/{wid}/{entry.name}")
            os.replace(tmp, cp)
            rec_c = {"file": cp.name, "sha256": sha256_file(cp), "bytes": cp.stat().st_size}
            log(f"    {cp.name}  {rec_c['bytes'] / 2**20:.1f} MiB  {time.monotonic() - t0:.1f} s")
        else:
            log(f"    {cp.name}  复用")
        keep.update({wp.name, cp.name})
        entries.append({k: d[k] for k in ("world_id", "content_version", "world_json_sha256", "show_sha256", "coordinate_sha256", "derive_sha8",
                                          "cache_bytes", "grid25_bytes", "dataset", "terms")}
                       | {"world_pack": rec_w, "cache_pack": rec_c})
    shared = None
    env_dir = worlds_dir / "_shared" / "env"
    if env_dir.is_dir():
        sp = out_dir / f"shared-env-{_env_digest(env_dir)[:8]}.tar.gz"
        shared = _reuse(sp, old) or _write_tar(sp, worlds_dir, ["_shared/env"], level)
        keep.add(sp.name)
    idx = {"schema": SCHEMA, "created_at": _now_iso(), "derive_version": derive_version, "worlds": entries,
           "shared": shared}
    _atomic_write(out_dir / INDEX, json.dumps(idx, ensure_ascii=False, indent=1) + "\n")
    sums = [f"{r['sha256']}  {r['file']}" for e in entries for r in (e["world_pack"], e["cache_pack"])]
    if shared:
        sums.append(f"{shared['sha256']}  {shared['file']}")
    sums.append(f"{sha256_file(out_dir / INDEX)}  {INDEX}")
    _atomic_write(out_dir / SUMS, "\n".join(sums) + "\n")
    for p in out_dir.iterdir():  # 索引不再引用的旧包
        if p.is_file() and p.name.endswith(".tar.gz") and p.name not in keep:
            p.unlink()
            log(f"    删除旧包 {p.name}")
    return idx


def _add_entry(tar: tarfile.TarFile, entry: Path, rel: str) -> None:
    """派生缓存条目（只有普通文件）以 rel 为目录名加入。"""
    ti = tar.gettarinfo(str(entry), arcname=rel)
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mode = 0o755
    tar.addfile(ti)
    for name in sorted(os.listdir(entry)):
        p = entry / name
        if p.is_symlink() or not p.is_file():
            raise PackError(f"{p}: 派生缓存条目只应包含普通文件")
        fi = tar.gettarinfo(str(p), arcname=f"{rel}/{name}")
        fi.uid = fi.gid = 0
        fi.uname = fi.gname = ""
        fi.mode = 0o644
        with open(p, "rb") as f:
            tar.addfile(fi, f)


# ---------------------------------------------------------------- 校验


def _member_ok(name: str, prefixes: tuple[str, ...], exact: tuple[str, ...]) -> bool:
    if not name or name.startswith("/") or "\\" in name or "\0" in name:
        return False
    n = name[2:] if name.startswith("./") else name
    n = n.rstrip("/")
    parts = n.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return n in exact or any(n == p or n.startswith(p + "/") for p in prefixes)


def check_tar(path: Path, prefixes: tuple[str, ...], exact: tuple[str, ...] = ()) -> list[tarfile.TarInfo]:
    """成员只能是普通文件与目录，路径在给定前缀内；返回成员表（供解包）。"""
    with tarfile.open(path, "r:gz") as tar:
        members = tar.getmembers()
    if not members:
        raise PackError(f"{path.name}: 空包")
    for m in members:
        if not (m.isfile() or m.isdir()):
            raise PackError(f"{path.name}: 成员 {m.name!r} 不是普通文件或目录")
        if not _member_ok(m.name, prefixes, exact):
            raise PackError(f"{path.name}: 成员 {m.name!r} 超出允许的路径 {prefixes + exact}")
    return members


def load_index(src: Path) -> dict:
    src = Path(src)
    idx_p = src / INDEX
    if not idx_p.is_file():
        raise PackError(f"{idx_p} 不存在：不是 build-local.sh 生成的世界包目录")
    idx = _read_json(idx_p)
    if idx.get("schema") != SCHEMA:
        raise PackError(f"{idx_p}: schema {idx.get('schema')!r} 不是 {SCHEMA}")
    return idx


def verify(src: Path, *, deep: bool = True) -> dict:
    """索引与 WORLDS.sha256 一致、每个包的 sha256 与成员合法；返回索引。"""
    src = Path(src)
    idx = load_index(src)
    sums_p = src / SUMS
    if not sums_p.is_file():
        raise PackError(f"{sums_p} 不存在")
    sums = {}
    for line in sums_p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            h, _, name = line.partition("  ")
            sums[name.strip()] = h.strip()
    if sums.get(INDEX) != sha256_file(src / INDEX):
        raise PackError(f"{INDEX} 与 {SUMS} 不一致（索引被改动）")
    seen: set[str] = set()
    for e in idx.get("worlds") or []:
        wid, cv, key = e.get("world_id"), str(e.get("content_version") or ""), str(e.get("derive_sha8") or "")
        if not (isinstance(wid, str) and WORLD_ID.match(wid) and HEX.match(cv) and HEX.match(key)):
            raise PackError(f"索引条目非法：{wid!r} {cv!r} {key!r}")
        if wid in seen:
            raise PackError(f"索引中世界 {wid} 重复")
        seen.add(wid)
        for k, prefixes, exact in (("world_pack", (wid,), (f".status/{wid}.json", ".status")),
                                   ("cache_pack", (f".geo-cache/{wid}/{cv}-{key}",), (".geo-cache", f".geo-cache/{wid}"))):
            _verify_file(src, e.get(k) or {}, sums, prefixes, exact, deep)
    if idx.get("shared"):
        _verify_file(src, idx["shared"], sums, ("_shared/env",), ("_shared",), deep)
    return idx


def _verify_file(src: Path, rec: dict, sums: dict, prefixes: tuple[str, ...], exact: tuple[str, ...], deep: bool) -> None:
    name = rec.get("file")
    if not name or "/" in name or name.startswith("."):
        raise PackError(f"包文件名非法：{name!r}")
    p = src / name
    if not p.is_file():
        raise PackError(f"{p} 不存在（上传不完整）")
    if p.stat().st_size != rec.get("bytes"):
        raise PackError(f"{name}: 大小 {p.stat().st_size} 与索引 {rec.get('bytes')} 不符")
    if sums.get(name) != rec.get("sha256"):
        raise PackError(f"{name}: 索引与 {SUMS} 的 sha256 不一致")
    if deep and sha256_file(p) != rec.get("sha256"):
        raise PackError(f"{name}: sha256 校验失败（包不完整或被改动）")
    check_tar(p, prefixes, exact)


# ---------------------------------------------------------------- 安装


def _installed_cv(worlds_dir: Path, wid: str) -> str | None:
    with contextlib.suppress(OSError, ValueError, PackError):
        return str(_read_json(worlds_dir / wid / "world.json").get("contentVersion"))
    return None


def _world_json_sha(worlds_dir: Path, wid: str) -> str | None:
    with contextlib.suppress(OSError):
        return sha256_file(worlds_dir / wid / "world.json")
    return None


def _show_sha(worlds_dir: Path, wid: str) -> str | None:
    with contextlib.suppress(OSError):
        return sha256_file(worlds_dir / wid / SHOW_REL)
    return None


def _status_cv(worlds_dir: Path, wid: str) -> str | None:
    with contextlib.suppress(OSError, ValueError, PackError):
        return str(_read_json(worlds_dir / ".status" / f"{wid}.json").get("content_version"))
    return None


def _chown_tree(p: Path, uid: int | None, gid: int | None) -> None:
    if uid is None:
        return
    os.chown(p, uid, gid if gid is not None else -1)
    for root, dirs, files in os.walk(p):
        for n in dirs + files:
            os.chown(os.path.join(root, n), uid, gid if gid is not None else -1)


def _extract(pack_path: Path, dest: Path, members: list[tarfile.TarInfo]) -> None:
    with tarfile.open(pack_path, "r:gz") as tar:
        tar.extractall(dest, members=members, filter="data")


def _swap_dir(new: Path, target: Path) -> None:
    """把 new 原子地放到 target（target 存在时先改名移开，成功后删除旧目录）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    old = target.with_name(f".old-{target.name}-{os.getpid()}")
    if target.exists():
        os.rename(target, old)
    os.rename(new, target)
    shutil.rmtree(old, ignore_errors=True)


def prune_cache(worlds_dir: Path, wid: str, current: str, keep: int) -> list[str]:
    """每个世界的派生缓存保留当前条目与最近 keep-1 个旧条目（按修改时间）。"""
    d = worlds_dir / ".geo-cache" / wid
    if not d.is_dir():
        return []
    others = sorted((p for p in d.iterdir() if p.is_dir() and p.name != current and not p.name.startswith(".")),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for p in others[max(0, keep - 1):]:
        shutil.rmtree(p, ignore_errors=True)
        removed.append(p.name)
    return removed


def install(src: Path, worlds_dir: Path, *, owner: str | None = None, keep: int = 2, deep: bool = True,
            log=print) -> dict:
    src, worlds_dir = Path(src), Path(worlds_dir)
    idx = verify(src, deep=deep)
    uid = gid = None
    if owner:
        import pwd

        pw = pwd.getpwnam(owner)
        uid, gid = pw.pw_uid, pw.pw_gid
    worlds_dir.mkdir(parents=True, exist_ok=True)
    for sub in (".status", ".geo-cache"):
        (worlds_dir / sub).mkdir(exist_ok=True)
        if uid is not None:
            os.chown(worlds_dir / sub, uid, gid)
    report: dict = {"worlds": [], "shared": None}
    tmp_root = worlds_dir / f".unpack-{os.getpid()}"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir()
    try:
        for e in idx.get("worlds") or []:
            wid, cv, key = e["world_id"], str(e["content_version"]), str(e["derive_sha8"])
            row = {"world_id": wid, "content_version": cv, "derive_sha8": key, "world": "skip", "cache": "skip",
                   "pruned": []}
            want_wj = e.get("world_json_sha256")
            want_show = e.get("show_sha256")  # 只有新索引才有；城市演示录制只换录制时也重装该世界
            stale = _installed_cv(worlds_dir, wid) != cv or _status_cv(worlds_dir, wid) != cv
            if (stale or (want_wj and _world_json_sha(worlds_dir, wid) != want_wj)
                    or (want_show and _show_sha(worlds_dir, wid) != want_show)):
                p = src / e["world_pack"]["file"]
                t = tmp_root / f"w-{wid}"
                t.mkdir()
                _extract(p, t, check_tar(p, (wid,), (f".status/{wid}.json", ".status")))
                _chown_tree(t, uid, gid)
                _swap_dir(t / wid, worlds_dir / wid)
                os.replace(t / ".status" / f"{wid}.json", worlds_dir / ".status" / f"{wid}.json")
                row["world"] = "installed"
            entry = f"{cv}-{key}"
            target = worlds_dir / ".geo-cache" / wid / entry
            if not (target / "manifest.json").is_file():
                p = src / e["cache_pack"]["file"]
                t = tmp_root / f"c-{wid}"
                t.mkdir()
                _extract(p, t, check_tar(p, (f".geo-cache/{wid}/{entry}",), (".geo-cache", f".geo-cache/{wid}")))
                _chown_tree(t, uid, gid)
                (worlds_dir / ".geo-cache" / wid).mkdir(exist_ok=True)
                if uid is not None:
                    os.chown(worlds_dir / ".geo-cache" / wid, uid, gid)
                _swap_dir(t / ".geo-cache" / wid / entry, target)
                row["cache"] = "installed"
            row["pruned"] = prune_cache(worlds_dir, wid, entry, keep)
            report["worlds"].append(row)
            log(f"    {wid}: 世界 {row['world']}，派生缓存 {row['cache']}（{entry}）"
                + (f"，清理旧条目 {', '.join(row['pruned'])}" if row["pruned"] else ""))
        sh = idx.get("shared")
        if sh:
            marker = worlds_dir / "_shared" / ".env-pack"
            have = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None
            if have != sh["sha256"] or not (worlds_dir / "_shared" / "env").is_dir():
                p = src / sh["file"]
                t = tmp_root / "shared"
                t.mkdir()
                _extract(p, t, check_tar(p, ("_shared/env",), ("_shared",)))
                _chown_tree(t, uid, gid)
                (worlds_dir / "_shared").mkdir(exist_ok=True)
                if uid is not None:
                    os.chown(worlds_dir / "_shared", uid, gid)
                _swap_dir(t / "_shared" / "env", worlds_dir / "_shared" / "env")
                _atomic_write(marker, sh["sha256"] + "\n")
                if uid is not None:
                    os.chown(marker, uid, gid)
                report["shared"] = "installed"
            else:
                report["shared"] = "skip"
            log(f"    _shared/env: {report['shared']}")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return report


# ---------------------------------------------------------------- 命令行


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="worldpack.py", description="公开演示站的世界包与派生缓存包（AWR-04 §4.4.7）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack", help="打包（开发机）")
    p.add_argument("--worlds-dir", default=str(ROOT / "worlds"))
    p.add_argument("--cache-root", default=None, help="派生缓存根（缺省 AWR_GEO_CACHE_DIR 或 <worlds>/.geo-cache）")
    p.add_argument("--out", required=True)
    p.add_argument("--ids", default="all", help="all（public profile 的七个世界）或逗号分隔的世界 id")
    p.add_argument("--level", type=int, default=1, help="gzip 级别（点云与栅格本身压缩率低，缺省 1）")
    p = sub.add_parser("verify", help="校验索引、sha256 与成员")
    p.add_argument("--src", required=True)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("install", help="装入世界目录（服务器，root 执行时以 --owner 设置属主）")
    p.add_argument("--src", required=True)
    p.add_argument("--worlds-dir", required=True)
    p.add_argument("--owner", default=None)
    p.add_argument("--keep", type=int, default=2, help="每个世界保留的派生缓存条目数（含当前）")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "pack":
            ids = public_worlds() if a.ids == "all" else [x.strip() for x in a.ids.split(",") if x.strip()]
            if not ids:
                print("worldpack: 没有要打包的世界", file=sys.stderr)
                return 2
            cache_root = Path(a.cache_root) if a.cache_root else None
            idx = pack(Path(a.worlds_dir), Path(a.out), ids, cache_root=cache_root, level=max(1, min(9, a.level)))
            total = sum(r["bytes"] for e in idx["worlds"] for r in (e["world_pack"], e["cache_pack"]))
            print(f"    {len(idx['worlds'])} 个世界，合计 {total / 2**30:.2f} GiB → {a.out}/{INDEX}")
        elif a.cmd == "verify":
            idx = verify(Path(a.src))
            if a.json:
                print(json.dumps({"ok": True, "worlds": [e["world_id"] for e in idx["worlds"]]}, ensure_ascii=False))
            else:
                print(f"    校验通过：{', '.join(e['world_id'] for e in idx['worlds'])}")
        else:
            rep = install(Path(a.src), Path(a.worlds_dir), owner=a.owner, keep=max(1, a.keep))
            if a.json:
                print(json.dumps(rep, ensure_ascii=False))
    except PackError as e:
        print(f"worldpack: {e}", file=sys.stderr)
        return 1
    except (OSError, tarfile.TarError, ValueError) as e:
        print(f"worldpack: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
