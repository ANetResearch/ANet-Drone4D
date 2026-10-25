#!/usr/bin/env python3
"""d3-registry 的提交镜像接入与部署演练（AWR-05 §5.6.7、ADR-183；AWR-17 §18.23.2、§18.23.3；D3-AC-59）。

子命令（结果写 JSON；不输出、不记录任何令牌、Cookie 与口令）：

- `materialize --data-root D [--registry 127.0.0.1:18671] [--once | --interval S] [--prune] [--out F]`（dmax 宿主执行，只用标准库与
  docker 命令行，Python ≥ 3.10）：推送到 d3-registry 的镜像 → 宿主 docker（内部提交流水线 `subworker` 以
  `docker image inspect <image_digest>` 读取，D3-AC-59 扫描阶段）。对每个 RECEIVED 提交先在团队命名空间中查找来源；镜像已在宿主
  时，受邀成员的提交只有来源在本团队命名空间才记 `present`，否则记 `foreign_image`（他队镜像即使已被拉取到宿主也不认），
  内部成员可提交宿主本机构建的镜像。查找只在提交方团队的命名空间 `<ns>/…`（`registry_namespace(team)`，与 `awr.bench.auth`
  同一口径）中进行：先按清单摘要直接读取，再遍历标签比对清单摘要与配置摘要（多架构索引取 linux/amd64）；镜像不在宿主而命中时
  `docker pull <registry>/<repo>@<清单摘要>`
  （经回环直连后端，Docker 缺省信任 127.0.0.0/8 的明文仓库），再以 `docker image inspect <image_digest>` 确认。
  宿主为 overlay2 存储时镜像 ID 即配置摘要，提交须以配置摘要（`docker image inspect --format '{{.Id}}'` 或清单的 `config.digest`）
  作 `image_digest`；以清单摘要提交时记 `digest_is_manifest` 并给出配置摘要。记录写 `submissions/<sid>/registry.json`
  （仓库、清单与配置摘要、拉取时刻、结论），不改提交记录本身。`--prune` 删除已 REJECTED、WITHDRAWN 或 FAILED 的提交拉取的镜像。
- `drill --lab https://<ZeroTier 地址>:18660 --registry https://<ZeroTier 地址>:18670 --data-root D --cafile F [--trials N] [--out F]`
  （d3-test-b25-drill 容器内执行，需 httpx 与 awr）：进程内临时签发两个受邀团队与一个管理员的 bench token（1 h，结束时撤销），
  经部署的鉴权前端逐项核查：
  1. 受邀成员经 lab-web 登录、BN-82 取推送令牌；对实时 topic（BN-04 aud=lab-rt）、Rerun catalog（BN-04 aud=rerun、BN-57）、
     真值接口（BN-52）与他队提交返回 403 115，且审计中有对应 `deny` 记录；
  2. d3-registry：无凭据与错误口令 401（`WWW-Authenticate: Basic realm="d3-registry"`）；本团队命名空间推送（分块上传 + 清单）
     201 与拉取 200（摘要一致）；推送到他队命名空间、拉取他队镜像、`_catalog`、受邀成员 DELETE 一律 403 且审计；管理员 DELETE
     202；清单推送写 `registry_push` 审计；
  3. 撤销：N 次独立试验（每次新签发受邀 token、登录、取推送令牌），撤销 bench token 后并行轮询会话（GET /session）与推送
     令牌（HEAD 清单），记录至 401 的时延，阈值 ≤ 2 s（D3-AC-59）。
  结束时输出演练镜像（团队 A）的仓库与摘要，供 `regctl.sh materialize-drill` 接续"推送 → 接入 → 扫描"链路。
- `scan --digest D --cache C --work W [--out F]`（d3-test-b25-scan 容器内，挂载 docker 套接字）：以内部提交流水线的
  `subworker.scan_image`（inspect 与 Trivy 离线扫描）判定接入的镜像，结论写 JSON。
- `image --label L`：打印演练镜像的层、配置与清单摘要（确定性构造，本机与 dmax 结果相同）。

退出码：0 全部通过；1 有未通过项；2 参数或环境错误。
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PREFIX = "/api/bench/v1"
REALM = 'Basic realm="d3-registry"'
MT_DOCKER = "application/vnd.docker.distribution.manifest.v2+json"
MT_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MT_OCI = "application/vnd.oci.image.manifest.v1+json"
MT_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MT_CONFIG = "application/vnd.docker.container.image.v1+json"
MT_LAYER = "application/vnd.docker.image.rootfs.diff.tar.gzip"
ACCEPT = ", ".join((MT_DOCKER, MT_OCI, MT_DOCKER_LIST, MT_OCI_INDEX))
TERMINAL_PRUNE = ("REJECTED", "WITHDRAWN", "FAILED")
REVOKE_MAX_S = 2.0
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: str | Path | None, obj: Any) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, p)
    else:
        print(text)


def registry_namespace(team: str) -> str:
    """团队 → 仓库命名空间（与 `awr.bench.auth.registry_namespace` 相同：小写，非 `[a-z0-9]` 折叠为 `-`）。宿主不导入 awr。"""
    ns = re.sub(r"[^a-z0-9]+", "-", team.lower()).strip("-")
    if not ns:
        raise ValueError(f"team {team!r} has no valid registry namespace")
    return ns


def sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


# ── 演练镜像（单层，linux/amd64，Docker schema 2） ──────────────────────────────────────────────────────────────────────

def drill_image(label: str) -> dict:
    """确定性的最小镜像：一层（`/d3-registry-drill.txt`）、配置与清单的字节及摘要。"""
    payload = f"d3-registry drill image ({label}); D3-AC-59\n".encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        ti = tarfile.TarInfo("d3-registry-drill.txt")
        ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = len(payload), 0, 0o644, 0, 0, "", ""
        tf.addfile(ti, io.BytesIO(payload))
    tar = raw.getvalue()
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", mtime=0) as g:
        g.write(tar)
    layer = gz.getvalue()
    config = json.dumps({"architecture": "amd64", "os": "linux", "created": "1970-01-01T00:00:00Z",
                         "config": {"Cmd": ["/d3-registry-drill"], "Labels": {"org.anet.drone4d.drill": label}},
                         "rootfs": {"type": "layers", "diff_ids": [sha256(tar)]},
                         "history": [{"created": "1970-01-01T00:00:00Z", "created_by": "regctl.py drill"}]},
                        separators=(",", ":"), sort_keys=True).encode()
    manifest = json.dumps({"schemaVersion": 2, "mediaType": MT_DOCKER,
                           "config": {"mediaType": MT_CONFIG, "size": len(config), "digest": sha256(config)},
                           "layers": [{"mediaType": MT_LAYER, "size": len(layer), "digest": sha256(layer)}]},
                          separators=(",", ":")).encode()
    return {"layer": layer, "config": config, "manifest": manifest, "layer_digest": sha256(layer), "config_digest": sha256(config),
            "manifest_digest": sha256(manifest)}


# ── materialize（宿主，标准库） ──────────────────────────────────────────────────────────────────────────────────────────

class Registry:
    """后端（回环、无鉴权）的只读访问。"""

    def __init__(self, base: str, timeout: float = 30.0) -> None:
        self.base = base.rstrip("/") if "://" in base else f"http://{base.rstrip('/')}"
        self.timeout = timeout

    def get(self, path: str, accept: str | None = None) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(self.base + path, headers={"Accept": accept} if accept else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

    def repositories(self) -> list[str]:
        out, last = [], ""
        while True:
            st, _, body = self.get(f"/v2/_catalog?n=1000{'&last=' + last if last else ''}")
            if st != 200:
                raise OSError(f"registry catalog: HTTP {st}")
            repos = json.loads(body).get("repositories") or []
            out += repos
            if len(repos) < 1000:
                return out
            last = repos[-1]

    def tags(self, repo: str) -> list[str]:
        st, _, body = self.get(f"/v2/{repo}/tags/list")
        return (json.loads(body).get("tags") or []) if st == 200 else []

    def manifest(self, repo: str, ref: str) -> tuple[str, dict] | None:
        """→ (清单摘要, 清单)；索引取 linux/amd64 子清单（返回子清单的摘要）。"""
        st, h, body = self.get(f"/v2/{repo}/manifests/{ref}", ACCEPT)
        if st != 200:
            return None
        doc = json.loads(body)
        digest = h.get("docker-content-digest") or sha256(body)
        if doc.get("mediaType") in (MT_DOCKER_LIST, MT_OCI_INDEX) or "manifests" in doc:
            child = next((m for m in doc.get("manifests", []) if (m.get("platform") or {}).get("os") == "linux"
                          and (m.get("platform") or {}).get("architecture") == "amd64"), None)
            return self.manifest(repo, child["digest"]) if child else None
        return digest, doc


def find_image(reg: Registry, ns: str, digest: str) -> dict | None:
    """在命名空间 `<ns>/…` 中查找清单摘要或配置摘要为 `digest` 的镜像。"""
    repos = [r for r in reg.repositories() if r.startswith(ns + "/")]
    for repo in repos:                                       # 以清单摘要提交（docker push 打印的 digest）
        m = reg.manifest(repo, digest)
        if m is not None:
            return {"repository": repo, "manifest_digest": m[0], "config_digest": (m[1].get("config") or {}).get("digest"), "matched": "manifest"}
    for repo in repos:                                       # 以配置摘要（镜像 ID）提交：遍历标签
        for tag in reg.tags(repo):
            m = reg.manifest(repo, tag)
            if m is not None and ((m[1].get("config") or {}).get("digest") == digest or m[0] == digest):
                return {"repository": repo, "tag": tag, "manifest_digest": m[0], "config_digest": m[1]["config"]["digest"],
                        "matched": "config" if m[1]["config"]["digest"] == digest else "manifest"}
    return None


def _docker(docker: str, *args: str, timeout: float = 1800) -> subprocess.CompletedProcess:
    return subprocess.run([docker, *args], capture_output=True, text=True, check=False, timeout=timeout)


def _inspect(docker: str, ref: str) -> dict | None:
    r = _docker(docker, "image", "inspect", ref, "--format", "{{json .}}", timeout=60)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def materialize_once(data_root: Path, reg: Registry, *, docker: str = "docker", prune: bool = False, registry_host: str | None = None) -> dict:
    subs = Path(data_root) / "submissions"
    host = registry_host or reg.base.split("://", 1)[1]
    items = []
    for p in sorted(subs.glob("sub-*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid, state, digest = d.get("submission_id"), d.get("state"), d.get("image_digest", "")
        rec_path = subs / str(sid) / "registry.json"
        rec = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.exists() else None
        if prune and state in TERMINAL_PRUNE and rec and rec.get("status") == "pulled" and not rec.get("pruned_at"):
            r = _docker(docker, "image", "rm", f"{host}/{rec['repository']}@{rec['manifest_digest']}", timeout=120)
            rec.update(pruned_at=utc(), prune_rc=r.returncode)
            write_json(rec_path, rec)
            items.append({"submission_id": sid, "state": state, "action": "pruned", "rc": r.returncode})
            continue
        if state != "RECEIVED" or not _DIGEST.match(str(digest)):
            continue
        team = (d.get("_meta") or {}).get("team") or ""
        role = (d.get("_meta") or {}).get("role") or "researcher"
        out = {"schema": "awr.d3.registry_materialize.v1", "submission_id": sid, "team": team, "role": role, "image_digest": digest,
               "t_utc": utc()}
        try:
            ns = registry_namespace(team)
            hit = find_image(reg, ns, digest)
        except (ValueError, OSError) as e:
            ns, hit = None, None
            out.update(status="error", error=f"{type(e).__name__}: {e}"[:200])
        out["namespace"] = ns
        if hit is not None:
            out.update(hit)
        if "status" in out:
            pass
        elif _inspect(docker, digest) is not None:
            # 宿主已有：内部成员可提交宿主本机构建的镜像；受邀成员只能提交本团队命名空间中的镜像（他队镜像即使已被拉取也不认）
            out.update(status="present" if hit is not None or role != "invited" else "foreign_image",
                       in_namespace=hit is not None)
        elif hit is None:
            out.update(status="not_found")
        else:
            ref = f"{host}/{hit['repository']}@{hit['manifest_digest']}"
            r = _docker(docker, "pull", "-q", ref)
            if r.returncode != 0:
                out.update(status="pull_failed", error=(r.stderr or r.stdout).strip()[-300:])
            elif _inspect(docker, digest) is not None:
                out.update(status="pulled", pulled_at=utc(), in_namespace=True)
            else:
                info = _inspect(docker, ref) or {}
                out.update(status="digest_is_manifest", local_image_id=info.get("Id"),
                           hint="submit image_digest = config digest (image ID) on the overlay2 host store")
        if rec is None or rec.get("status") != out.get("status") or out.get("status") == "pulled":
            write_json(rec_path, out)
        items.append({k: out.get(k) for k in ("submission_id", "team", "image_digest", "status", "repository", "matched")})
    return {"schema": "awr.d3.registry_materialize_run.v1", "t_utc": utc(), "data_root": str(data_root), "registry": host, "items": items}


def cmd_materialize(a: argparse.Namespace) -> int:
    reg = Registry(a.registry)
    while True:
        rep = materialize_once(Path(a.data_root), reg, docker=a.docker, prune=a.prune)
        write_json(a.out, rep)
        if a.once or not a.interval:
            bad = [i for i in rep["items"] if i["status"] not in ("present", "pulled", None)]
            return 1 if bad else 0
        time.sleep(a.interval)


# ── drill（容器内，httpx + awr） ──────────────────────────────────────────────────────────────────────────────────────

class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, ok: bool, **detail: Any) -> bool:
        self.items.append({"check": name, "ok": bool(ok), **detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {json.dumps(detail, ensure_ascii=False)[:300]}", file=sys.stderr)
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(i["ok"] for i in self.items)


def _code(r) -> Any:
    try:
        return r.json().get("code")
    except ValueError:
        return None


class Push:
    """经鉴权前端的 OCI 分发协议客户端（Basic：用户名为主体，口令为推送令牌；行为与 docker 客户端相同：先 HEAD 去重，
    分块上传为 POST → PATCH → PUT ?digest=）。"""

    def __init__(self, client, base: str, user: str, token: str) -> None:
        self.c, self.base, self.auth = client, base.rstrip("/"), (user, token)

    def req(self, method: str, path: str, **kw):
        url = path if path.startswith("http") else self.base + path
        return self.c.request(method, url, auth=self.auth, **kw)

    def upload(self, repo: str, digest: str, data: bytes, chunk: int = 4096) -> tuple[int, list[int]]:
        statuses = []
        h = self.req("HEAD", f"/v2/{repo}/blobs/{digest}")
        statuses.append(h.status_code)
        if h.status_code == 200:
            return 200, statuses
        r = self.req("POST", f"/v2/{repo}/blobs/uploads/")
        statuses.append(r.status_code)
        if r.status_code != 202:
            return r.status_code, statuses
        loc = r.headers["location"]
        for i in range(0, len(data), chunk):
            part = data[i:i + chunk]
            r = self.req("PATCH", loc, content=part, headers={"Content-Type": "application/octet-stream",
                                                                 "Content-Range": f"{i}-{i + len(part) - 1}"})
            statuses.append(r.status_code)
            if r.status_code != 202:
                return r.status_code, statuses
            loc = r.headers["location"]
        sep = "&" if "?" in loc else "?"
        r = self.req("PUT", f"{loc}{sep}digest={digest}", headers={"Content-Length": "0"})
        statuses.append(r.status_code)
        return r.status_code, statuses

    def push_image(self, repo: str, tag: str, img: dict) -> dict:
        lr, ls = self.upload(repo, img["layer_digest"], img["layer"])
        cr, cs = self.upload(repo, img["config_digest"], img["config"])
        m = self.req("PUT", f"/v2/{repo}/manifests/{tag}", content=img["manifest"], headers={"Content-Type": MT_DOCKER})
        return {"layer": lr, "layer_steps": ls, "config": cr, "config_steps": cs, "manifest": m.status_code,
                "digest": m.headers.get("docker-content-digest")}


def _t(r: dict) -> float:
    """审计记录时刻（UNIX 秒；`AuditLog` 写 `t_unix_ns` 字符串）。"""
    try:
        return int(r.get("t_unix_ns", 0)) / 1e9
    except (TypeError, ValueError):
        return 0.0


def _login(client, lab: str, token: str) -> dict:
    r = client.post(f"{lab}{PREFIX}/session", json={"token": token}, headers={"origin": lab})
    return {"d3_session": r.cookies.get("d3_session")} if r.status_code == 204 and r.cookies.get("d3_session") else {}


def _push_token(client, lab: str, jar: dict) -> tuple[int, dict]:
    r = client.post(f"{lab}{PREFIX}/registry/token", cookies=jar, headers={"origin": lab, "x-d3-csrf": "1"})
    return r.status_code, (r.json() if r.status_code == 200 else {"code": _code(r)})


def revoke_trial(client, auth, lab: str, reg: str, team: str, k: int, repo_ref: str, *, period_s: float = 0.02, limit_s: float = 10.0) -> dict:
    """一次撤销试验：新签发受邀 token → 登录 → 推送令牌 → 撤销 → 并行轮询会话与推送令牌至 401。"""
    tid, tok = auth.issue_token(f"b25-revoke-{k}", "invited", (team,), ttl_s=3600)
    try:
        jar = _login(client, lab, tok)
        st, body = _push_token(client, lab, jar)
        ptok = body.get("token")
        if not jar or st != 200 or not ptok:
            return {"trial": k, "ok": False, "error": f"setup login={bool(jar)} push_token={st}"}
        p = Push(client, reg, body.get("username", ""), ptok)
        pre = (client.get(f"{lab}{PREFIX}/session", cookies=jar).status_code, p.req("HEAD", repo_ref).status_code)
        res: dict[str, float | None] = {}

        def poll(name: str, fn) -> None:
            end = t0 + limit_s
            while time.monotonic() < end:
                if fn() == 401:
                    res[name] = time.monotonic() - t0
                    return
                time.sleep(period_s)
            res[name] = None

        t0 = time.monotonic()
        auth.revoke_token(tid)
        ths = [threading.Thread(target=poll, args=("session_s", lambda: client.get(f"{lab}{PREFIX}/session", cookies=jar).status_code)),
               threading.Thread(target=poll, args=("push_s", lambda: p.req("HEAD", repo_ref).status_code))]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        ok = pre == (200, 200) and all(v is not None and v <= REVOKE_MAX_S for v in res.values())
        return {"trial": k, "ok": ok, "pre": list(pre), **{k2: (round(v, 4) if v is not None else None) for k2, v in res.items()}}
    finally:
        auth.revoke_token(tid)


def cmd_drill(a: argparse.Namespace) -> int:
    import ssl

    import httpx

    from awr.bench.auth import BenchAuth
    from awr.bench.settings import BenchSettings

    ctx = ssl.create_default_context(cafile=a.cafile) if a.cafile else ssl.create_default_context()
    s = BenchSettings(data_root=Path(a.data_root))
    auth = BenchAuth(s.keys_dir, s.data_root / "registry-drill.sqlite")
    c = Checks()
    t_start = time.time()
    team_a, team_b, team_z = f"{a.tag}-drill-a", f"{a.tag}-drill-b", f"{a.tag}-drill-admin"
    issued = []
    rec: dict[str, Any] = {"schema": "awr.d3.registry_drill.v1", "t_start_utc": utc(), "lab": a.lab, "registry": a.registry}
    lab, reg = a.lab.rstrip("/"), a.registry.rstrip("/")
    try:
        tid_a, tok_a = auth.issue_token(f"{a.tag}-invited-a", "invited", (team_a,), ttl_s=3600)
        tid_b, tok_b = auth.issue_token(f"{a.tag}-invited-b", "invited", (team_b,), ttl_s=3600)
        tid_z, tok_z = auth.issue_token(f"{a.tag}-admin", "admin", (team_z,), ttl_s=3600)
        issued += [tid_a, tid_b, tid_z]
        with httpx.Client(verify=ctx, follow_redirects=False, timeout=30.0) as client:
            # 1. 受邀成员经 lab-web：登录、BN-82、越权 403 115 与审计
            jar_a, jar_b, jar_z = _login(client, lab, tok_a), _login(client, lab, tok_b), _login(client, lab, tok_z)
            r = client.get(f"{lab}{PREFIX}/session", cookies=jar_a)
            c.add("invited login via lab-web, session role invited", r.status_code == 200 and r.json().get("role") == "invited",
                  status=r.status_code, role=r.json().get("role") if r.status_code == 200 else None)
            st_a, pa = _push_token(client, lab, jar_a)
            _, pb = _push_token(client, lab, jar_b)
            _, pz = _push_token(client, lab, jar_z)
            c.add("BN-82 push token for invited team namespace (10 min)", st_a == 200 and pa.get("namespace") == team_a
                  and pa.get("repository_prefix") == f"{team_a}/" and 0 < int(pa.get("exp_unix_ns", 0)) // 10**9 - time.time() <= 600,
                  status=st_a, namespace=pa.get("namespace"))
            denied = [("POST", "/session/short-token", {"aud": "lab-rt"}, "BN-04 aud=lab-rt (real-time topics)"),
                      ("POST", "/session/short-token", {"aud": "rerun"}, "BN-04 aud=rerun (Rerun catalog)"),
                      ("GET", f"/runs/{a.run}/viewer_url", None, "BN-57 viewer_url (Rerun catalog)"),
                      ("GET", f"/runs/{a.run}/truth/event/tape", None, "BN-52 truth tape"),
                      ("GET", f"/runs/{a.run}/truth/frames/uav0/rgb/0", None, "BN-52 truth frames")]
            for method, path, body, label in denied:
                r = client.request(method, f"{lab}{PREFIX}{path}", cookies=jar_a, json=body, headers={"origin": lab, "x-d3-csrf": "1"})
                c.add(f"invited -> {label}: 403 115", r.status_code == 403 and _code(r) == 115, status=r.status_code, code=_code(r))
            rows = [x for x in auth.audit.read() if x.get("principal") == f"{a.tag}-invited-a" and x.get("code") == 115 and _t(x) >= t_start - 1]
            c.add("deny audit rows for invited 115 (lab-web)", len(rows) >= len(denied), n=len(rows))

            # 2. d3-registry
            r = client.get(f"{reg}/v2/")
            c.add("registry /v2/ without credentials: 401 Basic realm + API version header",
                  r.status_code == 401 and r.headers.get("www-authenticate") == REALM
                  and r.headers.get("docker-distribution-api-version") == "registry/2.0", status=r.status_code,
                  challenge=r.headers.get("www-authenticate"))
            r = client.get(f"{reg}/v2/", auth=(f"{a.tag}-invited-a", "not-a-token"))
            c.add("registry /v2/ with a wrong password: 401", r.status_code == 401, status=r.status_code)
            A = Push(client, reg, pa.get("username", ""), pa.get("token", ""))
            B = Push(client, reg, pb.get("username", ""), pb.get("token", ""))
            Z = Push(client, reg, pz.get("username", ""), pz.get("token", ""))
            r = A.req("GET", "/v2/")
            c.add("registry /v2/ with push token: 200", r.status_code == 200, status=r.status_code)
            img_a, img_b = drill_image(team_a), drill_image(team_b)
            repo_a, repo_b = f"{team_a}/drill", f"{team_b}/drill"
            res = A.push_image(repo_a, a.tag, img_a)
            c.add("invited push to own namespace: blobs 201 (or 200 when already present), manifest 201, digest echoed",
                  res["layer"] in (200, 201) and res["config"] in (200, 201) and res["manifest"] == 201
                  and res["digest"] == img_a["manifest_digest"], **res)
            resb = B.push_image(repo_b, a.tag, img_b)
            c.add("second team push to its namespace: manifest 201", resb["manifest"] == 201, manifest=resb["manifest"])
            r = A.req("GET", f"/v2/{repo_a}/manifests/{a.tag}", headers={"Accept": MT_DOCKER})
            r2 = A.req("GET", f"/v2/{repo_a}/blobs/{img_a['layer_digest']}")
            c.add("invited pull own image: manifest and blob 200, digests match", r.status_code == 200 and sha256(r.content) == img_a["manifest_digest"]
                  and r2.status_code == 200 and sha256(r2.content) == img_a["layer_digest"], manifest=r.status_code, blob=r2.status_code)
            r = A.req("GET", f"/v2/{repo_a}/tags/list")
            c.add("invited tags/list own repository: 200", r.status_code == 200 and a.tag in (r.json().get("tags") or []), status=r.status_code)
            t_deny = time.time()
            forbidden = [("POST", f"/v2/{repo_b}/blobs/uploads/", "push to another team's namespace"),
                         ("PUT", f"/v2/{repo_b}/manifests/x", "manifest PUT into another team's namespace"),
                         ("GET", f"/v2/{repo_b}/manifests/{a.tag}", "pull another team's image"),
                         ("GET", "/v2/_catalog", "_catalog"),
                         ("DELETE", f"/v2/{repo_a}/manifests/{img_a['manifest_digest']}", "invited DELETE (admin only)"),
                         ("POST", f"/v2/{team_a}-x/drill/blobs/uploads/", "prefix collision namespace")]
            for method, path, label in forbidden:
                r = A.req(method, path, content=b"{}" if method == "PUT" else None,
                          headers={"Content-Type": MT_DOCKER} if method == "PUT" else None)
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                c.add(f"registry {label}: 403 DENIED", r.status_code == 403 and (body.get("errors") or [{}])[0].get("code") == "DENIED",
                      status=r.status_code)
            rows = [x for x in auth.audit.read() if x.get("principal") == f"{a.tag}-invited-a" and x.get("route") == "registry"
                    and x.get("code") == 115 and _t(x) >= t_deny - 1]
            c.add("deny audit rows for registry 115", len(rows) >= len(forbidden), n=len(rows))
            pushes = [x for x in auth.audit.read() if x.get("principal") == f"{a.tag}-invited-a" and x.get("repository") == repo_a
                      and x.get("action") == "registry_push" and _t(x) >= t_start - 1]
            c.add("registry_push audit for own manifest PUT", len(pushes) >= 1, n=len(pushes))
            audit_text = Path(auth.audit.path_for()).read_text(encoding="utf-8")
            c.add("audit contains no token plaintext", "d3st_v1." not in audit_text and "d3bt_" not in audit_text)
            r = Z.req("GET", f"/v2/{repo_b}/manifests/{a.tag}", headers={"Accept": MT_DOCKER})
            c.add("admin pull any repository: 200", r.status_code == 200, status=r.status_code)
            r = Z.req("DELETE", f"/v2/{repo_b}/manifests/{img_b['manifest_digest']}")
            c.add("admin DELETE manifest: 202", r.status_code == 202, status=r.status_code)
            r = Z.req("GET", "/v2/_catalog")
            c.add("admin _catalog: 200", r.status_code == 200, status=r.status_code)

            # 3. 撤销时延
            trials = [revoke_trial(client, auth, lab, reg, team_a, k, f"/v2/{repo_a}/manifests/{a.tag}") for k in range(a.trials)]
            mx = {k: max((t.get(k) or 99.0) for t in trials) for k in ("session_s", "push_s")}
            c.add(f"revocation: session and push token invalid within {REVOKE_MAX_S:g} s ({a.trials} trials)",
                  all(t["ok"] for t in trials), max_session_s=mx["session_s"], max_push_s=mx["push_s"])
            rec["revocation"] = {"threshold_s": REVOKE_MAX_S, "trials": trials, "max": mx}
            r = A.req("GET", "/v2/")
            c.add("push token still valid before its own revocation (control)", r.status_code == 200, status=r.status_code)
            auth.revoke_token(tid_a)
            r = A.req("HEAD", f"/v2/{repo_a}/manifests/{a.tag}")
            r2 = client.get(f"{lab}{PREFIX}/session", cookies=jar_a)
            c.add("after revoking team A token: push token 401 and session 401", r.status_code == 401 and r2.status_code == 401,
                  push=r.status_code, session=r2.status_code)
        rec["image"] = {"repository": repo_a, "tag": a.tag, "manifest_digest": img_a["manifest_digest"], "config_digest": img_a["config_digest"],
                        "team": team_a}
    finally:
        for tid in issued:
            with contextlib.suppress(Exception):
                auth.revoke_token(tid)
    rec.update(t_end_utc=utc(), elapsed_s=round(time.time() - t_start, 2), ok=c.ok, checks=c.items)
    write_json(a.out, rec)
    return 0 if c.ok else 1


def cmd_scan(a: argparse.Namespace) -> int:
    """演练链路的扫描阶段：以内部提交流水线同一函数（`awr.bench.subworker.scan_image`：inspect 平台与 4 GB 体积、Trivy 离线扫描）
    判定接入的镜像；结论 `pass` 或 692 的 `detail.check`。需在挂载 docker 套接字的容器内执行（d3-test-b25-scan）。"""
    from awr.bench.subworker import ScanError, TrivyScanner, scan_image

    trivy = TrivyScanner(cache_dir=Path(a.cache), work_dir=Path(a.work), image=a.trivy_image)
    rec: dict[str, Any] = {"schema": "awr.d3.registry_drill_scan.v1", "t_start_utc": utc(), "image_digest": a.digest}
    try:
        rec["db"] = trivy.ready()
        bad = scan_image(a.digest, trivy=trivy, name=a.name, record=rec)
        rec["verdict"] = "pass" if bad is None else "rejected"
        rec["reason"] = bad
    except ScanError as e:
        rec.update(verdict="error", error=str(e)[:300])
    except Exception as e:
        rec.update(verdict="error", error=f"{type(e).__name__}: {e}"[:300])
    rec["t_end_utc"] = utc()
    write_json(a.out, rec)
    return 0 if rec["verdict"] == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="regctl.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("materialize")
    m.add_argument("--data-root", required=True)
    m.add_argument("--registry", default="127.0.0.1:18671")
    m.add_argument("--docker", default="docker")
    m.add_argument("--once", action="store_true")
    m.add_argument("--interval", type=float, default=0.0)
    m.add_argument("--prune", action="store_true")
    m.add_argument("--out")
    d = sub.add_parser("drill")
    d.add_argument("--lab", required=True)
    d.add_argument("--registry", required=True)
    d.add_argument("--data-root", required=True)
    d.add_argument("--cafile")
    d.add_argument("--tag", default="b25")
    d.add_argument("--run", default="cap-crop-3uav-demo")
    d.add_argument("--trials", type=int, default=5)
    d.add_argument("--out")
    sc = sub.add_parser("scan")
    sc.add_argument("--digest", required=True)
    sc.add_argument("--cache", required=True)
    sc.add_argument("--work", required=True)
    sc.add_argument("--name", default="d3-subworker-scan-drill")
    sc.add_argument("--trivy-image", default="d3-trivy:dev")
    sc.add_argument("--out")
    i = sub.add_parser("image", help="print the drill image digests for a label (JSON)")
    i.add_argument("--label", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "materialize":
        return cmd_materialize(a)
    if a.cmd == "scan":
        return cmd_scan(a)
    if a.cmd == "image":
        img = drill_image(a.label)
        write_json(None, {k: v for k, v in img.items() if k.endswith("_digest")})
        return 0
    return cmd_drill(a)


if __name__ == "__main__":
    sys.exit(main())
