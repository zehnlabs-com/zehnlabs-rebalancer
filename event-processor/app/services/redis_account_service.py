"""
Redis Account Service for Event Processor
Handles all account data operations in Redis
"""
import json
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from app.services.base_redis_service import BaseRedisService
from app.models.account_data import AccountData, DashboardSummary
from app.models.redis_data import DashboardUpdateMessage
from app.config import config
from app.logger import AppLogger

app_logger = AppLogger(__name__)


class RedisAccountService(BaseRedisService):
    """Service for account data operations in Redis"""
    
    def __init__(self):
        """Initialize Redis Account Service"""
        redis_url = f"redis://{config.redis.host}:{config.redis.port}/{config.redis.db}"
        super().__init__(redis_url=redis_url)
    
    async def update_account_data(self, account_id: str, account_data: AccountData) -> None:
        """Update account portfolio data using strongly typed AccountData"""
        try:
            async def update_operation(client):
                return await client.set(f"account:{account_id}", json.dumps(account_data.to_dict()))
            
            await self.execute_with_retry(update_operation)
            app_logger.log_debug(f"Updated account data for {account_id}")
        except Exception as e:
            app_logger.log_error(f"Failed to update account data: {e}")
            raise
    
    async def get_account_data(self, account_id: str) -> Optional[AccountData]:
        """Get account portfolio data using strongly typed AccountData"""
        try:
            async def get_operation(client):
                return await client.get(f"account:{account_id}")
            
            data = await self.execute_with_retry(get_operation)
            if data:
                data_dict = json.loads(data)
                return AccountData.from_dict(data_dict)
            return None
        except Exception as e:
            app_logger.log_error(f"Failed to get account data: {e}")
            return None
    
    async def update_last_rebalanced(self, account_id: str, timestamp: Optional[datetime] = None) -> None:
        """Update last rebalanced timestamp for account"""
        try:
            timestamp = timestamp or datetime.now(timezone.utc)
            
            async def update_timestamp(client):
                # Try both possible keys where account data might be stored
                for key in [f"account_data:{account_id}", f"account:{account_id}"]:
                    existing_data = await client.get(key)
                    if existing_data:
                        account_data = json.loads(existing_data)
                    else:
                        account_data = {'account_id': account_id}
                    
                    account_data['last_rebalanced_on'] = timestamp.isoformat()
                    await client.set(key, json.dumps(account_data))
            
            await self.execute_with_retry(update_timestamp)
            app_logger.log_info(f"Updated last_rebalanced_on for account {account_id}")
            
        except Exception as e:
            app_logger.log_error(f"Failed to update last_rebalanced timestamp: {e}")
            raise
    
    async def update_dashboard_summary(self, summary: DashboardSummary) -> None:
        """Update dashboard summary data using strongly typed DashboardSummary"""
        try:
            async def update_operation(client):
                return await client.set("dashboard:summary", json.dumps(summary.to_dict()))
            
            await self.execute_with_retry(update_operation)
            app_logger.log_debug("Updated dashboard summary")
        except Exception as e:
            app_logger.log_error(f"Failed to update dashboard summary: {e}")
            raise
    
    async def publish_dashboard_update(self, message: DashboardUpdateMessage) -> None:
        """Publish dashboard update message"""
        try:
            async def publish_operation(client):
                return await client.publish("dashboard_updates", message.model_dump_json())
            
            await self.execute_with_retry(publish_operation)
            app_logger.log_debug(f"Published dashboard update: {message.update_type}")
        except Exception as e:
            app_logger.log_error(f"Failed to publish dashboard update: {e}")
            raise
    
    async def update_collection_status(self, status: str) -> None:
        """Update data collection status"""
        try:
            async def update_operation(client):
                return await client.set("collection:status", status)
            
            await self.execute_with_retry(update_operation)
            app_logger.log_debug(f"Updated collection status to: {status}")
        except Exception as e:
            app_logger.log_error(f"Failed to update collection status: {e}")
    
    async def update_collection_timestamp(self) -> None:
        """Update data collection timestamp"""
        try:
            async def update_operation(client):
                return await client.set("collection:last_run", datetime.now(timezone.utc).isoformat())
            
            await self.execute_with_retry(update_operation)
            app_logger.log_debug("Updated collection timestamp")
        except Exception as e:
            app_logger.log_error(f"Failed to update collection timestamp: {e}")
    
    async def update_last_rebalanced(self, account_id: str) -> None:
        """Update last rebalanced timestamp for an account"""
        try:
            async def update_operation(client):
                timestamp = datetime.now(timezone.utc).isoformat()
                return await client.set(f"account:{account_id}:last_rebalanced", timestamp)
            
            await self.execute_with_retry(update_operation)
            app_logger.log_debug(f"Updated last rebalanced timestamp for account {account_id}")
        except Exception as e:
            app_logger.log_error(f"Failed to update last rebalanced for {account_id}: {e}")
            raise