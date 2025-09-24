"""Trading Executor - Pure functions for subprocess execution"""

import os
import sys
import asyncio
import signal
import atexit
import logging
from datetime import datetime
from typing import List, Dict, Any
from contextlib import asynccontextmanager

def execute_strategy_batch(strategy_name: str, accounts: List[dict], event_data: dict, env: dict):
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
        return result
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

async def process_strategy_accounts(strategy_name: str, accounts: List[dict], event_data: dict):
    """Process all accounts for a strategy in parallel"""

    tasks = []
    for i, account in enumerate(accounts):
        # Deterministic client ID assignment
        client_id = 1000 + (i * 100)  # 1000, 1100, 1200, ...

        task = process_single_account(account, client_id, event_data)
        tasks.append(task)

    # Execute all accounts in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Format results
    return {
        'strategy': strategy_name,
        'timestamp': datetime.now().isoformat(),
        'event': event_data.get('exec'),
        'accounts_processed': len(accounts),
        'results': [
            {
                'account_id': acc['account_id'],
                'success': not isinstance(res, Exception),
                'error': str(res) if isinstance(res, Exception) else None,
                'details': res if not isinstance(res, Exception) else None
            }
            for acc, res in zip(accounts, results)
        ]
    }

async def process_single_account(account: dict, client_id: int, event_data: dict):
    """Process a single account with dedicated IBKR client"""

    account_id = account['account_id']
    strategy_name = account['strategy_name']

    # All logs within this context automatically include account ID
    async with account_logger_context(account_id, strategy_name) as logger:
        manager = SubprocessManager()

        async with manager.managed_ibkr_client(account_id, client_id, logger) as ibkr:
            # All IBKR operations here - connection is guaranteed to be cleaned up
            sys.path.append('/app')  # Ensure we can import our modules
            from app.trading.rebalancer import Rebalancer

            rebalancer = Rebalancer(ibkr, logger=logger)

            if event_data.get('exec') == 'rebalance':
                logger.info("Executing LIVE rebalance")
                result = await rebalancer.rebalance_account(account)
                logger.info(f"Rebalance completed: {len(result.get('orders', []))} trades executed")
                return {
                    'success': True,
                    'action': 'rebalance',
                    'trades_executed': len(result.get('orders', [])),
                    'total_value': result.get('total_value')
                }

            elif event_data.get('exec') == 'print-rebalance':
                logger.info("Calculating rebalance (preview mode)")
                result = await rebalancer.calculate_rebalance(account)

                proposed_trades = result.get('proposed_trades', [])
                logger.info(f"Preview calculated: {len(proposed_trades)} proposed trades")

                # Log each individual trade for visibility
                if proposed_trades:
                    logger.info("=== PROPOSED TRADES ===")
                    for trade in proposed_trades:
                        action = "BUY" if trade['quantity'] > 0 else "SELL"
                        logger.info(f"{action} {abs(trade['quantity'])} shares of {trade['symbol']} @ ${trade.get('price', 'MARKET')}")
                    logger.info("=====================")
                else:
                    logger.info("No trades required - portfolio is already balanced")

                return {
                    'success': True,
                    'action': 'print-rebalance',
                    'proposed_trades': len(proposed_trades),
                    'current_value': result.get('current_value'),
                    'trades_detail': proposed_trades
                }

            else:
                raise ValueError(f"Unknown command: {event_data.get('exec')}")

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
    async def managed_ibkr_client(self, account_id: str, client_id: int, logger):
        """Context manager for IBKR client with guaranteed cleanup"""

        ibkr = None
        try:
            sys.path.append('/app')  # Ensure we can import our modules
            from app.trading.ibkr_client import IBKRClient

            logger.info(f"Creating IBKR client with ID {client_id}")
            ibkr = IBKRClient(client_id=client_id, logger=logger)

            # Track active connection
            self.active_connections[account_id] = ibkr

            # Connect
            logger.info("Establishing IBKR connection")
            connected = await ibkr.connect()

            if not connected:
                raise ConnectionError("Failed to connect to IBKR Gateway")

            logger.info("IBKR connection established successfully")
            yield ibkr

        except Exception as e:
            logger.error(f"IBKR client error: {e}")
            raise

        finally:
            # Guaranteed cleanup
            if ibkr:
                try:
                    if ibkr.is_connected():
                        logger.info("Cleaning up IBKR connection")
                        await ibkr.disconnect()
                        logger.info("IBKR connection closed successfully")
                except Exception as e:
                    logger.error(f"Error during cleanup: {e}")
                finally:
                    # Remove from tracking
                    self.active_connections.pop(account_id, None)

    def _cleanup_all(self):
        """Emergency cleanup of all connections"""
        for account_id, ibkr in self.active_connections.items():
            try:
                if ibkr.is_connected():
                    # Synchronous disconnect for emergency cleanup
                    ibkr.ib.disconnect()
                    print(f"Emergency cleanup: Disconnected {account_id}")
            except Exception as e:
                print(f"Emergency cleanup failed for {account_id}: {e}")

    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        print(f"Received signal {signum}, initiating cleanup...")
        self._cleanup_all()
        sys.exit(0)