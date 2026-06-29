"""
Secure wallet keypair loader.

Supports three sources (checked in order):

  1. WALLET_PRIVATE_KEY — base58-encoded private key exported directly from
     Solflare, Phantom, or the Solana CLI (`solana-keygen show --outfile` → copy
     the "Private Key" line).  This is the most convenient option for browser
     wallet users.  The value is a base58 string of the 64-byte ed25519 keypair.

  2. WALLET_KEYPAIR_JSON — base64-encoded JSON array of the 64 keypair bytes.
     Use this for Railway / cloud deployments.

  3. WALLET_KEYPAIR_PATH — path to a local .json keypair file (solana-keygen
     default format).  Falls back to ~/.config/solana/mainnet.json.

--- Solflare / Phantom export steps ---

  In Solflare:
    Settings → Security → Export Private Key → copy the base58 string
  Set it as:
    WALLET_PRIVATE_KEY=<paste here>

  To convert to WALLET_KEYPAIR_JSON instead (for Railway):
    python3 -c "
import base64, json, sys
import base58  # pip install base58
raw = base58.b58decode(sys.argv[1])
print(base64.b64encode(json.dumps(list(raw)).encode()).decode())
" YOUR_BASE58_KEY_HERE
"""

import base64
import json
import os
import tempfile
from pathlib import Path

_cached_temp_path: str | None = None


def _decode_base58_key(b58_value: str) -> list[int]:
    """Decode a base58 private key (Solflare/Phantom export) to a byte list."""
    try:
        import base58 as _base58  # type: ignore
        raw = _base58.b58decode(b58_value)
    except ImportError:
        # Fallback: use solders which ships its own base58 decoder
        try:
            from solders.keypair import Keypair  # type: ignore
            kp = Keypair.from_base58_string(b58_value)
            raw = bytes(kp)
        except Exception as exc:
            raise ImportError(
                "Install 'base58' (pip install base58) to use WALLET_PRIVATE_KEY "
                "with a Solflare/Phantom export."
            ) from exc
    if len(raw) not in (32, 64):
        raise ValueError(
            f"Expected 32 or 64 bytes from WALLET_PRIVATE_KEY, got {len(raw)}. "
            "Make sure you copied the full private key from Solflare."
        )
    if len(raw) == 32:
        # Seed-only: expand to full 64-byte keypair via solders
        from solders.keypair import Keypair  # type: ignore
        raw = bytes(Keypair.from_seed(raw))
    return list(raw)


def _write_temp_keypair(keypair_data: list[int]) -> str:
    """Write keypair byte list to a mode-0600 temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="sol_wallet_")
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(keypair_data, f)
    except Exception:
        os.close(fd)
        raise
    return path


def get_keypair_path() -> str:
    """
    Return a filesystem path to the keypair JSON file.

    Sources are checked in priority order:
      WALLET_PRIVATE_KEY  → base58 string (Solflare/Phantom export)
      WALLET_KEYPAIR_JSON → base64-encoded JSON byte array
      WALLET_KEYPAIR_PATH → local .json file path
    """
    global _cached_temp_path

    if _cached_temp_path and Path(_cached_temp_path).exists():
        return _cached_temp_path

    # 1. Solflare / Phantom base58 private key
    b58_key = os.getenv("WALLET_PRIVATE_KEY", "").strip()
    if b58_key:
        keypair_data = _decode_base58_key(b58_key)
        _cached_temp_path = _write_temp_keypair(keypair_data)
        return _cached_temp_path

    # 2. Base64-encoded JSON array (Railway / cloud deployments)
    raw_env = os.getenv("WALLET_KEYPAIR_JSON", "").strip()
    if raw_env:
        try:
            decoded = base64.b64decode(raw_env).decode("utf-8")
            keypair_data = json.loads(decoded)
        except Exception:
            keypair_data = json.loads(raw_env)
        _cached_temp_path = _write_temp_keypair(keypair_data)
        return _cached_temp_path

    # 3. Local file path
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
