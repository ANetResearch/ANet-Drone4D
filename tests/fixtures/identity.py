"""内部身份用例的口令夹具（WP-13b，M16；AWR-04 §15.9.5 第 2 条；D2-AC-51；d2-WP-04b-to-M16 第 4 条）。

口令只在测试运行时随机生成并写入临时密钥文件；仓库、夹具、日志与报告中从不出现任何口令（部署口令只由 install.sh
`--admin-secret-file` 写在服务器上）。本模块与 M17 的过渡夹具 `tests/sandbox/identitykit.py` 同接口：
`make_secret_file(dir) -> (path, secret)`。

| 函数 | 用途 |
|---|---|
| `make_secret_file(d, mode=0o600, newline=True)` | 在 d 下写一个随机口令（24 个 URL 安全字符，单行）的密钥文件，返回 (路径, 口令) |
| `secret_file(d)` | `SecretFile(path, secret, state_dir)`：密钥文件与身份库状态目录（锁定表、撤销表），供栈的环境 `AWR_ADMIN_SECRET_FILE`、`AWR_IDENTITY_STATE_DIR` 使用 |
| `SecretFile.env()` | 上述两个环境变量 |
| `issue_token(sf, principal=None)` | 以同一密钥派生的 `K_ident` 签发身份令牌（`awr.sandbox.identity.IdentityService.issue`），用于 R79 尚未由 show-api 提供时直接验证池与 sandbox-api 的内部档 |
| `revoke(sf, token)` | 把令牌的 jti 写入同一撤销表（与 R80 的效果相同） |
| `scan_paths(paths, secret)` | 在文件或目录树中查找口令：只返回 (文件, 行号)，从不返回或打印内容 |
| `rel04(secret_path, files=None)` | 运行 `tools/lint/check-release.mjs --secret-file`（REL-04，M00）；返回退出码、REL-04 报告数与输出中是否出现口令 |

本模块不是测试文件（文件名不以 `test_` 开头），由用例经 `sys.path` 引入：
`sys.path.insert(0, str(ROOT / "tests" / "fixtures")); import identity as idfx`。
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SecretFile", "issue_token", "make_secret_file", "rel04", "revoke", "scan_paths", "secret_file"]

ROOT = Path(__file__).resolve().parents[2]
SCAN_MAX_BYTES = 64 << 20


def make_secret_file(d: Path, *, mode: int = 0o600, newline: bool = True) -> tuple[Path, str]:
    """在 d 下写一个随机口令的密钥文件（0600、单行、可打印 ASCII、无空白、长度 24，满足 install.sh 的校验）；返回 (路径, 口令)。"""
    secret = secrets.token_urlsafe(18)
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"admin-{secrets.token_hex(4)}.secret"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (secret + ("\n" if newline else "")).encode("ascii"))
    finally:
        os.close(fd)
    os.chmod(p, mode)
    return p, secret


@dataclass(frozen=True)
class SecretFile:
    path: Path
    secret: str
    state_dir: Path

    def env(self) -> dict[str, str]:
        """栈进程的环境：密钥文件与身份库状态目录（两个 api 与池读同一目录中的撤销表）。"""
        return {"AWR_ADMIN_SECRET_FILE": str(self.path), "AWR_IDENTITY_STATE_DIR": str(self.state_dir)}

    def __repr__(self) -> str:                # 口令不进入断言信息与日志
        return f"SecretFile(path={str(self.path)!r}, state_dir={str(self.state_dir)!r})"


def secret_file(d: Path) -> SecretFile:
    d = Path(d)
    p, s = make_secret_file(d)
    sd = d / "identity-state"
    sd.mkdir(mode=0o700, parents=True, exist_ok=True)
    return SecretFile(path=p, secret=s, state_dir=sd)


def _service(sf: SecretFile):
    from awr.sandbox.identity import IdentityService

    return IdentityService(secret=sf.secret.encode("ascii"), state_dir=sf.state_dir)


def issue_token(sf: SecretFile, principal: str | None = None) -> tuple[str, dict]:
    """身份令牌与载荷（`scope = identity`、`role = internal`、12 h）；签名密钥与服务端同一派生（HKDF，ADR-123）。"""
    return _service(sf).issue(principal)


def revoke(sf: SecretFile, token: str) -> dict | None:
    return _service(sf).revoke(token)


def _files(paths: Iterable[Path | str]) -> Iterable[Path]:
    for p in paths:
        p = Path(p)
        if p.is_file():
            yield p
        elif p.is_dir():
            for q in sorted(p.rglob("*")):
                if q.is_file() and not q.is_symlink():
                    yield q


def scan_paths(paths: Iterable[Path | str], secret: str) -> list[tuple[str, int]]:
    """在文件与目录树中查找口令的出现位置（文件, 行号）；二进制文件按字节查找（行号为 0）。从不返回内容。"""
    needle = secret.encode("utf-8")
    hits: list[tuple[str, int]] = []
    for f in _files(paths):
        try:
            if f.stat().st_size > SCAN_MAX_BYTES:
                continue
            b = f.read_bytes()
        except OSError:
            continue
        i = b.find(needle)
        if i < 0:
            continue
        line = 0 if b"\0" in b[:4096] else b.count(b"\n", 0, i) + 1
        hits.append((str(f), line))
    return hits


@dataclass(frozen=True)
class Rel04Result:
    returncode: int
    findings: int
    secret_in_output: bool
    files: list[str]


def rel04(secret_path: Path, files: Iterable[Path | str] | None = None, *, timeout_s: float = 300.0) -> Rel04Result:
    """REL-04（M00 `check-release.mjs`）：files 为 None 时在无 git 的本机扫描 lint 范围，否则只扫描给出的文件；
    返回 REL-04 报告数（只数 REL-04 规则，REL-01/02 对临时文件与构建产物的报告不计）与输出中是否出现口令。"""
    secret = Path(secret_path).read_text(encoding="ascii").split("\n", 1)[0]
    cmd = ["node", str(ROOT / "tools" / "lint" / "check-release.mjs"), "--secret-file", str(secret_path)]
    lst = None
    if files is not None:
        fd, lst = tempfile.mkstemp(prefix="awr-rel04-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(str(Path(f).resolve()) for f in _files(files)) + "\n")
        cmd += ["--files", lst]
    runs = Path(tempfile.mkdtemp(prefix="awr-rel04-runs-"))
    env = {k: v for k, v in os.environ.items() if k != "AWR_ADMIN_SECRET_FILE"}
    env["AWR_RUNS_DIR"] = str(runs)                 # 摘要 runs/lint/<ts>-check-release.json 写到临时目录，随后一并检查
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout_s, check=False, env=env)
        extra = ""
        for p in sorted((runs / "lint").glob("*.json")) if (runs / "lint").is_dir() else []:
            try:
                extra += json.dumps(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    finally:
        if lst:
            Path(lst).unlink(missing_ok=True)
        for p in sorted(runs.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        runs.rmdir()
    out = r.stdout + r.stderr
    hits = [ln for ln in out.splitlines() if "REL-04" in ln and "contains the deployment admin secret" in ln]
    return Rel04Result(returncode=r.returncode, findings=len(hits), secret_in_output=secret in out or secret in extra,
                       files=[ln.split(":", 1)[0].strip() for ln in hits])
