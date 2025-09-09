#!/bin/bash 

# Script to trigger rebalance commands for accounts
# This is a convenience wrapper for queue.sh with the rebalance command
# Usage: 
#   ./rebalance.sh -all              # Process all accounts
#   ./rebalance.sh -account U123456  # Process specific account
#   ./rebalance.sh --status          # Show queue status

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

show_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo "Options:"
    echo "  -all                Process all accounts from accounts.yaml"
    echo "  -account ACCOUNT_ID Process specific account"
    echo "  --status            Show queue status"
    echo "  -h, --help         Show this help message"
}

# Parse and forward arguments to queue.sh
case "$1" in
    -all|--all)
        exec "$SCRIPT_DIR/queue.sh" -command rebalance -all
        ;;
    -account|--account)
        if [[ -z "$2" ]]; then
            echo "Error: Account ID required with -account option"
            show_usage
            exit 1
        fi
        exec "$SCRIPT_DIR/queue.sh" -command rebalance -account "$2"
        ;;
    --status)
        exec "$SCRIPT_DIR/queue.sh" --status
        ;;
    -h|--help|"")
        show_usage
        ;;
    *)
        echo "Error: Unknown option $1"
        show_usage
        exit 1
        ;;
esac