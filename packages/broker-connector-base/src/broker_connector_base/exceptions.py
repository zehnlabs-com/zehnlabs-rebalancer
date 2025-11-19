class BrokerConnectionError(Exception):
    """Raised when broker connection fails"""
    pass

class BrokerAPIError(Exception):
    """Raised when broker API returns an error"""
    pass

class OrderExecutionError(Exception):
    """Raised when order execution fails"""
    pass
