"""RFC 8785 JSON 规范化（JCS）：磁带规范头与 `event_sha256` 的字节来源（M23 §6.7.1、§6.7.5）。

对象键按 UTF-16 码元排序；字符串只转义 `"`、`\\` 与 U+0000–U+001F（其余原样 UTF-8）；数字按 ECMAScript `Number.prototype.toString`
（整数值不带小数点，|x| ≥ 1e21 或 < 1e-6 用指数形式 `1e+21`、`1e-7`）；不允许 NaN 与无穷。布尔与 null 为字面量。
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

__all__ = ["jcs", "jcs_number"]

_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _str(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def jcs_number(x: float | int) -> str:
    if isinstance(x, bool):
        raise TypeError("bool is not a number")
    if isinstance(x, int):
        if abs(x) >= 2 ** 53:
            raise ValueError("integer outside the IEEE-754 exact range")
        return str(x)
    if not math.isfinite(x):
        raise ValueError("NaN and infinity are not valid JSON")
    if x == 0:
        return "0"
    if x.is_integer() and abs(x) < 2 ** 53:
        return str(int(x))
    sign = "-" if x < 0 else ""
    t = Decimal(repr(abs(x))).as_tuple()           # repr 为最短往返表示，有效数字与 ECMAScript 相同
    digits = "".join(map(str, t.digits)).rstrip("0") or "0"
    n = len("".join(map(str, t.digits))) + int(t.exponent)    # 值 = 0.d1d2…dk × 10^n
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    m = digits[0] + ("." + digits[1:] if k > 1 else "")
    return f"{sign}{m}e{'+' if e > 0 else '-'}{abs(e)}"


def _enc(o: Any, out: list[str]) -> None:
    if o is None:
        out.append("null")
    elif o is True:
        out.append("true")
    elif o is False:
        out.append("false")
    elif isinstance(o, (int, float)):
        out.append(jcs_number(o))
    elif isinstance(o, str):
        out.append(_str(o))
    elif isinstance(o, (list, tuple)):
        out.append("[")
        for i, v in enumerate(o):
            if i:
                out.append(",")
            _enc(v, out)
        out.append("]")
    elif isinstance(o, dict):
        out.append("{")
        for i, k in enumerate(sorted(o, key=lambda s: s.encode("utf-16-be"))):
            if not isinstance(k, str):
                raise TypeError("object keys must be strings")
            if i:
                out.append(",")
            out.append(_str(k))
            out.append(":")
            _enc(o[k], out)
        out.append("}")
    else:
        raise TypeError(f"not JSON-serialisable: {type(o).__name__}")


def jcs(obj: Any) -> bytes:
    """RFC 8785 规范化字节（UTF-8）。"""
    out: list[str] = []
    _enc(obj, out)
    return "".join(out).encode("utf-8")
