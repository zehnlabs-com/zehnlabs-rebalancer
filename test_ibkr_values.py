#!/usr/bin/env python3
"""
Test script to inspect IBKR account values structure
This will help us understand exactly what data IBKR returns
"""

import asyncio
import os
import sys
from ib_async import IB

async def inspect_account_values():
    """Inspect account values from IBKR"""

    ib = IB()

    try:
        # Connect to paper trading by default
        port = 4004  # Paper trading port
        host = 'localhost'

        print(f"Connecting to IBKR Gateway at {host}:{port}...")
        await ib.connectAsync(host=host, port=port, clientId=9999)

        print("Connected successfully!\n")

        # Get all managed accounts
        accounts = ib.managedAccounts()
        print(f"Managed accounts: {accounts}\n")

        if not accounts:
            print("No accounts found!")
            return

        account_id = accounts[0]  # Use first account
        print(f"Inspecting account: {account_id}\n")

        # Get all account values
        print("=" * 60)
        print("ALL ACCOUNT VALUES:")
        print("=" * 60)
        account_values = ib.accountValues(account=account_id)

        # Group by tag for easier reading
        values_by_tag = {}
        for av in account_values:
            if av.tag not in values_by_tag:
                values_by_tag[av.tag] = []
            values_by_tag[av.tag].append(av)

        # Print important values first
        important_tags = [
            'NetLiquidation',
            'TotalCashValue',
            'CashBalance',
            'AvailableFunds',
            'BuyingPower',
            'GrossPositionValue',
            'ExcessLiquidity',
            'SettledCash'
        ]

        print("\nIMPORTANT VALUES:")
        for tag in important_tags:
            if tag in values_by_tag:
                for av in values_by_tag[tag]:
                    print(f"  {tag}: {av.value} {av.currency}")

        # Print all other values
        print("\nALL OTHER VALUES:")
        for tag, values in sorted(values_by_tag.items()):
            if tag not in important_tags:
                for av in values:
                    print(f"  {tag}: {av.value} {av.currency}")

        # Get portfolio
        print("\n" + "=" * 60)
        print("PORTFOLIO POSITIONS:")
        print("=" * 60)
        portfolio = ib.portfolio(account=account_id)

        total_positions_value = 0
        for item in portfolio:
            print(f"\n{item.contract.symbol}:")
            print(f"  Position: {item.position}")
            print(f"  Market Price: ${item.marketPrice}")
            print(f"  Market Value: ${item.marketValue}")
            print(f"  Average Cost: ${item.averageCost}")
            total_positions_value += item.marketValue

        print(f"\nTotal Positions Value: ${total_positions_value:,.2f}")

        # Calculate what our code is doing wrong
        print("\n" + "=" * 60)
        print("CALCULATION ANALYSIS:")
        print("=" * 60)

        # Find NetLiquidation
        net_liq = None
        total_cash = None

        for av in account_values:
            if av.tag == 'NetLiquidation' and av.currency == 'USD':
                net_liq = float(av.value)
            elif av.tag == 'TotalCashValue' and av.currency == 'USD':
                total_cash = float(av.value)

        if net_liq and total_cash is not None:
            print(f"NetLiquidation (actual account value): ${net_liq:,.2f}")
            print(f"TotalCashValue: ${total_cash:,.2f}")
            print(f"Total Positions Value: ${total_positions_value:,.2f}")
            print(f"\nCurrent buggy calculation:")
            print(f"  positions ({total_positions_value:,.2f}) + cash ({total_cash:,.2f}) = {total_positions_value + total_cash:,.2f}")
            print(f"\nCorrect calculation:")
            print(f"  NetLiquidation already includes everything: ${net_liq:,.2f}")
            print(f"\nImplied actual cash (NetLiq - Positions):")
            print(f"  ${net_liq:,.2f} - ${total_positions_value:,.2f} = ${net_liq - total_positions_value:,.2f}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("\nDisconnected from IBKR")

if __name__ == "__main__":
    asyncio.run(inspect_account_values())