"""Trading Executor - Pure functions for subprocess execution"""

import os
import sys
import asyncio
import signal
import atexit
import logging
from datetime import datetime
from typing import List
from contextlib import asynccontextmanager
from app.models import AccountConfig, AccountExecutionResult, StrategyExecutionResult
from app.services.pdt_protection_service import PDTProtectionService

def execute_strategy_batch(strategy_name: str, accounts: List[dict], event_data: dict, env: dict) -> dict:
    """
    Pure function that executes trades for all accounts in a strategy.
    Runs in subprocess for complete isolation.
    No shared state, no side effects beyond trading.
    """

    # Set up environment
    for key, value in env.items():
        if value:
            os.environ[key] = value

    # Run async execution with proper event loop policy
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            process_strategy_accounts(strategy_name, accounts, event_data)
        )
        # Convert Pydantic model to dict for subprocess serialization
        return result.model_dump()
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

def extract_client_id_from_account(account_id: str) -> int:
    """
    Extract numeric portion of IBKR account ID to use as client ID.
    E.g., 'U21240574' -> 21240574
    This ensures unique client IDs across all accounts, preventing collisions
    when multiple strategies execute in parallel.
    """
    numeric_part = ''.join(filter(str.isdigit, account_id))
    return int(numeric_part)

async def process_strategy_accounts(strategy_name: str, accounts: List[dict], event_data: dict) -> StrategyExecutionResult:
    """Process all accounts for a strategy in parallel"""

    tasks = []
    for account in accounts:
        # Extract unique client ID from account ID (e.g., 'U21240574' -> 21240574)
        # This ensures no collisions even when multiple strategies run in parallel
        client_id = extract_client_id_from_account(account['account_id'])

        task = process_single_account(account, client_id, event_data)
        tasks.append(task)

    # Execute all accounts in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Format results using Pydantic models
    return StrategyExecutionResult(
        strategy=strategy_name,
        timestamp=datetime.now().isoformat(),
        event=event_data.get('exec'),
        accounts_processed=len(accounts),
        results=[
            AccountExecutionResult(
                account_id=acc['account_id'],
                success=not isinstance(res, Exception),
                error=str(res) if isinstance(res, Exception) else None,
                details=res if not isinstance(res, Exception) else None
            )
            for acc, res in zip(accounts, results)
        ]
    )

async def process_single_account(account: dict, client_id: int, event_data: dict):
    """Process a single account with dedicated IBKR client"""
    account_config = AccountConfig(**account)
    account_id = account_config.account_id
    strategy_name = account_config.strategy_name

    async with account_logger_context(account_id, strategy_name) as logger:
        # Check PDT protection before execution
        await _check_pdt_protection(account_config, event_data, logger)

        # Execute trading operation
        manager = SubprocessManager()
        async with manager.managed_broker_client(account_config, client_id, logger) as broker_client:
            rebalancer = _create_rebalancer(account_config, broker_client, logger)
            
            exec_command = event_data.get('exec')
            if exec_command == 'rebalance':
                return await _execute_live_rebalance(rebalancer, account_config, logger)
            elif exec_command == 'print-rebalance':
                return await _execute_preview_rebalance(rebalancer, account_config, logger)
            else:
                raise ValueError(f"Unknown command: {exec_command}")

async def _check_pdt_protection(account_config: AccountConfig, event_data: dict, logger):
    """Check PDT protection rules before execution"""
    from app.services.pdt_protection_service import PDTProtectionService
    
    if not account_config.pdt_protection_enabled or event_data.get('exec') != 'rebalance':
        return

    pdt_service = PDTProtectionService(logger=logger)
    check_result = pdt_service.is_execution_allowed(account_config.account_id)

    if not check_result.allowed:
        error_msg = (
            f"PDT Protection: Account {account_config.account_id} was already rebalanced earlier today "
            f"so it was skipped to protect against PDT. "
            f"You can manually rebalance after {check_result.next_allowed_time}."
        )
        logger.warning(error_msg)
        raise Exception(error_msg)

def _create_rebalancer(account_config: AccountConfig, broker_client, logger):
    """Create appropriate rebalancer based on broker type"""
    if account_config.broker.lower() == 'ibkr':
        from ibkr_connector import IBKRRebalancer
        return IBKRRebalancer(broker_client, logger=logger)
    else:
        raise ValueError(f"Unsupported broker: {account_config.broker}")

async def _execute_live_rebalance(rebalancer, account_config: AccountConfig, logger) -> dict:
    """Execute live rebalance with real trades"""
    from app.services.pdt_protection_service import PDTProtectionService
    
    logger.info("Executing LIVE rebalance")
    result = await rebalancer.rebalance_account(account_config)

    if not result.success:
        logger.error(f"Rebalance failed: {result.error}")
        raise Exception(f"Rebalance failed: {result.error}")

    logger.info(f"Rebalance completed: {len(result.orders)} trades executed")

    # Record execution for PDT protection
    pdt_service = PDTProtectionService(logger=logger)
    pdt_service.record_execution(account_config.account_id)

    return {
        'success': True,
        'action': 'rebalance',
        'trades_executed': len(result.orders),
        'total_value': result.total_value,
        'cash_balance': result.cash_balance,
        'warnings': result.warnings
    }

async def _execute_preview_rebalance(rebalancer, account_config: AccountConfig, logger) -> dict:
    """Execute preview rebalance without real trades"""
    logger.info("Calculating rebalance (preview mode)")
    result = await rebalancer.calculate_rebalance(account_config)

    proposed_trades = result.proposed_trades
    logger.info(f"Preview calculated: {len(proposed_trades)} proposed trades")

    if proposed_trades:
        _log_proposed_trades(proposed_trades, logger)
    else:
        logger.info("No trades required - portfolio is already balanced")

    return {
        'success': True,
        'action': 'print-rebalance',
        'proposed_trades': len(proposed_trades),
        'current_value': result.current_value,
        'trades_detail': [trade.model_dump() for trade in proposed_trades],
        'warnings': result.warnings
    }

def _log_proposed_trades(proposed_trades: list, logger):
    """Log proposed trades in a readable format"""
    logger.info("=== PROPOSED TRADES ===")
    for trade in proposed_trades:
        action = "BUY" if trade.quantity > 0 else "SELL"
        logger.info(f"{action} {abs(trade.quantity)} shares of {trade.symbol} @ ${trade.price}")
    logger.info("=====================")

# Account-Level Logging Architecture

@asynccontextmanager
async def account_logger_context(account_id: str, strategy_name: str):
    """Create a logger context with account-specific formatting"""

    # Create account-specific logger
    logger = logging.getLogger(f"account.{account_id}")

    # Clear any existing handlers to prevent duplicates
    logger.handlers.clear()

    logger.setLevel(logging.INFO)
    logger.propagate = False  # Prevent duplicate logs from parent loggers

    # Add handler with account ID in every log line
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        f'[%(asctime)s] [ACCOUNT:{account_id}] [STRATEGY:{strategy_name}] '
        f'[%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    try:
        yield logger
    finally:
        # Clean up handler to prevent duplication
        logger.removeHandler(handler)

# Connection Cleanup and Resource Management

class SubprocessManager:
    """Manages subprocess lifecycle and cleanup"""

    def __init__(self):
        self.active_connections = {}

        # Register cleanup handlers
        atexit.register(self._cleanup_all)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    @asynccontextmanager
    async def managed_broker_client(self, account_config: AccountConfig, client_id: int, logger):
        """Context manager for broker client with guaranteed cleanup"""

        broker_client = None
        account_id = account_config.account_id

        try:
            sys.path.append('/app')  # Only for app.* imports
            from app.trading.broker_factory import create_broker_client

            logger.info(f"Creating broker client with ID {client_id}")
            broker_client = create_broker_client(
                account_config=account_config,
                client_id=client_id,
                logger=logger
            )

            # Track active connection
            self.active_connections[account_id] = broker_client

            # Connect
            logger.info("Establishing broker connection")
            connected = await broker_client.connect()

            if not connected:
                raise ConnectionError(f"Failed to connect to {account_config.broker.upper()} broker")

            logger.info("Broker connection established successfully")
            yield broker_client

        except Exception as e:
            logger.error(f"Broker client error: {e}")
            raise

        finally:
            # Guaranteed cleanup
            if broker_client:
                try:
                    if broker_client.is_connected():
                        logger.info("Cleaning up broker connection")
                        await broker_client.disconnect()
                        logger.info("Broker connection closed successfully")
                except Exception as e:
                    logger.error(f"Error during cleanup: {e}")
                finally:
                    # Remove from tracking
                    self.active_connections.pop(account_id, None)

    def _cleanup_all(self):
        """Emergency cleanup of all connections"""
        for account_id, broker_client in self.active_connections.items():
            try:
                if broker_client.is_connected():
                    # Synchronous disconnect for emergency cleanup
                    # For IBKR clients, access the underlying ib object
                    if hasattr(broker_client, 'ib'):
                        broker_client.ib.disconnect()
                    print(f"Emergency cleanup: Disconnected {account_id}")
            except Exception as e:
                print(f"Emergency cleanup failed for {account_id}: {e}")

    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        print(f"Received signal {signum}, initiating cleanup...")
        self._cleanup_all()
        sys.exit(0)