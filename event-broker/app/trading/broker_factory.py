"""Factory for creating broker clients"""

import logging
from typing import Optional

try:
    from broker_connector_base import BrokerClient, AccountConfig
    from ibkr_connector import IBKRClient
except ImportError as e:
    raise ImportError(
        f"Failed to import broker packages: {e}. "
        "Ensure packages are installed."
    )

def create_broker_client(
    account_config: AccountConfig,
    client_id: int,
    logger: Optional[logging.Logger] = None
) -> BrokerClient:
    """
    Factory to create appropriate broker client.

    Args:
        account_config: Account configuration
        client_id: Unique client ID for this connection
        logger: Optional logger instance

    Returns:
        BrokerClient instance
    """
    broker = account_config.broker.lower()

    if logger:
        logger.debug(f"Creating {broker} broker client with client_id={client_id}")

    if broker == 'ibkr':
        return IBKRClient(client_id=client_id, logger=logger)
    else:
        raise ValueError(f"Unsupported broker: {broker}")
