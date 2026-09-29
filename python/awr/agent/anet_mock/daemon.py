"""MockDaemon（每 AID 一个；M14 §6.12.1；M14-FR-045；语义来自 ANet daemon `capability.go`、`delegation.go`、`ledger.go`）。

- provider registry（精确 + 父级回退）；委派解析失败一律 UNAVAILABLE（不配置 auto-reply）；
- 长任务并发 ≤ 4（`maxConcurrentLongCalls`），满额立即 UNAVAILABLE；
- 结果签回执 `Receipt{ix, requester, provider, request_cid, result_cid = sha256(规范 JSON(deliverable)), completed_at_ns, sig}`，
  字段对应 `ANetCore@v0.14.0/evidence.Receipt`；Mock 签名为 `HMAC-SHA256(K_agent(provider), 规范 JSON(回执去掉 sig))`；
- 账本即该 AID 的证据链（`EvidenceLog`，提供方视角）。
- `verify_receipt()`：请求方按 `delegation.VerifyResult` 的 7 项绑定校验，外加 Mock 的 request_cid 一致检查。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..runtime.evidence import canonical_json, sha256_cid
from ..runtime.provider import Registry
from . import identity as ID

__all__ = ["MAX_LONG_CALLS", "MockDaemon", "sign_receipt", "verify_receipt"]

MAX_LONG_CALLS = 4


def _receipt_body(r: Mapping[str, Any]) -> bytes:
    return canonical_json({k: v for k, v in r.items() if k != "sig"})


def sign_receipt(key: bytes, receipt: Mapping[str, Any]) -> dict[str, Any]:
    r = dict(receipt)
    r["sig"] = ID.sign(key, _receipt_body(r))
    return r


def verify_receipt(receipt: Mapping[str, Any] | None, deliverable: Mapping[str, Any], *, ix: str, requester: str, provider: str,
                   request_cid: str, key_of: Callable[[str], bytes | None]) -> tuple[bool, str]:
    """7 项绑定校验 + request_cid 附加检查；返回 (通过, 失败项)。"""
    if not receipt:
        return False, "1_no_receipt"
    key = key_of(str(receipt.get("provider", "")))
    if key is None:
        return False, "2_no_key"
    if not ID.verify(key, _receipt_body(receipt), str(receipt.get("sig", ""))):
        return False, "3_bad_sig"
    if receipt.get("provider") != provider:
        return False, "4_provider"
    if receipt.get("requester") != requester:
        return False, "5_requester"
    if receipt.get("ix") != ix:
        return False, "6_ix"
    if receipt.get("result_cid") != sha256_cid(dict(deliverable)):
        return False, "7_result_cid"
    if receipt.get("request_cid") != request_cid:
        return False, "8_request_cid"
    return True, ""


class MockDaemon:
    def __init__(self, aid: str, agent_no: int, provider: Any, *, key: bytes, ledger: Any = None) -> None:
        self.aid = aid
        self.agent_no = int(agent_no)
        self.provider = provider
        self.key = key
        self.ledger = ledger
        self.registry = Registry()
        self.registry.register(getattr(provider, "id", aid), provider.capabilities())
        self.long_calls: set[str] = set()
        self.alive = True
        self.stats = {"received": 0, "unresolved": 0, "busy": 0}

    def resolve(self, capability: str) -> str | None:
        return self.registry.resolve(capability)

    def long_full(self) -> bool:
        return len(self.long_calls) >= MAX_LONG_CALLS

    def receipt(self, *, ix: str, requester: str, request_cid: str, deliverable: Mapping[str, Any], t_ns: int) -> dict[str, Any]:
        return sign_receipt(self.key, {"ix": ix, "requester": requester, "provider": self.aid, "request_cid": request_cid,
                                       "result_cid": sha256_cid(dict(deliverable)), "completed_at_ns": int(t_ns)})

    def append(self, type_: str, payload: Mapping[str, Any]) -> None:
        if self.ledger is not None:
            self.ledger.append(type_, payload)
