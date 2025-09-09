"""
Redis Data Models for strongly typed Redis operations
"""
from pydantic import BaseModel
from typing import Any, Optional
from datetime import datetime


class IBKRError(BaseModel):
    """IBKR error data stored in Redis"""
    req_id: int
    error_code: int
    error_message: str
    timestamp: datetime


class DashboardUpdateMessage(BaseModel):
    """Dashboard update message published to Redis"""
    account_id: Optional[str] = None  # None for summary updates
    update_type: str  # 'account' or 'summary'
    data: Any
    timestamp: str


class AccountConfigData(BaseModel):
    """Account configuration data from accounts.yaml"""
    account_id: str
    name: str
    replacement_set: Optional[str] = None
    strategy: Optional[str] = None