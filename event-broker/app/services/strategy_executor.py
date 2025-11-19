"""Strategy Executor - Main orchestrator for parallel account processing"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from concurrent.futures import ProcessPoolExecutor
from app_config import get_config
from .notification_service import NotificationService

class StrategyExecutor:
    """Orchestrates parallel execution of strategy trading"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.config = get_config()
        self.logger = logger or logging.getLogger(__name__)
        self.active_strategies = set()  # Simple deduplication
        self.executor = ProcessPoolExecutor(max_workers=self.config.executor.max_workers)
        self.notification_service = NotificationService(logger=logger)

    async def execute_strategy(self, strategy_name: str, accounts: List[dict], event_data: dict):
        """Execute strategy for all accounts in parallel subprocess"""

        # Deduplication - prevent double execution
        if strategy_name in self.active_strategies:
            self.logger.info(f"Strategy {strategy_name} already running")
            return {'status': 'already_running', 'strategy': strategy_name}

        self.active_strategies.add(strategy_name)

        try:
            # Track execution start time
            start_time = datetime.now()
            self.logger.info(f"Starting strategy {strategy_name} execution for {len(accounts)} accounts")

            # Execute in subprocess for complete isolation
            from .trading_executor import execute_strategy_batch
            result = await asyncio.get_event_loop().run_in_executor(
                self.executor,
                execute_strategy_batch,
                strategy_name,
                accounts,
                event_data,
                self._get_environment()
            )

            # Log summary with account-level details
            execution_time = (datetime.now() - start_time).total_seconds()
            successful_accounts = [r for r in result.get('results', []) if r.get('success', False)]
            failed_accounts = [r for r in result.get('results', []) if not r.get('success', True)]

            self.logger.info(
                f"Strategy {strategy_name} completed in {execution_time:.1f}s: "
                f"{len(successful_accounts)}/{len(accounts)} successful"
            )

            # Log failures with account context
            for failure in failed_accounts:
                self.logger.error(
                    f"Strategy {strategy_name} failed for account {failure.get('account_id')}: "
                    f"{failure.get('error', 'Unknown error')}"
                )

            # Send notifications for each account
            await self._send_account_notifications(strategy_name, result)

            return result

        except Exception as e:
            self.logger.error(f"Strategy {strategy_name} execution failed completely: {e}")
            # Return structured error response
            return {
                'status': 'failed',
                'strategy': strategy_name,
                'error': str(e),
                'accounts_affected': len(accounts),
                'timestamp': datetime.now().isoformat()
            }

        finally:
            self.active_strategies.discard(strategy_name)

    def _get_environment(self) -> Dict[str, str]:
        """Get environment variables for subprocess"""
        env_vars = {
            'TRADING_MODE': os.getenv('TRADING_MODE', 'paper'),
            'IB_HOST': os.getenv('IB_HOST', 'ibkr-gateway'),
            'ALLOCATIONS_BASE_URL': os.getenv('ALLOCATIONS_BASE_URL', 'https://fintech.zehnlabs.com/api'),
            'ALLOCATIONS_API_KEY': os.getenv('ALLOCATIONS_API_KEY', ''),
            'LOG_LEVEL': os.getenv('LOG_LEVEL', 'INFO'),
            'USER_NOTIFICATIONS_ENABLED': os.getenv('USER_NOTIFICATIONS_ENABLED', 'false'),
            'USER_NOTIFICATIONS_CHANNEL': os.getenv('USER_NOTIFICATIONS_CHANNEL', '')
        }

        # Only include IB_PORT if explicitly set (allow automatic port detection otherwise)
        ib_port = os.getenv('IB_PORT')
        if ib_port:
            env_vars['IB_PORT'] = ib_port

        return env_vars

    async def _send_account_notifications(self, strategy_name: str, result: dict):
        """Send ntfy notifications for each account result"""

        timestamp = result.get('timestamp', datetime.now().isoformat())
        operation = result.get('event', 'unknown')

        for account_result in result.get('results', []):
            account_id = account_result.get('account_id')
            success = account_result.get('success', False)
            error = account_result.get('error')
            details = account_result.get('details')

            await self.notification_service.send_account_notification(
                account_id=account_id,
                strategy_name=strategy_name,
                operation=operation,
                timestamp=timestamp,
                success=success,
                error=error,
                details=details
            )

            # Send warnings if present
            if details and details.get('warnings'):
                await self.notification_service.send_warnings(
                    account_id=account_id,
                    strategy_name=strategy_name,
                    operation=operation,
                    warnings=details.get('warnings')
                )

    def cleanup(self):
        """Cleanup resources"""
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.logger.info("Strategy executor cleaned up")
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")