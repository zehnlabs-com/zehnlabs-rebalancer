"""Target validation/scaling and replacement application (PRD §7.1–7.2).

Pure functions: no I/O, no clock, no logging, no config lookups.
"""

from __future__ import annotations

import re
import math
from typing import Dict, List, Sequence, Tuple

from .models import Note, RawAllocation, Targets
from .replacement_sets import ReplacementRule

EPS = 1e-9

_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")

# En-dash as used in the PRD message.
_INVALID_TOTAL_MSG = "Strategy weights total {pct:.2f}%, outside 99%\u2013101%."


class InvalidTargetsError(ValueError):
    """The strategy's raw allocations fail PRD TGT-1 validation."""


def validate_and_scale(raw: Sequence[RawAllocation]) -> Targets:
    """Normalise, validate and scale raw API weights (TGT-1..4).

    Runs on the raw API weights *before* replacements (TGT-5). An empty list is
    valid and means "hold nothing" (TGT-2). Weight-0 entries are dropped from
    the final targets (TGT-4).
    """
    normalized: List[Tuple[str, float]] = []
    seen: set[str] = set()
    total = 0.0
    for alloc in raw:
        symbol = alloc.symbol
        weight = alloc.weight
        if symbol is None or (isinstance(symbol, str) and symbol.strip() == ""):
            raise InvalidTargetsError(
                "Strategy lists an allocation with a missing or empty symbol."
            )
        if not isinstance(symbol, str):
            raise InvalidTargetsError("Strategy contains an invalid symbol.")
        s = symbol.strip().upper()
        if not _SYMBOL_RE.fullmatch(s):
            raise InvalidTargetsError("Strategy contains an invalid symbol.")
        if s in seen:
            raise InvalidTargetsError("Strategy contains a duplicate symbol.")
        if weight is None or isinstance(weight, bool) \
                or not isinstance(weight, (int, float)) or not _is_finite(weight) \
                or weight < 0:
            raise InvalidTargetsError(
                "Strategy contains a missing, non-finite, or negative weight."
            )
        seen.add(s)
        normalized.append((s, float(weight)))
        total += float(weight)

    if not normalized:
        return Targets(
            weights={},
            raw={},
            scaled={},
            notes=(Note("info", "Strategy has no allocations: selling all positions."),),
        )

    if abs(total - 1.0) > 0.01:
        raise InvalidTargetsError(_INVALID_TOTAL_MSG.format(pct=total * 100.0))

    raw_map = dict(normalized)
    scaled = {s: w / total for s, w in normalized}
    # TGT-4: weight-0 entries are removed from the targets.
    weights = {s: w for s, w in scaled.items() if w != 0}
    return Targets(weights=weights, raw=raw_map, scaled=scaled)


def _is_finite(value: float) -> bool:
    return not math.isnan(value) and not math.isinf(value)


def apply_replacements(
    weights: Dict[str, float], rules: Sequence[ReplacementRule]
) -> Tuple[Dict[str, float], List[Note]]:
    """Replace restricted exposure per REP-3. Input weights already sum to 1.0."""
    rule_by_source = {r.source: r for r in rules}
    protected: Dict[str, float] = {}
    ordinary: Dict[str, float] = {}
    for symbol, w in weights.items():
        rule = rule_by_source.get(symbol)
        if rule is not None:
            protected[rule.target] = protected.get(rule.target, 0.0) + w * rule.scale
        else:
            ordinary[symbol] = ordinary.get(symbol, 0.0) + w

    p = sum(protected.values())
    o = sum(ordinary.values())
    notes: List[Note] = []

    if p >= 1:  # replacements alone fill the account
        f = 1.0 / p
        result = {s: w * f for s, w in protected.items()}
        targets_txt = ", ".join(sorted(result))
        notes.append(Note(
            "warning",
            f"Replacement exposure is {p:.1%}; holding {targets_txt} only, at 100%.",
        ))
    elif p + o > 1 + EPS:  # shrink the ordinary symbols to make room
        f = (1.0 - p) / o
        result = dict(protected)
        for s, w in ordinary.items():
            result[s] = result.get(s, 0.0) + w * f
        notes.append(Note(
            "info",
            f"Replacements need {p + o - 1:.1%} extra; other symbols reduced by {(1 - f):.1%}.",
        ))
    else:
        result = dict(protected)
        for s, w in ordinary.items():
            result[s] = result.get(s, 0.0) + w

    result = {s: w for s, w in result.items() if w != 0}
    assert abs(sum(result.values()) - 1.0) < 1e-9
    return result, notes
