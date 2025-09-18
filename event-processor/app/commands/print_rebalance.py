"""
Print rebalance command implementation.
"""

from typing import Dict, Any
from app.commands.base import EventCommand, EventCommandResult, CommandStatus
from app.logger import AppLogger
from app.models.account_config import EventAccountConfig
from app.context import set_current_event, clear_current_event
from app.models.events import EventInfo

app_logger = AppLogger(__name__)


class PrintRebalanceCommand(EventCommand):
    """Command to print rebalance information"""
    
    def _get_command_type(self) -> str:
        return "print-rebalance"
    
    async def execute(self, services: Dict[str, Any]) -> EventCommandResult:
        """Execute print rebalance command"""

        # Set the event context for all downstream method calls
        # If event doesn't have an event_id, create a dummy one
        if not hasattr(self.event, 'event_id') or not self.event.event_id:
            # Create a proper EventInfo with dummy ID for context
            from datetime import datetime
            dummy_event = EventInfo(
                event_id="00000000-0000-0000-0000-000000000000",
                account_id=self.event.account_id,
                exec_command=getattr(self.event, 'exec_command', 'print-rebalance'),
                status=getattr(self.event, 'status', 'pending'),
                payload=getattr(self.event, 'payload', {}),
                received_at=getattr(self.event, 'received_at', datetime.now()),
                times_queued=getattr(self.event, 'times_queued', 1),
                created_at=getattr(self.event, 'created_at', datetime.now())
            )
            set_current_event(dummy_event)
        else:
            set_current_event(self.event)

        app_logger.log_info(f"Printing rebalance orders for account {self.event.account_id} (dry run)")

        try:
            rebalancer_service = services.get('rebalancer_service')
            if not rebalancer_service:
                return EventCommandResult(
                    status=CommandStatus.FAILED,
                    error="Rebalancer service not available"
                )
            
            if not self.event.payload.get('strategy_name'):
                return EventCommandResult(
                    status=CommandStatus.FAILED,
                    error=f"No strategy_name found in event payload for account {self.event.account_id}"
                )
            
            # Merge event account_id with payload data
            config_data = {
                'account_id': self.event.account_id,
                **self.event.payload
            }
            account_config = EventAccountConfig.from_dict(config_data)
            
            # Execute dry run rebalancing
            result = await rebalancer_service.dry_run_rebalance(account_config)
            
            if not result.orders:
                app_logger.log_info(f"No rebalance orders needed for account {self.event.account_id}")
            else:
                app_logger.log_info(f"Rebalance orders for account {self.event.account_id} (would execute {len(result.orders)} orders):")
                
                for order in result.orders:
                    app_logger.log_info(f"  Would {order.action} {order.shares} shares of {order.symbol} (${order.estimated_value:.2f})")
            
            return EventCommandResult(
                status=CommandStatus.SUCCESS,
                message="Print rebalance command executed successfully",
                data={"action": "print-rebalance", "orders": result.orders, "equity_info": result.equity_info}
            )
            
        except Exception as e:
            app_logger.log_error(f"Print rebalance failed: {e}")
            
            return EventCommandResult(
                status=CommandStatus.FAILED,
                error=str(e)
            )
        finally:
            # Clear the context when done
            clear_current_event()