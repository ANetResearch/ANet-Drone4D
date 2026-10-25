"""已弃用的兼容别名：`drone4d` 即 `anet4d`（ANet4D V0.3.2 保留一个版本，下一版本移除）。

V0.3.1 及以前 SDK 的发行包与导入包名为 `drone4d`。本包在导入时发出 DeprecationWarning，并把 `drone4d.<子模块>` 映射到
`anet4d.<子模块>` 的同一模块对象（`drone4d.baselines is anet4d.baselines`），类、常量与基线登记表与 `anet4d` 完全相同。
容器入口 `python -m drone4d.serve` 与命令 `drone4d-agent` 由 `drone4d/serve.py` 转发到 `anet4d.serve`。
新代码请改为 `import anet4d`、`python -m anet4d.serve`、`anet4d-agent`。
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import warnings

import anet4d as _anet4d
from anet4d import *  # noqa: F403
from anet4d import __version__  # noqa: F401 (explicit re-export)

MESSAGE = ("the 'drone4d' package is a deprecated alias of 'anet4d' (ANet4D V0.3.2) and will be removed in the next release; "
           "use 'import anet4d', 'python -m anet4d.serve' or 'anet4d-agent'")
warnings.warn(MESSAGE, DeprecationWarning, stacklevel=2)

_OLD, _NEW = __name__, _anet4d.__name__
_LOCAL = frozenset({f"{_OLD}.serve"})


class _AliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """`drone4d.x.y` → 已导入的 `anet4d.x.y`（同一模块对象）。排在 sys.meta_path 首位，避免常规查找器沿 `anet4d` 子包的
    `__path__` 以 `drone4d.*` 之名再载入一份；本包目录中实际存在的模块（`_LOCAL`，即 serve.py）交给常规查找器。"""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + ".") or fullname in _LOCAL:
            return None
        real = _NEW + fullname[len(_OLD):]
        try:
            spec = importlib.util.find_spec(real)
        except (ImportError, ValueError):
            return None
        if spec is None:
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=spec.submodule_search_locations is not None)

    def create_module(self, spec):
        return importlib.import_module(_NEW + spec.name[len(_OLD):])

    def exec_module(self, module):
        return None


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())
