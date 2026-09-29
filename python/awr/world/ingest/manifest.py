"""`configs/data.yaml`（原始数据地址、字节数与 sha256 的唯一真源）与本机 `MANIFEST.json` 的读写（M03 §6.17；16 §10.4）。

`fetch_urbanscene3d` 实现 `make fetch-data`（`awr data fetch urbanscene3d [--verify] [--force]`，19 §8.2）：
断点续传下载 7z、校验 sha256、解压到临时目录、逐文件核对后原子移入，生成 MANIFEST.json。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

from .types import ConfigError, RawDataError

REPO_ROOT = Path(__file__).resolve().parents[4]
_ID_RE = re.compile(r"^[a-z0-9-]{1,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# 19 §16.2 的 fetch 退出码
EXIT_FETCH_CHECKSUM = 5
EXIT_FETCH_DISK = 7
EXIT_FETCH_TOOL = 8


def repo_root() -> Path:
    return Path(os.environ.get("AWR_REPO_ROOT", REPO_ROOT))


def data_yaml_path() -> Path:
    return Path(os.environ.get("AWR_DATA_YAML", repo_root() / "configs" / "data.yaml"))


def default_raw_dir() -> Path:
    base = os.environ.get("AWR_DATA_DIR")
    return Path(base) / "urbanscene3d" if base else repo_root() / "data" / "raw" / "urbanscene3d"


def default_worlds_dir() -> Path:
    return Path(os.environ.get("AWR_WORLDS_DIR", repo_root() / "worlds"))


@dataclass(frozen=True, slots=True)
class RawFileSpec:
    world_id: str
    name: str
    bytes: int
    points: int
    header_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DataConfig:
    archive: dict
    files: tuple[RawFileSpec, ...]
    raw: dict

    def by_world(self, world_id: str) -> RawFileSpec | None:
        return next((f for f in self.files if f.world_id == world_id), None)

    @property
    def world_ids(self) -> list[str]:
        return [f.world_id for f in self.files]


def _pos_int(v, what: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ConfigError(f"configs/data.yaml: {what} must be a positive integer (got {v!r})")
    return v


def load_data_config(path: Path | None = None) -> DataConfig:
    """读取并校验 `configs/data.yaml`；失败抛 ConfigError（退出码 3）。未知顶层键忽略。"""
    path = Path(path) if path else data_yaml_path()
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"{path} 不存在") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: YAML 解析失败：{e}") from e
    u = (doc or {}).get("urbanscene3d")
    if not isinstance(u, dict):
        raise ConfigError("configs/data.yaml: 缺少 urbanscene3d")
    arch = u.get("archive")
    if not isinstance(arch, dict) or not arch.get("name"):
        raise ConfigError("configs/data.yaml: urbanscene3d.archive 缺失或无 name")
    _pos_int(arch.get("bytes"), "archive.bytes")
    if not _SHA_RE.match(str(arch.get("sha256", ""))):
        raise ConfigError("configs/data.yaml: archive.sha256 必须是 64 位小写十六进制")
    files = []
    for i, f in enumerate(u.get("files") or []):
        wid = str(f.get("world_id", ""))
        if not _ID_RE.match(wid):
            raise ConfigError(f"configs/data.yaml: files[{i}].world_id {wid!r} 不匹配 ^[a-z0-9-]{{1,63}}$")
        if not _SHA_RE.match(str(f.get("sha256", ""))):
            raise ConfigError(f"configs/data.yaml: files[{i}].sha256 必须是 64 位小写十六进制")
        files.append(RawFileSpec(wid, str(f["name"]), _pos_int(f.get("bytes"), f"files[{i}].bytes"),
                                 _pos_int(f.get("points"), f"files[{i}].points"),
                                 _pos_int(f.get("header_bytes"), f"files[{i}].header_bytes"), str(f["sha256"])))
    if not files:
        raise ConfigError("configs/data.yaml: urbanscene3d.files 为空")
    return DataConfig(archive=dict(arch), files=tuple(files), raw=doc)


def read_manifest(raw_dir: Path) -> dict | None:
    p = Path(raw_dir) / "MANIFEST.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def manifest_doc(cfg: DataConfig) -> dict:
    """16 §10.4 的 MANIFEST.json 内容（按 world_id 字母序）。"""
    arch = cfg.archive
    return {"schema": "awr.raw_manifest.v1", "schema_version": "1.0.0", "dataset": "UrbanScene3D",
            "source": {"url": "https://github.com/Linxius/UrbanScene3D/releases (v0.0.1)",
                       "archive": {"name": arch["name"], "bytes": arch["bytes"], "sha256": arch["sha256"]}},
            "files": [{"world_id": f.world_id, "name": f.name, "bytes": f.bytes, "points": f.points,
                       "header_bytes": f.header_bytes, "sha256": f.sha256}
                      for f in sorted(cfg.files, key=lambda f: f.world_id)]}


def sha256_stream(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


# ---------------------------------------------------------------- make fetch-data


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def verify_raw(cfg: DataConfig, raw_dir: Path) -> list[str]:
    """逐文件核对字节数与 sha256；返回不符的文件名列表（缺失也算）。"""
    bad = []
    for f in cfg.files:
        p = raw_dir / f.name
        if not p.exists() or p.stat().st_size != f.bytes or sha256_stream(p) != f.sha256:
            bad.append(f.name)
    return bad


def _download(url: str, dest: Path, expect_bytes: int) -> None:
    """断点续传下载（HTTP Range 或 file:// 镜像）。"""
    have = dest.stat().st_size if dest.exists() else 0
    if have >= expect_bytes:
        return
    if url.startswith("file://"):
        src = Path(urllib.request.url2pathname(url[len("file://"):]))
        with open(src, "rb") as fi, open(dest, "ab") as fo:
            fi.seek(have)
            shutil.copyfileobj(fi, fo, length=1 << 20)
        return
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as r:
        mode = "ab" if (have and getattr(r, "status", 200) == 206) else "wb"   # 服务器不支持 Range 时重新下载
        with open(dest, mode) as f:
            shutil.copyfileobj(r, f, length=1 << 20)


def fetch_urbanscene3d(*, verify_only: bool = False, force: bool = False, raw_dir: Path | None = None) -> int:
    """返回 19 §16.2 退出码（0、5、7、8）。"""
    cfg = load_data_config()
    raw_dir = Path(raw_dir) if raw_dir else default_raw_dir()
    raw_dir.mkdir(parents=True, exist_ok=True)
    if verify_only:
        bad = verify_raw(cfg, raw_dir)
        if bad:
            for n in bad:
                _log(f"sha256 或字节数不符：{n}")
            _log("修复：make fetch-data")
            return EXIT_FETCH_CHECKSUM
        (raw_dir / "MANIFEST.json").write_text(json.dumps(manifest_doc(cfg), indent=1, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
        _log(f"六个原始文件校验通过，已写 {raw_dir / 'MANIFEST.json'}")
        return 0
    if not force and not verify_raw(cfg, raw_dir):
        (raw_dir / "MANIFEST.json").write_text(json.dumps(manifest_doc(cfg), indent=1, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
        _log("原始数据已就绪（字节数与 sha256 全部一致）")
        return 0
    arch = cfg.archive
    need = int(arch["bytes"]) * 3
    if shutil.disk_usage(raw_dir).free < need:
        _log(f"磁盘空间不足：需要约 {need / 1e9:.1f} GB")
        return EXIT_FETCH_DISK
    try:
        import py7zr
    except ImportError:
        py7zr = None
    if py7zr is None and shutil.which("7z") is None:
        _log("缺少解压工具：py7zr 或系统 7z；修复：make setup")
        return EXIT_FETCH_TOOL
    archive = raw_dir / arch["name"]
    urls = list(arch.get("urls") or [])
    mirror = os.environ.get("AWR_DATA_MIRROR")
    if mirror:
        urls.insert(0, f"{mirror.rstrip('/')}/urbanscene3d/{arch['name']}")
    if not archive.exists() or archive.stat().st_size != arch["bytes"] or sha256_stream(archive) != arch["sha256"]:
        part = archive.with_suffix(archive.suffix + ".part")
        for url in urls:
            try:
                _log(f"下载 {url}")
                _download(url, part, int(arch["bytes"]))
                break
            except OSError as e:
                _log(f"下载失败：{e}")
        if not part.exists() or part.stat().st_size != arch["bytes"] or sha256_stream(part) != arch["sha256"]:
            _log(f"归档字节数或 sha256 不符：{arch['name']}")
            return EXIT_FETCH_CHECKSUM
        os.replace(part, archive)
    with tempfile.TemporaryDirectory(dir=raw_dir, prefix=".extract-") as tmp:
        if py7zr is not None:
            with py7zr.SevenZipFile(archive, "r") as z:
                z.extractall(tmp)
        else:
            import subprocess

            subprocess.run(["7z", "x", "-y", f"-o{tmp}", str(archive)], check=True, capture_output=True)
        found = {p.name: p for p in Path(tmp).rglob("*.ply")}
        for f in cfg.files:
            p = found.get(f.name)
            if p is None or p.stat().st_size != f.bytes or sha256_stream(p) != f.sha256:
                _log(f"解压结果字节数或 sha256 不符：{f.name}")
                return EXIT_FETCH_CHECKSUM
        for f in cfg.files:
            os.replace(found[f.name], raw_dir / f.name)
    (raw_dir / "MANIFEST.json").write_text(json.dumps(manifest_doc(cfg), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    _log("原始数据已获取并校验")
    return 0


def check_raw_file(spec: RawFileSpec, raw_dir: Path) -> Path:
    """构建前检查：存在且字节数相符，否则 RawDataError（退出码 4，提示 make fetch-data）。sha256 在读取时流式校验。"""
    p = Path(raw_dir) / spec.name
    if not p.exists():
        raise RawDataError(f"原始数据缺失：{p}；修复：make fetch-data", missing=True)
    size = p.stat().st_size
    if size != spec.bytes:
        raise RawDataError(f"{spec.name} 字节数 {size} 与 configs/data.yaml 的 {spec.bytes} 不符；修复：make fetch-data")
    return p
