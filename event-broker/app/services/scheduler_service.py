"""Scheduler Service - Market open scheduled rebalancing"""

import os
import json
import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional, Callable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import exchange_calendars as xcals

from app_config import get_config


def add_account_to_schedule(account_id: str, logger: Optional[logging.Logger] = None) -> bool:
    """
    Add an account to the scheduled.json file for next market-open rebalancing.

    This utility function can be called from anywhere (e.g., PDT protection)
    to auto-schedule an account for the next trading day.

    Args:
        account_id: The account ID to add to the schedule
        logger: Optional logger instance

    Returns:
        True if account was added (or already scheduled), False on error
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    try:
        config = get_config()
        scheduled_file = config.scheduler.scheduled_file_path

        # Ensure directory exists
        os.makedirs(os.path.dirname(scheduled_file), exist_ok=True)

        # Read current scheduled accounts
        if os.path.exists(scheduled_file):
            with open(scheduled_file, 'r') as f:
                account_ids = json.load(f)
                if not isinstance(account_ids, list):
                    account_ids = []
        else:
            account_ids = []

        # Check if already scheduled
        if account_id in account_ids:
            logger.debug(f"Account {account_id} is already scheduled for market-open rebalance")
            return True

        # Add account
        account_ids.append(account_id)

        # Write back atomically
        temp_file = scheduled_file + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(account_ids, f)
        os.replace(temp_file, scheduled_file)

        logger.info(f"Auto-scheduled {account_id} for next market-open rebalance")
        return True

    except Exception as e:
        if logger:
            logger.error(f"Failed to auto-schedule {account_id}: {e}")
        return False


class SchedulerService:
    """
    Handles scheduled rebalancing at market open.

    This service runs a scheduled job at the configured market open time (default 9:30 AM ET)
    on trading days (Mon-Fri, excluding NYSE holidays). It reads accounts from the scheduled
    file, groups them by strategy, and executes strategies in parallel.

    The container runs with TZ=America/New_York, so datetime.now() returns ET time.
    """

    def __init__(self, strategy_executor, accounts_lookup: Callable, logger: Optional[logging.Logger] = None):
        """
        Initialize the scheduler service.

        Args:
            strategy_executor: The StrategyExecutor instance for executing rebalances
            accounts_lookup: Function that takes account_id and returns account dict or None
            logger: Optional logger instance
        """
        self.config = get_config()
        self.logger = logger or logging.getLogger(__name__)
        self.strategy_executor = strategy_executor
        self.accounts_lookup = accounts_lookup
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.nyse = xcals.get_calendar("NYSE")
        self.scheduled_file = self.config.scheduler.scheduled_file_path
        self.running = False

    def _ensure_scheduled_file_exists(self):
        """Ensure the scheduled file directory and file exist at startup."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.scheduled_file), exist_ok=True)

            # Create empty file if it doesn't exist
            if not os.path.exists(self.scheduled_file):
                with open(self.scheduled_file, 'w') as f:
                    json.dump([], f)
                self.logger.info(f"Created scheduled file: {self.scheduled_file}")
            else:
                self.logger.debug(f"Scheduled file exists: {self.scheduled_file}")
        except Exception as e:
            self.logger.error(f"Failed to ensure scheduled file exists: {e}")
            raise

    async def start(self):
        """Start the scheduler."""
        if not self.config.scheduler.enabled:
            self.logger.info("Scheduler is disabled in configuration")
            return

        # Ensure scheduled file exists at startup
        self._ensure_scheduled_file_exists()

        # APScheduler uses system timezone (container has TZ=America/New_York)
        self.scheduler = AsyncIOScheduler()

        # Parse market open time from config (e.g., "09:30")
        hour, minute = self.config.scheduler.market_open_time.split(':')

        self.scheduler.add_job(
            self._execute_scheduled_rebalance,
            CronTrigger(
                hour=int(hour),
                minute=int(minute),
                day_of_week='mon-fri'
            ),
            id='market_open_rebalance',
            name='Market Open Rebalance'
        )

        self.scheduler.start()
        self.running = True
        self.logger.info(f"Scheduler started - market open rebalance at {hour}:{minute} ET (Mon-Fri)")

    async def stop(self):
        """Stop the scheduler gracefully."""
        if self.scheduler and self.running:
            self.scheduler.shutdown(wait=False)
            self.running = False
            self.logger.info("Scheduler stopped")

    def _is_trading_day(self, date) -> bool:
        """
        Check if date is a valid NYSE trading day.

        Args:
            date: The date to check

        Returns:
            True if the date is a trading day, False otherwise
        """
        try:
            return self.nyse.is_session(date.strftime('%Y-%m-%d'))
        except Exception as e:
            self.logger.error(f"Error checking trading day: {e}")
            return False  # Fail-safe: don't execute on error

    async def _execute_scheduled_rebalance(self):
        """Execute scheduled rebalance if it's a trading day."""
        # Container runs with TZ=America/New_York, so datetime.now() is already ET
        today = datetime.now().date()

        # Skip weekends/holidays
        if not self._is_trading_day(today):
            self.logger.info(f"Skipping scheduled rebalance - {today} is not a trading day")
            return

        await self._process_scheduled_accounts()

    async def _process_scheduled_accounts(self):
        """Process all scheduled accounts, grouped by strategy for parallel execution."""
        try:
            # Read scheduled accounts
            with open(self.scheduled_file, 'r') as f:
                account_ids = json.load(f)

            if not account_ids or not isinstance(account_ids, list):
                self.logger.info("Scheduled file is empty or invalid")
                self._clear_scheduled_file()
                return

            self.logger.info(f"Processing {len(account_ids)} scheduled accounts")

            # Group accounts by strategy (using defaultdict for cleaner grouping)
            strategies = defaultdict(list)
            skipped = []
            for account_id in account_ids:
                account = self.accounts_lookup(account_id)
                if not account:
                    self.logger.warning(f"Account {account_id} not found in configuration, skipping")
                    skipped.append(account_id)
                    continue
                strategies[account['strategy_name']].append(account)

            if not strategies:
                self.logger.info("No valid accounts to process")
                self._clear_scheduled_file()
                return

            # Log strategy breakdown
            for strategy_name, accounts in strategies.items():
                account_ids_str = ', '.join(acc['account_id'] for acc in accounts)
                self.logger.info(f"Strategy '{strategy_name}': {len(accounts)} accounts ({account_ids_str})")

            # Build event data
            event_data = {
                'exec': 'rebalance',
                'source': 'scheduled',
                'timestamp': datetime.now().isoformat()
            }

            # Execute all strategies in parallel (follows trading_executor asyncio.gather pattern)
            tasks = [
                self.strategy_executor.execute_strategy(strategy_name, accounts, event_data)
                for strategy_name, accounts in strategies.items()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results and log summary (follows strategy_executor logging pattern)
            total_successful = 0
            total_failed = 0
            for (strategy_name, accounts), result in zip(strategies.items(), results):
                if isinstance(result, Exception):
                    self.logger.error(f"Strategy {strategy_name} failed completely: {result}")
                    total_failed += len(accounts)
                elif result.get('status') == 'already_running':
                    self.logger.warning(f"Strategy {strategy_name} was already running, skipped")
                    total_failed += len(accounts)
                else:
                    account_results = result.get('results', [])
                    successful = sum(1 for r in account_results if r.get('success', False))
                    failed = len(account_results) - successful
                    total_successful += successful
                    total_failed += failed

            self.logger.info(
                f"Scheduled rebalance complete: {total_successful} successful, "
                f"{total_failed} failed, {len(skipped)} skipped"
            )

            # Clear the scheduled file after processing
            self._clear_scheduled_file()

        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in scheduled file: {e}")
            self._clear_scheduled_file()
        except Exception as e:
            self.logger.error(f"Error processing scheduled accounts: {e}")

    def _clear_scheduled_file(self):
        """Clear the scheduled file after processing."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.scheduled_file), exist_ok=True)
            # Write empty array
            with open(self.scheduled_file, 'w') as f:
                json.dump([], f)
            self.logger.info("Cleared scheduled.json file")
        except Exception as e:
            self.logger.error(f"Failed to clear scheduled file: {e}")

    def get_next_run_time(self) -> Optional[str]:
        """
        Get the next scheduled run time.

        Returns:
            ISO format string of next run time, or None if scheduler not running
        """
        if self.scheduler and self.running:
            job = self.scheduler.get_job('market_open_rebalance')
            if job and job.next_run_time:
                return job.next_run_time.isoformat()
        return None
