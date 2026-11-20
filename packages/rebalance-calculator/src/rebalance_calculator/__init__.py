from .calculator import TradeCalculator
from .models import TradeCalculationResult
from broker_connector_base import Trade, AllocationItem, AccountConfig

# PRD §7 (new)
from .targets import validate_and_scale, apply_replacements, InvalidTargetsError
from .replacement_sets import REPLACEMENT_SETS, ReplacementRule, validate_replacement_set
from .planner import round_quantity, limit_price, plan_sells, plan_buys
from .models import Note, PlannedOrder, RawAllocation, Targets, SellPlan, BuyPlan

__version__ = "1.0.0"

__all__ = [
    "TradeCalculator",
    "TradeCalculationResult",
    "Trade",
    "AllocationItem",
    "AccountConfig",
    "validate_and_scale",
    "apply_replacements",
    "InvalidTargetsError",
    "REPLACEMENT_SETS",
    "ReplacementRule",
    "validate_replacement_set",
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
    "__version__",
]