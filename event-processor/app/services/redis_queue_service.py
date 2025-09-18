"""
Redis Queue Service for Event Processor
Handles all queue-related Redis operations
"""
import json
import time
import yaml
import os
import redis.asyncio as redis
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from app.services.base_redis_service import BaseRedisService
from app.models.events import EventInfo
from app.models.event_data import EventData
from app.models.redis_data import AccountConfigData
from app.config import config
from app.logger import AppLogger

app_logger = AppLogger(__name__)


class RedisQueueService(BaseRedisService):
    """Service for queue operations in Redis"""
    
    def __init__(self):
        """Initialize Redis Queue Service"""
        redis_url = f"redis://{config.redis.host}:{config.redis.port}/{config.redis.db}"
        super().__init__(redis_url=redis_url)
    
    async def dequeue_event(self, timeout: Optional[int] = None) -> Optional[EventInfo]:
        """
        Get next event from queue with timeout
        
        Args:
            timeout: Timeout in seconds, defaults to config.processing.queue_timeout
            
        Returns:
            EventInfo if available, None if timeout
        """
        try:
            timeout = timeout or config.processing.queue_timeout
            
            # For brpop, timeout is expected behavior when no events are available
            # Don't use retry logic for this operation
            client = await self._get_client()
            result = await client.brpop("rebalance_queue", timeout=timeout)
            
            if result:
                queue_name, event_json = result
                event_data_dict = json.loads(event_json)
                
                # Convert to strongly typed EventData
                event_data = EventData.from_dict(event_data_dict)
                
                # Parse datetime for EventInfo compatibility
                received_at = event_data_dict.get('received_at')
                if received_at and isinstance(received_at, str):
                    received_at = datetime.fromisoformat(received_at.replace('Z', ''))
                elif not received_at:
                    received_at = datetime.now()
                
                # Create EventInfo object
                event_info = EventInfo(
                    event_id=event_data.event_id,
                    account_id=event_data.account_id,
                    exec_command=event_data.exec_command.value,
                    status=event_data_dict.get('status', 'pending'),
                    payload=event_data.data,
                    received_at=received_at,
                    times_queued=event_data.times_queued,
                    created_at=event_data.created_at
                )
                
                app_logger.log_debug(f"Retrieved event from queue")
                return event_info
            
            return None
            
        except redis.TimeoutError:
            # Timeout is expected when no events are available
            return None
        except Exception as e:
            app_logger.log_error(f"Failed to dequeue event: {e}")
            return None
    
    async def move_to_delayed(self, event_info: EventInfo, execution_time: datetime) -> None:
        """
        Move event to delayed execution queue
        """
        try:
            # Convert EventInfo to dict format with delayed info
            event_data = self._event_info_to_dict(event_info)
            event_data['status'] = 'delayed'
            event_data['delayed_until'] = execution_time.isoformat()
            
            # Add to delayed queue (keep in active set to prevent duplicates)
            execution_timestamp = int(execution_time.timestamp())
            deduplication_key = f"{event_info.account_id}:{event_info.exec_command}"
            
            async def delay_operation(client):
                pipe = client.pipeline()
                pipe.zadd("delayed_execution_set", {json.dumps(event_data): execution_timestamp})                
                return await pipe.execute()
            
            await self.execute_with_retry(delay_operation)
            
            app_logger.log_info(f"Event delayed until {execution_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
        except Exception as e:
            app_logger.log_error(f"Failed to delay event: {e}")
            raise
    
    async def process_delayed_queue(self) -> int:
        """
        Process events ready for execution
        
        Returns:
            Number of events moved back to main queue
        """
        try:
            current_timestamp = int(time.time())
            
            async def get_ready_events(client):
                return await client.zrangebyscore("delayed_execution_set", 0, current_timestamp)
            
            ready_events = await self.execute_with_retry(get_ready_events)
            
            if not ready_events:
                return 0
            
            app_logger.log_info(f"Found {len(ready_events)} delayed events ready for execution")
            
            # Move events back to main queue
            async def move_events(client):
                pipe = client.pipeline()
                for event_json in ready_events:
                    event_data = json.loads(event_json)
                    account_id = event_data['account_id']
                    exec_command = event_data.get('exec')
                    deduplication_key = f"{account_id}:{exec_command}"
                    
                    # Reset status
                    if 'delayed_until' in event_data:
                        del event_data['delayed_until']
                    event_data['status'] = event_data.get('original_status', 'pending')
                    
                    pipe.lpush("rebalance_queue", json.dumps(event_data))
                    pipe.sadd("active_events_set", deduplication_key)
                    pipe.zrem("delayed_execution_set", event_json)
                
                return await pipe.execute()
            
            await self.execute_with_retry(move_events)
            
            app_logger.log_info(f"Moved {len(ready_events)} delayed events to main queue")
            return len(ready_events)
            
        except Exception as e:
            app_logger.log_error(f"Failed to process delayed queue: {e}")
            return 0
    
    async def remove_from_active(self, account_id: str, exec_command: Optional[str] = None) -> None:
        """Remove account+command from active events set"""
        try:
            if exec_command:
                # Remove specific account+command combination
                deduplication_key = f"{account_id}:{exec_command}"
                
                async def remove_specific(client):
                    return await client.srem("active_events_set", deduplication_key)
                
                await self.execute_with_retry(remove_specific)
                app_logger.log_debug(f"Removed {deduplication_key} from active set")
            else:
                # Remove all events for account
                async def remove_all(client):
                    active_events = await client.smembers("active_events_set")
                    keys_to_remove = [key for key in active_events if key.startswith(f"{account_id}:")]
                    if keys_to_remove:
                        return await client.srem("active_events_set", *keys_to_remove)
                    return 0
                
                removed = await self.execute_with_retry(remove_all)
                if removed:
                    app_logger.log_debug(f"Removed {removed} events for account {account_id}")
                    
        except Exception as e:
            app_logger.log_error(f"Failed to remove from active events: {e}")
            raise
    
    async def get_queue_stats(self) -> Dict[str, int]:
        """Get queue statistics"""
        try:
            async def get_stats(client):
                return {
                    'main_queue': await client.llen("rebalance_queue"),
                    'active_events': await client.scard("active_events_set"),
                    'delayed_queue': await client.zcard("delayed_execution_set")
                }
            
            return await self.execute_with_retry(get_stats)
            
        except Exception as e:
            app_logger.log_error(f"Failed to get queue stats: {e}")
            return {'main_queue': 0, 'active_events': 0, 'delayed_queue': 0}
    
    async def recover_stuck_events(self) -> int:
        """
        Recover events stuck in active_events_set after service restart
        
        Returns:
            Number of events recovered
        """
        try:
            async def get_active_events(client):
                return await client.smembers("active_events_set")
            
            active_event_keys = await self.execute_with_retry(get_active_events)
            
            if not active_event_keys:
                return 0
            
            app_logger.log_info(f"Found {len(active_event_keys)} active events during startup")
            
            recovered_count = 0
            
            async def recover_events(client):
                nonlocal recovered_count
                pipe = client.pipeline()
                
                for event_key in active_event_keys:
                    try:
                        # Parse account_id and exec_command
                        account_id, exec_command = event_key.split(':', 1)
                        
                        # Load account configuration
                        account_config = self._load_account_config(account_id)
                        
                        # Create recovery event
                        recovery_event_data = {
                            'event_id': f"recovery_{int(time.time())}_{account_id}_{exec_command}",
                            'account_id': account_id,
                            'exec': exec_command,
                            'created_at': datetime.now().isoformat(),
                            'times_queued': 1,
                            'strategy_name': account_config.strategy if account_config else '',
                            'cash_reserve_percent': 0.0,  # Default value
                            'replacement_set': account_config.replacement_set if account_config else None,
                        }
                        
                        pipe.lpush("rebalance_queue", json.dumps(recovery_event_data))
                        recovered_count += 1
                        app_logger.log_debug(f"Recovering stuck event: {event_key}")
                        
                    except Exception as e:
                        app_logger.log_error(f"Failed to recover stuck event {event_key}: {e}")
                        continue
                
                return await pipe.execute()
            
            await self.execute_with_retry(recover_events)
            
            app_logger.log_info(f"Successfully recovered {recovered_count} stuck events")
            return recovered_count
            
        except Exception as e:
            app_logger.log_error(f"Failed to recover stuck events: {e}")
            return 0
    
    def _event_info_to_dict(self, event_info: EventInfo) -> Dict[str, Any]:
        """Convert EventInfo to dictionary format using EventData"""
        # Create EventData and convert to dict
        event_data = EventData(
            event_id=event_info.event_id,
            account_id=event_info.account_id,
            exec_command=event_info.exec_command,
            times_queued=event_info.times_queued,
            created_at=event_info.created_at or datetime.now(),
            data=event_info.payload
        )
        result = event_data.model_dump()
        # Add additional fields for compatibility
        result['status'] = event_info.status
        result['received_at'] = event_info.received_at.isoformat() if event_info.received_at else None
        result['exec'] = event_info.exec_command  # Alias for backward compatibility
        
        result = self._make_json_serializable(result)
        return result
    
    def _make_json_serializable(self, obj):
        """Recursively convert datetime objects to ISO format strings for JSON serialization"""
        if isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, dict):
            return {key: self._make_json_serializable(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [self._make_json_serializable(item) for item in obj]
        else:
            return obj
    
    def _load_account_config(self, account_id: str) -> Optional[AccountConfigData]:
        """Load account configuration from accounts.yaml"""
        try:
            accounts_path = os.path.join("/app", "accounts.yaml")
            if not os.path.exists(accounts_path):
                app_logger.log_warning(f"accounts.yaml not found at {accounts_path}")
                return None
            
            with open(accounts_path, 'r') as f:
                yaml_data = yaml.safe_load(f)
            
            if not yaml_data:
                return None
            
            accounts_data = yaml_data.get('accounts', [])
            
            for account in accounts_data:
                if account.get('account_id') == account_id:
                    return AccountConfigData(
                        account_id=account_id,
                        name=account.get('name', account_id),
                        strategy=account.get('strategy_name'),
                        replacement_set=account.get('replacement_set')
                    )
            
            app_logger.log_warning(f"Account {account_id} not found in accounts.yaml")
            return None
            
        except Exception as e:
            app_logger.log_error(f"Failed to load account config for {account_id}: {e}")
            return None