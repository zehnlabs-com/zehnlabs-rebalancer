"""Built-in replacement sets (PRD §7.2).

Replacement rules move exposure from an instrument the account cannot hold to
one it can. The sets live in code — `replacement-sets.yaml` is gone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class ReplacementRule:
    source: str
    target: str
    scale: float


REPLACEMENT_SETS: Dict[str, Tuple[ReplacementRule, ...]] = {
    "ira": (
        ReplacementRule("DBC", "PDBC", 1.0),
        ReplacementRule("UUP", "USDU", 1.0),
        ReplacementRule("VIXY", "VXX", 1.0),
        ReplacementRule("VIXM", "VXZ", 1.0),
        ReplacementRule("UVXY", "VXX", 1.5),
        ReplacementRule("SVXY", "USMV", 1.0),
    ),
}


def _is_upper_symbol(symbol: str) -> bool:
    return bool(symbol) and symbol == symbol.upper()


def validate_replacement_set(name: str, rules: Tuple[ReplacementRule, ...]) -> None:
    """Validate a replacement set (REP-2). Raises ValueError on the first problem."""
    sources: set[str] = set()
    targets: set[str] = set()
    for rule in rules:
        for symbol in (rule.source, rule.target):
            if not _is_upper_symbol(symbol):
                raise ValueError(
                    f"replacement set '{name}': a source/target is empty or not upper case"
                )
        if not math.isfinite(rule.scale) or rule.scale < 1.0:
            raise ValueError(
                f"replacement set '{name}': rule {rule.source}->{rule.target} scale must be >= 1.0"
            )
        if rule.source == rule.target:
            raise ValueError(
                f"replacement set '{name}': source equals target ({rule.source})"
            )
        if rule.source in sources:
            raise ValueError(
                f"replacement set '{name}': source {rule.source} appears more than once"
            )
        if rule.target in sources or rule.source in targets:
            raise ValueError(
                f"replacement set '{name}': a symbol is both a source and a target (no chains)"
            )
        sources.add(rule.source)
        targets.add(rule.target)


for _name, _rules in REPLACEMENT_SETS.items():
    validate_replacement_set(_name, _rules)