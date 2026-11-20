"""WP5 acceptance tests: strict accounts.yaml (PRD §5.2)."""

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "event-broker"))

from app.accounts import load_accounts, AccountRegistry, AccountConfig  # noqa: E402

REPLACEMENT_SETS = {"ira"}


def write(accounts) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(yaml.safe_dump({"accounts": accounts}))
    f.close()
    return Path(f.name)


def acct(**over):
    base = {"account_id": "U100001", "type": "paper", "enabled": True,
            "strategy_name": "strategy-one"}
    base.update(over)
    return base


class LoadTests(unittest.TestCase):
    def test_example_file_loads(self):
        reg = load_accounts(REPO / "accounts.yaml.example", "paper")
        self.assertIsInstance(reg, AccountRegistry)

    def test_registry_filters_active_mode(self):
        accounts = [
            acct(account_id="U100001"),
            acct(account_id="U100002"),
            acct(account_id="U100003", enabled=False),
            acct(account_id="U100004", type="live"),
        ]
        p = write(accounts)
        self.addCleanup(p.unlink)
        reg = load_accounts(p, "paper")
        self.assertEqual({a.account_id for a in reg.accounts}, {"U100001", "U100002"})
        self.assertEqual([a.account_id for a in reg.for_strategy("strategy-one")],
                         ["U100001", "U100002"])
        self.assertEqual(reg.strategies(), ["strategy-one"])
        self.assertIsNotNone(reg.get("U100001"))
        self.assertIsNone(reg.get("U100003"))

    def test_client_id_is_digits_of_account_id(self):
        cfg = AccountConfig(account_id="DUM959247", type="live", strategy_name="s")
        self.assertEqual(cfg.client_id, 959247)


class ErrorTests(unittest.TestCase):
    def expect(self, accounts, fragment):
        p = write(accounts)
        self.addCleanup(p.unlink)
        with self.assertRaises(ValueError) as ctx:
            load_accounts(p, "paper")
        self.assertIn(fragment, str(ctx.exception))

    def test_removed_cash_reserve_key(self):
        self.expect([acct(cash_reserve_percent=1.0)],
                    "account #1 (U100001): 'cash_reserve_percent' is no longer supported")

    def test_removed_pdt_key(self):
        self.expect([acct(pdt_protection_enabled=True)],
                    "account #1 (U100001): 'pdt_protection_enabled' is no longer supported")

    def test_unknown_key(self):
        self.expect([acct(bogus=1)], "account #1 (U100001): unknown setting 'bogus'.")

    def test_unknown_replacement_set(self):
        self.expect([acct(replacement_set="nope")],
                    "replacement_set 'nope' does not exist. Valid: ira.")

    def test_duplicate_account(self):
        self.expect([acct(), acct(account_id="U100002"), acct(account_id="U100001")],
                    "account 'U100001' is listed more than once (#1, #3).")

    def test_duplicate_client_id_active(self):
        self.expect([acct(account_id="U123"), acct(account_id="A123", strategy_name="other")],
                    "produce the same IBKR client ID 123")

    def test_client_id_too_large(self):
        self.expect([acct(account_id="U99999999999999999999999")],
                    "client ID 99999999999999999999999 is too large")

    def test_invalid_account_id(self):
        self.expect([acct(account_id="abc-123")], "account_id")

    def test_invalid_strategy_name(self):
        self.expect([acct(strategy_name="ETF Blend")], "strategy_name")

    def test_bad_type(self):
        self.expect([acct(type="margin")], "type")

    def test_disabled_account_with_removed_key_still_fails(self):
        self.expect([acct(enabled=False, cash_reserve_percent=1.0)],
                    "'cash_reserve_percent' is no longer supported")

    def test_duplicate_client_id_between_active_accounts(self):
        # two accounts with the same digit sequence (same client ID) both active
        self.expect([acct(account_id="U123456"), acct(account_id="W123456", strategy_name="other")],
                    "produce the same IBKR client ID 123456")

    def test_all_problems_fail_once(self):
        p = write([acct(cash_reserve_percent=1.0), acct(bogus=1)])
        self.addCleanup(p.unlink)
        with self.assertRaises(ValueError) as ctx:
            load_accounts(p, "paper")
        msg = str(ctx.exception)
        self.assertIn("cash_reserve_percent", msg)
        self.assertIn("unknown setting 'bogus'", msg)


if __name__ == "__main__":
    unittest.main()