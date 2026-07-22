#!/usr/bin/env python3
"""
cli.py

Command-line entry point for the Binance Futures Testnet trading bot.

Usage examples:
    python cli.py --symbol BTCUSDT --side BUY --type MARKET --quantity 0.01

    python cli.py --symbol BTCUSDT --side SELL --type LIMIT \
        --quantity 0.01 --price 60000

    python cli.py --symbol BTCUSDT --side BUY --type STOP_LIMIT \
        --quantity 0.01 --price 65000 --stop-price 64500

API credentials are read from environment variables (see README.md):
    BINANCE_TESTNET_API_KEY
    BINANCE_TESTNET_API_SECRET
"""

import argparse
import os
import sys

from bot.client import BinanceClientError, BinanceNetworkError, FuturesTestnetClient
from bot.logging_config import setup_logger
from bot.orders import place_order


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading_bot",
        description="Simplified trading bot for Binance Futures Testnet (USDT-M).",
    )
    parser.add_argument("--symbol", required=True, help="Trading pair, e.g. BTCUSDT")
    parser.add_argument("--side", required=True, choices=["BUY", "SELL", "buy", "sell"],
                         help="Order side")
    parser.add_argument("--type", dest="order_type", required=True,
                         choices=["MARKET", "LIMIT", "STOP_LIMIT",
                                  "market", "limit", "stop_limit"],
                         help="Order type")
    parser.add_argument("--quantity", required=True, help="Order quantity")
    parser.add_argument("--price", required=False, default=None,
                         help="Limit price (required for LIMIT and STOP_LIMIT)")
    parser.add_argument("--stop-price", dest="stop_price", required=False, default=None,
                         help="Stop trigger price (required for STOP_LIMIT)")
    parser.add_argument("--api-key", dest="api_key", default=None,
                         help="Overrides BINANCE_TESTNET_API_KEY env var")
    parser.add_argument("--api-secret", dest="api_secret", default=None,
                         help="Overrides BINANCE_TESTNET_API_SECRET env var")
    return parser


def get_credentials(args) -> tuple[str, str]:
    api_key = args.api_key or os.environ.get("BINANCE_TESTNET_API_KEY")
    api_secret = args.api_secret or os.environ.get("BINANCE_TESTNET_API_SECRET")

    if not api_key or not api_secret:
        print(
            "ERROR: Missing API credentials.\n"
            "Set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET "
            "environment variables, or pass --api-key / --api-secret.",
            file=sys.stderr,
        )
        sys.exit(1)

    return api_key, api_secret


def main(argv=None) -> int:
    logger = setup_logger()
    parser = build_parser()
    args = parser.parse_args(argv)

    api_key, api_secret = get_credentials(args)

    try:
        client = FuturesTestnetClient(api_key=api_key, api_secret=api_secret, logger=logger)
    except (BinanceClientError, BinanceNetworkError) as exc:
        print(f"ERROR: Could not initialize Binance client - {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        logger.exception("Unexpected error initializing Binance client")
        print(f"ERROR: Unexpected error initializing Binance client - {exc}", file=sys.stderr)
        return 1

    result = place_order(
        client=client,
        symbol=args.symbol,
        side=args.side,
        order_type=args.order_type,
        quantity=args.quantity,
        price=args.price,
        stop_price=args.stop_price,
        logger=logger,
    )

    print()
    print("\n".join(result.summary_lines()))
    print()

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
