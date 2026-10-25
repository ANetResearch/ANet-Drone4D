#!/usr/bin/env python3
"""tools/deploy/emax/d3-public/labsite.py：生成带 D3 公开只读样例的 emax 站点文件（扩展点 16；AWR-05 §7.9、AWR-17 §18.8.5；D3-AC-21）。

公开站主构建保持 D2 标签构建（AWR-05 §7.9 实现约束 1、ADR-194 (5) ⑤）：公开站点文件 `anet4d.agentnetwork.org.cn.conf`（下文仍称
"D2 站点文件"）不改；旧域名的跳转站点 `drone4d.agentnetwork.org.cn.conf` 只有 301，不插入增补段。
本脚本把 `lab-http.conf` 插入它的 "# ---- 80" 段之前（http 层：并发区与限速 map）、把 `lab-server.conf` 插入 443 server 的
"# ---- 前端构建产物" 段之前（`/api/bench` 404、`/lab/`、`/lab-static/`），得到"D2 + /lab/"站点文件。回滚到 D2（撤下 `/lab/`）
就是重新安装 D2 标签构建自带的站点文件，二者除插入的两段外逐字节相同（`check` 子命令与 tests/d3/deploy/test_d3_public_site.py
校验）。

子命令：
  render [--base FILE] [--out FILE]      生成站点文件（缺省 base 为本仓库的 D2 站点文件，缺省输出到标准输出）
  check  FILE [--base FILE]              FILE 是否恰为 render(base) 的结果（emax 上安装前复核）
  strip  FILE [--out FILE]               从"D2 + /lab/"站点文件还原 D2 站点文件（只删插入段，供无 D2 发布目录时回滚）

`localize()` 把站点文件改写成可在普通主机上运行的形式（回环端口、临时目录、自签证书或明文），供本目录的 drill.py 与
tests/d3/deploy 的 nginx 端到端用例共用：只替换监听、证书、上游端口与目录前缀，location、限速、限流与头一律原样。
只用标准库（emax 系统 Python 3.12、dmax 容器 Python 3.10 均可运行）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EMAX = HERE.parent
D2_SITE = EMAX / "anet4d.agentnetwork.org.cn.conf"
HTTP_PART = HERE / "lab-http.conf"
SERVER_PART = HERE / "lab-server.conf"
DOMAIN = "anet4d.agentnetwork.org.cn"
LEGACY_DOMAIN = "drone4d.agentnetwork.org.cn"   # 旧域名：只做 301（drone4d.agentnetwork.org.cn.conf）
SRV_ROOT = "/srv/drone4d"   # 遗留的服务器目录名（不随产品更名）

#: 插入点（D2 站点文件中各恰好出现一次）
HTTP_ANCHOR = re.compile(r"^# ---- 80：", re.M)
SERVER_ANCHOR = re.compile(r"^    # ---- 前端构建产物", re.M)
BEGIN_HTTP = "# >>> d3-public lab-http.conf（labsite.py render 插入；撤下 /lab/ 时整段删除）\n"
END_HTTP = "# <<< d3-public lab-http.conf\n\n"
BEGIN_SERVER = "    # >>> d3-public lab-server.conf（labsite.py render 插入；撤下 /lab/ 时整段删除）\n"
END_SERVER = "    # <<< d3-public lab-server.conf\n\n"


class SiteError(ValueError):
    pass


def _one(rx: re.Pattern[str], text: str, what: str) -> int:
    hits = [m.start() for m in rx.finditer(text)]
    if len(hits) != 1:
        raise SiteError(f"D2 站点文件中 {what} 出现 {len(hits)} 次（期望 1 次）：D2 站点文件结构已变，需同步更新 labsite.py 的插入点")
    return hits[0]


def render(base: str, http_part: str | None = None, server_part: str | None = None) -> str:
    """D2 站点文件 + 两段增补 → "D2 + /lab/" 站点文件。base 已含增补时报错（不重复插入）。"""
    if BEGIN_HTTP.strip() in base or BEGIN_SERVER.strip() in base:
        raise SiteError("base 已经包含 d3-public 增补段：请以 D2 标签构建自带的站点文件为 base")
    if "proxy_pass" not in base:
        raise SiteError(f"base 不是公开站点文件（没有反向代理段）：旧域名 {LEGACY_DOMAIN} 的跳转站点不能作为 base，请用 {D2_SITE.name}")
    hp = http_part if http_part is not None else HTTP_PART.read_text(encoding="utf-8")
    sp = server_part if server_part is not None else SERVER_PART.read_text(encoding="utf-8")
    i = _one(HTTP_ANCHOR, base, "'# ---- 80' 段")
    j = _one(SERVER_ANCHOR, base, "'# ---- 前端构建产物' 段")
    if not i < j:
        raise SiteError("插入点顺序不对：'# ---- 80' 段应在 443 server 之前")
    hp = hp if hp.endswith("\n") else hp + "\n"
    sp = sp.rstrip("\n") + "\n"
    return base[:i] + BEGIN_HTTP + hp + END_HTTP + base[i:j] + BEGIN_SERVER + sp + END_SERVER + base[j:]


def strip(site: str) -> str:
    """"D2 + /lab/" 站点文件 → D2 站点文件（删除两段增补）。"""
    out = site
    for b, e in ((BEGIN_HTTP, END_HTTP), (BEGIN_SERVER, END_SERVER)):
        i, k = out.find(b), out.find(e)
        if i < 0 or k < i:
            raise SiteError(f"站点文件中没有完整的增补段 {b.strip()}")
        out = out[:i] + out[k + len(e):]
    return out


def has_lab(site: str) -> bool:
    return BEGIN_SERVER.strip() in site


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def localize(site: str, *, root: Path, show_port: int, sandbox_port: int, listen: str, opt_root: Path | None = None,
             micro_dir: Path | None = None, cert: tuple[Path, Path] | None = None, http2: bool = False) -> str:
    """把站点文件改写成可在普通主机上运行的形式（drill.py 与 nginx 端到端用例共用）。

    - 443 的两条 listen 换成 `listen <listen> [ssl] [http2]`，80 端口段换成回环随机端口（只保留 ACME 与 301 行为）；
    - `cert` 给出时换成该证书与私钥（自签），否则去掉全部 ssl_* 指令改为明文；
    - 两个 upstream 指向 show_port、sandbox_port；`/srv/drone4d` → root，`/opt/anet-drone4d` → opt_root，微缓存目录 → micro_dir。
    其余（location、限速、限流、map、头）原样。"""
    s = site.replace("127.0.0.1:18640", f"127.0.0.1:{show_port}").replace("127.0.0.1:18641", f"127.0.0.1:{sandbox_port}")
    s = s.replace(SRV_ROOT + "/", str(root).rstrip("/") + "/")
    if opt_root is not None:
        s = s.replace("/opt/anet-drone4d", str(opt_root))
    if micro_dir is not None:
        s = s.replace("/var/lib/nginx/anet4d-micro", str(micro_dir))
    s, n80 = re.subn(r"^\s*listen 80;\n\s*listen \[::\]:80;\n", "    listen 127.0.0.1:1;\n", s, flags=re.M)
    tls = " ssl" if cert else ""
    h2 = " http2" if http2 else ""
    s, n443 = re.subn(r"^\s*listen 443 ssl http2;\n\s*listen \[::\]:443 ssl http2;\n", f"    listen {listen}{tls}{h2};\n", s, flags=re.M)
    if n80 != 1 or n443 != 1:
        raise SiteError(f"listen 行与预期不符（80：{n80}，443：{n443}）")
    # 80 段只用于 ACME 与 301；本地运行时绑定到一个不会用到的回环端口会失败，直接删掉整个 80 server
    s = re.sub(r"^server \{\n    listen 127\.0\.0\.1:1;\n.*?^\}\n", "", s, count=1, flags=re.M | re.S)
    if cert:
        s = re.sub(r"ssl_certificate\s+\S+;", f"ssl_certificate     {cert[0]};", s, count=1)
        s = re.sub(r"ssl_certificate_key\s+\S+;", f"ssl_certificate_key {cert[1]};", s, count=1)
    else:
        s = "\n".join(line for line in s.splitlines() if not line.lstrip().startswith("ssl_")) + "\n"
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="labsite.py", description="生成带 D3 公开只读样例的 emax 站点文件（D3-AC-21）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="D2 站点文件 + 增补 → 站点文件")
    r.add_argument("--base", type=Path, default=D2_SITE)
    r.add_argument("--out", type=Path)
    c = sub.add_parser("check", help="FILE 是否恰为 render(base)")
    c.add_argument("file", type=Path)
    c.add_argument("--base", type=Path, default=D2_SITE)
    s = sub.add_parser("strip", help="还原 D2 站点文件")
    s.add_argument("file", type=Path)
    s.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "render":
            out = render(a.base.read_text(encoding="utf-8"))
            if a.out:
                a.out.write_text(out, encoding="utf-8")
                print(f"labsite: 已写出 {a.out}（sha256 {sha256_text(out)[:16]}，base {a.base}）")
            else:
                sys.stdout.write(out)
            return 0
        if a.cmd == "check":
            want = render(a.base.read_text(encoding="utf-8"))
            got = a.file.read_text(encoding="utf-8")
            if got != want:
                print(f"labsite: {a.file} 与 render({a.base}) 不一致", file=sys.stderr)
                return 1
            print(f"labsite: {a.file} 与 render({a.base}) 一致")
            return 0
        out = strip(a.file.read_text(encoding="utf-8"))
        if a.out:
            a.out.write_text(out, encoding="utf-8")
        else:
            sys.stdout.write(out)
        return 0
    except (OSError, SiteError) as e:
        print(f"labsite: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
