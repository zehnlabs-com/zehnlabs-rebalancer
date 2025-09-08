"""
Portfolio Data Collection Service for Dashboard

This service polls IBKR account data and caches it in Redis
for dashboard consumption with real-time WebSocket updates.
"""
import asyncio
import json
import math
import os
import yaml
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.config import config
from app.logger import AppLogger
from app.services.ibkr_client import IBKRClient
from app.services.redis_account_service import RedisAccountService
from app.models.account_data import AccountData, PositionData, DashboardSummary

app_logger = AppLogger(__name__)


class DataCollectorService:
    """Service for periodic portfolio data collection and caching for dashboard"""
    
    def __init__(self, ibkr_client: IBKRClient, redis_account_service: RedisAccountService):
        self.ibkr_client = ibkr_client
        self.redis_account_service = redis_account_service
        self._collection_task: Optional[asyncio.Task] = None
        self._running = False
        self._accounts = []
        self._collection_interval = 60  # Poll every 60 seconds
        
    async def start_collection_tasks(self) -> None:
        """Start periodic data collection"""
        if self._running:
            app_logger.log_warning("Data collection already running")
            return
        
        self._running = True
        app_logger.log_info("Starting portfolio data collection service")
        
        # Load accounts from config
        self._accounts = self.load_accounts_config()
        if not self._accounts:
            app_logger.log_warning("No accounts found in accounts.yaml")
            return
        
        # Perform initial data sync to populate Redis
        await self.perform_initial_sync()
        
        # Start periodic collection task
        self._collection_task = asyncio.create_task(self._periodic_collection_loop())
        
        app_logger.log_info(f"Data collection service started for {len(self._accounts)} accounts (polling every {self._collection_interval}s)")
        
    async def stop_collection_tasks(self) -> None:
        """Stop periodic data collection"""
        self._running = False
        
        if self._collection_task:
            self._collection_task.cancel()
            try:
                await self._collection_task
            except asyncio.CancelledError:
                pass
                
        app_logger.log_info("Portfolio data collection service stopped")
    
    async def perform_initial_sync(self) -> None:
        """Perform initial data sync to populate Redis with current account data"""
        app_logger.log_debug(f"Performing initial sync for {len(self._accounts)} accounts")
        
        for account_id in self._accounts:
            try:
                await self.collect_account_data(account_id)
                await asyncio.sleep(1)  # Small delay to avoid overwhelming IBKR
            except Exception as e:
                app_logger.log_error(f"Failed to sync initial data for account {account_id}: {e}")
                continue
        
        # Publish initial dashboard summary
        await self._publish_dashboard_summary_update()
        await self.redis_account_service.update_collection_timestamp()
        await self.redis_account_service.update_collection_status("polling")
        
        app_logger.log_info("Initial data sync completed")
    
    async def _periodic_collection_loop(self) -> None:
        """Main loop for periodic data collection"""
        while self._running:
            try:
                # Wait for the next collection interval
                await asyncio.sleep(self._collection_interval)
                
                if not self._running:
                    break
                
                app_logger.log_debug(f"Starting periodic data collection for {len(self._accounts)} accounts")
                
                # Collect data for all accounts
                for account_id in self._accounts:
                    if not self._running:
                        break
                    
                    try:
                        await self.collect_account_data(account_id)
                        await asyncio.sleep(1)  # Small delay between accounts
                    except Exception as e:
                        app_logger.log_error(f"Failed to collect data for account {account_id}: {e}")
                        continue
                
                # Update dashboard summary
                await self._publish_dashboard_summary_update()
                await self.redis_account_service.update_collection_timestamp()
                
                app_logger.log_debug("Periodic data collection completed")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                app_logger.log_error(f"Error in periodic collection loop: {e}")
                await asyncio.sleep(10)  # Brief pause before retrying
                
    async def collect_account_data(self, account_id: str) -> None:
        """Collect and cache data for a single account"""
        try:
            app_logger.log_debug(f"Collecting data for account {account_id}")
            
            # Get complete portfolio snapshot with current market prices 
            portfolio_snapshot = await self.ibkr_client.get_portfolio_snapshot(account_id)
            
            # Get P&L data
            pnl_data = await self.ibkr_client.get_account_pnl(account_id)
            todays_pnl = pnl_data["daily_pnl"]
            total_upnl = pnl_data["unrealized_pnl"]
            
            # Get IRA status from accounts.yaml (based on replacement_set)
            account_config = self._accounts.get(account_id, {})
            is_ira = account_config.get("replacement_set") == "ira"
            
            # Prepare position data from portfolio snapshot
            positions = []
            invested_amount = 0.0
            
            for portfolio_pos in portfolio_snapshot.positions:
                if portfolio_pos.shares != 0:
                    cost_basis = portfolio_pos.average_cost * abs(portfolio_pos.shares)
                    unrealized_pnl_percent = (portfolio_pos.unrealized_pnl / cost_basis * 100) if cost_basis != 0 else 0
                    
                    position_data = PositionData(
                        symbol=portfolio_pos.symbol,
                        position=portfolio_pos.shares,
                        market_price=portfolio_pos.market_price,
                        market_value=portfolio_pos.market_value,
                        avg_cost=portfolio_pos.average_cost,
                        cost_basis=cost_basis,
                        unrealized_pnl=portfolio_pos.unrealized_pnl,
                        unrealized_pnl_percent=unrealized_pnl_percent,
                        weight=(portfolio_pos.market_value / portfolio_snapshot.total_value * 100) if portfolio_snapshot.total_value > 0 else 0
                    )
                    
                    positions.append(position_data)
                    invested_amount += portfolio_pos.market_value
            
            # Create strongly typed AccountData
            account_data = AccountData(
                account_id=account_id,
                account_name=account_config.get("name", account_id),
                strategy_name=account_config.get("strategy"),
                is_ira=is_ira,
                net_liquidation=portfolio_snapshot.total_value,
                cash_balance=portfolio_snapshot.cash_balance,
                todays_pnl=todays_pnl,
                todays_pnl_percent=(todays_pnl / (portfolio_snapshot.total_value - todays_pnl) * 100) if portfolio_snapshot.total_value > todays_pnl else 0,
                total_upnl=total_upnl,
                total_upnl_percent=(total_upnl / portfolio_snapshot.total_value * 100) if portfolio_snapshot.total_value > 0 else 0,
                invested_amount=invested_amount,
                cash_percent=((portfolio_snapshot.total_value - invested_amount) / portfolio_snapshot.total_value * 100) if portfolio_snapshot.total_value > 0 else 0,
                last_updated=datetime.now(timezone.utc),
                positions=positions
            )
            
            # Save to Redis
            await self.redis_account_service.update_account_data(account_id, account_data)
            
            # Publish update notification
            await self._publish_account_update(account_id)
            
            app_logger.log_debug(f"Successfully collected data for account {account_id}")
            
        except Exception as e:
            app_logger.log_error(f"Failed to collect account data for {account_id}: {e}")
            raise
            
    async def _publish_account_update(self, account_id: str) -> None:
        """Publish account update notification via Redis pub/sub"""
        try:
            message = {
                "type": "account_data_updated",
                "account_id": account_id,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            await self.redis_account_service.publish_dashboard_update(message)
            app_logger.log_debug(f"Published account update notification for {account_id}")
        except Exception as e:
            app_logger.log_error(f"Failed to publish account update: {e}")
            
    async def _publish_dashboard_summary_update(self) -> None:
        """Publish dashboard summary update notification"""
        try:
            # Calculate summary statistics
            total_value = 0
            total_pnl_today = 0
            total_accounts = 0
            
            for account_id in self._accounts:
                data = await self.redis_account_service.get_account_data(account_id)
                if data:
                    total_value += data.net_liquidation
                    total_pnl_today += data.todays_pnl
                    total_accounts += 1
            
            summary = DashboardSummary(
                total_value=total_value,
                total_pnl_today=total_pnl_today,
                total_pnl_today_percent=(total_pnl_today / (total_value - total_pnl_today) * 100) if total_value > total_pnl_today else 0,
                total_accounts=total_accounts,
                last_updated=datetime.now(timezone.utc)
            )
            
            await self.redis_account_service.update_dashboard_summary(summary)
            
            # Publish notification
            message = {
                "type": "dashboard_summary_updated",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            await self.redis_account_service.publish_dashboard_update(message)
            app_logger.log_debug("Published dashboard summary update notification")
            
        except Exception as e:
            app_logger.log_error(f"Failed to publish dashboard summary: {e}")
    
    def load_accounts_config(self) -> dict:
        """Load accounts configuration from accounts.yaml"""
        try:
            accounts_file = "accounts.yaml"
            with open(accounts_file, 'r') as f:
                config_data = yaml.safe_load(f)
                
            accounts = {}
            for account in config_data.get('accounts', []):
                if account.get('type') == 'live' and account.get('enabled', False):
                    account_id = account.get('account_id')
                    if account_id:
                        accounts[account_id] = {
                            'name': account.get('name', account_id),
                            'replacement_set': account.get('replacement_set'),
                            'strategy': account.get('strategy_name')
                        }
            
            app_logger.log_info(f"Loaded {len(accounts)} live accounts from accounts.yaml")
            return accounts
            
        except Exception as e:
            app_logger.log_error(f"Failed to load accounts configuration: {e}")
            return {}