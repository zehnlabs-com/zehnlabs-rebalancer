"""
Service container using dependency-injector for Event Processor
"""
from dependency_injector import containers, providers
from app.services.redis_queue_service import RedisQueueService
from app.services.redis_account_service import RedisAccountService
from app.services.redis_notification_service import RedisNotificationService
from app.services.redis_monitoring_service import RedisMonitoringService
from app.services.queue_service import QueueService
from app.services.ibkr_client import IBKRClient
from app.services.user_notification_service import UserNotificationService
from app.commands.factory import CommandFactory


class ServiceContainer(containers.DeclarativeContainer):
    """DI Container for Event Processor service"""
    
    # Configuration
    config = providers.Configuration()
    
    # Redis Services (Singletons)
    redis_queue_service = providers.Singleton(
        RedisQueueService
    )
    
    redis_account_service = providers.Singleton(
        RedisAccountService
    )
    
    redis_notification_service = providers.Singleton(
        RedisNotificationService
    )
    
    redis_monitoring_service = providers.Singleton(
        RedisMonitoringService
    )
    
    # User notification service
    user_notification_service = providers.Singleton(
        UserNotificationService,
        redis_notification_service=redis_notification_service
    )
    
    # Queue service with dependencies
    queue_service = providers.Singleton(
        QueueService,
        redis_queue_service=redis_queue_service,
        user_notification_service=user_notification_service
    )    
    
    # Rebalancing client
    ibkr_rebalance_client = providers.Singleton(
        IBKRClient,
        service_container=providers.Self(),
        client_id_range=(1000, 1999)
    )
    
    # Data collection client
    ibkr_data_client = providers.Singleton(
        IBKRClient,
        service_container=providers.Self(),
        client_id_range=(2000, 2999)
    )
    
    # Command factory
    command_factory = providers.Singleton(
        CommandFactory,
        service_container=providers.Self()
    )
    
    # Rebalancer service (lazy initialization for optional import)
    rebalancer_service = providers.Singleton(
        providers.Callable(
            lambda ibkr_rebalance_client: _get_rebalancer_service(ibkr_rebalance_client)
        ),
        ibkr_rebalance_client=ibkr_rebalance_client
    )


def _get_rebalancer_service(ibkr_client):
    """Lazy loader for rebalancer service to handle optional import"""
    try:
        from app.services.rebalancer_service import RebalancerService
        return RebalancerService(ibkr_client)
    except ImportError as e:
        raise RuntimeError(f"Failed to initialize critical financial services: {e}")