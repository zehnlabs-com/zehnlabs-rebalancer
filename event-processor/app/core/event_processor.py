"""
Refactored event processor using command pattern.
"""

import asyncio
from typing import Dict
from app.core.service_container import ServiceContainer
from app.commands.base import CommandStatus
from app.models.events import EventInfo
from app.config import config
from app.logger import AppLogger

app_logger = AppLogger(__name__)


class EventProcessor:
    """Main event processing class using command pattern"""
    
    def __init__(self, service_container: ServiceContainer):
        self.service_container = service_container
        self.user_notification_service = service_container.user_notification_service()
        self.running = False
        self.delayed_processor_task = None
        self.processing_tasks = set()
        self.semaphore = None
    
    async def start_processing(self):
        """Start the event processing loop"""
        app_logger.log_info("Starting event processing loop...")
        
        try:
            self.running = True
            
            # Initialize semaphore for concurrent processing
            max_concurrent = config.processing.max_concurrent_events
            self.semaphore = asyncio.Semaphore(max_concurrent)
            app_logger.log_info(f"Event processing loop started with max {max_concurrent} concurrent events")
            
            # Start delayed event processor task
            self.delayed_processor_task = asyncio.create_task(self._delayed_event_processor())
            
            # Start main processing loop
            await self._main_loop()
            
        except Exception as e:
            app_logger.log_error(f"Failed to start event processing loop: {e}")
            raise
    
    async def stop_processing(self):
        """Stop the event processing loop"""
        if not self.running:
            return
            
        app_logger.log_info("Stopping event processing loop...")
        self.running = False
        
        # Cancel all active processing tasks
        for task in list(self.processing_tasks):
            if not task.done():
                app_logger.log_info(f"Cancelling processing task for account: {task.get_name()}")
                task.cancel()
        
        # Wait for all processing tasks to complete
        if self.processing_tasks:
            await asyncio.gather(*self.processing_tasks, return_exceptions=True)
        
        # Cancel delayed processor task
        if self.delayed_processor_task and not self.delayed_processor_task.done():
            self.delayed_processor_task.cancel()
            try:
                await self.delayed_processor_task
            except asyncio.CancelledError:
                pass
        
        app_logger.log_info("Event processing loop stopped")
    
    async def _main_loop(self):
        """Main event processing loop with concurrent processing"""
        app_logger.log_info("Starting main processing loop")
        
        queue_service = self.service_container.queue_service()
        
        while self.running:
            try:
                # Clean up completed tasks
                completed_tasks = {task for task in self.processing_tasks if task.done()}
                if completed_tasks:
                    task_names = [task.get_name() for task in completed_tasks]
                    app_logger.log_debug(f"Cleaning up {len(completed_tasks)} completed tasks for accounts: {task_names}")
                self.processing_tasks -= completed_tasks
                
                # Check if we can start more tasks
                if len(self.processing_tasks) < config.processing.max_concurrent_events:
                    app_logger.log_debug("Checking for events in queue...")
                    # Get event from queue with timeout
                    event_info = await queue_service.get_next_event()
                    
                    if event_info:
                        app_logger.log_debug(f"Starting concurrent processing for event: {event_info.event_id}", event_info)
                        # Create task for concurrent processing with account ID as name
                        task = asyncio.create_task(
                            self._process_event_with_semaphore(event_info), 
                            name=event_info.account_id
                        )
                        self.processing_tasks.add(task)
                    else:
                        app_logger.log_debug("No events available, waiting...")
                        await asyncio.sleep(5)
                else:
                    # At max capacity, wait a bit before checking again
                    await asyncio.sleep(1)
                    
            except Exception as e:
                # Get currently processing task names for context
                active_task_names = [task.get_name() for task in self.processing_tasks if not task.done()]
                app_logger.log_error(f"Error in main loop: {e}. Active tasks for accounts: {active_task_names}")
                await asyncio.sleep(10)
    
    async def process_event(self, event_info: EventInfo):
        """Process a single event using command pattern"""
        app_logger.log_debug("Processing event", event_info)
        
        queue_service = self.service_container.queue_service()
        
        try:
            # Send event started notification
            await self.user_notification_service.send_notification(event_info, 'event_started')
            
            # Times queued tracking now handled in Redis only
            
            # Get command factory and create command
            command_factory = self.service_container.command_factory()
            command = command_factory.create_command(event_info.exec_command, event_info.event_id, event_info.account_id, event_info)
            
            if not command:
                await self._handle_failed_event(event_info, f"No command handler found for: {event_info.exec_command}")
                return
            
            # Execute command with services
            services = {
                'queue_service': self.service_container.queue_service(),
                'redis_account_service': self.service_container.redis_account_service(),
                'redis_notification_service': self.service_container.redis_notification_service(),
                'ibkr_client': self.service_container.ibkr_client(),
                'rebalancer_service': self.service_container.rebalancer_service()
            }
            result = await command.execute(services)
            
            # Handle command result
            if result.status == CommandStatus.SUCCESS:
                app_logger.log_info(f"Command executed successfully: {result.message}", event_info)
                
                # Send success notification (different types for first vs retry)
                success_event_type = 'event_success_first' if event_info.times_queued <= 1 else 'event_success_retry'
                await self.user_notification_service.send_notification(event_info, success_event_type)
                
                # Remove from active events set after successful processing
                await queue_service.remove_from_queued(event_info.account_id, event_info.exec_command)
                app_logger.log_debug("Event processed successfully, removed from active events", event_info)
            else:
                app_logger.log_error(f"Command failed: {result.error}", event_info)
                await self._handle_failed_event(event_info, result.error)
                
        except Exception as e:
            app_logger.log_error(f"Error processing event {event_info.event_id}: {e}", event_info)
            await self._handle_failed_event(event_info, str(e))
    

    async def _handle_failed_event(self, event_info: EventInfo, error_message: str):
        """Handle failed events by notifying user and completing event - no retry"""
        try:
            # Send notification to user with clear instructions for manual intervention
            await self.user_notification_service.send_notification(
                event_info, 
                'event_permanent_failure', 
                {
                    'error_message': error_message,
                    'action_required': f'Please resolve the issue and manually rebalance account {event_info.account_id}'
                }
            )
            
            # Remove from active events (complete the event, no retry)
            queue_service = self.service_container.queue_service()
            await queue_service.remove_from_queued(event_info.account_id, event_info.exec_command)
            
            # Update event status
            event_info.status = 'failed'
            
            app_logger.log_error(f"Rebalance failed - manual intervention required: {error_message}", event_info)
            
        except Exception as e:
            app_logger.log_error(f"Failed to handle failed event {event_info.event_id}: {e}", event_info)
    
    
    
    async def _delayed_event_processor(self):
        """Background task to process delayed events periodically"""
        app_logger.log_info("Starting delayed event processor")
        queue_service = self.service_container.queue_service()
        
        while self.running:
            try:
                # Check every minute for delayed events ready for execution
                await asyncio.sleep(60)
                if self.running:  # Check again after sleep
                    await queue_service.process_delayed_events()
            except asyncio.CancelledError:
                app_logger.log_info("Delayed event processor cancelled")
                break
            except Exception as e:
                app_logger.log_error(f"Error in delayed event processor: {e}")
                # Continue running even if there's an error
                await asyncio.sleep(10)
        
        app_logger.log_info("Delayed event processor stopped")
    
    async def _process_event_with_semaphore(self, event_info: EventInfo):
        """Process event with semaphore to limit concurrency"""
        # Get current task name for logging
        current_task = asyncio.current_task()
        task_name = current_task.get_name() if current_task else "unknown"
        
        async with self.semaphore:
            app_logger.log_debug(f"Acquired semaphore for account {task_name}, event: {event_info.event_id}", event_info)
            try:
                await self.process_event(event_info)
            finally:
                app_logger.log_debug(f"Released semaphore for account {task_name}, event: {event_info.event_id}", event_info)