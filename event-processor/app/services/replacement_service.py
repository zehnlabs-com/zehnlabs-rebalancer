"""
ETF Replacement Service for IRA account restrictions

This service handles replacing restricted ETFs with allowed alternatives
while maintaining equivalent exposure through scaling factors.
"""
import os
import yaml
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from app.logger import AppLogger
from app.models.rebalance_data import TargetAllocation

app_logger = AppLogger(__name__)


@dataclass
class ReplacementRule:
    """ETF replacement rule with scaling factor"""
    source: str      # Original ETF symbol (e.g., "UVXY")
    target: str      # Replacement ETF symbol (e.g., "VXX") 
    scale: float     # Scaling factor (e.g., 1.5 means 1 UVXY = 1.5 VXX)


class ReplacementService:
    """Service for applying ETF replacements with scaling"""
    
    def __init__(self):
        self.replacement_sets: Dict[str, List[ReplacementRule]] = {}
        self._load_replacement_sets()
    
    def _load_replacement_sets(self):
        """Load replacement sets from replacement-sets.yaml"""
        try:
            replacement_sets_path = os.path.join("/app", "replacement-sets.yaml")
            if not os.path.exists(replacement_sets_path):
                app_logger.log_warning(f"replacement-sets.yaml not found at {replacement_sets_path}")
                return
            
            with open(replacement_sets_path, 'r') as f:
                replacement_sets_data = yaml.safe_load(f)
            
            if not replacement_sets_data:
                app_logger.log_info("replacement-sets.yaml is empty")
                return
            
            # Parse replacement sets
            for set_name, rules_data in replacement_sets_data.items():
                rules = []
                for rule_data in rules_data:
                    rule = ReplacementRule(
                        source=rule_data['source'],
                        target=rule_data['target'],
                        scale=rule_data['scale']
                    )
                    rules.append(rule)
                
                self.replacement_sets[set_name] = rules
                app_logger.log_info(f"Loaded replacement set '{set_name}' with {len(rules)} rules")
            
        except Exception as e:
            app_logger.log_error(f"Failed to load replacement sets: {e}")
    
    def apply_replacements_with_scaling(self, allocations: List[TargetAllocation], replacement_set_name: Optional[str]) -> List[TargetAllocation]:
        """
        Apply ETF replacements with scaling, adjusting non-replaced allocations to maintain 100% total

        Args:
            allocations: List of TargetAllocation dataclasses
            replacement_set_name: Name of replacement set to use, or None to skip replacements

        Returns:
            Modified allocations list with replacements applied and normalized to 100%
        """
        if not replacement_set_name or replacement_set_name not in self.replacement_sets:
            app_logger.log_debug(f"No replacement set '{replacement_set_name}' found - returning original allocations")
            return allocations
        
        replacement_rules = {rule.source: rule for rule in self.replacement_sets[replacement_set_name]}
        
        if not replacement_rules:
            app_logger.log_debug(f"No replacement rules in set '{replacement_set_name}' - returning original allocations")
            return allocations
        
        # Step 1: Apply replacements and track changes  
        modified_allocations = []
        replaced_symbols = set()
        total_excess = 0.0
        
        app_logger.log_debug(f"Applying replacement set '{replacement_set_name}' with {len(replacement_rules)} rules")
        
        for allocation in allocations:
            if allocation.symbol in replacement_rules:
                rule = replacement_rules[allocation.symbol]
                old_allocation_percent = allocation.allocation_percent
                new_allocation_percent = old_allocation_percent * rule.scale
                
                # Create new TargetAllocation with replacement
                modified_allocations.append(TargetAllocation(
                    symbol=rule.target,
                    allocation_percent=new_allocation_percent
                ))
                
                # Track changes
                excess = new_allocation_percent - old_allocation_percent
                total_excess += excess
                replaced_symbols.add(rule.target)
                
                app_logger.log_debug(f"Replaced {allocation.symbol} -> {rule.target}: {old_allocation_percent:.3f} -> {new_allocation_percent:.3f} (scale: {rule.scale})")
            else:
                # Keep original allocation for now
                modified_allocations.append(allocation)
        
        # Step 2: If we have excess allocation, scale down non-replaced holdings proportionally
        if total_excess > 0:
            non_replaced_allocations = [a for a in modified_allocations if a.symbol not in replaced_symbols]
            non_replaced_total = sum(a.allocation_percent for a in non_replaced_allocations)
            
            if non_replaced_total > 0:
                # Calculate scale factor to absorb the excess
                target_non_replaced_total = non_replaced_total - total_excess
                
                if target_non_replaced_total < 0:
                    app_logger.log_warning(f"Replacement scaling exceeds available non-replaced allocation. Excess: {total_excess:.3f}, Available: {non_replaced_total:.3f}")
                    # Continue anyway - will result in over-allocation but preserve replacement intent
                    target_non_replaced_total = 0.1 * non_replaced_total  # Leave minimal allocation
                
                scale_factor = target_non_replaced_total / non_replaced_total
                
                app_logger.log_debug(f"Scaling down {len(non_replaced_allocations)} non-replaced holdings by factor {scale_factor:.3f} to absorb excess {total_excess:.3f}")
                
                # Recreate the list with scaled allocations
                final_allocations = []
                for allocation in modified_allocations:
                    if allocation.symbol not in replaced_symbols:
                        # Scale down non-replaced
                        old_allocation = allocation.allocation_percent
                        new_allocation = allocation.allocation_percent * scale_factor
                        final_allocations.append(TargetAllocation(
                            symbol=allocation.symbol,
                            allocation_percent=new_allocation
                        ))
                        app_logger.log_debug(f"Scaled down {allocation.symbol}: {old_allocation:.3f} -> {new_allocation:.3f}")
                    else:
                        # Keep replaced allocations as-is
                        final_allocations.append(allocation)
                
                modified_allocations = final_allocations
        
        # Step 3: Consolidate duplicate symbols that resulted from replacements
        symbol_consolidation = {}
        for allocation in modified_allocations:
            if allocation.symbol in symbol_consolidation:
                symbol_consolidation[allocation.symbol] += allocation.allocation_percent
            else:
                symbol_consolidation[allocation.symbol] = allocation.allocation_percent
        
        # Convert back to TargetAllocation list
        consolidated_allocations = [
            TargetAllocation(symbol=symbol, allocation_percent=total_allocation)
            for symbol, total_allocation in symbol_consolidation.items()
        ]
        
        # Verify final total
        final_total = sum(a.allocation_percent for a in consolidated_allocations)
        app_logger.log_debug(f"After consolidation: {len(consolidated_allocations)} unique symbols, total: {final_total:.3f} (should be ~1.0)")
        
        if abs(final_total - 1.0) > 0.01:
            app_logger.log_warning(f"Final allocation total is {final_total:.3f}, not 1.0 - may indicate replacement scaling issues")
        
        return consolidated_allocations