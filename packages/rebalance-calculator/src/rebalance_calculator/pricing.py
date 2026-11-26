"""Shared broker price calculations."""

from decimal import ROUND_HALF_UP, Decimal


def limit_price(reference: float, slippage_pct: float) -> float:
    """Return a buy limit using the broker's existing half-up tick rule."""
    raw = reference * (1.0 + slippage_pct / 100.0)
    quantum = Decimal("0.01") if reference >= 1.00 else Decimal("0.0001")
    return float(Decimal(str(raw)).quantize(quantum, rounding=ROUND_HALF_UP))
