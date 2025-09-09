"""
Notification Types for strongly typed notification details
"""
from pydantic import BaseModel
from typing import Optional


class NotificationDetails(BaseModel):
    """Base notification details common to all notification types"""
    event_id: str
    account_id: str
    strategy_name: str
    exec_command: str
    times_queued: int


class ErrorNotificationDetails(NotificationDetails):
    """Notification details for error events"""
    error_message: Optional[str] = None
    error_code: Optional[str] = None


class DelayedNotificationDetails(NotificationDetails):
    """Notification details for delayed execution events"""
    delayed_until: str
    reason: Optional[str] = None


class BasicNotificationDetails(NotificationDetails):
    """Simple notification details for basic events like start/completion"""
    pass