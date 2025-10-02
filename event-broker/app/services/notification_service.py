"""Notification Service - Sends ntfy notifications for account rebalancing results"""

import os
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
import aiohttp


class NotificationService:
    """Handles sending notifications via ntfy for rebalancing events"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.enabled = os.getenv('USER_NOTIFICATIONS_ENABLED', 'false').lower() == 'true'
        self.channel_name = os.getenv('USER_NOTIFICATIONS_CHANNEL', '')
        self.ntfy_url = "https://ntfy.sh"

    async def send_account_notification(
        self,
        account_id: str,
        strategy_name: str,
        operation: str,
        timestamp: str,
        success: bool,
        error: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        """Send notification for an account rebalancing result"""

        self.logger.debug(f"send_account_notification called: enabled={self.enabled}, channel={self.channel_name}")

        if not self.enabled:
            self.logger.info("Notifications disabled, skipping")
            return

        if not self.channel_name:
            self.logger.warning("USER_NOTIFICATIONS_CHANNEL not set, skipping notification")
            return

        try:
            self.logger.info(f"Sending {'SUCCESS' if success else 'FAILURE'} notification for {account_id}")
            if success:
                await self._send_success_notification(
                    account_id, strategy_name, operation, timestamp, details
                )
            else:
                await self._send_failure_notification(
                    account_id, strategy_name, operation, timestamp, error
                )
            self.logger.info(f"Notification sent successfully for {account_id}")
        except Exception as e:
            self.logger.error(f"Failed to send notification for {account_id}: {e}")

    async def _send_success_notification(
        self,
        account_id: str,
        strategy_name: str,
        operation: str,
        timestamp: str,
        details: Optional[Dict[str, Any]]
    ):
        """Send success notification with snapshot data"""

        # Format timestamp
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            time_str = dt.strftime('%Y-%m-%d %H:%M:%S ET')
        except:
            time_str = timestamp

        # Build message body
        message_lines = [
            f"Account: {account_id}",
            f"Strategy: {strategy_name}",
            f"Operation: {operation}",
            f"Time: {time_str}",
            ""
        ]

        if details:
            if operation == 'rebalance':
                trades_count = details.get('trades_executed', 0)
                total_value = details.get('total_value', 0)
                cash_balance = details.get('cash_balance')
                message_lines.append(f"Trades Executed: {trades_count}")
                message_lines.append(f"Portfolio Value: ${total_value:,.2f}")
                if cash_balance is not None:
                    message_lines.append(f"Cash Balance: ${cash_balance:,.2f}")
            elif operation == 'print-rebalance':
                proposed_count = details.get('proposed_trades', 0)
                current_value = details.get('current_value', 0)
                message_lines.append(f"Proposed Trades: {proposed_count}")
                message_lines.append(f"Current Value: ${current_value:,.2f}")

        message = "\n".join(message_lines)
        title = f"✅ {operation.replace('-', ' ').title()} Success"

        await self._send_ntfy(
            title=title,
            message=message,
            priority="default",
            tags=["white_check_mark"]
        )

    async def _send_failure_notification(
        self,
        account_id: str,
        strategy_name: str,
        operation: str,
        timestamp: str,
        error: Optional[str]
    ):
        """Send failure notification with error details"""

        # Format timestamp
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            time_str = dt.strftime('%Y-%m-%d %H:%M:%S ET')
        except:
            time_str = timestamp

        # Build message body
        message_lines = [
            f"Account: {account_id}",
            f"Strategy: {strategy_name}",
            f"Operation: {operation}",
            f"Time: {time_str}",
            "",
            f"Error: {error or 'Unknown error'}"
        ]

        message = "\n".join(message_lines)
        title = f"❌ {operation.replace('-', ' ').title()} Failed"

        await self._send_ntfy(
            title=title,
            message=message,
            priority="default",
            tags=["x"]
        )

    async def send_warnings(
        self,
        account_id: str,
        strategy_name: str,
        operation: str,
        warnings: List[str]
    ):
        """Send warning notifications for an account"""

        if not self.enabled:
            self.logger.info("Notifications disabled, skipping warnings")
            return

        if not self.channel_name:
            self.logger.warning("USER_NOTIFICATIONS_CHANNEL not set, skipping warning notification")
            return

        if not warnings:
            return

        try:
            self.logger.info(f"Sending {len(warnings)} warning(s) for {account_id}")

            message_lines = [
                f"Account: {account_id}",
                f"Strategy: {strategy_name}",
                f"Operation: {operation}",
                "",
                "Warnings:",
                ""
            ]

            for i, warning in enumerate(warnings, 1):
                if len(warnings) > 1:
                    message_lines.append(f"{i}. {warning}")
                else:
                    message_lines.append(warning)
                message_lines.append("")

            message = "\n".join(message_lines)
            title = f"⚠️ {operation.replace('-', ' ').title()} Warnings"

            await self._send_ntfy(
                title=title,
                message=message,
                priority="default",
                tags=["warning"]
            )

            self.logger.info(f"Warning notification sent successfully for {account_id}")
        except Exception as e:
            self.logger.error(f"Failed to send warning notification for {account_id}: {e}")

    async def _send_ntfy(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: Optional[list] = None
    ):
        """Send notification via ntfy.sh"""

        topic = self.channel_name
        url = f"{self.ntfy_url}/{topic}"

        headers = {
            "Title": title,
            "Priority": priority,
        }

        if tags:
            headers["Tags"] = ",".join(tags)

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=message.encode('utf-8'), headers=headers) as response:
                self.logger.debug(f"ntfy response status: {response.status}")
                if response.status == 200:
                    self.logger.debug(f"Notification sent successfully to topic {topic}")
                else:
                    error_text = await response.text()
                    self.logger.error(
                        f"Failed to send notification: {response.status} - {error_text}"
                    )