from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_config import AppConfig
from ibkr_connector import AccountDiscoveryError, AccountDiscoveryResult
from rebalance_calculator import RawAllocation

from app.accounts import AccountConfig, AccountRegistry, parse_accounts_yaml
from app.allocations import AllocationsAuthenticationError
from app.services.account_manager import (
    AccountManager,
    ReadinessError,
    ReadinessState,
)
from app.settings import RuntimeSettings
from app.state.sqlite import SQLiteState


class FakeAllocationsClient:
    calls: list[str] = []
    fail = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, strategy):
        self.calls.append(strategy)
        if self.fail:
            raise RuntimeError("secret upstream body")
        return [RawAllocation(symbol="SPY", weight=1.0)]


class FakeSubscriber:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def reconcile(self, desired):
        self.calls.append({key: [a.account_id for a in value] for key, value in desired.items()})
        if self.failure:
            raise self.failure


class RejectedCatalogClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch_catalog(self):
        raise AllocationsAuthenticationError("secret rejected response")


class AccountManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SQLiteState(Path(self.tmp.name) / "state.db")
        self.state.initialize()
        self.registry = AccountRegistry()
        self.subscriber = FakeSubscriber()
        self.manager = AccountManager(
            config=AppConfig(),
            settings=RuntimeSettings(
                dashboard_admin_password="safe-dashboard-password-123",
                zehnlabs_api_key="synthetic-key",
            ),
            state=self.state,
            registry=self.registry,
            subscriber=self.subscriber,
        )
        FakeAllocationsClient.calls = []
        FakeAllocationsClient.fail = False
        self.manager._discovered_accounts = {"U100001", "U100002", "U200002"}

    async def asyncTearDown(self):
        await self.manager.stop()

    @patch("app.services.account_manager.AllocationsClient", FakeAllocationsClient)
    async def test_save_fetches_each_strategy_once_refreshes_and_reconciles(self):
        accounts = [
            AccountConfig(account_id="U100001", type="paper", strategy_name="strategy-one"),
            AccountConfig(account_id="U100002", type="paper", strategy_name="strategy-one"),
        ]
        result = await self.manager.save(accounts)
        self.assertEqual(FakeAllocationsClient.calls, ["strategy-one"])
        self.assertEqual([a.account_id for a in self.registry.accounts], ["U100001", "U100002"])
        self.assertEqual(self.subscriber.calls[-1], {"strategy-one": ["U100001", "U100002"]})
        self.assertIsNone(result.degraded_error)

    @patch("app.services.account_manager.AllocationsClient", FakeAllocationsClient)
    async def test_failed_validation_changes_nothing_and_redacts_upstream(self):
        FakeAllocationsClient.fail = True
        account = AccountConfig(
            account_id="U100001", type="paper", strategy_name="strategy-one"
        )
        with self.assertRaisesRegex(ValueError, "Strategy 'strategy-one' could not be validated") as caught:
            await self.manager.save([account])
        self.assertNotIn("secret upstream body", str(caught.exception))
        self.assertEqual(self.state.list_accounts(), [])

    @patch("app.services.account_manager.AllocationsClient", FakeAllocationsClient)
    async def test_new_account_must_come_from_managed_account_discovery(self):
        account = AccountConfig(
            account_id="U999999", type="paper", strategy_name="strategy-one"
        )
        with self.assertRaisesRegex(ValueError, "Discover managed IBKR Accounts"):
            await self.manager.save([account])
        self.assertEqual(self.state.list_accounts(), [])

    @patch("app.services.account_manager.AllocationsClient", FakeAllocationsClient)
    async def test_subscription_failure_keeps_committed_state_and_reports_degraded(self):
        self.subscriber.failure = RuntimeError("synthetic subscription failure")
        account = AccountConfig(
            account_id="U100001", type="paper", strategy_name="strategy-one"
        )
        result = await self.manager.save([account])
        self.assertEqual(self.state.list_accounts("paper"), [account])
        self.assertEqual(
            result.degraded_error,
            "Signal subscriptions are unavailable. The service will retry automatically.",
        )
        self.assertNotIn("synthetic subscription failure", result.degraded_error)

    @patch("app.services.account_manager.AllocationsClient", FakeAllocationsClient)
    async def test_unexpected_iteration_failure_recovers_without_another_save(self):
        account = AccountConfig(
            account_id="U100001", type="paper", strategy_name="strategy-one"
        )
        load_committed = self.manager.load_committed
        load_calls = 0
        recovery_wait_started = asyncio.Event()
        resume_recovery = asyncio.Event()

        def fail_first_loop_load():
            nonlocal load_calls
            load_calls += 1
            if load_calls == 2:
                raise RuntimeError("synthetic committed-load failure")
            load_committed()

        async def controlled_recovery_delay(delay):
            self.assertEqual(delay, 1)
            recovery_wait_started.set()
            await resume_recovery.wait()

        replace_accounts = self.state.replace_accounts
        save_count = 0

        def count_account_save(*args, **kwargs):
            nonlocal save_count
            save_count += 1
            return replace_accounts(*args, **kwargs)

        self.manager.load_committed = fail_first_loop_load
        self.manager.state.replace_accounts = count_account_save
        self.manager.config = AppConfig(service={"error_recovery_delay_seconds": 1})

        with patch(
            "app.services.account_manager.asyncio.sleep",
            controlled_recovery_delay,
        ):
            save = asyncio.create_task(self.manager.save([account]))
            await asyncio.wait_for(recovery_wait_started.wait(), timeout=0.5)
            result = await asyncio.wait_for(save, timeout=0.5)
            reconciliation_task = self.manager._reconcile_task

            self.assertIsNotNone(reconciliation_task)
            self.assertFalse(reconciliation_task.done())
            self.assertEqual(save_count, 1)
            self.assertEqual(self.state.list_accounts("paper"), [account])
            self.assertEqual(
                result.degraded_error,
                "Signal subscriptions are unavailable. The service will retry automatically.",
            )
            self.assertNotIn("synthetic committed-load failure", result.degraded_error)

            resume_recovery.set()
            await asyncio.wait_for(reconciliation_task, timeout=0.5)

        self.assertEqual(save_count, 1)
        self.assertEqual(self.subscriber.calls, [{"strategy-one": ["U100001"]}])
        self.assertIsNone(self.manager.degraded_error)
        self.assertTrue(reconciliation_task.done())

    @patch("app.services.account_manager.AllocationsClient", FakeAllocationsClient)
    async def test_shutdown_settles_import_waiting_for_reconciliation(self):
        class BlockingSubscriber:
            def __init__(self):
                self.started = asyncio.Event()

            async def reconcile(self, _desired):
                self.started.set()
                await asyncio.Event().wait()

        await self.manager.stop()
        subscriber = BlockingSubscriber()
        self.manager = AccountManager(
            config=AppConfig(),
            settings=RuntimeSettings(
                dashboard_admin_password="safe-dashboard-password-123",
                zehnlabs_api_key="synthetic-key",
            ),
            state=self.state,
            registry=self.registry,
            subscriber=subscriber,
        )
        account = AccountConfig(
            account_id="U100001", type="paper", strategy_name="strategy-one"
        )
        document = """accounts:
  - account_id: U100001
    type: paper
    enabled: true
    strategy_name: strategy-one
"""

        save = asyncio.create_task(self.manager.import_yaml(document))
        await subscriber.started.wait()
        await self.manager.stop()
        result = await asyncio.wait_for(save, timeout=0.5)

        self.assertEqual(result.accounts, (account,))
        self.assertEqual(self.state.list_accounts("paper"), [account])

    async def test_discovery_sets_specific_safe_readiness_failures(self):
        async def gateway_failure(**_kwargs):
            raise AccountDiscoveryError("secret gateway response")

        with patch(
            "app.services.account_manager.discover_managed_accounts",
            gateway_failure,
        ):
            with self.assertRaisesRegex(
                ReadinessError, "unreachable or unauthenticated"
            ) as gateway_error:
                await self.manager.discover()
        self.assertEqual(
            self.manager.gateway_readiness.state, ReadinessState.UNAVAILABLE
        )
        self.assertNotIn("secret gateway response", str(gateway_error.exception))

        async def gateway_ready(**_kwargs):
            return AccountDiscoveryResult(accounts=("U100001",))

        with (
            patch(
                "app.services.account_manager.discover_managed_accounts",
                gateway_ready,
            ),
            patch(
                "app.services.account_manager.AllocationsClient",
                RejectedCatalogClient,
            ),
        ):
            with self.assertRaisesRegex(ReadinessError, "rejected") as key_error:
                await self.manager.discover()
        self.assertEqual(self.manager.gateway_readiness.state, ReadinessState.READY)
        self.assertEqual(self.manager.zehnlabs_readiness.state, ReadinessState.REJECTED)
        self.assertNotIn("secret rejected response", str(key_error.exception))

    @patch("app.services.account_manager.AllocationsClient", FakeAllocationsClient)
    async def test_yaml_import_is_atomic_all_modes_and_export_has_no_secrets(self):
        document = """accounts:
  - account_id: U100001
    type: paper
    enabled: true
    strategy_name: strategy-one
  - account_id: U200002
    type: live
    enabled: false
    strategy_name: strategy-two
"""
        await self.manager.import_yaml(document)
        exported = self.manager.export_yaml()
        self.assertEqual(
            sorted(parse_accounts_yaml(exported), key=lambda account: account.account_id),
            sorted(parse_accounts_yaml(document), key=lambda account: account.account_id),
        )
        self.assertNotIn("synthetic-key", exported)
        with self.assertRaisesRegex(Exception, "only before Accounts exist"):
            await self.manager.import_yaml(document)

    @patch("app.services.account_manager.AllocationsClient", FakeAllocationsClient)
    async def test_save_and_recovery_reconciliation_have_one_latest_registry_owner(self):
        class BlockingSubscriber:
            def __init__(self):
                self.calls = []
                self.started = asyncio.Queue()
                self.releases = asyncio.Queue()
                self.active = 0
                self.max_active = 0

            async def reconcile(self, desired):
                snapshot = {
                    key: [account.account_id for account in value]
                    for key, value in desired.items()
                }
                self.calls.append(snapshot)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await self.started.put(len(self.calls))
                should_fail = await self.releases.get()
                self.active -= 1
                if should_fail:
                    raise RuntimeError("synthetic first failure")

        await self.manager.stop()
        subscriber = BlockingSubscriber()
        self.manager = AccountManager(
            config=AppConfig(service={"error_recovery_delay_seconds": 1}),
            settings=RuntimeSettings(
                dashboard_admin_password="safe-dashboard-password-123",
                zehnlabs_api_key="synthetic-key",
            ),
            state=self.state,
            registry=self.registry,
            subscriber=subscriber,
        )
        self.manager._discovered_accounts = {"U100001", "U100002"}
        first = AccountConfig(
            account_id="U100001", type="paper", strategy_name="strategy-one"
        )
        latest = AccountConfig(
            account_id="U100002", type="paper", strategy_name="strategy-two"
        )

        first_save = asyncio.create_task(self.manager.save([first]))
        await subscriber.started.get()
        second_save = asyncio.create_task(self.manager.save([latest]))
        await asyncio.sleep(0)
        await subscriber.releases.put(True)
        await subscriber.started.get()
        await subscriber.releases.put(False)
        await asyncio.gather(first_save, second_save)

        self.assertEqual(subscriber.max_active, 1)
        self.assertEqual(subscriber.calls[-1], {"strategy-two": ["U100002"]})
        self.assertEqual(self.registry.accounts, [latest])


if __name__ == "__main__":
    unittest.main()
