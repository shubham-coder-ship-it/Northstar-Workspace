"""
orders.py

Order placement logic. Sits between the CLI layer and the API client
layer: validates and normalizes input (via validators.py), calls the
client, and formats results for display.
"""

import logging
from typing import Optional

from bot.client import BinanceClientError, BinanceNetworkError, FuturesTestnetClient
from bot.logging_config import setup_logger
from bot.validators import ValidationError, validate_order_input


class OrderResult:
    """Simple container describing the outcome of an order request."""

    def __init__(self, success: bool, request: dict, response: Optional[dict] = None,
                 error: Optional[str] = None):
        self.success = success
        self.request = request
        self.response = response
        self.error = error

    def summary_lines(self) -> list[str]:
        lines = []
        lines.append("Order Request Summary")
        lines.append("-" * 40)
        for key, value in self.request.items():
            if value is not None:
                lines.append(f"  {key:12}: {value}")

        lines.append("")
        if self.success and self.response:
            lines.append("Order Response")
            lines.append("-" * 40)
            lines.append(f"  orderId     : {self.response.get('orderId')}")
            lines.append(f"  status      : {self.response.get('status')}")
            lines.append(f"  executedQty : {self.response.get('executedQty')}")
            avg_price = self.response.get("avgPrice")
            if avg_price is not None:
                lines.append(f"  avgPrice    : {avg_price}")
            lines.append("")
            lines.append("Result: SUCCESS - order placed.")
        else:
            lines.append("Result: FAILURE")
            lines.append(f"  reason: {self.error}")

        return lines


def place_order(
    client: FuturesTestnetClient,
    symbol: str,
    side: str,
    order_type: str,
    quantity,
    price=None,
    stop_price=None,
    logger: Optional[logging.Logger] = None,
) -> OrderResult:
    """
    Validate input, then place the order via the client. Returns an
    OrderResult regardless of outcome (never raises for expected
    failure modes) so the CLI can print a clean summary either way.
    """
    logger = logger or setup_logger()

    try:
        clean = validate_order_input(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
        )
    except ValidationError as exc:
        logger.warning("Validation failed: %s", exc)
        return OrderResult(success=False, request={
            "symbol": symbol, "side": side, "order_type": order_type,
            "quantity": quantity, "price": price, "stop_price": stop_price,
        }, error=f"Invalid input - {exc}")

    try:
        response = client.place_order(
            symbol=clean["symbol"],
            side=clean["side"],
            order_type=clean["order_type"],
            quantity=clean["quantity"],
            price=clean["price"],
            stop_price=clean["stop_price"],
        )
        return OrderResult(success=True, request=clean, response=response)

    except BinanceClientError as exc:
        logger.error("Order failed (API error): %s", exc)
        return OrderResult(success=False, request=clean, error=f"Binance API error - {exc}")

    except BinanceNetworkError as exc:
        logger.error("Order failed (network error): %s", exc)
        return OrderResult(success=False, request=clean, error=f"Network error - {exc}")

    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        logger.error("Unexpected error while placing order")
        return OrderResult(success=False, request=clean, error="Unexpected error. Verify the order on Binance testnet before resubmitting.")
