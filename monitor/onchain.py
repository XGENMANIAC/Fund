"""
On-chain monitor — subscribes to Solana program logs via WebSocket RPC.

DISCLAIMER: This module is part of an educational system for Solana devnet only.
On-chain monitoring on mainnet-beta requires a reliable, low-latency RPC node.
Free public RPCs enforce strict rate limits and WebSocket connection limits.
For educational mainnet monitoring, consider free-tier Helius or QuickNode.

What we monitor:
  - Raydium AMM program logs: detect new pool/market initialization events.
  - Token Metadata program: detect new mint + metadata creation.

Event flow:
  1. A new pump.fun token "graduates" and migrates to Raydium.
  2. Raydium emits an 'initialize2' instruction log.
  3. We parse the log to extract the new pool/mint addresses.
  4. We emit a detection event for the analysis pipeline.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

from utils.logger import get_logger
from utils.helpers import content_hash, load_config, utcnow_iso
from utils.rpc import SolanaRPCClient

logger = get_logger(__name__)

# Raydium AMM v4 program ID (same on mainnet; devnet may vary)
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
# Metaplex Token Metadata program
TOKEN_METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"


@dataclass
class OnChainAlert:
    """Detection from on-chain log monitoring."""

    source: str = "onchain"
    event_type: str = ""           # "new_pool" | "new_mint" | "new_metadata"
    signature: str = ""             # Transaction signature
    slot: int = 0
    token_address: str = ""
    pool_address: str = ""
    base_vault: str = ""
    quote_vault: str = ""
    log_messages: list[str] = field(default_factory=list)
    detected_at: str = field(default_factory=utcnow_iso)

    @property
    def content_hash(self) -> str:
        return content_hash("onchain", self.signature, self.token_address)


class OnChainMonitor:
    """
    Listens for on-chain events by subscribing to program logs via RPC WebSocket.

    Emits OnChainAlert objects for:
      - New Raydium AMM pool initialization (most important)
      - New SPL token mint creation
    """

    def __init__(self):
        cfg = load_config()
        onchain_cfg = cfg.get("monitor", {}).get("onchain", {})
        self._raydium_program = onchain_cfg.get("raydium_amm_program", RAYDIUM_AMM_V4)
        self._commitment = onchain_cfg.get("commitment", "confirmed")
        rpc_cfg = cfg.get("rpc", {})
        self._ws_url = rpc_cfg.get("ws_endpoint", "wss://api.devnet.solana.com")
        self._network = cfg.get("network", "devnet")
        self._alert_queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    # ------------------------------------------------------------------
    # Main monitoring loop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the WebSocket log subscription in the background."""
        self._running = True
        asyncio.create_task(self._monitor_raydium_logs())
        logger.info(f"On-chain monitor started (network: {self._network})")

    async def stop(self) -> None:
        self._running = False

    async def get_alerts(self, max_wait: float = 1.0) -> list[OnChainAlert]:
        """
        Drain all currently queued alerts without blocking longer than max_wait.
        """
        alerts = []
        deadline = asyncio.get_event_loop().time() + max_wait
        while asyncio.get_event_loop().time() < deadline:
            try:
                alert = self._alert_queue.get_nowait()
                alerts.append(alert)
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
        return alerts

    # ------------------------------------------------------------------
    # WebSocket subscription
    # ------------------------------------------------------------------

    async def _monitor_raydium_logs(self) -> None:
        """
        Subscribe to Raydium AMM log events and push OnChainAlerts to the queue.
        Reconnects automatically on disconnect.
        """
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.error("websockets package not installed. Run: pip install websockets")
            return

        subscribe_payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [self._raydium_program]},
                {"commitment": self._commitment},
            ],
        })

        retry_delay = 2
        while self._running:
            try:
                logger.info(f"Connecting to Solana WebSocket: {self._ws_url}")
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=20,
                    max_size=2 ** 23,  # 8 MB max message
                ) as ws:
                    await ws.send(subscribe_payload)
                    logger.info(f"Subscribed to Raydium logs on {self._ws_url}")
                    retry_delay = 2

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            await self._handle_log_notification(msg)
                        except json.JSONDecodeError:
                            pass

            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    f"WebSocket disconnected ({exc}), "
                    f"retrying in {retry_delay}s"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def _handle_log_notification(self, msg: dict) -> None:
        """
        Parse a logsNotification message and emit an OnChainAlert if it's
        a new pool creation event.
        """
        if msg.get("method") != "logsNotification":
            return

        params = msg.get("params", {})
        result = params.get("result", {})
        context = result.get("context", {})
        value = result.get("value", {})

        signature = value.get("signature", "")
        logs = value.get("logs", [])
        slot = context.get("slot", 0)
        err = value.get("err")

        # Skip failed transactions
        if err:
            return

        # Check if this is a pool initialization
        alert = self._detect_new_pool(logs, signature, slot)
        if alert:
            logger.info(
                f"[ON-CHAIN] New Raydium pool detected: "
                f"sig={signature[:8]}... slot={slot}"
            )
            await self._alert_queue.put(alert)

    def _detect_new_pool(
        self, logs: list[str], signature: str, slot: int
    ) -> OnChainAlert | None:
        """
        Analyze log messages for Raydium 'initialize2' pool creation signature.
        Returns an OnChainAlert if found, None otherwise.

        The Raydium AMM program emits specific log patterns during initialization:
          "Program log: initialize2: InitializeInstruction2"
        followed by ray_log output containing base/quote vault addresses.
        """
        is_init = any(
            "initialize2" in log.lower() or "InitializeInstruction2" in log
            for log in logs
        )
        if not is_init:
            return None

        # Extract addresses from log messages using heuristics
        # Real implementation would deserialize the instruction data
        token_address = ""
        pool_address = ""

        for log in logs:
            # Look for patterns like "token: <address>" or "mint: <address>"
            addr_match = re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", log)
            if addr_match and not token_address:
                candidate = addr_match.group(0)
                # Raydium logs often contain the mint address
                if "mint" in log.lower() or "token" in log.lower():
                    token_address = candidate
            if "pool" in log.lower() and addr_match and not pool_address:
                pool_address = addr_match.group(0)

        return OnChainAlert(
            event_type="new_pool",
            signature=signature,
            slot=slot,
            token_address=token_address,
            pool_address=pool_address,
            log_messages=logs[:10],  # store first 10 log lines
        )

    # ------------------------------------------------------------------
    # Polling fallback (for environments where WebSocket is unavailable)
    # ------------------------------------------------------------------

    async def poll_new_signatures(
        self,
        program_address: str = RAYDIUM_AMM_V4,
        last_signature: str = "",
    ) -> tuple[list[OnChainAlert], str]:
        """
        Alternative to WebSocket: poll for new signatures of the Raydium program.
        Returns (new_alerts, latest_signature).

        Limitation: polling creates significantly more RPC calls and adds 5–30s latency.
        Useful as a fallback when WebSocket is rate-limited.
        """
        async with SolanaRPCClient(self._network) as client:
            params: dict = {"limit": 20}
            if last_signature:
                params["until"] = last_signature

            sigs = await client.get_signatures_for_address(
                program_address, limit=20
            )

        if not sigs:
            return [], last_signature

        new_latest = sigs[0].get("signature", last_signature)
        alerts = []

        for sig_info in sigs:
            if sig_info.get("err"):
                continue
            sig = sig_info.get("signature", "")
            # We'd need getTransaction to get log messages — omitted for rate limit reasons
            # In practice, use WebSocket subscription which gives us logs in real-time
            alerts.append(OnChainAlert(
                event_type="new_transaction",
                signature=sig,
                slot=sig_info.get("slot", 0),
            ))

        return alerts, new_latest


import re  # used above but imported at module level for clarity
