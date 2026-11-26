# PRD §7 (new)
from .targets import validate_and_scale, apply_replacements, InvalidTargetsError
from .replacement_sets import REPLACEMENT_SETS, ReplacementRule, validate_replacement_set
from .planner import (
    FRACTIONAL_QUANTITY_UNIT,
    round_quantity,
    limit_price,
    plan_sells,
    plan_buys,
)
from .models import Note, PlannedOrder, RawAllocation, Targets, SellPlan, BuyPlan
from .optimizer import OptimizationError

__version__ = "1.0.0"

__all__ = [
    "validate_and_scale",
    "apply_replacements",
    "InvalidTargetsError",
    "REPLACEMENT_SETS",
    "ReplacementRule",
    "validate_replacement_set",
    "FRACTIONAL_QUANTITY_UNIT",
    "round_quantity",
    "limit_price",
    "plan_sells",
    "plan_buys",
    "Note",
    "PlannedOrder",
    "RawAllocation",
    "Targets",
    "SellPlan",
    "BuyPlan",
    "OptimizationError",
    "__version__",
]
