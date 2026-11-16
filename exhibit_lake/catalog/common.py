"""无依赖的标识、日期与时间校验，供各目录模块复用。"""
from __future__ import annotations

from datetime import date, datetime, timezone

from exhibit_lake.errors import InvalidInput


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or not all(
            ch.isalnum() or ch in "_-:." for ch in value):
        raise InvalidInput(f"{label} must be a nonempty identifier of at most 80 characters")
    return value


def iso_day(value: str, label: str) -> str:
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise InvalidInput(f"{label} must be an ISO date") from None
    return value


def iso_time(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError()
    except (TypeError, ValueError):
        raise InvalidInput(f"{label} must be an offset-aware ISO timestamp") from None
    return parsed.astimezone(timezone.utc).isoformat()

