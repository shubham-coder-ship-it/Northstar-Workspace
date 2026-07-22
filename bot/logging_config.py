"""
logging_config.py

Central logging configuration for the trading bot.
Logs API requests, responses, and errors to both a log file and the console.
"""

import logging
import os

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_FILE = os.path.join(LOG_DIR, "trading_bot.log")


def setup_logger(name: str = "trading_bot") -> logging.Logger:
    """
    Configure and return a logger that writes to both a log file and
    the console. Safe to call multiple times (won't duplicate handlers).
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        # Logger already configured (e.g. called again in the same process)
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler - captures everything, used for audit / grading evidence
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Console handler - keeps terminal output readable
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger
