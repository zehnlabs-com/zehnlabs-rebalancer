"""User-facing timestamp presentation tests."""

from datetime import datetime, timezone

import pytest

from app.presentation import format_eastern_time


def test_eastern_time_distinguishes_winter_and_summer() -> None:
    assert format_eastern_time("2026-11-27T14:35:00.000000Z") == (
        "2026-11-27 09:35 EST (America/New_York)"
    )
    assert format_eastern_time(datetime(2026, 7, 1, 13, 35, tzinfo=timezone.utc)) == (
        "2026-07-01 09:35 EDT (America/New_York)"
    )


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 11, 27, 9, 35), "2026-11-27T14:35:00", "not-a-time"],
)
def test_eastern_time_rejects_naive_or_invalid_values(value: datetime | str) -> None:
    with pytest.raises(ValueError):
        format_eastern_time(value)
