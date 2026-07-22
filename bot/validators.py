"""
validators.py

Input validation for CLI-supplied order parameters.
Raises ValidationError (a ValueError subclass) with a clear message
whenever input is invalid, so the CLI layer can catch a single
exception type and report it cleanly.
"""

import re

VALID_SIDES = {"BUY", "SELL"}
VALID_ORDER_TYPES = {"MARKET", "LIMIT", "STOP_LIMIT"}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")


class ValidationError(ValueError):
    """Raised when user-supplied order input fails validation."""


def validate_symbol(symbol: str) -> str:
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol is required (e.g. BTCUSDT).")
    symbol = symbol.strip().upper()
    if not SYMBOL_PATTERN.match(symbol):
        raise ValidationError(
            f"Invalid symbol '{symbol}'. Expected a format like 'BTCUSDT'."
        )
    return symbol


def validate_side(side: str) -> str:
    if not side or not isinstance(side, str):
        raise ValidationError("Side is required (BUY or SELL).")
    side = side.strip().upper()
    if side not in VALID_SIDES:
        raise ValidationError(f"Invalid side '{side}'. Must be one of {sorted(VALID_SIDES)}.")
    return side


def validate_order_type(order_type: str) -> str:
    if not order_type or not isinstance(order_type, str):
        raise ValidationError("Order type is required (MARKET, LIMIT, or STOP_LIMIT).")
    order_type = order_type.strip().upper()
    if order_type not in VALID_ORDER_TYPES:
        raise ValidationError(
            f"Invalid order type '{order_type}'. Must be one of {sorted(VALID_ORDER_TYPES)}."
        )
    return order_type


def validate_quantity(quantity) -> float:
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        raise ValidationError(f"Quantity must be a number, got '{quantity}'.")
    if quantity <= 0:
        raise ValidationError("Quantity must be greater than 0.")
    return quantity


def validate_price(price, required: bool) -> float | None:
    if price is None or price == "":
        if required:
            raise ValidationError("Price is required for LIMIT and STOP_LIMIT orders.")
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        raise ValidationError(f"Price must be a number, got '{price}'.")
    if price <= 0:
        raise ValidationError("Price must be greater than 0.")
    return price


def validate_stop_price(stop_price, required: bool) -> float | None:
    if stop_price is None or stop_price == "":
        if required:
            raise ValidationError("Stop price is required for STOP_LIMIT orders.")
        return None
    try:
        stop_price = float(stop_price)
    except (TypeError, ValueError):
        raise ValidationError(f"Stop price must be a number, got '{stop_price}'.")
    if stop_price <= 0:
        raise ValidationError("Stop price must be greater than 0.")
    return stop_price


def validate_order_input(symbol, side, order_type, quantity, price=None, stop_price=None) -> dict:
    """
    Validate a full set of order parameters at once and return a clean,
    normalized dict ready to be handed to the orders layer.
    """
    symbol = validate_symbol(symbol)
    side = validate_side(side)
    order_type = validate_order_type(order_type)
    quantity = validate_quantity(quantity)

    needs_price = order_type in {"LIMIT", "STOP_LIMIT"}
    needs_stop_price = order_type == "STOP_LIMIT"

    price = validate_price(price, required=needs_price)
    stop_price = validate_stop_price(stop_price, required=needs_stop_price)

    return {
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "quantity": quantity,
        "price": price,
        "stop_price": stop_price,
    }
