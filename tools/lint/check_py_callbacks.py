#!/usr/bin/env python3
"""PY-CB-01: bus callbacks only enqueue (AWR-18 §13.1; M11 §9.5 item 3; g05 §10 R4).

The callback passed to `serve`, `subscribe` or `watch` (second positional argument, or keyword handler / callback / cb /
on_request / on_sample) runs on a zenoh callback thread and may only hand work over. Allowed callbacks:
  - a bound method or function whose name is one of put, put_nowait, call_soon_threadsafe, set_result, append;
  - a lambda whose body is one such call;
  - a coroutine function (the awr.runtime.bus wrapper posts it with call_soon_threadsafe);
  - a function defined in the same file whose body holds only a docstring, such calls, local aliases (name = name,
    attribute or constant), subscript assignments (dict writes), a bare return, and if statements built from these.
awr/runtime/bus.py (the wrapper itself) is exempt. Scope: python/awr/** (18 §13.1). The rule was a pytest of M11
(tests/runtime/test_lint_runtime.py) and moved here at the M11 request (M11-R-to-M00 item 5).

Output `<file>:<line>:<col> PY-CB-01 <message>`; exit 0 pass, 1 violations, 2 tool error; summary in runs/lint/.
Usage: python tools/lint/check_py_callbacks.py [paths...]
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "python" / "awr"
EXEMPT = {PKG / "runtime" / "bus.py"}
REGISTER = {"serve", "subscribe", "watch"}
CB_KEYWORDS = {"handler", "callback", "cb", "on_request", "on_sample"}
ALLOWED_CALLS = {"put", "put_nowait", "call_soon_threadsafe", "set_result", "append"}


def _allowed_call(v: ast.AST) -> bool:
    return isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr in ALLOWED_CALLS


def _stmt_ok(st: ast.stmt) -> bool:
    if isinstance(st, ast.Expr):
        return isinstance(st.value, ast.Constant) or _allowed_call(st.value)
    if isinstance(st, ast.Assign):
        if all(isinstance(t, ast.Subscript) for t in st.targets):
            return True
        return all(isinstance(t, ast.Name) for t in st.targets) and isinstance(st.value, (ast.Name, ast.Attribute, ast.Constant))
    if isinstance(st, ast.If):
        return all(_stmt_ok(x) for x in st.body) and all(_stmt_ok(x) for x in st.orelse)
    if isinstance(st, ast.Return):
        return st.value is None
    return isinstance(st, ast.Pass)


def _callback_ok(node: ast.AST, defs: dict[str, list[ast.AST]]) -> bool:
    if isinstance(node, ast.Lambda):
        return _allowed_call(node.body)
    name = node.attr if isinstance(node, ast.Attribute) else (node.id if isinstance(node, ast.Name) else None)
    if name in ALLOWED_CALLS and isinstance(node, ast.Attribute):
        return True
    if name in defs:
        # every same-named definition in the file must qualify (methods of different classes may share a name)
        return all(isinstance(fn, ast.AsyncFunctionDef) or all(_stmt_ok(st) for st in fn.body) for fn in defs[name])  # type: ignore[attr-defined]
    return False


def check_file(path: Path, out: list[tuple[str, int, int, str, str]]) -> None:
    rel = path.relative_to(ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        out.append((rel, e.lineno or 1, (e.offset or 0) + 1, "PY-CB-01", f"syntax error: {e.msg}"))
        return
    defs: dict[str, list[ast.AST]] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(n.name, []).append(n)
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in REGISTER):
            continue
        cb = n.args[1] if len(n.args) >= 2 else next((k.value for k in n.keywords if k.arg in CB_KEYWORDS), None)
        if cb is None or _callback_ok(cb, defs):
            continue
        desc = ast.unparse(cb)[:60]
        out.append((rel, cb.lineno, cb.col_offset + 1, "PY-CB-01",
                    f"{n.func.attr}() callback {desc!r} must only enqueue (put, call_soon_threadsafe, set_result, dict writes)"))


def main(argv: list[str] | None = None) -> int:
    t0 = time.time()
    args = [a for a in (argv if argv is not None else sys.argv[1:]) if not a.startswith("--")]
    try:
        files: list[Path] = []
        for a in args or [str(PKG)]:
            p = Path(a).resolve()
            files += sorted(p.rglob("*.py")) if p.is_dir() else [p]
        files = [f for f in files if "__pycache__" not in f.parts and f.is_relative_to(ROOT) and f not in EXEMPT]
        violations: list[tuple[str, int, int, str, str]] = []
        for f in files:
            check_file(f, violations)
    except Exception as e:  # tool error, exit 2
        print(f"check_py_callbacks: tool error: {e!r}", file=sys.stderr)
        return 2
    violations = sorted(set(violations))
    for v in violations:
        print(f"{v[0]}:{v[1]}:{v[2]} {v[3]} {v[4]}")
    summary = {"tool": "check_py_callbacks", "ok": not violations, "violations": len(violations), "files": len(files),
               "elapsed_ms": round((time.time() - t0) * 1000), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "items": [dict(zip(("file", "line", "col", "rule", "msg"), v, strict=True)) for v in violations[:2000]]}
    try:
        out = Path(os.environ.get("AWR_RUNS_DIR", ROOT / "runs")) / "lint"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-check_py_callbacks.json").write_text(json.dumps(summary, indent=1) + "\n")
    except OSError:
        pass
    print(f"check_py_callbacks: {'ok' if not violations else f'{len(violations)} violation(s)'} ({summary['elapsed_ms']} ms, {len(files)} files)",
          file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
