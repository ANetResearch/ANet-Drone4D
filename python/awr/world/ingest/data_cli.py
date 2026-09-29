"""`awr data fetch urbanscene3d [--verify] [--force]` 与 `awr data prune-archive urbanscene3d`（M03-FR-045；19 §8.2）。

`register(subparsers)` 供 M11 的 `awr` CLI（`awr/runtime/cli.py`）挂接；本模块也可独立运行：
`python -m awr.world.ingest.data_cli fetch urbanscene3d --verify`。退出码按 19 §16.2（0、5、7、8；3 参数或配置错误）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .manifest import default_raw_dir, fetch_urbanscene3d, load_data_config
from .types import WorldpkgError


def _fetch(a) -> int:
    if a.dataset != "urbanscene3d":
        print(f"未知数据集 {a.dataset}（D1 只有 urbanscene3d）", file=sys.stderr)
        return 3
    return fetch_urbanscene3d(verify_only=a.verify, force=a.force, raw_dir=Path(a.raw) if a.raw else None)


def _prune(a) -> int:
    cfg = load_data_config()
    p = (Path(a.raw) if a.raw else default_raw_dir()) / cfg.archive["name"]
    if p.exists():
        p.unlink()
        print(f"removed {p}")
    return 0


def register(sub, extra_datasets: tuple[str, ...] = ()) -> None:
    p = sub.add_parser("fetch", help="下载并校验原始数据（make fetch-data）")
    p.add_argument("dataset", choices=["urbanscene3d", *extra_datasets])
    p.add_argument("--verify", action="store_true", help="只校验字节数与 sha256，并补写 MANIFEST.json")
    p.add_argument("--force", action="store_true")
    p.add_argument("--raw")
    p.set_defaults(data_fn=_fetch)
    p = sub.add_parser("prune-archive", help="删除本地 7z 归档（P2）")
    p.add_argument("dataset", choices=["urbanscene3d"])
    p.add_argument("--raw")
    p.set_defaults(data_fn=_prune)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="awr data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    register(sub)
    a = ap.parse_args(argv)
    try:
        return int(a.data_fn(a))
    except WorldpkgError as e:
        print(f"错误（退出码 {e.exit_code}）：{e}", file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
