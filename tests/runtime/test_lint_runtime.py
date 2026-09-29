"""M11 编码规范（M11 §9.5）：zenoh 与 key 字面量边界、格式串只在生成的 frame.py、回调只入队、禁止字形。"""

from __future__ import annotations

import ast
import re
import subprocess
import sys

import rtlib

RUNTIME = rtlib.ROOT / "python" / "awr" / "runtime"
OURS = [*RUNTIME.glob("*.py"), *(rtlib.ROOT / "tools" / "fake").glob("*.py"), *(rtlib.ROOT / "tools" / "bench" / "ipc").glob("*.py")]
# struct 格式串规则同样覆盖网关 awr.api（M11 §9.5 第 2 条；请求 M11-R-to-M00 第 5 条）；回调规则已并入 tools/lint（PY-CB-01）
STRUCT_SCOPE = [*OURS, *(rtlib.ROOT / "python" / "awr" / "api").rglob("*.py")]
ALLOWED_CB_CALLS = {"put", "put_nowait", "call_soon_threadsafe", "set_result", "append"}


def test_check_py_imports_clean_for_runtime() -> None:
    r = subprocess.run([sys.executable, str(rtlib.ROOT / "tools" / "lint" / "check_py_imports.py"), str(RUNTIME)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_zenoh_imported_only_in_bus() -> None:
    for f in RUNTIME.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        if "zenoh" in mods:
            assert f.name == "bus.py", f


def test_no_struct_format_literals() -> None:
    """struct 格式串只允许在生成的 awr/contracts/frame.py（M11 §9.5 第 2 条）。"""
    for f in STRUCT_SCOPE:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = n.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
                if name in ("Struct", "pack", "unpack", "pack_into", "unpack_from", "calcsize") and n.args:
                    a0 = n.args[0]
                    is_struct_mod = isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "struct"
                    if (name == "Struct" or is_struct_mod) and isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        raise AssertionError(f"{f}:{n.lineno} 格式串字面量 {a0.value!r}")


def _stmt_ok(st: ast.stmt) -> bool:
    """回调函数体只允许：文档串、允许的方法调用（入队、投递、置结果）、局部别名赋值、下标赋值与由这些构成的 if。"""
    if isinstance(st, ast.Expr):
        v = st.value
        if isinstance(v, ast.Constant):
            return True
        return isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr in ALLOWED_CB_CALLS
    if isinstance(st, ast.Assign):
        if all(isinstance(t, ast.Subscript) for t in st.targets):
            return True
        return all(isinstance(t, ast.Name) for t in st.targets) and isinstance(st.value, (ast.Name, ast.Attribute, ast.Constant))
    if isinstance(st, ast.If):
        return all(_stmt_ok(x) for x in st.body) and all(_stmt_ok(x) for x in st.orelse)
    if isinstance(st, ast.Return):
        return st.value is None
    return False


def _callback_ok(node: ast.AST, defs: dict[str, ast.AST]) -> bool:
    if isinstance(node, ast.Lambda):
        b = node.body
        return isinstance(b, ast.Call) and isinstance(b.func, ast.Attribute) and b.func.attr in ALLOWED_CB_CALLS
    name = node.attr if isinstance(node, ast.Attribute) else (node.id if isinstance(node, ast.Name) else None)
    if name in defs:
        fn = defs[name]
        if isinstance(fn, ast.AsyncFunctionDef):
            return True  # 协程 handler 由包装器经 call_soon_threadsafe 投递到事件循环
        return all(_stmt_ok(st) for st in fn.body)  # type: ignore[attr-defined]
    return isinstance(node, ast.Attribute) and node.attr in ALLOWED_CB_CALLS


def test_bus_callbacks_only_enqueue() -> None:
    """Bus.serve / Bus.subscribe / watch / call_cb 的回调只允许入队（M11 §9.5 第 3 条；g05 §10 R4）。"""
    targets = [*RUNTIME.glob("*.py"), *(rtlib.ROOT / "tools" / "bench" / "ipc").glob("*.py")]
    bad = []
    for f in targets:
        if f.name == "bus.py":
            continue  # 包装器本身
        tree = ast.parse(f.read_text(encoding="utf-8"))
        defs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        bad.extend(f"{f.name}:{n.lineno}" for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in ("serve", "subscribe", "watch") and len(n.args) >= 2
                   and not _callback_ok(n.args[1], defs))
    assert bad == []


_RANGES = ((0x2194, 0x21FF), (0x25A0, 0x25FF), (0x2600, 0x26FF), (0x2700, 0x27BF), (0xFE0F, 0xFE0F), (0x1F000, 0x1FAFF))
FORBIDDEN = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _RANGES) + "]")  # 源码中只写码位（D1-AC-20）


def test_no_emoji_or_forbidden_glyphs() -> None:
    files = [*OURS, *rtlib.FAKE_CHILD.parent.glob("*.py"), rtlib.ROOT / "configs" / "runtime.yaml", RUNTIME / "zenoh.json5"]
    mk = rtlib.ROOT / "mk" / "m11.mk"
    if mk.exists():
        files.append(mk)
    for f in files:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            assert not FORBIDDEN.search(line), f"{f}:{i}"
