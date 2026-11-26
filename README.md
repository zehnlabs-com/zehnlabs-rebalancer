# Zehnlabs Rebalancer

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

The Zehnlabs Rebalancer is an automated tool for rebalancing your accounts using target allocations provided by various [Zehnlabs](https://zehnlabs.com) strategies. It is designed to run continuously and execute trades automatically based on real-time events.

**Multi-Account & Multi-Strategy Support**
The system is fully capable of managing multiple IBKR accounts under your login simultaneously. You can configure any number of accounts, and any account can be aligned with any Zehnlabs strategy that you subscribe to. 

---

### Disclaimer

**This is a community-supported project and is not an official product of Zehnlabs. It comes without any implicit or explicit warranty of any kind. Zehnlabs DOES NOT provide any support for this software.**

This reference implementation demonstrates how to automate portfolio rebalancing using the Zehnlabs API. Although fully capable of executing real trades, it is primarily an educational tool intended to show developers how to integrate the Zehnlabs API with broker trading systems.

---

## Key Considerations & Limitations

Before using this software, please understand the following limitations:

-   **Broker Support:** Currently, [**Interactive Brokers**](https://www.interactivebrokers.com) is the only supported broker.
-   **Account Requirements:** You will need an [**IBKR Pro account**](https://www.interactivebrokers.com/en/general/compare-lite-pro.php) (not commission-free). You will also need a subscription to the following IBKR market data feeds:
    -   **US Securities Snapshot and Futures Value Bundle (NP,L1):** $10.00/month (waived if monthly commissions reach $30).
    -   **Cboe One Add-On Bundle (NP,L1):** $1.00/month (waived if monthly commissions reach $5).
-   **Fractional Shares:** The IBKR TWS API **does not support trading fractional shares**. Consequently, this tool cannot trade them.
-   **Account Types:** Designed for margin and IRA accounts. In a cash account, sale proceeds may not be spendable until settlement, so buys can be rejected; the account is then retried at the next open.
-   **Dividend Reinvestment:** Due to the lack of fractional share support, you should **disable automatic dividend reinvestment** in your IBKR account settings to prevent the creation of small, untradeable positions.
-   **Order Types:** To ensure trades are filled while protecting against unfavorable prices, the system uses **`MARKET` orders for sells** and **`LIMIT` orders for buys**. Buy orders are submitted with a limit price set slightly above the current ask price to increase the likelihood of execution. This "slippage" buffer is configurable in `config.yaml` (`trading.buy_slippage_percent`).
-   **Time In Force (TIF):** All orders are **`DAY`** orders.
-   **Performance Discrepancies:** Due to these constraints (e.g., rounding to whole shares), your portfolio's performance, P&L, and other metrics may somewhat differ from the ideal.

## How It Works

The rebalancer is designed to align your configured accounts with their respective target strategies while maximizing the use of available cash.

### Two-Phase Rebalancing

To ensure cash from sales is available for purchases, the rebalancing process occurs in two distinct phases:

1.  **Sell Phase:** The system first identifies and sells any positions that must be sold according to the latest allocations. This liquidates unnecessary assets and increases your available cash balance.
2.  **Buy Phase:** After the sell phase, the system calculates the required purchases to meet the target allocations. It uses your available cash (including proceeds from the sell phase) to execute these buys.

### How a Rebalance Works

For each enabled account, one run moves the portfolio toward its strategy's target weights:

1. Targets are fetched from the allocations API, validated, and scaled to sum to exactly 100%, then replacements are applied if the account uses one ([Replacement Sets](#replacement-sets)). An empty target list means "sell everything".
2. For IBKR whole-share accounts, a portfolio-wide optimizer selects the broker-reachable holdings with the least total absolute dollar drift from all targets. It may sell one symbol to fund a closer portfolio elsewhere; exact ties prefer higher target weight, then symbol, then the lower share quantity.
3. The initial optimization determines one **market sell** wave. Its provisional buys are discarded. After those sells fill, the app reads conservative actual cash, fetches fresh quotes, and runs a buy-only optimization for the one **limit buy** wave. Preview uses the same sequence with simulated sell proceeds and places no orders.
4. A **cash reserve** is fixed once from the start-of-run account value (default 0.25%) and kept outside the strategy. The initial and post-sell optimizations use the same dollar reserve and conservative sell references or buy limits in their cash constraints.
5. At the start of each live run the app cancels any of *its own* working orders. If it finds *foreign* working orders (placed manually in TWS or the mobile app), the run stops and warns instead of trading.

Fractional-capable brokers keep the existing six-decimal, round-down planner and do not invoke the whole-share optimizer.

---

## Installation and Setup

### Prerequisites

Before getting started, ensure you have the following installed:

-   **Git** - Required to clone the repository
    -   [Install Git for Windows](https://git-scm.com/download/win)
    -   [Install Git for Mac](https://git-scm.com/download/mac)
    -   [Install Git for Linux](https://git-scm.com/download/linux)
-   **Docker Desktop** (Windows/Mac) or **Docker Engine** (Linux) - Version 4.49.0 or higher
    -   [Install Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/)
    -   [Install Docker Desktop for Mac](https://docs.docker.com/desktop/install/mac-install/)
    -   [Install Docker Engine for Linux](https://docs.docker.com/engine/install/)

**Recommended System Requirements:**
-   2GB RAM minimum, 4GB or more recommended for a high number of accounts 
-   20GB free disk space
-   1GHz CPU or better

### Step 1: Get the Code

Clone this repository to your local machine:

```bash
git clone https://github.com/zehnlabs-com/zehnlabs-rebalancer.git
cd zehnlabs-rebalancer
```

### Step 2: Configure Environment Variables

Create a `.env` file by copying the example file:

```bash
cp .env.example .env
```

Now, edit the `.env` file with your specific details:

| Variable                             | Description                                                                                                                            |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `IB_USERNAME`                        | Your Interactive Brokers username.                                                                                                     |
| `IB_PASSWORD`                        | Your Interactive Brokers password.                                                                                                     |
| `ZEHNLABS_API_KEY`                   | Your Zehnlabs API key. Send `/api` to `FintechZL_bot` in Telegram.                                                         |
| `ALLOCATIONS_BASE_URL`               | Allocation API base, including `/strategies`. Default: `https://workers.fintech.zehnlabs.com/api/v1/strategies`.                            |
| `TRADING_MODE`                       | Set to `live` for real money trading or `paper` for paper trading.                                                                       |
| `AUTO_RESTART_TIME`                  | The time (in `America/New_York` timezone) when the IBKR Gateway will automatically restart. Defaults to `10:00 PM`. E.g., `02:00 PM`.      |
| `VNC_PASSWORD`                       | A password for optional VNC access to the IBKR Gateway GUI. **Choose a strong password.**                                              |
| `USER_NOTIFICATIONS_ENABLED`         | Set to `true` to enable push notifications via ntfy.sh.                                                                                |
| `USER_NOTIFICATIONS_CHANNEL`         | A unique, hard-to-guess topic name for your ntfy.sh notifications (e.g., `my-secret-rebalancer-alerts-a1b2c3`).                         |

The following variables are pre-configured with safe defaults and do not need to be changed for a single instance. If you are running **multiple instances on the same host** (multiple IBKR logins), each instance must use unique values to avoid port and namespace conflicts.

| Variable                | Description                                                                                      | Default               |
| ----------------------- | ------------------------------------------------------------------------------------------------ | --------------------- |
| `COMPOSE_PROJECT_NAME`  | Unique project name — namespaces all Docker containers, volumes, and networks for this instance. | `zehnlabs-rebalancer` |
| `IB_LIVE_API_PORT`      | Host port for IBKR Gateway live trading API.                                                     | `4001`                |
| `IB_PAPER_API_PORT`     | Host port for IBKR Gateway paper trading API.                                                    | `4002`                |
| `VNC_PORT`              | Host port for VNC access to the IBKR Gateway GUI.                                               | `5900`                |
| `VNC_BIND_ADDRESS`      | Network interface VNC binds to. Use `127.0.0.1` to restrict to localhost.                        | `127.0.0.1`           |

### Step 3: Configure Your Accounts

Create an `accounts.yaml` file by copying the example:

```bash
cp accounts.yaml.example accounts.yaml
```

Edit `accounts.yaml` to define which IBKR accounts should be managed. You can configure multiple accounts, each with its own strategy:

```yaml
accounts:
  - account_id: U1234567  # Your IBKR account number
    type: paper            # Must match TRADING_MODE in .env
    enabled: true
    strategy_name: etf-blend-103-20 # The allocation strategy to follow
    # replacement_set: ira  # optional: use IRA-safe replacements
```

-   `account_id`: Your IBKR account number.
-   `type`: `live` or `paper`. The rebalancer will only process accounts whose `type` matches the `TRADING_MODE` set in your `.env` file.
-   `strategy_name`: The name of the allocation strategy you are subscribed to.
-   `replacement_set` (optional): The name of a built-in replacement set (today only `ira`) to use when an account cannot hold certain ETFs. See [Replacement Sets](#replacement-sets).

### Additional Configuration Files

**`config.yaml`** - Advanced trading parameters (slippage, cash reserve, timeouts, etc.)
-   Most users do not need to modify this file
-   Advanced users can optimize these settings as needed
-   The file contains inline documentation for all parameters
-   `trading.minimum_cash_reserve_percent` (default `0.25`) is the fixed cash allocation outside the strategy, calculated from the account's start-of-run value. Setting it below 0.25% may not cover commissions on large purchases of low-priced shares.
-   `trading.optimization_timeout_seconds` (default `10.0`, range `0.1`–`60.0`) is the limit for each whole-share optimizer solve stage. A timeout fails the run; an unproven incumbent is never traded.

Both files require a service restart after changes: `docker compose restart`

### Step 4: Start the Services

Run the application using Docker Compose:

```bash
docker compose up -d
```

The `-d` flag runs the services in detached mode (in the background).

### Step 5: Verify the Setup

After starting the services and approving the IBKR authentication on your mobile app, verify everything is working correctly by performing a test rebalance:

```bash
./tools/rebalance.sh -account YOUR_ACCOUNT_ID
```

This performs a **dry-run** (preview mode) that calculates trades without executing them. Check the logs to see the calculated orders:

```bash
docker compose logs -f event-broker
```

If you have `USER_NOTIFICATIONS_ENABLED=true`, you'll also receive a notification with the rebalance summary. If the preview looks correct and shows expected trades, your setup is complete!

---

## Interactive Brokers Authentication (MFA)

To connect to your Interactive Brokers account, the system needs to handle Two-Factor Authentication (2FA/MFA).

-   **Use IB Key:** You must use the **IB Key** security method via the IBKR Mobile app on your smartphone. Physical security devices or other 2FA/MFA methods are not supported by this automated setup.
-   **Initial Login:** When you start the services for the first time, you will need to approve a login notification from the IBKR Mobile app on your phone.
-   **Weekly Re-authentication:** This authentication is typically valid for about **one week**. After it expires, you will need to approve another push notification on your phone to re-establish the connection. The system is configured to automatically attempt to re-login when this happens.

---

## Triggering a Manual Rebalance

In addition to automatic rebalancing based on strategy updates, you can trigger a manual rebalance for a specific account at any time. This is useful if you have just deposited funds or want to align your portfolio with the latest target allocations immediately.

The script offers two modes:
-   **Preview Mode (`print-rebalance`):** This is the default mode. It calculates and displays the trades that *would* be made, but **does not execute them**.
-   **Execution Mode (`rebalance`):** This mode calculates and **executes the trades**.

### Usage

From the project's root directory, run the script with the following parameters:

**To preview a rebalance:**
```bash
./tools/rebalance.sh -account YOUR_ACCOUNT_ID
```
*(This is the same as running `./tools/rebalance.sh -account YOUR_ACCOUNT_ID -exec print-rebalance`)*

**To execute a rebalance:**
```bash
./tools/rebalance.sh -account YOUR_ACCOUNT_ID -exec rebalance
```

Replace `YOUR_ACCOUNT_ID` with your actual IBKR account number (e.g., `U1234567`) that is configured in `accounts.yaml`.

---

## Scheduled Rebalancing

Accounts can be scheduled to rebalance at market open (09:35 AM ET) on trading days — for example ahead of an expected deposit, or because you missed an event. The scheduler is also how failed live runs are retried: a failed run is queued and attempts once more at the next market open (see [Retries](#retries)). A run that fails during the market-open job itself is **not** retried again.

### How It Works

- The scheduler fires at 09:35 AM ET every weekday (configurable with `scheduler.market_open_time` in `config.yaml`)
- NYSE holidays and weekends are automatically skipped
- Each scheduled account runs for its strategy; if a strategy is already running when the job fires, its scheduled accounts are skipped (they are notified, not retried)
- Only the IDs that were actually processed are removed from the schedule list

### Managing Scheduled Accounts

Use the `schedule.sh` tool to manage which accounts are scheduled for market-open rebalancing:

**List scheduled accounts:**
```bash
./tools/schedule.sh list
```

**Add an account to the schedule:**
```bash
./tools/schedule.sh add U12345678
```

**Remove an account from the schedule:**
```bash
./tools/schedule.sh remove U12345678
```

**Clear all scheduled accounts:**
```bash
./tools/schedule.sh clear
```

### Notes

- Account IDs must be configured in `accounts.yaml`
- Changes take effect immediately (no restart required)
- The schedule persists across container restarts
- Requires `jq` to be installed (use WSL on Windows)

---

## Retries

A live run that fails for any reason — for example the market closed mid-run, an allocation error, or a foreign working order — is queued once and retried at the next market open (09:35 AM ET) with fresh targets and fresh account data. You get a `will retry` notification with the day and time. A run that fails during that scheduled retry is **not** retried again (you get a `not retried` notification instead). Preview runs never retry. If you have disabled the scheduler, failed live runs are not queued at all.

---

## Replacement Sets

Some accounts cannot hold certain ETFs (e.g. IRAs). A replacement set moves that exposure to an allowed instrument. Replacement sets are built into the app; there is no `replacement-sets.yaml` file to edit.

The built-in **`ira`** set:

| Replaced symbol | Replacement | Scale |
|-----------------|-------------|-------|
| `DBC` | `PDBC` | 1.0 |
| `UUP` | `USDU` | 1.0 |
| `VIXY` | `VXX` | 1.0 |
| `VIXM` | `VXZ` | 1.0 |
| `UVXY` | `VXX` | 1.5 |
| `SVXY` | `USMV` | 1.0 |

Reference a set from `accounts.yaml` with `replacement_set: ira`. When a replacement would be needed, any non-replaced symbols are trimmed to make room.

---

## Upgrading to v3.0.0

This release deliberately changes behaviour: **the first run after upgrading may trade more than usual**, because small drifts are now corrected on every run (the old "close enough" band and buy scaling are gone).

**Config keys removed from `config.yaml`:** the `replacement` and `pdt_protection` sections, and the keys `price_cache_ttl_seconds`, `synthetic_ask_offset_usd`, `minimum_cash_reserve_usd`, `commission_rate`, `max_account_utilization`, `allocation_threshold_percent`, `order_tif`, `post_completion_delay_seconds`. Remove them or start-up will fail with a message naming each one. `trading.minimum_cash_reserve_usd` and `trading.commission_rate` are replaced by `trading.minimum_cash_reserve_percent`.

**Account keys removed from `accounts.yaml`:** `cash_reserve_percent` and `pdt_protection_enabled`. Remove them or start-up will fail. The cash floor is now the global `trading.minimum_cash_reserve_percent`.

---

## Additional Information

### IBKR Login and Auto-Restart

The system uses a Docker container for the IBKR Gateway, which requires your login credentials. The `AUTO_RESTART_TIME` in the `.env` file sets a daily restart time for this gateway. This is required for maintaining a stable connection, as IBKR sessions can expire.

### Notifications with ntfy.sh

You can receive real-time notifications about rebalancing activities on your phone or desktop using [ntfy.sh](https://ntfy.sh/).

-   **Channel Security:** ntfy.sh channels are public. **Choose a long, random, and hard-to-guess channel name** for `USER_NOTIFICATIONS_CHANNEL` to keep your notifications as private as possible.
-   **Sensitive Information:** Account IDs are redacted and run IDs are omitted. Notifications still include the strategy, balances, and trade details needed to understand a run.
-   **Disabling:** You can disable notifications by setting `USER_NOTIFICATIONS_ENABLED=false` in the `.env` file.

### VNC Access

The IBKR Gateway runs with a graphical user interface (GUI) that can be accessed via a VNC client (like [RealVNC](https://www.realvnc.com/en/connect/download/viewer/) or [TightVNC](https://www.tightvnc.com/)). This is **rarely needed** but can be useful for debugging. Connect to `localhost:<VNC_PORT>` (default `localhost:5900`) using the `VNC_PASSWORD` you set in the `.env` file.

### Viewing Logs

To view the logs for the running services, use the following Docker command:

```bash
# View logs for the event-broker service
docker compose logs -f event-broker

# View logs for the IBKR gateway
docker compose logs -f ibkr-gateway
```

The `-f` flag follows the log output in real-time.

---

## Support and Contributing

### Getting Help

If you encounter issues or have questions:
-   Check the [GitHub Discussions](https://github.com/Zehnlabs-com/Zehnlabs-rebalancer/discussions) for community support
-   Search existing discussions to see if your question has been answered
-   Create a new discussion if you need help

### Contributing

Contributions are welcome! To contribute:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Make your changes and test thoroughly
4. Commit your changes (`git commit -m 'Add some feature'`)
5. Push to the branch (`git push origin feature/your-feature`)
6. Open a Pull Request

Please ensure your code follows the existing style and includes appropriate tests.

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
