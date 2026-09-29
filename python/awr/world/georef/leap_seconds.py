"""Leap second table (TAI - UTC) from IERS Bulletin C (M02 §9.1; source: IERS leap-seconds.list).

Each entry is (UTC POSIX second at which the offset takes effect, TAI - UTC in seconds): 28 entries, 1972-01-01
start value 10 s plus 27 insertions. Update this table (and LEAP_TABLE_VALID_UNTIL_UNIX_S) when IERS announces a
change; expired tables only warn (M02 §7.9 degradation rule 2).
"""

from __future__ import annotations

LEAP_TABLE: tuple[tuple[int, int], ...] = (
    (63072000, 10),  # 1972-01-01
    (78796800, 11),  # 1972-07-01
    (94694400, 12),  # 1973-01-01
    (126230400, 13),  # 1974-01-01
    (157766400, 14),  # 1975-01-01
    (189302400, 15),  # 1976-01-01
    (220924800, 16),  # 1977-01-01
    (252460800, 17),  # 1978-01-01
    (283996800, 18),  # 1979-01-01
    (315532800, 19),  # 1980-01-01
    (362793600, 20),  # 1981-07-01
    (394329600, 21),  # 1982-07-01
    (425865600, 22),  # 1983-07-01
    (489024000, 23),  # 1985-07-01
    (567993600, 24),  # 1988-01-01
    (631152000, 25),  # 1990-01-01
    (662688000, 26),  # 1991-01-01
    (709948800, 27),  # 1992-07-01
    (741484800, 28),  # 1993-07-01
    (773020800, 29),  # 1994-07-01
    (820454400, 30),  # 1996-01-01
    (867715200, 31),  # 1997-07-01
    (915148800, 32),  # 1999-01-01
    (1136073600, 33),  # 2006-01-01
    (1230768000, 34),  # 2009-01-01
    (1341100800, 35),  # 2012-07-01
    (1435708800, 36),  # 2015-07-01
    (1483228800, 37),  # 2017-01-01
)

# Bulletin C 71 (2026-01): no leap second at the end of June 2026; table valid until 2026-12-28.
LEAP_TABLE_VALID_UNTIL_UNIX_S = 1798416000
