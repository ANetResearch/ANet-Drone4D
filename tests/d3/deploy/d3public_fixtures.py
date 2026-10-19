"""公开样例发布线测试的公共夹具（tools/deploy/emax/d3-public；D3-AC-21）：合成的版本目录、草稿索引与 /lab/ 构建。

制品布局按 AWR-17 §18.8.5：`events/<eid>/tape.bin`、`runs/<rid>/video/<uav>_<cam>.mp4`、`runs/<rid>/slices/<slice>.rrd`、
`runs/<rid>/slices/basemap.rrd`，另有 LICENSE、NOTICE。内容为确定的伪随机字节（各版本可按需改动一个文件，验证 --link-dest
的硬链接去重与磁盘账）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
D3PUB = ROOT / "tools" / "deploy" / "emax" / "d3-public"
MIB = 1 << 20
RUN = "run-abcdefghij"
EVENT = "crop-handoff-001"


def load(name: str) -> ModuleType:
    """d3-public 下的脚本模块（labctl、labsite、verify、drill）。"""
    if str(D3PUB) not in sys.path:
        sys.path.insert(0, str(D3PUB))
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, D3PUB / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def blob(n: int, seed: str) -> bytes:
    """n 字节的确定伪随机内容（sha256 计数器流）。"""
    out = bytearray()
    k = 0
    while len(out) < n:
        out += hashlib.sha256(f"{seed}:{k}".encode()).digest() * 64
        k += 1
    return bytes(out[:n])


def write(root: Path, rel: str, data: bytes | str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)
    return p


def version_dir(parent: Path, version: str, *, video_mib: float = 1.0, slice_seed: str = "s1", big_mib: float = 0.0) -> Path:
    """一个版本目录（未生成 SHA256SUMS 与索引）；big_mib > 0 时另放一个该大小的视频（限速与并发用例）。"""
    v = parent / version
    write(v, "LICENSE", "CC-BY-4.0\n")
    write(v, "NOTICE", "OpenStreetMap contributors (ODbL-1.0)\n")
    write(v, f"events/{EVENT}/tape.bin", blob(64 * 1024, "tape"))
    write(v, f"events/{EVENT}/tape.json", json.dumps({"event": EVENT}) + "\n")
    write(v, f"runs/{RUN}/video/uav-01_cam0.mp4", blob(int(video_mib * MIB), "video-rgb"))
    write(v, f"runs/{RUN}/video/uav-01_thermal0.mp4", blob(256 * 1024, "video-thermal"))
    write(v, f"runs/{RUN}/slices/encounter_ab.rrd", blob(512 * 1024, slice_seed))
    write(v, f"runs/{RUN}/slices/basemap.rrd", blob(300 * 1024, "basemap"))
    write(v, f"runs/{RUN}/tracks_obs.bin", blob(32 * 1024, "tracks"))
    if big_mib:
        write(v, f"runs/{RUN}/video/uav-02_cam0.mp4", blob(int(big_mib * MIB), "video-big"))
    return v


def draft(*, big: bool = False) -> dict:
    videos = [{"uav": "uav-01", "cam": "cam0", "kind": "rgb", "path": f"runs/{RUN}/video/uav-01_cam0.mp4"},
              {"uav": "uav-01", "cam": "thermal0", "kind": "thermal", "path": f"runs/{RUN}/video/uav-01_thermal0.mp4"}]
    if big:
        videos.append({"uav": "uav-02", "cam": "cam0", "kind": "rgb", "path": f"runs/{RUN}/video/uav-02_cam0.mp4"})
    return {"license": {"files": ["LICENSE", "NOTICE"], "attribution": ["OpenStreetMap contributors（ODbL-1.0）"]},
            "worlds": [{"id": "oryahovo-crop1"}],
            "events": [{"id": EVENT, "version": 1, "world_id": "oryahovo-crop1", "tape": f"events/{EVENT}/tape.bin",
                        "tape_json": f"events/{EVENT}/tape.json",
                        "runs": [{"run_id": RUN, "policy": "track_share", "n_uav": 3, "obs_level": "L2", "videos": videos,
                                  "tracks_obs": f"runs/{RUN}/tracks_obs.bin"}]}]}


def slices_manifest() -> dict:
    """R04 `slices.json`（drone4d.bench.public_slice.v1）中发布线用到的部分：run_id 与 index 片段（rrd 为文件名）。"""
    return {"schema": "drone4d.bench.public_slice.v1", "run_id": RUN,
            "index": {"slices": [{"event_id": "encounter_ab", "t_from_s": 17.5, "t_to_s": 27.5, "rrd": "encounter_ab.rrd",
                                  "bytes": 0, "sha256": "0" * 64}], "basemap": "basemap.rrd"}}


def lab_dist(parent: Path, release: str = "20261019-060000-2a8f58d555") -> Path:
    """最小的 /lab/ 构建（index.html 引用 /lab/assets/*.js；build-info.json 带 release）。"""
    d = parent / f"lab-dist-{release}"
    write(d, "index.html", '<!doctype html><html><head><script type="module" src="/lab/assets/index-Ab12Cd34.js"></script>'
                           f'</head><body><div id="root" data-release="{release}"></div></body></html>\n')
    write(d, "assets/index-Ab12Cd34.js", f"console.log('lab {release}');\n")
    write(d, "assets/index-Ab12Cd34.css", "body{margin:0}\n")
    write(d, "brand/anet-logo.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>\n")
    write(d, "build-info.json", json.dumps({"version": "0.3.0", "channel": "demo", "commit": release[-10:], "dirty": False,
                                            "built_at": "2026-10-19T06:00:00Z", "release": release}) + "\n")
    return d


def published(labctl: ModuleType, parent: Path, version: str, *, release: str = "20261019-060000-2a8f58d555", **kw) -> tuple[Path, Path]:
    """生成索引后的版本目录与构建：(版本目录, 构建目录)。"""
    v = version_dir(parent, version, **{k: x for k, x in kw.items() if k != "big"})
    d = draft(big=bool(kw.get("big_mib")))
    labctl.merge_slices(d, [slices_manifest()])
    labctl.build_index(v, d, lab_build=release, built_unix_ns=1_793_000_000_000_000_000)
    return v, lab_dist(parent, release)
