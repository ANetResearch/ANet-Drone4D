"""nginx 站点文件的最小解析器（tests/ops 用；M00，D2 WP-16）。

把 `tools/deploy/emax/drone4d.agentnetwork.org.cn.conf` 解析成指令树：`Directive(name, args, block)`，`block` 为子指令列表或
None。支持 `#` 注释、单双引号（引号内的 `{`、`}`、`;`、`#` 不是语法字符，反斜杠转义按 nginx 规则保留）。只用于结构断言，
不是 nginx 语义的完整实现；有 nginx 可执行文件时 `test_nginx_static_parity.py` 另做真实对拍。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "tools" / "deploy" / "emax" / "drone4d.agentnetwork.org.cn.conf"
BOOTSTRAP = ROOT / "tools" / "deploy" / "emax" / "drone4d.agentnetwork.org.cn.bootstrap.conf"


@dataclass
class Directive:
    name: str
    args: list[str]
    block: list[Directive] | None = None
    line: int = 0

    def find(self, name: str) -> list[Directive]:
        return [d for d in (self.block or []) if d.name == name]

    def one(self, name: str) -> Directive:
        hits = self.find(name)
        assert len(hits) == 1, f"{self.name} {self.args}: 期望 1 条 {name}，实际 {len(hits)}"
        return hits[0]

    def values(self, name: str) -> list[list[str]]:
        return [d.args for d in self.find(name)]


def tokenize(text: str) -> list[tuple[str, int, bool]]:
    """(token, line, quoted)；`{`、`}`、`;` 单独成词。"""
    out: list[tuple[str, int, bool]] = []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif c in "{};":
            out.append((c, line, False))
            i += 1
        elif c in "\"'":
            q, j, buf = c, i + 1, []
            while j < n and text[j] != q:
                if text[j] == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    buf.append(nxt if nxt in "\"'\\" else "\\" + nxt)
                    j += 2
                    continue
                if text[j] == "\n":
                    line += 1
                buf.append(text[j])
                j += 1
            out.append(("".join(buf), line, True))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "{};" and text[j] not in "\"'":
                j += 1
            out.append((text[i:j], line, False))
            i = j
    return out


def parse(text: str) -> Directive:
    toks = tokenize(text)
    pos = 0

    def block() -> list[Directive]:
        nonlocal pos
        items: list[Directive] = []
        while pos < len(toks):
            tok, line, quoted = toks[pos]
            if tok == "}" and not quoted:
                pos += 1
                return items
            words: list[str] = []
            while pos < len(toks) and not (toks[pos][0] in ("{", ";") and not toks[pos][2]):
                if toks[pos][0] == "}" and not toks[pos][2]:
                    raise ValueError(f"第 {toks[pos][1]} 行：意外的 }}")
                words.append(toks[pos][0])
                pos += 1
            if pos >= len(toks):
                raise ValueError(f"第 {line} 行：指令未结束")
            end = toks[pos][0]
            pos += 1
            d = Directive(words[0], words[1:], line=line)
            if end == "{":
                d.block = block()
            items.append(d)
        return items

    top = block()
    if pos != len(toks):
        raise ValueError("多余的 }")
    return Directive("main", [], top)


def load(path: Path = SITE) -> Directive:
    return parse(path.read_text(encoding="utf-8"))


def locations(server: Directive) -> dict[str, Directive]:
    """`location` 的修饰符与路径拼成键：`= /api/rt`、`~ ^/worlds/…`、`/api/`、`^~ /api/viewer/queue`。"""
    return {" ".join(d.args): d for d in server.find("location")}


def https_server(main: Directive) -> Directive:
    for s in main.find("server"):
        if any("443" in " ".join(a) for a in s.values("listen")):
            return s
    raise AssertionError("没有 443 server")


def nginx_regex_to_python(rx: str) -> re.Pattern[str]:
    """PCRE 命名捕获 `(?<name>…)` → Python `(?P<name>…)`（本文件的正则只用到这一处差异）。"""
    return re.compile(re.sub(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>", r"(?P<\1>", rx))


def eval_map(main: Directive, var_in: str, var_out: str, value: str, variables: dict[str, str] | None = None) -> str:
    """按 nginx map 的规则求值：精确字符串优先，其后按出现顺序的第一个匹配正则，最后 default；值可引用命名捕获与变量。"""
    blk = next(d for d in main.find("map") if d.args == [var_in, var_out])
    exact = {d.name: d.args[0] for d in blk.block or [] if not d.name.startswith("~") and d.name != "default"}
    if value in exact:
        return exact[value]
    env = dict(variables or {})
    for d in blk.block or []:
        if d.name.startswith("~"):
            m = nginx_regex_to_python(d.name[1:]).search(value)
            if m:
                env.update({k: v for k, v in m.groupdict().items() if v is not None})
                return _expand(d.args[0], env)
    default = next((d.args[0] for d in blk.block or [] if d.name == "default"), "")
    return _expand(default, env)


def _expand(template: str, env: dict[str, str]) -> str:
    return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", lambda m: env.get(m.group(1), ""), template)
