"""Shared user-facing presentation helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


EASTERN_TIME = ZoneInfo("America/New_York")


def format_eastern_time(value: datetime | str) -> str:
    """Render an aware instant in the dashboard's documented Eastern timezone."""

    if isinstance(value, str):
        if not value.endswith("Z"):
            raise ValueError("Stored execution timestamps must be UTC RFC3339 values ending in Z.")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("The execution timestamp is invalid.") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError("Execution timestamps must be aware datetimes or stored UTC strings.")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Execution timestamps must include a timezone.")
    return parsed.astimezone(EASTERN_TIME).strftime(
        "%Y-%m-%d %H:%M %Z (America/New_York)"
    )
