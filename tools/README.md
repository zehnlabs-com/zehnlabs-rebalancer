# Manual Rebalancing Tools

This directory contains scripts for manually triggering rebalancing in the enhanced event-broker architecture.

## Scripts

### `rebalance.sh`
Account-based manual rebalancing script with validation and safety checks.

**Usage:**
```bash
./tools/rebalance.sh -account ACCOUNT_ID [-exec COMMAND]
```

**Examples:**
```bash
# Preview trades for specific account (default behavior)
./tools/rebalance.sh -account U123456

# Execute live trades for specific account
./tools/rebalance.sh -account U123456 -exec rebalance
```

## How It Works

1. **File-Based Events**: Scripts create JSON files in `data/manual-rebalance/` directory
2. **Event-Broker Monitoring**: The event-broker watches for these files every second
3. **Automatic Processing**: Files are processed immediately and deleted
4. **Account Processing**: Direct account-based execution (no strategy grouping)

## Event File Format

```json
{
  "account_id": "U123456",
  "exec": "print-rebalance",
  "source": "manual",
  "timestamp": "2025-01-15T10:00:00.000Z"
}
```

**Fields:**
- `account_id` (required): Account ID to process
- `exec` (optional): `"print-rebalance"` (preview) or `"rebalance"` (execute)
- `source` (optional): Source identifier for logging
- `timestamp` (optional): Event timestamp

## Monitoring

**View real-time logs:**
```bash
docker-compose logs -f event-broker
```

**Monitor all services:**
```bash
docker-compose logs -f
```

## Safety Notes

1. **Preview First**: Always use `print-rebalance` before `rebalance`
2. **Monitor Logs**: Watch logs during execution
3. **One at a Time**: Only one manual event per strategy at a time
4. **Container Running**: Ensure event-broker container is running

## Migration from Old Redis Tools

| Old Redis Tool | New Account-Based Tool |
|----------------|------------------------|
| `./tools/rebalance.sh -account U123456` | `./tools/rebalance.sh -account U123456 -exec rebalance` |
| `./tools/enqueue.sh -command print-rebalance -account U123456` | `./tools/rebalance.sh -account U123456` |

**Key Differences:**
- No Redis dependency
- Account-based processing only (no strategy grouping)
- File-based triggering instead of queue-based
- Immediate processing instead of worker polling
- Single account per execution for better isolation