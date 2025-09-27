"""
ETF Replacement Service for IRA account restrictions

This service handles replacing restricted ETFs with allowed alternatives
while maintaining equivalent exposure through scaling factors.
"""
import os
import yaml
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass
from app.models import AllocationItem


@dataclass
class ReplacementRule:
    """ETF replacement rule with scaling factor"""
    source: str      # Original ETF symbol (e.g., "UVXY")
    target: str      # Replacement ETF symbol (e.g., "VXX") 
    scale: float     # Scaling factor (e.g., 1.5 means 1 UVXY = 1.5 VXX)


class ReplacementService:
    """Service for applying ETF replacements with scaling"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.replacement_sets: Dict[str, List[ReplacementRule]] = {}
        self._load_replacement_sets()
    
    def _load_replacement_sets(self):
        """Load replacement sets from replacement-sets.yaml"""
        try:
            replacement_sets_path = os.path.join("/app", "replacement-sets.yaml")
            if not os.path.exists(replacement_sets_path):
                self.logger.warning(f"replacement-sets.yaml not found at {replacement_sets_path}")
                return
            
            with open(replacement_sets_path, 'r') as f:
                replacement_sets_data = yaml.safe_load(f)
            
            if not replacement_sets_data:
                self.logger.info("replacement-sets.yaml is empty")
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
                self.logger.info(f"Loaded replacement set '{set_name}' with {len(rules)} rules")
            
        except Exception as e:
            self.logger.error(f"Failed to load replacement sets: {e}")
    
    def apply_replacements_with_scaling(self, allocations: List[AllocationItem], replacement_set_name: Optional[str]) -> List[AllocationItem]:
        """
        Apply ETF replacements with scaling, adjusting non-replaced allocations to maintain 100% total

        Args:
            allocations: List of allocation dictionaries with 'symbol' and 'allocation' keys
            replacement_set_name: Name of replacement set to use, or None to skip replacements

        Returns:
            Modified allocations list with replacements applied and normalized to 100%
        """
        if not replacement_set_name or replacement_set_name not in self.replacement_sets:
            self.logger.debug(f"No replacement set '{replacement_set_name}' found - returning original allocations")
            return allocations
        
        replacement_rules = {rule.source: rule for rule in self.replacement_sets[replacement_set_name]}
        
        if not replacement_rules:
            self.logger.debug(f"No replacement rules in set '{replacement_set_name}' - returning original allocations")
            return allocations
        
        # Step 1: Apply replacements and track changes  
        modified_allocations = []
        replaced_symbols = set()
        total_excess = 0.0
        
        self.logger.debug(f"Applying replacement set '{replacement_set_name}' with {len(replacement_rules)} rules")

        
        for allocation in allocations:
            symbol = allocation.symbol
            allocation_percent = allocation.allocation

            if symbol in replacement_rules:
                rule = replacement_rules[symbol]
                old_allocation_percent = allocation_percent
                new_allocation_percent = old_allocation_percent * rule.scale

                modified_allocations.append({
                    'symbol': rule.target,
                    'allocation': new_allocation_percent
                })

                excess = new_allocation_percent - old_allocation_percent
                total_excess += excess
                replaced_symbols.add(rule.target)

                self.logger.debug(f"Replaced {symbol} -> {rule.target}: {old_allocation_percent:.3f} -> {new_allocation_percent:.3f} (scale: {rule.scale})")
            else:
                modified_allocations.append({
                    'symbol': symbol,
                    'allocation': allocation_percent
                })
        
        # Step 2: If we have excess allocation, scale down non-replaced holdings proportionally
        if total_excess > 0:
            non_replaced_allocations = [a for a in modified_allocations if a['symbol'] not in replaced_symbols]
            non_replaced_total = sum(a['allocation'] for a in non_replaced_allocations)
            
            if non_replaced_total > 0:
                # Calculate scale factor to absorb the excess
                target_non_replaced_total = non_replaced_total - total_excess
                
                if target_non_replaced_total < 0:
                    self.logger.warning(f"Replacement scaling exceeds available non-replaced allocation. Excess: {total_excess:.3f}, Available: {non_replaced_total:.3f}")
                    # Continue anyway - will result in over-allocation but preserve replacement intent
                    target_non_replaced_total = 0.1 * non_replaced_total  # Leave minimal allocation
                
                scale_factor = target_non_replaced_total / non_replaced_total
                
                self.logger.debug(f"Scaling down {len(non_replaced_allocations)} non-replaced holdings by factor {scale_factor:.3f} to absorb excess {total_excess:.3f}")
                
                # Recreate the list with scaled allocations
                final_allocations = []
                for allocation in modified_allocations:
                    if allocation['symbol'] not in replaced_symbols:
                        # Scale down non-replaced
                        old_allocation = allocation['allocation']
                        new_allocation = allocation['allocation'] * scale_factor
                        final_allocations.append({
                            'symbol': allocation['symbol'],
                            'allocation': new_allocation
                        })
                        self.logger.debug(f"Scaled down {allocation['symbol']}: {old_allocation:.3f} -> {new_allocation:.3f}")
                    else:
                        # Keep replaced allocations as-is
                        final_allocations.append(allocation)
                
                modified_allocations = final_allocations
        
        # Step 3: Consolidate duplicate symbols that resulted from replacements
        symbol_consolidation = {}
        for allocation in modified_allocations:
            symbol = allocation['symbol']
            if symbol in symbol_consolidation:
                symbol_consolidation[symbol] += allocation['allocation']
            else:
                symbol_consolidation[symbol] = allocation['allocation']

        consolidated_allocations = [
            {'symbol': symbol, 'allocation': total_allocation}
            for symbol, total_allocation in symbol_consolidation.items()
        ]

        final_total = sum(a['allocation'] for a in consolidated_allocations)
        self.logger.debug(f"After consolidation: {len(consolidated_allocations)} unique symbols, total: {final_total:.3f}%")

        if final_total > 0 and abs(final_total - 100.0) > 0.001:
            normalization_factor = 100.0 / final_total
            for allocation in consolidated_allocations:
                allocation['allocation'] *= normalization_factor

            final_total_after_norm = sum(a['allocation'] for a in consolidated_allocations)
            self.logger.debug(f"Normalized allocations from {final_total:.3f}% to {final_total_after_norm:.3f}%")

            if abs(final_total_after_norm - 100.0) > 1.0:
                self.logger.warning(f"Final allocation total is {final_total_after_norm:.3f}%, not 100% - normalization failed")

        return [AllocationItem(**alloc) for alloc in consolidated_allocations]