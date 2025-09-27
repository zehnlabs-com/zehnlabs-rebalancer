"""Allocation service for fetching target allocations from API"""

import os
import json
import aiohttp
import logging
from typing import List, Optional
from app.models import AllocationItem, AccountConfig

class AllocationService:
    """Service for fetching target allocations from the API"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.base_url = os.getenv('ALLOCATIONS_BASE_URL', 'https://fintech.zehnlabs.com/api')
        self.api_key = os.getenv('ALLOCATIONS_API_KEY')

    async def get_allocations(self, account_config: AccountConfig) -> List[AllocationItem]:
        """Fetch target allocations for a strategy"""

        strategy_name = account_config.strategy_name
        allocations_url = f"{self.base_url}/{strategy_name}/allocations"

        headers = {}
        if self.api_key:
            headers['x-api-key'] = self.api_key

        self.logger.debug(f"Retrieving allocations from {allocations_url}")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    allocations_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:

                    if response.status != 200:
                        response_text = await response.text()
                        raise Exception(f"API returned status {response.status}: {response_text}")

                    data = await response.json()

                    if not isinstance(data, dict):
                        raise ValueError("API response must be a JSON object")

                    if data.get("status") != "success":
                        raise ValueError(f"API returned error status: {data.get('status', 'unknown')}")

                    response_data = data.get("data", {})
                    allocations_list = response_data.get("allocations", [])

                    if not isinstance(allocations_list, list):
                        raise ValueError("Allocations must be a list")

                    allocations = [
                        AllocationItem(
                            symbol=item.get('ticker', item.get('symbol')),
                            allocation=float(item.get('allocation', 0))
                        )
                        for item in allocations_list
                    ]

                    self.logger.info(f"Retrieved {len(allocations)} allocations for {strategy_name}")
                    return allocations

        except Exception as e:
            self.logger.error(f"Failed to get allocations: {e}")
            raise