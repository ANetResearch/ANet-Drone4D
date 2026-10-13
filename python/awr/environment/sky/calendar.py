"""仿真日历（AWR-04 §6.5、ADR-102；AWR-17 §17.6.9 `env/calendar`）。

会话配置 `calendar{start_utc, tz}`：`t_cal = start_utc + t_sim`。内部只保存 `cal0_unix_ms`（t_sim = 0 对应的日历时刻），
`env/calendar{start_utc, tz, jump = true}` 在生效网格时刻 `t_apply` 处把日历设为 `start_utc`（`cal0 = start_utc − t_apply`），
`jump = false` 时按 `t_cal = start_utc + t_sim` 解释。快进时日历随仿真时间推进（ADR-045 规则 ①）。线上 `start_utc` 写 cal0
（与 §6.5 的定义 `t_cal = start_utc + t_sim` 自洽），`t_cal_unix_ns` 为本条关键帧 `t_ns` 处的日历时刻（十进制字符串）。

`start_utc`：ISO 8601（`YYYY-MM-DDTHH:MM[:SS[.fff]]`，分隔符可为空格，后缀 `Z` 或 `±HH:MM`；无后缀按 UTC），年份 1901–2099
（NOAA 算法的有效区间）；`tz`：IANA 名（zoneinfo 可解析），只用于界面本地时刻显示，太阳位置与热背景用经度求真太阳时。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["WORLD_TZ", "Calendar", "CalendarError", "calendar_for_local", "format_utc", "parse_utc_ms", "valid_tz"]

YEAR_MIN, YEAR_MAX = 1901, 2099
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:?\d{2})?$")

# 世界锚点的缺省时区（AWR-17 §17.4.2 `calendar` 缺省值；synthcity 的示意锚点 30°N、120°E，ADR-077）
WORLD_TZ: dict[str, str] = {
    "synthcity": "Asia/Shanghai", "shanghai": "Asia/Shanghai", "shenzhen": "Asia/Shanghai", "suzhou": "Asia/Shanghai",
    "sanfrancisco": "America/Los_Angeles", "newyork": "America/New_York", "chicago": "America/Chicago",
}


class CalendarError(ValueError):
    def __init__(self, field: str, why: str) -> None:
        super().__init__(f"{field}: {why}")
        self.detail = {"field": field, "why": why}


@lru_cache(maxsize=64)
def valid_tz(tz: str) -> bool:
    if not isinstance(tz, str) or not tz or len(tz) > 64:
        return False
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def parse_utc_ms(s: str) -> int:
    """ISO 8601 -> UNIX 毫秒（整数，截断到毫秒）；格式或年份越界抛 CalendarError。"""
    if not isinstance(s, str):
        raise CalendarError("start_utc", "NOT_A_STRING")
    m = _ISO.match(s.strip())
    if m is None:
        raise CalendarError("start_utc", "NOT_ISO8601")
    y, mo, d, hh, mm = (int(m.group(i)) for i in range(1, 6))
    ss = int(m.group(6) or 0)
    frac = (m.group(7) or "").ljust(3, "0")[:3]
    if not YEAR_MIN <= y <= YEAR_MAX:
        raise CalendarError("start_utc", "YEAR_OUT_OF_RANGE")
    try:
        dt = datetime(y, mo, d, hh, mm, ss, tzinfo=UTC)
    except ValueError as ex:
        raise CalendarError("start_utc", "INVALID_DATE") from ex
    off = m.group(8)
    if off and off != "Z":
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        oh, om = int(digits[:2]), int(digits[2:])
        if oh > 18 or om > 59:
            raise CalendarError("start_utc", "INVALID_OFFSET")
        dt = dt - sign * timedelta(hours=oh, minutes=om)
    return int(dt.timestamp()) * 1000 + int(frac)


def format_utc(unix_ms: int) -> str:
    """UNIX 毫秒 -> `YYYY-MM-DDTHH:MM:SSZ`（毫秒非零时带 `.fff`）。"""
    ms = int(unix_ms)
    sec, frac = divmod(ms, 1000)
    dt = datetime.fromtimestamp(sec, tz=UTC)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{frac:03d}Z" if frac else f"{base}Z"


@dataclass(frozen=True, slots=True)
class Calendar:
    """仿真日历：`t_cal_ms(t_sim_ns) = cal0_unix_ms + t_sim_ns / 1e6`。"""

    cal0_unix_ms: int
    tz: str

    @classmethod
    def from_request(cls, start_utc: str, tz: str, *, jump: bool = True, t_apply_ns: int = 0) -> Calendar:
        start = parse_utc_ms(start_utc)
        if not valid_tz(tz):
            raise CalendarError("tz", "UNKNOWN_TIME_ZONE")
        return cls(start - (int(t_apply_ns) // 1_000_000 if jump else 0), str(tz))

    def t_cal_ms(self, t_sim_ns: int) -> float:
        return self.cal0_unix_ms + int(t_sim_ns) / 1e6

    def t_cal_unix_ns(self, t_sim_ns: int) -> int:
        return self.cal0_unix_ms * 1_000_000 + int(t_sim_ns)

    def start_utc(self) -> str:
        return format_utc(self.cal0_unix_ms)

    def to_wire(self, t_ns: int) -> dict[str, str]:
        return {"start_utc": self.start_utc(), "tz": self.tz, "t_cal_unix_ns": str(self.t_cal_unix_ns(t_ns))}

    def to_config(self, t_ns: int = 0) -> dict[str, str]:
        """会话快照形态 `{start_utc, tz}`：start_utc 取 t_ns 处的日历时刻（恢复时以该时刻为新会话的 t_sim = 0）。"""
        return {"start_utc": format_utc(self.cal0_unix_ms + int(t_ns) // 1_000_000), "tz": self.tz}


def calendar_for_local(year: int, month: int, day: int, hour: int, minute: int, tz: str) -> dict[str, str]:
    """世界时区的本地时刻 -> `{start_utc, tz}`（例如模板缺省"当日 10:00"）。"""
    if not valid_tz(tz):
        raise CalendarError("tz", "UNKNOWN_TIME_ZONE")
    dt = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))
    return {"start_utc": format_utc(int(dt.timestamp() * 1000)), "tz": tz}
