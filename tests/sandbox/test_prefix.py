"""地址前缀与聚合前缀（M17-FR-012、AC-009 的纯逻辑部分；17 §17.3.4）。"""

from __future__ import annotations

from awr.sandbox.prefix import hash_prefix, prefixes


def test_ipv4_and_mapped() -> None:
    assert prefixes("198.51.100.7") == ("198.51.100.7/32", "198.51.100.0/24")
    assert prefixes("::ffff:198.51.100.7") == ("198.51.100.7/32", "198.51.100.0/24")
    assert prefixes("198.51.100.7/32") == ("198.51.100.7/32", "198.51.100.0/24")


def test_ipv6_rotation_inside_64_counts_once() -> None:
    a = prefixes("2001:db8:1:2:aaaa::1")
    b = prefixes("2001:db8:1:2:ffff:1:2:3")
    c = prefixes("2001:db8:1:3::1")
    assert a == b == ("2001:db8:1:2::/64", "2001:db8:1::/48")
    assert c[0] != a[0] and c[1] == a[1]
    assert prefixes("2001:db8:1:2::/64") == a


def test_unknown_and_hash() -> None:
    assert prefixes(None) == ("unknown", "unknown")
    assert prefixes("not-an-ip") == ("not-an-ip", "not-an-ip")
    k = b"k" * 32
    h = hash_prefix(k, "198.51.100.7/32")
    assert len(h) == 32 and h == hash_prefix(k, "198.51.100.7/32") and h != hash_prefix(b"x" * 32, "198.51.100.7/32")
    assert "198" not in h
