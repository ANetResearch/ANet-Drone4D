"""M14-AC-005：伪 AID（§6.3）。"""

from __future__ import annotations

import string

from awr.agent.anet_mock import identity as ID


def test_aid_deterministic_and_shape() -> None:
    a = ID.aid("newyork", "p600-b1")
    assert a == ID.aid("newyork", "p600-b1")
    assert len(a) == 59 and a.startswith("bafyrei")
    assert set(a[1:]) <= set(string.ascii_lowercase + "234567")
    assert ID.decode_aid(a)[:4] == bytes((0x01, 0x71, 0x12, 0x20))
    assert ID.is_aid(a)


def test_aid_short_golden() -> None:
    s = ID.aid_short(ID.aid("newyork", "p600-b1"))
    assert s == "bafyrei…cy7bou"
    assert len(s) == 14


def test_no_collision_1000() -> None:
    aids = {ID.aid("newyork", f"uav{i:04d}") for i in range(1000)}
    assert len(aids) == 1000
    assert ID.coordinator_aid("newyork") == ID.aid("newyork", "gcs")
    assert ID.aid("shenzhen", "p600-b1") != ID.aid("newyork", "p600-b1")


def test_agent_key_and_sign() -> None:
    a = ID.aid("newyork", "p600-b1")
    k1 = ID.agent_key(b"secret", "run-1", a)
    assert k1 == ID.agent_key(b"secret", "run-1", a)
    assert k1 != ID.agent_key(b"secret", "run-2", a)
    sig = ID.sign(k1, "sha256:abc")
    assert ID.verify(k1, "sha256:abc", sig)
    assert not ID.verify(k1, "sha256:abd", sig)
