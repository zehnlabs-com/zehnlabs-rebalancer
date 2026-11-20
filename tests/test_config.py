"""WP5 acceptance tests: strict config.yaml (PRD §5.1)."""

import tempfile
import unittest
from pathlib import Path

import yaml
from pydantic import ValidationError

from app_config import AppConfig, load_config, get_config

REPO = Path(__file__).resolve().parents[1]


def write(raw: dict) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(yaml.safe_dump(raw))
    f.close()
    return Path(f.name)


VALID = {
    "ibkr": {"request_timeout_seconds": 10.0, "ports": {"live_internal": 4003}},
    "trading": {"minimum_cash_reserve_percent": 0.25},
    "service": {},
    "scheduler": {"market_open_time": "09:35"},
    "executor": {},
    "api": {},
}


class LoadTests(unittest.TestCase):
    def test_shipped_config_loads(self):
        cfg = load_config(REPO / "config.yaml")
        self.assertEqual(cfg.scheduler.market_open_time, "09:35")
        self.assertEqual(cfg.ibkr.ports.live_internal, 4003)
        self.assertIsInstance(cfg, AppConfig)

    def test_defaults_apply(self):
        cfg = AppConfig()
        self.assertEqual(cfg.scheduler.market_open_time, "09:35")
        self.assertEqual(cfg.trading.minimum_cash_reserve_percent, 0.25)
        self.assertEqual(cfg.api.rebalance_auth_timeout_seconds, 10)
        # Above IBKR's 11 s snapshot window (reqTickers ends on tickSnapshotEnd).
        self.assertEqual(cfg.ibkr.request_timeout_seconds, 15.0)
        self.assertEqual(cfg.executor.max_workers, 32)

    def test_unknown_top_level_key(self):
        p = write({"ibkr": {"request_timeout_seconds": 10.0}, "bogus": {}})
        self.addCleanup(p.unlink)
        with self.assertRaisesRegex(ValueError, "unknown setting 'bogus'"):
            load_config(p)

    def test_unknown_nested_key(self):
        p = write({"trading": {"minimum_cash_reserve_percent": 0.25, "bogus": 1}})
        self.addCleanup(p.unlink)
        with self.assertRaisesRegex(ValueError, "unknown setting 'trading.bogus'"):
            load_config(p)


class RemovedKeyTests(unittest.TestCase):
    CASES = [
        ("ibkr", {"price_cache_ttl_seconds": 30}, "'ibkr.price_cache_ttl_seconds' is no longer supported (the price cache was removed). Delete this line."),
        ("ibkr", {"synthetic_ask_offset_usd": 1.0}, "'ibkr.synthetic_ask_offset_usd' is no longer supported (prices now fall back to last/close/bar). Delete this line."),
        ("trading", {"minimum_cash_reserve_usd": 100.0}, "'trading.minimum_cash_reserve_usd' is no longer supported replaced by 'trading.minimum_cash_reserve_percent'. Delete this line."),
        ("trading", {"commission_rate": 0.01}, "'trading.commission_rate' is no longer supported (commissions are covered by 'trading.minimum_cash_reserve_percent'). Delete this line."),
        ("trading", {"max_account_utilization": 0.995}, "'trading.max_account_utilization' is no longer supported (removed with buy scaling). Delete this line."),
        ("trading", {"allocation_threshold_percent": 0.5}, "'trading.allocation_threshold_percent' is no longer supported (every run now aligns all symbols to target). Delete this line."),
        ("trading", {"order_tif": "DAY"}, "'trading.order_tif' is no longer supported (orders are always DAY). Delete this line."),
        ("trading", {"post_completion_delay_seconds": 1.0}, "'trading.post_completion_delay_seconds' is no longer supported replaced by 'trading.cash_update_timeout_seconds'. Delete this line."),
    ]

    def test_each_removed_key_message(self):
        for section, bad, expected in self.CASES:
            with self.subTest(section=section):
                raw = {**VALID, section: {**VALID.get(section, {}), **bad}}
                p = write(raw)
                self.addCleanup(p.unlink)
                with self.assertRaises(ValueError) as ctx:
                    load_config(p)
                self.assertIn(expected, str(ctx.exception))

    def test_removed_sections(self):
        for section, expected in [
            ("replacement", "section 'replacement' is no longer supported (replacement sets are built in). Delete this section."),
            ("pdt_protection", "section 'pdt_protection' is no longer supported (PDT protection was removed). Delete this section."),
        ]:
            with self.subTest(section=section):
                p = write({**VALID, section: {"x": 1}})
                self.addCleanup(p.unlink)
                with self.assertRaises(ValueError) as ctx:
                    load_config(p)
                self.assertIn(expected, str(ctx.exception))

    def test_all_offending_keys_listed_together(self):
        p = write({**VALID, "trading": {"minimum_cash_reserve_usd": 100, "order_tif": "DAY"}})
        self.addCleanup(p.unlink)
        with self.assertRaises(ValueError) as ctx:
            load_config(p)
        msg = str(ctx.exception)
        self.assertIn("trading.minimum_cash_reserve_usd", msg)
        self.assertIn("trading.order_tif", msg)


class RangeErrorTests(unittest.TestCase):
    def test_pydantic_range_error_surfaces_with_config_prefix(self):
        p = write({"trading": {"minimum_cash_reserve_percent": 99}})
        self.addCleanup(p.unlink)
        with self.assertRaises(ValueError) as ctx:
            load_config(p)
        self.assertIn("config.yaml:", str(ctx.exception))

    def test_bad_market_open_time_rejected(self):
        p = write({"scheduler": {"market_open_time": "99:99"}})
        self.addCleanup(p.unlink)
        with self.assertRaises(ValueError):
            load_config(p)


if __name__ == "__main__":
    unittest.main()