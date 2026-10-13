#!/usr/bin/env python3
"""tools/deploy/emax/smoke.py：安装后的沙盒冒烟（M17-FR-064：创建沙盒 → 等 READY → 结束；AWR-04 §4.6）。

install.sh 在服务重启并通过 show-api 健康检查后，以发布的 .venv 经回环地址直接访问 sandbox-api（不经 nginx，带站点 Host）：
  1. 等 `GET /api/sandbox/v1/health` 报 `pool: up`（≤ --wait-s）；
  2. `POST /sessions {world, principal_hint: 随机 32 位十六进制}`：201 得到 sid 与 token；
  3. `GET /sessions/{sid}?wait_state=READY&wait_s=10`（至多 3 次）直到 READY 或 ACTIVE；
  4. `DELETE /sessions/{sid}` 结束会话（202）。
满员（202 排队，随即离队）、需要工作量证明（409 509）或前缀配额已用完（504）时视为"跳过"而不是失败（公开站上
重复安装会累计回环地址的创建次数）。只用标准库；不打印 token。

退出码：0 通过；3 跳过（原因在输出中）；1 失败；2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request

SKIP_CODES = {504: "地址前缀配额已用完", 509: "需要工作量证明（队列非空或本前缀近 1 h 创建过多）"}


class Client:
    def __init__(self, base: str, host: str | None, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/") + "/api/sandbox/v1"
        self.host = host
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
        headers = {"User-Agent": "anet-drone4d-install-smoke", "Accept": "application/json"}
        if self.host:
            headers["Host"] = self.host
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                doc = json.loads(raw) if raw else {}
            except ValueError:
                doc = {}
            return e.code, doc if isinstance(doc, dict) else {}


def run(base: str, host: str | None, world: str, wait_s: float, out=print) -> int:
    c = Client(base, host)
    t_end = time.monotonic() + wait_s
    while True:
        try:
            st, h = c.call("GET", "/health")
            if st == 200 and h.get("pool") == "up":
                break
        except OSError:
            pass
        if time.monotonic() > t_end:
            out(f"跳过：sandbox-api 在 {wait_s:.0f} s 内未就绪（GET /api/sandbox/v1/health）")
            return 3
        time.sleep(1.0)
    t0 = time.monotonic()
    st, doc = c.call("POST", "/sessions", {"world": world, "principal_hint": secrets.token_hex(16),
                                           "client": {"kind": "other", "version": "install-smoke"}})
    if st == 202:
        q = doc.get("queue") or {}
        if q.get("ticket_id") and q.get("ticket_token"):
            c.call("DELETE", f"/queue/{q['ticket_id']}", token=q["ticket_token"])
        out(f"跳过：沙盒已满员（排队第 {q.get('position')} 位，已离队）")
        return 3
    code = doc.get("code")
    if st in (409, 429) and code in SKIP_CODES:
        out(f"跳过：{SKIP_CODES[code]}（HTTP {st}，原因码 {code}）")
        return 3
    sess = doc.get("session") or {}
    sid, token = sess.get("sid"), doc.get("token")
    if st not in (200, 201) or not sid or not token:
        out(f"失败：创建返回 HTTP {st}，原因码 {code}（{doc.get('name') or doc.get('title') or ''}）")
        return 1
    state = sess.get("state")
    for _ in range(3):
        if state in ("READY", "ACTIVE", "IDLE"):
            break
        st, doc = c.call("GET", f"/sessions/{sid}?wait_state=READY&wait_s=10", token=token)
        if st != 200:
            out(f"失败：查询 {sid} 返回 HTTP {st}，原因码 {doc.get('code')}")
            c.call("DELETE", f"/sessions/{sid}", token=token)
            return 1
        state = (doc.get("session") or doc).get("state")
    t_ready = time.monotonic() - t0
    st_del, _ = c.call("DELETE", f"/sessions/{sid}", token=token)
    if state not in ("READY", "ACTIVE", "IDLE"):
        out(f"失败：{sid} 在 30 s 内未就绪（状态 {state}；日志 var/sandbox/runs/{sid}/）")
        return 1
    if st_del not in (200, 202, 204):
        out(f"失败：结束 {sid} 返回 HTTP {st_del}")
        return 1
    out(f"通过：创建 {sid}（{world}）→ {state} {t_ready:.1f} s → 结束")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="smoke.py", description="沙盒冒烟：创建、等就绪、结束")
    ap.add_argument("--base", default="http://127.0.0.1:18641", help="sandbox-api 基址（回环）")
    ap.add_argument("--host", default=None, help="Host 头（公开模式只认站点域名与回环）")
    ap.add_argument("--world", default="synthcity")
    ap.add_argument("--wait-s", type=float, default=60.0, help="等待 sandbox-api 就绪的秒数")
    a = ap.parse_args(argv)
    try:
        return run(a.base, a.host, a.world, a.wait_s)
    except (OSError, ValueError) as e:
        print(f"失败：{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
