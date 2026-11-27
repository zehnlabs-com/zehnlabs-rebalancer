"""Code-owned configuration and environment setting acceptance tests."""

import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "rebalancer"))

from app_config import AppConfig  # noqa: E402
from app.settings import build_settings  # noqa: E402


class DefaultConfigurationTests(unittest.TestCase):
    def test_retained_defaults_match_the_shipped_v3_values(self):
        config = AppConfig()
        self.assertEqual(config.ibkr.request_timeout_seconds, 15.0)
        self.assertEqual(config.ibkr.connection_timeout_seconds, 10)
        self.assertEqual(config.ibkr.connection_stabilization_delay_seconds, 0.5)
        self.assertEqual(config.ibkr.market_data_type, 1)
        self.assertEqual(config.ibkr.order_placement_delay_seconds, 1.0)
        self.assertEqual(config.ibkr.market_data_retry_delay_seconds, 2.0)
        self.assertEqual(config.ibkr.market_data_max_retries, 10)
        self.assertEqual(config.ibkr.ports.live_internal, 4003)
        self.assertEqual(config.ibkr.ports.paper_internal, 4004)
        self.assertEqual(config.trading.minimum_cash_reserve_percent, 0.25)
        self.assertEqual(config.trading.buy_slippage_percent, 0.5)
        self.assertEqual(config.trading.optimization_timeout_seconds, 10.0)
        self.assertEqual(config.trading.order_timeout_seconds, 300)
        self.assertEqual(config.trading.order_status_check_interval_seconds, 2.0)
        self.assertEqual(config.trading.cancel_confirm_timeout_seconds, 30)
        self.assertEqual(config.trading.cash_update_timeout_seconds, 30)
        self.assertEqual(config.service.error_recovery_delay_seconds, 5.0)
        self.assertEqual(config.scheduler.market_open_time, "09:35")
        self.assertEqual(config.executor.max_workers, 32)
        self.assertEqual(config.api.rebalance_auth_timeout_seconds, 10)
        self.assertEqual(config.api.allocation_timeout_seconds, 30)

    def test_configuration_is_immutable_and_strict(self):
        config = AppConfig()
        with self.assertRaises(ValidationError):
            config.scheduler.market_open_time = "10:00"
        with self.assertRaises(ValidationError):
            AppConfig(scheduler={"scheduled_file_path": "removed.json"})
        with self.assertRaises(ValidationError):
            AppConfig(trading={"minimum_cash_reserve_percent": 99})

    def test_file_loader_and_global_getter_are_removed(self):
        import app_config

        self.assertFalse(hasattr(app_config, "load_config"))
        self.assertFalse(hasattr(app_config, "get_config"))
        self.assertFalse(
            (REPO / "packages/app-config/src/app_config/loader.py").exists()
        )


class RuntimeSettingsTests(unittest.TestCase):
    PASSWORD = "0123456789abcdef"

    def test_missing_operational_key_is_allowed_and_redacted(self):
        settings = build_settings({"DASHBOARD_ADMIN_PASSWORD": self.PASSWORD})
        self.assertEqual(settings.zehnlabs_api_key, "")
        self.assertEqual(settings.port, 18778)
        self.assertNotIn(self.PASSWORD, repr(settings))
        self.assertNotIn(self.PASSWORD, str(settings))

    def test_zehnlabs_key_is_redacted(self):
        settings = build_settings({
            "DASHBOARD_ADMIN_PASSWORD": self.PASSWORD,
            "ZEHNLABS_API_KEY": "sensitive-zehnlabs-key",
        })
        for rendered in (repr(settings), str(settings)):
            self.assertNotIn("sensitive-zehnlabs-key", rendered)
            self.assertNotIn("sensitive", rendered)
            self.assertNotIn("sen", rendered)
        self.assertIn("<redacted>", repr(settings))

    def test_unsafe_passwords_fail_without_echoing_input(self):
        for password in ("", "short", "admin", "password", "changeme",
                         "replace-with-a-strong-random-password",
                         "replace-with-a-unique-random-password"):
            with self.subTest(password=password):
                with self.assertRaises(ValueError) as caught:
                    build_settings({"DASHBOARD_ADMIN_PASSWORD": password})
                self.assertIn("DASHBOARD_ADMIN_PASSWORD", str(caught.exception))
                if password:
                    self.assertNotIn(password, str(caught.exception))

    def test_invalid_mode_and_port_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "TRADING_MODE"):
            build_settings({
                "DASHBOARD_ADMIN_PASSWORD": self.PASSWORD,
                "TRADING_MODE": "production",
            })
        for port in ("zero", "0", "65536"):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ValueError, "PORT"):
                    build_settings({
                        "DASHBOARD_ADMIN_PASSWORD": self.PASSWORD,
                        "PORT": port,
                    })


if __name__ == "__main__":
    unittest.main()
