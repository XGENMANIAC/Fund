"""
Solana RPC client with automatic fallback and rate-limit handling.

DISCLAIMER: This module is part of an educational system for Solana devnet only.
Do not use on mainnet without proper rate-limit agreements and compliance review.

Design notes:
- Cycles through a list of public RPC endpoints on failure or 429.
- Uses exponential backoff (tenacity) for transient errors.
- All calls include the commitment level ("confirmed" by default).
- WebSocket subscriptions for real-time log monitoring.
"""

import asyncio
import json
import os
import random
import time
from typing import Any, Callable, Optional

import aiohttp
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Default free public RPC endpoints (devnet + mainnet fallbacks for monitoring)
# ---------------------------------------------------------------------------
DEVNET_ENDPOINTS = [
    "https://api.devnet.solana.com",
    "https://devnet.helius-rpc.com",  # free tier, no key needed for public methods
]

MAINNET_ENDPOINTS = [
    # For MONITORING ONLY — reading public on-chain data
    # Do NOT send transactions to mainnet from this educational system
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.g.alchemy.com/v2/demo",  # Alchemy demo key (limited)
    "https://rpc.ankr.com/solana",                    # Ankr free tier
]


class RPCError(Exception):
    """Raised when all RPC endpoints fail after retries."""


class RateLimitError(Exception):
    """Raised when an endpoint returns HTTP 429."""


class SolanaRPCClient:
    """
    Async Solana JSON-RPC client with automatic endpoint rotation and retries.

    Usage:
        client = SolanaRPCClient(network="devnet")
        async with client:
            slot = await client.get_slot()
            token_info = await client.get_account_info(address)
    """

    def __init__(self, network: str = "devnet", endpoints: list[str] | None = None):
        if network not in ("devnet", "testnet", "mainnet-beta"):
            raise ValueError(f"Unknown network: {network}")

        self.network = network
        self._endpoints = endpoints or (
            DEVNET_ENDPOINTS if network == "devnet" else MAINNET_ENDPOINTS
        )
        self._endpoint_idx = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_count = 0

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def current_endpoint(self) -> str:
        return self._endpoints[self._endpoint_idx % len(self._endpoints)]

    def _rotate_endpoint(self):
        """Advance to the next endpoint in the rotation list."""
        self._endpoint_idx += 1
        logger.warning(
            f"Rotating to RPC endpoint: {self.current_endpoint}"
        )

    async def _raw_request(self, method: str, params: list) -> Any:
        """
        Send one JSON-RPC request, rotating endpoints on 429 or connection error.
        Raises RPCError if all endpoints fail.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_count,
            "method": method,
            "params": params,
        }
        self._request_count += 1

        for attempt in range(len(self._endpoints) * 2):
            endpoint = self.current_endpoint
            try:
                async with self._session.post(endpoint, json=payload) as resp:
                    if resp.status == 429:
                        logger.warning(f"Rate limited by {endpoint}, rotating.")
                        self._rotate_endpoint()
                        await asyncio.sleep(1.5 ** (attempt % 4))
                        continue

                    resp.raise_for_status()
                    data = await resp.json()

                    if "error" in data:
                        err = data["error"]
                        logger.error(f"RPC error from {endpoint}: {err}")
                        raise RPCError(f"RPC error: {err}")

                    return data.get("result")

            except aiohttp.ClientConnectionError as exc:
                logger.warning(f"Connection error to {endpoint}: {exc}")
                self._rotate_endpoint()
                await asyncio.sleep(0.5 * (attempt + 1))

        raise RPCError(
            f"All RPC endpoints failed after {len(self._endpoints) * 2} attempts"
        )

    # ------------------------------------------------------------------
    # Public RPC methods
    # ------------------------------------------------------------------

    async def get_slot(self) -> int:
        """Return the current confirmed slot."""
        return await self._raw_request("getSlot", [{"commitment": "confirmed"}])

    async def get_account_info(self, address: str, encoding: str = "jsonParsed") -> dict | None:
        """
        Fetch account info for a given public key address.
        Returns None if the account does not exist.
        """
        params = [
            address,
            {"encoding": encoding, "commitment": "confirmed"},
        ]
        return await self._raw_request("getAccountInfo", params)

    async def get_token_supply(self, mint_address: str) -> dict:
        """Return the total supply of an SPL token."""
        return await self._raw_request(
            "getTokenSupply", [mint_address, {"commitment": "confirmed"}]
        )

    async def get_token_largest_accounts(self, mint_address: str) -> list[dict]:
        """Return the largest token holders — used for concentration risk checks."""
        result = await self._raw_request(
            "getTokenLargestAccounts",
            [mint_address, {"commitment": "confirmed"}],
        )
        return result.get("value", []) if result else []

    async def get_signatures_for_address(
        self,
        address: str,
        limit: int = 10,
    ) -> list[dict]:
        """Return recent transaction signatures for a given address."""
        params = [
            address,
            {"limit": limit, "commitment": "confirmed"},
        ]
        return await self._raw_request("getSignaturesForAddress", params) or []

    async def get_program_accounts(
        self,
        program_id: str,
        filters: list | None = None,
    ) -> list[dict]:
        """
        Fetch all accounts owned by a program — useful for finding all pools.
        Warning: This can be very expensive on public RPCs; use sparingly.
        """
        params: list = [program_id, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        if filters:
            params[1]["filters"] = filters
        return await self._raw_request("getProgramAccounts", params) or []

    async def get_multiple_accounts(self, addresses: list[str]) -> list[dict | None]:
        """Batch-fetch account info for multiple addresses in one RPC call."""
        params = [addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        result = await self._raw_request("getMultipleAccounts", params)
        return result.get("value", []) if result else []

    async def get_balance(self, address: str) -> float:
        """Return the SOL balance of an address in SOL (not lamports)."""
        lamports = await self._raw_request(
            "getBalance", [address, {"commitment": "confirmed"}]
        )
        return (lamports or 0) / 1e9

    async def send_transaction(self, encoded_tx: str) -> str:
        """
        Broadcast a base64-encoded signed transaction.
        Returns the transaction signature string.
        """
        return await self._raw_request(
            "sendTransaction",
            [encoded_tx, {"encoding": "base64", "preflightCommitment": "confirmed"}],
        )

    async def confirm_transaction(self, signature: str, timeout: int = 60) -> bool:
        """
        Poll until a transaction is confirmed or timeout is reached.
        Returns True if confirmed, False if timeout or failed.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await self._raw_request(
                "getSignatureStatuses",
                [[signature], {"searchTransactionHistory": True}],
            )
            if result:
                statuses = result.get("value", [])
                if statuses and statuses[0]:
                    status = statuses[0]
                    if status.get("err"):
                        logger.error(f"Transaction {signature} failed: {status['err']}")
                        return False
                    if status.get("confirmationStatus") in ("confirmed", "finalized"):
                        return True
            await asyncio.sleep(2)

        logger.error(f"Transaction {signature} confirmation timed out after {timeout}s")
        return False


# ---------------------------------------------------------------------------
# WebSocket log subscription (for real-time new pool detection)
# ---------------------------------------------------------------------------

async def subscribe_program_logs(
    ws_url: str,
    program_id: str,
    callback: Callable[[dict], None],
    commitment: str = "confirmed",
):
    """
    Subscribe to program log events via WebSocket.

    This is how the on-chain monitor detects new Raydium pool creation events
    without polling. Each new log mentioning 'initialize' is passed to callback.

    Args:
        ws_url: WebSocket RPC endpoint (e.g. wss://api.devnet.solana.com).
        program_id: Program to watch (e.g. Raydium AMM program ID).
        callback: Async function called with each log notification dict.
        commitment: 'confirmed' or 'finalized'.
    """
    import websockets  # type: ignore — optional heavy dep, lazy import

    subscribe_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [program_id]},
            {"commitment": commitment},
        ],
    }

    retry_delay = 1
    while True:
        try:
            logger.info(f"Connecting to WebSocket: {ws_url}")
            async with websockets.connect(ws_url, ping_interval=20) as ws:
                await ws.send(json.dumps(subscribe_msg))
                logger.info(f"Subscribed to logs for program {program_id}")
                retry_delay = 1  # reset on successful connect

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        if msg.get("method") == "logsNotification":
                            await callback(msg["params"]["result"])
                    except json.JSONDecodeError:
                        logger.debug(f"Non-JSON WS message: {raw[:100]}")

        except Exception as exc:
            logger.warning(
                f"WebSocket error ({exc}), reconnecting in {retry_delay}s..."
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)  # cap at 60s


# ---------------------------------------------------------------------------
# Simple sync wrapper for use in non-async contexts
# ---------------------------------------------------------------------------

class SyncRPCClient:
    """
    Synchronous wrapper around SolanaRPCClient for scripts that don't use asyncio.
    Creates a private event loop internally.
    """

    def __init__(self, network: str = "devnet"):
        self.network = network
        self._loop = asyncio.new_event_loop()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def get_slot(self) -> int:
        async def _go():
            async with SolanaRPCClient(self.network) as client:
                return await client.get_slot()
        return self._run(_go())

    def get_balance(self, address: str) -> float:
        async def _go():
            async with SolanaRPCClient(self.network) as client:
                return await client.get_balance(address)
        return self._run(_go())

    def get_account_info(self, address: str) -> dict | None:
        async def _go():
            async with SolanaRPCClient(self.network) as client:
                return await client.get_account_info(address)
        return self._run(_go())
