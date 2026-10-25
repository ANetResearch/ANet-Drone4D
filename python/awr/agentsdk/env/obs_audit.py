"""发往 Agent 的全部消息的白名单在线审计（M26-FR-022、FR-026、FR-105、§8.4；ADR-161；D3-AC-16、D3-AC-56）。

宿主侧每发出一帧之前，先按线上字节解码（即 Agent 实际收到的对象，`decode_body` 同一解码上限），再按帧类型校验：

| 帧 | 校验 schema |
|---|---|
| CONFIG（reset 配置） | `bench/agent_config.v1` |
| OBS（观测与 inbox） | `bench/obs.v1` |
| ERROR（错误消息） | `bench/wire.v1#/$defs/Error` |
| CLOSE | `bench/wire.v1#/$defs/Close` |

OBS 另做两项字节断言：相机 `rgb`（`codec = jpeg`）只含 SOI、APP0（JFIF）、DQT、SOF0、DHT、DRI、SOS、RSTn、EOI 段；
LiDAR `tag` 的位 7:6 恒为 0。未知字段 = 白名单对象（`additionalProperties: false`）中 schema 未声明的键；`oneOf`
分支（传感器帧）按"除未知字段外错误最少"的分支计数。

校验分两级：先用由 schema 编译的判定函数（`compile_schema`，覆盖这些契约用到的关键字子集，语义与 Draft 2020-12 一致，未覆盖的
关键字在编译时报错）判定是否有效；只有判为无效的帧再交 `jsonschema` 给出权威错误列表与未知字段计数。

失败即关闭（缺省）：任一违规时该帧不发送，抛 `ObsSchemaViolation`（644 `OBS_SCHEMA_VIOLATION`），运行中止并由 M27 判平台失败。
计数与违规样例（只记字段路径与键名，不记取值）由 `summary()` 输出，写入运行元数据（`agents/obs_audit.json` 与
`manifest.json` 的 `obs_audit`）。

运行级审计经 `audit_scope()` 绑定到当前上下文：其间新建的 `SocketAgentHost` 共用同一个 `ObsAudit`（按机分项计数）。
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from functools import cache
from typing import Any
from urllib.parse import urldefrag, urljoin

from awr.agentsdk.wire import T, WireError, decode_body

__all__ = ["KINDS", "ObsAudit", "ObsSchemaViolation", "audit_scope", "compile_schema", "current_audit", "jpeg_segments_ok", "lidar_tag_ok"]

KINDS = {T.CONFIG: "config", T.OBS: "obs", T.ERROR: "error", T.CLOSE: "close"}
SAMPLES_MAX = 20
_JPEG_OK = frozenset({0xD8, 0xE0, 0xDB, 0xC0, 0xC4, 0xDD, 0xDA, 0xD9, *range(0xD0, 0xD8)})
_MARKER = re.compile(rb"\xff[^\x00\xd0-\xd7]")              # 熵编码段之后的下一个标记（跳过字节填充 FF00 与 RSTn）
_TAG_HIGH = bytes(range(0x40))                               # translate 删除表：删去位 7:6 为 0 的字节后应为空


class ObsSchemaViolation(Exception):
    """644 `OBS_SCHEMA_VIOLATION`：下发前白名单校验失败（`why` ∈ schema、jpeg_segment、lidar_tag）；只携带计数与路径，不携带取值。"""

    code = 644

    def __init__(self, why: str, kind: str, uav: str | None, paths: list[str]) -> None:
        super().__init__(f"644 {why} {kind} {uav or '-'} {', '.join(paths[:3])}")
        self.why, self.kind, self.uav, self.paths = why, kind, uav, paths


def _root(kind: str) -> dict:
    from awr.agentsdk.schemas import BENCH_ID

    if kind in ("config", "obs"):
        return {"$ref": BENCH_ID.format(name={"config": "agent_config", "obs": "obs"}[kind])}
    return {"$ref": BENCH_ID.format(name="wire") + "#/$defs/" + {"error": "Error", "close": "Close"}[kind]}


@cache
def _validator(kind: str):
    from jsonschema import Draft202012Validator

    from awr.sim.schemas import contracts_registry

    return Draft202012Validator(_root(kind), registry=contracts_registry())


@cache
def _fast(kind: str) -> Callable[[Any], bool]:
    from awr.sim.schemas import contracts_registry

    reg = contracts_registry()
    return compile_schema(_root(kind), "", lambda uri: reg.contents(uri))


# ---------------------------------------------------------------------------------------------------------------- 编译判定
_ANNOT = frozenset({"$schema", "$id", "$defs", "title", "description", "examples", "default", "$comment"})
_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda x: isinstance(x, dict),
    "array": lambda x: isinstance(x, list),
    "string": lambda x: isinstance(x, str),
    "boolean": lambda x: isinstance(x, bool),
    "null": lambda x: x is None,
    "number": lambda x: isinstance(x, int | float) and not isinstance(x, bool),
    "integer": lambda x: (isinstance(x, int) and not isinstance(x, bool)) or (isinstance(x, float) and x.is_integer()),
}


def _num(x) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def _eq(a, b) -> bool:
    """JSON 相等（与 jsonschema 一致：布尔与数值不相等，1 与 1.0 相等）。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b, strict=True))
    if _num(a) and _num(b):
        return a == b
    return type(a) is type(b) and a == b


def _object(req: tuple[str, ...], ps: dict[str, Callable[[Any], bool]], ap: Callable[[Any], bool] | None) -> Callable[[Any], bool]:
    def check(x) -> bool:
        if not isinstance(x, dict):
            return True
        for r in req:
            if r not in x:
                return False
        for p, val in x.items():
            f = ps.get(p, ap)
            if f is not None and not f(val):
                return False
        return True
    return check


def compile_schema(schema: Any, base: str, load: Callable[[str], Any]) -> Callable[[Any], bool]:
    """把 schema 编译为判定函数 instance → bool（Draft 2020-12 的子集：type、const、enum、properties、required、
    additionalProperties、items、minItems、maxItems、minimum、maximum、exclusiveMinimum、exclusiveMaximum、minLength、maxLength、
    pattern、oneOf、anyOf、allOf、$ref；x-* 与注解关键字忽略）。其他关键字在编译时抛 `NotImplementedError`，不静默放过。"""
    memo: dict[tuple[str, str], Callable[[Any], bool]] = {}

    def ref(uri: str) -> Callable[[Any], bool]:
        doc_uri, frag = urldefrag(uri)
        key = (doc_uri, frag)
        if key not in memo:
            slot: list[Callable[[Any], bool]] = []
            memo[key] = lambda x: slot[0](x)                 # 先占位，支持递归引用
            node = load(doc_uri)
            for part in [p for p in frag.split("/") if p]:
                node = node[part.replace("~1", "/").replace("~0", "~")]
            slot.append(comp(node, doc_uri))
        return memo[key]

    def comp(s: Any, base: str) -> Callable[[Any], bool]:
        if s is True:
            return lambda x: True
        if s is False:
            return lambda x: False
        base = urljoin(base, s["$id"]) if "$id" in s else base
        checks: list[Callable[[Any], bool]] = []
        for k, v in s.items():
            if k in _ANNOT or k.startswith("x-"):
                continue
            if k == "$ref":
                checks.append(ref(urljoin(base, v)))
            elif k == "type":
                ts = [_TYPES[t] for t in ([v] if isinstance(v, str) else v)]
                checks.append(lambda x, ts=ts: any(t(x) for t in ts))
            elif k == "const":
                checks.append(lambda x, c=v: _eq(x, c))
            elif k == "enum":
                checks.append(lambda x, es=tuple(v): any(_eq(x, e) for e in es))
            elif k == "minimum":
                checks.append(lambda x, m=v: not _num(x) or x >= m)
            elif k == "maximum":
                checks.append(lambda x, m=v: not _num(x) or x <= m)
            elif k == "exclusiveMinimum":
                checks.append(lambda x, m=v: not _num(x) or x > m)
            elif k == "exclusiveMaximum":
                checks.append(lambda x, m=v: not _num(x) or x < m)
            elif k == "minLength":
                checks.append(lambda x, m=v: not isinstance(x, str) or len(x) >= m)
            elif k == "maxLength":
                checks.append(lambda x, m=v: not isinstance(x, str) or len(x) <= m)
            elif k == "pattern":
                checks.append(lambda x, r=re.compile(v): not isinstance(x, str) or r.search(x) is not None)
            elif k == "minItems":
                checks.append(lambda x, m=v: not isinstance(x, list) or len(x) >= m)
            elif k == "maxItems":
                checks.append(lambda x, m=v: not isinstance(x, list) or len(x) <= m)
            elif k == "items":
                it = comp(v, base)
                checks.append(lambda x, it=it: not isinstance(x, list) or all(it(y) for y in x))
            elif k in ("required", "properties", "additionalProperties"):
                if k == "required" or (k == "additionalProperties" and "properties" in s):
                    continue                                 # 与 properties 合并为一个对象判定（先查 required，分支不符时尽早失败）
                ps = {p: comp(sub, base) for p, sub in s.get("properties", {}).items()}
                ap = comp(s["additionalProperties"], base) if "additionalProperties" in s else None
                checks.insert(0, _object(tuple(s.get("required", ())), ps, ap))
            elif k in ("oneOf", "anyOf", "allOf"):
                bs = [comp(b, base) for b in v]
                if k == "oneOf":
                    checks.append(lambda x, bs=bs: sum(1 for b in bs if b(x)) == 1)
                elif k == "anyOf":
                    checks.append(lambda x, bs=bs: any(b(x) for b in bs))
                else:
                    checks.append(lambda x, bs=bs: all(b(x) for b in bs))
            else:
                raise NotImplementedError(f"schema keyword {k!r}")
        if "required" in s and "properties" not in s and "additionalProperties" not in s:
            checks.insert(0, _object(tuple(s["required"]), {}, None))
        if "patternProperties" in s:
            raise NotImplementedError("patternProperties")
        if not checks:
            return lambda x: True
        if len(checks) == 1:
            return checks[0]
        return lambda x, cs=tuple(checks): all(c(x) for c in cs)

    return comp(schema, base)


def jpeg_segments_ok(b: bytes) -> bool:
    """JPEG 段白名单（M26-FR-026）：SOI 起、EOI 止，其间只允许 APP0（JFIF）、DQT、SOF0、DHT、DRI、SOS、RSTn。"""
    if b[:2] != b"\xff\xd8":
        return False
    i, n = 2, len(b)
    while i + 1 < n:
        if b[i] != 0xFF:
            return False
        m = b[i + 1]
        if m == 0xFF:                                        # 填充字节
            i += 1
            continue
        if m == 0xD9:
            return True
        if m not in _JPEG_OK:
            return False
        if 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            return False
        seg = int.from_bytes(b[i + 2:i + 4], "big")
        if seg < 2 or (m == 0xE0 and b[i + 4:i + 9] != b"JFIF\x00"):
            return False
        i += 2 + seg
        if m == 0xDA:
            hit = _MARKER.search(b, i)
            if hit is None:
                return False
            i = hit.start()
    return False


def lidar_tag_ok(tag: bytes) -> bool:
    """Livox `tag` 位 7:6 必须为 0（不携带识别物位）。"""
    return not bytes(tag).translate(None, _TAG_HIGH)


def _path(err) -> str:
    return "/".join(map(str, err.absolute_path))


def _extra(err) -> list[str]:
    props, pats = err.schema.get("properties", {}), err.schema.get("patternProperties", {})
    inst = err.instance if isinstance(err.instance, dict) else {}
    base = _path(err)
    return [f"{base}/{k}" if base else str(k) for k in inst if k not in props and not any(re.search(p, k) for p in pats)]


def _split(errors) -> tuple[list[str], list[str]]:
    """→ (未知字段路径, 其他错误的"路径:校验关键字")。`oneOf`/`anyOf` 失败时取"其他错误最少"的分支计其未知字段。"""
    unknown: list[str] = []
    other: list[str] = []
    for e in errors:
        if e.validator == "additionalProperties" and e.validator_value is False:
            unknown += _extra(e)
        elif e.validator in ("oneOf", "anyOf") and e.context:
            branches: dict[int, list] = {}
            for c in e.context:
                branches.setdefault(int(c.relative_schema_path[0]), []).append(c)
            best = min((_split(v) for v in branches.values()), key=lambda r: (len(r[1]), len(r[0])))
            unknown += best[0]
            other += best[1] or [f"{_path(e)}:{e.validator}"]
        else:
            other.append(f"{_path(e)}:{e.validator}")
    return unknown, other


def _walk_sensors(obs: dict) -> tuple[list[str], list[str]]:
    """→ (JPEG 段违规路径, LiDAR tag 违规路径)。"""
    bad_jpeg, bad_tag = [], []
    sensors = obs.get("sensors") if isinstance(obs, dict) else None
    for sid, frames in (sensors or {}).items():
        if sid == "detections" or not isinstance(frames, list):
            continue
        for i, f in enumerate(frames):
            if not isinstance(f, dict):
                continue
            rgb = f.get("rgb")
            if isinstance(rgb, dict) and rgb.get("codec") == "jpeg" and isinstance(rgb.get("data"), bytes | bytearray) \
                    and not jpeg_segments_ok(rgb["data"]):
                bad_jpeg.append(f"obs/sensors/{sid}/{i}/rgb/data")
            tag = f.get("tag")
            if isinstance(tag, bytes | bytearray) and not lidar_tag_ok(tag):
                bad_tag.append(f"obs/sensors/{sid}/{i}/tag")
    return bad_jpeg, bad_tag


def _zero() -> dict:
    return {"checked": 0, "valid": 0, "violations": 0, "unknown_fields": 0}


class ObsAudit:
    """一个运行的下发消息审计；线程安全（多机宿主可在不同线程发送）。"""

    def __init__(self, *, fail_closed: bool = True) -> None:
        self.fail_closed = fail_closed
        self.by_kind = {k: _zero() for k in KINDS.values()}
        self.by_uav: dict[str, dict] = {}
        self.jpeg_segment_violations = 0
        self.lidar_tag_violations = 0
        self.decode_violations = 0
        self.samples: list[dict] = []
        self._obs_ms: list[float] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 校验
    def check_body(self, t: int, body: bytes, *, uav: str | None = None) -> None:
        """按线上字节（msgpack 帧体）解码后校验；违规且失败即关闭时抛 `ObsSchemaViolation`。"""
        kind = KINDS.get(t)
        if kind is None:
            return
        t0 = time.perf_counter()
        try:
            doc = decode_body(t, body)
        except WireError:
            self._record(kind, uav, ["<decode>"], other=1, why="decode", k=None)
            self._raise("schema", kind, uav, ["<decode>"])
            return
        self.check(t, doc, uav=uav, _t0=t0)

    def check(self, t: int, doc, *, uav: str | None = None, _t0: float | None = None) -> None:
        kind = KINDS.get(t)
        if kind is None:
            return
        t0 = time.perf_counter() if _t0 is None else _t0
        unknown, other = ([], []) if _fast(kind)(doc) else _split(_validator(kind).iter_errors(doc))
        bad_jpeg, bad_tag = _walk_sensors(doc.get("obs")) if kind == "obs" and isinstance(doc, dict) else ([], [])
        k = doc.get("obs", {}).get("k") if kind == "obs" and isinstance(doc, dict) and isinstance(doc.get("obs"), dict) else None
        dt_ms = (time.perf_counter() - t0) * 1e3
        bad = unknown or other or bad_jpeg or bad_tag
        with self._lock:
            if kind == "obs":
                self._obs_ms.append(dt_ms)
            self.jpeg_segment_violations += len(bad_jpeg)
            self.lidar_tag_violations += len(bad_tag)
        why = "unknown_field" if unknown else "schema" if other else "jpeg_segment" if bad_jpeg else "lidar_tag" if bad_tag else None
        paths = unknown + other + bad_jpeg + bad_tag
        self._record(kind, uav, paths, other=len(other), unknown=len(unknown), why=why, k=k, valid=not bad)
        if bad:
            self._raise("schema" if (unknown or other) else "jpeg_segment" if bad_jpeg else "lidar_tag", kind, uav, paths)

    def _record(self, kind: str, uav: str | None, paths: list[str], *, other: int, why: str | None, k, unknown: int = 0,
                valid: bool = False) -> None:
        with self._lock:
            for c in (self.by_kind[kind], self.by_uav.setdefault(uav or "-", {x: _zero() for x in KINDS.values()})[kind]):
                c["checked"] += 1
                c["valid"] += int(valid)
                c["violations"] += int(not valid)
                c["unknown_fields"] += unknown
            if why == "decode":
                self.decode_violations += 1
            if not valid and len(self.samples) < SAMPLES_MAX:
                self.samples.append({"kind": kind, "uav": uav, "k": k, "why": why, "other_errors": other, "paths": paths[:8]})

    def _raise(self, why: str, kind: str, uav: str | None, paths: list[str]) -> None:
        if self.fail_closed:
            raise ObsSchemaViolation(why, kind, uav, paths)

    # ------------------------------------------------------------ 输出
    @property
    def checked(self) -> int:
        return sum(c["checked"] for c in self.by_kind.values())

    @property
    def violations(self) -> int:
        return sum(c["violations"] for c in self.by_kind.values())

    @property
    def unknown_fields(self) -> int:
        return sum(c["unknown_fields"] for c in self.by_kind.values())

    @property
    def passed(self) -> bool:
        """全部下发消息通过校验：违规 0 且未知字段 0。"""
        return self.violations == 0 and self.unknown_fields == 0

    def summary(self) -> dict:
        with self._lock:
            ms = sorted(self._obs_ms)
            timing = {"obs_check_ms_mean": round(sum(ms) / len(ms), 4) if ms else None,
                      "obs_check_ms_p99": round(ms[min(len(ms) - 1, int(0.99 * len(ms)))], 4) if ms else None,
                      "obs_check_ms_max": round(ms[-1], 4) if ms else None}
            return {"contracts": {"config": "drone4d.bench.agent_config.v1", "obs": "drone4d.bench.obs.v1", "error": "drone4d.bench.wire.v1#Error",
                                  "close": "drone4d.bench.wire.v1#Close"},
                    "schema_checked": self.checked, "schema_valid": self.checked - self.violations, "schema_violations": self.violations,
                    "unknown_fields": self.unknown_fields, "jpeg_segment_violations": self.jpeg_segment_violations,
                    "lidar_tag_violations": self.lidar_tag_violations, "decode_violations": self.decode_violations,
                    "passed": self.passed, "fail_closed": self.fail_closed,
                    "by_kind": {k: dict(v) for k, v in self.by_kind.items()},
                    "by_uav": {u: {k: dict(v) for k, v in d.items()} for u, d in sorted(self.by_uav.items())},
                    "timing": timing, "samples": list(self.samples)}


_CURRENT: ContextVar[ObsAudit | None] = ContextVar("awr_obs_audit", default=None)


def current_audit() -> ObsAudit | None:
    return _CURRENT.get()


@contextlib.contextmanager
def audit_scope(audit: ObsAudit | None = None) -> Iterator[ObsAudit]:
    """在此上下文中新建的 `SocketAgentHost` 共用 `audit`（运行级计数）。"""
    a = audit if audit is not None else ObsAudit()
    tok = _CURRENT.set(a)
    try:
        yield a
    finally:
        _CURRENT.reset(tok)
