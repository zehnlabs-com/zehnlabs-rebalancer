"""
Application service for managing application lifecycle.
"""

from app.core.service_container import ServiceContainer
from app.core.signal_handler import SignalHandler
from app.logger import AppLogger

app_logger = AppLogger(__name__)


class ApplicationService:
    """Service for managing application lifecycle and orchestration"""
    
    def __init__(self):
        self.service_container = ServiceContainer()
        self.user_notification_service = None
        self.data_collector_service = None
        self.signal_handler = SignalHandler(self.stop)
        self.running = False
    
    async def start(self):
        """Start the application services"""
        app_logger.log_info("Starting application services...")
        
        try:
            # Get user notification service from container
            self.user_notification_service = self.service_container.user_notification_service()
            
            # Set up signal handlers
            self.signal_handler.setup_signal_handlers()
            
            # Start user notification service            
            await self.user_notification_service.start()
            
            # Recover any events stuck in active_events_set from previous service restart
            queue_service = self.service_container.queue_service()
            recovered_count = await queue_service.recover_stuck_active_events()
            if recovered_count > 0:
                app_logger.log_info(f"Startup recovery completed: {recovered_count} events recovered")
            
            # Start real-time portfolio event service for dashboard
            from app.services.data_collector_service import DataCollectorService
            ibkr_data_client = self.service_container.ibkr_data_client()
            redis_account_service = self.service_container.redis_account_service()
            self.data_collector_service = DataCollectorService(ibkr_data_client, redis_account_service)
            await self.data_collector_service.start_collection_tasks()  # Now starts real-time event subscriptions
            
            self.running = True
            app_logger.log_info("Application services started successfully")
            
        except Exception as e:
            app_logger.log_error(f"Failed to start application services: {e}")
            raise
    
    async def stop(self):
        """Stop the application services"""
        if not self.running:
            return            
        
        self.running = False
        
        # Stop real-time portfolio event service
        if self.data_collector_service:
            await self.data_collector_service.stop_collection_tasks()  # Now stops real-time event subscriptions
        
        # Stop user notification service
        if self.user_notification_service:
            await self.user_notification_service.stop()

        app_logger.log_info("Application services stopped successfully")
        
    
    def get_service_container(self) -> ServiceContainer:
        """Get the service container"""
        return self.service_container
    
    def is_running(self) -> bool:
        """Check if application is running"""
        return self.running