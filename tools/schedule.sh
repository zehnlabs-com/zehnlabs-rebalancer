#!/bin/bash
#
# Scheduled Rebalance Management Tool
#
# Manages accounts scheduled for market-open rebalancing.
# The event-broker will process these accounts at 9:30 AM ET on trading days.
#
# Usage:
#   ./tools/schedule.sh list                    - Show scheduled accounts
#   ./tools/schedule.sh add ACCOUNT_ID          - Add account to schedule
#   ./tools/schedule.sh remove ACCOUNT_ID       - Remove account from schedule
#   ./tools/schedule.sh clear                   - Clear all scheduled accounts
#

set -e

SCHEDULED_FILE="./data/market-open/scheduled.json"

show_usage() {
    echo "Usage: $0 <command> [ACCOUNT_ID]"
    echo ""
    echo "Commands:"
    echo "  list                Show all scheduled accounts"
    echo "  add ACCOUNT_ID      Add account to schedule"
    echo "  remove ACCOUNT_ID   Remove account from schedule"
    echo "  clear               Clear all scheduled accounts"
    echo ""
    echo "Examples:"
    echo "  $0 list"
    echo "  $0 add U12345678"
    echo "  $0 remove U12345678"
    echo "  $0 clear"
    echo ""
    echo "Notes:"
    echo "  - Accounts are processed at 9:30 AM ET on trading days"
    echo "  - Weekends and NYSE holidays are skipped"
    echo "  - Account must be configured in accounts.yaml"
}

ensure_file_exists() {
    mkdir -p "$(dirname "$SCHEDULED_FILE")"
    if [[ ! -f "$SCHEDULED_FILE" ]]; then
        echo "[]" > "$SCHEDULED_FILE"
    fi
}

validate_account_id() {
    local account_id="$1"

    # Basic format check
    if [[ ! "$account_id" =~ ^[A-Z0-9]+$ ]]; then
        echo "Error: Account ID should contain only uppercase letters and numbers"
        echo "Example: U12345678, DUM12345678"
        exit 1
    fi

    # Check if account exists in accounts.yaml
    if ! grep -q "account_id: $account_id" ./accounts.yaml 2>/dev/null; then
        echo "Error: Account '$account_id' not found in accounts.yaml"
        echo ""
        echo "Available accounts:"
        grep "account_id:" ./accounts.yaml | sed 's/.*account_id: /  /' | head -20
        exit 1
    fi
}

list_accounts() {
    ensure_file_exists

    echo "Scheduled accounts for market-open rebalance:"
    echo ""

    local count
    count=$(jq 'length' "$SCHEDULED_FILE" 2>/dev/null || echo "0")

    if [[ "$count" == "0" ]]; then
        echo "  (none)"
    else
        jq -r '.[]' "$SCHEDULED_FILE" | while read -r account_id; do
            echo "  - $account_id"
        done
    fi

    echo ""
    echo "Total: $count account(s)"
    echo ""
    echo "Next execution: 9:30 AM ET on next trading day"
}

add_account() {
    local account_id="$1"

    if [[ -z "$account_id" ]]; then
        echo "Error: ACCOUNT_ID is required"
        echo ""
        show_usage
        exit 1
    fi

    validate_account_id "$account_id"
    ensure_file_exists

    # Check if already scheduled
    if jq -e ".[] | select(. == \"$account_id\")" "$SCHEDULED_FILE" > /dev/null 2>&1; then
        echo "Account $account_id is already scheduled"
        exit 0
    fi

    # Add to array (atomic write via temp file)
    jq ". + [\"$account_id\"]" "$SCHEDULED_FILE" > "${SCHEDULED_FILE}.tmp" && \
        mv "${SCHEDULED_FILE}.tmp" "$SCHEDULED_FILE"

    echo "Added $account_id to schedule"
    echo ""
    list_accounts
}

remove_account() {
    local account_id="$1"

    if [[ -z "$account_id" ]]; then
        echo "Error: ACCOUNT_ID is required"
        echo ""
        show_usage
        exit 1
    fi

    ensure_file_exists

    # Check if account is in schedule
    if ! jq -e ".[] | select(. == \"$account_id\")" "$SCHEDULED_FILE" > /dev/null 2>&1; then
        echo "Account $account_id is not in the schedule"
        exit 0
    fi

    # Remove from array (atomic write via temp file)
    jq "map(select(. != \"$account_id\"))" "$SCHEDULED_FILE" > "${SCHEDULED_FILE}.tmp" && \
        mv "${SCHEDULED_FILE}.tmp" "$SCHEDULED_FILE"

    echo "Removed $account_id from schedule"
    echo ""
    list_accounts
}

clear_accounts() {
    ensure_file_exists
    echo "[]" > "$SCHEDULED_FILE"
    echo "Cleared all scheduled accounts"
}

# Main command handling
case "${1:-}" in
    list)
        list_accounts
        ;;
    add)
        add_account "$2"
        ;;
    remove)
        remove_account "$2"
        ;;
    clear)
        clear_accounts
        ;;
    -h|--help)
        show_usage
        exit 0
        ;;
    "")
        echo "Error: No command specified"
        echo ""
        show_usage
        exit 1
        ;;
    *)
        echo "Error: Unknown command '${1}'"
        echo ""
        show_usage
        exit 1
        ;;
esac
