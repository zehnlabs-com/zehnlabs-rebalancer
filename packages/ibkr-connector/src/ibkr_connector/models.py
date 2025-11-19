from pydantic import BaseModel
from datetime import datetime
from broker_connector_base import ContractPrice

class CachedPrice(BaseModel):
    """Cached price data with timestamp for TTL validation"""
    price: ContractPrice
    cached_at: datetime
