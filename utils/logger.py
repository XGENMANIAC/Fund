"""
Structured logging setup for the meme coin educational system.

DISCLAIMER: This module is part of an educational system for Solana devnet only.
Do not use in production or on mainnet without full legal and compliance review.
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from rich.logging import RichHandler
from rich.console import Console

console = Console()

_configured_loggers: set[str] = set()


def get_logger(name: str, level: str | None = None) -> logging.Logger:
    """
    Return a named logger with Rich console output + rotating file handler.

    Args:
        name: Logger name (usually __name__ of the calling module).
        level: Override log level string (DEBUG/INFO/WARNING/ERROR).
               Falls back to LOG_LEVEL env var, then INFO.

    Returns:
        Configured Logger instance.
    """
    if name in _configured_loggers:
        return logging.getLogger(name)

    log_level_str = level or os.getenv("LOG_LEVEL", "INFO")
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    # Avoid adding duplicate handlers when get_logger is called multiple times
    if logger.handlers:
        _configured_loggers.add(name)
        return logger

    # --- Rich console handler (colorful, readable) ---
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
    )
    rich_handler.setLevel(log_level)
    logger.addHandler(rich_handler)

    # --- Rotating file handler ---
    log_dir = Path("data")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "bot.log"

    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    # Prevent messages from propagating to the root logger
    logger.propagate = False

    _configured_loggers.add(name)
    return logger


def log_banner(logger: logging.Logger, text: str) -> None:
    """Print a prominent section banner in the log — useful for pipeline steps."""
    border = "=" * 60
    logger.info(border)
    logger.info(f"  {text}")
    logger.info(border)
