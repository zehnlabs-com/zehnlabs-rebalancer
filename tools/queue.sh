#!/bin/bash 

# Script to enqueue commands for accounts using direct Redis access
# Uses direct Redis queue access (bypasses API authentication)
# Usage: 
#   ./queue.sh -command rebalance -all              # Process all accounts
#   ./queue.sh -command rebalance -account U123456  # Process specific account
#   ./queue.sh --status                             # Show queue status

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACCOUNTS_FILE="$SCRIPT_DIR/../accounts.yaml"
REDIS_CONTAINER="redis"

show_usage() {
    echo "Usage: $0 -command COMMAND [OPTIONS]"
    echo "Commands:"
    echo "  rebalance           Execute portfolio rebalancing"
    echo "  print-rebalance     Preview rebalancing without executing"
    echo "Options:"
    echo "  -all                Process all accounts from accounts.yaml"
    echo "  -account ACCOUNT_ID Process specific account"
    echo "  --status            Show queue status"
    echo "  --flush             Flush all Redis queues (DANGEROUS: removes all data)"
    echo "  -h, --help         Show this help message"
}

# Function to execute Redis commands
redis_exec() {
    docker exec -i "$REDIS_CONTAINER" redis-cli "$@"
}

# Function to create and enqueue event
enqueue_event() {
    local account_id="$1"
    local command="$2"
    local strategy_name="$3"
    local cash_reserve_percent="$4"
    local replacement_set="$5"
    
    # Check if already queued
    local dedup_key="${account_id}:${command}"
    if redis_exec SISMEMBER active_events_set "$dedup_key" | grep -q "1"; then
        echo "Account $account_id with command $command already queued, skipping"
        return 1
    fi
    
    # Generate event ID
    local event_id=$(uuidgen | tr '[:upper:]' '[:lower:]')
    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")
    
    # Build JSON payload - exact format that event broker produces
    # The data field contains the enhanced_payload from ably service
    local data_payload="{\"exec\":\"$command\",\"account_id\":\"$account_id\",\"strategy_name\":\"$strategy_name\",\"cash_reserve_percent\":$cash_reserve_percent"
    if [[ -n "$replacement_set" && "$replacement_set" != "null" ]]; then
        data_payload="${data_payload},\"replacement_set\":\"$replacement_set\""
    fi
    data_payload="${data_payload}}"
    
    local event_json="{\"event_id\":\"$event_id\",\"account_id\":\"$account_id\",\"exec\":\"$command\",\"times_queued\":1,\"created_at\":\"$timestamp\",\"data\":$data_payload}"
    
    # Add to queue and tracking set atomically using Redis transaction
    # Use LPUSH since processor uses BRPOP (creates LIFO queue)
    # Use individual commands to avoid heredoc JSON escaping issues
    redis_exec MULTI >/dev/null 2>&1
    redis_exec SADD active_events_set "$dedup_key" >/dev/null 2>&1
    redis_exec LPUSH rebalance_queue "$event_json" >/dev/null 2>&1
    redis_exec EXEC >/dev/null 2>&1
    
    echo "✓ Enqueued $command for account $account_id (strategy: $strategy_name, cash_reserve: ${cash_reserve_percent}%)"
    if [[ -n "$replacement_set" && "$replacement_set" != "null" ]]; then
        echo "  Using replacement set: $replacement_set"
    fi
    return 0
}

# Function to show queue status
show_status() {
    echo ""
    echo "=== Queue Status ==="
    local queue_length=$(redis_exec LLEN rebalance_queue)
    local active_count=$(redis_exec SCARD active_events_set)
    
    echo "Queue length: $queue_length"
    echo "Active events: $active_count"
    
    if [[ "$active_count" -gt 0 ]]; then
        echo ""
        echo "Active event keys:"
        redis_exec SMEMBERS active_events_set | while read -r key; do
            echo "  - $key"
        done
    fi
    echo ""
}

# Function to flush all Redis data
flush_all() {
    echo ""
    echo "WARNING: This will delete ALL data in Redis including:"
    echo "  - All queued events"
    echo "  - Active event tracking"
    echo "  - Any cached data"
    echo ""
    read -p "Are you sure you want to flush all Redis data? (yes/no): " confirm
    
    if [[ "$confirm" == "yes" ]]; then
        echo "Flushing all Redis data..."
        redis_exec FLUSHALL
        echo "✓ All Redis data has been flushed"
    else
        echo "Flush operation cancelled"
    fi
    echo ""
}

# Function to process single account
process_account() {
    local account_id="$1"
    local command="$2"
    
    # Extract account data from YAML
    local account_data=$(awk -v id="$account_id" '
        BEGIN { found=0; strategy=""; cash="0"; replacement="" }
        /^[[:space:]]*-[[:space:]]*account_id:/ {
            gsub(/^[[:space:]]*-[[:space:]]*account_id:[[:space:]]*"?/, "")
            gsub(/"?[[:space:]]*$/, "")
            if ($0 == id) found=1
            else if (found) exit
        }
        found && /^[[:space:]]+strategy_name:/ {
            gsub(/^[[:space:]]+strategy_name:[[:space:]]*"?/, "")
            gsub(/"?[[:space:]]*$/, "")
            gsub(/#.*$/, "")
            gsub(/[[:space:]]+$/, "")
            strategy = $0
        }
        found && /^[[:space:]]+cash_reserve_percent:/ {
            gsub(/^[[:space:]]+cash_reserve_percent:[[:space:]]*/, "")
            gsub(/#.*$/, "")
            gsub(/[[:space:]]*$/, "")
            cash = $0
        }
        found && /^[[:space:]]+replacement_set:/ {
            gsub(/^[[:space:]]+replacement_set:[[:space:]]*"?/, "")
            gsub(/"?[[:space:]]*$/, "")
            gsub(/#.*$/, "")
            gsub(/[[:space:]]+$/, "")
            replacement = $0
        }
        END {
            if (found && strategy != "") {
                print strategy "|" cash "|" replacement
            }
        }
    ' "$ACCOUNTS_FILE")
    
    if [[ -z "$account_data" ]]; then
        echo "Error: Account $account_id not found in accounts.yaml"
        return 1
    fi
    
    IFS='|' read -r strategy_name cash_reserve replacement_set <<< "$account_data"
    enqueue_event "$account_id" "$command" "$strategy_name" "$cash_reserve" "$replacement_set"
}

# Function to process all accounts
process_all_accounts() {
    local command="$1"
    local count=0
    local success=0
    
    echo "Processing all accounts with command: $command"
    echo "------------------------------------------------------------"
    
    # Parse accounts from YAML
    while IFS='|' read -r account_id strategy_name cash_reserve replacement_set; do
        if [[ -n "$account_id" && -n "$strategy_name" ]]; then
            ((count++))
            if enqueue_event "$account_id" "$command" "$strategy_name" "$cash_reserve" "$replacement_set"; then
                ((success++))
            fi
        fi
    done < <(awk '
        BEGIN { account_id = ""; strategy_name = ""; cash_reserve = "0"; replacement_set = "" }
        /^[[:space:]]*-[[:space:]]*account_id:/ {
            if (account_id != "" && strategy_name != "") {
                print account_id "|" strategy_name "|" cash_reserve "|" replacement_set
            }
            gsub(/^[[:space:]]*-[[:space:]]*account_id:[[:space:]]*"?/, "")
            gsub(/"?[[:space:]]*$/, "")
            account_id = $0
            strategy_name = ""
            cash_reserve = "0"
            replacement_set = ""
        }
        /^[[:space:]]+strategy_name:/ {
            gsub(/^[[:space:]]+strategy_name:[[:space:]]*"?/, "")
            gsub(/"?[[:space:]]*$/, "")
            gsub(/#.*$/, "")
            gsub(/[[:space:]]+$/, "")
            strategy_name = $0
        }
        /^[[:space:]]+cash_reserve_percent:/ {
            gsub(/^[[:space:]]+cash_reserve_percent:[[:space:]]*/, "")
            gsub(/#.*$/, "")
            gsub(/[[:space:]]*$/, "")
            cash_reserve = $0
        }
        /^[[:space:]]+replacement_set:/ {
            gsub(/^[[:space:]]+replacement_set:[[:space:]]*"?/, "")
            gsub(/"?[[:space:]]*$/, "")
            gsub(/#.*$/, "")
            gsub(/[[:space:]]+$/, "")
            replacement_set = $0
        }
        END {
            if (account_id != "" && strategy_name != "") {
                print account_id "|" strategy_name "|" cash_reserve "|" replacement_set
            }
        }
    ' "$ACCOUNTS_FILE")
    
    echo "------------------------------------------------------------"
    echo "Successfully enqueued $success/$count accounts"
}

# Parse command line arguments
COMMAND=""
ACCOUNT=""
ALL_FLAG=""
STATUS_FLAG=""
FLUSH_FLAG=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -command|--command)
            COMMAND="$2"
            shift 2
            ;;
        -account|--account)
            ACCOUNT="$2"
            shift 2
            ;;
        -all|--all)
            ALL_FLAG="1"
            shift
            ;;
        --status)
            STATUS_FLAG="1"
            shift
            ;;
        --flush)
            FLUSH_FLAG="1"
            shift
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

# Handle status request
if [[ -n "$STATUS_FLAG" ]]; then
    show_status
    exit 0
fi

# Handle flush request
if [[ -n "$FLUSH_FLAG" ]]; then
    flush_all
    exit 0
fi

# Validate command
if [[ -z "$COMMAND" ]]; then
    echo "Error: -command argument is required"
    show_usage
    exit 1
fi

if [[ "$COMMAND" != "rebalance" && "$COMMAND" != "print-rebalance" ]]; then
    echo "Error: Invalid command '$COMMAND'"
    show_usage
    exit 1
fi

# Process accounts
if [[ -n "$ALL_FLAG" ]]; then
    process_all_accounts "$COMMAND"
elif [[ -n "$ACCOUNT" ]]; then
    process_account "$ACCOUNT" "$COMMAND"
else
    echo "Error: Either -all or -account must be specified"
    show_usage
    exit 1
fi