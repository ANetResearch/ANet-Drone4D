"""带 /lab/ 的 emax 站点文件（tools/deploy/emax/d3-public/labsite.py、lab-http.conf、lab-server.conf；D3-AC-21、扩展点 16）。

结构断言（不需要 nginx；真实 nginx 的行为见 test_d3_public_nginx_e2e.py）：
- render 只在两个插入点各插入一段，strip 逐字节还原 D2 站点文件（回滚到 D2 = 重新安装 D2 站点文件）；
- `/api/bench`、`/api/bench/**` 404 且不回源；`/lab/` 别名到 `lab/current/`、SPA 回退、assets immutable；
- `/lab-static/index.json` no-cache；`/lab-static/v/<ver>/` immutable、`limit_rate_after`（整段下载 8m、分段 0）、`limit_rate 2m`、
  每前缀 `limit_conn 4`、全站 `limit_conn 32`（AWR-05 §7.9 实现约束 4、17 §18.8.5 规则 4）；只允许 GET 与 HEAD；
- 直出正则与 labctl 的路径规则一致（以点开头的段、`.staging`、非法版本名不匹配）；
- 新增的区名不与 D2 站点文件冲突（drone4d_lab 前缀）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "ops"))

from d3public_fixtures import load  # noqa: E402
from nginx_conf import Directive, https_server, locations, nginx_regex_to_python, parse  # noqa: E402

labsite = load("labsite")
labctl = load("labctl")
D2 = labsite.D2_SITE.read_text(encoding="utf-8")
SITE = labsite.render(D2)


def _main() -> Directive:
    return parse(SITE)


def test_render_inserts_two_blocks_and_strip_restores_d2():
    assert SITE.count(labsite.BEGIN_HTTP.strip()) == 1 and SITE.count(labsite.BEGIN_SERVER.strip()) == 1
    assert labsite.strip(SITE) == D2
    assert labsite.has_lab(SITE) and not labsite.has_lab(D2)
    # 插入点之外逐字节相同：D2 文本是 SITE 的子序列，且两段增补之外没有任何差异
    i = SITE.index(labsite.BEGIN_HTTP)
    assert SITE[:i] == D2[:i]
    assert SITE.endswith(D2[D2.index("    # ---- 前端构建产物"):])
    with pytest.raises(labsite.SiteError, match="已经包含"):
        labsite.render(SITE)
    with pytest.raises(labsite.SiteError, match="出现 0 次"):
        labsite.render(D2.replace("# ---- 80：", "# ---- 八十："))
    assert labsite.main(["check", str(labsite.D2_SITE)]) == 1


def test_zones_and_maps_are_prefixed_and_unique():
    main = _main()
    zones = [d.args for d in main.find("limit_conn_zone")]
    names = [next(a for a in z if a.startswith("zone=")).split("=")[1].split(":")[0] for z in zones]
    assert len(names) == len(set(names))
    assert ["$drone4d_prefix", "zone=drone4d_lab_ip:10m"] in zones and ["$server_name", "zone=drone4d_lab_all:1m"] in zones
    m = next(d for d in main.find("map") if d.args == ["$http_range", "$drone4d_lab_rate_after"])
    table = {d.name: d.args[0] for d in m.block or []}
    assert table == {"": "8m", "bytes=0-": "8m", "default": "0"}
    m = next(d for d in main.find("map") if d.args == ["$request_method", "$drone4d_lab_bad_method"])
    assert {d.name: d.args[0] for d in m.block or []} == {"GET": "0", "HEAD": "0", "default": "1"}


def test_bench_404_lab_and_lab_static_locations():
    srv = https_server(_main())
    locs = locations(srv)
    for k in ("= /api/bench", "^~ /api/bench/"):
        assert locs[k].values("return") == [["404"]] and not locs[k].find("proxy_pass")
    lab = locs["^~ /lab/"]
    assert lab.values("alias") == [["/srv/drone4d/lab/current/"]] and lab.values("try_files") == [["$uri", "/lab/index.html"]]
    assert ["Cache-Control", "no-cache", "always"] in lab.values("add_header")
    assets = locs["^~ /lab/assets/"]
    assert assets.values("try_files") == [["$uri", "=404"]]
    assert ["Cache-Control", "public, max-age=31536000, immutable", "always"] in assets.values("add_header")
    assert locs["= /lab"].values("return") == [["301", "/lab/"]]
    idx = locs["= /lab-static/index.json"]
    assert idx.values("alias") == [["/srv/drone4d/lab-static/index.json"]] and ["Cache-Control", "no-cache", "always"] in idx.values("add_header")
    v = next(d for k, d in locs.items() if k.startswith("~ ^/lab-static/v/"))
    assert v.values("limit_rate") == [["2m"]] and v.values("limit_rate_after") == [["$drone4d_lab_rate_after"]]
    assert sorted(v.values("limit_conn")) == [["drone4d_lab_all", "32"], ["drone4d_lab_ip", "4"]]
    assert ["Cache-Control", "public, max-age=31536000, immutable", "always"] in v.values("add_header")
    assert v.values("max_ranges") == [["1"]]
    assert v.values("sendfile_max_chunk") == [["256k"]]       # 每次写事件 ≤ 256 KiB 后让出 worker（WS 推送时延）
    assert locs["/lab-static/"].values("return") == [["404"]]
    # 只允许 GET 与 HEAD：每个 /lab 与 /lab-static 的直出 location 都有 405 分支
    for k in ("^~ /lab/", "^~ /lab/assets/", "= /lab-static/index.json"):
        ifs = locs[k].find("if")
        assert ifs and ifs[0].args == ["($drone4d_lab_bad_method)"] and ifs[0].values("return") == [["405"]], k
    assert v.find("if")[0].args == ["($drone4d_lab_bad_method)"] and v.find("if")[0].values("return") == [["405"]]
    # 增补段在 location / 之前，location / 仍为最后一个（SPA 回退给 show-api）
    order = [" ".join(d.args) for d in srv.find("location")]
    assert order[-1] == "/" and order.index("^~ /lab/") < order.index("/")


@pytest.mark.parametrize(("uri", "match"), [
    ("/lab-static/v/2026.10.19-1/SHA256SUMS", True),
    ("/lab-static/v/2026.10.19-1/runs/run-abcdefghij/video/uav-01_cam0.mp4", True),
    ("/lab-static/v/2026.10.19-1/.index.json", False),
    ("/lab-static/v/2026.10.19-1/runs/.hidden/x", False),
    ("/lab-static/v/2026.10.19-1/", False),
    ("/lab-static/v/latest/SHA256SUMS", False),
    ("/lab-static/v/current/SHA256SUMS", False),
    ("/lab-static/.staging/2026.10.19-1/SHA256SUMS", False),
    ("/lab-static/v/2026.10.19-1/a/../b", False),
])
def test_static_regex_matches_labctl_path_rules(uri, match):
    v = next(d for k, d in locations(https_server(_main())).items() if k.startswith("~ ^/lab-static/v/"))
    rx = nginx_regex_to_python(v.args[1])
    assert bool(rx.match(uri)) is match
    if match:
        rel = uri.split("/", 4)[4]
        assert all(labctl.SAFE_SEG_RE.match(s) for s in rel.split("/"))


def test_localize_keeps_locations_and_swaps_listen_paths(tmp_path):
    loc = labsite.localize(SITE, root=tmp_path / "srv", show_port=31001, sandbox_port=31002, listen="127.0.0.1:31443",
                           opt_root=tmp_path / "opt", micro_dir=tmp_path / "micro")
    assert "/srv/drone4d" not in loc and str(tmp_path / "srv") in loc and "ssl_" not in loc
    assert "127.0.0.1:31001" in loc and "listen 127.0.0.1:31443;" in loc and "listen 80" not in loc
    a = {k: v.values("limit_rate") for k, v in locations(https_server_any(parse(loc))).items()}
    b = {k: v.values("limit_rate") for k, v in locations(https_server(_main())).items()}
    assert len(a) == len(b)
    cert = (tmp_path / "c.pem", tmp_path / "k.pem")
    tls = labsite.localize(SITE, root=tmp_path, show_port=1, sandbox_port=2, listen="127.0.0.1:31444", cert=cert, http2=True)
    assert "listen 127.0.0.1:31444 ssl http2;" in tls and f"ssl_certificate     {cert[0]};" in tls
    assert re.search(r"ssl_protocols TLSv1\.2 TLSv1\.3;", tls)


def https_server_any(main: Directive) -> Directive:
    return next(s for s in main.find("server") if s.find("location"))
