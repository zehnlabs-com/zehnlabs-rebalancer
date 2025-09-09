# -*- coding: utf-8 -*-
"""
User Notification Service with Redis-based notification queue
"""
import json
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import aiohttp
from dataclasses import dataclass
from app.config import config
from app.logger import AppLogger
from app.models.events import EventInfo
from app.services.redis_notification_service import RedisNotificationService
from app.models.notification_data import NotificationData, NotificationType
from app.models.notification_types import (
    NotificationDetails, ErrorNotificationDetails, DelayedNotificationDetails, BasicNotificationDetails
)

app_logger = AppLogger(__name__)



class UserNotificationService:
    """
    Redis-based user notification service
    Stores notifications in a global queue with automatic cleanup
    """
    
    def __init__(self, service_container=None, redis_notification_service=None):
        self.service_container = service_container
        self.redis_notification_service = redis_notification_service
        self.cleanup_task = None
        self.running = False
        
        # Notification settings from config
        self.enabled = config.user_notification.enabled
        self.management_api_url = "http://management-service:8000"
    
    async def start(self):
        """Start the user notification service and background cleanup task"""
        if not self.enabled:
            app_logger.log_info("Notifications disabled, skipping start")
            return
        
        # Validate RedisNotificationService is available
        if not self.redis_notification_service:
            app_logger.log_error("UserNotificationService requires a RedisNotificationService")
            raise RuntimeError("UserNotificationService requires a RedisNotificationService")
            
        app_logger.log_info("Starting notification service")
        self.running = True
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
    
    async def stop(self):
        """Stop the user notification service"""
        if not self.running:
            return
            
        app_logger.log_info("Stopping notification service")
        self.running = False
        
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
    
    async def notify_event_started(self, event: EventInfo):
        """Queue notification for event start"""
        details = BasicNotificationDetails(
            event_id=event.event_id,
            account_id=event.account_id,
            strategy_name=event.payload.get('strategy_name', 'unknown'),
            exec_command=event.exec_command,
            times_queued=event.times_queued
        )
        await self._queue_notification_internal(event, 'event_started', details)
    
    async def notify_event_completed(self, event: EventInfo):
        """Queue notification for successful event completion on first try"""
        details = BasicNotificationDetails(
            event_id=event.event_id,
            account_id=event.account_id,
            strategy_name=event.payload.get('strategy_name', 'unknown'),
            exec_command=event.exec_command,
            times_queued=event.times_queued
        )
        await self._queue_notification_internal(event, 'event_success_first', details)
    
    async def notify_event_execution_delayed(self, event: EventInfo, delayed_until: str):
        """Queue notification for delayed event with specific delay time"""
        details = DelayedNotificationDetails(
            event_id=event.event_id,
            account_id=event.account_id,
            strategy_name=event.payload.get('strategy_name', 'unknown'),
            exec_command=event.exec_command,
            times_queued=event.times_queued,
            delayed_until=delayed_until
        )
        await self._queue_notification_internal(event, 'event_delayed', details)
    
    async def notify_event_connection_error(self, event: EventInfo, error_message: Optional[str] = None):
        """Queue notification for connection error"""
        details = ErrorNotificationDetails(
            event_id=event.event_id,
            account_id=event.account_id,
            strategy_name=event.payload.get('strategy_name', 'unknown'),
            exec_command=event.exec_command,
            times_queued=event.times_queued,
            error_message=error_message
        )
        await self._queue_notification_internal(event, 'event_connection_error', details)
    
    async def notify_event_critical_error(self, event: EventInfo, error_message: Optional[str] = None):
        """Queue notification for critical error"""
        details = ErrorNotificationDetails(
            event_id=event.event_id,
            account_id=event.account_id,
            strategy_name=event.payload.get('strategy_name', 'unknown'),
            exec_command=event.exec_command,
            times_queued=event.times_queued,
            error_message=error_message
        )
        await self._queue_notification_internal(event, 'event_critical_error', details)
    
    async def send_notification(self, event_info: EventInfo, event_type: str, extra_details: Optional[Dict[str, Any]] = None):
        """Route notification to appropriate method based on event type"""
        try:
            if event_type == 'event_started':
                await self.notify_event_started(event_info)
            elif event_type == 'event_success_first':
                await self.notify_event_completed(event_info)
            elif event_type == 'event_delayed':
                delayed_until = event_info.payload.get('delayed_until', 'unknown')
                await self.notify_event_execution_delayed(event_info, delayed_until)
            elif event_type == 'event_connection_error':
                error_message = extra_details.get('error_message') if extra_details else None
                await self.notify_event_connection_error(event_info, error_message)
            elif event_type == 'event_critical_error':
                error_message = extra_details.get('error_message') if extra_details else None
                await self.notify_event_critical_error(event_info, error_message)
            elif event_type == 'event_permanent_failure':
                details = ErrorNotificationDetails(
                    event_id=event_info.event_id,
                    account_id=event_info.account_id,
                    strategy_name=event_info.payload.get('strategy_name', 'unknown'),
                    exec_command=event_info.exec_command,
                    times_queued=event_info.times_queued,
                    error_message=extra_details.get('error_message') if extra_details else None
                )
                await self._queue_notification_internal(event_info, event_type, details)
            elif event_type == 'event_partial_execution_suspected':
                details = ErrorNotificationDetails(
                    event_id=event_info.event_id,
                    account_id=event_info.account_id,
                    strategy_name=event_info.payload.get('strategy_name', 'unknown'),
                    exec_command=event_info.exec_command,
                    times_queued=event_info.times_queued,
                    error_message=extra_details.get('error_message') if extra_details else None
                )
                await self._queue_notification_internal(event_info, event_type, details)
            else:
                app_logger.log_warning(f"Unknown event type for notification: {event_type}")
            
        except Exception as e:
            app_logger.log_warning(f"Failed to send notification: {e}")
    
    async def _queue_notification_internal(self, event: EventInfo, event_type: str, details: NotificationDetails):
        """
        Internal method to queue a notification in the global notification queue
        """
        if not self.enabled:
            return
            
        try:
            # Use the provided typed details
            
            # Create strongly typed notification
            timestamp = datetime.now()
            
            # Convert event_type string to NotificationType enum
            notification_type = None
            try:
                notification_type = NotificationType(event_type)
            except ValueError:
                notification_type = NotificationType.SYSTEM_INFO
            
            notification_data = NotificationData(
                account_id=event.account_id,
                strategy_name=details.strategy_name,
                event_type=notification_type,
                message=self._format_event_message(event_type, details, timestamp),
                markdown_body=self._format_markdown_body(event_type, details, timestamp),
                created_at=timestamp
            )
            
            # Queue notification via Redis data service
            await self.redis_notification_service.queue_notification(notification_data)
            
            app_logger.log_debug(f"Queued {event_type} notification for account {event.account_id}")
            
        except Exception as e:
            app_logger.log_error(f"Failed to queue notification: {e}")
    
    
    def _format_event_message(self, event_type: str, details: NotificationDetails, timestamp: Optional[datetime] = None) -> str:
        """Format concise event message for notification title"""
        strategy_name = details.strategy_name
        time_str = (timestamp or datetime.now()).strftime('%H:%M:%S')
        
        event_formats = {
            'event_started': f"Rebalance started for {strategy_name} at {time_str}",
            'event_success_first': f"Rebalance completed for {strategy_name} at {time_str}",
            'event_delayed': f"Rebalance delayed until {getattr(details, 'delayed_until', 'unknown')} for {strategy_name} at {time_str}",
            'event_connection_error': f"Connection error for {strategy_name} at {time_str}",
            'event_critical_error': f"Critical error for {strategy_name} at {time_str}",
            'event_permanent_failure': f"Rebalance failed for {strategy_name} at {time_str} - Manual rebalance required",
            'event_partial_execution_suspected': f"Partial execution suspected for {strategy_name} at {time_str}"
        }
        
        return event_formats.get(event_type, f"Event {event_type} for {strategy_name} at {time_str}")
    
    def _format_markdown_body(self, event_type: str, details: NotificationDetails, timestamp: Optional[datetime] = None) -> str:
        """Format detailed markdown body for notification"""
        account_id = details.account_id
        strategy_name = details.strategy_name
        exec_command = details.exec_command
        times_queued = details.times_queued
        time_str = (timestamp or datetime.now()).strftime('%H:%M:%S')
        
        # Base information
        body = f"**Account:** {account_id}\n"
        body += f"**Strategy:** {strategy_name}\n"
        body += f"**Command:** {exec_command}\n"
        body += f"**Time:** {time_str}\n"
        
        # Add event-specific details
        if event_type == 'event_delayed':
            delayed_until = getattr(details, 'delayed_until', 'unknown')
            body += f"**Delayed Until:** {delayed_until}\n"
        elif event_type in ['event_connection_error', 'event_critical_error']:
            error_msg = getattr(details, 'error_message', 'No error details available')
            body += f"**Error:** {error_msg}\n"
        elif event_type in ['event_permanent_failure', 'event_partial_execution_suspected']:
            error_msg = getattr(details, 'error_message', 'No error details available')
            action_required = getattr(details, 'action_required', f'Please manually rebalance account {account_id}')
            body += f"**Error:** {error_msg}\n"
            body += f"**Action Required:** {action_required}\n"
            
        return body
    
    async def _cleanup_loop(self):
        """Background task to periodically cleanup old notifications"""
        while self.running:
            try:
                # Sleep for 1 hour between cleanups
                await asyncio.sleep(3600)
                
                # Clean up old notifications
                await self._cleanup_old_notifications()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                app_logger.log_error(f"Error in cleanup loop: {e}")
                # Sleep a bit before retrying
                await asyncio.sleep(60)
    
    async def _cleanup_old_notifications(self):
        """Clean up old notifications (older than 24 hours)"""
        try:
            count = await self.redis_notification_service.cleanup_old_notifications(24)
            if count > 0:
                app_logger.log_info(f"Cleaned up {count} old notifications")
        except Exception as e:
            app_logger.log_error(f"Failed to cleanup old notifications: {e}")