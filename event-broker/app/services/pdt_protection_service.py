"""PDT Protection Service - Prevents pattern day trading violations"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from app_config import get_config
from app.models import PDTCheckResult


class PDTProtectionService:
    """
    Manages PDT (Pattern Day Trader) protection by tracking account executions.

    For accounts with pdt_protection_enabled=true, this service prevents
    multiple rebalances within the same trading day by tracking execution
    timestamps and enforcing a next-execution time.
    """

    def __init__(self, data_dir: str = "/app/data/last-executions", logger: Optional[logging.Logger] = None):
        """
        Initialize PDT Protection Service.

        Args:
            data_dir: Directory to store execution tracking files
            logger: Optional logger instance
        """
        self.config = get_config()
        self.data_dir = data_dir
        self.logger = logger or logging.getLogger(__name__)
        self._ensure_directory_exists()

    def _ensure_directory_exists(self):
        """Create the data directory if it doesn't exist"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            self.logger.debug(f"PDT data directory ready: {self.data_dir}")
        except Exception as e:
            self.logger.error(f"Failed to create PDT data directory: {e}")
            raise

    def is_execution_allowed(self, account_id: str) -> PDTCheckResult:
        """
        Check if execution is allowed for this account based on PDT rules.

        Args:
            account_id: The account ID to check

        Returns:
            PDTCheckResult with allowed status and optional next_allowed_time
        """
        file_path = self._get_file_path(account_id)

        # If no previous execution file exists, allow execution
        if not os.path.exists(file_path):
            self.logger.debug(f"No previous execution file for {account_id}, allowing execution")
            return PDTCheckResult(allowed=True)

        try:
            with open(file_path, 'r') as f:
                data = json.load(f)

            next_execution_str = data.get('next_execution')
            last_executed_str = data.get('last_executed')

            if not next_execution_str:
                self.logger.warning(f"Invalid PDT file for {account_id}: missing next_execution")
                return PDTCheckResult(allowed=True)  # Fail-open: allow if file is malformed

            # Parse timestamps
            next_execution = datetime.fromisoformat(next_execution_str)
            now = datetime.now()

            # Check if enough time has passed
            if now >= next_execution:
                self.logger.info(
                    f"PDT check PASSED for {account_id}: "
                    f"Last execution: {last_executed_str}, Next allowed: {next_execution_str}"
                )
                return PDTCheckResult(allowed=True)
            else:
                self.logger.warning(
                    f"PDT check BLOCKED for {account_id}: "
                    f"Last execution: {last_executed_str}, Next allowed: {next_execution_str}"
                )
                return PDTCheckResult(allowed=False, next_allowed_time=next_execution_str)

        except json.JSONDecodeError as e:
            self.logger.warning(f"Corrupt PDT file for {account_id}: {e}. Allowing execution (fail-open)")
            return PDTCheckResult(allowed=True)
        except Exception as e:
            self.logger.error(f"Error checking PDT for {account_id}: {e}. Allowing execution (fail-open)")
            return PDTCheckResult(allowed=True)

    def record_execution(self, account_id: str):
        """
        Record a successful execution and calculate next allowed execution time.

        This is called after a successful rebalance to update the execution timestamp
        and prevent duplicate executions within the same trading day.

        Args:
            account_id: The account ID that was executed
        """
        try:
            now = datetime.now()

            # Calculate next execution: tomorrow at configured market open time
            next_time_str = self.config.pdt_protection.next_execution_time
            next_hour, next_minute = map(int, next_time_str.split(':'))
            next_exec = (now + timedelta(days=1)).replace(
                hour=next_hour, minute=next_minute, second=0, microsecond=0
            )

            data = {
                "last_executed": now.isoformat(),
                "next_execution": next_exec.isoformat()
            }

            file_path = self._get_file_path(account_id)

            # Write atomically by writing to temp file first, then renaming
            temp_path = file_path + '.tmp'
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)

            # Atomic rename (overwrites existing file if present)
            os.replace(temp_path, file_path)

            self.logger.info(
                f"Recorded execution for {account_id}: "
                f"Last: {now.strftime('%Y-%m-%d %H:%M:%S')}, "
                f"Next allowed: {next_exec.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        except Exception as e:
            self.logger.error(f"Failed to record execution for {account_id}: {e}")
            # Don't raise - this is non-critical for the rebalance operation itself

    def _get_file_path(self, account_id: str) -> str:
        """
        Get the file path for an account's execution tracking file.

        Args:
            account_id: The account ID

        Returns:
            Full path to the account's JSON tracking file
        """
        return os.path.join(self.data_dir, f"{account_id}.json")
