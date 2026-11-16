"""本地日历日与 UTC 生效时刻之间的换算。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from exhibit_lake.errors import InvalidInput


def zone(timezone_name) -> ZoneInfo:
    if isinstance(timezone_name, ZoneInfo):
        return timezone_name
    try:
        return ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError):
        raise InvalidInput("unknown venue timezone") from None


def next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def day_start_utc(day: str, timezone_name: str) -> str:
    """该场馆时区中某本地日的零点，规范化为 UTC ISO 时间。"""
    parsed = date.fromisoformat(day)
    start = datetime.combine(parsed, time.min, tzinfo=zone(timezone_name))
    return start.astimezone(timezone.utc).isoformat()


def day_end_utc(day: str, timezone_name: str) -> str:
    """该本地日的开区间终点，即次日零点的 UTC 时刻。"""
    return day_start_utc(next_day(day), timezone_name)


def local_day(instant: str, timezone_name: str) -> str:
    return datetime.fromisoformat(instant).astimezone(zone(timezone_name)).date().isoformat()


def instant_day(instant: str, timezone_name: str) -> str:
    """开区间终点之前一个微秒所在本地日，用于展示区间最后一天。"""
    moment = datetime.fromisoformat(instant) - timedelta(microseconds=1)
    return moment.astimezone(zone(timezone_name)).date().isoformat()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
