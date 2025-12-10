# Zehnlabs Rebalancer

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

The Zehnlabs Rebalancer is an automated tool for rebalancing your accounts using target allocations provided by various Zehnlabs strategies. It is designed to run continuously and execute trades automatically based on real-time events.

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
-   **Dividend Reinvestment:** Due to the lack of fractional share support, you should **disable automatic dividend reinvestment** in your IBKR account settings to prevent the creation of small, untradeable positions.
-   **Order Types:** To ensure trades are filled while protecting against unfavorable prices, the system uses **`MARKET` orders for sells** and **`LIMIT` orders for buys**. Buy orders are submitted with a limit price set slightly above the current ask price to increase the likelihood of execution. This "slippage" buffer is configurable in `config.yaml` (`trading.buy_slippage_percent`) and defaults to 0.5%.
-   **Time In Force (TIF):** All orders are submitted with an explicit Time In Force setting (default: `DAY`). The TIF setting is configurable in `config.yaml` (`trading.order_tif`).
-   **Performance Discrepancies:** Due to these constraints (e.g., rounding to whole shares, scaling, order execution logic), your portfolio's performance, P&L, and other metrics may somewhat differ from what you would achieve with manual trading.

## How It Works

The rebalancer is designed to align your configured accounts with their respective target strategies while maximizing the use of available cash.

### Two-Phase Rebalancing

To ensure cash from sales is available for purchases, the rebalancing process occurs in two distinct phases:

1.  **Sell Phase:** The system first identifies and sells any positions that must be sold according to the latest allocations. This liquidates unnecessary assets and increases your available cash balance.
2.  **Buy Phase:** After the sell phase, the system calculates the required purchases to meet the target allocations. It uses your available cash (including proceeds from the sell phase) to execute these buys.

### Cash Usage Maximization

The rebalancer aims to deploy as much of your available cash as possible into your target investments.

-   After calculating the ideal number of shares to buy for each asset, it checks if there is enough cash to cover all purchases.
-   If cash is limited, it scales down the buy orders proportionally to fit within your available balance.
-   If there is surplus cash, it intelligently scales up the buy orders to use the extra funds, ensuring your portfolio stays as fully invested as possible while respecting the target allocation ratios.

### Minimum Purchase Quantity

The system ensures that **at least one share is owned** for each symbol in the target allocations. This guarantees that all assets in your target allocation are represented in your portfolio, even if the ideal allocation would have been a fractional share.

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
-   1GB RAM
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
| `ALLOCATIONS_API_KEY`                | API key for fetching strategy allocations. Obtain this by sending the `/me` command to the `#FintechZL_bot` on Telegram.                 |
| `REBALANCE_EVENT_SUBSCRIPTION_API_KEY` | API key for receiving real-time rebalance events. Also obtained from the `#FintechZL_bot`.                                             |
| `TRADING_MODE`                       | Set to `live` for real money trading or `paper` for paper trading.                                                                       |
| `AUTO_RESTART_TIME`                  | The time (in `America/New_York` timezone) when the IBKR Gateway will automatically restart. Defaults to `10:00 PM`. E.g., `02:00 PM`.      |
| `VNC_PASSWORD`                       | A password for optional VNC access to the IBKR Gateway GUI. **Choose a strong password.**                                              |
| `USER_NOTIFICATIONS_ENABLED`         | Set to `true` to enable push notifications via ntfy.sh.                                                                                |
| `USER_NOTIFICATIONS_CHANNEL`         | A unique, hard-to-guess topic name for your ntfy.sh notifications (e.g., `my-secret-rebalancer-alerts-a1b2c3`).                         |

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
    cash_reserve_percent: 0.0 # Percentage of equity to reserve as buffer (0-100)
    pdt_protection_enabled: true # Prevents more than one rebalance per day
```

-   `account_id`: Your IBKR account number.
-   `type`: `live` or `paper`. The rebalancer will only process accounts whose `type` matches the `TRADING_MODE` set in your `.env` file.
-   `strategy_name`: The name of the allocation strategy you are subscribed to.
-   `cash_reserve_percent`: Percentage of equity to reserve as a buffer for quickly changing prices (0-100). Default is 0%.
-   `pdt_protection_enabled`: If `true`, the system will rebalance at most once per trading day to help avoid Pattern Day Trader (PDT) rule violations. Next allowed rebalance is at 9:30 AM ET the following day (configurable in `config.yaml`).
-   `replacement_set` (optional): If you want to trade equities different from your strategy allocations (e.g., IRA accounts with ETF restrictions), configure replacement sets in `replacement-sets.yaml` and reference them here.

### Additional Configuration Files

**`config.yaml`** - Advanced trading parameters (slippage, thresholds, timeouts, etc.)
-   Most users do not need to modify this file
-   Advanced users can optimize these settings as needed
-   The file contains inline documentation for all parameters

**`replacement-sets.yaml`** - ETF replacement mappings
-   Use this if you need to trade different equities than your strategy specifies
-   Common for IRA accounts with restrictions on certain ETFs
-   Reference a replacement set in your account config with `replacement_set: ira`

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

## Additional Information

### IBKR Login and Auto-Restart

The system uses a Docker container for the IBKR Gateway, which requires your login credentials. The `AUTO_RESTART_TIME` in the `.env` file sets a daily restart time for this gateway. This is required for maintaining a stable connection, as IBKR sessions can expire.

### Notifications with ntfy.sh

You can receive real-time notifications about rebalancing activities on your phone or desktop using [ntfy.sh](https://ntfy.sh/).

-   **Channel Security:** ntfy.sh channels are public. **Choose a long, random, and hard-to-guess channel name** for `USER_NOTIFICATIONS_CHANNEL` to keep your notifications as private as possible.
-   **Sensitive Information:** Notifications include your account numbers but **do not contain any sensitive data**.
-   **Disabling:** You can disable notifications by setting `USER_NOTIFICATIONS_ENABLED=false` in the `.env` file.

### VNC Access

The IBKR Gateway runs with a graphical user interface (GUI) that can be accessed via a VNC client (like [RealVNC](https://www.realvnc.com/en/connect/download/viewer/) or [TightVNC](https://www.tightvnc.com/)). This is **rarely needed** but can be useful for debugging. Connect to `localhost:5900` using the `VNC_PASSWORD` you set in the `.env` file.

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
