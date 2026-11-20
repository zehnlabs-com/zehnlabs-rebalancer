"""Broker errors (PRD §6.3).

Messages are user-readable and never contain credentials.
"""


class BrokerError(Exception):
    """Base class for all broker-connector errors."""


class ConnectionFailedError(BrokerError):
    """Could not establish or hold a connection to the broker."""


class AccountDataError(BrokerError):
    """Account data (cash, net liquidation, positions) is missing or invalid."""


class PricingError(BrokerError):
    """Could not determine prices for one or more symbols."""


class NoPriceError(PricingError):
    """No price available for a specific symbol."""


class CancelNotConfirmedError(BrokerError):
    """Own orders were not confirmed cancelled within the timeout."""


class OrderPlacementError(BrokerError):
    """An order could not be placed."""