"""nginx 站点文件的结构与路由（M00，D2 WP-16；M17-FR-062、M17-AC-020；AWR-04 §4.7 第 3 条、§11.5、§15.5.4；ADR-092、
ADR-109、ADR-119、ADR-120、ADR-123）。

本机没有 nginx（不装新依赖），因此这里以解析器（`nginx_conf.py`）检查站点文件：路径分流到两个 api、`/worlds/` 白名单 alias
与缓存头、隐藏路径不直出、前缀限流键、容量接口微缓存、构建信息与清单的缓存头、`/sdk/`、安全头与 HSTS 在带 add_header 的
location 中都在；并按 nginx 的 location 选择规则（精确 > 最长前缀 ^~ > 第一个匹配的正则 > 最长前缀）对一组请求路径断言落点。
地址前缀 map 与 `awr.api.public.prefix_of` 对拍 200 个地址（d2-WP-03-to-M00 第 5 条）。真实 nginx 的静态对拍见
`test_nginx_static_parity.py`（有 nginx 时执行）。
"""

from __future__ import annotations

import ipaddress
import random
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nginx_conf import BOOTSTRAP, ROOT, SITE, Directive, eval_map, https_server, load, parse

from awr.api.middleware import SECURITY_HEADERS
from awr.api.public import prefix_of
from awr.api.static import IMMUTABLE

WORLDS_ROOT = "/opt/anet-drone4d/var/worlds"
HSTS = ["Strict-Transport-Security", "max-age=31536000", "always"]


@pytest.fixture(scope="module")
def conf() -> Directive:
    return load()


@pytest.fixture(scope="module")
def server(conf) -> Directive:
    return https_server(conf)


@pytest.fixture(scope="module")
def public_profile() -> dict:
    cfg = yaml.safe_load((ROOT / "configs" / "runtime.yaml").read_text(encoding="utf-8"))
    return cfg["profiles"]["public"]


def resolve(server: Directive, uri: str) -> Directive:
    """nginx 的 location 选择：精确匹配；最长前缀（带 ^~ 时直接选中）；按出现顺序的第一个匹配正则；否则最长前缀。"""
    locs = server.find("location")
    for d in locs:
        if d.args[0] == "=" and d.args[1] == uri:
            return d
    prefixes = [d for d in locs if d.args[0] not in ("=", "~", "~*")]
    best = None
    for d in prefixes:
        path = d.args[-1]
        if uri.startswith(path) and (best is None or len(path) > len(best.args[-1])):
            best = d
    if best is not None and best.args[0] == "^~":
        return best
    for d in locs:
        if d.args[0] in ("~", "~*") and re.search(d.args[1], uri, re.IGNORECASE if d.args[0] == "~*" else 0):
            return d
    assert best is not None, uri
    return best


def upstream(loc: Directive) -> str | None:
    """proxy_pass 的 upstream 名（`http://drone4d_sandbox$uri` → `drone4d_sandbox`）。"""
    pp = loc.find("proxy_pass")
    return re.match(r"http://([A-Za-z0-9_]+)", pp[0].args[0]).group(1) if pp else None


def secs(v: str) -> float:
    m = re.fullmatch(r"(\d+)(ms|s|m)?", v)
    assert m, v
    return int(m.group(1)) * {"ms": 0.001, "s": 1.0, "m": 60.0, None: 1.0}[m.group(2)]


def headers(loc: Directive) -> dict[str, str]:
    return {a[0].lower(): a[1] for a in loc.values("add_header")}


# ---------------------------------------------------------------- 语法与命名


def test_site_and_bootstrap_parse(conf):
    parse(BOOTSTRAP.read_text(encoding="utf-8"))
    assert len(conf.find("server")) == 2


def test_regex_locations_with_braces_are_quoted():
    """含 `{` 的正则必须整体加引号，否则 nginx -t 报错（M17 PRD §8.2 的片段漏了引号）。"""
    for i, line in enumerate(SITE.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"\s*location\s+~\*?\s+(\S+)\s*\{\s*$", line)
        if m:
            assert "{" not in m.group(1) or m.group(1).startswith('"'), f"第 {i} 行：{line.strip()}"


def test_zone_and_variable_names_are_prefixed(conf):
    """只新增本站点：限流区、连接区、缓存区、upstream 与 map 变量都带 drone4d 前缀（不与其他站点冲突）。"""
    for d in conf.find("limit_req_zone") + conf.find("limit_conn_zone"):
        assert d.args[0] == "$drone4d_prefix", d.args          # 按地址前缀计（AWR-04 §11.5）
        zone = next(a for a in d.args if a.startswith("zone="))
        assert zone.startswith("zone=drone4d_"), zone
    for d in conf.find("upstream"):
        assert d.args[0].startswith("drone4d_")
    for d in conf.find("map"):
        assert d.args[1].startswith("$drone4d_")
    (pc,) = conf.find("proxy_cache_path")
    assert any(a.startswith("keys_zone=drone4d_micro:") for a in pc.args)


def test_every_referenced_zone_is_defined(conf, server):
    req = {next(a for a in d.args if a.startswith("zone=")).split("=")[1].split(":")[0] for d in conf.find("limit_req_zone")}
    con = {next(a for a in d.args if a.startswith("zone=")).split("=")[1].split(":")[0] for d in conf.find("limit_conn_zone")}

    def walk(d: Directive):
        for c in d.block or []:
            yield c
            yield from walk(c)

    for d in walk(server):
        if d.name == "limit_req":
            assert d.args[0].removeprefix("zone=") in req, d.args
        if d.name == "limit_conn":
            assert d.args[0] in con, d.args


def test_upstream_ports_match_public_profile(conf, public_profile):
    ups = {d.args[0]: d for d in conf.find("upstream")}
    assert ups["drone4d_backend"].values("server")[0][0] == f"127.0.0.1:{public_profile['net']['port']}"
    assert ups["drone4d_sandbox"].values("server")[0][0] == f"127.0.0.1:{public_profile['sandbox']['port']}"
    assert all(u.find("keepalive") for u in ups.values())


# ---------------------------------------------------------------- 路由


@pytest.mark.parametrize(("uri", "want"), [
    ("/api/rt", "drone4d_backend"),
    ("/api/auth/token", "drone4d_backend"),
    ("/api/auth/identity", "drone4d_backend"),                         # R79 只在 show-api（唯一比对点）
    ("/api/viewer/queue", "drone4d_backend"),                          # R81
    ("/api/viewer/queue/vq-abcdefghij", "drone4d_backend"),            # R82、R83
    ("/api/health", "drone4d_backend"),
    ("/api/world/synthcity/query", "drone4d_backend"),
    ("/api/sandbox/v1/sessions", "drone4d_sandbox"),
    ("/api/sandbox/v1/sessions/sb-abcdefghij", "drone4d_sandbox"),
    ("/api/sandbox/v1/sessions/sb-abcdefghij/rt", "drone4d_sandbox"),
    ("/api/sandbox/v1/sessions/sb-abcdefghij/park", "drone4d_sandbox"),
    ("/api/sandbox/v1/sessions/sb-abcdefghij/resume", "drone4d_sandbox"),
    ("/api/sandbox/v1/queue/q-abcdefghij", "drone4d_sandbox"),
    ("/api/sandbox/v1/capacity", "drone4d_sandbox"),
    ("/api/sandbox/v1/openapi.json", "drone4d_sandbox"),
    ("/worlds/unknown/world.json", "drone4d_backend"),                  # 白名单外：show-api 返回 404 305
    ("/", "drone4d_backend"),
    ("/world/shenzhen", "drone4d_backend"),
    ("/worlds", "drone4d_backend"),                                    # World Hub 前端路由
    ("/sandbox/api", "drone4d_backend"),
    ("/assets/index-abc.js", "drone4d_backend"),
])
def test_proxy_routes(server, uri, want):
    assert upstream(resolve(server, uri)) == want, uri


def test_websocket_locations(server):
    for uri, zone in (("/api/rt", "drone4d_ws_v2"), ("/api/sandbox/v1/sessions/sb-abcdefghij/rt", "drone4d_sbws")):
        loc = resolve(server, uri)
        assert loc.values("proxy_buffering") == [["off"]]
        assert loc.values("proxy_read_timeout") == [["3600s"]] and loc.values("proxy_send_timeout") == [["3600s"]]
        assert loc.values("limit_conn") == [[zone, "4"]]
    # 非法 sid 不进入会话 WS location（落到一般沙盒 REST，由 sandbox-api 返回 422/404）
    assert resolve(server, "/api/sandbox/v1/sessions/sb-ABC/rt").args == ["/api/sandbox/"]


def test_sandbox_create_is_rate_limited_by_prefix(server):
    loc = resolve(server, "/api/sandbox/v1/sessions")
    assert loc.args == ["=", "/api/sandbox/v1/sessions"]
    assert loc.values("limit_req")[0][0] == "zone=drone4d_sb_create"


def test_identity_is_rate_limited(server):
    loc = resolve(server, "/api/auth/identity")
    assert loc.values("limit_req")[0][0] == "zone=drone4d_identity"


@pytest.mark.parametrize("uri", ["/api/sandbox/v1/queue/q-abcdefghij", "/api/viewer/queue/vq-abcdefghij"])
def test_long_poll_timeout_covers_25s(server, uri):
    loc = resolve(server, uri)
    t = (loc.values("proxy_read_timeout") or server.values("proxy_read_timeout"))[0][0]
    assert int(t.rstrip("s")) >= 30


# ---------------------------------------------------------------- 静态世界（ADR-092、ADR-109）


def test_world_whitelist_matches_public_profile(server, public_profile):
    allow = public_profile["net"]["worlds_allow"]
    assert allow == public_profile["sandbox"]["worlds_allow"]
    assert len(allow) == 7 and "synthcity" in allow
    regexes = [d.args[1] for d in server.find("location") if d.args[0] == "~" and "synthcity" in d.args[1]]
    assert len(regexes) == 2
    for rx in regexes:
        ids = re.match(r"\^/worlds/\(([^)]*)\)", rx).group(1).split("|")
        assert ids == allow, rx


@pytest.mark.parametrize("wid", ["synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco"])
def test_world_files_are_aliased(server, wid):
    wj = resolve(server, f"/worlds/{wid}/world.json")
    assert wj.values("alias") == [[f"{WORLDS_ROOT}/$1/world.json"]]
    assert headers(wj)["cache-control"] == "no-cache"
    f = resolve(server, f"/worlds/{wid}/visual/pointcloud/octree.bin")
    assert f.values("alias") == [[f"{WORLDS_ROOT}/$1/$2"]]
    assert f.values("max_ranges") == [["1"]]                         # 多区间按整体 200（与 show-api 相同）
    assert headers(f)["cache-control"] == "$drone4d_world_cc"


@pytest.mark.parametrize("uri", ["/worlds/synthcity/.status/x.json", "/worlds/.geo-cache/synthcity/a/manifest.json",
                                 "/worlds/.status/synthcity.json", "/worlds/shenzhen/.work/x", "/worlds/.staging/a"])
def test_hidden_paths_are_not_served(server, uri):
    loc = resolve(server, uri)
    assert loc.values("return") == [["404"]], uri


def test_shared_env_only(server):
    loc = resolve(server, "/worlds/_shared/env/turb/box.awrv")
    assert loc.values("alias") == [[f"{WORLDS_ROOT}/_shared/$1"]]
    assert upstream(resolve(server, "/worlds/_shared/other/x")) == "drone4d_backend"


def test_world_cache_control_map_matches_show_api(conf):
    assert eval_map(conf, "$arg_v", "$drone4d_world_cc", "") == "no-cache"
    assert eval_map(conf, "$arg_v", "$drone4d_world_cc", "9e05c2364f58") == IMMUTABLE


def test_world_dir_matches_env_example():
    env = (ROOT / "tools" / "deploy" / "emax" / "env.example").read_text(encoding="utf-8")
    assert f"AWR_WORLDS_DIR={WORLDS_ROOT}\n" in env


def test_world_mime_types(server):
    loc = resolve(server, "/worlds/synthcity/semantic/zones.geojson")
    types = {ext: d.name for d in loc.one("types").block for ext in d.args}
    assert types["json"] == "application/json" and types["geojson"] == "application/geo+json"
    assert {types[e] for e in ("bin", "f32", "u8", "u16", "awrv")} == {"application/octet-stream"}


# ---------------------------------------------------------------- 安全头与 HSTS


def test_add_header_locations_repeat_hsts(server):
    """location 内一旦有 add_header 就不再继承 server 层的 HSTS，必须重复（否则这些响应丢 HSTS）。"""
    assert HSTS in server.values("add_header")
    for loc in server.find("location"):
        if loc.find("add_header"):
            assert HSTS in loc.values("add_header"), loc.args


@pytest.mark.parametrize("uri", ["/worlds/synthcity/world.json", "/worlds/chicago/geometry/terrain/dsm_2m.f32",
                                 "/worlds/_shared/env/turb/a.awrv"])
def test_static_world_security_headers_match_api(server, uri):
    h = headers(resolve(server, uri))
    for k, v in SECURITY_HEADERS:
        assert h.get(k.decode()) == v.decode(), (uri, k)


def test_proxied_locations_do_not_duplicate_security_headers(server):
    """api 已在响应中带 COOP、COEP、CORP；nginx 再加一份会成为重复头（浏览器把 COOP 的重复值当作无效）。"""
    for loc in server.find("location"):
        if upstream(loc):
            names = {a[0].lower() for a in loc.values("add_header")}
            assert not names & {"cross-origin-opener-policy", "cross-origin-embedder-policy", "cross-origin-resource-policy"}, loc.args
    assert not {a[0].lower() for a in server.values("add_header")} - {"strict-transport-security"}


# ---------------------------------------------------------------- D2.1：容量微缓存、构建信息、清单、SDK


CAP = "/api/sandbox/v1/capacity"


def test_capacity_microcache(server):
    """容量接口 1 s 微缓存（D2-AC-42、ADR-119）：无论上游回复什么，到达 sandbox-api ≤ 1 次/s（D2-FX-R2-deploy 第 2 轮：
    容器内 nginx 1.24 实测改前的配置在上游 503、500 时 69 至 85 次/s，GET 与 HEAD 混合时 2 次/s，冷缓存且上游 2.5 s 时 101 次；
    行为用例见 test_nginx_site_e2e.py）。"""
    loc = resolve(server, CAP)
    assert loc.args == ["=", CAP]
    assert loc.values("proxy_cache") == [["drone4d_micro"]]
    assert loc.values("proxy_cache_valid") == [["any", "1s"]]               # 任何回复（含 503 211、500、502、504）都缓存 1 s
    assert loc.values("proxy_cache_use_stale") == [["updating"]]             # 错误按 stale 时不入缓存，每个请求都回源
    assert loc.values("proxy_cache_background_update") == [["off"]]          # 后台刷新失败时访客一直拿到旧副本
    assert loc.values("proxy_cache_lock") == [["on"]]
    assert loc.values("proxy_cache_key") == [["$uri"]]                       # HEAD 转 GET 回源，与 GET 共用；不含查询参数与访客变量
    assert loc.values("proxy_cache_methods") == [["GET", "HEAD"]]
    assert not loc.values("proxy_cache_convert_head") or loc.values("proxy_cache_convert_head") == [["on"]]
    ignore = loc.values("proxy_ignore_headers")[0]
    assert {"Cache-Control", "Expires", "Set-Cookie", "Vary", "X-Accel-Expires"} <= set(ignore), ignore


def test_capacity_lock_outlasts_upstream_timeouts(server):
    """冷缓存时其余访客等锁而不各自回源：回源的连接与读取超时之和小于锁等待，且不超过锁龄。"""
    loc = resolve(server, CAP)
    connect = secs((loc.values("proxy_connect_timeout") or server.values("proxy_connect_timeout"))[0][0])
    read = secs((loc.values("proxy_read_timeout") or server.values("proxy_read_timeout"))[0][0])
    lock_timeout = secs(loc.values("proxy_cache_lock_timeout")[0][0])
    lock_age = secs((loc.values("proxy_cache_lock_age") or [["5s"]])[0][0])
    assert connect + read < lock_timeout and connect + read <= lock_age, (connect, read, lock_timeout, lock_age)


def test_capacity_copy_is_visitor_independent(server):
    """共享副本只由 nginx 以自己的身份回源：规范路径（无查询参数）、不转发访客的请求头与请求体、只设 Host 与
    X-Forwarded-Proto；隐藏上游的 X-Request-Id 与 Set-Cookie；GET、HEAD 以外的方法 405 不回源。"""
    loc = resolve(server, CAP)
    assert loc.values("proxy_pass") == [["http://drone4d_sandbox$uri"]]
    assert loc.values("proxy_pass_request_headers") == [["off"]]
    assert loc.values("proxy_pass_request_body") == [["off"]]
    assert {a[0].lower(): a[1] for a in loc.values("proxy_set_header")} == {"host": "$host", "x-forwarded-proto": "$scheme",
                                                                            "connection": ""}
    assert {a[0].lower() for a in loc.values("proxy_hide_header")} == {"x-request-id", "set-cookie"}
    (cond,) = loc.find("if")
    assert cond.args == ["($request_method", "!~", "^(GET|HEAD)$", ")"], cond.args
    assert cond.values("return") == [["405"]]
    assert ["X-Drone4d-Cache", "$upstream_cache_status", "always"] in loc.values("add_header")


def test_location_proxy_set_header_is_deliberate(server):
    """location 内一旦出现 proxy_set_header 就不再继承 server 层那组：要么整组重复，要么有意不转发访客请求头。"""
    base = {a[0].lower() for a in server.values("proxy_set_header")}
    assert {"host", "x-forwarded-for", "connection"} <= base
    for loc in server.find("location"):
        own = {a[0].lower() for a in loc.values("proxy_set_header")}
        if own:
            assert base <= own or loc.values("proxy_pass_request_headers") == [["off"]], loc.args


@pytest.mark.parametrize(("uri", "file"), [("/build-info.json", "build-info.json"), ("/manifest.webmanifest", "manifest.webmanifest")])
def test_build_info_and_manifest_no_cache(server, uri, file):
    loc = resolve(server, uri)
    assert loc.values("alias") == [[f"/opt/anet-drone4d/current/apps/web/dist/{file}"]]
    assert headers(loc)["cache-control"] == "no-cache"


def test_sdk_alias(server):
    loc = resolve(server, "/sdk/awr_sandbox.py")
    assert loc.values("alias") == [["/opt/anet-drone4d/current/tools/sdk/"]]
    assert (ROOT / "tools" / "sdk" / "awr_sandbox.py").is_file()
    assert resolve(server, "/sdk/examples/__pycache__/x.pyc").values("return") == [["404"]]
    assert resolve(server, "/sdk/.hidden").values("return") == [["404"]]


# ---------------------------------------------------------------- 地址前缀 map 与 prefix_of 对拍（200 个地址）


def _v6(rng: random.Random) -> str:
    groups = [rng.randrange(0x10000) for _ in range(8)]
    a, b = sorted(rng.sample(range(9), 2))                    # 随机置零一段，覆盖 "::" 落在不同位置的规范压缩形式
    for i in range(a, b):
        groups[i] = 0
    return str(ipaddress.IPv6Address(":".join(f"{g:x}" for g in groups)))


def _net_of_key(key: str) -> ipaddress.IPv6Network:
    """IPv6 键是 /64 前 4 组的十六进制文本（无前导零），唯一确定一个 /64。"""
    groups = key.split(":")
    assert len(groups) == 4 and all(re.fullmatch(r"[0-9a-f]{1,4}", g) for g in groups), key
    assert all(g == "0" or not g.startswith("0") for g in groups), key
    return ipaddress.IPv6Network(":".join(groups) + "::/64")


def test_prefix_map_parity_with_prefix_of(conf):
    rng = random.Random(20261005)
    addrs = [str(ipaddress.IPv4Address(rng.getrandbits(32))) for _ in range(70)]
    addrs += [_v6(rng) for _ in range(110)]
    addrs += [f"::ffff:{ipaddress.IPv4Address(rng.getrandbits(32))}" for _ in range(15)]
    addrs += ["::1", "::", "::13d:9bea:a2bf:9ed5:8892:98dd", "::5:6:7:8:9", "2001:db8::1"]
    assert len(addrs) == 200
    keys_per_net: dict[str, set[str]] = {}
    for a in addrs:
        key = eval_map(conf, "$remote_addr", "$drone4d_prefix", a, {"remote_addr": a})
        assert key, a                                        # 空键不计入 limit_req（等于不限流）
        want = prefix_of(a)
        if "." in key:                                        # IPv4 与 IPv4 映射地址：整个地址（/32）
            assert f"{key}/32" == want, (a, key)
        else:                                                 # IPv6：键唯一确定一个 /64
            assert str(_net_of_key(key)) == want, (a, key)
            keys_per_net.setdefault(want, set()).add(key)
    # 每个 /64 恰好一个键：同一 /64 内任意地址（不同的压缩形式）得到同一个键
    for net in {k for k in keys_per_net}:
        assert len(keys_per_net[net]) == 1, (net, keys_per_net[net])
    base = int(ipaddress.IPv6Address("2001:db8:1::"))
    keys = set()
    for low in (0, 1, 0x10000, 0x1_0000_0000, 0xFFFF_0000_0000_0000, 0x0001_0000_0000_0005):
        a = str(ipaddress.IPv6Address(base + low))
        keys.add(eval_map(conf, "$remote_addr", "$drone4d_prefix", a, {"remote_addr": a}))
    assert keys == {"2001:db8:1:0"}
