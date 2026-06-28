"""
Miscellaneous helper utilities.

DISCLAIMER: Educational system for Solana devnet only.
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_CONFIG_CACHE: dict | None = None


def load_config(path: str = "config.yaml") -> dict:
    """
    Load and cache the YAML config file.
    Falls back to config.example.yaml if config.yaml doesn't exist.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    config_path = Path(path)
    if not config_path.exists():
        config_path = Path("config.example.yaml")

    with open(config_path, "r") as f:
        _CONFIG_CACHE = yaml.safe_load(f)

    return _CONFIG_CACHE


def load_env(path: str = ".env") -> None:
    """Load .env file into os.environ. Safe to call multiple times."""
    load_dotenv(dotenv_path=path, override=False)


def get_config_value(key_path: str, default: Any = None) -> Any:
    """
    Get a nested config value using dot notation.
    Example: get_config_value("monitor.poll_interval")
    """
    cfg = load_config()
    keys = key_path.split(".")
    current = cfg
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return default
        current = current[k]
    return current


# ---------------------------------------------------------------------------
# Solana address helpers
# ---------------------------------------------------------------------------

def is_valid_solana_address(addr: str) -> bool:
    """Basic check: Solana addresses are 32–44 base58 chars."""
    if not addr or not isinstance(addr, str):
        return False
    # Base58 character set
    base58_chars = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return 32 <= len(addr) <= 44 and all(c in base58_chars for c in addr)


def shorten_address(addr: str, chars: int = 4) -> str:
    """Shorten a Solana address for display: AbCd...xYz0"""
    if not addr or len(addr) <= chars * 2:
        return addr
    return f"{addr[:chars]}...{addr[-chars:]}"


# ---------------------------------------------------------------------------
# Number formatting
# ---------------------------------------------------------------------------

def format_usd(amount: float) -> str:
    """Format a dollar amount with K/M/B suffix."""
    if amount >= 1_000_000_000:
        return f"${amount/1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"${amount/1_000_000:.2f}M"
    if amount >= 1_000:
        return f"${amount/1_000:.1f}K"
    return f"${amount:.2f}"


def format_pct(value: float, decimals: int = 1) -> str:
    """Format a 0–1 float as a percentage string."""
    return f"{value * 100:.{decimals}f}%"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


def utcnow_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return utcnow().isoformat()


def seconds_ago(iso_ts: str) -> float:
    """Return how many seconds ago an ISO 8601 timestamp was."""
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (utcnow() - dt).total_seconds()


def hours_ago(iso_ts: str) -> float:
    """Return how many hours ago an ISO 8601 timestamp was."""
    return seconds_ago(iso_ts) / 3600


# ---------------------------------------------------------------------------
# Data serialization helpers
# ---------------------------------------------------------------------------

def safe_json_load(text: str) -> dict | None:
    """Parse JSON without raising — returns None on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def to_serializable(obj: Any) -> Any:
    """Recursively convert an object to JSON-serializable types."""
    if isinstance(obj, dict):
        return {k: to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_serializable(i) for i in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return to_serializable(obj.__dict__)
    return obj


# ---------------------------------------------------------------------------
# Hashing / deduplication
# ---------------------------------------------------------------------------

def content_hash(*parts: str) -> str:
    """
    Stable hash of multiple string parts — used to deduplicate detections
    across monitor sources without storing full duplicates.
    """
    combined = "|".join(str(p) for p in parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def clean_symbol(raw: str) -> str:
    """Strip non-alphanumeric chars and uppercase — safe token symbol."""
    return re.sub(r"[^A-Za-z0-9]", "", raw).upper()[:10]


def clean_name(raw: str) -> str:
    """Strip disallowed chars from a token name, limit to 32 chars."""
    cleaned = re.sub(r"[^\w\s\-\.]", "", raw).strip()
    return cleaned[:32]


# ---------------------------------------------------------------------------
# Retry helper (simple sync, for non-async use)
# ---------------------------------------------------------------------------

def retry_with_backoff(
    fn,
    *args,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 32.0,
    exceptions: tuple = (Exception,),
    **kwargs,
):
    """
    Call fn(*args, **kwargs) up to max_attempts times with exponential backoff.
    Returns the result on success, re-raises the last exception on final failure.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except exceptions as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = min(base_delay * (2 ** attempt), max_delay)
                time.sleep(delay)
    raise last_exc


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------

def ensure_data_dir() -> Path:
    """Create the data/ directory if it doesn't exist and return its path."""
    d = Path("data")
    d.mkdir(exist_ok=True)
    return d
