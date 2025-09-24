#!/bin/bash
#
# Manual Account Rebalance Tool for Enhanced Event-Broker Architecture
#
# This script replaces the old Redis-based manual rebalancing tools.
# It works by creating a JSON file that the event-broker monitors.
#
# Usage:
#   ./tools/rebalance.sh -account ACCOUNT_ID [-exec COMMAND]
#
# Examples:
#   ./tools/rebalance.sh -account DUM959247 -exec print-rebalance
#   ./tools/rebalance.sh -account DUM959247 -exec rebalance
#

set -e

# Default values
EXEC_COMMAND="print-rebalance"
ACCOUNT_ID=""
MANUAL_EVENT_FILE="./manual-events/manual-event.json"

# Function to show usage
show_usage() {
    echo "Usage: $0 -account ACCOUNT_ID [-exec COMMAND]"
    echo ""
    echo "Options:"
    echo "  -account ACCOUNT_ID     Required. Account to rebalance (e.g., DUM959247, U123456)"
    echo "  -exec COMMAND           Optional. Command to execute:"
    echo "                            'print-rebalance' (default) - Preview trades without executing"
    echo "                            'rebalance' - Execute actual trades"
    echo ""
    echo "Examples:"
    echo "  $0 -account DUM959247"
    echo "  $0 -account DUM959247 -exec print-rebalance"
    echo "  $0 -account DUM959247 -exec rebalance"
    echo "  $0 -account U123456 -exec rebalance"
    echo ""
    echo "Notes:"
    echo "  - The event-broker container must be running"
    echo "  - Account must be configured in accounts.yaml"
    echo "  - Use 'print-rebalance' first to preview trades before executing"
    echo "  - Check event-broker logs: docker-compose logs -f event-broker"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -account)
            ACCOUNT_ID="$2"
            shift 2
            ;;
        -exec)
            EXEC_COMMAND="$2"
            shift 2
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            echo "Error: Unknown option $1"
            show_usage
            exit 1
            ;;
    esac
done

# Validate required arguments
if [[ -z "$ACCOUNT_ID" ]]; then
    echo "Error: -account is required"
    show_usage
    exit 1
fi

# Validate exec command
if [[ "$EXEC_COMMAND" != "print-rebalance" && "$EXEC_COMMAND" != "rebalance" ]]; then
    echo "Error: -exec must be either 'print-rebalance' or 'rebalance'"
    show_usage
    exit 1
fi

# Validate account ID format (basic check)
if [[ ! "$ACCOUNT_ID" =~ ^[A-Z0-9]+$ ]]; then
    echo "Error: Account ID should contain only uppercase letters and numbers"
    echo "Examples: DUM959247, U123456"
    exit 1
fi

# Check if event-broker is running
if ! docker-compose ps event-broker | grep -q "Up"; then
    echo "Error: event-broker container is not running"
    echo "Start it with: docker-compose up -d event-broker"
    exit 1
fi

# Ensure manual-events directory exists
mkdir -p manual-events

# Check if there's already a manual event file
if [[ -f "$MANUAL_EVENT_FILE" ]]; then
    echo "Warning: Manual event file already exists. Previous event may still be processing."
    echo "Continue anyway? (y/N)"
    read -r response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        echo "Cancelled."
        exit 1
    fi
    rm -f "$MANUAL_EVENT_FILE"
fi

# Build the event JSON
echo "Creating manual rebalance event..."
echo "  Account: $ACCOUNT_ID"
echo "  Command: $EXEC_COMMAND"

# Create the event JSON (account-only design)
cat > "$MANUAL_EVENT_FILE" <<EOF
{
  "account_id": "$ACCOUNT_ID",
  "exec": "$EXEC_COMMAND",
  "source": "manual",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")"
}
EOF

echo ""
echo "✅ Manual event created successfully!"
echo ""
echo "📁 Event file: $MANUAL_EVENT_FILE"
echo "📋 Event content:"
cat "$MANUAL_EVENT_FILE" | jq '.' 2>/dev/null || cat "$MANUAL_EVENT_FILE"
echo ""
echo "📊 Monitor progress with:"
echo "   docker-compose logs -f event-broker"
echo ""
echo "🔍 Check execution results in:"
echo "   ./event-broker/logs/executions/"
echo ""

# Inform about execution time
if [[ "$EXEC_COMMAND" == "rebalance" ]]; then
    echo "⚠️  LIVE TRADING INITIATED for account $ACCOUNT_ID - Monitor logs carefully!"
else
    echo "ℹ️  Preview mode for account $ACCOUNT_ID - No actual trades will be executed"
fi

echo ""
echo "The event-broker will process this file automatically within 1-2 seconds."