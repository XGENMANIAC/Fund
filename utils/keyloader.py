"""
Secure wallet keypair loader.

Supports two sources (checked in order):
  1. WALLET_KEYPAIR_JSON env var — base64-encoded JSON array of the keypair bytes.
     Use this for cloud/Railway deployments so the private key never lives
     as a file on disk.
  2. WALLET_KEYPAIR_PATH env var — path to a local .json keypair file.

To generate the base64 value from an existing keypair file:

    python3 -c "
import base64, sys
print(base64.b64encode(open(sys.argv[1], 'rb').read()).decode())
" ~/.config/solana/mainnet.json

Paste that output into Railway → Variables → WALLET_KEYPAIR_JSON.
"""

import base64
import json
import os
import tempfile
from pathlib import Path

_cached_temp_path: str | None = None


def get_keypair_path() -> str:
    """
    Return a filesystem path to the keypair JSON file.

    If WALLET_KEYPAIR_JSON is set, the bytes are written to a temp file
    with mode 0o600 (owner-read-only) and that path is returned.
    The temp file is created once per process and reused on subsequent calls.
    """
    global _cached_temp_path

    raw_env = os.getenv("WALLET_KEYPAIR_JSON", "").strip()
    if raw_env:
        if _cached_temp_path and Path(_cached_temp_path).exists():
            return _cached_temp_path

        # Prefer base64 decode; fall back to treating the value as raw JSON.
        try:
            decoded = base64.b64decode(raw_env).decode("utf-8")
            keypair_data = json.loads(decoded)
        except Exception:
            keypair_data = json.loads(raw_env)

        fd, path = tempfile.mkstemp(suffix=".json", prefix="sol_wallet_")
        try:
            os.chmod(path, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(keypair_data, f)
        except Exception:
            os.close(fd)
            raise

        _cached_temp_path = path
        return path

    # Fall back to explicit file path.
    raw_path = os.getenv(
        "WALLET_KEYPAIR_PATH",
        str(Path.home() / ".config/solana/mainnet.json"),
    )
    return raw_path.replace("~", str(Path.home()))


def get_keypair_bytes() -> bytes:
    """Return the raw 64-byte secret key as bytes."""
    with open(get_keypair_path()) as f:
        data = json.load(f)
    return bytes(data)
