"""WP3 acceptance tests: targets + replacement sets (PRD §7.1–7.2, Appendix A.1)."""

import unittest

from rebalance_calculator import (
    RawAllocation,
    Targets,
    REPLACEMENT_SETS,
    ReplacementRule,
    validate_and_scale,
    apply_replacements,
    InvalidTargetsError,
    validate_replacement_set,
)

EPS = 1e-9

IRA = REPLACEMENT_SETS["ira"]


def alloc(*pairs):
    return [RawAllocation(symbol=s, weight=w) for s, w in pairs]


class ValidateTests(unittest.TestCase):
    def assertWeights(self, targets, expected, places=9):
        for symbol, w in expected.items():
            self.assertAlmostEqual(targets.weights[symbol], w, places=places)

    def test_A5_total_995_scales_weights(self):
        t = validate_and_scale(alloc(("SPY", 0.60), ("TLT", 0.395)))
        self.assertWeights(t, {"SPY": 0.6030150753773, "TLT": 0.396984924623})
        self.assertAlmostEqual(t.scaled["SPY"], 0.603015075377, places=9)

    def test_A7_total_98_invalid(self):
        with self.assertRaises(InvalidTargetsError) as ctx:
            validate_and_scale(alloc(("SPY", 0.60), ("TLT", 0.38)))
        self.assertIn("98.00%, outside 99%", str(ctx.exception))

    def test_A8_duplicate_normalised_invalid(self):
        with self.assertRaises(InvalidTargetsError) as ctx:
            validate_and_scale(alloc(("SECRET", 0.5), ("secret", 0.5)))
        self.assertEqual(str(ctx.exception), "Strategy contains a duplicate symbol.")
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_A9_negative_weight_invalid(self):
        with self.assertRaises(InvalidTargetsError):
            validate_and_scale(alloc(("SPY", 1.05), ("TLT", -0.05)))

    def test_A10_empty_list_valid_no_holdings(self):
        t = validate_and_scale([])
        self.assertEqual(t.weights, {})
        self.assertEqual(t.notes[0].text, "Strategy has no allocations: selling all positions.")

    def test_missing_weight_invalid(self):
        with self.assertRaises(InvalidTargetsError) as ctx:
            validate_and_scale([RawAllocation("SECRET", None)])
        self.assertEqual(
            str(ctx.exception),
            "Strategy contains a missing, non-finite, or negative weight.",
        )
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_invalid_symbol_is_not_echoed(self):
        with self.assertRaises(InvalidTargetsError) as ctx:
            validate_and_scale([RawAllocation("synthetic-secret", 1.0)])
        self.assertEqual(str(ctx.exception), "Strategy contains an invalid symbol.")
        self.assertNotIn("synthetic-secret", str(ctx.exception))

    def test_non_string_symbol_is_invalid(self):
        with self.assertRaises(InvalidTargetsError) as ctx:
            validate_and_scale([RawAllocation(123, 1.0)])
        self.assertEqual(str(ctx.exception), "Strategy contains an invalid symbol.")

    def test_weight_zero_dropped_from_targets(self):
        t = validate_and_scale(alloc(("SPY", 1.0), ("GLD", 0.0)))
        self.assertEqual(t.weights, {"SPY": 1.0})
        self.assertEqual(t.scaled["GLD"], 0.0)  # kept for logging

    def test_sum_exact_one_unchanged_raw_scaled(self):
        t = validate_and_scale(alloc(("SPY", 0.5), ("TLT", 0.5)))
        self.assertWeights(t, {"SPY": 0.5, "TLT": 0.5})

    def test_symbol_stripped_and_upper(self):
        t = validate_and_scale(alloc((" spy ", 0.5), ("tlt", 0.5)))
        self.assertIn("SPY", t.weights)
        self.assertIn("TLT", t.weights)


class ReplacementTests(unittest.TestCase):
    def run_rules(self, weights):
        return apply_replacements(weights, IRA)

    def test_A1_uvxy_spy(self):
        result, notes = self.run_rules({"UVXY": 0.70, "SPY": 0.30})
        self.assertAlmostEqual(result["VXX"], 1.0, places=6)
        self.assertNotIn("SPY", result)
        self.assertEqual(notes[0].level, "warning")
        self.assertIn("VXX", notes[0].text)

    def test_A2_uvxy_spy_half(self):
        result, _ = self.run_rules({"UVXY": 0.50, "SPY": 0.50})
        self.assertAlmostEqual(result["VXX"], 0.75, places=10)
        self.assertAlmostEqual(result["SPY"], 0.25, places=10)

    def test_A3_direct_vxx_is_ordinary(self):
        result, _ = self.run_rules({"UVXY": 0.20, "VXX": 0.10, "SPY": 0.70})
        self.assertAlmostEqual(result["VXX"], 0.3875, places=10)
        self.assertAlmostEqual(result["SPY"], 0.6125, places=10)

    def test_A4_vixy_uvxy(self):
        result, _ = self.run_rules({"VIXY": 0.10, "UVXY": 0.20, "SPY": 0.70})
        self.assertAlmostEqual(result["VXX"], 0.40, places=10)
        self.assertAlmostEqual(result["SPY"], 0.60, places=10)

    def test_A6_dbc_gld_no_trim(self):
        result, _ = self.run_rules({"DBC": 0.25, "GLD": 0.75})
        self.assertAlmostEqual(result["PDBC"], 0.25, places=10)
        self.assertAlmostEqual(result["GLD"], 0.75, places=10)

    def test_result_always_sums_to_one(self):
        result, _ = self.run_rules({"UVXY": 0.30, "VIXY": 0.20, "SPY": 0.30, "VXX": 0.20})
        self.assertAlmostEqual(sum(result.values()), 1.0, places=9)

    def test_70_uvxy_30_spy_validates_then_replacement_applied_after_one_percent_check(self):
        # TGT-5: the 1% check runs on raw weights first; replacements change the total.
        t = validate_and_scale(alloc(("UVXY", 0.70), ("SPY", 0.30)))
        result, _ = self.run_rules(t.weights)
        self.assertAlmostEqual(result["VXX"], 1.0, places=6)


class ReplacementDefinitionTests(unittest.TestCase):
    def test_shipped_set_valid(self):
        # REP-2: the shipped sets must pass validation (already runs at import).
        self.assertEqual(REPLACEMENT_SETS["ira"][0].source, "DBC")
        self.assertEqual(REPLACEMENT_SETS["ira"][0].target, "PDBC")

    def test_empty_symbol_rejected(self):
        with self.assertRaises(ValueError):
            validate_replacement_set("x", (ReplacementRule("", "PDBC", 1.0),))
        with self.assertRaises(ValueError):
            validate_replacement_set("x", (ReplacementRule("DBC", "", 1.0),))

    def test_lowercase_symbol_rejected(self):
        with self.assertRaises(ValueError):
            validate_replacement_set("x", (ReplacementRule("dbc", "PDBC", 1.0),))

    def test_scale_less_than_one_rejected(self):
        with self.assertRaises(ValueError):
            validate_replacement_set("x", (ReplacementRule("DBC", "PDBC", 0.5),))
        with self.assertRaises(ValueError):
            validate_replacement_set("x", (ReplacementRule("DBC", "PDBC", float("nan")),))

    def test_source_equals_target_rejected(self):
        with self.assertRaises(ValueError):
            validate_replacement_set("x", (ReplacementRule("AAA", "AAA", 1.0),))

    def test_duplicate_source_rejected(self):
        with self.assertRaises(ValueError):
            validate_replacement_set("x", (
                ReplacementRule("DBC", "PDBC", 1.0),
                ReplacementRule("DBC", "VXX", 1.0),
            ))

    def test_chain_target_is_source_rejected(self):
        with self.assertRaises(ValueError):
            validate_replacement_set("x", (
                ReplacementRule("ABC", "DEF", 1.0),
                ReplacementRule("DEF", "GHI", 1.0),
            ))


if __name__ == "__main__":
    unittest.main()
