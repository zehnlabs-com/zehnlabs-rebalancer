"""MarketClock (PRD §10.1)."""

from __future__ import annotations

import datetime as dt
import logging
from datetime import datetime, time
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


class MarketClock:
    """NY Stock Exchange clock with an injectable `now()`.

    CLK-1: built on `exchange_calendars.get_calendar("XNYS")`; half-day closes are
    included because we compare against the calendar's real session times.
    """

    def __init__(
        self,
        market_open_time: str,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self._calendar = xcals.get_calendar("XNYS")
        hour, minute = market_open_time.split(":")
        self._open_time = time(int(hour), int(minute))
        self._now = now or (lambda: datetime.now(_ET))

    def now(self) -> datetime:
        return self._now()

    def _aware(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=_ET)
        return value.astimezone(dt.timezone.utc)

    def is_open(self, now: Optional[datetime] = None) -> bool:
        n = self._aware(now or self.now())
        date = n.date()
        if not self._calendar.is_session(date):
            return False
        open_dt = self._calendar.session_open(date)
        close_dt = self._calendar.session_close(date)
        return open_dt <= n < close_dt

    def is_trading_day(self, d: dt.date | datetime) -> bool:
        date = d.date() if isinstance(d, datetime) else d
        return self._calendar.is_session(date)

    def next_scheduled_run(self, now: Optional[datetime] = None) -> datetime:
        n = self._aware(now or self.now())
        session = self._calendar.date_to_session(n.date(), direction="next")
        target = datetime.combine(session, self._open_time, tzinfo=_ET)
        while target <= n:
            session = self._calendar.date_to_session(
                session + dt.timedelta(days=1), direction="next"
            )
            target = datetime.combine(session, self._open_time, tzinfo=_ET)
        return target