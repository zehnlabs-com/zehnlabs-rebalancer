"""Read-only IBKR managed-Account discovery tests."""

import unittest

from ib_async import StartupFetch

from ibkr_connector.discovery import (
    DISCOVERY_CLIENT_ID,
    discover_managed_accounts,
)


class FakeIB:
    def __init__(self, accounts=None, failure=None):
        self.accounts = accounts or []
        self.failure = failure
        self.connect_kwargs = None
        self.disconnected = False

    async def connectAsync(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.failure:
            raise self.failure

    def managedAccounts(self):
        return self.accounts

    def disconnect(self):
        self.disconnected = True


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_no_fetch_discovery_sorts_and_deduplicates(self):
        ib = FakeIB(["U200002", " U100001 ", "U200002", "invalid-account"])
        result = await discover_managed_accounts(
            host="gateway",
            port=4004,
            timeout_seconds=10,
            ib_factory=lambda: ib,
        )
        self.assertEqual(result.accounts, ("U100001", "U200002"))
        self.assertEqual(ib.connect_kwargs["clientId"], DISCOVERY_CLIENT_ID)
        self.assertTrue(ib.connect_kwargs["readonly"])
        self.assertEqual(ib.connect_kwargs["account"], "")
        self.assertEqual(ib.connect_kwargs["fetchFields"], StartupFetch(0))
        self.assertTrue(ib.disconnected)

    async def test_disconnects_after_connection_failure(self):
        ib = FakeIB(failure=RuntimeError("credential text must not escape"))
        with self.assertRaisesRegex(RuntimeError, "Could not discover Accounts") as caught:
            await discover_managed_accounts(
                host="gateway",
                port=4004,
                timeout_seconds=10,
                ib_factory=lambda: ib,
            )
        self.assertNotIn("credential text", str(caught.exception))
        self.assertTrue(ib.disconnected)


if __name__ == "__main__":
    unittest.main()
