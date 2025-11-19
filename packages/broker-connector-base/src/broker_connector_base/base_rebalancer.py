from abc import ABC, abstractmethod
from typing import Optional
import logging
from .base_client import BrokerClient
from .models import RebalanceResult, CalculateRebalanceResult

class BaseRebalancer(ABC):
    """Base rebalancer class with common functionality"""

    def __init__(self, broker_client: BrokerClient, logger: Optional[logging.Logger] = None):
        self.broker = broker_client
        self.logger = logger or logging.getLogger(__name__)

    @abstractmethod
    async def rebalance_account(self, account_config) -> RebalanceResult:
        """Execute live rebalancing for account"""
        pass

    @abstractmethod
    async def calculate_rebalance(self, account_config) -> CalculateRebalanceResult:
        """Calculate rebalance without executing (preview mode)"""
        pass
