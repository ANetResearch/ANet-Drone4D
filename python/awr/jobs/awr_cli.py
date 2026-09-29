"""`awr` CLI 插件（M11 的 `awr.runtime.cli` 按 `_PLUGIN_MODULES` 导入并调用 `register(sub)`）。

注册 `awr data *`（M03-FR-045；19 §7.6、§8.2、§8.5）：
- `awr data fetch urbanscene3d [--verify] [--force]`：下载、续传并校验原始数据，补写 MANIFEST.json；
- `awr data fetch worlds`：预构建世界制品（P1）。发布位置未定（`configs/data.yaml` 的 `worlds_prebuilt.base_url` 为 null
  且未设置 `AWR_DATA_MIRROR`）时以退出码 4 结束，并提示本机执行 `make worlds`；
- `awr data prune-archive urbanscene3d`：删除本地 7z 归档（P2）。

模块放在 `awr/jobs/`（M03 所有）而不是 `awr/world/awr_cli.py`：后者不在 03 §4.3 路径所有权表内。
"""

from __future__ import annotations

import os
import sys

from awr.world.ingest import data_cli
from awr.world.ingest.manifest import load_data_config
from awr.world.ingest.types import WorldpkgError


def _fetch_worlds(a) -> int:
    cfg = load_data_config()
    pre = cfg.raw.get("worlds_prebuilt") or {}
    base = pre.get("base_url") or os.environ.get("AWR_DATA_MIRROR")
    if not base:
        print("预构建世界的发布位置未定（configs/data.yaml worlds_prebuilt.base_url 为 null，且未设置 AWR_DATA_MIRROR）",
              file=sys.stderr)
        print("make worlds")
        return 4
    print(f"预构建世界制品下载（19 §8.5，P1）尚未实现：{base}", file=sys.stderr)
    print("make worlds")
    return 8


def _run(a) -> int:
    if getattr(a, "dataset", None) == "worlds" and a.data_cmd == "fetch":
        return _fetch_worlds(a)
    try:
        return int(a.data_fn(a))
    except WorldpkgError as e:
        print(f"错误（退出码 {e.exit_code}）：{e}", file=sys.stderr)
        print("make fetch-data")
        return e.exit_code


def register(sub) -> None:
    if "data" in sub.choices:
        return
    p = sub.add_parser("data", help="原始数据与预构建世界（M03）")
    s = p.add_subparsers(dest="data_cmd", required=True)
    data_cli.register(s, extra_datasets=("worlds",))
    p.set_defaults(fn=_run)
