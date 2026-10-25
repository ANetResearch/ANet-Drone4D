#!/usr/bin/env python3
"""pylib-gz 核对（K07）：在 d3-gz 容器内运行，核对目标目录中已安装的分发包与锁文件逐包一致（名称、版本，无多余、无缺失），
并导入宿主侧必需模块（numpy 取自目标目录而非系统包）。输出 JSON，一致且导入全部成功时退出码 0。

用法：python3 pylib_check.py <pylib 目录> <pylib-gz.lock> [--repo <仓库副本，含 python/>]
给出 `--repo` 时另导入 awr 的 Env、录制与网格查询入口（与 run_proto_board.sh 的宿主同一路径）。
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from importlib import metadata
from pathlib import Path

MODULES = ("numpy", "scipy", "open3d", "zmq", "google.protobuf", "jsonschema", "msgpack", "yaml", "mcap", "mcap.writer", "zstandard",
           "lz4.frame", "mcap_protobuf", "foxglove_schemas_protobuf", "pytest")
AWR_MODULES = ("awr.agentsdk.env.core", "awr.agentsdk.env.recorder", "awr.world.mesh.query", "awr.bench.eval.evaluator")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)==([^\s;\\]+)", line)
        if m:
            out[_norm(m[1])] = m[2]
    return out


def main(argv: list[str]) -> int:
    target, lock = Path(argv[0]), Path(argv[1])
    repo = Path(argv[argv.index("--repo") + 1]) if "--repo" in argv else None
    want = locked(lock)
    have = {_norm(d.metadata["Name"]): d.version for d in metadata.distributions(path=[str(target)])}
    diff = {"missing": sorted(set(want) - set(have)), "extra": sorted(set(have) - set(want)),
            "version": {k: [have[k], want[k]] for k in sorted(set(want) & set(have)) if have[k] != want[k]}}
    sys.path.insert(0, str(target))
    if repo is not None:
        sys.path.insert(1, str(repo / "python"))
    imports, errors = {}, {}
    for mod in (*MODULES, *(AWR_MODULES if repo is not None else ())):
        try:
            m = importlib.import_module(mod)
            imports[mod] = str(getattr(m, "__file__", "") or "")
        except Exception as e:                                   # 汇总全部导入失败
            errors[mod] = f"{type(e).__name__}: {e}"
    import numpy

    np_ok = Path(numpy.__file__).resolve().is_relative_to(target.resolve())
    ok = not any(diff.values()) and not errors and np_ok
    print(json.dumps({"target": str(target), "n_locked": len(want), "n_installed": len(have), "diff": diff, "import_errors": errors,
                      "numpy": {"version": numpy.__version__, "from_target": np_ok}, "python": sys.version.split()[0], "ok": ok},
                     ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
