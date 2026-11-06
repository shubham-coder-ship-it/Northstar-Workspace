"""
client.py

Thin wrapper around the Binance Futures Testnet REST API.

Wraps the `python-binance` Client so the rest of the application never
talks to the Binance SDK directly. This keeps the API/network concerns
in one place and makes the bot easy to test or swap transport later.
"""

import logging
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from requests.exceptions import RequestException

from bot.logging_config import setup_logger

FUTURES_TESTNET_BASE_URL = "https://testnet.binancefuture.com"


class BinanceClientError(Exception):
    """Raised when the Binance API call fails (bad request, auth, etc.)."""


class BinanceNetworkError(Exception):
    """Raised when the request never reached Binance (network/timeout)."""


class FuturesTestnetClient:
    """
    Wrapper around python-binance's Client configured for Binance
    Futures Testnet (USDT-M).
    """

    def __init__(self, api_key: str, api_secret: str, logger: Optional[logging.Logger] = None):
        self.logger = logger or setup_logger()
        self.logger.info("Initializing Binance Futures Testnet client")

        # Skip the unrelated spot ping and hard-pin Futures Testnet.
        try:
            self._client = Client(api_key, api_secret, testnet=True, ping=False, requests_params={"timeout": (5, 15)})
        except (BinanceAPIException, BinanceRequestException) as exc:
            self.logger.error("Binance initialization failed")
            raise BinanceClientError("Binance rejected the request. Check your testnet credentials and order parameters.") from exc
        except RequestException as exc:
            self.logger.error("Binance connection failed")
            raise BinanceNetworkError("Connection interrupted. Verify the order on Binance testnet before resubmitting.") from exc

        self._client.FUTURES_TESTNET_URL = f"{FUTURES_TESTNET_BASE_URL}/fapi"

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "GTC",
    ) -> dict:
        """
        Place an order on Binance Futures Testnet.

        order_type: "MARKET", "LIMIT", or "STOP_LIMIT" (mapped to
        Binance's STOP order type with a limit price).
        """
        params = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
        }

        if order_type == "MARKET":
            params["type"] = "MARKET"
        elif order_type == "LIMIT":
            params["type"] = "LIMIT"
            params["price"] = price
            params["timeInForce"] = time_in_force
        elif order_type == "STOP_LIMIT":
            # Binance futures uses "STOP" for a stop-limit order
            # (stopPrice triggers, price is the limit once triggered).
            params["type"] = "STOP"
            params["price"] = price
            params["stopPrice"] = stop_price
            params["timeInForce"] = time_in_force
        else:
            raise BinanceClientError(f"Unsupported order type: {order_type}")

        self.logger.info("Submitting testnet order | symbol=%s side=%s type=%s", symbol, side, order_type)

        try:
            response = self._client.futures_create_order(**params)
            self.logger.info("Testnet request completed")
            return response

        except (BinanceAPIException, BinanceRequestException) as exc:
            self.logger.error("Binance request rejected")
            raise BinanceClientError("Binance rejected the request. Check your testnet credentials and order parameters.") from exc

        except RequestException as exc:
            self.logger.error("Binance connection interrupted")
            raise BinanceNetworkError("Connection interrupted. Verify the order on Binance testnet before resubmitting.") from exc

    def get_order_status(self, symbol: str, order_id: int) -> dict:
        """Fetch the current status of a previously placed order."""
        self.logger.info(
            "API REQUEST | futures_get_order | symbol=%s order_id=%s", symbol, order_id
        )
        try:
            response = self._client.futures_get_order(symbol=symbol, orderId=order_id)
            self.logger.info("Testnet request completed")
            return response
        except (BinanceAPIException, BinanceRequestException) as exc:
            self.logger.error("Binance request rejected")
            raise BinanceClientError("Binance rejected the request. Check your testnet credentials and order parameters.") from exc
        except RequestException as exc:
            self.logger.error("Binance connection interrupted")
            raise BinanceNetworkError("Connection interrupted. Verify the order on Binance testnet before resubmitting.") from exc
