#!/usr/bin/env python3
"""tools/deploy/emax/d3-public/verify.py：公开样例上线后的验收检查（D3-AC-21；AWR-05 §7.9、AWR-17 §18.8.5）。

站点检查（经 HTTPS 访问站点，emax 上用 `--connect 127.0.0.1` 走本机回环，开发机演练用 drill.py 起的容器 nginx）：

| 检查 | 判据（D3-AC-21 原文或 §7.9 实现约束） |
|---|---|
| bench404 | `/api/bench`、`/api/bench/**`（含 `/api/bench/v1/public/**`）的 GET、HEAD、POST 一律 404 |
| lab | `/lab/` 与 History 路由（`/lab/samples/<rid>`）200 且为同一份 index.html（no-cache）；`/lab` 301；assets immutable，缺失 404；非 GET/HEAD 405；点路径 404 |
| static | `index.json` 200、no-cache、ETag 且 If-None-Match 304、schema 与版本；索引引用的制品 HEAD 200 且长度与索引一致、immutable；`SHA256SUMS` 与一个小文件的摘要一致；目录、`.staging`、`.index.json`、非法版本名 404；POST 405 |
| rate | 单连接整段下载：前 8 MiB 不限（达到 limit_rate 的数倍），之后稳态 ≤ 2 MiB/s（容差 5 %，跳过越过阈值后的 2 s）；分段请求（Range）从首字节起限速 |
| conn_ip | 同一地址前缀第 5 条并发下载 429（limit_conn 4） |
| conn_all | 全站第 33 条并发下载 429（limit_conn 32；需要 ≥ 9 个源地址，只在回环上可测：`--source-ips 127.0.0.2-127.0.0.12`） |
| nmax | `/api/sandbox/v1/capacity` 的 sandbox.max 等于 `--expect-nmax`（D2-AC-04 的 N_max 不变） |
| d2 | `--expect d2`（回滚到 D2 后）：`/lab-static/index.json` 不再是静态索引、`/lab/` 不再返回 /lab/ 构建；落地页 200 |

本机检查（`--local`，emax 上以 root 执行）：

| 检查 | 判据 |
|---|---|
| root | 公开组合根 `AWR_D3_ENABLED = 0`：`/etc/anet-drone4d/env` 未设或为 0，且没有任何进程的环境为 1；进程表中无 D3 进程与 d3-* 容器 |
| disk | D3 总占用（含保留版本，硬链接去重）≤ 3 GB（Q-D3-02），无 > 64 MB 的单文件 |
| content | 当前版本目录 `labctl.py check` 通过：SHA256SUMS、test 集文件 0、真值文件 0 |

报告 `awr.deploy.d3_public.verify.v1`（`--out`）；退出码 0 通过、1 不通过、2 参数错误或站点不可达。只用标准库。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import labctl  # noqa: E402

SCHEMA = "awr.deploy.d3_public.verify.v1"
DOMAIN = "anet4d.agentnetwork.org.cn"   # 公开站点；旧域名 drone4d.agentnetwork.org.cn 只做 301
MIB = 1 << 20
RATE_BPS = 2 * MIB                 # limit_rate 2m（nginx 的 m 为 MiB）
RATE_AFTER = 8 * MIB               # limit_rate_after 8m
RATE_TOL = 0.05
RATE_SKIP_S = 2.0                # 越过阈值后跳过的秒数（预支额度与套接字缓冲）
RATE_WINDOW_S = 5.0              # 稳态窗口
RATE_SLACK = 4 * MIB             # 可测制品大小的余量（回环套接字缓冲）
PER_IP = 4
GLOBAL = 32
IMMUTABLE = "public, max-age=31536000, immutable"
BENCH_PATHS = ("/api/bench", "/api/bench/", "/api/bench/v1/runs", "/api/bench/v1/public/runs", "/api/bench/v1/public/submissions",
               "/api/bench/v1/session", "/api/bench/v1/events", "/api/bench/v1/leaderboard", "/api/bench/v1/board")
#: 公开组合根不得出现的 D3 进程（AWR-05 §8.4 / M27 §8.4 的 d3 profile 与动态容器）：按命令行匹配
D3_PROC_PATTERNS = (r"\bawr\.api\.bench_main\b", r"\bawr\.bench\.(sched|rt_main|rerun_proxy|rerun\.relay|catalog|subworker)\b",
                    r"\bawr\.sensorsvc\b", r"\bgz sim\b", r"\bruby .*gz\b", r"\bfastlivo\b|\bfast_livo\b", r"\brerun\b.*--serve")


class _TLSConn(http.client.HTTPSConnection):
    """连接地址与 SNI 分开：连回环地址，证书与 SNI 仍按站点主机名。"""

    def __init__(self, addr: str, port: int, *, sni: str, ctx: ssl.SSLContext, timeout: float, source: tuple[str, int] | None) -> None:
        super().__init__(addr, port, timeout=timeout, context=ctx, source_address=source)
        self._sni = sni
        self._tls = ctx

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        self.sock = self._tls.wrap_socket(sock, server_hostname=self._sni)


@dataclass
class Site:
    """站点地址：base 的主机名用于 Host 与 SNI，connect 为实际连接地址（缺省同主机名）。"""

    base: str
    connect: str | None = None
    insecure: bool = False
    timeout: float = 15.0
    host: str = ""
    port: int = 443
    tls: bool = True
    _ctx: ssl.SSLContext | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        m = re.match(r"^(https?)://([^/:]+)(?::(\d+))?/?$", self.base)
        if not m:
            raise ValueError(f"--base {self.base!r} 应形如 https://{DOMAIN}")
        self.tls = m.group(1) == "https"
        self.host = m.group(2)
        self.port = int(m.group(3) or (443 if self.tls else 80))
        if self.tls:
            self._ctx = ssl.create_default_context()
            if self.insecure:
                self._ctx.check_hostname = False
                self._ctx.verify_mode = ssl.CERT_NONE

    def conn(self, source: str | None = None) -> http.client.HTTPConnection:
        addr = self.connect or self.host
        src = (source, 0) if source else None
        if self.tls:
            assert self._ctx is not None
            return _TLSConn(addr, self.port, sni=self.host, ctx=self._ctx, timeout=self.timeout, source=src)
        return http.client.HTTPConnection(addr, self.port, timeout=self.timeout, source_address=src)

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None, body: bytes | None = None,
                source: str | None = None, read: bool = True) -> tuple[int, dict[str, str], bytes]:
        c = self.conn(source)
        try:
            c.request(method, path, body=body, headers={"Host": self.host, "User-Agent": "d3-public-verify/1", **(headers or {})})
            r = c.getresponse()
            data = r.read() if read and method != "HEAD" else b""
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            c.close()


def _check(name: str, problems: list[str], detail: dict | None = None) -> dict:
    return {"name": name, "ok": not problems, "problems": problems, "detail": detail or {}}


# ---------------------------------------------------------------------------------------------------------------------
# 站点检查


def check_bench404(site: Site) -> dict:
    probs, seen = [], {}
    for p in BENCH_PATHS:
        for m in ("GET", "HEAD", "POST"):
            st, _, _ = site.request(m, p, body=b"{}" if m == "POST" else None,
                                    headers={"Content-Type": "application/json"} if m == "POST" else None)
            seen[f"{m} {p}"] = st
            if st != 404:
                probs.append(f"{m} {p} → {st}（应为 404）")
    return _check("bench404", probs, {"status": seen})


LAB_MARKER = re.compile(rb'src="/lab/assets/[^"]+\.js"')


def check_lab(site: Site) -> dict:
    probs: list[str] = []
    st, h, body = site.request("GET", "/lab/")
    if st != 200 or "text/html" not in h.get("content-type", ""):
        return _check("lab", [f"GET /lab/ → {st} {h.get('content-type')}（应为 200 text/html）"])
    if h.get("cache-control") != "no-cache":
        probs.append(f"/lab/ Cache-Control {h.get('cache-control')!r}（应为 no-cache）")
    if not LAB_MARKER.search(body):
        probs.append("/lab/ 不是 /lab/ 构建（index.html 中没有 /lab/assets/*.js）")
    for p in ("/lab/samples", "/lab/samples/run-abcdefghij"):
        s2, _, b2 = site.request("GET", p)
        if s2 != 200 or b2 != body:
            probs.append(f"GET {p} → {s2}，{'同一份' if b2 == body else '不同的'} index.html（History 路由应回退到 /lab/index.html）")
    s3, h3, _ = site.request("GET", "/lab")
    if s3 != 301 or not h3.get("location", "").endswith("/lab/"):
        probs.append(f"GET /lab → {s3} {h3.get('location')}（应为 301 /lab/）")
    s4, _, _ = site.request("POST", "/lab/", body=b"x")
    if s4 != 405:
        probs.append(f"POST /lab/ → {s4}（应为 405）")
    for p in ("/lab/.env", "/lab/assets/.x", "/lab/assets/not-there-0000.js"):
        s5, _, _ = site.request("GET", p)
        if s5 != 404:
            probs.append(f"GET {p} → {s5}（应为 404）")
    m = LAB_MARKER.search(body)
    if m:
        asset = m.group(0)[5:-1].decode()
        s6, h6, _ = site.request("HEAD", asset)
        if s6 != 200 or h6.get("cache-control") != IMMUTABLE:
            probs.append(f"HEAD {asset} → {s6} {h6.get('cache-control')!r}（应为 200 immutable）")
    return _check("lab", probs, {"index_bytes": len(body)})


def _index_files(idx: dict) -> list[tuple[str, int | None, str | None]]:
    out = []
    for e in idx.get("events") or []:
        out.append((e["tape"], e.get("bytes"), e.get("sha256")))
        for r in e.get("runs") or []:
            out += [(v["path"], v.get("bytes"), v.get("sha256")) for v in r.get("videos") or [] if not v.get("external")]
            out += [(s["rrd"], s.get("bytes"), s.get("sha256")) for s in r.get("slices") or [] if not s.get("external")]
            if r.get("basemap") and not r.get("basemap_external"):
                out.append((r["basemap"], r.get("basemap_bytes"), r.get("basemap_sha256")))
    return out


def fetch_index(site: Site) -> tuple[dict | None, dict[str, str], str | None]:
    st, h, body = site.request("GET", "/lab-static/index.json")
    if st != 200:
        return None, h, f"GET /lab-static/index.json → {st}"
    try:
        doc = json.loads(body)
    except ValueError:
        return None, h, "index.json 不是 JSON"
    if not isinstance(doc, dict) or doc.get("schema") != labctl.SCHEMA_INDEX:
        return None, h, f"index.json schema {doc.get('schema') if isinstance(doc, dict) else type(doc)!r}"
    return doc, h, None


def check_static(site: Site, *, max_head: int = 64) -> dict:
    probs: list[str] = []
    idx, h, err = fetch_index(site)
    if idx is None:
        return _check("static", [err or "index.json 不可用"])
    ver = idx["version"]
    if h.get("cache-control") != "no-cache":
        probs.append(f"index.json Cache-Control {h.get('cache-control')!r}（应为 no-cache）")
    etag = h.get("etag")
    if not etag:
        probs.append("index.json 没有 ETag")
    else:
        s, _, _ = site.request("GET", "/lab-static/index.json", headers={"If-None-Match": etag})
        if s != 304:
            probs.append(f"If-None-Match → {s}（应为 304）")
    if (idx.get("features") or {}).get("leaderboard") or (idx.get("features") or {}).get("submit"):
        probs.append("index.json 声明了 leaderboard 或 submit（R-D3-46）")
    base = f"/lab-static/v/{ver}/"
    s, hs, sums_body = site.request("GET", base + labctl.SUMS)
    sums: dict[str, str] = {}
    if s != 200:
        probs.append(f"GET {base}{labctl.SUMS} → {s}")
    else:
        if hs.get("cache-control") != IMMUTABLE:
            probs.append(f"{labctl.SUMS} Cache-Control {hs.get('cache-control')!r}（应为 immutable）")
        for line in sums_body.decode("utf-8", "replace").splitlines():
            mm = re.match(r"^([0-9a-f]{64}) [ *](.+)$", line)
            if mm:
                sums[mm.group(2)] = mm.group(1)
    heads = 0
    for path, size, sha in _index_files(idx)[:max_head]:
        s, hh, _ = site.request("HEAD", base + path)
        heads += 1
        if s != 200:
            probs.append(f"HEAD {base}{path} → {s}")
            continue
        if size is not None and int(hh.get("content-length", -1)) != size:
            probs.append(f"{path}：Content-Length {hh.get('content-length')} 与索引 {size} 不一致")
        if sha and sums.get(path) not in (None, sha):
            probs.append(f"{path}：索引 sha256 与 SHA256SUMS 不一致")
    small = sorted(((p, d) for p, d in sums.items()), key=lambda x: x[0])[:1]
    for p, d in small:
        s, _, data = site.request("GET", base + p)
        if s != 200 or hashlib.sha256(data).hexdigest() != d:
            probs.append(f"GET {base}{p} → {s}，摘要{'一致' if s == 200 and hashlib.sha256(data).hexdigest() == d else '不一致'}")
    for p in ("/lab-static/", "/lab-static/v/", f"/lab-static/v/{ver}/", f"/lab-static/v/{ver}/{labctl.INDEX_IN_VERSION}",
              f"/lab-static/.staging/{ver}/{labctl.SUMS}", f"/lab-static/v/current/{labctl.SUMS}", f"/lab-static/v/{ver}/../index.json",
              f"/lab-static/v/latest/{labctl.SUMS}"):
        s, _, _ = site.request("GET", p)
        if s != 404:
            probs.append(f"GET {p} → {s}（应为 404）")
    s, _, _ = site.request("POST", base + labctl.SUMS, body=b"x")
    if s != 405:
        probs.append(f"POST {base}{labctl.SUMS} → {s}（应为 405）")
    s, _, _ = site.request("POST", "/lab-static/index.json", body=b"x")
    if s != 405:
        probs.append(f"POST /lab-static/index.json → {s}（应为 405）")
    return _check("static", probs, {"version": ver, "heads": heads, "sums": len(sums),
                                    "files_bytes_total": idx.get("files_bytes_total")})


def _largest(site: Site, idx: dict, min_bytes: int) -> tuple[str, int] | None:
    files = sorted(((p, b) for p, b, _ in _index_files(idx) if b), key=lambda x: -x[1])
    if not files or files[0][1] < min_bytes:
        return None
    return f"/lab-static/v/{idx['version']}/{files[0][0]}", files[0][1]


def timed_download(site: Site, path: str, *, headers: dict[str, str] | None = None, source: str | None = None,
                   limit: int | None = None, hold: tuple[int, float] | None = None, chunk: int = 64 * 1024) -> dict:
    """单连接下载并记录 (时刻, 累计字节)；limit 给出时读到该字节数即停；hold = (b0, s) 给出时累计字节越过 b0 后再读 s 秒即停。

    稳态窗口按时间而不按字节截取：越过阈值时 nginx 预支的 1 s 额度与回环套接字缓冲（数 MiB）中的数据会立即到达，按字节截取时
    这部分会吃掉稳态窗口。"""
    c = site.conn(source)
    t0 = time.monotonic()
    marks: list[tuple[float, int]] = [(0.0, 0)]
    crossed: float | None = None
    try:
        c.request("GET", path, headers={"Host": site.host, "User-Agent": "d3-public-verify/1", **(headers or {})})
        r = c.getresponse()
        n = 0
        while True:
            b = r.read(chunk)
            if not b:
                break
            n += len(b)
            t = time.monotonic() - t0
            marks.append((t, n))
            if limit is not None and n >= limit:
                break
            if hold is not None:
                if crossed is None and n >= hold[0]:
                    crossed = t
                if crossed is not None and t - crossed >= hold[1]:
                    break
        return {"status": r.status, "bytes": n, "seconds": round(time.monotonic() - t0, 3), "marks": marks}
    finally:
        c.close()


def rate_between(marks: list[tuple[float, int]], b0: int, b1: int | None = None) -> float | None:
    """marks 中累计字节从 b0 到 b1（缺省末尾）之间的平均速率（B/s）。"""
    s = next((m for m in marks if m[1] >= b0), None)
    e = marks[-1] if b1 is None else next((m for m in marks if m[1] >= b1), marks[-1])
    if s is None or e[0] <= s[0] or e[1] <= s[1]:
        return None
    return (e[1] - s[1]) / (e[0] - s[0])


def steady_rate(marks: list[tuple[float, int]], b0: int, *, skip_s: float = RATE_SKIP_S, min_window_s: float = RATE_WINDOW_S) -> float | None:
    """累计字节越过 b0 之后、再过 skip_s 秒起到结束的稳态速率（最小二乘斜率，B/s）；窗口短于 min_window_s 时为 None。

    nginx 的 limit_rate 按秒计额度且允许预支 1 s（ngx_http_write_filter：rate ×（已过秒数 + 1））、套接字缓冲中还有已发送未读的
    数据，因此越过阈值后的头几秒平均速率会高于设定；稳态（每秒补足一次额度）的斜率才是设定的速率。"""
    s = next((m for m in marks if m[1] >= b0), None)
    if s is None:
        return None
    pts = [m for m in marks if m[0] >= s[0] + skip_s]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < min_window_s:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mb = sum(b for _, b in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    return sum((t - mt) * (b - mb) for t, b in pts) / den if den > 0 else None


def check_rate(site: Site, *, path: str | None = None) -> dict:
    """单访客速率（D3-AC-21）：前 8 MiB 不限，之后 ≤ 2 MiB/s（稳态，容差 5 %）；Range 请求不享受前 8 MiB。"""
    probs: list[str] = []
    hold_s = RATE_SKIP_S + RATE_WINDOW_S + 0.5
    # 可测制品的最小字节数：前 8 MiB + 预支额度 1 s + 稳态窗口所需时长 × 2 MiB/s + 套接字缓冲余量 4 MiB
    need = RATE_AFTER + int((1.0 + hold_s) * RATE_BPS) + RATE_SLACK
    if path is None:
        idx, _, err = fetch_index(site)
        if idx is None:
            return _check("rate", [err or "index.json 不可用"])
        big = _largest(site, idx, need)
        if big is None:
            # ADR-221：大文件只经 ModelScope 分发时，本站托管的制品都在前 8 MiB 的不限速额度内，限速不会生效，也无从实测；
            # 站点文件中的 limit_rate 由 tests/d3/deploy/test_d3_public_site.py 与 nginx 端到端用例核对
            return _check("rate", [], {"skipped": f"没有 ≥ {need / MIB:.0f} MiB 的本站托管制品（大文件经分发渠道，ADR-221）；--rate-path 可指定"})
        path = big[0]
    full = timed_download(site, path, hold=(RATE_AFTER, hold_s))
    if full["status"] != 200:
        return _check("rate", [f"GET {path} → {full['status']}"])
    burst = rate_between(full["marks"], 1 * MIB, RATE_AFTER - MIB)
    after = steady_rate(full["marks"], RATE_AFTER)
    if after is None:
        probs.append(f"8 MiB 之后的稳态窗口不足 {RATE_WINDOW_S:.0f} s（制品读完或连接中断，共 {full['bytes']} 字节）")
    elif after > RATE_BPS * (1 + RATE_TOL):
        probs.append(f"8 MiB 之后稳态 {after and round(after / MIB, 3)} MiB/s（应 ≤ {RATE_BPS / MIB:.0f} MiB/s × {1 + RATE_TOL}）")
    if burst is not None and burst < RATE_BPS * 1.5:
        probs.append(f"前 8 MiB 只有 {round(burst / MIB, 3)} MiB/s（前 8 MiB 不应限速）")
    rng = timed_download(site, path, headers={"Range": f"bytes={MIB}-"}, hold=(0, hold_s))
    r_rate = steady_rate(rng["marks"], 0)
    r_first = rate_between(rng["marks"], 0, 6 * MIB)
    if rng["status"] != 206:
        probs.append(f"Range 请求 → {rng['status']}（应为 206）")
    elif r_rate is None or r_rate > RATE_BPS * (1 + RATE_TOL):
        probs.append(f"Range 请求稳态 {r_rate and round(r_rate / MIB, 3)} MiB/s（分段请求从首字节起限速，应 ≤ 2 MiB/s）")
    elif r_first is not None and r_first > 2 * RATE_BPS:
        # 限速时前 6 MiB 约 3 MiB/s（预支 1 s 额度 2 MiB + 2 s × 2 MiB/s）；享受前 8 MiB 不限时为数百 MiB/s
        probs.append(f"Range 请求前 6 MiB {round(r_first / MIB, 3)} MiB/s（分段请求不应享受前 8 MiB 不限速）")
    return _check("rate", probs, {"path": path, "burst_mib_s": burst and round(burst / MIB, 3),
                                  "after_8mib_mib_s": after and round(after / MIB, 3), "range_mib_s": r_rate and round(r_rate / MIB, 3),
                                  "range_first_6mib_mib_s": r_first and round(r_first / MIB, 3), "limit_mib_s": RATE_BPS / MIB,
                                  "tolerance": RATE_TOL, "skip_s": RATE_SKIP_S, "window_s": RATE_WINDOW_S,
                                  "seconds": full["seconds"] + rng["seconds"]})


class Holder:
    """并发持有的下载连接：发请求、读响应头后不读正文（连接保持在 limit_conn 计数中）。"""

    def __init__(self, site: Site) -> None:
        self.site = site
        self.conns: list[http.client.HTTPConnection] = []

    def open(self, path: str, source: str | None) -> int:
        c = self.site.conn(source)
        c.request("GET", path, headers={"Host": self.site.host, "User-Agent": "d3-public-verify/1"})
        r = c.getresponse()
        if r.status != 200:
            r.read()
            c.close()
        else:
            self.conns.append(c)
        return r.status

    def close(self) -> None:
        for c in self.conns:
            with contextlib.suppress(Exception):
                c.close()
        self.conns.clear()


def check_conn(site: Site, *, source_ips: list[str] | None = None, path: str | None = None) -> list[dict]:
    """limit_conn：同一地址前缀第 5 条 429；source_ips ≥ 9 个时另测全站第 33 条 429。"""
    out = []
    if path is None:
        idx, _, err = fetch_index(site)
        big = _largest(site, idx, RATE_AFTER + 4 * MIB) if idx else None
        if big is None:
            return [_check("conn_ip", [err or "没有 ≥ 12 MiB 的制品可测"])]
        path = big[0]
    src0 = source_ips[0] if source_ips else None
    h = Holder(site)
    try:
        st = [h.open(path, src0) for _ in range(PER_IP + 1)]
    finally:
        h.close()
    probs = [] if st[:PER_IP] == [200] * PER_IP and st[PER_IP] == 429 else [f"同一地址 5 条并发：{st}（应为 4 × 200 后 429）"]
    out.append(_check("conn_ip", probs, {"status": st, "source": src0}))
    if source_ips and len(source_ips) >= GLOBAL // PER_IP + 1:
        time.sleep(0.5)
        h = Holder(site)
        st2: list[int] = []
        try:
            st2 += [h.open(path, source_ips[k // PER_IP]) for k in range(GLOBAL)]
            last = h.open(path, source_ips[GLOBAL // PER_IP])
        finally:
            h.close()
        probs = [] if st2 == [200] * GLOBAL and last == 429 else [f"全站 32 条后第 33 条：{last}（前 32 条 {sorted(set(st2))}，应为 200 后 429）"]
        out.append(_check("conn_all", probs, {"held": len(st2), "last": last, "sources": GLOBAL // PER_IP + 1}))
    return out


def check_nmax(site: Site, expect: int | None) -> dict:
    st, _, body = site.request("GET", "/api/sandbox/v1/capacity")
    if st != 200:
        return _check("nmax", [f"GET /api/sandbox/v1/capacity → {st}"])
    doc = json.loads(body)
    n = (doc.get("sandbox") or {}).get("max")
    probs = [] if expect is None or n == expect else [f"N_max {n}（应为 {expect}，D2-AC-04）"]
    return _check("nmax", probs, {"n_max": n, "expect": expect, "accepting": doc.get("accepting")})


def check_d2(site: Site) -> dict:
    """回滚到 D2 后：/lab/ 与 /lab-static/ 不再由 /lab/ 构建与静态索引提供；落地页 200；/api/bench/* 仍 404。"""
    probs: list[str] = []
    st, _, body = site.request("GET", "/lab/")
    if st == 200 and LAB_MARKER.search(body):
        probs.append("/lab/ 仍返回 /lab/ 构建")
    idx, _, _ = fetch_index(site)
    if idx is not None:
        probs.append("/lab-static/index.json 仍返回静态索引")
    st, _, _ = site.request("GET", "/")
    if st != 200:
        probs.append(f"GET / → {st}（落地页应为 200）")
    b = check_bench404(site)
    probs += b["problems"]
    return _check("d2", probs)


# ---------------------------------------------------------------------------------------------------------------------
# 本机检查


def read_env_file(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with contextlib.suppress(OSError):
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def scan_procs(proc: Path = Path("/proc")) -> dict:
    """进程表：命令行匹配 D3 进程的、环境中 AWR_D3_ENABLED=1 的（读不到环境的进程计数）。"""
    rx = re.compile("|".join(D3_PROC_PATTERNS))
    me = os.getpid()
    d3, enabled, unreadable, n = [], [], 0, 0
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        n += 1
        if cmd and rx.search(cmd) and "verify.py" not in cmd:
            d3.append({"pid": int(d.name), "cmd": cmd[:200]})
        try:
            env = (d / "environ").read_bytes().split(b"\0")
        except OSError:
            unreadable += 1
            continue
        if b"AWR_D3_ENABLED=1" in env:
            enabled.append({"pid": int(d.name), "cmd": cmd[:200]})
    return {"processes": n, "d3": d3, "d3_enabled_env": enabled, "environ_unreadable": unreadable}


def docker_d3() -> list[str] | None:
    if shutil.which("docker") is None:
        return None
    r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return [x for x in r.stdout.split() if x.startswith("d3-")]


def check_root(env_file: Path, proc: Path = Path("/proc")) -> dict:
    probs: list[str] = []
    env = read_env_file(env_file)
    v = env.get("AWR_D3_ENABLED")
    if v not in (None, "", "0"):
        probs.append(f"{env_file}: AWR_D3_ENABLED={v}（公开组合根应为 0）")
    ps = scan_procs(proc)
    if ps["d3"]:
        probs.append(f"进程表中有 D3 进程：{ps['d3'][:5]}")
    if ps["d3_enabled_env"]:
        probs.append(f"进程环境 AWR_D3_ENABLED=1：{ps['d3_enabled_env'][:5]}")
    if ps["environ_unreadable"] and os.geteuid() != 0:
        probs.append(f"{ps['environ_unreadable']} 个进程的环境不可读：请以 root 执行 --local")
    cont = docker_d3()
    if cont:
        probs.append(f"运行中的 d3-* 容器：{cont}")
    return _check("root", probs, {"env_file": str(env_file), "AWR_D3_ENABLED": v, **{k: ps[k] for k in ("processes", "environ_unreadable")},
                                  "d3_processes": len(ps["d3"]), "docker": cont})


def check_disk(root: Path) -> dict:
    rep = labctl.budget_report(labctl.Layout(root))
    probs = []
    if rep["bytes_total"] > rep["budget_bytes"]:
        probs.append(f"D3 总占用 {rep['bytes_total']} B > {rep['budget_bytes']} B（Q-D3-02）")
    if rep["oversize"]:
        probs.append(f"> 64 MB 的文件：{rep['oversize'][:5]}")
    return _check("disk", probs, {k: rep[k] for k in ("bytes_total", "budget_bytes")} | {"oversize": len(rep["oversize"])})


def check_content(root: Path, test_ids: frozenset[str] = frozenset()) -> dict:
    lay = labctl.Layout(root)
    cur = lay.state()["lab_static"]["current"]
    if not cur:
        return _check("content", ["state.json 中没有当前版本"])
    rep = labctl.check_version_dir(lay.versions / cur, test_ids=test_ids)
    b = lay.current_build()
    brep = labctl.check_lab_dist(lay.lab / b) if b else {"ok": False, "problems": ["没有 lab/current"]}
    return _check("content", rep["problems"] + brep["problems"],
                  {"version": cur, "build": b, "test_files": rep["test_files"], "truth_files": rep["truth_files"], "warnings": rep["warnings"]})


# ---------------------------------------------------------------------------------------------------------------------


def parse_ips(spec: str | None) -> list[str]:
    """127.0.0.2-127.0.0.12 或逗号列表。"""
    if not spec:
        return []
    out: list[str] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = (ipaddress.ip_address(x.strip()) for x in part.split("-", 1))
            out += [str(ipaddress.ip_address(i)) for i in range(int(a), int(b) + 1)]
        elif part.strip():
            out.append(part.strip())
    return out


def run(site: Site | None, *, expect: str = "lab", checks: set[str] | None = None, source_ips: list[str] | None = None,
        expect_nmax: int | None = None, local_root: Path | None = None, env_file: Path | None = None,
        test_ids: frozenset[str] = frozenset()) -> dict:
    t0 = time.monotonic()
    want = checks or {"bench404", "lab", "static", "rate", "conn", "nmax", "root", "disk", "content", "d2"}
    res: list[dict] = []
    if site is not None:
        if expect == "d2":
            res.append(check_d2(site))
        else:
            for name, fn in (("bench404", check_bench404), ("lab", check_lab), ("static", check_static), ("rate", check_rate)):
                if name in want:
                    res.append(fn(site))
            if "conn" in want:
                res += check_conn(site, source_ips=source_ips)
        if "nmax" in want and expect_nmax is not None:
            res.append(check_nmax(site, expect_nmax))
    if local_root is not None:
        if "root" in want:
            res.append(check_root(env_file or Path("/etc/anet-drone4d/env")))
        if expect != "d2":
            if "disk" in want:
                res.append(check_disk(local_root))
            if "content" in want:
                res.append(check_content(local_root, test_ids))
    return {"schema": SCHEMA, "t": labctl.utc_now(), "base": site.base if site else None, "expect": expect, "checks": res,
            "seconds": round(time.monotonic() - t0, 3), "ok": all(c["ok"] for c in res) and bool(res)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify.py", description="公开样例上线后的验收检查（D3-AC-21）")
    ap.add_argument("--base", default=f"https://{DOMAIN}")
    ap.add_argument("--connect", help="实际连接地址（emax 上 127.0.0.1）")
    ap.add_argument("--insecure", action="store_true", help="不校验证书（演练用的自签证书）")
    ap.add_argument("--expect", choices=("lab", "d2"), default="lab")
    ap.add_argument("--checks", help="逗号分隔：bench404,lab,static,rate,conn,nmax,root,disk,content")
    ap.add_argument("--source-ips", help="并发检查用的源地址（回环：127.0.0.2-127.0.0.12）")
    ap.add_argument("--expect-nmax", type=int)
    ap.add_argument("--local", action="store_true", help="另做本机检查（emax 上以 root 执行）")
    ap.add_argument("--root", type=Path, default=labctl.DEFAULT_ROOT)
    ap.add_argument("--env-file", type=Path, default=Path("/etc/anet-drone4d/env"))
    ap.add_argument("--no-site", action="store_true", help="只做本机检查")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    try:
        site = None if a.no_site else Site(a.base, connect=a.connect, insecure=a.insecure)
        rep = run(site, expect=a.expect, checks=set(a.checks.split(",")) if a.checks else None, source_ips=parse_ips(a.source_ips),
                  expect_nmax=a.expect_nmax, local_root=a.root if a.local or a.no_site else None, env_file=a.env_file)
    except (OSError, ValueError, http.client.HTTPException) as e:
        print(f"verify: {e}", file=sys.stderr)
        return 2
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for c in rep["checks"]:
        print(f"{'通过' if c['ok'] else '不通过'} {c['name']}" + ("" if c["ok"] else "：" + "；".join(c["problems"][:4])))
    print(f"verify: {'通过' if rep['ok'] else '不通过'}（{rep['seconds']} s）")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
